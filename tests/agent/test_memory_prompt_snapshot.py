"""Readable snapshots preserve exact prompt text and the existing acquisition contract."""

import json

import pytest

from nanobot.agent.memory_prompt import (
    BoundedMemoryPrompt,
    append_only_summary,
    apply_summary_edits,
    encode_snapshot,
    make_snapshot,
    snapshot_summary,
)
from nanobot.agent.task_note import Target, TaskNote, TaskUpdate, active_task_note
from nanobot.agent.tools.context import current_tool_output_limit
from nanobot.agent.tools.filesystem import ReadFileTool


def test_legacy_and_chunked_snapshots_render_identical_memory(tmp_path):
    source = tmp_path / 'MEMORY.md'
    text = 'Current source: no sending without approval.'
    source.write_text(text)
    summary = '한국어로 답한다. Never send without approval.\n\n' + 'emoji 😀 exact identifiers; ' * 1000
    data = make_snapshot(text, summary, source, 20000)
    snapshot = source.with_name('prompt_snapshot.json')
    snapshot.write_text(json.dumps(data))
    loader = BoundedMemoryPrompt(source, 20000)
    legacy = loader.build(text)
    encoded = encode_snapshot(data)
    snapshot.write_bytes(encoded)
    stored = json.loads(encoded)
    assert stored['version'] == 2 and 'summary' not in stored
    assert max(map(len, stored['summary_chunks'])) <= 1024
    assert max(map(len, encoded.decode().splitlines())) < 7000
    assert snapshot_summary(stored) == summary
    assert loader.build(text) == legacy
    assert source.read_text() == text


@pytest.mark.parametrize('chunks', [None, [], 'text', [None], [123], ['x' * 1025]])
def test_malformed_chunks_keep_complete_current_source(tmp_path, chunks):
    source = tmp_path / 'MEMORY.md'
    text = 'New current restriction: never send.'
    source.write_text(text)
    data = make_snapshot(text, text, source, 200)
    data.update(version=2, summary_chunks=chunks)
    source.with_name('prompt_snapshot.json').write_text(json.dumps(data))
    assert BoundedMemoryPrompt(source, 200).build(text) is None
    assert source.read_text() == text


@pytest.mark.parametrize('ending', ['', '\n', '\r\n'])
def test_append_reuse_requires_the_exact_original_prefix(tmp_path, ending):
    source = tmp_path / 'MEMORY.md'
    previous = 'old fact\nCurrent rule: ask before sending.' + ending
    summary = 'Current rule: ask before sending.'
    data = make_snapshot(previous, summary, source, 200)
    addition = '\nCorrection: sending is prohibited. Verification SNAP-7218.\n'
    assert append_only_summary(previous + addition, data) == summary + '\n\n' + addition
    assert append_only_summary(previous.replace('ask', 'never') + addition, data) is None
    assert append_only_summary('prefix changed\n' + previous + addition, data) is None
    data['source_sha256'] = '0' * 64
    assert append_only_summary(previous + addition, data) is None


def test_empty_original_cannot_supply_a_reviewed_prefix(tmp_path):
    data = make_snapshot('', 'An unrelated remembered rule.', tmp_path / 'MEMORY.md', 200)
    assert append_only_summary('\nNew actual memory.', data) is None


@pytest.mark.parametrize('edits', [
    [], {'edits': 'bad'}, {'edits': [None]}, {'edits': [{'old': 'missing', 'new': 'new'}]},
    {'edits': [{'old': 'x', 'new': 'new'}]}, {'edits': [{'old': 3, 'new': 'new'}]},
    {'edits': [{'old': '', 'new': 'new', 'extra': True}]},
    {'edits': [{'old': '', 'new': 'new'}] * 21},
    {'summary': ''}, {'summary': '  '}, {'summary': None}, {'summary': ['not text']},
    {'summary': 'replacement', 'edits': []}, {'summary': 'replacement', 'approved': True},
])
def test_proposed_summary_edits_must_be_unambiguous_and_bounded(edits):
    with pytest.raises(ValueError):
        apply_summary_edits('x x: previous approved rule.', edits)


def test_exact_summary_edits_keep_order_and_allow_explicit_corrections():
    base = 'Old permission: may send. Keep personas separate.'
    result = apply_summary_edits(base, {'edits': [
        {'old': 'Old permission: may send.', 'new': 'Correction: sending is prohibited.'},
        {'old': '', 'new': 'Verification SNAP-7218.'},
    ]})
    assert result == 'Correction: sending is prohibited. Keep personas separate.\n\nVerification SNAP-7218.'
    assert apply_summary_edits(base, {'edits': []}) == base
    assert apply_summary_edits(base, {'summary': 'New concise rules.'}) == 'New concise rules.'


async def test_chunked_json_fully_readable_without_raising_tool_budget_or_weakening_completion(tmp_path):
    memory = tmp_path / 'memory'
    memory.mkdir()
    source = memory / 'MEMORY.md'
    text = 'Reply in Korean. Never send project data.'
    source.write_text(text)
    summary = 'Exact policy and retained historical reference. ' * 600
    data = make_snapshot(text, summary, source, 20000)
    snapshot = memory / 'prompt_snapshot.json'
    snapshot.write_text(json.dumps(data))
    reader = ReadFileTool(workspace=tmp_path, restrict_to_workspace=True)
    note = TaskNote([{'role': 'user', 'content': 'memory/prompt_snapshot.json 파일을 읽고 확인해줘.'}], tmp_path, {})
    target = Target(path='memory/prompt_snapshot.json', selection_reason='explicit request')
    token = current_tool_output_limit.set(16000)
    task_token = active_task_note.set(note)
    try:
        first = await reader.execute(path=str(snapshot), force=True)
        note.observe('read_file', {'path': str(snapshot)}, first, reader, 'ok')
        assert not note.covered(target)  # Legacy JSON's long string cannot be acquired.
        snapshot.write_bytes(encode_snapshot(data))
        offset, reads = 1, 0
        while True:
            result = await reader.execute(path=str(snapshot), offset=offset, force=True)
            assert len(str(result)) <= 16000
            evidence = result.read_evidence
            assert evidence['end'] >= offset
            note.observe('read_file', {'path': str(snapshot)}, result, reader, 'ok')
            reads += 1
            if evidence['end'] == evidence['total']:
                break
            offset = evidence['end'] + 1
            assert reads < 10
        assert note.covered(target) and reads <= 4
        accepted = json.loads(note.accept(TaskUpdate(request_id=note.request_id, intent='new',
            goal='Read the requested snapshot', targets=[target], source='files',
            analysis_complete=True, status='completed', explanation='Read all ranges',
            final_answer='검토본 전체를 읽고 확인했습니다.')))
        assert accepted['defects'] == []
    finally:
        active_task_note.reset(task_token)
        current_tool_output_limit.reset(token)
