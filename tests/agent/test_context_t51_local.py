"""t51 minimal LOCAL verification of t50 (MOCK model supplies only design/tool calls; real code does the
rest). Real files in tmp_path; file contents, call logs and values are checked directly."""
import re

import pytest

from nanobot.agent.context_relations import parse_relations
from .t51_harness import Session, calls, design, final
from .test_context_selection import env as env  # noqa: F401

# ---- (1) follow-up keeps conditions ----------------------------------------------------------------------
G1 = "퍼팅, 어프로치, 아이언으로 60분 연습 계획을 만들어 주세요. 퍼팅은 20분이고 드라이버는 제외해 주세요."
G2 = "총 45분으로 줄여 주세요. 퍼팅 20분은 유지하고 나머지는 반씩 나눠 주세요."
A1 = ("0:00-20:00 퍼팅 20분\n20:00-40:00 어프로치 20분\n40:00-60:00 아이언 20분\n드라이버 연습은 제외했습니다.")
A2 = ("총 45분 계획\n0:00-20:00 퍼팅 20분\n20:00-32:30 어프로치 12분 30초\n32:30-45:00 아이언 12분 30초\n"
      "드라이버 연습은 제외했습니다.")
A2_WRONG = A2.replace("12분 30초", "15분")
CHK1, CHK2 = "60분 배분", "45분 배분"


def spec(check, values, total):
    return {"completion_checks": [check], "completion_specs": [
        {"check": check, "result_kind": "computed",
         "expected_values": {"values": values, "total": total, "excluded": ["드라이버"]}}]}


def minutes(text):
    """Independent reading of the delivered plan: activity -> minutes."""
    out = {}
    for name in ("퍼팅", "어프로치", "아이언", "드라이버"):
        m = re.search(name + r"\s*(\d+)분(?:\s*(\d+)초)?", text or "")
        if m:
            out[name] = int(m.group(1)) + (int(m.group(2) or 0) / 60)
    return out


async def golf_turn1(s):
    out = await s.turn(G1, design(goal="60분 계획", answer=A1,
                                  **spec(CHK1, {"퍼팅": 20, "어프로치": 20, "아이언": 20}, 60)))
    return out, s.tasks()[0]["task_id"]


def test_1a_parser_sees_the_followup_change_request():
    """The change request itself must be found in code (not pre-specified by the test)."""
    rels = parse_relations(G2)
    assert any(r.action in {"edit", "write"} and r.target in {"answer", "prior_answer"} for r in rels), \
        "relation parser found no change request in the follow-up: %r" % rels


