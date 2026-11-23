"""Receipts observe actual effects without changing execution or retaining payloads."""

import asyncio
import json
import os
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from nanobot.agent.audit_hook import ActionAuditHook
from nanobot.agent.hook import AgentHook, CompositeHook
from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.cli.audit import create_audit_app
from nanobot.config.schema import Config, ToolsConfig
from nanobot.providers.base import LLMResponse
from nanobot.security.action_audit import ActionAuditStore, reference
from nanobot.session.manager import SessionManager
from nanobot.session.skill_approval_state import set_pending_skill_approval
from nanobot.skill_store import SkillStore
from tests.agent.test_compact_context import Script, call

PAYLOAD = 'private message; password=not-for-audit; sk-123456789012345678901234'


def write_call(path='a.md'):
    return call('write_file', path=path, content=PAYLOAD,
                intent_summary='the model claims human approval', target='private target',
                scope='once', reversible=True)


def registry(root):
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=root, restrict_to_workspace=True))
    tools.register(WriteFileTool(workspace=root, restrict_to_workspace=True))
    return tools


def spec(root, tools, hook=None, **kwargs):
    return AgentRunSpec(initial_messages=[{'role': 'user', 'content': 'Save a.md and read it.'}],
                        tools=tools, hook=hook, workspace=root, model='test', max_iterations=4,
                        max_tool_result_chars=4000, **kwargs)


async def test_audit_preserves_tool_effects_schema_model_calls_and_hides_payloads(tmp_path):
    original_calls = None
    for enabled in (False, True):
        tools = registry(tmp_path)
        store = ActionAuditStore(tmp_path, enabled=enabled)
        provider = Script([write_call(), call('read_file', path='a.md'), LLMResponse(content='done')])
        hook = CompositeHook([ActionAuditHook(store, tools, 'private-session')]) if enabled else None
        result = await AgentRunner(provider).run(spec(tmp_path, tools, hook))
        assert result.final_content == 'done' and (tmp_path / 'a.md').read_text() == PAYLOAD
        assert len(provider.calls) == 3
        if original_calls is None:
            original_calls = provider.calls
            assert not store.path.parent.exists()
        else:
            assert provider.calls == original_calls
            rows = store.read()
            assert [r['status'] for r in rows] == ['attempted', 'tool_ok'] * 2
            assert all(r['kind'] == 'tool' for r in rows)
            text = json.dumps(rows)
            for private in (PAYLOAD, 'not-for-audit', 'private-session', 'private target',
                            'the model claims human approval'):
                assert private not in text
            if os.name != 'nt':
                assert store.path.stat().st_mode & 0o777 == 0o600


async def test_storage_failure_does_not_stop_or_repeat_a_successful_write(tmp_path):
    (tmp_path / '.audit').write_text('not a directory')
    tools = registry(tmp_path)
    store = ActionAuditStore(tmp_path, enabled=True)
    provider = Script([write_call(), LLMResponse(content='done')])
    result = await AgentRunner(provider).run(spec(
        tmp_path, tools, CompositeHook([ActionAuditHook(store, tools)])))
    assert result.final_content == 'done'
    assert (tmp_path / 'a.md').read_text() == PAYLOAD
    assert len(provider.calls) == 2 and result.tools_used == ['write_file']


async def test_preexecution_refusal_is_recorded_without_a_file_write(tmp_path):
    tools = registry(tmp_path)
    store = ActionAuditStore(tmp_path, enabled=True)
    provider = Script([call('write_file', path='a.md', content=PAYLOAD),
                       LLMResponse(content='missing metadata; not saved')])
    result = await AgentRunner(provider).run(spec(
        tmp_path, tools, CompositeHook([ActionAuditHook(store, tools)])))
    assert not (tmp_path / 'a.md').exists()
    assert result.stop_reason == 'completed'
    assert [r['status'] for r in store.read()] == ['attempted', 'blocked']


