"""Bounded memory must preserve fallback, current instructions and raw retrieval."""

import json

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory_prompt import SNAPSHOT_NAME, BoundedMemoryPrompt, make_snapshot
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config, ToolsConfig
from nanobot.providers.base import LLMResponse
from nanobot.utils.helpers import estimate_message_tokens
from tests.agent.test_compact_context import Script, call

SUMMARY = 'Never restart other gateways. Keep current approvals. Project Aurora uses raw memory.'
DETAIL = 'Original detail ORIGINAL_48219; later correction ORIGINAL_48220 supersedes it.\n'


def workspace(root):
    directory = root / 'memory'
    directory.mkdir()
    source = directory / 'MEMORY.md'
    text = SUMMARY + '\n' + DETAIL + ('Historical detail only.\n' * 500)
    source.write_text(text, encoding='utf-8')
    snapshot = directory / SNAPSHOT_NAME
    snapshot.write_text(json.dumps(make_snapshot(text, SUMMARY, source, 200)), encoding='utf-8')
    return source, snapshot


def test_bound_applies_to_full_compact_and_followup_without_changing_sources(tmp_path):
    source, snapshot = workspace(tmp_path)
    before = source.read_bytes(), snapshot.read_bytes()
    (tmp_path / 'AGENTS.md').write_text('Current rule: do not deploy.')
    (tmp_path / 'USER.md').write_text('Reply in Korean.')
    builder = ContextBuilder(tmp_path, memory_prompt_tokens=200)
    prompts = [builder.build_system_prompt(), builder.build_compact_system_prompt(),
               builder.build_messages([{'role': 'user', 'content': 'Earlier Aurora question'},
                                       {'role': 'assistant', 'content': 'Earlier answer'}],
                                      '계속해줘')[0]['content']]
    for prompt in prompts:
        assert SUMMARY in prompt and 'Historical detail only.' not in prompt
        assert 'Current rule: do not deploy.' in prompt and 'Reply in Korean.' in prompt
        assert str(source) in prompt and 'omission is not absence' in prompt
    assert estimate_message_tokens({'role': 'system', 'content': builder._memory_context()}) <= 200
    assert (source.read_bytes(), snapshot.read_bytes()) == before


@pytest.mark.parametrize('problem', ['missing', 'malformed', 'stale', 'oversize', 'empty',
                                     'wrong_type', 'symlink_escape'])
def test_invalid_snapshot_keeps_complete_current_memory(tmp_path, problem):
    source, snapshot = workspace(tmp_path)
    if problem == 'missing':
        snapshot.unlink()
    elif problem == 'malformed':
        snapshot.write_text('{secret-bearing invalid JSON')
    elif problem == 'stale':
        source.write_text(source.read_text() + 'New current restriction: never send.\n')
    elif problem in {'oversize', 'empty', 'wrong_type'}:
        data = json.loads(snapshot.read_text())
        data['summary'] = {'oversize': 'detail ' * 1000, 'empty': '', 'wrong_type': []}[problem]
        snapshot.write_text(json.dumps(data))
    else:
        outside = tmp_path / 'elsewhere.json'
        snapshot.replace(outside)
        try:
            snapshot.symlink_to(outside)
        except OSError:
            pytest.skip('Symlinks are unavailable on this platform')
    original = source.read_text()
    builder = ContextBuilder(tmp_path, memory_prompt_tokens=200)
    assert original in builder.build_system_prompt()
    assert original in builder.build_compact_system_prompt()
    assert source.read_text() == original


def test_default_on_and_project_scope_remain_unchanged(tmp_path):
    source, _ = workspace(tmp_path)
    original = source.read_text()
    assert Config().agents.defaults.memory_prompt_tokens == 5000
    # ContextBuilder's own constructor default is unaffected by the Config default above;
    # with no explicit budget passed, projection stays off and raw memory is used.
    assert original in ContextBuilder(tmp_path).build_system_prompt()
    other = tmp_path / 'other-project'
    other.mkdir()
    builder = ContextBuilder(tmp_path, memory_prompt_tokens=200)
    assert original in builder.build_system_prompt(workspace=other)


def test_config_wiring_disables_projection_without_file_access(tmp_path):
    workspace(tmp_path)
    for file_access in (True, False):
        config = Config.model_validate({
            'agents': {'defaults': {'workspace': str(tmp_path), 'memoryPromptTokens': 200}},
            'tools': {'file': {'enable': file_access}, 'safeMode': True},
        })
        loop = AgentLoop.from_config(config, provider=Script([]))
        try:
            assert loop.context.memory_prompt.budget == (200 if file_access else 0)
        finally:
            loop.stop()


async def test_compact_tool_activation_retrieves_exact_source_without_extra_memory_calls(tmp_path):
    source, _ = workspace(tmp_path)
    before = source.read_bytes()
    provider = Script([call('activate_tools', tool_names=['read_file']),
                       call('read_file', path='memory/MEMORY.md'), LLMResponse(content=DETAIL)])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     memory_prompt_tokens=200, compact_context=True, mcp_servers={},
                     tools_config=ToolsConfig(safe_mode=True), unified_session=False)
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path, restrict_to_workspace=True))
    try:
        response = await loop.process_direct('Show the exact original project correction.',
                                             tools=registry)
        assert response.content == DETAIL.rstrip()
        assert len(provider.calls) == 3
        assert any(DETAIL.rstrip() in message.get('content', '')
                   for message in provider.calls[-1]['messages'] if message['role'] == 'tool')
        assert all(SUMMARY in row['messages'][0]['content'] for row in provider.calls)
        assert all('Historical detail only.' not in row['messages'][0]['content']
                   for row in provider.calls)
        assert source.read_bytes() == before
    finally:
        loop.stop()


def test_source_update_invalidates_cached_projection_and_can_be_refreshed(tmp_path):
    source, snapshot = workspace(tmp_path)
    loader = BoundedMemoryPrompt(source, 200)
    assert 'Historical detail only.' not in loader.build(source.read_text())
    revised = source.read_text() + 'Updated restriction: no writes.\n'
    source.write_text(revised)
    assert loader.build(revised) is None
    summary = SUMMARY + '\nUpdated restriction: no writes.'
    snapshot.write_text(json.dumps(make_snapshot(revised, summary, source, 200)))
    assert 'Updated restriction: no writes.' in loader.build(revised)
