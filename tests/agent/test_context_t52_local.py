"""t52 contrast tests. The MOCK model supplies only the relation choice/design, tool calls and final
responses; ids it uses are read from the real assembled design input (nothing pre-specified by the test).
Input assembly, validation, linking, gating, tool execution, records, verdict and task state are real."""
import json

import pytest

from nanobot.agent.context_relations import parse_relations
from .t51_harness import Session, calls, design, design_response, final
from .test_context_selection import env as env  # noqa: F401
from .test_context_t51_local import (A2, CHK1, CHK2, R6, R6P, SUMMARY, ans, golf_turn1, minutes, notes,
                                     spec, write)

G2 = "총 45분으로 줄여 주세요. 퍼팅 20분은 유지하고 나머지는 반씩 나눠 주세요."
G2B = "전체 시간을 45분짜리로 맞춰 주실래요? 퍼팅은 그대로 20분이요."
NEW = "파이썬에서 리스트를 정렬하는 방법을 알려 주세요."


def payload_of(messages):
    return json.loads(messages[1]["content"])


def followup(answer, changed, keep=("드라이버",), relation="modify_answer", seen=None, **extra):
    """MOCK designer: reads ids and kept conditions from the real prior_context it was given."""
    def step(messages):
        pc = payload_of(messages)["prior_context"]
        if seen is not None:
            seen["pc"] = pc
        kept = [{"text": c["text"], "source_id": c["source_id"]} for c in pc["kept_conditions"]
                if any(k in c["text"] for k in keep)]
        ref = {"relation": relation, "task_id": pc["current_task_id"],
               "response_id": pc["previous_answer"]["ref_id"],
               "changed_conditions": [{"text": changed, "source_span": changed}], "kept_conditions": kept}
        return design_response(design(goal="45분으로 축소", request_type="modify", answer=answer, reference=ref,
                                      **spec(CHK2, {"퍼팅": 20, "어프로치": 12.5, "아이언": 12.5}, 45), **extra))
    return step


async def golf_followup(s, text, changed):
    _, task_id = await golf_turn1(s)
    seen = {}
    s.mock.designs.append(followup(A2, changed, seen=seen))
    out = await s.turn(text)
    return out, task_id, seen


@pytest.mark.parametrize("text,changed", [(G2, "총 45분으로"), (G2B, "45분짜리로")])
@pytest.mark.asyncio
async def test_f1_followup_reduces_and_keeps_conditions_without_parser(tmp_path, monkeypatch, text, changed):
    assert not [r for r in parse_relations(text) if r.action in {"edit", "write"}]   # parser sees nothing
    s = Session(tmp_path, monkeypatch)
    out, task_id, seen = await golf_followup(s, text, changed)
    pc = seen["pc"]
    assert "60:00" in pc["previous_answer"]["excerpt"] and "omitted" not in pc["previous_answer"]
    assert any("드라이버는 제외" in c["text"] for c in pc["kept_conditions"])
    assert pc["task_candidates"] == [task_id]
    sel = s.selected[-1]
    assert sel.design.task_id == task_id and sel.design.task_relation == "change"   # linked in code
    assert any("드라이버" in c["text"] for c in sel.state["carried_conditions"])
    assert sel.state["changed_conditions"][0]["source_span"] == changed
    got = minutes(out)
    assert got == {"퍼팅": 20, "어프로치": 12.5, "아이언": 12.5} and "드라이버 연습은 제외" in out
    assert CHK1 not in sel.completion_reports
    assert len(s.tasks()) == 1 and s.tasks()[0]["status"] == "DONE"


