"""Compact answers retain policy; execution restores full guidance and real tools."""

import re
from copy import deepcopy

from nanobot.agent.compact_context import CompactTools
from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.filesystem import ReadFileTool, WriteFileTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config, ModelPresetConfig, ToolsConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest


class Script:
    def __init__(self, steps):
        self.steps = iter(steps)
        self.calls = []
        self.generation = ModelPresetConfig(model="test").to_generation_settings()

    def get_default_model(self):
        return "test"

    async def chat_with_retry(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return next(self.steps)


def call(name, **kwargs):
    return LLMResponse(content="", tool_calls=[ToolCallRequest(
        id=name, name=name, arguments=kwargs)])


async def test_activation_restores_system_before_execution_without_truncation(tmp_path):
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path, restrict_to_workspace=True))
    (tmp_path / "a.md").write_text("actual evidence", encoding="utf-8")
    system = {"role": "system", "content": "compact"}
    full = "Full execution rules\n" * 1000
    overlay = CompactTools(registry, full, system)
    assert overlay.prepare_call("read_file", {"path": "a.md"})[2]
    provider = Script([call("activate_tools"), call("read_file", path="a.md"),
                       LLMResponse(content="actual evidence")])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[system, {"role": "user", "content": "read a.md"}], tools=overlay,
        model="test", max_iterations=4, max_tool_result_chars=200, workspace=tmp_path))
    assert result.stop_reason == "completed"
    assert provider.calls[0]["messages"][0]["content"] == "compact"
    assert provider.calls[1]["messages"][0]["content"] == full
    assert provider.calls[1]["tools"] == registry.get_definitions()
    assert "actual evidence" in str(provider.calls[-1]["messages"])
    assert not registry.has("activate_tools")
    assert not CompactTools(registry, full, {}).activated


def test_compact_prompt_preserves_custom_policy_memory_and_summary(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Always request approval before sending.")
    skill = tmp_path / "skills" / "custom-policy" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: custom-policy\ndescription: custom policy\nalways: true\n---\nCUSTOM POLICY")
    memory = tmp_path / "memory" / "MEMORY.md"
    memory.parent.mkdir()
    memory.write_text("User prefers Korean answers.")
    builder = ContextBuilder(tmp_path)
    compact = builder.build_compact_system_prompt(session_summary="Prior decision: don't deploy.")
    for required in ["Always request approval", "CUSTOM POLICY", "Korean answers", "don't deploy",
                     "Explicit Permission Gates", "activate_tools"]:
        assert required in compact
    assert len(compact) < len(builder.build_system_prompt()) * 0.6


def test_compact_respects_disabled_always_preloading_and_keeps_explicit_hot_skills(tmp_path):
    for name, always, body in [("custom", True, "CUSTOM BODY"), ("hot", False, "HOT BODY")]:
        skill = tmp_path / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            f"---\nname: {name}\ndescription: {name} policy\nalways: {str(always).lower()}\n---\n{body}"
        )
    builder = ContextBuilder(tmp_path, preload_always_skills=False,
                             hot_path_skills=["hot"], on_demand_skills=["custom"])
    compact = builder.build_compact_system_prompt()
    full = builder.build_system_prompt()
    assert "HOT BODY" in compact and "HOT BODY" in full
    assert "CUSTOM BODY" not in compact and "CUSTOM BODY" not in full
    assert "# On-demand Skills" in full and "skills/custom/SKILL.md" in full


