"""Designer repair and input de-duplication; no network or paid model calls."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nanobot.agent.context_selection import (
    design_validation_issues,
    design_wire_payload,
    parse_design_proposal,
)
from nanobot.providers.base import LLMResponse, ToolCallRequest

from .test_context_selection import env as env
from .test_context_selection import event, plan, response, select


def test_complete_json_fence_preserves_strict_contract():
    original = plan()
    parsed = parse_design_proposal('```json\n' + original.model_dump_json() + '\n```')
    assert parsed == original
    bad = original.model_dump()
    bad['request_type'] = 'invented'
    with pytest.raises(ValidationError):
        parse_design_proposal('```json\n' + json.dumps(bad) + '\n```')
    with pytest.raises(ValueError):
        parse_design_proposal('Instructions outside the JSON\n```json\n' + original.model_dump_json() + '\n```')


async def test_repair_receives_invalid_proposal_field_and_issue_without_logging_text(env):
    event(env, '2+2?')
    bad = plan().model_dump()
    bad['request_type'] = 'SENSITIVE_TEST_VALUE'
    _, provider, selected = await select(env, plan(), responses=[
        LLMResponse(content=None, tool_calls=[ToolCallRequest('bad', 'submit_context_design', bad)]),
        response(plan())])
    assert selected.blocked is None
    assert len(provider.requests) == 2
    repair = provider.requests[1]['messages']
    assert 'SENSITIVE_TEST_VALUE' in repair[-2]['content']
    assert 'request_type' in repair[-1]['content']
    assert 'literal_error' in repair[-1]['content']
    assert all(r['tool_choice'] == 'required' for r in provider.requests)
    trace = json.dumps(selected.trace.report())
    assert 'SENSITIVE_TEST_VALUE' not in trace
    assert 'design_schema:request_type:literal_error' in trace


def test_unknown_schema_key_is_not_logged_as_a_path():
    bad = plan().model_dump()
    bad['SENSITIVE_UNKNOWN_KEY'] = 'SENSITIVE_VALUE'
    with pytest.raises(ValidationError) as error:
        parse_design_proposal(bad)
    issues = design_validation_issues(error.value)
    assert issues == [{'path': ['unknown_field'], 'code': 'extra_forbidden'}]
    assert 'SENSITIVE' not in json.dumps(issues)


def test_wire_dedup_preserves_all_unique_constraints_sources_and_session_scope():
    text = 'Read only. No deployment. Preserve the budget.'
    payload = {'request': 'Continue', 'sources': [
        {'event_id': 'u1', 'actor': 'user', 'session_id': 's1', 'conversation_id': 's1',
         'redacted': False, 'parent_event_id': None, 'content': text},
        {'event_id': 'u2', 'actor': 'user', 'session_id': 's2', 'conversation_id': 'c2',
         'redacted': True, 'content': None}],
        'task_state': {'valid_constraint_candidates': [
            {'source_id': 'u1', 'text': text, 'status': 'user_original'},
            {'source_id': 'absent', 'text': 'A unique historical rule', 'status': 'user_original'}],
            'goal': 'Keep goal', 'unfinished': ['Verify remaining work'], 'last_event_id': 'u1'}}
    before = json.dumps(payload)
    wire = design_wire_payload(payload)
    assert json.dumps(payload) == before
    assert wire['sources'][0]['content'] == text
    assert wire['sources'][0]['session_id'] == 's1'
    assert wire['sources'][1]['conversation_id'] == 'c2'
    assert wire['sources'][1]['redacted'] is True
    candidates = wire['task_state']['valid_constraint_candidates']
    assert candidates[0] == {'source_id': 'u1', 'status': 'user_original'}
    assert candidates[1]['text'] == 'A unique historical rule'
    assert wire['task_state']['unfinished'] == ['Verify remaining work']
    assert len(json.dumps(wire)) < len(before)


async def test_design_failure_does_not_claim_missing_execution_proof(env):
    event(env, '2+2?')
    _, _, selected = await select(env, plan(), responses=[LLMResponse(content='bad'), LLMResponse(content='bad')])
    assert selected.design is None
    assert selected.blocked == 'design_validation_failed'
    final, status = selected.verdict('Design failed before execution')
    assert status == 'incomplete'
    assert 'design_validation_failed' in final
    assert 'completion_evidence_invalid_or_insufficient' not in final


async def test_xai_wire_forces_single_design_function_and_repairs_locally(env, monkeypatch):
    from types import SimpleNamespace

    from nanobot.agent.context_selection import ContextSelector
    from nanobot.config.schema import ContextSelectionConfig
    from nanobot.providers.xai_grok_provider import XAIGrokProvider

    monkeypatch.setattr('nanobot.providers.xai_grok_provider.get_xai_oauth_token',
                        lambda **kwargs: SimpleNamespace(access='synthetic-local-token'))
    wire_requests = []
    bad = plan().model_dump()
    bad['task_relation'] = 'invalid_relation'

    async def fake_request(url, headers, body, **kwargs):
        wire_requests.append(body)
        raw = bad if len(wire_requests) == 1 else plan().model_dump()
        return '', [ToolCallRequest('design-' + str(len(wire_requests)), 'submit_context_design', raw)], 'tool_calls', {}, None

    monkeypatch.setattr('nanobot.providers.xai_grok_provider._request_xai', fake_request)
    event(env, '2+2?')
    selector = ContextSelector(env[0], ContextSelectionConfig(enabled=True))
    selected = await selector.select(provider=XAIGrokProvider(hosted_search=False),
        model='xai-grok/grok-4.7', request='2+2?', scope=env[1], session_id='s',
        registry=env[2], skills=env[3], rules='Mandatory permissions', request_id='mock-xai-wire')
    assert selected.blocked is None
    assert selected.design == plan()
    assert len(wire_requests) == 2
    for body in wire_requests:
        assert body['tool_choice'] == {'type': 'function', 'name': 'submit_context_design'}
        assert body['parallel_tool_calls'] is False
        assert body['reasoning']['effort'] == 'low'
        assert [tool['name'] for tool in body['tools']] == ['submit_context_design']
        assert body['tools'][0]['parameters']['additionalProperties'] is False
    assert 'task_relation' in json.dumps(wire_requests[1]['input'])
    assert 'literal_error' in json.dumps(wire_requests[1]['input'])
    assert selected.trace.calls[0]['status'] == 'succeeded'
    assert len(selected.trace.calls) == 2


async def test_design_sse_to_schema_to_return_with_slow_cleanup_has_one_timeline(env, monkeypatch):
    from types import SimpleNamespace

    from nanobot.agent.context_selection import ContextSelector
    from nanobot.config.schema import ContextSelectionConfig
    from nanobot.providers.call_observation import _cleanup_tasks
    from nanobot.providers.xai_grok_provider import XAIGrokProvider
    from tests.providers.test_response_stream_lifecycle import Events, FakeClient, terminal

    raw = plan().model_dump()
    output = {'type': 'function_call', 'call_id': 'design', 'id': 'fc1',
              'name': 'submit_context_design', 'arguments': json.dumps(raw)}
    client = FakeClient(Events([terminal(output=[output])], hold=True), 'hold')
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.httpx.AsyncClient', lambda **kw: client)
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.get_xai_oauth_token',
                        lambda **kw: SimpleNamespace(access='synthetic-token'))
    event(env, '2+2?')
    selector = ContextSelector(env[0], ContextSelectionConfig(enabled=True))
    selected = await selector.select(provider=XAIGrokProvider(hosted_search=False),
        model='xai-grok/grok-4.7', request='2+2?', scope=env[1], session_id='s',
        registry=env[2], skills=env[3], rules='Mandatory permissions', request_id='mock-terminal-design')
    assert selected.design == plan()
    assert selected.blocked is None
    assert len(selected.trace.calls) == 1
    call = selected.trace.calls[0]
    assert call['status'] == 'succeeded'
    milestones = call['milestones']
    phases = [m['phase'] for m in milestones]
    for phase in ('input_assembly_started', 'input_assembly_finished', 'request_dispatch', 'connection_established',
                  'response_headers',
                  'first_event', 'first_output', 'terminal_event', 'result_extracted',
                  'schema_validation_finished', 'provider_call_returned'):
        assert phase in phases
    assert all(m['attempt_id'] == call['attempt_id'] for m in milestones)
    assert [m['monotonic_ms'] for m in milestones] == sorted(m['monotonic_ms'] for m in milestones)
    hashes = [m['schema_hash'] for m in milestones if 'schema_hash' in m]
    assert len(hashes) == 2 and hashes[0] == hashes[1]
    options = next(m for m in milestones if m['phase'] == 'wire_options')
    assert options['reasoning_effort'] == 'low'
    assert options['max_output_tokens'] == 3000
    assert options['stream'] is True
    assert options['tool_choice'] == {'type': 'function', 'name': 'submit_context_design'}
    assert call['component_estimated_tokens']['execution_tool_definitions'] == 0
    client.release.set()
    await __import__('asyncio').gather(*list(_cleanup_tasks))
    phases = [m['phase'] for m in call['milestones']]
    assert phases.index('provider_call_returned') < phases.index('cleanup_finished')
    # Post-return cleanup milestones are journaled off the event loop; wait for the writer.
    await selected.trace.flush_events()
    with env[0].connect() as db:
        saved = json.loads(db.execute('SELECT payload FROM context_model_attempts WHERE attempt_id=?',
                          (call['attempt_id'],)).fetchone()[0])
    assert saved['milestones'][-1]['phase'] == 'cleanup_finished'
    received = [m for m in saved['milestones'] if m['phase'] == 'stream_event_received']
    processed = [m for m in saved['milestones'] if m['phase'] == 'stream_event_processed']
    assert received and len(received) == len(processed)
    assert all(m['attempt_id'] == call['attempt_id'] for m in received + processed)
    assert all('item_type' in m and 'event_sequence' in m for m in received)
    assert all(m['processing_ms'] >= 0 and m['status'] == 'processed' for m in processed)
    assert 'synthetic-token' not in json.dumps(saved)


async def test_partial_valid_json_is_rejected_when_provider_reports_output_limit(env):
    event(env, '2+2?')
    partial = response(plan())
    partial.finish_reason = 'length'
    _, provider, selected = await select(env, plan(), responses=[partial])
    assert len(provider.requests) == 1
    assert selected.design is None
    assert selected.blocked == 'design_incomplete_response'
    assert 'design_recoveries' not in selected.trace.counters


def test_designer_does_not_inherit_executor_output_or_memory_write_contract(tmp_path):
    from nanobot.agent.context import ContextBuilder
    from nanobot.utils.prompt_templates import render_template

    unique_rule = 'All writes require explicit approval. Keep this exact user rule.'
    (tmp_path / 'AGENTS.md').write_text(unique_rule)
    builder = ContextBuilder(tmp_path)
    designer = builder.build_design_system_prompt(channel='telegram')
    executor = builder.build_selected_system_prompt(channel='telegram')
    gates = render_template('agent/_snippets/permission_gates.md').strip()
    assert gates in designer and gates in executor
    # Execution bootstrap files are executor-only; the designer gets design identity only.
    assert unique_rule not in designer and unique_rule in executor
    assert 'submit_context_design' in designer
    assert 'Reply directly with text' not in designer
    assert '## Topic Memory' not in designer
    assert 'General Tool Contract' not in designer
    assert 'Reply directly with text' in executor
    assert '## Topic Memory' in executor
    assert len(designer) < len(executor)


def test_wire_references_duplicate_request_without_removing_original_source_id():
    payload = {'request': 'Preserve this exact request', 'sources': [
        {'event_id': 'actual-user-event', 'actor': 'user', 'content': 'Preserve this exact request'}]}
    wire = design_wire_payload(payload)
    assert wire['request'] == payload['request']
    assert wire['sources'][0] == {'event_id': 'actual-user-event', 'actor': 'user', 'content_ref': 'request'}
    assert payload['sources'][0]['content'] == payload['request']


async def test_design_timeout_marks_cancel_transfer_and_never_validates_partial_output(env, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from nanobot.agent.context_selection import ContextSelector
    from nanobot.config.schema import ContextSelectionConfig
    from nanobot.providers.call_observation import _cleanup_tasks
    from nanobot.providers.xai_grok_provider import XAIGrokProvider
    from tests.providers.test_response_stream_lifecycle import Events, FakeClient

    partial = {'type': 'response.output_item.done', 'item': {'type': 'function_call',
        'call_id': 'partial', 'id': 'fc1', 'name': 'submit_context_design', 'arguments': plan().model_dump_json()}}
    client = FakeClient(Events([partial], hold=True))
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.httpx.AsyncClient', lambda **kw: client)
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.get_xai_oauth_token',
                        lambda **kw: SimpleNamespace(access='synthetic-token'))
    event(env, '2+2?')
    selector = ContextSelector(env[0], ContextSelectionConfig(enabled=True, design_timeout_seconds=1))
    selected = await selector.select(provider=XAIGrokProvider(hosted_search=False),
        model='xai-grok/grok-4.7', request='2+2?', scope=env[1], session_id='s',
        registry=env[2], skills=env[3], rules='Mandatory permissions', request_id='mock-cancel-transfer')
    assert selected.blocked == 'design_timeout_initial'
    assert selected.design is None
    assert len(selected.trace.calls) == 1
    call = selected.trace.calls[0]
    assert call['status'] == 'timed_out'
    assert call['provider_usage'] is None
    phases = {m['phase']: m for m in call['milestones']}
    assert 'first_output' in phases
    assert 'schema_validation_finished' not in phases
    assert phases['cancellation_received']['monotonic_ms'] <= phases['design_deadline_expired']['monotonic_ms']
    assert phases['provider_call_returned']['monotonic_ms'] <= phases['design_call_returned']['monotonic_ms']
    await asyncio.gather(*list(_cleanup_tasks))


async def test_eof_failure_is_not_repaired_or_retried_as_a_design(env, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from nanobot.agent.context_selection import ContextSelector
    from nanobot.config.schema import ContextSelectionConfig
    from nanobot.providers.call_observation import _cleanup_tasks
    from nanobot.providers.xai_grok_provider import XAIGrokProvider
    from tests.providers.test_response_stream_lifecycle import Events, FakeClient

    entries = []
    def fake_client(**kwargs):
        entries.append(True)
        return FakeClient(Events([]))
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.httpx.AsyncClient', fake_client)
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.get_xai_oauth_token',
                        lambda **kw: SimpleNamespace(access='synthetic-token'))
    event(env, '2+2?')
    selected = await ContextSelector(env[0], ContextSelectionConfig(enabled=True)).select(
        provider=XAIGrokProvider(hosted_search=False), model='xai-grok/grok-4.7', request='2+2?',
        scope=env[1], session_id='s', registry=env[2], skills=env[3], rules='Mandatory permissions',
        request_id='mock-eof-once')
    assert selected.design is None
    assert selected.blocked == 'design_provider_error'
    assert len(entries) == 1
    assert 'design_recoveries' not in selected.trace.counters
    await asyncio.gather(*list(_cleanup_tasks))


async def test_user_cancel_just_before_terminal_keeps_cancelled_attempt(env, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from nanobot.agent.context_selection import ContextSelector
    from nanobot.config.schema import ContextSelectionConfig
    from nanobot.providers.call_observation import _cleanup_tasks
    from nanobot.providers.xai_grok_provider import XAIGrokProvider
    from tests.providers.test_response_stream_lifecycle import Events, FakeClient, terminal

    ready = asyncio.Event()
    release = asyncio.Event()
    class BeforeTerminal(Events):
        async def aiter_lines(self):
            ready.set()
            await release.wait()
            yield 'data: ' + json.dumps(terminal())
            yield ''
    client = FakeClient(BeforeTerminal([]))
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.httpx.AsyncClient', lambda **kw: client)
    monkeypatch.setattr('nanobot.providers.xai_grok_provider.get_xai_oauth_token',
                        lambda **kw: SimpleNamespace(access='synthetic-token'))
    event(env, '2+2?')
    run = asyncio.create_task(ContextSelector(env[0], ContextSelectionConfig(enabled=True)).select(
        provider=XAIGrokProvider(hosted_search=False), model='xai-grok/grok-4.7', request='2+2?',
        scope=env[1], session_id='s', registry=env[2], skills=env[3], rules='Mandatory permissions',
        request_id='mock-user-cancel'))
    await ready.wait()
    run.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await run
    await asyncio.gather(*list(_cleanup_tasks))
    with env[0].connect() as db:
        saved = json.loads(db.execute('SELECT payload FROM context_model_attempts WHERE request_id=?',
                                     ('mock-user-cancel',)).fetchone()[0])
    assert saved['status'] == 'cancelled'
    assert saved['provider_usage'] is None
    phases = [m['phase'] for m in saved['milestones']]
    assert 'cancellation_received' in phases
    assert 'terminal_event' not in phases
    assert 'schema_validation_finished' not in phases


async def test_direct_continuation_loads_non_keyword_original_before_model(env):
    from nanobot.memory.task_context import TaskContextStore

    original = event(env, 'Use plain text, offline, with a zero budget.')
    tasks = TaskContextStore(env[0])
    state = tasks.create(env[1], 's', original, 'Explain notes', ['Answer'])
    state = tasks.incorporate_users(state, [original])
    tasks.save(env[1], state, expected_version=state['version'])
    for i in range(90):
        event(env, f'Unrelated historical response {i}', actor='assistant')
    event(env, 'Explain the tradeoffs.')
    selector, provider, selected = await select(env, plan(
        task_id=state['task_id'], task_relation='continue', answer='Plain text works offline.'),
        request='Explain the tradeoffs.')
    assert selected.blocked is None
    assert len(provider.requests) == 1
    wire = provider.requests[0]['messages'][1]['content']
    assert original.content in wire
    assert original.event_id in wire
    payload = selector.minimal_input('Explain the tradeoffs.', env[1], 's', env[2], env[3])
    selector.restore_design_sources(payload, env[1], 's')
    selector.restore_design_sources(payload, env[1], 's')
    assert sum(e['event_id'] == original.event_id for e in payload['sources']) == 1
    assert len(payload['sources']) < 90


def test_unavailable_original_still_fails_direct_validation():
    from nanobot.agent.context_selection import validate_meaning

    proposal = plan(task_id='task', task_relation='continue')
    payload = {'request': 'Explain notes', 'sources': [], 'capabilities': [],
               'task_candidates': [{'task_id': 'task'}],
               'task_state': {'task_id': 'task', 'constraint_source_ids': ['missing']}}
    assert 'unloaded_task_constraints' in validate_meaning(proposal, payload)
