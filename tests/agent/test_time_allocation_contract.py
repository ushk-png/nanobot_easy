"""Contract acceptance: raw synthetic proposals; independent expected user output and DB values.

Runtime IDs are bound to fixture labels. Expected quantities/decisions never come from
product computation, parsers, renderer, reports or verified flags.
"""
import copy
import json
import os
from pathlib import Path

import pytest

from nanobot.providers.base import LLMResponse, ToolCallRequest

from .t51_harness import Session

START = "총 45분 연습 계획을 짜 주세요. 퍼팅 20분, 어프로치 15분, 아이언 15분으로 고정해 주세요. 인터넷 검색은 하지 마세요."
VALUES = {"퍼팅": "20", "어프로치": "15", "아이언": "15"}
GOLDEN = ("퍼팅: 20분\n어프로치: 15분\n아이언: 15분\n"
          "총 시간: 50분 · 배분 합계: 50분 · 잔여: 0분\n합계는 20+15+15=50분입니다.")


def raw(data):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(
        id="design", name="submit_context_design", arguments=data)], usage={})


def session(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    s.loop.context_selection_config.time_allocation_contract_enabled = True
    original = s.turn
    async def observed(text, *args, **kwargs):
        output = await original(text, *args, **kwargs)
        if path := os.environ.get("NANOBOT_ALLOCATION_EVIDENCE"):
            with Path(path).open("a") as stream:
                stream.write(json.dumps({"test": os.environ.get("PYTEST_CURRENT_TEST"),
                    "provenance": "saved_actual_response_replay" if "saved_actual_response" in os.environ.get("PYTEST_CURRENT_TEST", "") else "synthetic",
                    "request": text, "user_output": output, "tasks": s.tasks(),
                    "report": s.selected[-1].calculation_report(),
                    "mock_calls": len(s.mock.requests), "actual_model_transmissions": 0}, ensure_ascii=False, default=str) + "\n")
        return output
    s.turn = observed
    return s


def proposal(values=None, total="50", remaining="0", task=None, route="direct", notes=None, answer="unused model prose", response_id=None):
    def step(messages):
        p = json.loads(messages[1]["content"])
        data = {"goal": "시간 배분", "task_relation": "new" if not task else "change", "task_id": task,
                "route": route, "constraints": [], "information_needs": [], "capabilities_needed": [],
                "unresolved": [], "answer": answer if route == "direct" else None}
        snapshot = (next(r["effective_requirements"] for r in p["allocation_task_requirements"] if r["task_id"] == task)
                    if task else p["allocation_new_requirements"])
        ids = {r["expected_values"]["label"]: r["id"] for r in snapshot["requirements"] if r["kind"] == "item"}
        if values is not None:
            data["allocation_result"] = {"items": [{"item_id": ids[k], "value": v} for k, v in values.items()],
                                         "total": total, "remaining": remaining}
        if task:
            data["reference"] = {"relation": "modify_answer", "task_id": task,
                "response_id": response_id or p["prior_context"]["previous_answer"]["ref_id"], "basis_span": p["request"]}
        if notes:
            data["allocation_explanations"] = [{"item_id": ids[k], "purpose": "accuracy", "method": "focus_on_form"} for k in notes]
        return raw(data)
    return step


async def turn(s, text, response):
    s.mock.designs.append(response)
    return await s.turn(text)


def stored_values(task, result=False):
    if result:
        bundle = task["work_records"][-1]["allocation"]
        labels = {r["id"]: r["expected_values"]["label"] for r in task["effective_requirements"]["requirements"] if r["kind"] == "item"}
        return {labels[i["item_id"]]: i["value"] for i in bundle["result"]["items"]}
    return {r["expected_values"]["label"]: (r["expected_values"]["mode"], r["expected_values"]["value"])
            for r in task["effective_requirements"]["requirements"] if r["kind"] == "item"}


async def begin(s):
    question = await turn(s, START, proposal())
    task = s.tasks()[0]
    assert task["status"] != "DONE" and "50분" in question and "45분" in question
    return task["task_id"]


async def test_a01_correct_final_output_and_a02_missing_carry_wrong_fixed(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    tid = await begin(s)
    output = await turn(s, "총 50분으로 할게요. 항목 시간은 유지해 주세요.", proposal(VALUES, task=tid))
    assert output == GOLDEN
    task = s.tasks()[0]
    assert task["status"] == "DONE"
    assert stored_values(task, True) == VALUES
    assert task["instruction_version"] == 2
    bundle = task["work_records"][-1]["allocation"]
    assert bundle["output"] == output and bundle["instruction_version"] == 2
    assert s.loop.conversation_memory.get_events(s.selected[-1].scope, [bundle["response_id"]])[0].content == output
    wrong = {"퍼팅": "25", "어프로치": "10", "아이언": "15"}
    output = await turn(s, "같은 조건으로 계속해 주세요.", proposal(wrong, task=tid))
    assert "result_error" in output and "25분" not in output, (s.selected[-1].trace.reasons, s.mock.requests[-1]["messages"][-1])
    assert s.tasks()[0]["status"] != "DONE"
    assert stored_values(s.tasks()[0]) == {k: ("fixed", v) for k, v in VALUES.items()}


async def ready(s):
    tid = await begin(s)
    assert await turn(s, "총 50분으로 할게요. 항목 시간은 유지해 주세요.", proposal(VALUES, task=tid)) == GOLDEN
    return tid


async def test_a03_omitted_checks_still_complete_and_no_legacy_numeric_calls(tmp_path, monkeypatch):
    from nanobot.agent import context_selection as cs
    s = session(tmp_path, monkeypatch)
    tid = await ready(s)
    def forbidden(*a, **k):
        raise AssertionError("replaced numeric parser executed")
    for name in ("direct_relation_errors", "computed_mismatch", "relation_answer_finding", "merge_current_groups"):
        monkeypatch.setattr(cs, name, forbidden)
    assert await turn(s, "같은 조건을 유지해 주세요.", proposal(VALUES, task=tid)) == GOLDEN
    assert s.tasks()[0]["status"] == "DONE"


async def test_a04_explicit_change_preserves_other_fixed_and_continuous_sequence(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    tid = await ready(s)
    values = {"퍼팅": "25", "어프로치": "10", "아이언": "15"}
    out = await turn(s, "퍼팅 25분, 어프로치 10분으로 바꾸고 아이언은 유지해 주세요.", proposal(values, task=tid))
    assert "퍼팅: 25분" in out and "어프로치: 10분" in out, s.selected[-1].relations().public()
    assert s.tasks()[0]["status"] == "DONE" and stored_values(s.tasks()[0], True) == values
    await turn(s, "총 45분으로 변경해 주세요.", proposal(task=tid))
    assert s.tasks()[0]["status"] != "DONE"
    assert s.tasks()[0]["quantity_questions"][-1]["instruction_version"] == 4
    assert await turn(s, "1번", proposal(values, task=tid)) != ""
    assert s.tasks()[0]["status"] == "DONE" and s.tasks()[0]["instruction_version"] == 5
    assert stored_values(s.tasks()[0], True) == values


async def test_a05_total_change_keeps_fixed_exclusion_equal_and_a16_remainder(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    req = "총 60분 연습 계획. 퍼팅 20분 고정. 드라이버 제외. 남은 시간을 어프로치와 아이언에 균등 배분해 주세요."
    out = await turn(s, req, proposal())
    assert "어프로치: 20분" in out and s.tasks()[0]["status"] == "DONE"
    tid = s.tasks()[0]["task_id"]
    out = await turn(s, "총 45분으로 변경하고 항목 조건은 유지해 주세요.", proposal(task=tid))
    assert "퍼팅: 20분" in out and "어프로치: 12.5분" in out and "아이언: 12.5분" in out
    assert "제외 항목: 드라이버" in out and s.tasks()[0]["status"] == "DONE"
    await turn(s, "총 60분으로 변경해 주세요.", proposal({"퍼팅": "20", "어프로치": "15", "아이언": "15"}, total="60", remaining="10", task=tid))
    assert s.tasks()[0]["status"] != "DONE"
    out = await turn(s, "같은 조건으로 해 주세요.", proposal({"퍼팅": "20", "어프로치": "20", "아이언": "20"}, total="60", task=tid))
    assert "잔여: 0분" in out and s.tasks()[0]["status"] == "DONE"


async def test_design_delegates_determined_numbers_without_losing_user_requirements(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    request = "총 60분 연습 계획. 퍼팅 20분 고정. 드라이버 제외. 남은 시간을 어프로치와 아이언에 균등 배분해 주세요. 인터넷 검색은 하지 마세요."
    output = await turn(s, request, proposal(answer=None))
    assert output.startswith("퍼팅: 20분\n어프로치: 20분\n아이언: 20분\n총 시간: 60분")
    task = s.tasks()[0]
    assert task["status"] == "DONE"
    assert stored_values(task, True) == {"퍼팅": "20", "어프로치": "20", "아이언": "20"}
    assert stored_values(task)["드라이버"] == ("excluded", None)
    prompt = s.mock.requests[0]["messages"][0]["content"]
    assert "OMIT allocation_result" in prompt and "answer=null" in prompt
    assert "genuine nonnumeric missing information" in prompt
    assert "lookup prohibitions" in prompt
    assert not s.executor_requests() and len(s.mock.requests) == 1
    assert task["work_records"][-1]["allocation"]["submitted_result"] is None


async def test_a06_explicit_release_only_named_fixed(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    tid = await ready(s)
    values = {"퍼팅": "25", "어프로치": "10", "아이언": "15"}
    out = await turn(s, "퍼팅과 어프로치 고정만 풀어 주세요.", proposal(values, task=tid))
    assert s.tasks()[0]["status"] == "DONE", out
    assert stored_values(s.tasks()[0]) == {"퍼팅": ("adjustable", None), "어프로치": ("adjustable", None), "아이언": ("fixed", "15")}
    assert sum(x.get("operation") == "release" for x in s.tasks()[0]["supersessions"]) == 2


async def test_a08_old_question_choice_cannot_change_new_requirements(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    tid = await ready(s)
    values = copy.deepcopy(stored_values(s.tasks()[0]))
    await turn(s, "1번", proposal(VALUES, task=tid))
    assert s.tasks()[0]["status"] != "DONE"
    assert stored_values(s.tasks()[0]) == values
    assert s.tasks()[0]["effective_requirements"]["unresolved"][0]["code"] == "question_binding_stale"


@pytest.mark.parametrize("prose", ["20+15+15=50분", "총50분: 퍼팅20분 어프로치15분 아이언15분 (20+15+15=50분)",
                                     "퍼팅20분 어프로치15분 아이언15분. 최종 퍼팅25분 어프로치10분 아이언15분."])
async def test_a10_a13_numeric_prose_never_changes_checked_output(tmp_path, monkeypatch, prose):
    s = session(tmp_path, monkeypatch)
    tid = await begin(s)
    out = await turn(s, "총 50분으로 하고 항목 시간은 유지해 주세요.", proposal(VALUES, task=tid, answer=prose))
    assert out == GOLDEN and s.tasks()[0]["status"] == "DONE"


async def test_a11_bound_missing_total_and_partial_model_relation(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    out = await turn(s, "총 60분 이내 연습 계획. 퍼팅20분, 어프로치15분, 아이언15분.",
                     proposal(VALUES, total="60", remaining="10"))
    assert s.tasks()[0]["status"] == "DONE", out
    assert "배분 합계: 50분 · 잔여: 10분" in out
    s2 = session(tmp_path / "missing", monkeypatch)
    out = await turn(s2, "퍼팅20분, 어프로치15분, 아이언15분 연습 계획.", proposal())
    assert s2.tasks()[0]["status"] != "DONE" and "user_info_missing" in out
    s3 = session(tmp_path / "partial", monkeypatch)
    base = proposal(VALUES)
    def partial(messages):
        r = base(messages)
        r.tool_calls[0].arguments["quantity_relations"] = [{"kind": "composition", "kind_span": "총 50분"}]
        return r
    out = await turn(s3, "총 50분 연습 계획. 퍼팅20분, 어프로치15분, 아이언15분.", partial)
    assert out == GOLDEN and s3.tasks()[0]["status"] == "DONE"


async def test_a12_required_purpose_method_and_excluded_item(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    req = "총 50분 연습 계획. 퍼팅20분, 어프로치15분, 아이언15분. 드라이버 제외. 각 항목 목적과 방법도 설명해 주세요."
    out = await turn(s, req, proposal(VALUES))
    assert s.tasks()[0]["status"] != "DONE" and "result_error" in out
    tid = s.tasks()[0]["task_id"]
    out = await turn(s, "같은 조건을 유지해 주세요.", proposal(VALUES, task=tid, notes=list(VALUES)))
    assert s.tasks()[0]["status"] == "DONE", out
    assert out.count("목적:") == 3 and out.count("방법:") == 3 and "제외 항목: 드라이버" in out


async def test_a13_exact_units(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    out = await turn(s, "총 50분 연습 계획. 퍼팅1200초, 어프로치900초, 아이언900초.", proposal(VALUES))
    assert out == GOLDEN and s.tasks()[0]["status"] == "DONE"
    s = session(tmp_path / "mixed", monkeypatch)
    values = {"퍼팅": "20", "어프로치": "12.5", "아이언": "12.5"}
    out = await turn(s, "총45분 연습 계획. 퍼팅20분, 어프로치12분30초, 아이언12.5분.", proposal(values, total="45"))
    assert s.tasks()[0]["status"] == "DONE" and stored_values(s.tasks()[0], True) == values, out


@pytest.mark.parametrize("user_text,code", [
    ("총50분 연습 계획. 그중 퍼팅20분과 아이언30분을 동시에.", "interpretation_unsupported"),
    ("총50분 연습 계획.", "user_info_missing"),
])
async def test_a15_unsupported_never_flattened_or_done(tmp_path, monkeypatch, user_text, code):
    s = session(tmp_path, monkeypatch)
    out = await turn(s, user_text, proposal())
    assert s.tasks()[0]["status"] != "DONE" and code in out


async def test_a07_other_task_never_enters_requirements(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    tid = await ready(s)
    a = next(t for t in s.tasks() if t["task_id"] == tid)
    rid = a["work_records"][-1]["allocation"]["response_id"]
    out = await turn(s, "총90분 연습 계획. 퍼팅30분, 어프로치30분, 아이언30분.",
                     proposal({"퍼팅": "30", "어프로치": "30", "아이언": "30"}, total="90"))
    assert "총 시간: 90분" in out
    b = next(t for t in s.tasks() if t["task_id"] != tid)
    out = await turn(s, f"응답 {rid}의 항목 조건을 유지해 주세요.", proposal(VALUES, task=tid, response_id=rid))
    assert out == GOLDEN
    a = next(t for t in s.tasks() if t["task_id"] == tid)
    assert a["status"] == "DONE" and stored_values(a, True) == VALUES
    assert next(t for t in s.tasks() if t["task_id"] == b["task_id"]) == b


async def test_a09_design_failure_invalidates_old_done(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    await ready(s)
    out = await turn(s, "총 45분으로 변경하고 항목은 유지해 주세요.", LLMResponse(content=None, finish_reason="error"))
    task = s.tasks()[0]
    assert "완료하지 못" in out and task["status"] != "DONE"
    assert task["instruction_version"] == 3 and task["instruction_status"] == "RECONCILE"
    assert task["work_records"][-2]["allocation"]["instruction_version"] == 2


@pytest.mark.parametrize("fault", ["cas", "raw_event", "new_event"])
async def test_a09_commit_failure_never_publishes_success(tmp_path, monkeypatch, fault):
    from nanobot.memory.task_context import TaskContextStore
    s = session(tmp_path, monkeypatch)
    tid = await begin(s)
    save = TaskContextStore.save
    def interrupted(tasks, scope, state, **kw):
        if kw.get("events"):
            if fault == "cas":
                raise ValueError("task_version_conflict")
            if fault == "raw_event":
                import sqlite3
                raise sqlite3.OperationalError("interrupted persistence")
            tasks.store.append_event(scope=scope, conversation_id=state["session_id"], session_id=state["session_id"],
                                     actor="user", event_type="USER_MESSAGE", content="총45분으로 다시 변경해 주세요.")
        return save(tasks, scope, state, **kw)
    monkeypatch.setattr(TaskContextStore, "save", interrupted)
    out = await turn(s, "총50분으로 하고 항목 유지해 주세요.", proposal(VALUES, task=tid))
    assert "allocation_save_failed" in out and "퍼팅: 20분" not in out
    task = s.tasks()[0]
    assert task["status"] != "DONE"
    bundle = task["work_records"][-1]["allocation"]
    assert bundle["status"] == "prepared"
    assert not s.loop.conversation_memory.get_events(s.selected[-1].scope, [bundle["response_id"]])


async def test_a09_inflight_event_held_without_other_task_condition_mix(tmp_path, monkeypatch):
    from nanobot.agent.time_allocation import AllocationController
    s = session(tmp_path, monkeypatch)
    tid = await begin(s)
    original = AllocationController.finalize
    injected = []
    def race(controller, content, stop_reason="completed"):
        if not injected:
            injected.append(True)
            c = controller.selected
            controller.tasks.store.append_event(scope=c.scope, conversation_id=c.state["session_id"],
                session_id=c.state["session_id"], actor="user", event_type="USER_MESSAGE",
                content="다른 작업: 총90분 연습 계획, 퍼팅30분.")
        return original(controller, content, stop_reason)
    monkeypatch.setattr(AllocationController, "finalize", race)
    out = await turn(s, "총50분으로 하고 항목은 유지해 주세요.", proposal(VALUES, task=tid))
    assert "완료하지 못" in out and s.tasks()[0]["status"] != "DONE"
    assert stored_values(s.tasks()[0]) == {k: ("fixed", v) for k, v in VALUES.items()}
    assert not any("다른 작업" in e["text"] for e in s.tasks()[0]["user_constraints"])


async def test_a14_plain_answer_keeps_existing_path(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    from .t51_harness import design
    out = await s.turn("안녕하세요. 인사해 주세요.", design(answer="안녕하세요!"))
    assert out == "안녕하세요!" and s.selected[-1].allocation is None
    assert "effective_requirements" not in s.tasks()[0]


@pytest.mark.parametrize("route", ["direct", "execute", "budget"])
async def test_routes_share_one_finalization_and_limit_never_done(tmp_path, monkeypatch, route):
    from .t51_harness import final
    s = session(tmp_path, monkeypatch)
    tid = await begin(s)
    if route == "budget":
        s.loop.max_iterations = 0
    s.mock.designs.append(proposal(VALUES, task=tid, route="direct" if route == "direct" else "execute"))
    out = await s.turn("총50분으로 하고 항목 시간은 유지해 주세요.", steps=[final("unchecked prose")] if route == "execute" else [])
    if route == "budget":
        assert s.tasks()[0]["status"] != "DONE" and out != GOLDEN
    else:
        assert out == GOLDEN and s.tasks()[0]["status"] == "DONE"


async def test_a17_channel_failure_retries_same_saved_output_and_stale_suppression(tmp_path, monkeypatch):
    from nanobot.bus.events import OutboundMessage
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.schema import Config
    s = session(tmp_path, monkeypatch)
    tid = await ready(s)
    task = s.tasks()[0]
    bundle = task["work_records"][-1]["allocation"]
    assert bundle["status"] == "delivery_unknown"
    config = Config()
    config.agents.defaults.workspace = str(tmp_path)
    config.channels.send_max_retries = 1
    manager = ChannelManager(config, MessageBus())
    from dataclasses import asdict
    marker = {"scope": asdict(s.selected[-1].scope), "session_id": task["session_id"], "task_id": tid,
              "result_id": bundle["result_id"], "instruction_version": 2}
    msg = OutboundMessage(channel="cli", chat_id="s", content=bundle["output"], metadata={"_allocation_delivery": marker})
    sent = []
    class Channel:
        async def send(self, message):
            sent.append(message.content)
            raise OSError("synthetic transport failure")
    await manager._send_with_retry(Channel(), msg)
    assert sent == [GOLDEN] and s.tasks()[0]["status"] == "DONE"
    assert s.tasks()[0]["work_records"][-1]["allocation"]["result_id"] == bundle["result_id"]
    class Success:
        async def send(self, message):
            sent.append(message.content)
    await manager._send_with_retry(Success(), msg)
    assert sent == [GOLDEN, GOLDEN]
    assert s.tasks()[0]["work_records"][-1]["allocation"]["status"] == "delivered"
    await turn(s, "총45분으로 변경해 주세요.", proposal(task=tid))
    await manager._send_with_retry(Success(), msg)
    assert len(sent) == 2 and s.tasks()[0]["status"] != "DONE"


async def test_saved_actual_response_replay_keeps_meaning_and_correct_equation(tmp_path, monkeypatch):
    from pathlib import Path

    from .test_followup_record_replay import replay_step
    root = Path(__file__).parent / "fixtures" / "time_allocation"
    records = [json.loads((root / f"live_t{i}.json").read_text()) for i in (1, 2)]
    s = session(tmp_path, monkeypatch)
    for i, rec in enumerate(records):
        response = replay_step(rec, records[0] if i else None)
        # Preserve the saved separate free text too; the official arguments remain authoritative.
        def saved(messages, response=response, rec=rec):
            result = response(messages)
            result.content = rec["raw_designs"][0]["raw"]
            return result
        out = await turn(s, rec["request"], saved)
    assert "20+15+15=50분" in records[1]["design_outputs"][0]["function_arguments"]["answer"]
    assert out == GOLDEN and s.tasks()[0]["status"] == "DONE"
    assert stored_values(s.tasks()[0], True) == VALUES and len(s.mock.requests) == 2


async def test_executor_structured_correction_uses_same_schema(tmp_path, monkeypatch):
    from .t51_harness import calls, final
    s = session(tmp_path, monkeypatch)
    tid = await ready(s)
    task = s.tasks()[0]
    ids = {r["expected_values"]["label"]: r["id"] for r in task["effective_requirements"]["requirements"] if r["kind"] == "item"}
    wrong = {"퍼팅": "25", "어프로치": "10", "아이언": "15"}
    s.mock.designs.append(proposal(wrong, task=tid, route="execute"))
    result = {"items": [{"item_id": ids[k], "value": v} for k, v in VALUES.items()], "total": "50", "remaining": "0"}
    out = await s.turn("같은 조건으로 해 주세요.", steps=[calls(("allocation", "submit_time_allocation", {"result": result})), final("unchecked prose")])
    assert out == GOLDEN and s.tasks()[0]["status"] == "DONE"


async def test_rollback_flag_does_not_reuse_contract_done(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    tid = await ready(s)
    s.loop.context_selection_config.time_allocation_contract_enabled = False
    # The old schema still accepts the fields, but the saved contract's DONE cannot be reused.
    def old_payload(messages):
        p = json.loads(messages[1]["content"])
        # Saved raw design with no new-schema fields, as an old deployment would submit.
        return raw({"goal": "계획 변경", "task_relation": "change", "task_id": tid,
                    "route": "direct", "constraints": [], "information_needs": [], "capabilities_needed": [],
                    "unresolved": [], "answer": "새 계획입니다.", "reference": {
                        "relation": "modify_answer", "task_id": tid, "response_id": p["prior_context"]["previous_answer"]["ref_id"],
                        "basis_span": p["request"]}})
    out = await turn(s, "총45분으로 해 주세요.", old_payload)
    assert "완료하지 못" in out
    assert s.selected[-1].blocked == "allocation_contract_disabled"


async def test_idempotent_committed_request_keeps_one_bundle_and_event(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    await ready(s)
    selected = s.selected[-1]
    before = copy.deepcopy(s.tasks()[0])
    out, status, rid = selected.allocation.persist(GOLDEN, "completed")
    after = s.tasks()[0]
    assert out == GOLDEN and status == "completed" and before == after
    assert rid == before["work_records"][-1]["allocation"]["response_id"]
    events = selected.control.tasks.events(selected.scope, before["session_id"])
    assert len([e for e in events if e.event_id == rid]) == 1


async def test_minimum_is_required_even_when_model_omits_it(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    out = await turn(s, "총50분 연습 계획. 퍼팅20분, 어프로치15분, 아이언15분. 각 항목 최소20분.", proposal(VALUES))
    assert s.tasks()[0]["status"] != "DONE" and "충돌" in out
    assert any(r["kind"] == "relation" and r["expected_values"]["kind"] == "min_each" for r in s.tasks()[0]["effective_requirements"]["requirements"])


@pytest.mark.parametrize("duration", ["0/0", "-20"])
async def test_invalid_duration_is_unresolved_not_crash_or_done(tmp_path, monkeypatch, duration):
    s = session(tmp_path, monkeypatch)
    out = await turn(s, f"총50분 연습 계획. 퍼팅{duration}분, 아이언30분.", proposal())
    assert s.tasks()[0]["status"] != "DONE" and "interpretation_unsupported" in out


async def test_prepared_commit_resume_keeps_same_result_and_output(tmp_path, monkeypatch):
    from nanobot.memory.task_context import TaskContextStore
    s = session(tmp_path, monkeypatch)
    tid = await begin(s)
    save = TaskContextStore.save
    failed = []
    def fail_once(tasks, scope, state, **kwargs):
        if kwargs.get("events") and not failed:
            failed.append(True)
            raise ValueError("crash before commit")
        return save(tasks, scope, state, **kwargs)
    monkeypatch.setattr(TaskContextStore, "save", fail_once)
    await turn(s, "총50분으로 하고 항목은 유지해 주세요.", proposal(VALUES, task=tid))
    previous = s.tasks()[0]["work_records"][-1]["allocation"]
    controller = s.selected[-1].allocation
    out, status, rid = controller.persist(GOLDEN, "completed")
    task = s.tasks()[0]
    assert out == GOLDEN and status == "completed" and task["status"] == "DONE"
    assert task["work_records"][-1]["allocation"]["result_id"] == previous["result_id"]
    assert rid == previous["response_id"]
    assert sum(c.get("result_id") == previous["result_id"] for c in task["completed"]) == 1


async def test_a15_missing_original_is_not_verified_from_model_carry(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    tid = await ready(s)
    task = s.tasks()[0]
    origin = task["origin_event_id"]
    with s.loop.conversation_memory.connect() as conn:
        conn.execute("UPDATE events SET redacted_at='test-redaction' WHERE event_id=?", (origin,))
    out = await turn(s, "같은 조건으로 해 주세요.", proposal(VALUES, task=tid))
    assert s.tasks()[0]["status"] != "DONE" and "source_unavailable" in out


async def test_additional_numeric_requirement_not_dropped_by_template_notes(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    out = await turn(s, "총50분 연습 계획. 퍼팅20분, 어프로치15분, 아이언15분. 각 항목 목적과 방법, 반복 3회도 설명해 주세요.", proposal(VALUES, notes=list(VALUES)))
    assert s.tasks()[0]["status"] != "DONE" and "interpretation_unsupported" in out


async def test_exact_fraction_does_not_round_fixed_values(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    out = await turn(s, "총20/3분 연습 계획. 걷기10/3분, 뛰기10/3분.", proposal({"걷기": "10/3", "뛰기": "10/3"}, total="20/3"))
    assert out == "걷기: 10/3분\n뛰기: 10/3분\n총 시간: 20/3분 · 배분 합계: 20/3분 · 잔여: 0분\n합계는 10/3+10/3=20/3분입니다."
    assert s.tasks()[0]["status"] == "DONE"


async def test_duplicate_label_is_ambiguous_not_silently_merged(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    out = await turn(s, "총10분 연습 계획. 운동10분, 운동10분.", proposal())
    assert s.tasks()[0]["status"] != "DONE" and "ambiguous_item_identity" in out


async def test_atomic_raw_event_index_failure_rolls_back_event_and_done(tmp_path, monkeypatch):
    import sqlite3

    from nanobot.memory.event_store import ConversationEventStore
    s = session(tmp_path, monkeypatch)
    tid = await begin(s)
    index = ConversationEventStore._index_event_chunks_conn
    def interrupted(store, conn, event):
        if event.content == GOLDEN:
            raise sqlite3.OperationalError("synthetic crash inside shared transaction")
        return index(store, conn, event)
    monkeypatch.setattr(ConversationEventStore, "_index_event_chunks_conn", interrupted)
    out = await turn(s, "총50분으로 하고 항목 유지해 주세요.", proposal(VALUES, task=tid))
    assert "allocation_save_failed" in out and s.tasks()[0]["status"] != "DONE"
    bundle = s.tasks()[0]["work_records"][-1]["allocation"]
    assert not s.loop.conversation_memory.get_events(s.selected[-1].scope, [bundle["response_id"]])
    assert bundle["status"] == "prepared"


async def test_quoted_obsolete_values_do_not_become_current_requirements(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    out = await turn(s, '총50분 연습 계획. 퍼팅20분, 어프로치15분, 아이언15분. 과거 메모 "퍼팅25분, 어프로치10분".', proposal(VALUES))
    assert out == GOLDEN and s.tasks()[0]["status"] == "DONE"
    assert stored_values(s.tasks()[0], True) == VALUES


async def test_a14_new_general_topic_preserves_old_allocation_task(tmp_path, monkeypatch):
    from .t51_harness import design
    s = session(tmp_path, monkeypatch)
    tid = await ready(s)
    before = s.tasks()[0]
    out = await s.turn("새 주제로 인사해 주세요.", design(answer="안녕하세요!"))
    assert out == "안녕하세요!" and s.selected[-1].allocation is None
    assert next(t for t in s.tasks() if t["task_id"] == tid) == before


@pytest.mark.parametrize("structured", [False, True])
async def test_saved_pair_synthetic_omitted_carry_control(tmp_path, monkeypatch, structured):
    from .test_followup_record_replay import replay_step
    root = Path(__file__).parent / "fixtures" / "time_allocation"
    first, second = [json.loads((root / f"live_t{i}.json").read_text()) for i in (1, 2)]
    second = copy.deepcopy(second)
    args = second["design_outputs"][0]["function_arguments"]
    args.update(constraints=[], completion_checks=[], completion_specs=[], quantity_relations=[],
                answer="총 50분: 퍼팅25분, 어프로치10분, 아이언15분.")
    args["reference"]["kept_conditions"] = []
    args["reference"]["changed_conditions"] = []
    args["obligations"] = {"target": "prior_answer", "lookup": "unneeded", "external_change": "none", "source_span": second["request"]}
    s = session(tmp_path, monkeypatch)
    await turn(s, first["request"], replay_step(first))
    response = replay_step(second, first)
    def adapted(messages):
        result = response(messages)
        if structured:
            payload = json.loads(messages[1]["content"])
            snapshot = payload["allocation_task_requirements"][0]["effective_requirements"]
            ids = {r["expected_values"]["label"]: r["id"] for r in snapshot["requirements"] if r["kind"] == "item"}
            result.tool_calls[0].arguments["allocation_result"] = {
                "items": [{"item_id": ids[k], "value": v} for k, v in {"퍼팅": "25", "어프로치": "10", "아이언": "15"}.items()],
                "total": "50", "remaining": "0"}
        return result
    out = await turn(s, second["request"], adapted)
    if structured:
        assert "result_error" in out and s.tasks()[0]["status"] != "DONE"
    else:
        assert out == GOLDEN and s.tasks()[0]["status"] == "DONE"
    assert "퍼팅25분" not in out
    assert stored_values(s.tasks()[0]) == {k: ("fixed", v) for k, v in VALUES.items()}


async def test_a08_explicit_old_question_cannot_select_new_question_option(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    tid = await ready(s)
    old_response = s.tasks()[0]["quantity_questions"][0]["response_id"]
    await turn(s, "총45분으로 변경해 주세요.", proposal(task=tid))
    assert s.tasks()[0]["instruction_version"] == 3
    out = await turn(s, "1번", proposal(VALUES, task=tid, response_id=old_response))
    task = s.tasks()[0]
    assert task["status"] != "DONE" and "question_binding_stale" in out
    total = next(r["expected_values"]["value"] for r in task["effective_requirements"]["requirements"] if r["kind"] == "total")
    assert total == "45"
    assert not task["quantity_questions"][-1].get("resolved_event_id")
    out = await turn(s, "총50분으로 변경하고 항목은 유지해 주세요.", proposal(VALUES, task=tid))
    assert out == GOLDEN and s.tasks()[0]["status"] == "DONE"


@pytest.mark.parametrize("bound", ["45분 이내", "최대45분"])
async def test_a11_bound_marker_position_and_unallocated_remainder(tmp_path, monkeypatch, bound):
    s = session(tmp_path, monkeypatch)
    values = {"퍼팅": "15", "어프로치": "10", "아이언": "10"}
    out = await turn(s, f"{bound}로 연습 계획. 퍼팅15분, 어프로치10분, 아이언10분이고 남는 시간은 여유로 두세요.",
                     proposal(values, total="45", remaining="10"))
    assert s.tasks()[0]["status"] == "DONE" and stored_values(s.tasks()[0], True) == values, out
    assert "배분 합계: 35분 · 잔여: 10분" in out


async def test_saved_actual_response_replay_t65_upper_bound(tmp_path, monkeypatch):
    from .test_context_t67_local import replay_u3
    s = session(tmp_path, monkeypatch)
    user_text, response = replay_u3()
    out = await turn(s, user_text, response)
    assert s.tasks()[0]["status"] == "DONE", out
    assert stored_values(s.tasks()[0], True) == {"퍼팅": "15", "어프로치": "10", "아이언": "10"}
    assert "총 시간: 45분 · 배분 합계: 35분 · 잔여: 10분" in out


async def submitted_once(s, *, values=None, omit=None, duplicate=False, steps=None):
    """Independent quantities; copy only opaque item IDs from the real design input."""
    from .t51_harness import calls
    pending = {}
    source = proposal(route="execute")
    def design(messages):
        payload = json.loads(messages[1]["content"])
        ids = {r["expected_values"]["label"]: r["id"] for r in
               payload["allocation_new_requirements"]["requirements"] if r["kind"] == "item"}
        expected = VALUES if values is None else values
        pending["result"] = {"items": [{"item_id": ids[k], "value": v}
                                       for k, v in expected.items() if k != omit],
                             "total": "50", "remaining": "0"}
        return source(messages)
    def execute(messages):
        call = ("submit-1", "submit_time_allocation", {"result": pending["result"]})
        return calls(call, ("submit-2", call[1], call[2])) if duplicate else calls(call)
    s.loop.max_iterations = 1
    s.mock.designs.append(design)
    return await s.turn("총50분 연습 계획. 퍼팅20분, 어프로치15분, 아이언15분 고정.",
                        steps=steps or [execute])


async def test_valid_submission_completes_without_final_model_iteration(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    output = await submitted_once(s)
    task = s.tasks()[0]
    assert output == GOLDEN and task["status"] == "DONE"
    assert len(s.mock.requests) == 2 and len(s.executor_requests()) == 1
    assert stored_values(task, True) == VALUES
    bundle = task["work_records"][-1]["allocation"]
    assert bundle["output"] == output and bundle["instruction_version"] == task["instruction_version"] == 1
    assert bundle["checks"] == task["completed"][-1]["calculation_report"]["checks"]
    event = s.loop.conversation_memory.get_events(s.selected[-1].scope, [bundle["response_id"]])
    assert len(event) == 1 and event[0].content == output
    assert s.selected[-1].calculation_report()["basis"]["result_id"] == bundle["result_id"]


@pytest.mark.parametrize("kind", ["wrong_fixed", "incomplete", "unsubmitted"])
async def test_iteration_limit_does_not_complete_unverified_submission(tmp_path, monkeypatch, kind):
    from .t51_harness import calls
    s = session(tmp_path, monkeypatch)
    output = await submitted_once(s,
        values={"퍼팅":"25", "어프로치":"10", "아이언":"15"} if kind == "wrong_fixed" else None,
        omit="아이언" if kind == "incomplete" else None,
        steps=[calls(("invalid", "submit_time_allocation", {}))] if kind == "unsubmitted" else None)
    task = s.tasks()[0]
    assert task["status"] == "IN_PROGRESS" and output != GOLDEN
    assert not task["work_records"][-1].get("allocation")
    assert not task["completed"]
    assert len(s.executor_requests()) == 1


async def test_conflict_question_stays_in_progress_at_one_iteration(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    s.loop.max_iterations = 1
    output = await turn(s, START, proposal(route="execute"))
    assert "고정 항목 합은 50분, 총 시간은 45분" in output
    assert s.tasks()[0]["status"] == "IN_PROGRESS"
    assert not s.executor_requests() and not s.tasks()[0]["completed"]
    assert s.tasks()[0]["work_records"][-1]["stop_reason"] == "awaiting_confirmation"


async def test_submission_and_finalization_are_idempotent(tmp_path, monkeypatch):
    s = session(tmp_path, monkeypatch)
    output = await submitted_once(s, duplicate=True)
    assert output == GOLDEN
    selected = s.selected[-1]
    before = copy.deepcopy(s.tasks()[0])
    bundle = before["work_records"][-1]["allocation"]
    for _ in range(2):
        assert selected.allocation.finalize(None) == (GOLDEN, "completed")
        assert selected.allocation.persist(GOLDEN, "completed") == (GOLDEN, "completed", bundle["response_id"])
    assert s.tasks()[0] == before
    events = selected.control.tasks.events(selected.scope, before["session_id"])
    assert len([e for e in events if e.actor == "assistant" and e.content == GOLDEN]) == 1
    assert len(before["work_records"]) == len(before["completed"]) == 1
    saved = s.loop.sessions.get_or_create("cli:s")
    assert len([m for m in saved.messages if m.get("role") == "assistant" and m.get("content") == GOLDEN]) == 1
    assert len(s.executor_requests()) == 1


@pytest.mark.parametrize("fault", ["cas", "raw_event", "new_instruction"])
async def test_submitted_success_never_published_when_commit_fails(tmp_path, monkeypatch, fault):
    from nanobot.memory.task_context import TaskContextStore
    s = session(tmp_path, monkeypatch)
    save = TaskContextStore.save
    def interrupted(tasks, scope, state, **kw):
        if kw.get("events"):
            if fault == "cas":
                raise ValueError("task_version_conflict")
            if fault == "raw_event":
                import sqlite3
                raise sqlite3.OperationalError("synthetic storage failure")
            tasks.store.append_event(scope=scope, conversation_id=state["session_id"], session_id=state["session_id"],
                                     actor="user", event_type="USER_MESSAGE", content="총45분으로 변경해 주세요.")
        return save(tasks, scope, state, **kw)
    monkeypatch.setattr(TaskContextStore,"save",interrupted)
    output = await submitted_once(s)
    assert output != GOLDEN and "allocation_save_failed" in output
    task = s.tasks()[0]
    assert task["status"] != "DONE"
    bundle = task["work_records"][-1]["allocation"]
    assert bundle["status"] == "prepared"
    assert not s.loop.conversation_memory.get_events(s.selected[-1].scope,[bundle["response_id"]])


async def test_submission_rechecks_new_instruction_before_finalization(tmp_path, monkeypatch):
    from nanobot.agent.time_allocation import AllocationController
    s = session(tmp_path, monkeypatch)
    original = AllocationController.submit
    def submit(controller,*a,**kw):
        result = original(controller,*a,**kw)
        selected = controller.selected
        controller.tasks.store.append_event(scope=selected.scope, conversation_id=selected.state["session_id"],
            session_id=selected.state["session_id"], actor="user", event_type="USER_MESSAGE",
            content="총45분으로 변경해 주세요.")
        return result
    monkeypatch.setattr(AllocationController,"submit",submit)
    output = await submitted_once(s)
    assert output != GOLDEN and s.tasks()[0]["status"] != "DONE"
    assert not any(r.get("allocation") for r in s.tasks()[0].get("work_records", []))
