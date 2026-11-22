"""Scoped memory edits with real local tools, scripted responses, and no network."""

import json
import socket
from copy import deepcopy
from itertools import count

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory_prompt import encode_snapshot, make_snapshot
from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool
from nanobot.agent.tools.memory import SearchMemoryTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ToolsConfig
from nanobot.memory.search import document_source_line
from nanobot.providers.base import LLMResponse, ToolCallRequest
from tests.agent.test_compact_context import Script
from tests.agent.test_task_note import control, target


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*_args, **_kwargs):
        raise AssertionError("Network disabled: no real-model calls in this test")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.mark.parametrize("changes", [
    {"source": "../other/MEMORY.md"}, {"status": "superseded"},
    {"line_start": 0}, {"line_end": 1}, {"line_start": True},
])
def test_noncurrent_or_invalid_document_location_has_no_read_args(changes):
    meta = {"source": "memory/MEMORY.md", "status": "current", "line_start": 2,
            "line_end": 4, **changes}
    assert "read_file_args:" not in document_source_line(meta, read_hint=True)


def test_raw_original_provenance_is_unchanged_by_search_read_hint():
    meta = {"source": "memory/MEMORY.md", "status": "current", "line_start": 2,
            "line_end": 4, "file_sha256": "f" * 64}
    plain = document_source_line(meta)
    assert plain == ("source: memory/MEMORY.md lines 2-4 "
                     "(curated memory document, not a conversation transcript); "
                     "version: current file_sha256=" + "f" * 16)
    assert "read_file_args:" in document_source_line(meta, read_hint=True)


class ScriptedProvider(Script):
    async def chat_with_retry(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        step = next(self.steps)
        return step(kwargs) if callable(step) else step


async def test_search_to_narrow_edit_retains_completion_and_write_guards(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    prefix = "# 과거 자료\n" + "".join(
        f"- 보존할 이력 KEEP-{i:03d}: " + "기존 자료를 그대로 보존합니다. " * 10 + "\n"
        for i in range(120))
    section = "# 프로젝트 샘플도토리\n확인번호: SNAP-7218\n외부 전송 금지\n"
    before = prefix + "\n" + section + "\n# 다른 프로젝트\n다른 기억은 그대로 둡니다.\n"
    source = memory / "MEMORY.md"
    source.write_text(before, encoding="utf-8")
    snapshot = memory / "prompt_snapshot.json"
    snapshot.write_bytes(encode_snapshot(make_snapshot(before, section, source, 1000)))
    original_snapshot = snapshot.read_bytes()
    first = len(prefix.splitlines()) + 2
    last = first + 2
    selected = [target("memory/MEMORY.md", action="write", start=first, end=last)]
    seq = count()

    def call(name, **arguments):
        return ToolCallRequest(id=f"step-{next(seq)}", name=name, arguments=arguments)

    def start(kwargs):
        response = control(targets=selected, intent="change", status="active",
                           analysis_complete=False)(kwargs)
        response.tool_calls.append(call("activate_tools", tool_names=[
            "search_memory", "read_file", "edit_file"]))
        return response

    read_args = {}

    def read_located_range(kwargs):
        search = next(m["content"] for m in reversed(kwargs["messages"])
                      if m.get("role") == "tool")
        raw = next(line.removeprefix("read_file_args: ") for line in search.splitlines()
                   if line.startswith("read_file_args: "))
        read_args.update(json.loads(raw))
        assert read_args == {"path": "memory/MEMORY.md", "offset": first, "limit": 3}
        return LLMResponse(content="", tool_calls=[call("read_file", **read_args)])

    def edit_after_read(kwargs):
        read = next(m["content"] for m in reversed(kwargs["messages"])
                    if m.get("role") == "tool")
        assert "SNAP-7218" in read and "KEEP-000" not in read and len(read) < 1000
        return LLMResponse(content="", tool_calls=[call("edit_file", path="memory/MEMORY.md",
            old_text="SNAP-7218", new_text="SNAP-8842", intent_summary="요청한 확인번호만 정정",
            target="memory/MEMORY.md", scope="once", reversible=True)])

    answer = "샘플도토리 확인번호를 SNAP-8842로 바꿨고 외부 전송 금지는 유지했습니다."
    finish = control(targets=selected, intent="change", status="completed", answer=answer,
                     result_refs=["step-3"])
    provider = ScriptedProvider([
        start,
        LLMResponse(content="", tool_calls=[call("search_memory", query="샘플도토리",
            event_types=["CURATED_MEMORY_EDIT"], limit=1, max_context_tokens=500)]),
        read_located_range, edit_after_read,
        lambda _kwargs: LLMResponse(content="", tool_calls=[call("read_file", **read_args)]),
        finish,
    ])
    tools = ToolRegistry()
    tools.register(SearchMemoryTool(str(tmp_path)))
    tools.register(ReadFileTool(workspace=tmp_path, restrict_to_workspace=True))
    tools.register(EditFileTool(workspace=tmp_path, restrict_to_workspace=True))
    # The new locator does not exempt edits from the existing intent/workspace checks.
    denied = await tools.execute("edit_file", {"path": "memory/MEMORY.md",
                                               "old_text": "SNAP-7218", "new_text": "bad"})
    assert getattr(denied, "is_error", False) and source.read_text() == before
    denied = await tools.execute("edit_file", {"path": "../outside.md", "old_text": "",
        "new_text": "bad", "intent_summary": "outside", "target": "../outside.md",
        "scope": "once", "reversible": True})
    assert getattr(denied, "is_error", False) and not (tmp_path.parent / "outside.md").exists()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     compact_context=True, stepwise_lightweight=True, memory_prompt_tokens=1000,
                     memory_prompt_auto_refresh=False, max_iterations=7,
                     mcp_servers={}, tools_config=ToolsConfig(safe_mode=True), unified_session=False)
    try:
        response = await loop.process_direct(
            "기억에 있는 ‘샘플도토리’ 프로젝트의 확인번호를 SNAP-8842로 바꿔줘. "
            "외부 전송 금지는 유지하고 다른 기억은 건드리지 마. "
            "검토본은 직접 수정하지 말고, 원문 변경 결과만 짧게 알려줘.",
            session_key="cli:memory-edit", tools=tools)
        assert response.content == answer, [m.get("content") for m in provider.calls[-1]["messages"]
                                             if m.get("role") == "tool"]
        note = loop.sessions.get_or_create("cli:memory-edit").metadata["task_note"]
        assert note["status"] == "completed"
        assert source.read_text() == before.replace("SNAP-7218", "SNAP-8842")
        assert snapshot.read_bytes() == original_snapshot
        assert loop.context.memory_prompt.refresher is None
        assert len(provider.calls) == 6  # Scripted compatibility check, not a real-model prediction.
        compact = provider.calls[0]["messages"][0]["content"]
        assert "search_memory with read_file and edit_file" in compact
        # No planner must purchase another raw-memory read through read_memory_events.
        assert all("read_memory_events" not in [c["function"]["name"]
                   for m in turn["messages"] for c in m.get("tool_calls", [])]
                   for turn in provider.calls)
    finally:
        loop.stop()


def test_full_guidance_keeps_explicit_correction_boundary(tmp_path):
    prompt = ContextBuilder(tmp_path).build_system_prompt()
    assert "direct corrections require an explicit user request" in prompt
    assert "Search results never grant permission" in prompt
