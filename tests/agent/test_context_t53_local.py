"""t53 local verification. The MOCK model supplies only designs (including the reference CHOICE, made
from ids it reads in the real assembled prompt), tool calls and final texts. Input assembly, reference
validation, requirement restoration, gating, tool execution, records, claim checks, verdict and task state
are the real implementation. Nothing here measures a real model's choice accuracy."""
import json

import pytest

from .t51_harness import Session, calls, design, design_response, final
from .test_context_selection import env as env  # noqa: F401
from .test_context_t51_local import A1, A2, A2_WRONG, CHK2, R6, SUMMARY, golf_turn1, minutes, notes, spec, write
from .test_context_t52_local import design6, sup, two_plans

G45 = "총 45분으로 줄여 주세요"
SETUP = "unexpected executor call"


def payload_of(messages):
    return json.loads(messages[1]["content"])


def no_setup_error(out):
    """An exhausted MOCK queue is a test-construction error, never counted as a product result."""
    if out and SETUP in out:
        pytest.fail("TEST SETUP ERROR (mock response not prepared): " + out[-200:])


def choose_from_prompt(answer, seen, keep=("퍼팅은 20분", "드라이버")):
    """MOCK rule: take ids only from the prompt actually sent; keep offered conditions naming the kept items;
    changed span = the request text itself. No id or relation is supplied by the test."""
    def step(messages):
        raw = messages[1]["content"]
        p = payload_of(messages)
        pc = p["prior_context"]
        seen.update(raw=raw, pc=pc, request=p["request"])
        kept = [{"text": c["text"], "source_id": c["source_id"]} for c in pc["kept_conditions"]
                if any(k in c["text"] for k in keep)]
        ref = {"relation": "modify_answer", "task_id": pc["current_task_id"],
               "response_id": pc["previous_answer"]["ref_id"], "kept_conditions": kept,
               "changed_conditions": [{"text": "총 45분", "source_span": "총 45분으로"}]}
        return design_response(design(goal="45분으로 축소", request_type="modify", answer=answer, reference=ref,
                                      **spec(CHK2, {"퍼팅": 20, "어프로치": 12.5, "아이언": 12.5}, 45)))
    return step


# ---- 1A input assembly ----------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_1A_first_design_prompt_carries_prior_plan_conditions_and_ids(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    _, task_id = await golf_turn1(s)
    seen = {}
    s.mock.designs.append(choose_from_prompt(A2, seen))
    no_setup_error(await s.turn(G45))                          # no design object / relation passed by the test
    first = [r for r in s.mock.requests if r["design"]][1]     # the FIRST design call of turn 2
    raw = first["messages"][1]["content"]
    pc = json.loads(raw)["prior_context"]
    assert json.loads(raw)["request"] == G45
    assert "40:00-60:00 아이언 20분" in pc["previous_answer"]["excerpt"]          # previous plan
    texts = " ".join(c["text"] for c in pc["kept_conditions"])
    assert "퍼팅은 20분" in texts and "드라이버는 제외" in texts
    assert pc["current_task_id"] == task_id and pc["task_candidates"] == [task_id]
    assert pc["previous_answer"]["ref_id"] and all(c["source_id"] for c in pc["kept_conditions"])
    assert not pc["parser_hints"] or all(h.get("action") not in {"edit", "write"} for h in pc["parser_hints"])


# ---- 1B after the MOCK choice ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_1B_code_restores_requirements_after_mock_choice(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    _, task_id = await golf_turn1(s)
    seen = {}
    s.mock.designs.append(choose_from_prompt(A2, seen))
    out = await s.turn(G45)
    no_setup_error(out)
    sel = s.selected[-1]
    assert sel.design.task_id == task_id and sel.design.task_relation == "change"   # linked by code
    carried = " ".join(c["text"] for c in sel.state["carried_conditions"])
    assert "퍼팅은 20분" in carried and "드라이버" in carried
    assert sel.state["changed_conditions"][0]["source_span"] == "총 45분으로"
    assert minutes(out) == {"퍼팅": 20, "어프로치": 12.5, "아이언": 12.5} and "드라이버 연습은 제외" in out
    tasks = s.tasks()
    assert len(tasks) == 1 and tasks[0]["status"] == "DONE"
    assert any("드라이버" in (c.get("text") or "") for c in tasks[0]["user_constraints"])   # originals stay


