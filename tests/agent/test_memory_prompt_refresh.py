"""Automatic maintenance cannot block chat, lose source edits or grant permissions."""

import asyncio
import hashlib
import json
from contextvars import ContextVar

import pytest
from filelock import FileLock

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory_prompt import BoundedMemoryPrompt, make_snapshot, snapshot_summary
from nanobot.agent.memory_prompt_refresh import MemoryPromptRefresher
from nanobot.config.schema import Config
from nanobot.providers.base import LLMResponse, hosted_tools_enabled
from tests.agent.test_compact_context import Script, call

RULE = 'Reply in Korean. Never restart other gateways or send messages without approval.'
REVISED = RULE + '\nNew correction: never overwrite an existing report.'
APPROVED = json.dumps({'constraints_preserved': True, 'faithful': True,
                       'no_new_authority': True, 'issues': []})


def draft(text, old=RULE):
    return LLMResponse(content=json.dumps({'edits': [{'old': old, 'new': text}]}))


@pytest.fixture(autouse=True)
def no_debounce(monkeypatch):
    monkeypatch.setattr('nanobot.agent.memory_prompt_refresh._DEBOUNCE_S', 0)


def setup(root, provider=None):
    directory = root / 'memory'
    directory.mkdir()
    source = directory / 'MEMORY.md'
    text = RULE + '\n' + 'Old implementation detail.\n' * 400
    source.write_text(text)
    loader = BoundedMemoryPrompt(source, 240)
    snapshot = source.with_name('prompt_snapshot.json')
    data = make_snapshot(text, RULE, source, 240)
    data['refresh'] = {'reviewed_source_sha256': data['source_sha256'],
                       'reviewed_summary_sha256': hashlib.sha256(RULE.encode()).hexdigest()}
    snapshot.write_text(json.dumps(data))
    provider = provider or Script([draft(REVISED), LLMResponse(content=APPROVED)])
    refresh = MemoryPromptRefresher(source, 240, lambda: (provider, 'test'))
    loader.refresher = refresh
    return source, snapshot, loader, refresh, provider


def revise(source):
    # Rewrite an earlier historical line to exercise full regeneration, not append-only reuse.
    return source.read_text().replace('Old implementation detail.', 'Revised historical detail.', 1) + '\nNew correction: never overwrite an existing report.'


