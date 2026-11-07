"""t37: museum replay stop – lookup-date check_date, pinned item links, no re-search for link
errors; calculation-only fields and multi-design comparison boundaries. Real t29 records, no network."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import nanobot.agent.context_evidence as ce
from nanobot.agent.context_items import lookup_date_item
from nanobot.agent.context_selection import (IntentDesign, ResolveContextNeedTool, SelectedContext,
                                             select_design_output)
from nanobot.agent.context_trace import ContextTrace
from nanobot.providers.base import LLMResponse, ToolCallRequest

HERE = Path(__file__).parent / "fixtures"
FIX = json.loads((HERE / "t29_design_outputs.json").read_text())
LOOK = json.loads((HERE / "t29_museum_lookups.json").read_text())
REQ = FIX["requests"]["MUSEUM"]
RO = SimpleNamespace(read_only=True)


def args(label):
    return json.loads(json.dumps(FIX["cases"][label]["tool_calls"][0]["arguments"]))


def museum(monkeypatch, tz="Asia/Seoul", upto=None):
    sel = SelectedContext(trace=ContextTrace("t37"), design=IntentDesign.model_validate(args("p2-museum-r1-t1")))
    sel.current_event = SimpleNamespace(content=REQ, sequence=5, event_id="u5")
    sel.recent, sel.artifacts = [], []
    sel.scope = SimpleNamespace(workspace_ids=["/ws"])
    sel.timezone = tz
    sel.missing = [n.id for n in sel.design.information_needs]
    tool = ResolveContextNeedTool(sel)
    for cid, c in list(LOOK["calls"].items())[:upto]:
        # a record replay uses the original lookup time
        monkeypatch.setattr(ce, "lookup_clock", lambda t=c["issued_at"]: datetime.fromtimestamp(t, timezone.utc).isoformat())
        sel.capture(SimpleNamespace(id=cid, name=c["name"], arguments=c["arguments"]), c["content"], "ok", RO)
        rec = sel.observations[cid]
        tool.observed[cid] = not rec.get("failure_class")
    return sel, tool


def resolve(tool, suffix):
    cid, a = next((k, v) for k, v in LOOK["resolve_calls"].items() if k.split("|")[0].endswith(suffix))
    return str(getattr(r := asyncio.run(tool.execute(**a)), "content", r)), a


# ---- 2. check_date = lookup date ----

def test_lookup_date_comes_from_the_successful_lookup_time(monkeypatch):
    sel, tool = museum(monkeypatch)
    out, _ = resolve(tool, "-41")
    item = sel.need_states["permanent_hours_fee"]["items"]["check_date"]
    assert item["status"] == "linked" and item["basis"] == "lookup_execution_time"
    assert item["value"] == "2026-10-28" and item["timezone"] == "Asia/Seoul"
    assert sel.observations[item["tool_call_id"]]["name"] in {"web_fetch", "web_search"}
    assert "check_date" not in out                                    # no page-body extraction asked


def test_user_time_zone_decides_the_date():
    rec = [{"name": "web_fetch", "status": "ok", "tool_call_id": "f", "executed_at": "2026-10-28T16:30:00+00:00"}]
    assert lookup_date_item({"date_kind": "lookup"}, rec, "Asia/Seoul")["value"] == "2026-10-29"
    assert lookup_date_item({"date_kind": "lookup"}, rec, "UTC")["value"] == "2026-10-28"


def test_lookup_date_unknown_stays_unknown():
    failed = [{"name": "web_fetch", "status": "ok", "failure_class": "transport", "tool_call_id": "f",
               "executed_at": "2026-10-28T01:00:00+00:00"}]
    assert lookup_date_item({"date_kind": "lookup"}, failed, "Asia/Seoul")["status"] == "missing"  # no successful lookup
    ok = [{**failed[0], "failure_class": None}]
    assert lookup_date_item({"date_kind": "lookup"}, ok, None)["status"] == "missing"              # no time zone


def test_posting_or_effective_date_is_not_the_lookup_date():
    rec = [{"name": "web_fetch", "status": "ok", "tool_call_id": "f", "executed_at": "2026-10-28T01:00:00+00:00"}]
    for kind in ("posted", "modified", "effective"):                        # page fact: needs a source
        assert lookup_date_item({"value": "2026-10-28", "date_kind": kind}, rec, "Asia/Seoul") is None
    # unlabelled and not the execution date: not recognised, so no lookup-date item is made
    assert lookup_date_item({"value": "2025-01-01"}, rec, "Asia/Seoul") is None
    assert lookup_date_item({}, rec, "Asia/Seoul") is None


def test_t38_label_is_verified_not_trusted():
    rec = [{"name": "web_fetch", "status": "ok", "tool_call_id": "f", "executed_at": "2026-10-28T01:00:00+00:00"}]
    v = lookup_date_item({"value": "2026-10-20", "date_kind": "lookup"}, rec, "Asia/Seoul")
    assert v["value"] == "2026-10-28" and v["model_value_mismatch"] is True   # code value wins, gap recorded
    u = lookup_date_item({"value": "2026년 10월 28일"}, rec, "Asia/Seoul")
    assert u["status"] == "linked" and u["verified_by"] == "value_equals_execution_date"


def test_t38_no_keyword_rule_for_lookup_date():
    import nanobot.agent.context_items as ci
    assert not hasattr(ci, "LOOKUP_DATE")


# ---- 3. pinned material, per-item links, no re-search ----

def test_linked_items_stay_and_only_missing_remain(monkeypatch):
    sel, tool = museum(monkeypatch)
    out41, _ = resolve(tool, "-41")
    assert out41.startswith("requested items not yet linked to a source: exhibition_kind")   # as recorded
    st = sel.need_states["permanent_hours_fee"]
    assert {k for k, v in st["items"].items() if v["status"] == "linked"} >= {"hours", "fee", "official_link", "check_date"}
    assert sel.judgment_error is None or sel.judgment_error["material_calls"]
    out42, _ = resolve(tool, "-42")
    assert out42.startswith("Evidence observation recorded")                                  # t36: check_date demanded
    # exhibition_kind linked in -42 (same source) satisfies the open item of the other need
    assert sel.need_states["permanent_hours_fee"]["status"] == "validated"
    assert sel.need_states["permanent_hours_fee"]["items"]["exhibition_kind"]["shared"] is True
    assert sel.missing == []


def test_unlinked_item_is_not_filled_from_nothing(monkeypatch):
    sel, tool = museum(monkeypatch)
    _, a = resolve(tool, "-42")
    a = json.loads(json.dumps(a)); a["need_id"] = "permanent_hours_fee"; a["items"] = {}
    out = str(getattr(r := asyncio.run(tool.execute(**a)), "content", r))
    assert out.startswith("requested items not yet linked")                                  # hours/fee never linked
    assert sel.need_states["permanent_hours_fee"]["status"] == "partial"


def test_item_with_failed_source_is_not_kept(monkeypatch):
    sel, tool = museum(monkeypatch)
    resolve(tool, "-41")
    for cid in list(tool.observed):
        if cid.split("|")[0].endswith("-23"):
            tool.observed[cid] = False                     # the hours source is no longer valid
    resolve(tool, "-42")
    assert sel.need_states["permanent_hours_fee"]["status"] == "partial"


def test_link_error_does_not_trigger_a_search(monkeypatch):
    sel, tool = museum(monkeypatch)
    _, a = resolve(tool, "-41")
    bad = json.loads(json.dumps(a)); bad["items"]["exhibition_kind"] = {"value": "상설/특별 구분", "tool_call_id": "no-such"}
    asyncio.run(tool.execute(**bad))
    assert sel.judgment_error and sel.judgment_error["material_calls"]                         # material in hand
    url = LOOK["calls"][next(iter(LOOK["calls"]))]["arguments"]
    assert sel.repeated_lookup("web_search", url) is not None or sel.judgment_error           # fix the record first


# ---- 4a. calculation-only fields never required elsewhere ----

@pytest.mark.parametrize("label", ["p2-museum-r1-t1", "p2-perm-r1-t1", "p2-file-r1-t1"])
def test_non_computed_designs_do_not_need_computed_fields(label):
    a = args(label)
    assert not any(s.get("result_kind") == "computed" for s in a.get("completion_specs") or [])
    sel = SelectedContext(trace=ContextTrace("t37"), design=IntentDesign.model_validate(a))
    sel.current_event = SimpleNamespace(content="x", sequence=5, event_id="u5")
    sel.recent, sel.artifacts, sel.scope = [], [], SimpleNamespace(workspace_ids=["/ws"])
    sel.answer_text = a.get("answer") or "상설전시 관람시간 9:30-17:30, 관람료 무료 (답변 본문)"
    assert sel.answer_check_failures() == []                                                  # allow
    a2 = args("p2-golf-r1-t2")
    for s in a2["completion_specs"]:
        if s.get("result_kind") == "computed":
            s["expected_values"]["values"] = {k: None for k in s["expected_values"]["values"]}
    s2 = SelectedContext(trace=ContextTrace("t37"), design=IntentDesign.model_validate(a2))
    s2.current_event = SimpleNamespace(content="x", sequence=5, event_id="u5")
    s2.recent, s2.artifacts, s2.scope = [], [], SimpleNamespace(workspace_ids=["/ws"])
    s2.answer_text = a2["answer"]
    assert s2.answer_check_failures() != []                                                   # computed still checked


# ---- 4b. multi-design comparison keeps real differences ----

def _two(a, b):
    return select_design_output(LLMResponse(content=None, tool_calls=[
        ToolCallRequest(id="0", name="submit_context_design", arguments=a),
        ToolCallRequest(id="1", name="submit_context_design", arguments=b)]))[2]


def _first_computed(a):
    return next(s for s in a["completion_specs"] if s.get("result_kind") == "computed")


@pytest.mark.parametrize("change", ["number", "unit", "target", "prohibition", "condition"])
def test_multi_design_differences_are_conflicts(change):
    base = args("p2-golf-r1-t2")
    other = json.loads(json.dumps(base))
    if change == "number":
        ev = _first_computed(other)["expected_values"]; k = next(iter(ev["values"])); ev["values"][k] += 1
    elif change == "unit":
        _first_computed(other)["expected_values"]["unit"] = "시간"
    elif change == "target":
        other["obligations"]["relations"] = [{"action": "write", "target": "external_resource", "necessity": "required",
                                              "source_span": "x", "target_ref": "/ws/a.md"}]
        base = json.loads(json.dumps(base)); base["obligations"]["relations"] = [dict(other["obligations"]["relations"][0], target_ref="/ws/b.md")]
    elif change == "prohibition":
        r = {"action": "write", "target": "external_resource", "necessity": "required", "source_span": "x", "target_ref": "/ws/a.md"}
        base["obligations"]["relations"] = [r]; other["obligations"]["relations"] = [dict(r, necessity="forbidden")]
    else:
        r = {"action": "lookup", "target": "web", "necessity": "conditional", "source_span": "x", "condition": "필요하면"}
        base["obligations"]["relations"] = [r]; other["obligations"]["relations"] = [dict(r, condition="항상")]
    assert _two(base, other) == ["design_conflicting_outputs"]                                # prevent
    assert _two(base, dict(reversed(list(base.items())))) == []                               # allow: same design
