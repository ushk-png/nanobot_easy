"""Offline context selection invariants; never calls a paid model."""
from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest

from nanobot.agent.context_selection import (
    Constraint,
    ContextCapacityError,
    ContextSelector,
    IntentDesign,
    SelectedContext,
    build_execution_tools,
    prepare_selected_loop,
    resolve_references,
)
from nanobot.agent.context_trace import ContextTrace
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import AgentDefaults, ContextSelectionConfig
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope
from nanobot.memory.task_context import TaskContextStore
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class FakeProvider(LLMProvider):
    def __init__(self, responses):
        super().__init__()
        self.responses = iter(responses)
        self.requests = []

    def get_default_model(self):
        return "offline"

    async def chat(self, messages, **kwargs):
        self.requests.append({"messages": copy.deepcopy(messages), **kwargs})
        return next(self.responses)


class ReadTool(Tool):
    name = "read_file"
    description = "Read current test file"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def __init__(self, workspace=None):
        self.workspace = workspace
        self.resource_identity = str(workspace / "current-test-config.txt") if workspace else None

    async def execute(self, **kwargs):
        from nanobot.agent.context_evidence import fingerprint
        from nanobot.agent.tools.base import ToolResult

        result = ToolResult("current config: version=2; deprecated method excluded")
        if self.workspace:
            target = self.workspace / "current-test-config.txt"
            result.read_evidence = {"target": str(target.resolve()), "unit": "lines", "start": 1,
                "end": 1, "total": 1, "extraction_ok": True, "version": fingerprint(target)}
        return result


class ChangeTool(Tool):
    name = "change_test_file"
    description = "Change a file"
    parameters = {"type": "object", "properties": {}}
    calls = 0

    async def execute(self, **kwargs):
        self.calls += 1
        return "updated"


def plan(**overrides):
    data = dict(goal="Explain arithmetic", request_type="explain", task_relation="new",
                task_id=None, route="direct", constraints=[], information_needs=[],
                capabilities_needed=[], completion_checks=["Answer supplied"],
                unresolved=[], answer="4")
    data.update(overrides)
    return IntentDesign.model_validate(data)


def response(design):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(
        id="design", name="submit_context_design", arguments=design.model_dump())],
        usage={"prompt_tokens": 90, "completion_tokens": 30})


def need(**overrides):
    result = dict(id="n1", required=True, question="What was decided?", source_kind="memory",
                  scope="current_task", query="deployment", source_ids=[],
                  acceptance="Latest instruction and later amendments are available")
    result.update(overrides)
    return result


@pytest.fixture
def env(tmp_path):
    store = ConversationEventStore(tmp_path)
    scope = MemoryScope.from_runtime(owner_id="owner", workspace_id=str(tmp_path), agent_id=None)
    registry = ToolRegistry()
    (tmp_path / "current-test-config.txt").write_text("current config: version=2; deprecated method excluded")
    registry.register(ReadTool(tmp_path))
    registry.register(ChangeTool())
    return store, scope, registry, SkillsLoader(tmp_path)


def event(env, text, *, actor="user", session="s", kind=None):
    store, scope, *_ = env
    return store.append_event(scope=scope, conversation_id=session, session_id=session,
                              actor=actor, event_type=kind or (
                                  "USER_MESSAGE" if actor == "user" else "ASSISTANT_MESSAGE"),
                              content=text)


async def select(env, design, request="2+2?", responses=None):
    store, scope, registry, skills = env
    selector = ContextSelector(store, ContextSelectionConfig(enabled=True))
    provider = FakeProvider(responses or [response(design)])
    selected = await selector.select(provider=provider, model="offline", request=request,
        scope=scope, session_id="s", registry=registry, skills=skills,
        rules="Mandatory permission limits", request_id="req")
    return selector, provider, selected


@pytest.mark.asyncio
async def test_direct_is_one_design_call_and_restart_restores_state(env):
    current = event(env, "2+2?")
    selector, provider, selected = await select(env, plan())
    assert len(provider.requests) == 1
    assert selected.design.answer == "4"
    assert selected.current_event.event_id == current.event_id
    selector.finish(selected, [], stop_reason="completed", had_injections=False)
    restored = TaskContextStore(env[0]).list(env[1], "s")[0]
    assert restored["status"] == "DONE"
    assert restored["last_event_id"] == current.event_id
    report = selected.trace.report()
    assert report["calls"][0]["stage"] == "design"
    assert report["calls"][0]["task_id"] == restored["task_id"]
    assert report["provider_total_tokens"] == 120
    assert "2+2" not in json.dumps(report)


@pytest.mark.asyncio
async def test_continuation_topic_switch_and_resume_do_not_complete_previous(env):
    event(env, "프로젝트 A를 구현해줘. 배포하지 마세요.")
    selector, _, first = await select(env, plan(route="execute", request_type="modify", answer=None),
                                     request="구현해줘")
    first_id = first.state["task_id"]
    event(env, "계속해 줘")
    _, _, follow = await select(env, plan(route="execute", answer=None,
        task_relation="continue", task_id=first_id), request="계속해 줘")
    assert follow.state["task_id"] == first_id
    assert any("배포하지" in c["text"] for c in follow.state["user_constraints"])
    event(env, "다른 프로젝트 B를 조사해줘")
    _, _, second = await select(env, plan(route="retrieve", request_type="investigate", answer=None),
                               request="다른 프로젝트 B를 조사해줘")
    assert second.state["task_id"] != first_id
    assert next(t for t in selector.tasks.list(env[1], "s") if t["task_id"] == first_id)["status"] != "DONE"
    event(env, "프로젝트 A를 재개해줘")
    _, _, resumed = await select(env, plan(route="execute", answer=None,
        task_relation="resume", task_id=first_id), request="프로젝트 A를 재개해줘")
    assert resumed.state["task_id"] == first_id


def test_compound_question_and_exclusion_references_are_candidates(env):
    questions = event(env, "Q1. 대상은?\nQ2. 범위는?\nQ3. 방식은?", actor="assistant")
    proposal = event(env, "방식 A를 제안합니다", actor="assistant")
    tasks = TaskContextStore(env[0])
    events = tasks.events(env[1], "s")
    refs = resolve_references("Q2,, Q3. 그 방식은 제외. 이전 설정으로 계속해 줘", events, [])
    assert refs["questions"]["source_ids"] == [questions.event_id]
    assert refs["questions"]["status"] == "candidate"
    assert proposal.event_id in refs["excluded_approach"]["source_ids"]
    assert refs["unfinished_task"]["status"] == "unresolved"
    assert refs["previous_configuration"]["status"] == "unresolved"


@pytest.mark.asyncio
async def test_minimal_input_missing_stale_and_no_old_tool_outputs(env):
    event(env, "a huge private tool result " * 10000, actor="tool", kind="TOOL_RESULT")
    event(env, "Q1. 대상? Q2. 범위? Q3. 방식?", actor="assistant")
    event(env, "Q2,, Q3")
    selector = ContextSelector(env[0], ContextSelectionConfig())
    data = selector.minimal_input("Q2,, Q3", env[1], "s", env[2], env[3])
    assert data["task_state_status"] == "missing"
    assert "private tool result" not in json.dumps(data)
    assert all("parameters" not in c for c in data["capabilities"])
    _, _, selected = await select(env, plan(route="retrieve", answer=None), request="Q2,, Q3")
    data = selector.minimal_input("계속", env[1], "s", env[2], env[3])
    assert data["task_state_status"] == "stale"
    assert data["task_state"]["last_event_id"] == selected.current_event.event_id


@pytest.mark.asyncio
async def test_old_instruction_restores_later_cancellation_and_raw_originals(env):
    old = event(env, "deployment: use method A", session="old")
    event(env, "method A 취소. 반드시 method B만 사용", session="old")
    event(env, "deployment 설정 조사해줘")
    _, _, selected = await select(env, plan(route="retrieve", answer=None,
        information_needs=[need(query="deployment")]), request="deployment 설정 조사해줘")
    assert any(e["event_id"] == old.event_id for e in selected.evidence)
    assert any("취소" in (e["content"] or "") for e in selected.evidence)
    assert selected.missing == []
    assert env[0].get_events(env[1], [old.event_id])[0].content == "deployment: use method A"


@pytest.mark.asyncio
async def test_required_failure_blocks_mutation_and_false_final(env):
    event(env, "notes.md 파일 수정해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        capabilities_needed=["change_test_file"], information_needs=[need(query="not-in-memory")]),
        request="notes.md 파일 수정해줘")
    assert selected.missing == ["n1"]
    # Gate prepare_call too: runner normally invokes prepared tool objects directly.
    assert selected.tools.prepare_call("change_test_file", {})[2]
    provider = FakeProvider([LLMResponse(content="Finished!")])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "수정해줘"}], tools=selected.tools,
        model="offline", max_iterations=2, max_tool_result_chars=1000, selected_context=selected))
    assert result.stop_reason == "incomplete"
    assert "완료하지 못" in result.final_content
    assert env[2].get("change_test_file").calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["json", "capability", "source", "constraint", "direct"])