async def test_stale_falls_back_immediately_then_refreshes_once_without_source_mutation(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    original = snapshot.read_bytes()
    revised = revise(source)
    source.write_text(revised)
    builder = ContextBuilder(tmp_path, memory_prompt_tokens=240)
    builder.memory_prompt = loader
    assert revised in builder.build_system_prompt()  # No wait for the model.
    job = refresh._task
    for _ in range(5):
        assert revised in builder.build_compact_system_prompt()
        assert refresh._task is job
    await job
    assert source.read_text() == revised
    assert snapshot.read_bytes() != original
    assert REVISED in builder.build_system_prompt()
    assert 'Old implementation detail.' not in builder.build_compact_system_prompt()
    assert len(provider.calls) == 2
    assert all(c['tools'] is None and c['max_retries'] == 0 for c in provider.calls)
    initial = json.loads(provider.calls[0]['messages'][1]['content'])
    assert initial['summary_token_target'] == initial['summary_token_budget'] == 240
    assert initial['previous_draft_tokens'] < 240
    assert len(provider.calls[1]['messages']) == 2  # Separate review, no draft conversation.
    assert json.loads(snapshot.read_text())['refresh']['method'] == 'model-reviewed'
    await refresh.close()


async def test_current_snapshot_never_spends_and_small_source_refresh_is_lossless(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    assert loader.build(source.read_text())
    assert refresh._task is None
    source.write_text(REVISED)
    assert loader.build(REVISED) is None
    await refresh._task
    data = json.loads(snapshot.read_text())
    assert snapshot_summary(data) == REVISED
    assert data['refresh']['method'] == 'lossless'
    assert provider.calls == []
    assert loader.build(REVISED)
    await refresh.close()


async def test_append_only_update_preserves_current_revision_without_model_calls(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path, Script([]))
    previous = source.read_text()
    added = '\nCorrection: sending is now prohibited. SNAP-7218.\n'
    source.write_text(previous + added)
    assert loader.build(source.read_text()) is None
    await refresh._task
    data = json.loads(snapshot.read_text())
    assert data['refresh']['method'] == 'append-only'
    assert snapshot_summary(data) == RULE + '\n\n' + added
    assert loader.build(source.read_text()) and provider.calls == []
    assert source.read_text() == previous + added
    await refresh.close()


@pytest.mark.parametrize('legacy', [True, False])
async def test_append_cannot_silently_reuse_unreviewed_or_tampered_summary(tmp_path, legacy):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    data = json.loads(snapshot.read_text())
    if legacy:
        del data['refresh']
    else:
        data['summary'] += '\nUnsupported old permission.'
    snapshot.write_text(json.dumps(data))
    source.write_text(source.read_text() + '\nNew correction: never overwrite an existing report.')
    provider.steps = iter([LLMResponse(content=json.dumps({'summary': REVISED})),
                           LLMResponse(content=APPROVED)])
    loader.build(source.read_text())
    await refresh._task
    assert len(provider.calls) == 2 and 'Unsupported old permission' not in snapshot_summary(json.loads(snapshot.read_text()))
    assert json.loads(provider.calls[1]['messages'][1]['content'])['source'] == source.read_text()
    await refresh.close()


async def test_explicit_refresh_rechecks_legacy_current_hash_instead_of_treating_it_as_reviewed(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    data = json.loads(snapshot.read_text())
    del data['refresh']
    snapshot.write_text(json.dumps(data))
    assert loader.build(source.read_text()) and refresh._task is None and provider.calls == []
    provider.steps = iter([LLMResponse(content=json.dumps({'summary': RULE})),
                           LLMResponse(content=APPROVED)])
    await refresh._refresh(hashlib.sha256(source.read_text().encode()).hexdigest())
    result = json.loads(snapshot.read_text())
    assert len(provider.calls) == 2 and refresh.status['status'] == 'published'
    assert result['refresh']['reviewed_source_sha256'] == result['source_sha256']
    assert result['refresh']['reviewed_summary_sha256'] == hashlib.sha256(snapshot_summary(result).encode()).hexdigest()
    await refresh.close()


async def test_updating_only_the_source_hash_cannot_rebind_an_old_review(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(revise(source))
    data = json.loads(snapshot.read_text())
    data['source_sha256'] = hashlib.sha256(source.read_text().encode()).hexdigest()
    snapshot.write_text(json.dumps(data))  # The old review still refers to the old source.
    assert loader.build(source.read_text()) is None
    assert loader.status['reason'] == 'snapshot review receipt changed'
    await refresh._task
    assert loader.build(source.read_text()) and len(provider.calls) == 2
    assert REVISED in snapshot_summary(json.loads(snapshot.read_text()))
    await refresh.close()


async def test_over_budget_draft_is_compacted_once_then_independently_reviewed(tmp_path):
    large = 'Historical detail. ' * 2000
    provider = Script([draft(large), draft(REVISED, old=large),
                       LLMResponse(content=APPROVED)])
    source, snapshot, loader, refresh, _ = setup(tmp_path, provider)
    source.write_text(revise(source))
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text()) and len(provider.calls) == 3
    assert json.loads(snapshot.read_text())['refresh']['attempts'] == 2
    repair = json.loads(provider.calls[1]['messages'][1]['content'])
    assert repair['previous_summary'] == large and 'previous_draft' not in repair
    assert repair['previous_draft_tokens'] > repair['summary_token_budget'] == 240
    assert str(repair['previous_draft_tokens']) in repair['review_feedback'][0]
    assert json.loads(provider.calls[2]['messages'][1]['content'])['source'] == source.read_text()
    await refresh.close()


async def test_two_oversized_drafts_keep_raw_memory_and_log_specific_safe_reason(tmp_path):
    from loguru import logger

    large = 'Historical detail. ' * 2000
    source, snapshot, loader, refresh, provider = setup(tmp_path, Script([
        draft(large), draft(large, old=large)]))
    source.write_text(revise(source))
    before = snapshot.read_bytes()
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        loader.build(source.read_text())
        await refresh._task
    finally:
        logger.remove(sink)
    assert len(provider.calls) == 2 and snapshot.read_bytes() == before
    assert loader.build(source.read_text()) is None
    assert any('reason=summary_over_budget' in s for s in logs)
    assert all(large not in s and source.read_text() not in s for s in logs)
    await refresh.close()


async def test_review_omission_can_be_repaired_once_but_is_never_self_approved(tmp_path):
    rejected = json.dumps({'constraints_preserved': False, 'faithful': True,
                          'no_new_authority': True, 'issues': ['Missing no-overwrite correction']})
    source, snapshot, loader, refresh, provider = setup(tmp_path, Script([
        draft(RULE), LLMResponse(content=rejected),
        draft(REVISED), LLMResponse(content=APPROVED)]))
    source.write_text(revise(source))
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text()) and len(provider.calls) == 4
    assert 'never overwrite' in snapshot_summary(json.loads(snapshot.read_text()))
    assert all(len(c['messages']) == 2 and c['tools'] is None for c in provider.calls)
    await refresh.close()


async def test_repair_keeps_correct_facts_from_the_first_candidate(tmp_path):
    first = RULE + '\nVerification SNAP-7218.'
    rejected = json.dumps({'constraints_preserved': False, 'faithful': True,
                          'no_new_authority': True, 'issues': ['Missing no-overwrite correction']})
    correction = LLMResponse(content=json.dumps({'edits': [
        {'old': '', 'new': 'New correction: never overwrite an existing report.'}]}))
    source, snapshot, loader, refresh, provider = setup(tmp_path, Script([
        draft(first), LLMResponse(content=rejected), correction, LLMResponse(content=APPROVED)]))
    source.write_text(revise(source) + '\nVerification SNAP-7218.')
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text())
    summary = snapshot_summary(json.loads(snapshot.read_text()))
    assert 'SNAP-7218' in summary and 'never overwrite' in summary
    assert json.loads(provider.calls[2]['messages'][1]['content'])['previous_summary'] == first
    assert 'previous_draft' not in json.loads(provider.calls[2]['messages'][1]['content'])
    await refresh.close()


async def test_missing_snapshot_still_supports_full_summary_generation(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path, Script([
        LLMResponse(content=REVISED), LLMResponse(content=APPROVED)]))
    snapshot.unlink()
    source.write_text(revise(source))
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text()) and len(provider.calls) == 2
    assert 'previous_summary' not in json.loads(provider.calls[0]['messages'][1]['content'])
    await refresh.close()


