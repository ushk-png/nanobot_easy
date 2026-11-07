"""Saved A1 replay and explicitly modified controls; no model or renderer-derived oracle."""
import copy
import json
from pathlib import Path

import pytest

from nanobot.agent.time_allocation import validate_result

from .t51_harness import calls
from .test_time_allocation_contract import raw, session, stored_values, turn

FIXTURE = Path(__file__).parent / "fixtures/time_allocation/live_submit_A1.json"
EXPECTED = ("퍼팅: 20분\n어프로치: 20분\n아이언: 20분\n"
            "총 시간: 60분 · 배분 합계: 60분 · 잔여: 0분\n"
            "합계는 20+20+20=60분입니다.\n제외 항목: 드라이버\n"
            "퍼팅 — 목적: 동작의 일관성을 높입니다. 방법: 자세와 동작의 흐름에 집중해 연습합니다.\n"
            "어프로치 — 목적: 목표에 대한 정확도를 높입니다. 방법: 동작을 반복하고 결과를 살펴 조정합니다.\n"
            "아이언 — 목적: 동작을 안정적으로 조절하는 감각을 익힙니다. 방법: 시도마다 결과를 비교하며 차이를 확인합니다.")
VALUES = {"퍼팅": "20", "어프로치": "20", "아이언": "20"}


def saved():
    record = json.loads(FIXTURE.read_text())
    return record, json.loads(record["raw_submission"])


def item_ids(snapshot):
    return {r["expected_values"]["label"]: r["id"]
            for r in snapshot["requirements"] if r["kind"] == "item"}


def test_saved_original_and_removed_control_validate_without_mutating_evidence():
    record, design = saved()
    snapshot = record["snapshot"]
    original = copy.deepcopy(design["allocation_result"])
    excluded = item_ids(snapshot)["드라이버"]
    removed = copy.deepcopy(original)
    removed["items"] = [i for i in removed["items"] if i["item_id"] != excluded]
    outputs = []
    for candidate in (original, removed):
        decision, checks, result = validate_result(snapshot, candidate, design["allocation_explanations"])
        assert decision == "pass" and len(checks) == 7
        assert all(c["status"] == "pass" for c in checks)
        labels = {v: k for k, v in item_ids(snapshot).items()}
        assert {labels[i["item_id"]]: i["value"] for i in result["items"]} == VALUES
        assert result["total"] == "60" and result["remaining"] == "0"
        outputs.append(result)
    assert outputs[0] == outputs[1] == removed
    assert original == design["allocation_result"]


@pytest.mark.parametrize("variant", ["removed", "zero", "positive", "wrong_fixed"])
@pytest.mark.parametrize("route", ["direct", "execute"])
async def test_saved_actual_response_replay_final_output_and_storage(tmp_path, monkeypatch, variant, route):
    record, original = saved()
    s = session(tmp_path, monkeypatch)
    s.loop.max_iterations = 1
    observed = {}

    def replay(messages):
        # Only opaque runtime IDs change; user text, amounts and design are saved actual data.
        payload = json.loads(messages[1]["content"])
        ids = item_ids(payload["allocation_new_requirements"])
        old_ids = item_ids(record["snapshot"])
        mapping = {old: ids[label] for label, old in old_ids.items()}
        mapping[record["user_event_id"]] = next(
            e["event_id"] for e in payload["sources"] if e.get("content_ref") == "request")

        def rebind(value):
            if isinstance(value, dict):
                return {k: rebind(v) for k, v in value.items()}
            if isinstance(value, list):
                return [rebind(v) for v in value]
            return mapping.get(value, value) if isinstance(value, str) else value

        data = rebind(original)
        candidate = data["allocation_result"]
        if variant == "removed":
            candidate["items"] = [i for i in candidate["items"] if i["item_id"] != ids["드라이버"]]
        elif variant in {"positive", "wrong_fixed"}:
            for item in candidate["items"]:
                if variant == "positive" and item["item_id"] == ids["드라이버"]:
                    item["value"] = "1"
                elif variant == "wrong_fixed" and item["item_id"] == ids["퍼팅"]:
                    item["value"] = "25"
                elif variant == "wrong_fixed" and item["item_id"] == ids["어프로치"]:
                    item["value"] = "15"
        observed["submitted"] = copy.deepcopy(candidate)
        observed["notes"] = copy.deepcopy(data["allocation_explanations"])
        if route == "execute":
            data["route"] = "execute"
        return raw(data)

    def submit(messages):
        return calls(("actual-result", "submit_time_allocation",
                      {"result": observed["submitted"], "explanations": observed["notes"]}))

    output = await turn(s, record["request"], replay) if route == "direct" else None
    if route == "execute":
        s.mock.designs.append(replay)
        output = await s.turn(record["request"], steps=[submit])
    task = s.tasks()[0]
    assert task["instruction_version"] == 1
    assert stored_values(task)["퍼팅"] == ("fixed", "20")
    assert stored_values(task)["드라이버"] == ("excluded", None)
    assert len(s.mock.requests) == (1 if route == "direct" else 2)
    if variant in {"positive", "wrong_fixed"}:
        assert task["status"] == "IN_PROGRESS" and output != EXPECTED
        assert not task["work_records"][-1].get("allocation") and not task["completed"]
        return
    assert output == EXPECTED and task["status"] == "DONE"
    assert stored_values(task, True) == VALUES
    bundle = task["work_records"][-1]["allocation"]
    assert bundle["result"]["total"] == "60" and bundle["result"]["remaining"] == "0"
    if variant == "zero":
        assert bundle["submitted_result"] == observed["submitted"]
    # Local _process_message returns the output without a channel delivery acknowledgement.
    assert bundle["output"] == output and bundle["status"] == "delivery_unknown"
    report = s.selected[-1].calculation_report()
    assert bundle["checks"] == report["checks"] == task["completed"][-1]["calculation_report"]["checks"]
    assert report["basis"]["result_id"] == bundle["result_id"]
    assert report["basis"]["instruction_version"] == bundle["instruction_version"] == 1
    assert all(c["status"] == "pass" and c["result_id"] == bundle["result_id"] for c in bundle["checks"])
    event = s.loop.conversation_memory.get_events(s.selected[-1].scope, [bundle["response_id"]])
    assert len(event) == 1 and event[0].content == output