async def test_invalid_design_bounded_repair_then_block(env, bad):
    event(env, "파일 구현해줘")
    design = plan(route="execute", answer=None)
    if bad == "capability":
        design.capabilities_needed = ["invented_tool"]
    if bad == "source":
        design.information_needs = [type(plan(information_needs=[need()]).information_needs[0])(
            **need(source_ids=["invented_event"]))]
    if bad == "constraint":
        design.constraints = [Constraint(text="user approved deployment", source_ids=["unknown"])]
    if bad == "direct":
        design = plan(unresolved=["which file"])  # structural unsafe_direct (model mistake)
    result = LLMResponse(content="{bad json") if bad == "json" else response(design)
    _, provider, selected = await select(env, design, request="파일 구현해줘", responses=[result, result])
    assert selected.blocked == "design_validation_failed"
    assert len(provider.requests) == 2
    assert selected.trace.counters["design_recoveries"] == 2


@pytest.mark.asyncio
async def test_validator_only_error_reroutes_without_repair(env):
    event(env, "파일 구현해줘")
    _, provider, selected = await select(env, plan(), request="파일 구현해줘",
                                         responses=[response(plan()), response(plan())])
    assert selected.blocked is None
    assert len(provider.requests) == 1  # no repair call for a validator heuristic
    assert selected.design.route == "execute" and selected.design.answer is None
    assert selected.trace.counters["validator_overrides"] == 1


@pytest.mark.asyncio
async def test_internal_clarification_runs_search_without_extra_model(env):
    event(env, "deployment configuration v2")
    _, provider, selected = await select(env, plan(route="clarify", answer=None,
        information_needs=[need(query="deployment")], unresolved=["configuration version"]),
        request="이전 설정 조사해줘")
    assert selected.design.route == "retrieve"
    assert selected.trace.counters["searches"] == 1
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_wrong_first_plan_can_discover_read_and_resolve_in_executor(env):
    event(env, "현재 설정을 수정해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        information_needs=[need(source_kind="file", query="current config")]), request="현재 설정을 수정해줘")
    assert not selected.tools.has("change_test_file")
    await selected.tools.execute("discover_context_capabilities", {"names": ["change_test_file"]})
    assert selected.tools.has("change_test_file")
    provider = FakeProvider([
        LLMResponse(content=None, tool_calls=[ToolCallRequest("read1", "read_file", {})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("resolve1", "resolve_context_need", {
            "need_id": "n1", "tool_call_id": "read1", "finding": "current version is 2"})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("change1", "change_test_file", {})]),
        LLMResponse(content="Modification checked against current version 2."),
    ])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "update current file"}],
        tools=selected.tools, model="offline", max_iterations=5,
        max_tool_result_chars=1000, selected_context=selected))
    assert result.tools_used == ["read_file", "resolve_context_need", "change_test_file"]
    assert selected.missing == []
    assert env[2].get("change_test_file").calls == 1
    assert len(provider.requests) == 4  # no per-tool designer or summarizer call


def loop_spec(env):
    selected = SelectedContext(trace=ContextTrace("req"), design=plan(route="execute", answer=None))
    tools = build_execution_tools(env[2], env[3], selected)
    return AgentRunSpec(initial_messages=[], tools=tools, model="offline", max_iterations=4,
                        max_tool_result_chars=1000, selected_context=selected,
                        context_window_tokens=200000)


def test_long_results_keep_facts_errors_and_open_tool_pairs_without_mutating_raw(env):
    spec = loop_spec(env)
    messages = [{"role": "system", "content": "Never deploy"},
                {"role": "user", "content": "modify"}]
    for n in range(5):
        messages.append({"role": "assistant", "tool_calls": [{"id": f"c{n}", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"}}]})
        messages.append({"role": "tool", "name": "read_file", "tool_call_id": f"c{n}",
            "content": json.dumps({"context_summary": {"facts": ["version=2"], "status": "failed",
                "error": "permission denied; do not repeat", "verification": "not performed"},
                "details": "x" * 30000})})
    # This fixture models a trusted structured producer, not a JSON document field.
    producer = ReadTool()
    producer.trusted_context_summary = True
    for n in range(5):
        original = messages[3 + 2 * n]["content"]
        with patch("nanobot.memory.event_store.extract_entities", return_value=[]):
            source = event(env, original, actor="tool", kind="TOOL_RESULT")
        spec.selected_context.register_summary(f"c{n}", source, producer,
                                               json.loads(original)["context_summary"])
    messages.append({"role": "assistant", "tool_calls": [{"id": "pending", "type": "function",
        "function": {"name": "read_file", "arguments": "{}"}}]})
    raw = copy.deepcopy(messages)
    model = prepare_selected_loop(spec, messages, FakeProvider([]))
    assert model[-1] == messages[-1]
    assert "permission denied" in model[3]["content"]
    assert "version=2" in model[3]["content"]
    assert len(model[3]["content"]) < len(messages[3]["content"])
    assert messages == raw
    assert model[-2] == raw[-2]  # newest result stays complete


def test_unknown_essential_result_expands_target_or_stops_without_erasing(env):
    spec = loop_spec(env)
    text = "important error and verification details " * 10000
    messages = [{"role": "user", "content": "do task"},
                {"role": "tool", "tool_call_id": "c", "content": text}]
    spec.context_window_tokens = 1000000
    assert prepare_selected_loop(spec, messages, FakeProvider([]))[-1]["content"] == text
    assert spec.selected_context.trace.counters["budget_expansions"] == 1
    spec.context_block_limit = 100
    with pytest.raises(ContextCapacityError):
        prepare_selected_loop(spec, messages, FakeProvider([]))
    assert messages[-1]["content"] == text


@pytest.mark.asyncio
async def test_restart_new_instruction_and_stale_writer_never_override_latest(env):
    event(env, "구현해줘. 방식 A만 사용.")
    selector, _, selected = await select(env, plan(route="execute", answer=None), request="구현해줘")
    stale = copy.deepcopy(selected.state)
    newer = copy.deepcopy(stale)
    newer["blockers"] = ["new constraint"]
    selector.tasks.save(env[1], newer, expected_version=newer["version"])
    with pytest.raises(ValueError, match="task_version_conflict"):
        selector.tasks.save(env[1], stale, expected_version=stale["version"])
    event(env, "방식 A 취소. 그 방식은 제외.")
    selector.finish(selected, [], stop_reason="completed", had_injections=True)
    assert selected.trace.counters["state_conflicts"] == 1
    restored = TaskContextStore(env[0]).list(env[1], "s")[0]
    assert restored["blockers"] == ["new constraint"]
    assert len(selector.tasks.events(env[1], "s", user_only=True)) == 2


def test_scope_isolation_and_duplicate_raw_events(env):
    raw = event(env, "private instruction")
    env[0].insert_events([raw, raw])
    projection = TaskContextStore(env[0])
    assert len(projection.events(env[1], "s")) == 1
    other = MemoryScope.from_runtime(owner_id="other", workspace_id=env[1].workspace_ids[0], agent_id=None)
    assert projection.events(other, "s") == []
    assert env[0].get_events(other, [raw.event_id]) == []


@pytest.mark.asyncio
async def test_disabled_default_and_full_loop_opt_in_direct(env, tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    assert AgentDefaults().context_selection.enabled is False
    provider = FakeProvider([response(plan())])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     context_selection=ContextSelectionConfig(enabled=True))
    with patch.object(loop.consolidator, "maybe_consolidate_by_tokens", return_value=None):
        outbound = await loop._process_message(InboundMessage(
            channel="cli", sender_id="owner", chat_id="s", content="2+2?"))
    assert outbound.content == "4"
    assert len(provider.requests) == 1
    session = loop.sessions.get_or_create("cli:s")
    assert [m["role"] for m in session.messages] == ["user", "assistant"]
    assert session.messages[-1]["content"] == "4"


@pytest.mark.asyncio
async def test_full_loop_executor_trace_and_lossless_tool_persistence(env, tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus

    long_result = "Essential data; failure=none; version=2\n" * 3000
    class LongRead(ReadTool):
        async def execute(self, **kwargs):
            return long_result

    design = plan(route="execute", request_type="investigate", answer=None,
                  capabilities_needed=["read_file"])
    provider = FakeProvider([response(design),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("long1", "read_file", {})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("check1", "record_context_completion", {
            "check": "Answer supplied", "tool_call_ids": ["long1"], "status": "passed",
            "finding": "Current file reports version 2"})]),
        LLMResponse(content="The current version is 2.")])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     context_selection=ContextSelectionConfig(enabled=True), max_tool_result_chars=1000)
    loop.tools.register(LongRead())
    with patch.object(loop.sessions.get_or_create("cli:s"), "get_history",
                      side_effect=AssertionError("must not replay full history")):
        out = await loop._process_message(InboundMessage(
            channel="cli", sender_id="owner", chat_id="s", content="현재 파일을 조사해줘"))
    assert out.content == "The current version is 2."
    session = loop.sessions.get_or_create("cli:s")
    stored = next(m for m in session.messages if m.get("tool_call_id") == "long1")
    assert stored["content"] == long_result
    raw = loop.conversation_memory.get_events(loop._memory_scope_for_message(InboundMessage(
        channel="cli", sender_id="owner", chat_id="s", content="")), [stored["memory_event_id"]])[0]
    assert raw.content == long_result
    with loop.conversation_memory.connect() as conn:
        report = json.loads(conn.execute("SELECT metrics FROM context_selection_runs").fetchone()[0])
    assert [c["stage"] for c in report["calls"]] == ["design", "execute", "execute", "execute"]
    assert [c["call_sequence"] for c in report["calls"]] == [1, 2, 3, 4]
    assert all(c["task_id"] for c in report["calls"])
    assert "Essential data" not in json.dumps(report)
    assert len(provider.requests) == 4
    task = TaskContextStore(loop.conversation_memory).list(
        loop._memory_scope_for_message(InboundMessage(channel="cli", sender_id="owner", chat_id="s", content="")),
        "cli:s")[0]
    assert task["status"] == "DONE"
    assert any(c.get("source_event_ids") for c in task["completed"])


@pytest.mark.asyncio
async def test_opt_in_legacy_recovery_is_explicit_and_does_not_duplicate_user(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus

    provider = FakeProvider([LLMResponse(content="invalid"), LLMResponse(content="invalid"),
                             LLMResponse(content="Legacy recovery answer")])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     context_selection=ContextSelectionConfig(enabled=True, failure_mode="legacy"))
    with patch.object(loop.consolidator, "maybe_consolidate_by_tokens", return_value=None):
        out = await loop._process_message(InboundMessage(
            channel="cli", sender_id="owner", chat_id="s", content="2+2?"))
    assert out.content == "Legacy recovery answer"
    assert sum(m["role"] == "user" for m in loop.sessions.get_or_create("cli:s").messages) == 1
    assert len(provider.requests) == 3


@pytest.mark.asyncio
async def test_repeated_external_message_id_does_not_execute_twice(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus

    provider = FakeProvider([response(plan())])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     context_selection=ContextSelectionConfig(enabled=True))
    msg = InboundMessage(channel="cli", sender_id="owner", chat_id="s", content="2+2?",
                         metadata={"message_id": "delivery-1"})
    await loop._process_message(msg)
    out = await loop._process_message(msg)
    assert "duplicate_user_event" in out.content
    assert len(provider.requests) == 1
    assert sum(m["role"] == "user" for m in loop.sessions.get_or_create("cli:s").messages) == 1


@pytest.mark.asyncio
async def test_failed_read_and_discovery_cannot_unlock_changes(env):
    from nanobot.agent.tools.base import ToolResult
    class FailedRead(ReadTool):
        async def execute(self, **kwargs):
            return ToolResult.error("file version unavailable")
    env[2].register(FailedRead())
    event(env, "수정해줘")
    _, _, selected = await select(env, plan(route="execute", answer=None,
        information_needs=[need(source_kind="file")]), request="수정해줘")
    provider = FakeProvider([
        LLMResponse(content=None, tool_calls=[ToolCallRequest("r", "read_file", {})]),
        LLMResponse(content=None, tool_calls=[ToolCallRequest("resolve", "resolve_context_need", {
            "need_id": "n1", "tool_call_id": "r", "finding": "fabricated success"})]),
        LLMResponse(content="done"),
    ])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "modify"}], tools=selected.tools,
        model="offline", max_iterations=3, max_tool_result_chars=1000, selected_context=selected))
    assert selected.missing == ["n1"]
    assert result.stop_reason == "incomplete"