@pytest.mark.asyncio
async def test_1B_wrong_numbers_after_mock_choice_are_not_accepted(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    await golf_turn1(s)
    s.mock.designs.extend([choose_from_prompt(A2_WRONG, {}), choose_from_prompt(A2_WRONG, {})])
    out = await s.turn(G45)
    no_setup_error(out)
    assert s.tasks()[0]["status"] != "DONE" and "12분 30초" not in out.splitlines()[0]


# ---- 1C failure / contrast paths -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_1C_nonexistent_reference_is_rejected_without_restoration(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    await golf_turn1(s)

    def bad(messages):
        ref = {"relation": "modify_answer", "task_id": "task-xyz", "response_id": "evt-xyz",
               "changed_conditions": [{"text": "총 45분", "source_span": "총 45분으로"}]}
        return design_response(design(goal="45분", answer=A2, reference=ref))
    s.mock.designs.extend([bad, bad])
    out = await s.turn(G45)
    no_setup_error(out)
    codes = [r for r in s.mock.requests if r["design"]][-1]["messages"][-1]["content"]
    assert "reference_unknown_task" in codes
    sel = s.selected[-1]
    assert not (sel.state or {}).get("carried_conditions")
    assert A2 not in (out or "") or s.tasks()[-1]["status"] != "DONE"


@pytest.mark.asyncio
async def test_1C_ambiguous_target_asks_instead_of_restoring(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    a, b = await two_plans(s, tmp_path)

    def guess(messages):
        pc = payload_of(messages)["prior_context"]
        art = pc["recent_artifacts"][0]
        return design_response(design(goal="45분", answer="45분 계획", reference={
            "relation": "modify_answer", "artifact_ids": [art["artifact_id"]], "task_id": pc["current_task_id"]}))
    s.mock.designs.append(guess)
    n = len(s.executor_requests())
    out = await s.turn("그 계획을 " + G45)
    no_setup_error(out)
    assert "plan_a.md" in out and "plan_b.md" in out and len(s.executor_requests()) == n
    assert not (s.selected[-1].state or {}).get("carried_conditions")
    assert a.read_text() == "A 계획 60분\n" and b.read_text() == "B 계획 60분\n"


@pytest.mark.asyncio
async def test_1C_clear_new_topic_is_a_new_task(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    _, old = await golf_turn1(s)
    s.mock.designs.append(design_response(design(goal="정렬", answer="sorted(xs)", reference={"relation": "new"})))
    out = await s.turn("파이썬 리스트 정렬 방법을 알려 주세요.")
    no_setup_error(out)
    tasks = s.tasks()
    new = next(t for t in tasks if t["task_id"] != old)
    assert len(tasks) == 2 and not new.get("carried_conditions") and out == "sorted(xs)"


# ---- 2 omitted save -> real save -> done -----------------------------------------------------------------
CLAIM = "요약: " + SUMMARY + " summary.md에 저장했습니다."


def check_cc():
    return ("cc", "record_context_completion", {"check": "summary.md 저장", "tool_call_ids": ["wr", "rd2"],
            "status": "passed", "finding": SUMMARY, "spec": {"result_kind": "file_content", "target": "summary.md",
            "expected_values": {"contains": SUMMARY}}})


def outbound_texts(s):
    q = s.loop.bus.outbound
    return [m.content for m in list(q._queue)]


@pytest.mark.asyncio
async def test_2_omitted_save_recovers_to_a_real_verified_save(tmp_path, monkeypatch):
    p, out_p = notes(tmp_path), tmp_path / "summary.md"
    assert "요약은 summary.md에 남겨 두세요" in R6
    s = Session(tmp_path, monkeypatch)
    out = await s.turn(R6, design6(), steps=[
        calls(("rd", "read_file", {"path": str(p)})),
        final(CLAIM),                                                   # 1: claim with no file
        calls(("sp", "revise_context_requirements", sup())),            # 3: supplement
        calls(write("wr", out_p, SUMMARY + "\n")),                      # 5: real write
        calls(("rd2", "read_file", {"path": str(out_p)})),              # 6: read back
        calls(check_cc()),
        final(CLAIM)])
    no_setup_error(out)
    sel = s.selected[-1]
    fb = [m["content"] for r in s.executor_requests() for m in r["messages"]
          if m.get("role") == "user" and "execution records do not support" in str(m.get("content"))]
    assert fb and "summary.md" in fb[0]                                  # 1 detected
    assert not any("저장했습니다" in (t or "") for t in outbound_texts(s))  # 2 nothing shown mid-way
    req = {r["target"]: r for r in sel.required_results() if r.get("target")}
    assert any(t.endswith("summary.md") for t in req)                    # 3 requirement now present
    wr = sel.observations["wr"]
    assert wr["status"] == "ok" and not wr["read_only"]                  # 4/5 the gate let a real write through
    assert out_p.read_text() == SUMMARY + "\n"                           # 5/6 real file content
    assert {c["target"]: c["status"] for c in sel.change_claims}["summary.md"] == "verified"
    t = s.tasks()[0]
    assert t["status"] == "DONE" and not t["unfinished"]                 # 7 state == report
    assert "summary.md: 수행 및 확인 완료" in out
    assert [o["name"] for o in sel.observations.values()].count("write_file") == 1


@pytest.mark.asyncio
async def test_2_write_before_supplement_is_gated_to_the_current_requirements(tmp_path, monkeypatch):
    """Permission/target check: a write the current requirements do not cover is not executed silently."""
    p, out_p = notes(tmp_path), tmp_path / "summary.md"
    s = Session(tmp_path, monkeypatch)
    out = await s.turn(R6, design6(), steps=[
        calls(("rd", "read_file", {"path": str(p)})), final(CLAIM),
        calls(write("wr", out_p, SUMMARY + "\n")), final(CLAIM), final(CLAIM)])
    no_setup_error(out)
    o = s.selected[-1].observations.get("wr", {})
    record = {"status": o.get("status"), "exists": out_p.exists(), "task": s.tasks()[0]["status"]}
    assert record == {"status": "error", "exists": False, "task": "IN_PROGRESS"}   # blocked, not silent
    assert "summary.md에 저장했습니다" not in out


@pytest.mark.asyncio
async def test_2_write_failure_during_recovery_is_not_success(tmp_path, monkeypatch):
    p, out_p = notes(tmp_path), tmp_path / "summary.md"
    s = Session(tmp_path, monkeypatch)
    tool = s.loop.tools.get("write_file")

    async def deny(**kw):
        raise PermissionError("permission denied: " + kw["path"])
    monkeypatch.setattr(tool, "execute", deny)
    out = await s.turn(R6, design6(), steps=[
        calls(("rd", "read_file", {"path": str(p)})), final(CLAIM),
        calls(("sp", "revise_context_requirements", sup())), calls(write("wr", out_p, SUMMARY + "\n")),
        final(CLAIM), final(CLAIM)])
    no_setup_error(out)
    assert not out_p.exists() and s.tasks()[0]["status"] != "DONE"
    assert "summary.md: 수행되지 않음" in out and "summary.md에 저장했습니다" not in out


@pytest.mark.asyncio
async def test_2_budget_ends_during_recovery_reports_facts_and_remaining(tmp_path, monkeypatch):
    p = notes(tmp_path)
    s = Session(tmp_path, monkeypatch)
    out = await s.turn(R6, design6(), steps=[
        calls(("rd", "read_file", {"path": str(p)})), final(CLAIM),
        calls(("sp", "revise_context_requirements", sup())), final(CLAIM)])
    no_setup_error(out)
    t = s.tasks()[0]
    assert t["status"] != "DONE" and t["unfinished"]
    assert "summary.md: 수행되지 않음" in out and "summary.md에 저장했습니다" not in out
    assert SUMMARY in out                                                   # what was done is still reported


@pytest.mark.asyncio
async def test_2_after_success_the_write_is_not_repeated(tmp_path, monkeypatch):
    p, out_p = notes(tmp_path), tmp_path / "summary.md"
    s = Session(tmp_path, monkeypatch)
    await s.turn(R6, design6(), steps=[
        calls(("rd", "read_file", {"path": str(p)})), final(CLAIM),
        calls(("sp", "revise_context_requirements", sup())), calls(write("wr", out_p, SUMMARY + "\n")),
        calls(("rd2", "read_file", {"path": str(out_p)})), calls(check_cc()), final(CLAIM)])
    assert s.tasks()[0]["status"] == "DONE"
    writes = [o for o in s.selected[-1].observations.values() if o.get("name") == "write_file"]
    assert len(writes) == 1 and not s.mock.steps                            # loop ended; no extra write asked


# ---- 3 claim identification scope -----------------------------------------------------------------------
async def answer_only(s, tmp_path, final_text, request=R6):
    p = notes(tmp_path)
    return await s.turn(request, design6(), steps=[
        calls(("rd", "read_file", {"path": str(p)})), final(final_text), final(final_text)])


@pytest.mark.asyncio
async def test_3_left_as_file_is_caught(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    out = await answer_only(s, tmp_path, "요약: " + SUMMARY + " 파일로 남겨 두었습니다.")
    no_setup_error(out)
    assert s.tasks()[0]["status"] != "DONE" and "파일로 남겨 두었습니다" not in out


@pytest.mark.xfail(strict=True, reason="t53 GAP: resultative '반영되어 있습니다' is not a claim candidate; "
                   "an unrecognized wording ends DONE when the design also omitted the save")
@pytest.mark.asyncio
async def test_3_reflected_in_the_document_wording_leaks(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    out = await answer_only(s, tmp_path, "요약: " + SUMMARY + " 요청하신 내용은 해당 문서에 반영되어 있습니다.")
    no_setup_error(out)
    assert s.tasks()[0]["status"] != "DONE"


@pytest.mark.asyncio
async def test_3_negated_report_is_not_blocked_as_a_claim(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    text = "요약: " + SUMMARY + " summary.md에는 저장하지 않았습니다."
    out = await answer_only(s, tmp_path, text)
    no_setup_error(out)
    assert not s.selected[-1].change_claims and text in out           # honest report shown, not scrubbed


@pytest.mark.asyncio
async def test_3_quoted_sentence_edit_request_is_not_a_claim(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    req = "‘저장했습니다’라는 문장을 고쳐 주세요."
    answer = "고친 문장: ‘저장을 마쳤습니다’ 또는 “저장해 두었습니다”."
    out = await s.turn(req, design(goal="문장 고치기", answer=answer))
    no_setup_error(out)
    assert out == answer and s.tasks()[0]["status"] == "DONE" and not s.selected[-1].change_claims


# ---- 4 kept-condition cap (8) ---------------------------------------------------------------------------
CONDS = ["조건%d: 항목%d는 %d분" % (i, i, i + 1) for i in range(1, 11)]       # 10 valid conditions


@pytest.mark.asyncio
async def test_4_more_than_eight_conditions_are_flagged_and_not_lost(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    g1 = "다음 조건으로 연습 계획을 만들어 주세요.\n" + "\n".join(CONDS)
    await s.turn(g1, design(goal="계획", answer="계획 v1"))
    task_id = s.tasks()[0]["task_id"]
    seen = {}

    def step(messages):
        pc = payload_of(messages)["prior_context"]
        seen["pc"] = pc
        ref = {"relation": "modify_answer", "task_id": pc["current_task_id"],
               "response_id": pc["previous_answer"]["ref_id"],
               "kept_conditions": [{"text": c["text"], "source_id": c["source_id"]} for c in pc["kept_conditions"]],
               "changed_conditions": [{"text": "총 45분", "source_span": "총 45분으로"}]}
        return design_response(design(goal="45분", answer="계획 v2", reference=ref))
    s.mock.designs.append(step)
    out = await s.turn(G45)
    no_setup_error(out)
    pc = seen["pc"]
    assert len(pc["kept_conditions"]) == 8 and pc.get("omitted_conditions", 0) >= 3
    omitted_src = pc.get("omitted_condition_sources") or []
    offered = {e["event_id"] for e in payload_of([r for r in s.mock.requests if r["design"]][1]["messages"])["sources"]}
    assert omitted_src and set(omitted_src) <= offered             # fetchable by needed_sources
    t = next(x for x in s.tasks() if x["task_id"] == task_id)
    stored = "\n".join(c.get("text") or "" for c in t["user_constraints"])
    assert all(c in stored for c in CONDS)                                  # final state keeps all 10
    assert "조건10" not in " ".join(c["text"] for c in s.selected[-1].state["carried_conditions"])
    assert not any(c.get("status") in {"released", "deleted"} for c in t["user_constraints"])
    # a third follow-up still offers the originals (nothing was released by the 8-item list)
    seen2 = {}

    def step2(messages):
        seen2["pc"] = payload_of(messages)["prior_context"]
        return design_response(design(goal="확인", answer="계획 v3", reference={
            "relation": "modify_answer", "task_id": seen2["pc"]["current_task_id"],
            "response_id": seen2["pc"]["previous_answer"]["ref_id"]}))
    s.mock.designs.append(step2)
    no_setup_error(await s.turn("조금 더 쉬운 표현으로 바꿔 주세요"))
    assert seen2["pc"].get("omitted_conditions", 0) >= 3 and seen2["pc"].get("omitted_condition_sources")