@pytest.mark.parametrize("mutation", ["positive", "negative", "ambiguous_value", "ambiguous_unit",
                                    "unknown_id", "unknown_zero", "missing_active", "duplicate_excluded",
                                    "wrong_fixed", "wrong_total", "wrong_remaining", "excluded_note"])
def test_bad_submissions_are_not_rescued_by_excluded_zero(mutation):
    record, design = saved()
    candidate = design["allocation_result"]
    notes = design["allocation_explanations"]
    ids = item_ids(record["snapshot"])
    excluded = next(i for i in candidate["items"] if i["item_id"] == ids["드라이버"])
    if mutation in {"positive", "negative", "ambiguous_value"}:
        excluded["value"] = {"positive": "1/1000000", "negative": "-1", "ambiguous_value": "약 0"}[mutation]
    elif mutation == "ambiguous_unit":
        excluded["unit"] = "hour"
    elif mutation == "unknown_id":
        excluded["item_id"] = "not-a-current-requirement"
    elif mutation == "unknown_zero":
        candidate["items"].append({"item_id": "unrequested", "value": "0"})
    elif mutation == "missing_active":
        candidate["items"] = [i for i in candidate["items"] if i["item_id"] != ids["아이언"]]
    elif mutation == "duplicate_excluded":
        candidate["items"].append(copy.deepcopy(excluded))
    elif mutation == "wrong_fixed":
        candidate["items"][0]["value"] = "25"
        candidate["items"][2]["value"] = "15"
    elif mutation == "wrong_total":
        candidate["total"] = "61"
        candidate["remaining"] = "1"
    elif mutation == "wrong_remaining":
        candidate["remaining"] = "1"
    elif mutation == "excluded_note":
        notes.append({"item_id": ids["드라이버"], "purpose": "control", "method": "focus_on_form"})
    assert validate_result(record["snapshot"], candidate, notes)[0] == "result_error"


def test_zero_active_item_is_retained_and_labels_do_not_control_normalization():
    record, design = saved()
    snapshot = record["snapshot"]
    ids = item_ids(snapshot)
    # Explicit contrast: different labels, active fixed zero, equal remaining items at 30 each.
    for row in snapshot["requirements"]:
        if row["kind"] == "item":
            row["expected_values"]["label"] = "renamed-" + row["id"]
            if row["id"] == ids["퍼팅"]:
                row["expected_values"]["value"] = "0"
    candidate = design["allocation_result"]
    for item in candidate["items"]:
        item["value"] = "0" if item["item_id"] in {ids["퍼팅"], ids["드라이버"]} else "30"
    decision, _, result = validate_result(snapshot, candidate, design["allocation_explanations"])
    assert decision == "pass"
    assert {i["item_id"]: i["value"] for i in result["items"]} == {
        ids["퍼팅"]: "0", ids["어프로치"]: "30", ids["아이언"]: "30"}