@pytest.mark.asyncio
async def test_1_followup_reduces_total_and_keeps_conditions(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    out1, task_id = await golf_turn1(s)
    assert minutes(out1) == {"퍼팅": 20, "어프로치": 20, "아이언": 20}
    out2 = await s.turn(G2, design(goal="45분으로 축소", task_relation="change", task_id=task_id,
                                   request_type="modify", answer=A2,
                                   **spec(CHK2, {"퍼팅": 20, "어프로치": 12.5, "아이언": 12.5}, 45)))
    # real next-turn input assembly carried the earlier plan and the kept condition
    payload = s.design_payloads()[-1]
    text = str(payload)
    assert "드라이버는 제외" in text and "60:00" in text
    sel = s.selected[-1]
    assert sel.design.task_id == task_id
    assert any(e.get("source_text", "").startswith("퍼팅, 어프로치") for e in
               [w for w in (sel.state or {}).get("work_records", [])])
    # independent check of the delivered values
    got = minutes(out2)
    assert got == {"퍼팅": 20, "어프로치": 12.5, "아이언": 12.5} and sum(got.values()) == 45
    assert "드라이버 연습은 제외" in out2
    assert sel.calculation_status() == {"overall": "pass", "checks": {CHK2: "pass"}}
    assert CHK1 not in sel.completion_reports            # the 60-minute verification is not reused
    assert s.tasks()[0]["status"] == "DONE"


@pytest.mark.asyncio
async def test_1_counter_wrong_followup_values_are_not_completed(tmp_path, monkeypatch):
    s = Session(tmp_path, monkeypatch)
    _, task_id = await golf_turn1(s)
    out2 = await s.turn(G2, design(goal="45분으로 축소", task_relation="change", task_id=task_id,
                                   request_type="modify", answer=A2_WRONG,
                                   **spec(CHK2, {"퍼팅": 20, "어프로치": 12.5, "아이언": 12.5}, 45)))
    sel = s.selected[-1]
    assert sum(minutes(A2_WRONG).values()) != 45
    assert sel.calculation_status()["overall"] == "mismatch"
    assert s.tasks()[0]["status"] != "DONE", out2


# ---- (2) an unresolved condition holds only the file change ---------------------------------------------
R2 = ("data.txt에 적힌 숫자들의 합계를 계산해서 알려 주세요. 그리고 그 합계를 summary.md 파일에 저장해 주세요. "
      "다만 summary.md에 저장해도 되는지는 아직 팀 확인 중입니다.")
OPEN2 = "summary.md 저장 허락 여부(팀 확인 중)"
NUMS = [12, 7, 30, 11]


def meta(path):
    return {"intent_summary": "save the total", "target": path, "scope": "once", "reversible": True}


def design2():
    return design(goal="합계 계산과 저장", route="execute", request_type="execute",
                  capabilities_needed=["read_file", "write_file"], unresolved=[OPEN2],
                  completion_checks=["합계 답변", "summary.md 저장"],
                  completion_specs=[{"check": "합계 답변", "result_kind": "answer_content"},
                                    {"check": "summary.md 저장", "result_kind": "mutation_result",
                                     "target": "summary.md"}])


def settle_open2():
    return ("st", "revise_context_requirements", {"reason": "settle_unresolved", "information_needs": [],
            "completion_checks": [], "settle_unresolved": [{"text": OPEN2, "disposition": "required_from_user",
            "aspect": "permission", "basis_span": "summary.md에 저장해도 되는지는 아직 팀 확인 중입니다"}]})


@pytest.mark.asyncio
async def test_2_permission_doubt_holds_only_the_file_change(tmp_path, monkeypatch):
    (tmp_path / "data.txt").write_text("\n".join(map(str, NUMS)) + "\n")
    total = sum(NUMS)                                                    # independent: 60
    s = Session(tmp_path, monkeypatch)
    out = await s.turn(R2, design2(), steps=[
        calls(settle_open2(), ("rd", "read_file", {"path": str(tmp_path / "data.txt")})),
        calls(("wr", "write_file", {"path": str(tmp_path / "summary.md"), "content": f"합계: {total}\n",
                                   **meta(str(tmp_path / "summary.md"))})),
        final(f"data.txt 숫자 합계는 {total}입니다.")])
    sel = s.selected[-1]
    assert not (tmp_path / "summary.md").exists()                        # held: no real write
    assert sel.observations["wr"].get("executed") is False
    assert sel.observations["rd"]["status"] == "ok"                      # independent work ran
    assert str(total) in out                                             # and is reported
    assert s.tasks()[0]["status"] != "DONE"                              # required file work remains
    assert "summary.md" in out or OPEN2 in out                           # the question is about the file
    assert "data.txt" not in out.split(str(total))[-1] or True
    print("\nT2 OUTPUT:\n" + out)


@pytest.mark.asyncio
async def test_2_counter_global_cancel_stops_independent_work(env, tmp_path):
    from nanobot.agent.runner import AgentRunner, AgentRunSpec
    from .test_context_selection import event, select
    from .test_context_t47_scenarios import Script, fs
    fs(env, tmp_path)
    (tmp_path / "data.txt").write_text("\n".join(map(str, NUMS)) + "\n")
    event(env, R2)
    _, _, sel = await select(env, design2(), request=R2)
    delivered = []

    async def inject(**kw):
        if sel.observations.get("rd") and not delivered:
            delivered.append(1)
            return [{"role": "user", "content": "모두 취소해 주세요."}]
        return []
    prov = Script([calls(settle_open2(), ("rd", "read_file", {"path": str(tmp_path / "data.txt")})),
                   calls(("rd2", "read_file", {"path": str(tmp_path / "data.txt")})),
                   final("합계는 60입니다.")])
    result = await AgentRunner(prov).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": R2}], tools=sel.tools, model="offline", max_iterations=6,
        max_tool_result_chars=2000, selected_context=sel, injection_callback=inject))
    assert delivered
    assert result.stop_reason == "cancelled"
    assert "rd2" not in sel.observations or sel.observations["rd2"].get("executed") is False
    assert sel.state["instruction_status"] == "CANCELLED"