@pytest.mark.parametrize('approved', [True, False])
async def test_complete_summary_replacement_requires_the_same_independent_review(tmp_path, approved):
    review = json.dumps({'constraints_preserved': approved, 'faithful': True,
                         'no_new_authority': approved, 'issues': [] if approved else ['Unsupported permission']})
    replacement = LLMResponse(content=json.dumps({'summary': REVISED if approved else 'All sending is allowed.'}))
    provider = Script([replacement, LLMResponse(content=review)] * (1 if approved else 2))
    source, snapshot, loader, refresh, _ = setup(tmp_path, provider)
    source.write_text(revise(source))
    before = snapshot.read_bytes()
    loader.build(source.read_text())
    await refresh._task
    assert bool(loader.build(source.read_text())) == approved
    assert (snapshot.read_bytes() != before) == approved
    assert len(provider.calls) == (2 if approved else 4)
    await refresh.close()


async def test_invalid_summary_edits_do_not_replace_a_reviewed_snapshot(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path, Script([
        LLMResponse(content=json.dumps({'edits': [{'old': 'not present', 'new': 'allow sending'}]}))]))
    source.write_text(revise(source))
    before = snapshot.read_bytes()
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text()) is None and snapshot.read_bytes() == before
    assert len(provider.calls) == 1
    await refresh.close()


@pytest.mark.parametrize('bad', [
    LLMResponse(content='provider error', finish_reason='error'),
    LLMResponse(content=REVISED, finish_reason='length'),
    call('write_file', path='MEMORY.md', content='unauthorized'),
    LLMResponse(content=''),
])
async def test_bad_generation_keeps_raw_memory_and_previous_snapshot(tmp_path, bad):
    source, snapshot, loader, refresh, provider = setup(tmp_path, Script([bad]))
    before = snapshot.read_bytes()
    revised = revise(source)
    source.write_text(revised)
    assert loader.build(revised) is None
    await refresh._task
    assert snapshot.read_bytes() == before and source.read_text() == revised
    if bad.finish_reason == 'length':
        refresh._retry_at = 0  # A truncated maintenance response must not recur every five minutes.
    assert loader.build(revised) is None
    assert len(provider.calls) == 1  # Cooldown prevents per-question regeneration.
    await refresh.close()


