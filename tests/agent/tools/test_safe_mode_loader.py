from dataclasses import dataclass
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry


class _ReadOnlyTool(Tool):
    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "read"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


class _DangerousTool(Tool):
    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "exec"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


@dataclass
class _ToolsConfig:
    safe_mode: bool


@dataclass
class _Ctx:
    config: _ToolsConfig


def test_tool_loader_skips_dangerous_tools_in_safe_mode() -> None:
    registry = ToolRegistry()
    ctx = _Ctx(config=_ToolsConfig(safe_mode=True))

    registered = ToolLoader(test_classes=[_ReadOnlyTool, _DangerousTool]).load(ctx, registry)

    assert registered == ["read_file"]
    assert registry.has("read_file")
    assert not registry.has("exec")


def test_tool_loader_keeps_dangerous_tools_outside_safe_mode() -> None:
    registry = ToolRegistry()
    ctx = _Ctx(config=_ToolsConfig(safe_mode=False))

    registered = ToolLoader(test_classes=[_DangerousTool]).load(ctx, registry)

    assert registered == ["exec"]
    assert registry.has("exec")