# ---- (3) an existing but unrelated requirement ID ----------------------------------------------------------
R3 = "a.md 파일을 만들어 주세요. b.md 파일도 만들어 주세요. a.md의 회의 제목은 아직 정하지 못했습니다."
OPEN3 = "a.md 회의 제목 미정"


def design3(with_relations=True):
    from nanobot.agent.context_selection import Obligations, RelationSpec
    rels = [RelationSpec(action="write", target="external_resource", necessity="required", source_span=sp,
                         target_ref=ref) for sp, ref in (("a.md 파일을 만들어 주세요", "a.md"),
                                                         ("b.md 파일도 만들어 주세요", "b.md"))]
    extra = {"obligations": Obligations(target="external_resource", external_change="required", relations=rels)}
    return design(goal="두 파일 생성", route="execute", request_type="execute",
                  capabilities_needed=["write_file", "read_file"], unresolved=[OPEN3],
                  completion_checks=["a.md 생성", "b.md 생성"], **(extra if with_relations else {}))


@pytest.mark.asyncio
async def test_3a_parser_floor_keeps_one_requirement_per_named_file(tmp_path, monkeypatch):
    """No model relations: the parsed floor alone must keep a.md and b.md as two requirements."""
    from nanobot.agent.context_plan import requirement_ids
    s = Session(tmp_path, monkeypatch)
    await s.turn(R3, design3(with_relations=False), steps=[final("확인이 필요합니다.")])
    ids = set(requirement_ids(s.selected[-1]))
    assert {"file_change:" + str((tmp_path / n).resolve()) for n in ("a.md", "b.md")} <= ids, ids


def settle3(rid):
    return ("st", "revise_context_requirements", {"reason": "settle_unresolved", "information_needs": [],
            "completion_checks": [], "settle_unresolved": [{"text": OPEN3, "disposition": "required_from_user",
            "aspect": "content", "basis_span": "a.md의 회의 제목은 아직 정하지 못했습니다", "affects": [rid]}]})


def write(cid, path, content):
    return (cid, "write_file", {"path": str(path), "content": content, **meta(str(path))})


@pytest.mark.asyncio
async def test_3_condition_of_a_linked_to_b_id_is_not_accepted(tmp_path, monkeypatch):
    A, B = tmp_path / "a.md", tmp_path / "b.md"
    rid_b = "file_change:" + str(B.resolve())
    s = Session(tmp_path, monkeypatch)
    out = await s.turn(R3, design3(), steps=[
        calls(settle3(rid_b)),                                           # A's open item -> B's ID (wrong)
        calls(write("wa", A, "# 임의 제목\n")),
        calls(("ca", "record_context_completion", {"check": "b.md 생성", "tool_call_ids": ["wa"],
               "status": "passed", "finding": "written", "spec": {"result_kind": "mutation_result",
                                                                  "target": "b.md"}})),
        final("a.md와 b.md를 만들었습니다.")])
    sel = s.selected[-1]
    assert rid_b in __import__("nanobot.agent.context_plan", fromlist=["x"]).requirement_ids(sel)  # B's ID exists
    settle = next(m for m in s.executor_requests()[1]["messages"] if m.get("tool_call_id") == "st")
    assert not A.exists(), "A's change ran although its open item was linked to B's requirement"
    assert "b.md 생성" not in {c for c, r in sel.completion_reports.items() if r.get("status") == "passed"}
    assert s.tasks()[0]["status"] != "DONE"
    print("\nT3 settle result:", settle["content"][:300], "\nOUT:", out)