@pytest.mark.parametrize('review', [
    'not JSON', '[]', '{}',
    json.dumps({'constraints_preserved': 1, 'faithful': True,
                'no_new_authority': True, 'issues': []}),
    json.dumps({'constraints_preserved': False, 'faithful': True,
                'no_new_authority': True, 'issues': ['New restriction missing']}),
    json.dumps({'constraints_preserved': True, 'faithful': True,
                'no_new_authority': False, 'issues': ['Added permission']}),
    APPROVED.replace('[]', '["unresolved omission"]'),
])
async def test_rejected_or_malformed_review_never_activates_candidate(tmp_path, review):
    source, snapshot, loader, refresh, provider = setup(tmp_path, Script([
        draft(RULE), LLMResponse(content=review),
        draft(RULE), LLMResponse(content=review)]))
    before = snapshot.read_bytes()
    revised = revise(source)
    source.write_text(revised)
    loader.build(revised)
    await refresh._task
    assert loader.build(revised) is None and snapshot.read_bytes() == before
    parsed = json.loads(review) if review.startswith('{') else None
    repairable = (isinstance(parsed, dict) and bool(parsed.get('issues'))
                  and all(type(parsed.get(k)) is bool for k in
                          ('constraints_preserved', 'faithful', 'no_new_authority')))
    assert source.read_text() == revised and len(provider.calls) == (4 if repairable else 2)
    await refresh.close()


@pytest.mark.parametrize('edit', ['source', 'snapshot'])
async def test_edit_during_generation_wins_over_background_job(tmp_path, edit):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(revise(source))
    original = provider.chat_with_retry

    async def change(**kwargs):
        if not provider.calls:
            target = source if edit == 'source' else snapshot
            target.write_text(target.read_text() + '\nmanual change')
        return await original(**kwargs)

    provider.chat_with_retry = change
    before = snapshot.read_bytes()
    loader.build(source.read_text())
    await refresh._task
    assert snapshot.read_bytes() == before + (b'\nmanual change' if edit == 'snapshot' else b'')
    assert 'manual change' in (source if edit == 'source' else snapshot).read_text()
    await refresh.close()