@pytest.mark.asyncio
async def test_midrun_user_amendment_is_preserved_and_task_not_done(env):
    event(env, "수정해줘. 배포는 제외.")
    selector, _, selected = await select(env, plan(route="execute", answer=None), request="수정해줘")
    event(env, "수정도 중단해줘")
    selector.finish(selected, [], stop_reason="completed", had_injections=True)
    state = selector.tasks.list(env[1], "s")[0]
    assert state["status"] == "BLOCKED"
    assert any(c["text"] == "수정도 중단해줘" for c in state["user_constraints"])
    assert "new_user_instruction_requires_reconciliation" in state["blockers"]


@pytest.mark.asyncio
async def test_executor_revision_adds_required_evidence_without_granting_authority(env):
    event(env, "설정을 조사해줘")
    _, _, selected = await select(env, plan(route="execute", answer=None), request="설정을 조사해줘")
    result = await selected.tools.execute("revise_context_requirements", {
        "reason": "evidence_conflict", "information_needs": [need(source_kind="file")],
        "completion_checks": ["Current version checked"]})
    assert "n1" in result
    assert selected.missing == ["n1"]
    assert "Current version checked" in selected.design.completion_checks
    assert selected.trace.counters["redesigns"] == 1
    assert selected.tools.prepare_call("read_file", {})[2] is None
    await selected.tools.execute("discover_context_capabilities", {"names": ["change_test_file"]})
    assert selected.tools.prepare_call("change_test_file", {})[2]
    bad = await selected.tools.execute("revise_context_requirements", {
        "reason": "missing_evidence", "information_needs": [need(id="n2", source_ids=["invented"])],
        "completion_checks": []})
    assert "unknown source" in bad


@pytest.mark.asyncio
async def test_historical_source_cannot_satisfy_current_file_need(env):
    current = event(env, "설정 파일 수정해줘")
    _, _, selected = await select(env, plan(route="execute", answer=None,
        information_needs=[need(source_kind="file", source_ids=[current.event_id])]), request="파일 수정해줘")
    assert selected.missing == ["n1"]
    selected.tools.resolver.observed["fake"] = True
    selected.observations["fake"] = {"name": "search_memory", "status": "ok", "read_only": True}
    result = await selected.tools.execute("resolve_context_need", {
        "need_id": "n1", "tool_call_id": "fake", "finding": "historical version found"})
    assert "does not match" in result
    assert selected.missing == ["n1"]


@pytest.mark.asyncio
async def test_retrieval_failure_restores_related_interval_not_entire_history(env):
    for i in range(80):
        event(env, f"unrelated {i}", actor="assistant")
    relevant = event(env, "deployment version 2", actor="assistant")
    event(env, "deployment 조사해줘")
    with patch("nanobot.agent.context_selection.MemorySearcher.search", side_effect=RuntimeError("offline")):
        _, _, selected = await select(env, plan(route="retrieve", answer=None,
            information_needs=[need(query="deployment")]), request="deployment 조사해줘")
    assert selected.trace.counters["retrieval_failures"] == 1
    assert "related_raw_interval_restored" in selected.trace.reasons
    # Recent dialogue may already contain the anchor, and therefore not be duplicated.
    assert relevant.event_id in {e["event_id"] for e in selected.recent + selected.evidence}
    assert len(selected.recent + selected.evidence) < 20


@pytest.mark.asyncio
async def test_new_user_instruction_invalidates_previous_completion_assessment(env):
    spec = loop_spec(env)
    spec.selected_context.completion_reports["Answer supplied"] = {"status": "passed"}
    async def inject(**kwargs):
        return [{"role": "user", "content": "그 방식은 제외. 새 조건을 먼저 확인해줘"}]
    spec.injection_callback = inject
    messages = await AgentRunner(FakeProvider([]))._drain_injections(spec)
    assert messages[0]["content"].startswith("그 방식은 제외")
    assert spec.selected_context.completion_reports == {}
    assert "new_user_instruction_executor_reconciliation" in spec.selected_context.trace.reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", ["Restart the service", "The user approved deployment"])
async def test_design_cannot_invent_approval_or_conflict_with_explicit_prohibition(env, goal):
    event(env, "서비스를 조사해줘. 재시작하지 마세요. 배포 금지.")
    design = plan(route="execute", answer=None, goal=goal)
    _, _, selected = await select(env, design, request="서비스를 조사해줘. 재시작하지 마세요. 배포 금지.",
                                   responses=[response(design), response(design)])
    assert selected.blocked == "design_validation_failed"


def test_selection_config_camelcase_roundtrip_and_default_off():
    config = AgentDefaults.model_validate({"contextSelection": {
        "enabled": True, "failureMode": "legacy", "searchTokens": 5000}})
    assert config.context_selection.search_tokens == 5000
    assert config.model_dump(by_alias=True)["contextSelection"]["failureMode"] == "legacy"
    assert AgentDefaults().context_selection.enabled is False