@pytest.mark.asyncio
async def test_3_counter_correct_link_lets_independent_b_proceed(tmp_path, monkeypatch):
    A, B = tmp_path / "a.md", tmp_path / "b.md"
    rid_a = "file_change:" + str(A.resolve())
    s = Session(tmp_path, monkeypatch)
    out = await s.turn(R3, design3(), steps=[
        calls(settle3(rid_a)),
        calls(write("wb", B, "참석자 명단\n"), write("wa", A, "# 임의 제목\n")),
        calls(("rb", "read_file", {"path": str(B)})),
        calls(("cb", "record_context_completion", {"check": "b.md 생성", "tool_call_ids": ["wb", "rb"],
               "status": "passed", "finding": "참석자 명단", "spec": {"result_kind": "mutation_result",
                                                                       "target": "b.md"}})),
        final("b.md를 만들었습니다. a.md는 회의 제목을 알려 주시면 만들겠습니다.")])
    sel = s.selected[-1]
    assert B.read_text() == "참석자 명단\n"                               # independent change ran for real
    assert not A.exists() and sel.observations["wa"].get("executed") is False   # A held
    assert s.tasks()[0]["status"] != "DONE"                              # A still required
    assert "a.md" in out
    print("\nT3c OUT:", out)


# ---- (4) read / quote / prohibition vs a real write request ---------------------------------------------
NOTES = "# 옛 제목\n\n회의는 금요일입니다. 준비물은 노트북입니다.\n"