async def test_loop_simple_answer_one_call_then_real_file_work(tmp_path):
    provider = Script([LLMResponse(content="4"), call("activate_tools"),
                       call("read_file", path="a.md"), LLMResponse(content="file evidence"),
                       LLMResponse(content="4")])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     compact_context=True, tools_config=ToolsConfig(safe_mode=True),
                     mcp_servers={}, unified_session=False)
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path, restrict_to_workspace=True))
    try:
        response = await loop.process_direct("2+2?", tools=registry)
        assert response.content == "4"
        assert len(provider.calls) == 1
        assert [t["function"]["name"] for t in provider.calls[0]["tools"]] == ["activate_tools"]
        (tmp_path / "a.md").write_text("file evidence")
        response = await loop.process_direct("Read a.md", tools=registry)
        assert response.content == "file evidence"
        assert "Tool Usage Notes" in provider.calls[2]["messages"][0]["content"]
        assert any(m.get("content") == "4" for m in provider.calls[1]["messages"])
        assert not registry.has("activate_tools")
        # The next turn carries over turn 2's activation instead of re-deferring: this is
        # what keeps the system message (and provider-side prompt cache) stable across turns.
        await loop.process_direct("2+2?", tools=registry)
        assert [t["function"]["name"] for t in provider.calls[-1]["tools"]] == ["read_file"]
        assert "Tool Usage Notes" in str(provider.calls[-1]["messages"])
    finally:
        loop.stop()


async def test_activation_does_not_bypass_intent_or_workspace_checks(tmp_path):
    registry = ToolRegistry()
    registry.register(WriteFileTool(workspace=tmp_path, restrict_to_workspace=True))
    overlay = CompactTools(registry, "rules", {})
    await overlay.execute("activate_tools", {})
    assert overlay.prepare_call("write_file", {"path": "a.md", "content": "x"})[2]
    result = await overlay.execute("write_file", {
        "path": "../outside.md", "content": "x", "intent_summary": "write", "target": "../outside.md",
        "scope": "once", "reversible": True})
    assert getattr(result, "is_error", False)
    assert not (tmp_path.parent / "outside.md").exists()


async def test_selected_activation_can_expand_without_bypassing_original_guards(tmp_path):
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path, restrict_to_workspace=True))
    registry.register(WriteFileTool(workspace=tmp_path, restrict_to_workspace=True))
    original = deepcopy(registry.get_definitions())
    system = {"role": "system", "content": "compact"}
    overlay = CompactTools(registry, "full execution guidance", system)
    invalid = await overlay.execute("activate_tools", {"tool_names": ["missing_tool"]})
    assert getattr(invalid, "is_error", False)
    assert not overlay.activated
    assert system["content"] == "compact"
    await overlay.execute("activate_tools", {"tool_names": ["read_file"]})
    assert system["content"] == "full execution guidance"
    definitions = {d["function"]["name"]: d for d in overlay.get_definitions()}
    assert set(definitions) == {"activate_tools", "read_file"}
    assert definitions["read_file"] == next(d for d in original if d["function"]["name"] == "read_file")
    assert overlay.get("write_file") is None
    assert overlay.prepare_call("write_file", {"path": "a.md", "content": "x"})[2]
    refused = await overlay.execute("write_file", {"path": "a.md", "content": "x"})
    assert getattr(refused, "is_error", False)
    assert not (tmp_path / "a.md").exists()
    await overlay.execute("activate_tools", {"tool_names": ["write_file"]})
    assert {d["function"]["name"] for d in overlay.get_definitions()} == {
        "activate_tools", "read_file", "write_file"}
    assert overlay.prepare_call("write_file", {"path": "a.md", "content": "x"})[2]
    result = await overlay.execute("write_file", {
        "path": "../outside.md", "content": "x", "intent_summary": "write",
        "target": "../outside.md", "scope": "once", "reversible": True})
    assert getattr(result, "is_error", False)
    assert not (tmp_path.parent / "outside.md").exists()
    assert registry.get_definitions() == original
    assert not CompactTools(registry, "rules", {}).activated
    # Omitting tool_names is the compatibility fallback to the entire registry.
    await overlay.execute("activate_tools", {})
    assert overlay.get_definitions() == original


