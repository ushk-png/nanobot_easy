"""Turn-local follow-ups preserve state and never need a routing call."""
import asyncio
import json
import time
from types import SimpleNamespace

from nanobot.reasoning.agent_tool import ReasoningAssistTool
from nanobot.reasoning.bridge import ReasoningSessions
from nanobot.reasoning.engine import ReasoningEngine
from nanobot.reasoning.llm import ScriptedStageLLM
from nanobot.reasoning.models import Judgment
from nanobot.reasoning.registry import procedure
from tests.reasoning.test_resume_and_update import ScriptProvider
from tests.reasoning.test_stepwise_instrumentation import STAGE_OUTPUTS


def setup():
    plan = procedure("method-evaluation")
    case = ReasoningEngine(ScriptedStageLLM(dict(STAGE_OUTPUTS))).run("평가", procedure=plan)
    sessions = ReasoningSessions()
    sessions.put("k", case, plan)
    provider = ScriptProvider([])
    loop = SimpleNamespace(provider=provider, model="m", _current_iteration=1,
                           _stepwise_sessions=lambda: sessions)
    ctx = SimpleNamespace(session_key="k", msg=SimpleNamespace(content="사실 정정"))
    return ReasoningAssistTool(loop, ctx, time.monotonic()), sessions, provider, case, plan


def test_read_no_calls_or_mutation():
    tool, sessions, provider, case, _ = setup()
    before = case.model_dump_json()
    result = json.loads(asyncio.run(tool.execute("read", {})))
    assert result["revision"] == 1 and result["answer"] == case.answer
    assert provider.calls == []
    assert sessions.get("k")[0].model_dump_json() == before


def test_revision_rejects_before_calls():
    tool, _, provider, _, _ = setup()
    result = asyncio.run(tool.execute("update_facts", {}, expected_revision=7, facts=[{"id": "f1", "proposition": "変更"}]))
    assert result.is_error and provider.calls == []


def test_fact_followup_no_classifier_and_atomic_commit():
    tool, sessions, provider, original, _ = setup()
    result = json.loads(asyncio.run(tool.execute("update_facts", {}, expected_revision=1,
        facts=[{"id": "f1", "proposition": "新しい情報", "status": "confirmed"}])))
    assert result["revision"] == 2
    assert "compose" not in provider.calls and "S2" not in provider.calls
    assert original.facts[0].proposition != "新しい情報"
    assert sessions.get("k")[0].facts[0].proposition == "新しい情報"


def test_new_fact_invalidates_previously_unlinked_judgment():
    _, _, _, case, plan = setup()
    case.judgments = [Judgment(id="j", criterion_id="c1", result="met", evidence_ids=["f1"])]
    ReasoningEngine(ScriptedStageLLM({})).update_facts(case,
        [{"id": "new", "proposition": "예외 근거", "source": "user"}], plan)
    assert case.judgments == [] and case.kept_judgments == []
    assert case.facts[-1].status == "unknown"
    assert "S4" not in case.step_outputs


def test_identical_fact_does_not_invalidate():
    _, _, _, case, plan = setup()
    before = case.model_dump_json()
    ReasoningEngine(ScriptedStageLLM({})).update_facts(case, [case.facts[0].model_dump()], plan)
    assert case.model_dump_json() == before


def test_criterion_patch_preserves_facts_and_no_extraction():
    tool, sessions, provider, case, _ = setup()
    before = [f.model_dump() for f in case.facts]
    result = json.loads(asyncio.run(tool.execute("update_contract", {}, expected_revision=1,
        criteria=[{"id": case.criteria[0].id, "statement": "수정된 기준"}])))
    assert result["revision"] == 2
    assert not set(provider.calls) & {"compose", "S1", "S2", "S3"}
    assert [f.model_dump() for f in sessions.get("k")[0].facts] == before


def test_resume_saved_stage_without_routing():
    tool, sessions, provider, case, plan = setup()
    case.status = "incomplete"
    case.step_states["S7"] = "stopped"
    sessions.put("k", case, plan)
    tool.ctx.msg.content = "계속"
    result = json.loads(asyncio.run(tool.execute("resume", {}, expected_revision=1)))
    assert result["status"] == "completed"
    assert provider.calls == ["S7"]


def test_late_result_not_committed(monkeypatch):
    tool, sessions, provider, case, plan = setup()
    real = ReasoningEngine.continue_with_facts
    def racing(engine, detached, definition, facts):
        result = real(engine, detached, definition, facts)
        newer = case.model_copy(deep=True)
        newer.execution_revision += 1
        sessions.put("k", newer, plan)
        return result
    monkeypatch.setattr(ReasoningEngine, "continue_with_facts", racing)
    result = asyncio.run(tool.execute("update_facts", {}, expected_revision=1,
        facts=[{"id": "f1", "proposition": "정정", "status": "confirmed"}]))
    assert result.is_error and "late result discarded" in result
    assert sessions.get("k")[0].facts[0].proposition == case.facts[0].proposition


def test_goal_change_reuses_facts():
    tool, sessions, provider, case, _ = setup()
    before = [f.model_dump() for f in case.facts]
    # The mock must link regenerated criteria to the changed goal; returning the
    # original goal correctly triggers the engine's invalid-criterion repair.
    provider.stages["S3"] = [{"criteria": [{**case.criteria[0].model_dump(),
                                           "linked_goal": "동일 작업의 변경된 목표"}]}]
    result = json.loads(asyncio.run(tool.execute("update_contract", {}, expected_revision=1,
                                                goal="동일 작업의 변경된 목표")))
    assert result["revision"] == 2 and result["status"] == "completed"
    assert not set(provider.calls) & {"compose", "S1", "S2"}
    assert sessions.get("k")[0].goal == "동일 작업의 변경된 목표"
    assert [f.model_dump() for f in sessions.get("k")[0].facts] == before


def test_new_topic_cannot_resume_incomplete_case():
    tool, sessions, provider, case, plan = setup()
    case.status = "incomplete"
    sessions.put("k", case, plan)
    tool.ctx.msg.content = "오늘 날씨 알려 줘"
    assert asyncio.run(tool.execute("resume", {}, expected_revision=1)).is_error
    assert provider.calls == []