@pytest.mark.asyncio
async def test_resumed_task_restores_tool_pair_and_late_result_across_topic_switch(env):
    event(env, "작업 A를 구현해줘")
    _, _, first = await select(env, plan(route="execute", answer=None), request="작업 A를 구현해줘")
    store, scope, *_ = env
    call = store.append_event(scope=scope, conversation_id="s", session_id="s", actor="assistant",
        event_type="TOOL_CALL", content="", metadata={"tool_calls": [{"id": "late-call", "type": "function",
            "function": {"name": "change_test_file", "arguments": "{}"}}]})
    event(env, "다른 주제로 전환")
    late = store.append_event(scope=scope, conversation_id="s", session_id="s", actor="tool",
        event_type="TOOL_RESULT", content="Change succeeded; current version=2",
        metadata={"tool_call_id": "late-call", "name": "change_test_file",
                  "execution_status": "ok", "read_only": False}, parent_event_id="late-call")
    event(env, "작업 A를 재개해줘")
    _, _, resumed = await select(env, plan(route="execute", answer=None, task_relation="resume",
                                          task_id=first.state["task_id"]), request="작업 A를 재개해줘")
    evidence = {e["event_id"]: e for e in resumed.evidence}
    assert call.event_id in evidence and late.event_id in evidence
    assert evidence[call.event_id]["tool_calls"][0]["id"] == "late-call"
    assert evidence[late.event_id]["parent_event_id"] == "late-call"
    assert resumed.observations["late-call"]["historical"] is True
    assert resumed.state["last_event_id"] != late.event_id


@pytest.mark.asyncio
async def test_unknown_file_path_in_designer_output_is_rejected(env):
    event(env, "이전 설정을 조사해줘")
    bad = plan(route="retrieve", answer=None,
               information_needs=[need(query="invented/config.json")])
    _, _, selected = await select(env, bad, request="이전 설정을 조사해줘",
                                  responses=[response(bad), response(bad)])
    assert selected.blocked == "design_validation_failed"
    assert "unknown_material_path" in selected.trace.reasons


@pytest.mark.asyncio
async def test_provider_failure_attempt_is_traced_without_raw_error_or_credentials():
    from nanobot.agent.context_trace import active_context_trace
    class BrokenProvider(FakeProvider):
        async def chat(self, **kwargs):
            raise RuntimeError("credential=private-synthetic-key")
    trace = ContextTrace("safe-request", task_id="safe-task")
    token = active_context_trace.set(trace)
    try:
        result = await BrokenProvider([]).chat_with_retry(messages=[{"role": "user", "content": "secret request"}])
    finally:
        active_context_trace.reset(token)
    assert result.finish_reason == "error"
    assert len(trace.calls) == 1
    report = json.dumps(trace.report())
    assert "private-synthetic-key" not in report and "secret request" not in report
    assert trace.report()["provider_usage_missing_calls"] == 1


@pytest.mark.asyncio
async def test_internal_continuation_reuses_design_and_cumulative_request_trace(env):
    event(env, "작업을 조사해줘")
    selector, _, first = await select(env, plan(route="execute", answer=None), request="작업을 조사해줘")
    selector.finish(first, [], stop_reason="max_iterations", had_injections=False)
    provider = FakeProvider([])
    resumed = await selector.select(provider=provider, model="offline", request="internal continuation",
        scope=env[1], session_id="s", registry=env[2], skills=env[3], rules="mandatory",
        request_id="slice-2", continuation=True)
    assert provider.requests == []
    assert resumed.trace.request_id == first.trace.request_id
    assert resumed.trace.calls == first.trace.calls
    assert resumed.trace.counters["design_reuse"] == 1
    assert resumed.design.task_id == first.state["task_id"]


@pytest.mark.asyncio
async def test_system_result_uses_selected_context_without_new_design_or_full_history(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus
    provider = FakeProvider([response(plan(route="execute", answer=None)),
                             LLMResponse(content="Still checking."),
                             LLMResponse(content="Received background evidence.")])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     context_selection=ContextSelectionConfig(enabled=True))
    await loop._process_message(InboundMessage(channel="cli", sender_id="owner", chat_id="s",
                                              content="현재 자료를 조사해줘"))
    with patch.object(loop.sessions.get_or_create("cli:s"), "get_history",
                      side_effect=AssertionError("must not replay full history")):
        out = await loop._process_message(InboundMessage(channel="system", sender_id="subagent",
            chat_id="cli:s", content="Observed test fixture version=2",
            metadata={"subagent_task_id": "background-1"}))
    assert "Received background evidence" in out.content
    assert len(provider.requests) == 3
    assert sum(bool(r.get("tools")) and r["tools"][0].get("function", {}).get("name")
               == "submit_context_design" for r in provider.requests) == 1
    session = loop.sessions.get_or_create("cli:s")
    assert sum(m["role"] == "user" for m in session.messages) == 1
    with loop.conversation_memory.connect() as conn:
        metrics = json.loads(conn.execute("SELECT metrics FROM context_selection_runs").fetchone()[0])
    assert len(metrics["calls"]) == 3
    assert metrics["counters"]["design_reuse"] == 1


def test_old_explicit_user_constraint_reaches_designer_even_outside_recent_dialogue(env):
    original = event(env, "항상 한국어로 답하세요. 서비스를 재시작하지 마세요.")
    for i in range(20):
        event(env, f"unrelated question {i}")
        event(env, f"unrelated answer {i}", actor="assistant")
    selector = ContextSelector(env[0], ContextSelectionConfig())
    payload = selector.minimal_input("2+2?", env[1], "s", env[2], env[3])
    preserved = next(s for s in payload["sources"] if s["event_id"] == original.event_id)
    assert "재시작하지" in preserved["content"]
    assert preserved["excerpt_only"] is True
    assert payload["task_state_status"] == "missing"


@pytest.mark.asyncio
async def test_numbered_comparison_and_followup_use_one_design_call_each(tmp_path):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    from nanobot.bus.queue import MessageBus

    request = ("무료, 오프라인, 회원가입 불필수 조건으로 답해줘.\n"
               "Q1. 텍스트의 장단점은?\nQ2. 마크다운의 장단점은?\n"
               "Q3. 혼합 폴더 구성은?\n앱 설치나 파일 생성은 하지 마.")
    answer = "Q1. 텍스트는 단순합니다.\nQ2. 마크다운은 제목과 목록이 편합니다.\nQ3. inbox, notes, archive로 구분합니다."
    provider = FakeProvider([response(plan(request_type="compare", answer=answer))])
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path,
                     context_selection=ContextSelectionConfig(enabled=True))
    first = InboundMessage(channel="cli", sender_id="owner", chat_id="s", content=request)
    session = loop.sessions.get_or_create("cli:s")
    with patch.object(session, "get_history", side_effect=AssertionError("no full replay")):
        out = await loop._process_message(first)
        assert out.content == answer
        scope = loop._memory_scope_for_message(first)
        task = TaskContextStore(loop.conversation_memory).list(scope, "cli:s")[0]
        provider.responses = iter([response(plan(
            task_relation="continue", task_id=task["task_id"],
            answer="Q2. # 제목과 - 목록을 사용합니다.\nQ3. inbox는 초안, notes는 정리, archive는 보관입니다."))])
        followup = InboundMessage(channel="cli", sender_id="owner", chat_id="s",
                                 content="Q2,, Q3만 더 구체적으로 설명해줘. 처음 조건은 그대로 유지해.")
        out = await loop._process_message(followup)
    assert "Q2." in out.content and "Q3." in out.content
    assert "완료 조건의 실행" not in out.content
    assert len(provider.requests) == 2
    payload = json.loads(provider.requests[-1]["messages"][1]["content"])
    assert payload["references"]["questions"]["status"] == "candidate"
    assert any("무료, 오프라인" in s["content"] for s in payload["sources"])
    assert any(answer == s["content"] for s in payload["sources"])
    assert TaskContextStore(loop.conversation_memory).list(scope, "cli:s")[0]["status"] == "DONE"


