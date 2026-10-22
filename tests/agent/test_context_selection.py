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

    async def execute(self, **kwargs):
        return "current config: version=2; deprecated method excluded"


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
    registry.register(ReadTool())
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
    event(env, "파일 수정해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        capabilities_needed=["change_test_file"], information_needs=[need(query="not-in-memory")]),
        request="파일 수정해줘")
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
        design = plan()
    result = LLMResponse(content="{bad json") if bad == "json" else response(design)
    _, provider, selected = await select(env, design, request="파일 구현해줘", responses=[result, result])
    assert selected.blocked == "design_validation_failed"
    assert len(provider.requests) == 2
    assert selected.trace.counters["design_recoveries"] == 2


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
    event(env, "현재 파일을 수정해줘")
    _, _, selected = await select(env, plan(route="execute", request_type="modify", answer=None,
        information_needs=[need(source_kind="file", query="current config")]), request="파일 수정해줘")
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