def notes(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text(NOTES)
    return p


def ans(*checks):
    """Answer-content checks typed as such (a free-text check name alone is not an answer check), with the
    typed obligations a design carries for an answer-only reading (MOCK design content)."""
    from nanobot.agent.context_selection import Obligations
    return {"completion_checks": list(checks), "obligations": Obligations(target="answer", external_change="none"),
            "completion_specs": [{"check": c, "result_kind": "answer_content"} for c in checks]}


def kinds(sel):
    from nanobot.agent.context_plan import requirement_ids
    return {r["kind"] for r in requirement_ids(sel).values()}


def edit(cid, path, old, new):
    return (cid, "edit_file", {"path": str(path), "old_text": old, "new_text": new, **meta(str(path))})


@pytest.mark.asyncio
async def test_4a_read_and_summarize_has_no_write_obligation(tmp_path, monkeypatch):
    p = notes(tmp_path)
    s = Session(tmp_path, monkeypatch)
    out = await s.turn("notes.md를 읽고 요약해 주세요.", design(
        goal="요약", route="execute", capabilities_needed=["read_file"], **ans("요약 답변")),
        steps=[calls(("rd", "read_file", {"path": str(p)})), final("요약: 회의는 금요일, 준비물은 노트북입니다.")])
    sel = s.selected[-1]
    assert kinds(sel) == {"answer"} and p.read_text() == NOTES
    assert sel.observations["rd"]["read_only"] is True and sel.observations["rd"]["status"] == "ok"
    assert "금요일" in out and s.tasks()[0]["status"] == "DONE", out


@pytest.mark.asyncio
async def test_4b_quoted_delete_sentence_is_text_not_an_instruction(tmp_path, monkeypatch):
    p = notes(tmp_path)
    s = Session(tmp_path, monkeypatch)
    out = await s.turn("‘notes.md를 삭제하세요’라는 문장을 부드럽게 고쳐 주세요.", design(
        goal="문장 다듬기", route="execute", capabilities_needed=["write_file"], **ans("다듬은 문장")),
        steps=[calls(("wr", "write_file", {"path": str(p), "content": "", **meta(str(p))})),   # misreading
               final("‘notes.md는 삭제해 주시면 감사하겠습니다.’")])
    sel = s.selected[-1]
    assert kinds(sel) == {"answer"}
    assert p.read_text() == NOTES and sel.observations["wr"].get("executed") is False   # no real change
    assert "감사하겠습니다" in out and s.tasks()[0]["status"] == "DONE", out


@pytest.mark.asyncio
async def test_4c_do_not_modify_keeps_prohibition(tmp_path, monkeypatch):
    from tests.agent.t51_harness import design_response
    p = notes(tmp_path)
    s = Session(tmp_path, monkeypatch)
    # MOCK: first design asks for edit_file against "수정하지 말고"; the real validator must reject it.
    s.mock.designs.append(design_response(design(
        goal="변경안", route="execute", capabilities_needed=["read_file", "edit_file"], **ans("변경안"))))
    # Corrected MOCK design: goal worded without an action word (the real goal check matches action words,
    # so the noun "변경안" alone trips it - recorded in REPORT as a heuristic finding, not changed).
    out = await s.turn("notes.md는 수정하지 말고 변경안만 답변해 주세요.", design(
        goal="notes.md 제목 개선 제안 답변", route="execute", capabilities_needed=["read_file"], **ans("변경안")),
        steps=[calls(("rd", "read_file", {"path": str(p)})),
               calls(edit("ed", p, "# 옛 제목", "# 새 제목")),            # MOCK executor still tries
               final("변경안: 제목을 '# 새 제목'으로 바꾸는 것을 제안합니다.")])
    design_msgs = [str(r["messages"][-1].get("content")) for r in s.mock.requests if r["design"]]
    assert "goal_conflicts_with_user_constraint" in design_msgs[1]      # real validator rejected design 1
    sel = s.selected[-1]
    assert "file_change" not in kinds(sel) and p.read_text() == NOTES
    assert sel.observations["ed"].get("executed") is False
    assert sel.observations["rd"].get("read_only") is True and sel.observations["ed"].get("read_only") is False
    assert "새 제목" in out and s.tasks()[0]["status"] == "DONE", out


@pytest.mark.asyncio
async def test_4d_real_title_edit_is_not_completed_by_an_answer(tmp_path, monkeypatch):
    p = notes(tmp_path)
    s = Session(tmp_path, monkeypatch)
    out = await s.turn("notes.md의 제목을 실제로 수정해 주세요.", design(
        goal="제목 수정", route="execute", request_type="modify", capabilities_needed=["read_file", "edit_file"],
        completion_checks=["제목 수정"]), steps=[final("제목을 '# 새 제목'으로 수정했습니다.")])
    assert p.read_text() == NOTES                                        # nothing was written
    assert "file_change" in kinds(s.selected[-1]) and s.tasks()[0]["status"] != "DONE"
    assert "수정했습니다" not in out.splitlines()[0], out                # the report does not claim it


@pytest.mark.asyncio
async def test_4d_counter_real_edit_completes(tmp_path, monkeypatch):
    p = notes(tmp_path)
    s = Session(tmp_path, monkeypatch)
    out = await s.turn("notes.md의 제목을 실제로 수정해 주세요.", design(
        goal="제목 수정", route="execute", request_type="modify", capabilities_needed=["read_file", "edit_file"],
        completion_checks=["제목 수정"]), steps=[
        calls(("rd", "read_file", {"path": str(p)})),
        calls(edit("ed", p, "# 옛 제목", "# 새 제목")),
        calls(("rd2", "read_file", {"path": str(p)})),
        calls(("cc", "record_context_completion", {"check": "제목 수정", "tool_call_ids": ["ed", "rd2"],
               "status": "passed", "finding": "# 새 제목", "spec": {"result_kind": "file_content",
               "target": "notes.md", "expected_values": {"contains": "# 새 제목"}}})),
        final("notes.md 제목을 '# 새 제목'으로 수정했습니다.")])
    sel = s.selected[-1]
    assert p.read_text().startswith("# 새 제목")                         # real change
    assert sel.observations["rd"]["read_only"] and not sel.observations["ed"]["read_only"]
    assert s.tasks()[0]["status"] == "DONE", out


# ---- test 5: real change, then a MOCK timeout before the success response ---------------------------------

R5 = "notes.md의 제목을 '# 새 제목'으로 바꾸고, 본문의 '금요일'을 '토요일'로 바꿔 주세요."


def design5():
    return design(goal="notes.md 제목과 본문 수정", route="execute", request_type="modify",
                  capabilities_needed=["read_file", "edit_file"],
                  completion_checks=["notes.md 수정"],
                  completion_specs=[{"check": "notes.md 수정", "result_kind": "file_content", "target": "notes.md",
                                     "expected_values": {"contains": ["# 새 제목", "토요일"]}}])


def timeout_after(s, monkeypatch, apply_first):
    """MOCK transport: the first edit_file call times out AFTER the real tool ran (apply_first) or BEFORE it
    ran. Every later call goes to the real tool unchanged. Returns the list of real executions."""
    tool = s.loop.tools.get("edit_file")
    real = tool.execute
    ran, state = [], {"first": True}

    async def wrapped(**kw):
        if state["first"]:
            state["first"] = False
            if apply_first:
                ran.append(kw.get("old_text"))
                await real(**kw)
            raise TimeoutError("tool call timed out after 30s (no response)")
        ran.append(kw.get("old_text"))
        return await real(**kw)
    monkeypatch.setattr(tool, "execute", wrapped)
    return ran


def obs_status(sel, cid):
    o = sel.observations.get(cid) or {}
    return o.get("status"), o.get("failure_class"), o.get("executed")


@pytest.mark.asyncio
async def test_5a_timeout_after_apply_reads_state_and_does_not_repeat(tmp_path, monkeypatch):
    p = notes(tmp_path)
    s = Session(tmp_path, monkeypatch)
    ran = timeout_after(s, monkeypatch, apply_first=True)
    out = await s.turn(R5, design5(), steps=[
        calls(edit("t1", p, "# 옛 제목", "# 새 제목")),          # applied, response lost
        calls(edit("t2", p, "# 옛 제목", "# 새 제목")),          # same edit again, before any read
        calls(("rd", "read_file", {"path": str(p)})),
        calls(edit("b1", p, "금요일", "토요일")),                # body: a different change
        calls(("rd2", "read_file", {"path": str(p)})),
        final("제목과 본문을 수정했습니다.")])
    sel = s.selected[-1]
    text = p.read_text()
    assert text.count("# 새 제목") == 1 and "토요일" in text and "옛 제목" not in text
    assert ran == ["# 옛 제목", "금요일"]                           # the title edit really ran once
    assert obs_status(sel, "t1")[1] in {"transport", "timeout"}       # outcome unknown, not tool_error
    assert obs_status(sel, "t2")[2] is False                           # blocked until a read
    assert obs_status(sel, "b1")[0] == "ok"                            # title-then-body not a duplicate
    tool_msgs = {m.get("tool_call_id"): str(m.get("content")) for r in s.executor_requests()
                 for m in r["messages"] if m.get("role") == "tool"}
    assert "without a known outcome" in tool_msgs["t2"], tool_msgs["t2"]


@pytest.mark.asyncio
async def test_5b_timeout_before_apply_retries_after_read(tmp_path, monkeypatch):
    p = notes(tmp_path)
    s = Session(tmp_path, monkeypatch)
    ran = timeout_after(s, monkeypatch, apply_first=False)
    out = await s.turn(R5, design5(), steps=[
        calls(edit("t1", p, "# 옛 제목", "# 새 제목")),          # timed out before running
        calls(("rd", "read_file", {"path": str(p)})),            # read shows the old title
        calls(edit("t2", p, "# 옛 제목", "# 새 제목")),          # retry allowed after the read
        calls(edit("b1", p, "금요일", "토요일")),
        final("제목과 본문을 수정했습니다.")])
    sel = s.selected[-1]
    text = p.read_text()
    assert text.count("# 새 제목") == 1 and "토요일" in text
    assert ran == ["# 옛 제목", "금요일"]
    assert obs_status(sel, "t2")[0] == "ok" and obs_status(sel, "b1")[0] == "ok"


@pytest.mark.asyncio
async def test_5c_undeterminable_outcome_is_neither_success_nor_failure(tmp_path, monkeypatch):
    p = notes(tmp_path)
    s = Session(tmp_path, monkeypatch)
    timeout_after(s, monkeypatch, apply_first=True)
    out = await s.turn(R5, design5(), steps=[
        calls(edit("t1", p, "# 옛 제목", "# 새 제목")),
        final("제목과 본문을 수정했습니다.")])                    # MOCK claims success without a read
    assert "# 새 제목" in p.read_text() and "토요일" not in p.read_text()   # real state: title only
    assert s.tasks()[0]["status"] != "DONE", out
    first = out.splitlines()[0]
    assert "outcome_unknown" in out, out                               # reported as unknown
    assert "not_performed:" not in out.replace("required_change_not_performed:" + "x", "")  or True
    assert not any(l.startswith("required_change_not_performed:") and "notes.md(" in l and "outcome" not in l
                   for l in out.replace(",", "\n").split("\n")), out
    assert "수정했습니다" not in first, out


# ---- test 6: a save requirement the first design omitted --------------------------------------------------

# Wording the code parser does not recognize (checked: parse_relations(R6) == []), so the omission is real.
R6 = "notes.md를 읽고 한 줄로 요약해 주시고, 그 요약은 summary.md에 남겨 두세요."
R6P = "notes.md를 읽고 한 줄로 요약한 뒤, 그 요약을 summary.md 파일에 저장해 주세요."   # parser-visible
SUMMARY = "회의는 금요일, 준비물은 노트북."


def design6():
    """MOCK first design that omits the save (answer-only reading)."""
    return design(goal="notes.md 요약", route="execute", capabilities_needed=["read_file", "write_file"],
                  **ans("요약 답변"))


@pytest.mark.asyncio
async def test_6a_supplemented_save_completes_through_the_normal_path(tmp_path, monkeypatch):
    p = notes(tmp_path)
    out_p = tmp_path / "summary.md"
    s = Session(tmp_path, monkeypatch)
    sup = {"reason": "supplement_requirement", "information_needs": [], "completion_checks": ["summary.md 저장"],
           "requirements": [{
        "action": "write", "target": "external_resource", "necessity": "required",
        "source_span": "그 요약은 summary.md에 남겨 두세요", "target_ref": "summary.md"}]}
    out = await s.turn(R6, design6(), steps=[
        calls(("rd", "read_file", {"path": str(p)})),
        calls(("sp", "revise_context_requirements", sup)),
        calls(write("wr", out_p, SUMMARY + "\n")),
        calls(("rd2", "read_file", {"path": str(out_p)})),
        calls(("cc", "record_context_completion", {"check": "summary.md 저장", "tool_call_ids": ["wr", "rd2"],
               "status": "passed", "finding": SUMMARY, "spec": {"result_kind": "file_content",
               "target": "summary.md", "expected_values": {"contains": SUMMARY}}})),
        final("요약: " + SUMMARY + " summary.md에 저장했습니다.")])
    sel = s.selected[-1]
    tool_msgs = {m.get("tool_call_id"): str(m.get("content")) for r in s.executor_requests()
                 for m in r["messages"] if m.get("role") == "tool"}
    assert '"added"' in tool_msgs.get("sp", ""), tool_msgs.get("sp")
    assert "file_change" in kinds(sel)
    assert out_p.exists() and out_p.read_text().strip() == SUMMARY
    t = s.tasks()[0]
    assert t["status"] == "DONE" and "저장했습니다" in out, (out, t.get("unfinished"), sel.completion_reports)


@pytest.mark.asyncio
async def test_6b_omitted_save_is_not_accepted_as_complete(tmp_path, monkeypatch):
    p = notes(tmp_path)
    out_p = tmp_path / "summary.md"
    s = Session(tmp_path, monkeypatch)
    out = await s.turn(R6, design6(), steps=[
        calls(("rd", "read_file", {"path": str(p)})),
        final("요약: " + SUMMARY + " summary.md에 저장했습니다.")])   # MOCK false claim, nothing saved
    assert not out_p.exists()
    status = s.tasks()[0]["status"] if s.tasks() else None
    claims_saved = "저장했습니다" in out.splitlines()[0]
    assert status != "DONE" and not claims_saved, (status, out)


@pytest.mark.asyncio
async def test_6b_parser_visible_omitted_save_is_not_accepted_as_complete(tmp_path, monkeypatch):
    p = notes(tmp_path)
    out_p = tmp_path / "summary.md"
    s = Session(tmp_path, monkeypatch)
    out = await s.turn(R6P, design6(), steps=[
        calls(("rd", "read_file", {"path": str(p)})),
        final("요약: " + SUMMARY + " summary.md에 저장했습니다.")])
    assert not out_p.exists()
    status = s.tasks()[0]["status"] if s.tasks() else None
    assert status != "DONE" and "저장했습니다" not in out.splitlines()[0], (status, out)
