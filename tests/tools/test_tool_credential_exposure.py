"""Credential paths and values must not leave through the my, message and sub-agent tools
(review C01, C08, C11)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.self import MyTool
from nanobot.config.schema import MCPServerConfig, ToolsConfig


@pytest.mark.asyncio
async def test_message_tool_refuses_credential_attachments_without_a_workspace_limit(tmp_path) -> None:
    sent: list[object] = []

    async def callback(message) -> None:
        sent.append(message)

    tool = MessageTool(
        send_callback=callback, default_channel="telegram", default_chat_id="111",
        workspace=tmp_path / "ws", restrict_to_workspace=False,
    )
    secret = tmp_path / ".ssh" / "id_rsa"
    result = await tool.execute(content="hi", channel="telegram", chat_id="999999", media=[str(secret)])
    assert "not allowed" in str(result)
    assert sent == []

    plain = tmp_path / "report.txt"
    plain.write_text("ok")
    result = await tool.execute(content="hi", channel="telegram", chat_id="111", media=[str(plain)])
    assert len(sent) == 1 and sent[0].media == [str(plain)]


def _my_tool(**kwargs) -> MyTool:
    config = ToolsConfig(mcp_servers={
        "gh": MCPServerConfig(command="npx", env={"GITHUB_TOKEN": "FAKE-ENV-SECRET"}),
        "web": MCPServerConfig(
            url="https://example.invalid/mcp",
            headers={"Authorization": "Bearer FAKE-HEADER-SECRET"},
            oauth={"enabled": True, "client_id": "c", "client_secret": "FAKE-CLIENT-SECRET"},
        ),
    })
    state = SimpleNamespace(
        tools_config=config, workspace="/tmp/ws", _runtime_vars={}, model="m", max_iterations=10,
        context_window_tokens=8192, model_preset=None, _last_usage=None, _active_preset=None,
        some_map={"user": "alice", "api_key": "FAKE-MAP-SECRET", "headers": {"x": "FAKE-NESTED"}},
    )
    return MyTool(state, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [
    "tools_config",
    "tools_config.mcp_servers.gh.env",
    "tools_config.mcp_servers.web.headers",
    "tools_config.mcp_servers.web.oauth.client_secret",
])
async def test_my_tool_does_not_return_configured_secrets(key) -> None:
    out = await _my_tool(modify_allowed=False).execute(action="check", key=key)
    assert "FAKE-" not in str(out)


@pytest.mark.asyncio
async def test_my_tool_hides_credential_entries_inside_small_dicts() -> None:
    out = await _my_tool(modify_allowed=False).execute(action="check", key="some_map")
    assert "FAKE-" not in str(out)
    assert "alice" in str(out)


@pytest.mark.asyncio
async def test_my_tool_cannot_rewrite_the_tools_config() -> None:
    tool = _my_tool(modify_allowed=True)
    config = tool._runtime_state.tools_config
    before = (config.restrict_to_workspace, list(config.exec.deny_patterns))
    for key, value in (("tools_config.restrict_to_workspace", True), ("tools_config.exec.deny_patterns", ["x"])):
        out = await tool.execute(action="set", key=key, value=value)
        assert "not accessible" in str(out) or "protected" in str(out), out
    assert (config.restrict_to_workspace, list(config.exec.deny_patterns)) == before


def test_subagents_inherit_safe_mode_and_cli_app_policy() -> None:
    from nanobot.agent.subagent import SubagentManager

    parent = ToolsConfig(safe_mode=True, hardened_mode=True, cli_apps={"enable": False})
    manager = SubagentManager.__new__(SubagentManager)
    manager.tools_config = parent
    manager.restrict_to_workspace = True
    child = manager._subagent_tools_config()
    assert child.safe_mode is True
    assert child.hardened_mode is True
    assert child.cli_apps.enable is False
    assert child.restrict_to_workspace is True
