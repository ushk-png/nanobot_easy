"""Actual user-original replay and independent numeric/output expectations."""
import copy
import json
from pathlib import Path

import pytest

from nanobot.agent.time_allocation import analyze, reduce_requirements

from .test_time_allocation_contract import proposal, session, stored_values, turn

FIXTURE = Path(__file__).parent / "fixtures/time_allocation/live_remainder_reference.json"
GOLDEN = ("독서: 30분\n쓰기: 20분\n듣기: 20분\n"
          "총 시간: 70분 · 배분 합계: 70분 · 잔여: 0분\n합계는 30+20+20=70분입니다.")


def saved():
    return json.loads(FIXTURE.read_text())


def test_saved_actual_user_reference_keeps_participants_and_original_sources():
    record = saved()
    state = {"instruction_version": 2, "effective_requirements": copy.deepcopy(record["first_good_snapshot"]),
             "supersessions": []}
    snapshot = reduce_requirements(state, record["originals"])
    items = {r["expected_values"]["label"]: r for r in snapshot["requirements"] if r["kind"] == "item"}
    assert set(items) == {"독서", "쓰기", "듣기"}
    assert items["독서"]["expected_values"] == {"label": "독서", "mode": "fixed", "value": "30"}
    before = {r["id"]: r for r in record["first_good_snapshot"]["requirements"]}
    for row in snapshot["requirements"]:
        if row["id"] != items["독서"]["id"]:
            assert row == before[row["id"]]
    assert analyze(snapshot) == ("pass", [])
    assert len(state["supersessions"]) == 1
    assert state["supersessions"][0]["requirement_id"] == items["독서"]["id"]


@pytest.mark.parametrize("reference", [
    "나머지는 같은 조건으로 배분해 주세요.",
    "남은 시간은 이전 조건을 유지해 주세요.",
    "나머지 시간은 그대로 배분해 주세요.",
])
async def test_reference_change_completes_output_storage_and_report(tmp_path, monkeypatch, reference):
    record = saved()
    s = session(tmp_path, monkeypatch)
    initial = await turn(s, record["originals"][0]["text"], proposal())
    assert "독서: 20분\n쓰기: 25분\n듣기: 25분" in initial
    before = s.tasks()[0]
    output = await turn(s, "총 시간은 그대로 두고 독서를 30분으로 변경해 주세요. " + reference,
                        proposal(task=before["task_id"], answer=None))
    task = s.tasks()[0]
    assert output == GOLDEN and task["status"] == "DONE"
    assert stored_values(task, True) == {"독서": "30", "쓰기": "20", "듣기": "20"}
    assert task["instruction_version"] == 2 and task["task_id"] == before["task_id"]
    bundle = task["work_records"][-1]["allocation"]
    assert bundle["output"] == output and bundle["result"]["total"] == "70"
    assert bundle["checks"] == task["completed"][-1]["calculation_report"]["checks"]
    assert len(s.mock.requests) == 2 and not s.executor_requests()
    state = s.loop.conversation_memory.get_events(s.selected[-1].scope, [bundle["response_id"]])
    assert len(state) == 1 and state[0].content == GOLDEN


@pytest.mark.parametrize("recipients", ["분석과 검토에", "분석과검토에", "분석, 검토에", "분석과 검토"])
def test_explicit_recipient_list_can_still_create_new_items(recipients):
    state = {"instruction_version": 1}
    snapshot = reduce_requirements(state, [{"event_id": "u1", "sequence": 1,
        "text": "총 70분 계획. 독서 30분 고정. 남은 시간을 " + recipients + " 균등 배분해 주세요."}])
    assert {r["expected_values"]["label"] for r in snapshot["requirements"] if r["kind"] == "item"} == {"독서", "분석", "검토"}
    assert analyze(snapshot) == ("pass", [])


def test_no_recipient_and_no_previous_relation_requires_user_information():
    state = {"instruction_version": 1}
    snapshot = reduce_requirements(state, [{"event_id": "u1", "sequence": 1,
        "text": "총 70분. 독서 30분 고정. 나머지는 같은 조건으로 배분해 주세요."}])
    assert {r["expected_values"]["label"] for r in snapshot["requirements"] if r["kind"] == "item"} == {"독서"}
    assert analyze(snapshot)[0] == "insufficient_info"


async def test_fixed_value_violation_is_still_blocked_after_reference_change(tmp_path, monkeypatch):
    record = saved()
    s = session(tmp_path, monkeypatch)
    await turn(s, record["originals"][0]["text"], proposal())
    task = s.tasks()[0]
    output = await turn(s, record["originals"][1]["text"],
                        proposal({"독서": "20", "쓰기": "25", "듣기": "25"}, total="70", task=task["task_id"]))
    assert s.tasks()[0]["status"] != "DONE" and output != GOLDEN
    assert stored_values(s.tasks()[0])["독서"] == ("fixed", "30")