async def test_selected_tools_expand_inside_runner_without_repeating_file_work(tmp_path):
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path, restrict_to_workspace=True))
    registry.register(WriteFileTool(workspace=tmp_path, restrict_to_workspace=True))
    (tmp_path / "a.md").write_text("original")
    overlay = CompactTools(registry, "execution rules", {"role": "system", "content": "compact"})
    provider = Script([
        call("activate_tools", tool_names=["read_file"]), call("read_file", path="a.md"),
        call("activate_tools", tool_names=["write_file"]),
        call("write_file", path="a.md", content="saved", intent_summary="save",
             target="a.md", scope="once", reversible=True),
        call("read_file", path="a.md"), LLMResponse(content="saved and verified"),
    ])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "read then save a.md"}], tools=overlay,
        model="test", max_iterations=7, max_tool_result_chars=4000, workspace=tmp_path))
    assert result.stop_reason == "completed"
    assert (tmp_path / "a.md").read_text() == "saved"
    assert {d["function"]["name"] for d in provider.calls[1]["tools"]} == {"activate_tools", "read_file"}
    assert {d["function"]["name"] for d in provider.calls[3]["tools"]} == {
        "activate_tools", "read_file", "write_file"}
    assert len([e for e in result.tool_events if e["name"] == "write_file"]) == 1


async def test_empty_catalog_still_allows_legacy_activation():
    overlay = CompactTools(ToolRegistry(), "execution rules", {})
    assert overlay.get_definitions()[0]["function"]["parameters"]["properties"] == {}
    result = await overlay.execute("activate_tools", {})
    assert not getattr(result, "is_error", False)
    assert overlay.get_definitions() == []


def test_compact_config_is_explicit_and_default_on():
    assert Config().agents.defaults.compact_context is True
    assert not Config.model_validate(
        {"agents": {"defaults": {"compactContext": False}}}
    ).agents.defaults.compact_context


async def test_compact_and_lightweight_preserve_both_paths(tmp_path):
    def update(status):
        def response(kwargs):
            request_id = re.search(r"current request_id=([a-f0-9]+)", str(kwargs["messages"]))[1]
            return call("update_task", request_id=request_id, intent="new", goal="save a.md",
                        targets=[{"path": "a.md", "action": "write", "selection_reason": "user target"}],
                        source="files", analysis_complete=status == "completed", status=status,
                        explanation="recorded actual work", result_refs=["write_file"] if status == "completed" else [],
                        final_answer="saved content" if status == "completed" else "")
        return response

    provider = Script([LLMResponse(content="4"), update("active"),
                       call("activate_tools", tool_names=["read_file", "write_file"]),
                       call("write_file", path="a.md", content="saved content", intent_summary="save",
                            target="a.md", scope="once", reversible=True),
                       call("read_file", path="a.md"), update("completed")])
    original_chat = provider.chat_with_retry

    async def respond(**kwargs):
        response = await original_chat(**kwargs)
        return response(kwargs) if callable(response) else response

    provider.chat_with_retry = respond
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     compact_context=True, stepwise_lightweight=True,
                     tools_config=ToolsConfig(safe_mode=True), mcp_servers={}, unified_session=False)
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path, restrict_to_workspace=True))
    registry.register(WriteFileTool(workspace=tmp_path, restrict_to_workspace=True))
    try:
        await loop.process_direct("2+2?", tools=registry)
        assert [t["function"]["name"] for t in provider.calls[0]["tools"]] == ["activate_tools"]
        assert "Task evidence control" not in str(provider.calls[0]["messages"])
        response = await loop.process_direct("a.md에 saved content를 저장하고 다시 읽어 확인해줘", tools=registry)
        assert response.content == "saved content"
        assert (tmp_path / "a.md").read_text() == "saved content"
        note = loop.sessions.get_or_create("cli:direct").metadata["task_note"]
        assert note["status"] == "completed"
        assert len([e for e in note["events"] if e["name"] == "write_file"]) == 1
        assert any(t["function"]["name"] == "activate_tools" for t in provider.calls[1]["tools"])
    finally:
        loop.stop()