@pytest.mark.asyncio
async def test_known_dialogue_originals_do_not_search_or_require_tool_completion(env):
    original = event(env, "무료, 오프라인. Q2. 마크다운은? Q3. 폴더는? 생성하지 마.")
    previous = event(env, "Q2. 제목과 목록. Q3. inbox, notes, archive.", actor="assistant")
    request = "Q2,, Q3만 더 구체적으로 설명해줘. 처음 조건은 그대로 유지해."
    event(env, request)
    design = plan(route="retrieve", answer=None, information_needs=[need(
        query="Q2 Q3 원문", source_ids=[original.event_id, previous.event_id])])
    with patch("nanobot.agent.context_selection.MemorySearcher.search",
               side_effect=AssertionError("known originals must be read without search")):
        selector, _, selected = await select(env, design, request=request)
    assert not selected.missing
    provider = FakeProvider([LLMResponse(content="Q2. 제목은 #, 목록은 -로 적습니다. Q3. 초안과 정리본을 구분합니다.")])
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": request}], tools=selected.tools,
        model="offline", max_iterations=2, max_tool_result_chars=1000, selected_context=selected))
    assert result.stop_reason == "completed"
    assert len(provider.requests) == 1
    assert not result.tools_used
    selector.finish(selected, result.messages, stop_reason=result.stop_reason, had_injections=False)
    task = TaskContextStore(env[0]).list(env[1], "s")[0]
    assert task["status"] == "DONE"
    assert task["completed"][-1]["verification"] == "delivery_only; semantic_quality_unverified"
    assert selected.trace.counters["completion_tool_supported"] == 0


def test_unresolved_question_reference_still_rejects_direct(env):
    from nanobot.agent.context_selection import validate_meaning

    request = "Q2,, Q3를 설명해줘"
    event(env, request)
    selector = ContextSelector(env[0], ContextSelectionConfig())
    payload = selector.minimal_input(request, env[1], "s", env[2], env[3])
    assert payload["references"]["questions"]["status"] == "unresolved"
    assert "unsafe_direct" in validate_meaning(plan(), payload)


@pytest.mark.asyncio
async def test_runtime_feedback_is_never_reinterpreted_as_a_user_decision(env):
    from nanobot.agent.premature_final import CORRECTION

    control = event(env, CORRECTION)
    event(env, "앱 설치하지 마. Q1. 텍스트는? Q2. 마크다운은?")
    selector, _, selected = await select(env, plan(request_type="compare"), request="비교해줘")
    payload = selector.minimal_input("비교해줘", env[1], "s", env[2], env[3])
    assert control.event_id not in {s["event_id"] for s in payload["sources"]}
    assert all(c["event_id"] != control.event_id for c in selected.state["user_constraints"])
    assert env[0].get_events(env[1], [control.event_id])[0].content == CORRECTION


@pytest.mark.asyncio
async def test_new_topic_does_not_bootstrap_unrelated_dialogue_as_task_constraints(env):
    unrelated = event(env, "일본어 학습을 시작해줘")
    current = event(env, "2+2?")
    _, _, selected = await select(env, plan())
    ids = {c["event_id"] for c in selected.state["user_constraints"]}
    assert ids == {current.event_id}
    assert unrelated.event_id in {e["event_id"] for e in selected.recent}


@pytest.mark.asyncio
async def test_answering_followup_does_not_complete_unfinished_execution(env):
    event(env, "파일을 수정해줘")
    selector, _, first = await select(env, plan(
        route="execute", request_type="modify", answer=None, completion_checks=["File modified"]))
    event(env, "변경 방법을 설명해줘")
    followup = plan(task_relation="continue", task_id=first.state["task_id"],
                    completion_checks=["Explanation supplied"], answer="검토 후 수정하고 다시 읽어 검증합니다.")
    _, _, selected = await select(env, followup, request="변경 방법을 설명해줘")
    selector.finish(selected, [{"role": "assistant", "content": followup.answer}],
                    stop_reason="completed", had_injections=False)
    task = TaskContextStore(env[0]).list(env[1], "s")[0]
    assert task["status"] == "IN_PROGRESS"
    assert "File modified" in task["unfinished"]
    assert "Explanation supplied" not in task["unfinished"]


@pytest.mark.asyncio
async def test_wrong_read_id_exposes_actual_ids_without_repeating_successful_read(env):
    event(env, "파일을 확인해줘")
    _, _, selected = await select(env, plan(route="execute", answer=None,
        information_needs=[need(source_kind="file")]))
    selected.tools.resolver.observed["actual-read-id"] = True
    result = await selected.tools.resolver.execute("n1", "invented-id", "확인했습니다")
    assert "actual-read-id" in result
    assert selected.missing == ["n1"]


@pytest.mark.asyncio
async def test_design_usage_survives_interruption_before_executor_finish(env):
    event(env, "2+2?")
    _, _, selected = await select(env, plan())
    with env[0].connect() as conn:
        row = conn.execute("SELECT metrics FROM context_selection_runs WHERE request_id=?",
                           (selected.trace.request_id,)).fetchone()
    metrics = json.loads(row[0])
    assert metrics["provider_total_tokens"] == 120
    assert len(metrics["calls"]) == 1


@pytest.mark.asyncio
async def test_mislabeled_execution_cannot_use_prose_completion_exemption(env):
    event(env, "파일 수정해줘")
    _, _, selected = await select(env, plan(route="retrieve", answer=None), request="파일 수정해줘")
    assert not selected.answer_only()
    assert not selected.completion_supported()


@pytest.mark.asyncio
async def test_related_history_need_still_searches_when_id_is_only_a_clue(env):
    current = event(env, "이전 메모 결정이 있는지 조사해줘")
    from nanobot.memory.search import MemorySearcher

    with patch("nanobot.agent.context_selection.MemorySearcher.search",
               autospec=True, side_effect=MemorySearcher.search) as search:
        await select(env, plan(route="retrieve", request_type="investigate", answer=None,
            information_needs=[need(scope="related_history", source_ids=[current.event_id])]),
            request=current.content)
    assert search.call_count == 1


@pytest.mark.asyncio
async def test_direct_preamble_is_repaired_before_delivery(env):
    request = "마크다운 장단점을 자세히 설명해줘"
    event(env, request)
    _, provider, selected = await select(env, plan(answer="프랭크, 핵심을 정리합니다."),
        request=request, responses=[response(plan(answer="프랭크, 핵심을 정리합니다.")),
                                   response(plan(answer="# 제목과 - 목록을 쓰면 구조를 쉽게 표현합니다."))])
    assert len(provider.requests) == 2
    assert "answer_body_missing" in provider.requests[-1]["messages"][-1]["content"]
    assert "핵심을 정리합니다" in provider.requests[-1]["messages"][-2]["content"]
    assert selected.design.answer.startswith("# 제목")



