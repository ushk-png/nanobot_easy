"""Incomplete work survives restart; evidence acquisition reaches the real tool loop."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from nanobot.reasoning.bridge import ReasoningSessions, run_reasoning_procedure
from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import BudgetExceededError, ScriptedStageLLM
from nanobot.reasoning.registry import procedure
from tests.reasoning.test_stepwise_instrumentation import STAGE_OUTPUTS


class Provider:
    def __init__(self, route=None, timeout=False, timeout_stage="S5"):
        self.route = route or {"mode": "direct"}
        self.timeout = timeout
        self.timeout_stage = timeout_stage
        self.stages = []
        self.payloads = []

    async def chat_with_retry(self, **kwargs):
        payload = json.loads(kwargs["messages"][1]["content"])
        stage = payload.get("stage", "procedure")
        self.stages.append(stage)
        self.payloads.append(payload)
        if self.timeout and stage == self.timeout_stage:
            raise BudgetExceededError("time", "timeout")
        result = self.route if stage == "procedure" else STAGE_OUTPUTS[stage]
        return SimpleNamespace(content=json.dumps(result), usage={}, finish_reason="stop")


def run(provider, tmp_path, sessions, request, **kwargs):
    return asyncio.run(run_reasoning_procedure(
        provider, model="m", workspace=tmp_path, sessions=sessions,
        session_key="telegram:1", request=request, layout="fine", **kwargs,
    ))


@pytest.mark.parametrize("restart", [False, True])
def test_timeout_continue_preserves_finished_stages(tmp_path, restart):
    sessions = ReasoningSessions()
    original = "겹치는 일정 조정 방법 평가"
    stopped = run(Provider(timeout=True), tmp_path, sessions, original,
                  procedure_id="method-evaluation")
    assert stopped.status == "incomplete"
    assert stopped.step_states["S4"] == "passed"
    if restart:
        sessions = ReasoningSessions(ttl_s=-1)  # Incomplete work is not expired.
    provider = Provider()
    resumed = run(provider, tmp_path, sessions, "계속 작업해 줘")
    assert resumed.id == stopped.id
    assert resumed.request == original
    assert resumed.status == "completed"
    assert provider.stages == ["S5", "S6", "S7"]
    assert {"event": "resumed", "from": "S5"} in resumed.history


def test_defective_result_is_rerun_not_passed():
    outputs = dict(STAGE_OUTPUTS)
    outputs["S4"] = {"judgments": []}

    class Limited(ScriptedStageLLM):
        budget = SimpleNamespace(can_spend=lambda _: False)

    case = ReasoningEngine(Limited(outputs)).run("평가")
    assert case.status == "incomplete" and case.step_states["S4"] == "stopped"
    llm = ScriptedStageLLM(dict(STAGE_OUTPUTS))
    resumed = ReasoningEngine(llm).resume(case, procedure("method-evaluation"), "계속")
    assert resumed.status == "completed"
    assert {"event": "resumed", "from": "S4"} in resumed.history
    assert resumed.request == "평가"


def test_waiting_info_semantics_unchanged(tmp_path):
    outputs = dict(STAGE_OUTPUTS)
    outputs["S1"] = {"goal": "일정 충돌 조정", "constraints": [], "questions": ["언제?"]}
    case = ReasoningEngine(ScriptedStageLLM(outputs)).run("평가")
    assert case.status == "waiting_info"
    sessions = ReasoningSessions(workspace=tmp_path)
    sessions.put("telegram:1", case, procedure("method-evaluation"))
    provider = Provider({"mode": "resume"})
    resumed = run(provider, tmp_path, sessions, "내일")
    assert resumed.status == "completed"
    assert "[사용자 답변] 내일" in resumed.request
    assert provider.stages[0:2] == ["procedure", "S1"]


def test_topic_switch_not_hijacked(tmp_path):
    sessions = ReasoningSessions()
    stopped = run(Provider(timeout=True), tmp_path, sessions, "방법 평가",
                  procedure_id="method-evaluation")
    provider = Provider()
    direct = run(provider, tmp_path, sessions, "계속 말고 날씨 알려줘")
    assert direct.status == "direct"
    assert direct.request == "계속 말고 날씨 알려줘"
    assert provider.stages == ["procedure"]
    assert stopped.status == "incomplete"


@pytest.mark.parametrize("followup", [False, True])
def test_actual_stepwise_hands_investigation_to_normal_loop(tmp_path, followup, monkeypatch):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage

    original = "reason-43c3e1e40b6e 로그를 확인해 원인 분석해 줘"
    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = tmp_path
    loop.provider = Provider({"mode": "reuse", "procedure_id": "cause-analysis"})
    loop.model = "m"
    loop.stepwise_enabled = True
    seen = []

    async def agent_loop(messages, **kwargs):
        seen.extend(messages)
        return "조사 완료", ["read_file"], messages, "completed", False

    loop._run_agent_loop = agent_loop
    monkeypatch.setattr("nanobot.agent.loop.turn_continuation.maybe_continue_turn", status)
    loop._runtime_events = lambda: SimpleNamespace(run_status_changed=status)
    sessions = loop._stepwise_sessions()
    if followup:
        case = ReasoningEngine(ScriptedStageLLM(dict(STAGE_OUTPUTS))).run(original)
        case.status = "incomplete"
        sessions.put("telegram:1", case, procedure("method-evaluation"))
        loop._reasoning_sessions = ReasoningSessions(workspace=tmp_path)
    text = "계속 작업해 줘" if followup else original
    ctx = SimpleNamespace(
        msg=InboundMessage(channel="telegram", sender_id="u", chat_id="1", content=text),
        session_key="telegram:1", visible_run_started_at=None,
        initial_messages=[{"role": "user", "content": text}], on_progress=None,
        on_stream=None, on_stream_end=None, on_retry_wait=None, session=None,
        pending_queue=None, ephemeral=False, run_extra_hooks_for_ephemeral=False,
        hooks=None, tools=None,
    )
    assert asyncio.run(loop._state_run(ctx)) == "ok"
    assert loop.provider.stages == []  # No synthetic 'please provide logs' stage.
    assert original in seen[-1]["content"]
    assert ctx.tools_used == ["read_file"] and ctx.final_content == "조사 완료"


async def status(*args, **kwargs):
    pass


def test_conceptual_cause_question_still_reasoned(tmp_path):
    provider = Provider({"mode": "reuse", "procedure_id": "method-evaluation"})
    case = run(provider, tmp_path, ReasoningSessions(), "제시한 근거로 원인 분석해 줘")
    assert case.status == "completed"
    assert provider.stages[0] == "procedure"


def test_workspace_isolation_and_drop(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    sessions = ReasoningSessions(workspace=a)
    case = ReasoningEngine(ScriptedStageLLM(dict(STAGE_OUTPUTS))).run("평가")
    sessions.put("../../session", case, procedure("method-evaluation"))
    sessions.bind(b)
    assert sessions.get("../../session") is None
    sessions.bind(a)
    assert sessions.get("../../session")[0].id == case.id
    sessions.drop("../../session")
    assert ReasoningSessions(workspace=a).get("../../session") is None


@pytest.mark.parametrize("restart", [False, True])
def test_timeout_at_review_resumes_at_review(tmp_path, restart):
    sessions = ReasoningSessions()
    stopped = run(Provider(timeout=True, timeout_stage="S6"), tmp_path, sessions,
                  "방법 평가", procedure_id="method-evaluation")
    assert stopped.status == "incomplete"
    assert stopped.step_states["S5"] == "passed"
    if restart:
        sessions = ReasoningSessions()
    provider = Provider()
    resumed = run(provider, tmp_path, sessions, "계속 작업해 줘")
    assert resumed.status == "completed" and resumed.id == stopped.id
    assert provider.stages == ["S6", "S7"]


def test_semantic_tool_signal_overrides_cause_analysis_reuse(tmp_path):
    provider = Provider({
        "mode": "reuse", "procedure_id": "cause-analysis", "requires_tools": True,
    })
    case = run(provider, tmp_path, ReasoningSessions(), "이 오류의 원인을 분석해 줘")
    assert case.status == "direct"
    assert provider.stages == ["procedure"]


@pytest.mark.parametrize("user_text", [
    "나노봇이 멈추는 원인을 확인하고 해결방안을 찾아줘",
    "지니가 작업하다 멈췄어 직접 조사해 줘",
])
def test_persona_failure_investigation_routes_to_tools(tmp_path, user_text):
    provider = Provider({"mode": "reuse", "procedure_id": "cause-analysis"})
    case = run(provider, tmp_path, ReasoningSessions(), user_text)
    assert case.status == "direct"
    assert provider.stages == []