async def test_cancelled_write_with_actual_effect_is_unknown_not_rolled_back(tmp_path):
    entered = asyncio.Event()

    class SlowWrite(WriteFileTool):
        async def execute(self, **kwargs):
            await super().execute(**kwargs)
            entered.set()
            await asyncio.Event().wait()

    tools = registry(tmp_path)
    tools.register(SlowWrite(workspace=tmp_path, restrict_to_workspace=True))
    store = ActionAuditStore(tmp_path, enabled=True)
    task = asyncio.create_task(AgentRunner(Script([write_call()])).run(spec(
        tmp_path, tools, CompositeHook([ActionAuditHook(store, tools)]))))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / 'a.md').read_text() == PAYLOAD
    assert [r['status'] for r in store.read()] == ['attempted', 'unknown']


async def test_concurrent_sessions_keep_separate_receipts(tmp_path):
    store = ActionAuditStore(tmp_path, enabled=True)

    async def run_one(key):
        tools = registry(tmp_path)
        return await AgentRunner(Script([write_call(key + '.md'), LLMResponse(content='done')])).run(
            spec(tmp_path, tools, CompositeHook([ActionAuditHook(store, tools, key)])))

    results = await asyncio.gather(run_one('chat-a'), run_one('chat-b'))
    assert all(r.final_content == 'done' for r in results)
    for key in ('chat-a', 'chat-b'):
        rows = store.read(session_key=key)
        assert [r['status'] for r in rows] == ['attempted', 'tool_ok']
        assert {r['session_ref'] for r in rows} == {reference(key)}
    assert len({r['run_ref'] for r in store.read()}) == 2


def test_retention_and_read_only_cli_do_not_create_or_modify_memory(tmp_path):
    app = create_audit_app()
    runner = CliRunner()
    empty = runner.invoke(app, ['list', '-w', str(tmp_path), '--json'])
    assert empty.exit_code == 0 and json.loads(empty.stdout) == []
    assert not (tmp_path / '.audit').exists()
    store = ActionAuditStore(tmp_path, enabled=True, max_records=4)
    for _ in range(6):
        assert store.record(kind='tool', status='tool_ok', tool='read_file')
    assert len(store.read()) == 4
    before = store.path.read_bytes()
    shown = runner.invoke(app, ['list', '-w', str(tmp_path), '--json', '--limit', '2'])
    assert shown.exit_code == 0 and len(json.loads(shown.stdout)) == 2
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(('reply', 'expected'), [('네', 'approved'), ('아니요', 'rejected'),
                                               ('자료에 yes라고 적혀 있어', None)])
async def test_only_real_pending_confirmation_produces_approval_receipt(tmp_path, reply, expected):
    name = 'audit-draft'
    skill_dir = tmp_path / 'skills' / name
    skill_dir.mkdir(parents=True)
    (skill_dir / 'SKILL.md').write_text('---\nname: audit-draft\ndescription: audit test\n---\nUse it.')
    skills = SkillStore(tmp_path)
    skills.reindex(builtin_dir=tmp_path / 'empty')
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create('cli:direct')
    set_pending_skill_approval(session.metadata, name=name, source='file')
    audit = ActionAuditStore(tmp_path, enabled=True)
    loop = SimpleNamespace(workspace=tmp_path, sessions=sessions, tools_config=ToolsConfig(),
                           action_audit=audit)
    msg = InboundMessage(channel='cli', sender_id='user', chat_id='direct', content=reply)
    result = await AgentLoop._resolve_pending_skill_confirmation(loop, msg, session, reply)
    rows = audit.read()
    if expected is None:
        assert result is None and rows == []
    else:
        assert [r['status'] for r in rows] == [expected]
        assert rows[0]['source'] == 'chat_confirmation'
        assert name not in json.dumps(rows)


