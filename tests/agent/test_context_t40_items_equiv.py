"""t40 unit 5: equivalent museum requests, ITEMS-recognized vs unrecognized wording, same design and
the same recorded t29 lookups (replay, no network). Requirements/targets/completion must not depend on
wording; only the missing-item candidate check (auxiliary) may differ."""
from nanobot.agent.context_items import requested_items
from . import test_context_t37_museum as m

PARA = ("국립중앙박물관 공식 웹페이지를 실제로 열어서 늘 열려 있는 전시를 언제 열고 언제 닫는지, 들어갈 때 얼마 내는지 알려주세요. "
        "기간 한정 기획 쪽 전시와 섞지 말고 어디서 봤는지 주소와 언제 봤는지도 같이 적어 주세요. 예약·결제·파일 저장은 하지 마세요.")


def run(monkeypatch, req):
    sel, tool = m.museum(monkeypatch)
    sel.current_event.content = req
    outs = [m.resolve(tool, s)[0] for s in ("-41", "-42")]
    return sel, outs


def test_wording_recognition_differs():
    assert requested_items(m.REQ) and requested_items(PARA) == []


def test_requirements_targets_and_completion_are_equal(monkeypatch):
    a, oa = run(monkeypatch, m.REQ)
    b, ob = run(monkeypatch, PARA)
    assert [n.model_dump() for n in a.design.information_needs] == [n.model_dump() for n in b.design.information_needs]
    assert {k: v["status"] for k, v in a.need_states.items()} == {k: v["status"] for k, v in b.need_states.items()} \
        == {"permanent_hours_fee": "validated", "special_vs_permanent": "validated"}
    assert a.missing == b.missing == []
    assert a.need_states["permanent_hours_fee"]["tool_call_ids"] == b.need_states["permanent_hours_fee"]["tool_call_ids"]
    # before t40 the unrecognized wording never matched ("needs a target"): no longer
    assert not any("needs a target" in o for o in oa + ob)
    # remaining, reported difference: the auxiliary missing-item candidate (exhibition_kind not supplied
    # in -41) is only detectable when the wording was recognized
    assert oa[0].startswith("requested items not yet linked to a source: exhibition_kind")
    assert ob[0].startswith("Evidence observation recorded")


def test_supplied_items_are_link_checked_without_recognition(monkeypatch):
    sel, tool = m.museum(monkeypatch)
    sel.current_event.content = PARA
    _, a = m.resolve(tool, "-41")
    st = sel.need_states["permanent_hours_fee"]["items"]
    assert set(st) and all(v["status"] in {"linked", "absent"} or k == "check_date" for k, v in st.items())
