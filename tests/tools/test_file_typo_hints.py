"""Typo hints for read_file/find_files and the boundary-block wording (names are lookup candidates only)."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.search import FindFilesTool
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

HINT = "try a different approach"


def make(tmp_path: Path, *names: str) -> None:
    for name in names:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")


# ---- read_file -----------------------------------------------------------------------------------------------

async def test_read_file_typo_names_up_to_three_close_files_and_opens_none(tmp_path):
    make(tmp_path, "report_2026.md", "report_2025.md", "report_2024.md", "report_2023.md", "unrelated.txt")
    result = await ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path).execute(path="report_2026.mdd")
    assert isinstance(result, ToolResult) and str(result).startswith("Error: File not found: report_2026.mdd")
    hint = str(result).split("Did you mean:", 1)[1]
    assert hint.count(".md") == 3 and "unrelated" not in hint and "report_2026.md" in hint
    assert "none was opened" in hint


async def test_read_file_keeps_the_plain_error_without_a_similar_file_or_folder(tmp_path):
    make(tmp_path, "alpha.txt")
    tool = ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    assert str(await tool.execute(path="zzzzzz.bin")) == "Error: File not found: zzzzzz.bin"
    assert str(await tool.execute(path="nofolder/alpha.txt")) == "Error: File not found: nofolder/alpha.txt"


async def test_read_file_a_directory_or_unreadable_folder_is_not_turned_into_a_hint(tmp_path, monkeypatch):
    make(tmp_path, "docs/readme.md")
    tool = ReadFileTool(workspace=tmp_path, allowed_dir=tmp_path)
    assert str(await tool.execute(path="docs")).startswith("Error: Not a file: docs")
    real_iterdir = Path.iterdir

    def deny(self):
        if self.name == "docs":
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", deny)
    assert str(await tool.execute(path="docs/readmee.md")) == "Error: File not found: docs/readmee.md"


async def test_read_file_does_not_name_a_candidate_the_read_check_refuses(tmp_path):
    workspace, outside = tmp_path / "ws", tmp_path / "outside"
    make(workspace, "notes.md")
    make(outside, "notes_secret.md")
    os.symlink(outside / "notes_secret.md", workspace / "notes_secret.md")          # a link out of the workspace
    result = await ReadFileTool(workspace=workspace, allowed_dir=workspace).execute(path="notes_secre.md")
    assert "notes_secret.md" not in str(result)


# ---- find_files ----------------------------------------------------------------------------------------------

async def test_find_files_suggests_close_names_only_when_nothing_matched(tmp_path):
    make(tmp_path, "docs/budget_2026.md", "docs/budget_2025.md", "src/app.py")
    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)
    result = await tool.execute(path=".", query="budget_2026.mdd")
    assert result.startswith("No files found\nSimilar file names") and "docs/budget_2026.md" in result
    assert result.count(".md") <= 3 + 1                                              # candidates only, at most three
    assert await tool.execute(path=".", query="qqqqqqqq") == "No files found"
    normal = await tool.execute(path=".", query="budget")
    assert normal.splitlines() == ["docs/budget_2025.md", "docs/budget_2026.md"]       # a real match is unchanged


async def test_find_files_an_empty_page_past_the_end_is_not_no_match(tmp_path):
    make(tmp_path, "docs/budget_2026.md", "docs/budget_2025.md")
    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)
    assert await tool.execute(path=".", query="budget", offset=10) == "No files found"
    assert await tool.execute(path=".", query="budget_2026.md", offset=5) == "No files found"


async def test_find_files_filters_still_apply_to_candidates(tmp_path):
    make(tmp_path, "docs/budget_2026.md", "src/budget_2026.py", "node_modules/pkg/budget_2026.mdx")
    tool = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)
    by_type = await tool.execute(path=".", query="budget_2026.mdd", type="py")
    assert "src/budget_2026.py" in by_type and "docs/" not in by_type and "node_modules" not in by_type
    assert "docs/budget_2026.md" not in await tool.execute(path="src", query="budget_2026.mdd")


async def test_find_files_does_not_expose_a_file_outside_the_allowed_root(tmp_path):
    workspace, outside = tmp_path / "ws", tmp_path / "outside"
    make(workspace, "docs/plan.md")
    make(outside, "plan_secret.md")
    os.symlink(outside / "plan_secret.md", workspace / "docs" / "plan_secret.md")
    result = await FindFilesTool(workspace=workspace, allowed_dir=workspace).execute(path=".", query="plan_secre.md")
    assert "plan_secret" not in result


async def test_find_files_errors_are_not_reported_as_no_match(tmp_path):
    make(tmp_path, "a.md")
    result = await FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path).execute(path="missing_dir", query="a.md")
    assert str(result).startswith("Error: Path not found")


async def test_find_files_an_interrupted_scan_gives_no_candidates(tmp_path):
    make(tmp_path, "budget_2026.md")
    from threading import Event

    stop = Event()
    stop.set()
    out = FindFilesTool(workspace=tmp_path, allowed_dir=tmp_path)._search(path=".", query="budget_2026.mdd", _stop=stop)
    assert out == "No files found"


# ---- boundary block wording ----------------------------------------------------------------------------------

class Failing(Tool):
    def __init__(self, text):
        self.text = text

    @property
    def name(self):
        return "read_file"

    @property
    def description(self):
        return "stub"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

    @property
    def read_only(self):
        return True

    async def execute(self, **kwargs):
        return ToolResult.error(self.text)


class Script(LLMProvider):
    def __init__(self, steps):
        super().__init__()
        self.generation = SimpleNamespace(max_tokens=1000, temperature=0.0, reasoning_effort=None)
        self.steps, self.i = steps, 0

    def get_default_model(self):
        return "m"

    async def chat(self, **kwargs):
        step = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        return step


async def tool_messages(error_text, calls=1):
    registry = ToolRegistry()
    registry.register(Failing(error_text))
    steps = [LLMResponse(content="", tool_calls=[ToolCallRequest(id=f"c{i}", name="read_file",
                                                                  arguments={"path": "/etc/passwd"})])
             for i in range(calls)] + [LLMResponse(content="done")]
    spec = AgentRunSpec(initial_messages=[{"role": "user", "content": "x"}], tools=registry, model="m",
                        max_iterations=10, max_tool_result_chars=4000)
    result = await AgentRunner(Script(steps)).run(spec)
    return [str(m["content"]) for m in result.messages if m.get("role") == "tool"]


async def test_a_workspace_block_no_longer_says_to_try_a_different_approach():
    (text,) = await tool_messages("Error: Path /etc/passwd is outside allowed directory /ws")
    assert "outside allowed directory" in text and HINT not in text


async def test_an_ordinary_error_keeps_the_generic_hint():
    (text,) = await tool_messages("Error: file is locked by another process")
    assert HINT in text


async def test_repeated_workspace_attempts_are_still_blocked():
    texts = await tool_messages("Error: Path /etc/passwd is outside allowed directory /ws", calls=5)
    assert any("refusing repeated workspace-bypass attempts" in t for t in texts)
    assert not any(HINT in t for t in texts)