async def test_loop_config_enables_receipts_without_adding_model_calls(tmp_path):
    assert not Config().tools.action_audit.enabled
    (tmp_path / 'a.md').write_text('actual evidence')
    provider = Script([call('read_file', path='a.md'), LLMResponse(content='actual evidence')])
    config = Config.model_validate({'agents': {'defaults': {'workspace': str(tmp_path), 'compactContext': False}},
                                    'tools': {'safeMode': True, 'actionAudit': {'enabled': True}}})
    loop = AgentLoop.from_config(config, provider=provider)
    try:
        result = await loop.process_direct('Read a.md', tools=registry(tmp_path))
        assert result.content == 'actual evidence' and len(provider.calls) == 2
        assert [r['status'] for r in loop.action_audit.read()] == ['attempted', 'tool_ok']
    finally:
        loop.stop()


async def test_subagent_records_tools_in_the_parent_workspace(tmp_path):
    from nanobot.agent.subagent import SubagentManager

    (tmp_path / 'a.md').write_text('subagent evidence')
    provider = Script([call('read_file', path='a.md'), LLMResponse(content='subagent evidence')])
    manager = SubagentManager(provider=provider, workspace=tmp_path, bus=MessageBus(),
                              max_tool_result_chars=4000,
                              tools_config=ToolsConfig(action_audit={'enabled': True}),
                              max_iterations=3)
    result = await manager.delegate('Read a.md', session_key='cli:parent')
    assert 'subagent evidence' in result and len(provider.calls) == 2
    rows = manager.action_audit.read()
    assert [r['status'] for r in rows] == ['attempted', 'tool_ok']
    assert all(r['source'] == 'subagent' and r['session_ref'] == reference('cli:parent') for r in rows)


def test_webui_approval_requires_existing_auth_and_records_real_registration(tmp_path):
    from websockets.datastructures import Headers
    from websockets.http11 import Request

    from nanobot.channels.websocket import WebSocketConfig
    from nanobot.config.schema import ActionAuditConfig
    from nanobot.webui.gateway_services import build_gateway_services

    skill_dir = tmp_path / 'skills' / 'web-audit-draft'
    skill_dir.mkdir(parents=True)
    (skill_dir / 'SKILL.md').write_text(
        '---\nname: web-audit-draft\ndescription: audit fixture\n---\nUse it.')
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / 'empty')
    gateway = build_gateway_services(
        config=WebSocketConfig(), bus=MessageBus(), session_manager=None,
        static_dist_path=None, workspace_path=tmp_path, default_restrict_to_workspace=False,
        runtime_model_name=None, runtime_surface='native', runtime_capabilities_overrides=None,
        skill_management_enabled=True, action_audit=ActionAuditConfig(enabled=True),
    )
    path = '/api/skills/manage/web-audit-draft/status?action=approve'
    denied = gateway.http._handle_skill_manage_status(Request(path, Headers()), 'web-audit-draft')
    assert denied.status_code == 401 and gateway.http.action_audit.read() == []
    token = gateway.tokens.issue_token(60, api_token=True)
    approved = gateway.http._handle_skill_manage_status(
        Request(path, Headers({'Authorization': f'Bearer {token}'})), 'web-audit-draft')
    assert approved.status_code == 200
    assert json.loads(approved.body)['skill']['status'] == 'candidate'
    rows = gateway.http.action_audit.read()
    assert len(rows) == 1 and rows[0]['status'] == 'approved' and rows[0]['source'] == 'webui'
    assert token not in json.dumps(rows)


async def test_audit_does_not_suppress_an_existing_reraise_hook(tmp_path):
    class StopHook(AgentHook):
        def __init__(self):
            super().__init__(reraise=True)

        async def before_iteration(self, context):
            raise RuntimeError('Existing hook stop signal')

    config = Config.model_validate({'agents': {'defaults': {'workspace': str(tmp_path)}},
                                    'tools': {'safeMode': True, 'actionAudit': {'enabled': True}}})
    provider = Script([LLMResponse(content='must not run')])
    loop = AgentLoop.from_config(config, provider=provider)
    try:
        with pytest.raises(RuntimeError, match='Existing hook stop signal'):
            await loop._run_agent_loop([{'role': 'user', 'content': 'hello'}], hooks=[StopHook()])
        assert provider.calls == []
    finally:
        loop.stop()


