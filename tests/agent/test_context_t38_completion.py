"""t38: common completion state reads the evidence-link state; mock follow-up after the real -42
stop (MOCK – not a replay result). Real t29 records up to -42, then a mock final answer."""
import asyncio
import json

from tests.agent.test_context_t37_museum import LOOK, museum, resolve

URL = "https://www.museum.go.kr/MUSEUM/contents/M0101000000.do"
MOCK_ANSWER = ("국립중앙박물관 상설전시 관람시간: 월·화·목·금·일 9:30–17:30, 수·토 9:30–21:00. "
               "기본 관람료: 무료(유료 기획·특별전은 별도 요금). 공식 출처: " + URL + " (확인 날짜 2026-10-28)")


def settled(monkeypatch):
    sel, tool = museum(monkeypatch)
    resolve(tool, "-41")
    out, _ = resolve(tool, "-42")                 # real records end here (after_divergence)
    assert out.startswith("Evidence observation recorded") and sel.missing == []
    return sel


def test_mock_followup_completes_from_evidence_state_without_completion_call(monkeypatch):
    sel = settled(monkeypatch)
    assert sel.completion_reports == {}           # no record_context_completion was ever called
    passed = {c: sel.check_passed(c) for c in sel.design.completion_checks}
    assert [c for c, p in passed.items() if p[2] == "evidence_state"]     # sourced checks met by links
    text, status = sel.verdict(MOCK_ANSWER)
    assert status == "completed", text
    assert text == MOCK_ANSWER


def test_answer_missing_the_official_url_or_lookup_date_is_not_complete(monkeypatch):
    sel = settled(monkeypatch)
    text, status = sel.verdict(MOCK_ANSWER.replace(URL, "공식 홈페이지"))
    assert status == "incomplete" and "answer_missing_item:official_link" in text
    sel = settled(monkeypatch)
    text, status = sel.verdict(MOCK_ANSWER.replace("2026-10-28", "오늘"))
    assert status == "incomplete" and "answer_missing_item:check_date" in text


def test_partial_need_is_not_passed_by_evidence_state(monkeypatch):
    sel, tool = museum(monkeypatch)
    resolve(tool, "-41")                          # exhibition_kind still open
    assert not any(sel.check_passed(c)[0] for c in sel.design.completion_checks)
    text, status = sel.verdict(MOCK_ANSWER)
    assert status == "incomplete"


def test_failed_source_after_validation_is_not_passed(monkeypatch):
    sel = settled(monkeypatch)
    for cid, rec in sel.observations.items():
        if cid.split("|")[0].endswith("-23"):
            rec["status"] = "error"               # the source no longer counts as successful
    text, status = sel.verdict(MOCK_ANSWER)
    assert status == "incomplete"


def _revise(sel, release):
    from nanobot.agent.context_selection import ReviseContextRequirementsTool
    r = asyncio.run(ReviseContextRequirementsTool(sel).execute(reason="release", information_needs=[],
                                                               completion_checks=[], release=release))
    return str(getattr(r, "content", r)), getattr(r, "is_error", False)


def test_model_proposed_need_released_with_reason(monkeypatch):
    sel, tool = museum(monkeypatch)
    out, err = _revise(sel, [{"need_id": "special_vs_permanent", "reason": "unnecessary"}])
    assert not err and "special_vs_permanent" not in sel.missing
    assert sel.need_states["special_vs_permanent"] == {"status": "released", "release_reason": "unnecessary",
                                                       "instruction_version": sel.instruction_version}
    assert not any(r.startswith("information_missing:special_vs_permanent") for r in sel.unmet_reasons())


def test_need_with_open_requested_item_is_not_released(monkeypatch):
    sel, tool = museum(monkeypatch)
    resolve(tool, "-41")                                   # exhibition_kind open (user asked for it)
    out, err = _revise(sel, [{"need_id": "permanent_hours_fee", "reason": "unnecessary"}])
    assert err and "exhibition_kind" in out and "permanent_hours_fee" in sel.missing
    out, err = _revise(sel, [{"need_id": "nope", "reason": "whatever"}])
    assert err