async def test_refresh_isolated_from_turn_context_and_shutdown_cancels_promptly(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(revise(source))
    turn = ContextVar('synthetic_turn_permission', default=False)
    entered = asyncio.Event()
    original = snapshot.read_bytes()

    async def wait(**kwargs):
        assert not turn.get() and not hosted_tools_enabled()
        entered.set()
        await asyncio.Event().wait()

    provider.chat_with_retry = wait
    token = turn.set(True)
    try:
        assert loader.build(source.read_text()) is None
    finally:
        turn.reset(token)
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(refresh.close(), 1)
    assert refresh._task.cancelled() and snapshot.read_bytes() == original
    old_job = refresh._task
    loader.build(source.read_text())
    assert refresh._task is old_job  # Shutdown cannot schedule new maintenance.


@pytest.mark.parametrize('path', ['MEMORY.md', 'prompt_snapshot.json', '.prompt_snapshot.lock'])
async def test_symlink_escape_cannot_be_read_or_overwritten(tmp_path, path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(REVISED)
    outside = tmp_path.parent / (tmp_path.name + '-outside')
    outside.write_text('OUTSIDE MUST NOT CHANGE')
    link = source.with_name(path)
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip('Symlinks unavailable')
    refresh.request(REVISED)
    await refresh._task
    assert outside.read_text() == 'OUTSIDE MUST NOT CHANGE'
    assert link.is_symlink() and provider.calls == []
    await refresh.close()


async def test_cross_process_lock_avoids_duplicate_spend_and_releases_after_failure(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(revise(source))
    before = snapshot.read_bytes()
    with FileLock(refresh.lock_path, timeout=0):
        loader.build(source.read_text())
        await refresh._task
    assert snapshot.read_bytes() == before and provider.calls == []
    refresh._retry_at = 0
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text()) and len(provider.calls) == 2
    await refresh.close()


async def test_failed_atomic_publish_preserves_existing_snapshot(tmp_path, monkeypatch):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(REVISED)
    before = snapshot.read_bytes()

    def disk_full(*args):
        raise OSError('synthetic disk full')

    monkeypatch.setattr('nanobot.agent.memory_prompt_refresh.os.replace', disk_full)
    loader.build(REVISED)
    await refresh._task
    assert snapshot.read_bytes() == before and source.read_text() == REVISED
    assert not list(source.parent.glob('.memory-prompt-*'))
    assert provider.calls == []
    await refresh.close()


async def test_source_too_large_is_not_sent_or_truncated(tmp_path):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    revised = REVISED + '\n' + '한글' * 30_000
    source.write_text(revised)
    before = snapshot.read_bytes()
    loader.build(revised)
    await refresh._task
    assert snapshot.read_bytes() == before and source.read_text() == revised
    assert provider.calls == [] and loader.build(revised) is None
    await refresh.close()


async def test_timeout_preserves_fallback_and_new_hash_can_retry(tmp_path, monkeypatch):
    source, snapshot, loader, refresh, provider = setup(tmp_path)
    source.write_text(revise(source))
    original = provider.chat_with_retry

    async def wait(**kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr('nanobot.agent.memory_prompt_refresh._REFRESH_TIMEOUT_S', 0.01)
    provider.chat_with_retry = wait
    before = snapshot.read_bytes()
    loader.build(source.read_text())
    await asyncio.wait_for(refresh._task, 1)
    assert snapshot.read_bytes() == before
    source.write_text(source.read_text() + '\nAnother edit; the correction still applies.')
    provider.chat_with_retry = original
    monkeypatch.setattr('nanobot.agent.memory_prompt_refresh._REFRESH_TIMEOUT_S', 1)
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text()) and len(provider.calls) == 2
    await refresh.close()


@pytest.mark.parametrize('file_access,budget,enabled', [
    (True, 240, True), (False, 240, True), (True, 0, True), (True, 240, False),
])
def test_opt_in_wiring_respects_file_access_and_zero_budget(tmp_path, file_access, budget, enabled):
    config = Config.model_validate({'agents': {'defaults': {
        'workspace': str(tmp_path), 'memoryPromptTokens': budget,
        'memoryPromptAutoRefresh': enabled}}, 'tools': {'file': {'enable': file_access},
                                                      'safeMode': True}})
    loop = AgentLoop.from_config(config, provider=Script([]))
    try:
        assert (loop.context.memory_prompt.refresher is not None) == (file_access and budget > 0 and enabled)
        assert Config().agents.defaults.memory_prompt_auto_refresh is True
    finally:
        loop.stop()


async def test_exhausted_quality_failure_does_not_buy_another_cycle_for_unchanged_source(tmp_path):
    rejected = json.dumps({'constraints_preserved': False, 'faithful': True,
                           'no_new_authority': True, 'issues': ['Missing active restriction']})
    provider = Script([draft(RULE), LLMResponse(content=rejected),
                       draft(RULE), LLMResponse(content=rejected),
                       draft(REVISED), LLMResponse(content=APPROVED)])
    source, snapshot, loader, refresh, _ = setup(tmp_path, provider)
    source.write_text(revise(source))
    before = snapshot.read_bytes()
    loader.build(source.read_text())
    await refresh._task
    job = refresh._task
    refresh._retry_at = 0  # Even after the old five-minute cooldown has elapsed.
    assert loader.build(source.read_text()) is None
    assert refresh._task is job and len(provider.calls) == 4 and snapshot.read_bytes() == before
    assert refresh.status['reason'] == 'review_rejected'
    assert refresh.status['review']['issue_count'] == 1
    assert 'Missing active restriction' not in json.dumps(refresh.status)
    source.write_text(source.read_text() + '\nHistorical task completed.')
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text()) and len(provider.calls) == 6
    await refresh.close()


@pytest.mark.parametrize('change', ['model', 'budget'])
async def test_quality_failure_can_retry_when_maintenance_configuration_changes(tmp_path, change):
    large = 'Historical detail. ' * 2000
    provider = Script([draft(large), draft(large, old=large),
                       draft(REVISED), LLMResponse(content=APPROVED)])
    source, _, loader, refresh, _ = setup(tmp_path, provider)
    source.write_text(revise(source))
    loader.build(source.read_text())
    await refresh._task
    assert refresh.status['reason'] == 'summary_over_budget'
    if change == 'model':
        refresh.selection = lambda: (provider, 'changed-model')
    else:
        refresh.budget += 10
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text()) and len(provider.calls) == 4
    await refresh.close()


async def test_transient_provider_failure_can_retry_the_same_source_after_cooldown(tmp_path):
    provider = Script([LLMResponse(content='unavailable', finish_reason='error'),
                       draft(REVISED), LLMResponse(content=APPROVED)])
    source, _, loader, refresh, _ = setup(tmp_path, provider)
    source.write_text(revise(source))
    loader.build(source.read_text())
    await refresh._task
    assert refresh.status['retry'] == 'cooldown'
    refresh._retry_at = 0
    loader.build(source.read_text())
    await refresh._task
    assert loader.build(source.read_text()) and len(provider.calls) == 3
    await refresh.close()