def test_executor_can_see_exact_runtime_ids_without_changing_original_protocol(env):
    from nanobot.providers.openai_responses import convert_messages

    spec = loop_spec(env)
    cid = "runtime-call|provider-item"
    spec.selected_context.observations[cid] = {
        "name": "read_file", "status": "ok", "read_only": True}
    messages = [{"role": "system", "content": "Preserve user constraints"},
                {"role": "assistant", "tool_calls": [{"id": cid, "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": cid, "name": "read_file", "content": "version=2"}]
    original = copy.deepcopy(messages)
    model = prepare_selected_loop(spec, messages, FakeProvider([]))
    assert cid in model[0]["content"]
    assert messages == original
    assert model[-1] == original[-1]
    converted = convert_messages(model)
    assert cid in json.dumps(converted)



@pytest.mark.asyncio
async def test_design_deadline_never_starts_second_slow_call(env):
    import asyncio

    from nanobot.providers.base import hosted_tools_enabled

    event(env, "2+2?")
    timeout = asyncio.timeout
    attempts = []
    cancelled = []

    class SlowProvider(FakeProvider):
        async def chat(self, messages, **kwargs):
            attempts.append(hosted_tools_enabled())
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

    selector = ContextSelector(env[0], ContextSelectionConfig(enabled=True))
    with patch("nanobot.agent.context_selection.asyncio.timeout",
               side_effect=lambda seconds: timeout(0.03)) as deadline:
        selected = await selector.select(provider=SlowProvider([]), model="offline", request="2+2?",
            scope=env[1], session_id="s", registry=env[2], skills=env[3],
            rules="Mandatory policy", request_id="timeout")
    assert attempts == [False]
    assert cancelled == [True]
    deadline.assert_called_once_with(90)
    assert selected.blocked == "design_timeout_initial"
    assert selected.trace.counters["design_timeouts"] == 1
    assert selected.trace.counters["design_timeouts_initial"] == 1
    assert "design_timeouts_repair" not in selected.trace.counters
    assert "design_timeout_initial" in selected.trace.reasons
    assert "design_recoveries" not in selected.trace.counters
    assert selected.trace.report()["provider_usage_status"] == "unavailable"
    assert hosted_tools_enabled()


@pytest.mark.asyncio
async def test_format_repair_gets_its_own_short_deadline_and_low_effort(env):
    import asyncio

    from nanobot.providers.base import hosted_tools_enabled

    event(env, "2+2?")
    timeout = asyncio.timeout
    settings = []

    class InvalidThenSlow(FakeProvider):
        async def chat(self, messages, **kwargs):
            settings.append((kwargs["reasoning_effort"], hosted_tools_enabled()))
            if len(settings) == 1:
                return LLMResponse(content="bad json")
            await asyncio.sleep(10)

    selector = ContextSelector(env[0], ContextSelectionConfig(enabled=True))
    with patch("nanobot.agent.context_selection.asyncio.timeout",
               side_effect=lambda seconds: timeout(0.04)) as deadline:
        selected = await selector.select(provider=InvalidThenSlow([]), model="offline", request="2+2?",
            scope=env[1], session_id="s", registry=env[2], skills=env[3],
            rules="Mandatory policy", request_id="repair-timeout")
    assert settings == [("low", False), ("low", False)]
    assert [c.args[0] for c in deadline.call_args_list] == [90, 12]
    assert selected.blocked == "design_timeout_repair"
    assert selected.trace.counters["design_timeouts"] == 1
    assert selected.trace.counters["design_timeouts_repair"] == 1
    assert "design_timeouts_initial" not in selected.trace.counters
    assert selected.trace.counters["design_recoveries"] == 1
    assert selected.trace.report()["provider_usage_status"] == "unavailable"


@pytest.mark.asyncio
async def test_repair_budget_is_separate_from_initial_budget(env):
    """Initial call using most of its budget must not starve the repair call."""
    import asyncio

    event(env, "2+2?")
    timeout = asyncio.timeout
    budgets = []

    class SlowInvalidThenSlowValid(FakeProvider):
        async def chat(self, messages, **kwargs):
            self.requests.append(kwargs)
            await asyncio.sleep(0.12)
            return LLMResponse(content="bad json") if len(self.requests) == 1 else response(plan())

    def scaled(seconds):
        budgets.append(seconds)
        return timeout(seconds / 100)  # 20s -> 0.2s, 12s -> 0.12s+; shared deadline would expire

    selector = ContextSelector(env[0], ContextSelectionConfig(
        enabled=True, design_timeout_seconds=20, design_repair_timeout_seconds=15))
    provider = SlowInvalidThenSlowValid([])
    with patch("nanobot.agent.context_selection.asyncio.timeout", side_effect=scaled):
        selected = await selector.select(provider=provider, model="offline", request="2+2?",
            scope=env[1], session_id="s", registry=env[2], skills=env[3],
            rules="Mandatory policy", request_id="separate-budget")
    assert budgets == [20, 15]
    assert len(provider.requests) == 2
    assert selected.blocked is None
    assert selected.design.answer == "4"
    assert "design_timeouts" not in selected.trace.counters
    assert selected.trace.counters["design_recoveries"] == 1
    starts = [m for c in selected.trace.calls for m in c["milestones"]
              if m["phase"] in {"design_deadline_started", "design_repair_deadline_started"}]
    assert [(m["phase"], m["timeout_seconds"], m["status"]) for m in starts] == [
        ("design_deadline_started", 20, "initial"), ("design_repair_deadline_started", 15, "repair")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "reason"), [
    (LLMResponse(content=""), "design_empty_response"),
    (LLMResponse(content=None, tool_calls=[]), "design_empty_response"),
    (LLMResponse(content=None, tool_calls=[ToolCallRequest(
        id="|fc1", name="submit_context_design", arguments=plan().model_dump())]),
     "design_tool_call_incomplete"),
])
async def test_empty_or_unattributed_design_response_is_not_recalled(env, reply, reason):
    event(env, "2+2?")
    _, provider, selected = await select(env, plan(), responses=[reply, response(plan())])
    assert len(provider.requests) == 1
    assert selected.design is None
    assert selected.blocked == reason
    assert selected.trace.counters["design_unusable_responses"] == 1
    assert reason in selected.trace.reasons
    assert "design_recoveries" not in selected.trace.counters
    assert "design_timeouts" not in selected.trace.counters


@pytest.mark.asyncio
async def test_auth_resend_runs_inside_the_initial_budget_and_is_counted(env):
    import asyncio

    from nanobot.providers.call_observation import observe

    event(env, "2+2?")
    timeout = asyncio.timeout

    class AuthResendThenSlow(FakeProvider):
        async def chat(self, messages, **kwargs):
            observe("authentication_retry_started", http_status=401)
            await asyncio.sleep(10)  # the resent request hangs

    selector = ContextSelector(env[0], ContextSelectionConfig(enabled=True))
    with patch("nanobot.agent.context_selection.asyncio.timeout",
               side_effect=lambda seconds: timeout(0.03)) as deadline:
        selected = await selector.select(provider=AuthResendThenSlow([]), model="offline", request="2+2?",
            scope=env[1], session_id="s", registry=env[2], skills=env[3],
            rules="Mandatory policy", request_id="auth-resend")
    deadline.assert_called_once_with(90)
    assert selected.blocked == "design_timeout_initial"
    assert selected.trace.counters["design_auth_resends"] == 1
    assert len(selected.trace.calls) == 1


@pytest.mark.asyncio
async def test_design_call_uses_bounded_transient_retries(env):
    event(env, "2+2?")
    for early in (False,):  # t28: only the completed-result path exists
        seen = {}

        class Recording(FakeProvider):
            async def chat_with_retry(self, **kwargs):
                seen.update(kwargs, path="completed")
                return response(plan())

            async def chat_stream_with_retry(self, **kwargs):
                seen.update(kwargs, path="stream")
                return response(plan())

        selector = ContextSelector(env[0], ContextSelectionConfig(
            enabled=True, design_transient_retries=0))
        selected = await selector.select(provider=Recording([]), model="offline", request="2+2?",
            scope=env[1], session_id="s", registry=env[2], skills=env[3],
            rules="Mandatory policy", request_id="bounded-retry")
        assert selected.blocked is None
        assert seen["path"] == ("stream" if early else "completed")
        assert seen["max_retries"] == 0
        assert seen["max_retry_delay_s"] == 2.0
        if early:
            assert seen["retry_after_streamed_content"] is True


@pytest.mark.asyncio
async def test_design_provider_error_does_not_trigger_json_repair(env):
    event(env, "2+2?")
    _, provider, selected = await select(env, plan(), responses=[
        LLMResponse(content="Synthetic non-retryable provider failure", finish_reason="error")])
    assert len(provider.requests) == 1
    assert selected.blocked == "design_provider_error"
    assert "design_recoveries" not in selected.trace.counters


def test_designer_gets_permission_rules_but_no_bootstrap_files_or_tool_tutorials(tmp_path):
    from nanobot.agent.context import ContextBuilder

    policies = {"AGENTS.md": "Do not restart services. Await explicit stage approval.",
                "SOUL.md": "Report verified results only.", "USER.md": "Always answer in Korean."}
    for name, text in policies.items():
        (tmp_path / name).write_text(text)
    builder = ContextBuilder(tmp_path)
    designer = builder.build_design_system_prompt(channel="telegram")
    executor = builder.build_selected_system_prompt(channel="telegram")
    full = builder.build_system_prompt(channel="telegram")
    for text in policies.values():
        assert text not in designer
        assert text in executor and text in full
    assert "## AGENTS.md" not in designer
    assert "Explicit Permission Gates" in designer
    assert "General Tool Contract" not in designer
    assert "General Tool Contract" in executor
    assert len(designer) < len(executor)


def test_design_timeout_and_effort_are_explicit_config_and_execution_unchanged():
    defaults = AgentDefaults()
    assert defaults.context_selection.design_timeout_seconds == 90
    assert defaults.context_selection.design_repair_timeout_seconds == 12
    assert defaults.context_selection.design_transient_retries == 1
    assert defaults.context_selection.design_reasoning_effort == "low"
    config = AgentDefaults.model_validate({"reasoningEffort": "high", "contextSelection": {
        "designTimeoutSeconds": 15, "designReasoningEffort": "medium"}})
    assert config.reasoning_effort == "high"
    assert config.context_selection.design_reasoning_effort == "medium"
    assert config.model_dump(by_alias=True)["contextSelection"]["designTimeoutSeconds"] == 15



@pytest.mark.asyncio
async def test_user_stop_cancels_designer_without_repair_and_retains_partial_trace(env):
    import asyncio

    from nanobot.providers.base import hosted_tools_enabled

    event(env, "2+2?")
    entered = asyncio.Event()
    calls = []

    class WaitingProvider(FakeProvider):
        async def chat(self, messages, **kwargs):
            calls.append(True)
            entered.set()
            await asyncio.sleep(10)

    selector = ContextSelector(env[0], ContextSelectionConfig(enabled=True))
    pending = asyncio.create_task(selector.select(provider=WaitingProvider([]), model="offline",
        request="2+2?", scope=env[1], session_id="s", registry=env[2], skills=env[3],
        rules="Keep permissions", request_id="user-stop"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    with env[0].connect() as conn:
        metrics = json.loads(conn.execute(
            "SELECT metrics FROM context_selection_runs WHERE request_id='user-stop'").fetchone()[0])
    assert len(calls) == 1
    assert metrics["counters"]["cancelled"] == 1
    assert "design_recoveries" not in metrics["counters"]
    assert metrics["provider_usage_status"] == "unavailable"
    assert hosted_tools_enabled()


# ---- Minimal design input (patch 3) ----

def test_designer_prompt_excludes_real_bootstrap_templates_and_execution_prompt_keeps_them(tmp_path):
    from nanobot.agent.context import ContextBuilder
    from nanobot.utils.helpers import sync_workspace_templates

    sync_workspace_templates(tmp_path, silent=True)
    builder = ContextBuilder(tmp_path)
    designer = builder.build_design_system_prompt(channel="cli")
    executor = builder.build_system_prompt(channel="cli")
    bootstrap = builder._load_bootstrap_files(tmp_path)
    assert bootstrap and bootstrap in executor
    for name in ContextBuilder.BOOTSTRAP_FILES:
        assert f"## {name}" not in designer and f"## {name}" in executor
    assert "submit_context_design" in designer
    assert len(designer) < len(bootstrap)


def test_capabilities_keep_every_name_with_short_sorted_descriptions(env):
    from nanobot.agent.context_selection import validate_meaning

    store, scope, registry, skills = env

    class LongTool(Tool):
        name = "aaa_long_tool"
        description = "First sentence is kept. " + "Second sentence is long and dropped. " * 20
        parameters = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return "ok"

    class HugeTool(LongTool):
        name = "zzz_huge"
        description = "x" * 500

    registry.register(HugeTool())
    registry.register(LongTool())
    payload = ContextSelector(store, ContextSelectionConfig()).minimal_input(
        "2+2?", scope, "s", registry, skills)
    caps = payload["capabilities"]
    tool_names = [c["name"] for c in caps[:len(registry.tool_names)]]
    assert tool_names == sorted(registry.tool_names)
    skill_names = {s["name"] for s in skills.list_skills()}
    assert {c["name"] for c in caps} == set(registry.tool_names) | skill_names
    assert all(len(c["description"]) <= 80 for c in caps)
    long = next(c for c in caps if c["name"] == "aaa_long_tool")
    assert long["description"] == "First sentence is kept."
    assert next(c for c in caps if c["name"] == "zzz_huge")["description"].endswith("…")
    # Validation still recognises every real name and rejects unknown ones.
    ok = plan(route="execute", answer=None, request_type="modify",
              capabilities_needed=["zzz_huge", "aaa_long_tool"])
    assert "unknown_capability" not in validate_meaning(ok, payload)
    bad = plan(route="execute", answer=None, request_type="modify", capabilities_needed=["invented"])
    assert "unknown_capability" in validate_meaning(bad, payload)


def test_standing_rule_scan_is_bounded_newest_first_and_deduplicated(env):
    from nanobot.agent.context_selection import _RULE_EXCERPT_MAX_RECORDS

    oldest = event(env, "always use metric units (oldest rule)")
    for i in range(30):
        event(env, f"항상 규칙 {i}번을 지켜 주세요.")
        event(env, "ok", actor="assistant")
    for _ in range(3):
        event(env, "always reply briefly.")  # identical line repeated: one excerpt only
    for i in range(10):
        event(env, f"plain question {i}")
        event(env, "plain answer", actor="assistant")
    selector = ContextSelector(env[0], ContextSelectionConfig())
    payload = selector.minimal_input("2+2?", env[1], "s", env[2], env[3])
    excerpts = [s for s in payload["sources"] if s.get("excerpt_only")]
    assert len(excerpts) == _RULE_EXCERPT_MAX_RECORDS
    texts = [s["content"] for s in excerpts]
    assert sum("always reply briefly." in t for t in texts) == 1
    assert "항상 규칙 29번을 지켜 주세요." in texts      # newest kept
    assert all(oldest.event_id != s["event_id"] for s in excerpts)  # oldest dropped by the cap
    assert payload["omitted_rule_excerpts"] > 0
    assert all(len(t) <= 300 for t in texts)


def test_standing_rule_scan_ignores_user_events_outside_tail_window(env):
    from nanobot.agent import context_selection as cs

    old = event(env, "always answer in Korean.")
    for i in range(5):
        event(env, f"q{i}")
    with patch.object(cs, "_RULE_SCAN_TAIL_USER_EVENTS", 3):
        payload = ContextSelector(env[0], ContextSelectionConfig(recent_messages=2)).minimal_input(
            "2+2?", env[1], "s", env[2], env[3])
    assert old.event_id not in {s["event_id"] for s in payload["sources"]}
    assert "omitted_rule_excerpts" not in payload


def _direct_payload_with_constraints(selector, env, ids):
    payload = selector.minimal_input("2+2?", env[1], "s", env[2], env[3])
    payload["task_candidates"] = [{"task_id": "t1", "goal": "g", "status": "ACTIVE"}]
    payload["task_state"] = {"task_id": "t1", "constraint_source_ids": ids,
                             "valid_constraint_candidates": []}
    return payload


def test_unrestorable_constraint_ids_are_unresolved_and_not_demanded_by_validation(env):
    from nanobot.agent.context_selection import validate_meaning

    store = env[0]
    other_session = event(env, "다른 세션 규칙: 배포하지 마.", session="other")
    assistant = event(env, "assistant proposal", actor="assistant")
    redacted = event(env, "삭제된 규칙")
    with store.connect() as conn:
        conn.execute("UPDATE events SET redacted_at='2026-01-01' WHERE event_id=?", (redacted.event_id,))
    loadable = event(env, "재시작하지 마세요.")
    for i in range(12):  # push the loadable one out of the recent window
        event(env, f"filler {i}")
    selector = ContextSelector(store, ContextSelectionConfig(recent_messages=2))
    ids = [other_session.event_id, assistant.event_id, redacted.event_id, "missing-id", loadable.event_id]
    payload = _direct_payload_with_constraints(selector, env, ids)
    assert loadable.event_id not in {s["event_id"] for s in payload["sources"]}
    restored = selector.restore_design_sources(payload, env[1], "s")
    assert restored == 1
    assert loadable.event_id in {s["event_id"] for s in payload["sources"]}
    reasons = {u["source_id"]: u["reason"] for u in payload["unresolved_constraint_source_ids"]}
    assert reasons == {other_session.event_id: "other_session", assistant.event_id: "not_user",
                       redacted.event_id: "missing_or_redacted", "missing-id": "missing_or_redacted"}
    design = plan(task_id="t1", task_relation="continue")
    assert "unloaded_task_constraints" not in validate_meaning(design, payload)
    # A loadable-but-absent original is still flagged.
    payload["sources"] = [s for s in payload["sources"] if s["event_id"] != loadable.event_id]
    assert "unloaded_task_constraints" in validate_meaning(design, payload)


@pytest.mark.asyncio
async def test_unresolved_constraint_ids_and_input_sizes_are_traced(env):
    other = event(env, "never deploy", session="other")
    event(env, "2+2?")
    selector = ContextSelector(env[0], ContextSelectionConfig(enabled=True))
    real_minimal = selector.minimal_input

    def with_task_state(*args, **kwargs):
        payload = real_minimal(*args, **kwargs)
        payload["task_state"] = {"task_id": "t1", "constraint_source_ids": [other.event_id],
                                 "valid_constraint_candidates": []}
        return payload

    selector.minimal_input = with_task_state
    selected = await selector.select(provider=FakeProvider([response(plan())]), model="offline",
        request="2+2?", scope=env[1], session_id="s", registry=env[2], skills=env[3],
        rules="Mandatory policy", request_id="unresolved")
    assert selected.trace.counters["unresolved_constraint_sources"] == 1
    assert "constraint_source_other_session" in selected.trace.reasons
    milestones = [m for c in selected.trace.calls for m in c["milestones"]]
    assert any(m["phase"] == "constraint_sources_unresolved" and m["count"] == 1 for m in milestones)
    sections = {m["status"]: m for m in milestones if m["phase"] == "design_input_section"}
    assert set(sections) == {"rules", "sources", "capabilities", "task_state", "other", "total"}
    assert all(m["bytes"] >= 0 and m["estimated_tokens"] >= 0 for m in sections.values())
    assert sections["total"]["bytes"] >= sections["rules"]["bytes"] + sections["sources"]["bytes"]


# ---- Validation aligned with available data (patch 4) ----

async def _task_with_unrecent_constraint(env):
    original = event(env, "이 작업의 출력 언어는 한국어이며 예산은 0원입니다.")
    _, _, first = await select(env, plan(route="retrieve", answer=None), request="설명해줘")
    for index in range(45):
        event(env, f"무관한 질문 {index}")
        event(env, f"무관한 답변 {index}", actor="assistant")
    request = "같은 조건으로 간단한 예시를 들어줘"
    event(env, request)
    design = plan(task_relation="continue", task_id=first.state["task_id"],
                  answer="같은 조건(한국어, 예산 0원)으로 만든 간단한 예시입니다.")
    return original, request, design


async def _select_with(env, request, design, restore_mode):
    selector = ContextSelector(env[0], ContextSelectionConfig(enabled=True))
    real = selector.restore_design_sources
    calls = []

    def restore(payload, scope, session_id):
        calls.append(restore_mode)
        if restore_mode == "never" or len(calls) == 1 and restore_mode == "late":
            return 0  # simulate a payload that is missing loadable originals at validation time
        return real(payload, scope, session_id)

    selector.restore_design_sources = restore
    provider = FakeProvider([response(design), response(design)])
    selected = await selector.select(provider=provider, model="offline", request=request,
        scope=env[1], session_id="s", registry=env[2], skills=env[3],
        rules="Mandatory policy", request_id="data-missing-" + restore_mode)
    return provider, selected, calls


@pytest.mark.asyncio
async def test_same_direct_design_passes_when_constraint_original_is_loaded(env):
    _, request, design = await _task_with_unrecent_constraint(env)
    provider, selected, _ = await _select_with(env, request, design, "real")
    assert selected.blocked is None and selected.design.route == "direct"
    assert len(provider.requests) == 1
    assert "design_recoveries" not in selected.trace.counters


@pytest.mark.asyncio
async def test_missing_constraint_data_is_restored_in_code_without_repair_call(env):
    _, request, design = await _task_with_unrecent_constraint(env)
    provider, selected, calls = await _select_with(env, request, design, "late")
    assert len(provider.requests) == 1          # no repair call
    assert len(calls) == 2                      # pre-design + in-validation restore
    assert selected.blocked is None
    phases = [m["phase"] for c in selected.trace.calls for m in c["milestones"]]
    assert "constraint_data_restore_finished" in phases
    assert "design_recoveries" not in selected.trace.counters


@pytest.mark.asyncio
async def test_unrestorable_constraint_data_fails_clearly_without_repair_call(env):
    _, request, design = await _task_with_unrecent_constraint(env)
    provider, selected, _ = await _select_with(env, request, design, "never")
    assert len(provider.requests) == 1
    assert selected.design is None
    assert selected.blocked == "design_constraint_data_missing"
    counters = selected.trace.counters
    assert counters["repair_skipped_data_missing"] == 1
    assert counters["design_constraint_data_missing"] == 1
    assert "design_recoveries" not in counters
    phases = [m["phase"] for c in selected.trace.calls for m in c["milestones"]]
    assert "repair_skipped_data_missing" in phases


@pytest.mark.parametrize(("text", "original", "ok"), [
    ("Always answer in Korean", "always answer in Korean.", True),
    ("ＡＢＣ 형식으로", "ABC 형식으로 정리", True),
    ("한국어로 답하세요", "항상 한국어로 답해 주세요.", True),
    ("재시작하지 마세요", "서비스를 재시작하지 마십시오.", True),
    ("예산은 0원", "이 작업의 출력 언어는 한국어이며\n예산은 0원입니다.", True),
    ("배포를 허용한다", "로그를 확인해 주세요.", False),
    ("서비스를 재시작하세요", "서비스를 재시작하지 마세요.", False),
    ("do not delete files", "delete files when done", False),
    ("영어", "한국어로 답하세요", False),
])
def test_constraint_text_normalized_and_paraphrase_matching(text, original, ok):
    from nanobot.agent.context_selection import constraint_text_supported
    assert constraint_text_supported(text, original) is ok


def test_constraint_reference_and_text_checks_in_validation(env):
    from nanobot.agent.context_selection import validate_meaning

    user = event(env, "항상 한국어로 답해 주세요.")
    bot = event(env, "한국어로 답하겠습니다.", actor="assistant")
    payload = ContextSelector(env[0], ContextSelectionConfig()).minimal_input("2+2?", env[1], "s", env[2], env[3])

    def check(text, ids):
        return "unsupported_user_constraint" in validate_meaning(
            plan(constraints=[Constraint(text=text, source_ids=ids)]), payload)

    assert not check("한국어로 답하세요", [user.event_id])          # paraphrase of a user source
    assert check("영어로 답하세요", [user.event_id])                # unrelated text
    assert check("한국어로 답하세요", [bot.event_id])               # non-user source
    assert check("한국어로 답하세요", ["invented"])                 # unknown reference
    payload["task_state"] = {"valid_constraint_candidates": [
        {"source_id": "cand-1", "text": "예산은 0원입니다.", "status": "user_original"}]}
    assert not check("예산은 0원", ["cand-1"])                        # task-state candidate reference


@pytest.mark.parametrize(("request_text", "prior", "unsafe"), [
    ("테스트 주도 개발을 설명해줘", False, False),
    ("구현 방식 A와 B를 비교해줘", False, False),
    ("How does a search index work?", False, False),
    ("이전 답변을 한 줄로 요약해줘", True, False),
    # Imperative action phrases are a validator-class signal (reroute, no repair), not unsafe_direct.
    ("파일 구현해줘", False, False),
    ("테스트를 실행해 주세요", False, False),
    ("please fix the failing test", False, False),
    ("서버에 배포해", False, False),
    # A resume word alone is not an obligation; the design's task_relation decides (below).
    ("계속 진행해줘", True, False),
    ("이전 답변을 요약해줘", False, True),
])
def test_unsafe_direct_only_for_actions_or_unloaded_context(request_text, prior, unsafe):
    from nanobot.agent.context_selection import validate_meaning

    sources = [{"event_id": "cur", "actor": "user", "content": request_text, "sequence": 2}]
    if prior:
        sources.insert(0, {"event_id": "a1", "actor": "assistant", "content": "earlier", "sequence": 1})
    payload = {"request": request_text, "sources": sources, "capabilities": [], "task_candidates": [],
               "references": {}, "known_material_clues": []}
    design = plan(answer="A complete explanation with the requested example and comparison.")
    assert ("unsafe_direct" in validate_meaning(design, payload)) is unsafe


@pytest.mark.parametrize(("request_text", "word"), [
    ("파일 구현해줘", True), ("테스트를 실행해 주세요", True), ("please fix the failing test", True),
    ("서버에 배포해", True), ("인터넷 검색은 필요 없습니다", False), ("검색하지 마", False),
    ("테스트 주도 개발을 설명해줘", False),
])
def test_imperative_action_word_is_validator_class(request_text, word):
    from nanobot.agent.context_selection import error_class, validate_meaning

    payload = {"request": request_text, "sources": [{"event_id": "cur", "actor": "user",
               "content": request_text, "sequence": 1}], "capabilities": [], "task_candidates": [],
               "references": {}, "known_material_clues": []}
    errors = validate_meaning(plan(answer="A complete explanation with the requested example."), payload)
    # Relation-level reroute ("relation_requires_work") or an unclassified imperative: both validator class.
    assert bool({"direct_action_word", "relation_requires_work"} & set(errors)) is word
    assert error_class("direct_action_word") == "validator"
    assert error_class("relation_requires_work") == "validator"
    assert error_class("unsafe_direct") == "plan_conflict"


@pytest.mark.parametrize(("request_type", "relation", "unsafe"), [
    ("modify", "new", False), ("execute", "new", False), ("other", "new", False),
    ("explain", "resume", True),
])
def test_unsafe_direct_ignores_request_type_but_not_resume(request_type, relation, unsafe):
    from nanobot.agent.context_selection import validate_meaning

    request_text = "방금 계획에서 전체 시간을 45분으로 줄여 주세요."
    payload = {"request": request_text, "sources": [
        {"event_id": "a1", "actor": "assistant", "content": "earlier plan", "sequence": 1},
        {"event_id": "cur", "actor": "user", "content": request_text, "sequence": 2}],
        "capabilities": [], "task_candidates": [{"task_id": "t1", "goal": "g", "status": "IN_PROGRESS"}],
        "references": {}, "known_material_clues": []}
    design = plan(request_type=request_type, task_relation=relation,
                  task_id="t1" if relation != "new" else None,
                  answer="A complete revised plan with every requested part.")
    assert ("unsafe_direct" in validate_meaning(design, payload)) is unsafe


def test_direct_with_needs_only_fails_when_required_info_is_unloaded(env):
    from nanobot.agent.context_selection import InformationNeed, validate_meaning

    loaded = event(env, "Q1. 텍스트의 장단점")
    payload = ContextSelector(env[0], ContextSelectionConfig()).minimal_input(
        "텍스트 장단점을 다시 설명해줘", env[1], "s", env[2], env[3])

    def errors(**kw):
        return validate_meaning(plan(information_needs=[InformationNeed(**need(**kw))]), payload)

    assert "unsafe_direct" not in errors(required=False)
    assert "unsafe_direct" not in errors(source_ids=[loaded.event_id], query="텍스트")
    assert "unsafe_direct" in errors(source_ids=[], query="텍스트")