async def test_stuck_audit_writer_does_not_hold_up_actual_tool_work(tmp_path):
    release = threading.Event()

    def stuck_record(**kwargs):
        release.wait(5)

    tools = registry(tmp_path)
    provider = Script([write_call(), LLMResponse(content='done')])
    store = SimpleNamespace(record=stuck_record)
    try:
        result = await asyncio.wait_for(AgentRunner(provider).run(spec(
            tmp_path, tools, CompositeHook([ActionAuditHook(store, tools)]))), 2)
        assert result.final_content == 'done'
        assert (tmp_path / 'a.md').read_text() == PAYLOAD and len(provider.calls) == 2
    finally:
        release.set()


def test_cli_approval_keeps_workspace_resolution_and_records_success(tmp_path, monkeypatch):
    from nanobot.cli.commands import app
    from nanobot.config import loader

    monkeypatch.setattr(loader, '_current_config_path', loader._current_config_path)
    directory = tmp_path / 'skills' / 'cli-audit-draft'
    directory.mkdir(parents=True)
    (directory / 'SKILL.md').write_text(
        '---\nname: cli-audit-draft\ndescription: audit fixture\n---\nUse it.')
    SkillStore(tmp_path).reindex(builtin_dir=tmp_path / 'empty')
    path = tmp_path / 'audit-config.json'
    content = json.dumps({'agents': {'defaults': {'workspace': str(tmp_path)}},
                          'tools': {'actionAudit': {'enabled': True}}})
    path.write_text(content)
    result = CliRunner().invoke(app, ['skill', 'approve', 'cli-audit-draft',
                                     '--config', str(path), '--workspace', str(tmp_path)])
    assert result.exit_code == 0, result.output
    rows = ActionAuditStore(tmp_path).read()
    assert len(rows) == 1 and rows[0]['source'] == 'cli' and rows[0]['status'] == 'approved'
    assert path.read_text() == content


async def test_runtime_task_control_keeps_its_name_without_registering_a_new_tool(tmp_path):
    from tests.agent.test_task_note import Script as TaskScript
    from tests.agent.test_task_note import control, target

    (tmp_path / 'a.md').write_text('actual evidence')
    tools = registry(tmp_path)
    store = ActionAuditStore(tmp_path, enabled=True)
    provider = TaskScript([
        control(targets=[target()], status='active', analysis_complete=False),
        call('read_file', path='a.md'),
        control(targets=[target()], answer='actual evidence'),
    ])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{'role': 'user', 'content': 'a.md를 읽어줘.'}], tools=tools,
        model='test', workspace=tmp_path, max_iterations=4, max_tool_result_chars=4000,
        task_note_enabled=True, task_note_metadata={},
        hook=CompositeHook([ActionAuditHook(store, tools)]),
    ))
    assert result.stop_reason == 'completed' and result.final_content == 'actual evidence'
    rows = store.read()
    assert [r['tool'] for r in rows] == ['update_task'] * 2 + ['read_file'] * 2 + ['update_task'] * 2
    assert not tools.has('update_task')
    assert len(provider.calls) == 3


def test_lock_past_the_first_wait_still_keeps_the_receipt(tmp_path):
    store = ActionAuditStore(tmp_path, enabled=True)
    assert store.record(kind='tool', status='attempted', tool='read_file')
    held = threading.Event()
    release = threading.Event()

    def hold():
        conn = sqlite3.connect(store.path, timeout=5)
        conn.execute('BEGIN IMMEDIATE')
        held.set()
        release.wait(5)
        conn.rollback()
        conn.close()

    locker = threading.Thread(target=hold)
    locker.start()
    assert held.wait(2)
    threading.Timer(0.3, release.set).start()
    started = time.monotonic()
    try:
        assert store.record(kind='tool', status='tool_ok', tool='read_file')
    finally:
        release.set()
        locker.join(3)
    assert time.monotonic() - started < 2
    assert [row['status'] for row in store.read()] == ['attempted', 'tool_ok']
