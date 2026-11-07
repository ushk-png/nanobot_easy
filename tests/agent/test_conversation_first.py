"""Control-flow checks with synthetic model outputs and real loop/tools/storage.

These establish routing and execution behavior, not model language accuracy.
"""
from pathlib import Path

import pytest

from nanobot.agent.context_selection import ContextSelector
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.bus.events import InboundMessage
from nanobot.security.workspace_access import WorkspaceScopeResolver

from .t51_harness import Session, calls, final


def ordinary(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    s.loop.context_selection_config.enabled = False
    # Retaining the dormant contract must not make it an implicit text-triggered gate.
    s.loop.context_selection_config.time_allocation_contract_enabled = True
    policy = Path(__file__).parents[2] / 'docs/runtime-profiles/testy-conversation-policy.md'
    (tmp_path / 'AGENTS.md').write_text(policy.read_text())

    async def forbidden(*args, **kwargs):
        raise AssertionError('mandatory designer entered ordinary conversation')

    monkeypatch.setattr(ContextSelector, 'select', forbidden)
    return s


async def reply(s, request, *responses):
    s.mock.steps.extend(responses)
    return await s.turn(request)


async def test_answer_and_amendment_use_recent_dialogue_without_task_projection(tmp_path, monkeypatch):
    s = ordinary(tmp_path, monkeypatch)
    first = '총 70분. 독서 20분 고정. 남은 시간을 쓰기와 듣기에 균등 배분해 주세요.'
    answer = '독서 20분, 쓰기 25분, 듣기 25분입니다. 합계 70분입니다.'
    follow = '총 시간은 그대로 두고 독서를 30분으로 변경해 주세요. 나머지는 같은 조건으로 배분해 주세요.'
    revised = '독서 30분, 쓰기 20분, 듣기 20분입니다. 합계 70분입니다.'
    assert await reply(s, first, final(answer)) == answer
    assert await reply(s, follow, final(revised)) == revised
    messages = s.mock.requests[-1]['messages']
    assert any(m['role'] == 'assistant' and answer in str(m['content']) for m in messages)
    assert any(m['role'] == 'user' and first in str(m['content']) for m in messages)
    assert len(s.mock.requests) == 2 and not s.selected and not s.tasks()
    saved = s.loop.sessions.get_or_create('cli:s').messages
    assert [m['content'] for m in saved if m['role'] == 'assistant'] == [answer, revised]


@pytest.mark.parametrize('request_text,answer', [
    ('독서를 30분으로 바꾸지 마세요. 기존 20분을 유지해 주세요.', '독서 20분을 유지하겠습니다.'),
    ('총 50분을 영어 회화와 수학 문제 풀이에 균등 배분해 주세요.', '영어 회화 25분, 수학 문제 풀이 25분입니다.'),
    ('총45분에 20/15/15분을 고정하고 싶어요.', '고정 시간의 합이 50분입니다. 총 시간을 50분으로 바꿀까요?'),
    ('설명만 해 주세요. 저장하거나 실행하지 마세요.', '계획은 답변으로 제시하고, 일정 등록은 별도 실행입니다.'),
])
async def test_language_and_questions_do_not_enter_numeric_parser(tmp_path, monkeypatch, request_text, answer):
    s = ordinary(tmp_path, monkeypatch)
    assert await reply(s, request_text, final(answer)) == answer
    assert len(s.mock.requests) == 1 and not s.tasks()
    assert not (tmp_path / 'memory/topics').exists()


async def test_missing_history_can_be_read_through_real_memory_tool(tmp_path, monkeypatch):
    s = ordinary(tmp_path, monkeypatch)
    msg = InboundMessage(channel='cli', sender_id='owner', chat_id='s', content='')
    scope = s.loop._memory_scope_for_message(msg)
    event = s.loop.conversation_memory.append_event(
        scope=scope, conversation_id='cli:old', session_id='cli:old', actor='user',
        event_type='USER_MESSAGE', content='Project Cedar meeting is at 14:30.')
    answer = '이전에 정한 Cedar 회의 시간은 14:30입니다.'
    assert await reply(s, '예전에 정한 Cedar 회의 시간이 언제였지?',
        calls(('search', 'search_memory', {'query': 'Cedar', 'limit': 5})),
        final(answer)) == answer
    assert len(s.mock.requests) == 2 and not s.tasks()
    tool_messages = [m for m in s.mock.requests[-1]['messages'] if m['role'] == 'tool']
    assert any(event.event_id in str(m['content']) and '14:30' in str(m['content']) for m in tool_messages)


async def test_authorized_write_and_readback_without_completion_bookkeeping(tmp_path, monkeypatch):
    s = ordinary(tmp_path, monkeypatch)
    s.loop.tools.register(WriteFileTool(workspace=tmp_path, allowed_dir=tmp_path))
    s.loop.tools.register(ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path))
    target = tmp_path / 'note.txt'
    params = {'path': str(target), 'content': 'Cedar 14:30', 'intent_summary': 'Save requested note',
              'target': str(target), 'scope': 'once', 'reversible': True}
    answer = 'note.txt에 Cedar 14:30을 저장하고 내용을 확인했습니다.'
    assert await reply(s, 'note.txt에 Cedar 14:30을 저장하고 다시 읽어서 확인해 주세요.',
        calls(('write', 'write_file', params)),
        calls(('read', 'read_file', {'path': str(target)})), final(answer)) == answer
    assert target.read_text() == 'Cedar 14:30'
    assert any(m['role'] == 'tool' and 'Cedar 14:30' in str(m['content'])
               for m in s.mock.requests[-1]['messages'])
    assert len(s.mock.requests) == 3 and not s.tasks()


async def test_write_without_required_metadata_still_cannot_execute(tmp_path, monkeypatch):
    s = ordinary(tmp_path, monkeypatch)
    target = tmp_path / 'note.txt'
    answer = '필수 실행 정보가 없어 저장하지 못했습니다.'
    assert await reply(s, 'note.txt에 메모를 저장해 주세요.',
        calls(('write', 'write_file', {'path': str(target), 'content': 'not authorized by metadata'})),
        final(answer)) == answer
    assert not target.exists()
    assert any(m['role'] == 'tool' and 'intent_summary' in str(m['content'])
               for m in s.mock.requests[-1]['messages'])


async def test_scope_denial_is_preserved_but_model_can_explain_it(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'private.txt'
    outside.write_text('OUTSIDE_SECRET')
    s = ordinary(workspace, monkeypatch)
    # Scope is set at the turn boundary, not by overriding a single tool alone.
    s.loop.workspace_scopes = WorkspaceScopeResolver(workspace, True)
    s.loop.tools.register(ReadFileTool(workspace=workspace, allowed_dir=workspace))
    answer = '요청한 파일은 허용된 작업 폴더 밖이라 읽을 수 없습니다.'
    assert await reply(s, '그 파일을 읽어 주세요.',
        calls(('read', 'read_file', {'path': str(outside)})), final(answer)) == answer
    tool_results = [str(m['content']) for m in s.mock.requests[-1]['messages'] if m['role'] == 'tool']
    assert tool_results and all('OUTSIDE_SECRET' not in r for r in tool_results)
    assert any('outside' in r.lower() for r in tool_results)
