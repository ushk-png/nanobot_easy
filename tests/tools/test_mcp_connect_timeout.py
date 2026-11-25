"""A stdio MCP server that never answers must not stall the agent loop (review N09).

Uses the real MCP SDK (test_mcp_tool.py replaces it with fakes), a local silent child process
and no network.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from nanobot.agent.tools.mcp import connect_mcp_servers
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import MCPServerConfig


@pytest.mark.asyncio
async def test_unresponsive_stdio_server_does_not_block_connect(tmp_path) -> None:
    server = tmp_path / "silent_server.py"
    server.write_text("import sys, time\nsys.stdin.readline()\ntime.sleep(3600)\n")
    cfg = MCPServerConfig(
        type="stdio", command=sys.executable, args=[str(server)], connect_timeout=1
    )
    registry = ToolRegistry()
    started = time.monotonic()
    stacks = await asyncio.wait_for(connect_mcp_servers({"silent": cfg}, registry), timeout=20)
    elapsed = time.monotonic() - started
    assert stacks == {}
    assert registry.tool_names == []
    assert 0.9 <= elapsed < 15, elapsed


def test_connect_timeout_default_and_alias() -> None:
    assert MCPServerConfig().connect_timeout == 30
    assert MCPServerConfig.model_validate({"connectTimeout": 5}).connect_timeout == 5
    assert MCPServerConfig.model_validate({"connectTimeout": 0}).connect_timeout == 0
