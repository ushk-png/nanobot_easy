"""Per-item matching of looked-up facts against what the user actually asked for (t26 h6).

A fact-finding request ("관람 시간과 휴관 여부") names items. A need is acquired only when every
requested item is linked to a successful observation; finding one item never completes the rest.
Gaps are classified, never "fixed" by more searching."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

# t39: auxiliary only - candidate extraction and missing-item checks. Non-recognition never deletes a
# requirement (needs are the design's); a matched word never creates or blocks an obligation.
ITEMS = {
    "hours": r"관람\s?시간|운영\s?시간|개관\s?시간|영업\s?시간|몇\s?시까지|opening\s+hours|\bhours\b",
    "closure": r"휴관|휴무|쉬는\s?날|문\s?(?:여|열)|closed|closure",
    "fee": r"관람료|입장료|요금|가격|무료|\bfee\b|price|ticket",
    "exhibition_kind": r"상설|특별\s?전|기획\s?전|permanent|special\s+exhibition",
    "official_link": r"공식\s?(?:링크|사이트|홈페이지|출처)|official\s+(?:link|site|source)",
    "check_date": r"확인\s?(?:날짜|일자|시점)|기준\s?(?:일|날짜)|as\s+of",
}
# t38: the kind of a check_date is labelled by the model (date_kind) and verified in code; no
# keyword rule over the request decides it. "lookup" = the date the lookup was done (an execution
# fact); posted / modified / effective dates are page facts and keep the normal source link.
PAGE_DATE_KINDS = {"posted", "modified", "effective"}


def lookup_execution_date(records: list[dict], tz: str | None) -> dict:
    """The latest successful web lookup's execution date in the user's time zone; status missing
    (cause recorded) when no successful lookup time or no time zone is known – never guessed."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    done = [r for r in records if r.get("name") in {"web_fetch", "web_search"} and r.get("status") == "ok"
            and not r.get("failure_class") and r.get("executed_at")]
    if not done or not tz:
        return {"status": "missing", "basis": "lookup_execution_time",
                "unknown": "time_zone" if done else "successful_lookup_time"}
    last = max(done, key=lambda r: r["executed_at"])
    when = datetime.fromisoformat(last["executed_at"]).astimezone(ZoneInfo(tz))
    return {"status": "linked", "value": when.date().isoformat(), "tool_call_id": last["tool_call_id"],
            "basis": "lookup_execution_time", "timezone": tz, "executed_at": last["executed_at"]}


def _iso_date(text: str) -> str | None:
    m = re.search(r"(\d{4})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})", text or "")
    return "%04d-%02d-%02d" % tuple(int(x) for x in m.groups()) if m else None


def lookup_date_item(given: dict | None, records: list[dict], tz: str | None) -> dict | None:
    """check_date as the lookup date, decided by the model's label and verified in code.

    - date_kind "lookup" (or a kept item already based on the execution time): the value is the
      execution date; a different model value is recorded, never used.
    - date_kind posted/modified/effective: None (page fact, needs its own source link).
    - unlabelled: a lookup date only when the given value equals the execution date (verified);
      otherwise None – non-recognition creates no item and no obligation.
    """
    given = given if isinstance(given, dict) else {}
    kind = given.get("date_kind") or ("lookup" if given.get("basis") == "lookup_execution_time" else None)
    if kind in PAGE_DATE_KINDS:
        return None
    looked = lookup_execution_date(records, tz)
    model_value = _iso_date(str(given.get("model_value") or given.get("value") or ""))
    if kind != "lookup":
        if not model_value or looked["status"] != "linked" or model_value != looked["value"]:
            return None
        looked["verified_by"] = "value_equals_execution_date"
    else:
        looked["verified_by"] = "model_label"
    if given.get("value") and given.get("basis") != "lookup_execution_time":
        looked["model_value"] = str(given["value"])[:80]
        if model_value and looked.get("value") and model_value != looked["value"]:
            looked["model_value_mismatch"] = True
    return looked


def requested_items(request: str) -> list[str]:
    return [k for k, pat in ITEMS.items() if re.search(pat, request or "", re.I)]


def need_items(request: str, need, needs: list) -> list[str]:
    """The requested items one need answers (t35).

    A need is the designer's split of the request: it owns the requested items its own question or
    acceptance names. A requested item no need names stays a duty of every need, so splitting a
    request never drops an item. Same item patterns as the request; no new wording rules."""
    asked = requested_items(request)

    def named(n) -> set[str]:
        return set(requested_items(" ".join(filter(None, [getattr(n, "question", ""), getattr(n, "acceptance", "")]))))

    owned = named(need) & set(asked)
    unowned = set(asked) - set().union(*(named(n) for n in needs)) if needs else set(asked)
    return [k for k in asked if k in owned or k in unowned]


def item_coverage(request: str, items: dict | None, ok_ids: set[str],
                  wanted: list[str] | None = None) -> dict[str, dict]:
    """Status per requested item: linked (value + successful source), unlinked (value without a valid
    source), absent (stated as not published, with a source), missing (not reported).
    ``wanted`` limits the items to those one need owns (``need_items``)."""
    out = {}
    for item in (requested_items(request) if wanted is None else wanted):
        got = (items or {}).get(item) or {}
        value, cid = str(got.get("value") or "").strip(), got.get("tool_call_id")
        if not value:
            out[item] = {"status": "missing"}
        elif cid not in ok_ids:
            out[item] = {"status": "unlinked", "value": value}
        elif got.get("absent"):
            out[item] = {"status": "absent", "tool_call_id": cid}
        else:
            out[item] = {"status": "linked", "value": value, "tool_call_id": cid}
    return out


def classify_gap(coverage: dict, finding: str, completion_checks: list[str], request: str,
                 fetch_repeats: int) -> list[str]:
    """Why an item-level need is not met: absent / not_extracted / not_linked / unrequested / repeated."""
    causes = []
    for item, state in coverage.items():
        if state["status"] == "absent":
            causes.append("absent:" + item)
        elif state["status"] == "unlinked" or (state["status"] == "missing"
                                               and re.search(ITEMS[item], finding or "", re.I)):
            causes.append("not_linked:" + item)
        elif state["status"] == "missing":
            causes.append("not_extracted:" + item)
    asked = set(requested_items(request))
    for check in completion_checks:
        extra = set(requested_items(check)) - asked
        if extra:
            causes.append("unrequested_condition:" + ",".join(sorted(extra)))
    if fetch_repeats:
        causes.append("repeated_lookup:" + str(fetch_repeats))
    return causes


def normal_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", parts.query, ""))