@pytest.mark.asyncio
async def test_f1_reference_with_foreign_ids_is_rejected(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    await golf_turn1(s)

    def bad(messages):
        ref = {"relation": "modify_answer", "task_id": "task-not-offered", "response_id": "evt-not-offered"}
        return design_response(design(goal="45분", answer=A2, reference=ref))
    s.mock.designs.extend([bad, followup(A2, "총 45분으로")])
    await s.turn(G2)
    repair = [r for r in s.mock.requests if r["design"]][-1]["messages"]
    codes = repair[-1]["content"]
    assert "reference_unknown_task" in codes and "reference_unknown_response" in codes
    assert s.selected[-1].design.reference.task_id == s.tasks()[0]["task_id"]


@pytest.mark.asyncio
async def test_f2_new_topic_does_not_carry_old_conditions(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    _, old = await golf_turn1(s)

    def carries(messages):    # MOCK: a wrong reading that drags the golf conditions into a new topic
        pc = payload_of(messages)["prior_context"]
        ref = {"relation": "new", "kept_conditions": [{"text": c["text"], "source_id": c["source_id"]}
                                                      for c in pc["kept_conditions"]]}
        return design_response(design(goal="정렬 방법", answer="sorted(xs) 또는 xs.sort()", reference=ref))
    s.mock.designs.extend([carries, design_response(design(goal="정렬 방법", answer="sorted(xs) 또는 xs.sort()",
                                                           reference={"relation": "new"}))])
    out = await s.turn(NEW)
    repair = [r for r in s.mock.requests if r["design"]][-1]["messages"][-1]["content"]
    assert "reference_new_topic_carries_prior" in repair
    tasks = s.tasks()
    assert len(tasks) == 2
    new = next(t for t in tasks if t["task_id"] != old)
    assert not new.get("carried_conditions") and all("드라이버" not in (c.get("text") or "")
                                                     for c in new["user_constraints"])
    assert "sorted" in out


async def two_plans(s, tmp_path):
    a, b = tmp_path / "plan_a.md", tmp_path / "plan_b.md"
    text = "plan_a.md와 plan_b.md 파일에 각각 60분 연습 계획을 저장해 주세요."
    await s.turn(text, design(goal="계획 파일 두 개", route="execute", request_type="execute",
                              capabilities_needed=["write_file"]), steps=[
        calls(write("wa", a, "A 계획 60분\n")), calls(write("wb", b, "B 계획 60분\n")),
        final("두 파일을 저장했습니다.")])
    return a, b


def pick(name, seen):
    def step(messages):
        pc = payload_of(messages)["prior_context"]
        seen.setdefault("pcs", []).append(pc)
        art = next(x for x in pc["recent_artifacts"] if x["name"] == name)
        ref = {"relation": "modify_answer", "artifact_ids": [art["artifact_id"]], "task_id": pc["current_task_id"]}
        return design_response(design(goal="45분 계획 제시", answer="45분 계획(" + name + " 기준): ...",
                                      reference=ref))
    return step


@pytest.mark.asyncio
async def test_f3_one_of_several_artifacts_by_name(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    a, b = await two_plans(s, tmp_path)
    seen = {}
    s.mock.designs.append(pick("plan_b.md", seen))
    out = await s.turn("plan_b.md의 계획을 45분 버전으로 보여 주세요.")
    arts = {x["name"]: x for x in seen["pcs"][0]["recent_artifacts"]}
    assert set(arts) >= {"plan_a.md", "plan_b.md"} and arts["plan_b.md"]["path"].endswith("plan_b.md")
    assert s.selected[-1].design.reference.artifact_ids == [arts["plan_b.md"]["artifact_id"]]
    assert "plan_b.md" in out and a.read_text() == "A 계획 60분\n" and b.read_text() == "B 계획 60분\n"


@pytest.mark.asyncio
async def test_f3_choice_contradicting_the_named_artifact_is_rejected(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    await two_plans(s, tmp_path)
    seen = {}
    s.mock.designs.extend([pick("plan_a.md", seen), pick("plan_b.md", seen)])
    await s.turn("plan_b.md의 계획을 45분 버전으로 보여 주세요.")
    repair = [r for r in s.mock.requests if r["design"]][-1]["messages"][-1]["content"]
    assert "reference_artifact_contradicts_request" in repair


@pytest.mark.asyncio
async def test_f4_unresolvable_reference_asks_a_specific_question(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    a, b = await two_plans(s, tmp_path)
    s.mock.designs.append(pick("plan_a.md", {}))         # MOCK guesses one; the request names none
    n_exec = len(s.executor_requests())
    out = await s.turn("그 파일의 계획을 45분으로 줄여 주세요.")
    assert "확인이 필요합니다" in out and "plan_a.md" in out and "plan_b.md" in out
    assert len([r for r in s.mock.requests if r["design"]]) == 2      # no repair call for an ambiguity
    assert len(s.executor_requests()) == n_exec                       # nothing executed
    assert a.read_text() == "A 계획 60분\n" and b.read_text() == "B 계획 60분\n"


# ---- save / report ---------------------------------------------------------------------------------------
CLAIM = "요약: " + SUMMARY + " summary.md에 저장했습니다."


def sup():
    return {"reason": "supplement_requirement", "information_needs": [], "completion_checks": ["summary.md 저장"],
            "requirements": [{"action": "write", "target": "external_resource", "necessity": "required",
                              "source_span": "그 요약은 summary.md에 남겨 두세요", "target_ref": "summary.md"}]}


def design6():
    return design(goal="notes.md 요약", route="execute", capabilities_needed=["read_file", "write_file"],
                  **ans("요약 답변"))


def feedback_seen(s):
    return list(dict.fromkeys(m["content"] for r in s.executor_requests() for m in r["messages"]
                              if m.get("role") == "user" and "execution records do not support" in str(m.get("content"))))


@pytest.mark.asyncio
async def test_s1_omission_supplement_save_done(tmp_path, monkeypatch):
    p, out_p = notes(tmp_path), tmp_path / "summary.md"
    s = Session(tmp_path, monkeypatch)
    out = await s.turn(R6, design6(), steps=[
        calls(("rd", "read_file", {"path": str(p)})),
        final(CLAIM),                                                  # claim before saving -> back to loop
        calls(("sp", "revise_context_requirements", sup())),
        calls(write("wr", out_p, SUMMARY + "\n")),
        calls(("rd2", "read_file", {"path": str(out_p)})),
        calls(("cc", "record_context_completion", {"check": "summary.md 저장", "tool_call_ids": ["wr", "rd2"],
               "status": "passed", "finding": SUMMARY, "spec": {"result_kind": "file_content",
               "target": "summary.md", "expected_values": {"contains": SUMMARY}}})),
        final(CLAIM)])
    fb = feedback_seen(s)
    assert len(fb) == 1 and "supplement_requirement" in fb[0] and R6 in fb[0]
    assert out_p.read_text().strip() == SUMMARY
    assert s.tasks()[0]["status"] == "DONE" and "summary.md: 수행 및 확인 완료" in out and "저장했습니다" in out


async def omit_twice(s, tmp_path, second):
    notes(tmp_path)
    return await s.turn(R6, design6(), steps=[
        calls(("rd", "read_file", {"path": str(tmp_path / "notes.md")})), final(CLAIM), final(second)])


@pytest.mark.parametrize("second", [CLAIM, "요약: " + SUMMARY + " (summary.md 저장은 하지 못했습니다.)"])
@pytest.mark.asyncio
async def test_s2_design_and_supplement_omit_then_claim(tmp_path, monkeypatch, second):
    s = Session(tmp_path, monkeypatch)
    out = await omit_twice(s, tmp_path, second)
    assert not (tmp_path / "summary.md").exists()
    assert len(feedback_seen(s)) == 1                                # one in-budget return, no more
    assert s.tasks()[0]["status"] != "DONE"
    first = out.splitlines()[0]
    assert "저장했습니다" not in first and "summary.md: 수행되지 않음" in out
    assert "summary.md에 저장했습니다" not in out                     # the unsupported claim is not shown
    sel = s.selected[-1]
    assert sel.claim_failures()                                      # retraction is not success either


def failing_write(s, monkeypatch, mode):
    tool = s.loop.tools.get("write_file")
    real = tool.execute

    async def wrapped(**kw):
        if mode == "error":
            raise PermissionError("permission denied: " + kw["path"])
        await real(**kw)
        raise TimeoutError("tool call timed out after 30s (no response)")
    monkeypatch.setattr(tool, "execute", wrapped)


def design6p():
    return design(goal="요약 저장", route="execute", request_type="execute",
                  capabilities_needed=["read_file", "write_file"], completion_checks=["summary.md 저장"],
                  completion_specs=[{"check": "summary.md 저장", "result_kind": "mutation_result",
                                     "target": "summary.md"}])


@pytest.mark.parametrize("mode,label", [("error", "수행되지 않음"), ("timeout", "결과 불명")])
@pytest.mark.asyncio
async def test_s3_failed_or_unknown_write_then_claim(tmp_path, monkeypatch, mode, label):
    p, out_p = notes(tmp_path), tmp_path / "summary.md"
    s = Session(tmp_path, monkeypatch)
    failing_write(s, monkeypatch, mode)
    out = await s.turn(R6P, design6p(), steps=[
        calls(("rd", "read_file", {"path": str(p)})), calls(write("wr", out_p, SUMMARY + "\n")),
        final(CLAIM), final(CLAIM)])
    sel = s.selected[-1]
    assert s.tasks()[0]["status"] != "DONE"
    assert ("summary.md: " + label) in out and "summary.md에 저장했습니다" not in out
    assert [c["status"] for c in sel.claim_failures()] == ["not_done" if mode == "error" else "unknown"]
    assert out_p.exists() == (mode == "timeout")                       # real file state matches the label


@pytest.mark.asyncio
async def test_s4_claim_citing_another_target_or_a_past_record(tmp_path, monkeypatch):
    p, out_p, other = notes(tmp_path), tmp_path / "summary.md", tmp_path / "other.md"
    s = Session(tmp_path, monkeypatch)
    await s.turn(R6P, design6p(), steps=[
        calls(("rd", "read_file", {"path": str(p)})), calls(write("wr", out_p, SUMMARY + "\n")),
        calls(("rd2", "read_file", {"path": str(out_p)})),
        calls(("cc", "record_context_completion", {"check": "summary.md 저장", "tool_call_ids": ["wr", "rd2"],
               "status": "passed", "finding": SUMMARY, "spec": {"result_kind": "file_content",
               "target": "summary.md", "expected_values": {"contains": SUMMARY}}})),
        final("summary.md에 저장했습니다.")])
    before = out_p.read_text()
    out = await s.turn("notes.md를 다시 읽고 요약을 other.md에 저장해 주세요.", design(
        goal="other 저장", route="execute", request_type="execute", capabilities_needed=["read_file", "write_file"]),
        steps=[calls(write("wo", other, "x\n")),
               final("summary.md에 저장했습니다 (기록 wr)."), final("summary.md에 저장했습니다 (기록 wr).")])
    sel = s.selected[-1]
    st = {c["target"]: c["status"] for c in sel.change_claims}
    assert st["summary.md"] == "prior_turn_only"                     # a past record is not this turn's work
    assert "other.md: 수행했으나 확인 미완료" in out and "summary.md: 이번 요청에서는 수행되지 않음" in out
    assert out_p.read_text() == before
    assert next(t for t in s.tasks())["status"] != "DONE"


@pytest.mark.asyncio
async def test_s5_existing_file_with_different_content_is_not_this_requests_save(tmp_path, monkeypatch):
    out_p = tmp_path / "summary.md"
    out_p.write_text("옛 내용\n")
    s = Session(tmp_path, monkeypatch)
    out = await omit_twice(s, tmp_path, CLAIM)
    assert out_p.read_text() == "옛 내용\n"
    assert s.tasks()[0]["status"] != "DONE" and "summary.md: 수행되지 않음" in out


@pytest.mark.asyncio
async def test_s5b_already_in_desired_state_is_distinguished(tmp_path, monkeypatch):
    p, out_p = notes(tmp_path), tmp_path / "summary.md"
    out_p.write_text(SUMMARY + "\n")
    s = Session(tmp_path, monkeypatch)
    d = design6p()
    d.completion_specs[0].result_kind = "file_content"
    out = await s.turn(R6P, d, steps=[
        calls(("rd", "read_file", {"path": str(p)}), ("rd2", "read_file", {"path": str(out_p)})),
        calls(("cc", "record_context_completion", {"check": "summary.md 저장", "tool_call_ids": ["rd2"],
               "status": "passed", "finding": SUMMARY})),
        final(CLAIM), final("요약: " + SUMMARY + " summary.md는 이미 같은 내용이라 변경하지 않았습니다.")])
    sel = s.selected[-1]
    assert len(feedback_seen(s)) == 1                    # "저장했습니다" with no write is not accepted
    assert not sel.claim_failures()                      # the accurate report is not a false claim
    assert "summary.md: 변경 없이 확인됨" in out and out_p.read_text() == SUMMARY + "\n"


@pytest.mark.asyncio
async def test_s6_quoting_the_sentence_is_not_a_claim(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    text = "'저장했습니다'라는 문장을 더 공손하게 고쳐 주세요."
    answer = "공손한 표현: 저장을 완료했습니다. 또는 \"저장해 두었습니다\"도 좋습니다."
    out = await s.turn(text, design(goal="문장 다듬기", answer=answer))
    assert out == answer and s.tasks()[0]["status"] == "DONE"
    assert not s.selected[-1].claim_failures()


@pytest.mark.asyncio
async def test_s7_no_file_change_is_not_an_answer_ban(tmp_path, monkeypatch):
    p = notes(tmp_path)
    before = p.read_text()
    s = Session(tmp_path, monkeypatch)
    text = "notes.md 파일은 수정하지 말고 제목 변경안만 보여 주세요."
    answer = "변경안: 첫 줄을 '# 새 제목'으로 바꾸는 안입니다. notes.md는 수정하지 않았습니다."
    out = await s.turn(text, design(goal="제목 제안 보여 주기", route="execute", capabilities_needed=["read_file"],
                                    **ans("변경안 답변")), steps=[
        calls(("rd", "read_file", {"path": str(p)})), final(answer)])
    assert p.read_text() == before
    assert out.startswith(answer) and not s.selected[-1].claim_failures() and not feedback_seen(s)
    assert s.tasks()[0]["status"] == "DONE", out


def test_scrub_ignores_sentence_less_retraction_flags():
    from nanobot.agent.context_claims import scrub
    claims = [{"sentence": "", "target": "summary.md", "status": "not_done", "retracted": True},
              {"sentence": "summary.md에 저장했습니다.", "target": "summary.md", "status": "not_done"}]
    out = scrub("요약: 금요일. summary.md에 저장했습니다.", claims)
    assert out == "요약: 금요일. [기록으로 확인되지 않은 변경 주장 생략]"
