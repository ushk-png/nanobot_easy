"""Opt-in flat time allocation: one source projection, result and completion bundle.

Only user originals enter the reducer. Model answers and model check lists are never
sources of requirements. Numeric prose is never an input to result verification.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import uuid
from fractions import Fraction
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.memory.models import RawEvent


class AllocationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    value: str


class AllocationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[AllocationItem]
    total: str
    remaining: str


class AllocationExplanation(BaseModel):
    """Closed qualitative templates: no unverified prose after the checked numeric block."""
    model_config = ConfigDict(extra="forbid")
    item_id: str
    purpose: Literal["consistency", "accuracy", "control"]
    method: Literal["repeat_and_review", "focus_on_form", "compare_attempts"]


PURPOSES = {"consistency": "동작의 일관성을 높입니다.", "accuracy": "목표에 대한 정확도를 높입니다.",
            "control": "동작을 안정적으로 조절하는 감각을 익힙니다."}
METHODS = {"repeat_and_review": "동작을 반복하고 결과를 살펴 조정합니다.",
           "focus_on_form": "자세와 동작의 흐름에 집중해 연습합니다.",
           "compare_attempts": "시도마다 결과를 비교하며 차이를 확인합니다."}
# A duration grammar, including mixed minutes/seconds, shared by all item labels.
DURATION = re.compile(r"(?P<n>-?\d+(?:\.\d+|/\d+)?)\s*(?P<u>분|초|시간|minutes?|seconds?)"
                      r"(?:\s*(?P<s>\d+(?:\.\d+)?)\s*초)?")
TIME_REQUEST = re.compile(r"(?:분|초|시간|minutes?|seconds?).*(?:배분|계획|연습|일정|allocate|plan)|"
                          r"(?:배분|계획|연습|일정|allocate|plan).*(?:분|초|시간|minutes?|seconds?)", re.S)
TOTAL = re.compile(r"(?:총(?:량|\s*시간)?|합계|전체(?:\s*시간)?|상한|total)\s*$", re.I)
UPPER = re.compile(r"이내|이하|최대|상한|at most|within", re.I)
REMAINDER = re.compile(r"(?:남은|남는)\s*시간(?:을|은|는)?|나머지(?:\s*시간)?(?:을|은|는)?")
EQUAL = re.compile(r"균등|똑같|동일|같게|equally", re.I)
UNALLOCATED = re.compile(r"여유|미배분|잔여|slack|unallocated", re.I)
EXCLUDE = re.compile(r"([가-힣A-Za-z]+?)(?:은|는|을|를)?\s*(?:제외|빼고|빼\s*줘)")
RELEASE = re.compile(r"고정(?:만|을|은|는)?\s*(?:풀|해제)|release|unfix", re.I)
UNSUPPORTED = re.compile(r"그중|그 중|동시에|병렬|중첩|동안|하면서|parallel|nested|반올림|정수\s*단위")
CONTENT = re.compile(r"목적|방법|설명|purpose|method", re.I)
PARTICLE = re.compile(r"(?:으로|에서|에는|에게|은|는|을|를|에|과|와|도)$")


def number(value: Any) -> Fraction:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("duration_must_be_exact_string")
    v = Fraction(value)
    if v < 0 or v.numerator.bit_length() > 256 or v.denominator.bit_length() > 256:
        raise ValueError("invalid_duration")
    return v


def duration_value(match) -> Fraction | None:
    try:
        value = number(match["n"]) * (60 if match["u"] == "시간" else Fraction(1, 60)
                                     if match["u"] in {"초", "second", "seconds"} else 1)
        return value + (number(match["s"]) / 60 if match["s"] else 0)
    except (ValueError, ZeroDivisionError):
        return None


def fmt(value: Fraction) -> str:
    d = value.denominator
    for factor in (2, 5):
        while d % factor == 0:
            d //= factor
    if d != 1:
        return str(value)
    # Exact finite decimal, with no Decimal context precision limit.
    places, scale = 0, 1
    while scale % value.denominator:
        places, scale = places + 1, scale * 10
    n = value.numerator * (scale // value.denominator)
    if not places:
        return str(n)
    digits = str(n).zfill(places + 1)
    return (digits[:-places] + "." + digits[-places:]).rstrip("0").rstrip(".")


def enabled(config: Any) -> bool:
    return bool(getattr(config, "time_allocation_contract_enabled", False))


def applies(request: str, state: dict | None = None) -> bool:
    return bool((state or {}).get("effective_requirements") or TIME_REQUEST.search(request))


def _id() -> str:
    return uuid.uuid4().hex


def _source(event: dict, span: str) -> dict:
    if span not in event["text"]:
        from nanobot.agent.context_relations import _blank_quotes
        start = _blank_quotes(event["text"]).find(span)
        if start < 0:
            raise ValueError("source_span_not_in_original")
        span = event["text"][start:start + len(span)]
    return {"event_id": event["event_id"], "source_span": span}


def _label(prefix: str) -> str | None:
    prefix = re.sub(r"(?:고정|fixed|[:=])\s*$", "", prefix).rstrip()
    m = re.search(r"([가-힣A-Za-z]+)$", prefix)
    if not m:
        return None
    label = PARTICLE.sub("", m[1])
    return label if label not in {"최소", "각", "최대", "합", "나머지"} else None


def _names(text: str) -> list[str]:
    text = re.split(r"균등|똑같|동일|같게|배분|고정|연습", text)[0]
    return [PARTICLE.sub("", n.strip()) for n in re.split(r"[,/]|(?:과|와|및)\s*|\s+", text)
            if n.strip() and PARTICLE.sub("", n.strip()) not in {"시간", "나머지", "남은", "항목", "모두"}]


def _remainder_names(text: str) -> list[str]:
    """Require a recipient phrase or a coordinated list, not arbitrary modifier words."""
    atom = r"[가-힣A-Za-z]+"
    coordinated = atom + r"(?:\s*(?:[,/]|과|와|및)\s*" + atom + r")*"
    match = re.match(r"\s*(" + coordinated + r")\s*(?:에게|에는|에)"
                     r"(?=\s|균등|똑같|동일|같게|최소|배분|$)", text)
    if match:
        return _names(match[1])
    head = re.split(r"균등|똑같|동일|같게|배분|고정|연습", text)[0].strip()
    return _names(head) if re.fullmatch(atom + r"(?:\s*(?:[,/]|과|와|및)\s*" + atom + r")+", head) else []


def reduce_requirements(state: dict, originals: list[dict], *, response_id: str | None = None) -> dict:
    """Apply only new, task-bound user events; previous rows survive model omissions."""
    snap = copy.deepcopy(state.get("effective_requirements") or {
        "instruction_version": state.get("instruction_version", 1), "requirements": [],
        "unresolved": [], "through_sequence": 0})
    rows = snap["requirements"]
    history = state.setdefault("supersessions", [])

    def unresolved(code, event, affects=()):
        entry = {"id": _id(), "code": code, "affects": list(affects),
                 "sources": [_source(event, event["text"])], "question_id": None}
        snap["unresolved"].append(entry)

    def put(kind, target, values, event, span, operation="replace"):
        old = next((r for r in rows if r["kind"] == kind and r["target"] == target), None)
        if old and old["expected_values"] == values:
            return old
        source = _source(event, span)
        if old:
            history.append({"requirement_id": old["id"], "operation": operation,
                            "event_id": event["event_id"], "instruction_version": state["instruction_version"],
                            "old": copy.deepcopy(old["expected_values"]), "new": values,
                            "sources": [source]})
            old.update(expected_values=values, sources=[source])
            return old
        rid = _id()
        row = {"id": rid, "kind": kind, "target": rid if kind == "item" else target,
               "expected_values": values, "sources": [source]}
        rows.append(row)
        return row

    def item(label, mode, value, event, span, operation="replace"):
        found = next((r for r in rows if r["kind"] == "item" and r["expected_values"]["label"] == label), None)
        return put("item", found["target"] if found else None,
                   {"label": label, "mode": mode, "value": value}, event, span, operation)

    for ev in sorted(originals, key=lambda e: e["sequence"]):
        if ev["sequence"] <= snap["through_sequence"]:
            continue
        from nanobot.agent.context_relations import _blank_quotes
        text = _blank_quotes(ev["text"])
        snap["through_sequence"] = ev["sequence"]
        masked = text
        for record in state.get("work_records", []):
            response = (record.get("allocation") or {}).get("response_id") or record.get("response_id")
            if response:
                masked = masked.replace(response, " ")
        masked = DURATION.sub(" ", masked)
        bare_choice = re.fullmatch(r"\s*(?:선택\s*)?[12](?:번)?\s*[.!]?", text)
        if re.search(r"\d", masked) and not bare_choice:
            unresolved("interpretation_unsupported", ev)
        if UNSUPPORTED.search(text):
            unresolved("interpretation_unsupported", ev)
            continue
        if re.search(r"전체\s*조건.*(?:취소|새\s*계획)", text):
            for row in list(rows):
                history.append({"requirement_id": row["id"], "operation": "release",
                                "event_id": ev["event_id"], "instruction_version": state["instruction_version"],
                                "old": row["expected_values"], "new": None, "sources": [_source(ev, text)]})
            rows.clear()
            snap["unresolved"].clear()
        # A later explicit instruction resolves the context error of an unbound choice.
        # That rejected choice never created a numeric requirement in the first place.
        if DURATION.search(text) or RELEASE.search(text):
            resolved = [u for u in snap["unresolved"] if u["code"] == "question_binding_stale"]
            for row in resolved:
                history.append({"requirement_id": row["id"], "operation": "release", "event_id": ev["event_id"],
                    "instruction_version": state["instruction_version"], "old": row, "new": None,
                    "sources": [_source(ev, text)], "reason": "new_explicit_instruction_replaces_unbound_choice"})
            snap["unresolved"] = [u for u in snap["unresolved"] if u not in resolved]
        # A bound numeric choice changes precisely the delta recorded with that question.
        choice = re.fullmatch(r"\s*(?:선택\s*)?([12])(?:번)?\s*[.!]?", text)
        if choice:
            questions = [q for q in state.get("quantity_questions", []) if q.get("allocation")
                         and not q.get("resolved_event_id")
                         and q["instruction_version"] == state["instruction_version"] - 1
                         and q.get("response_id") == response_id]
            if len(questions) != 1:
                unresolved("question_binding_stale", ev)
                continue
            q = questions[0]
            if choice[1] != "1":
                unresolved("user_info_missing", ev, q["affects"])
                continue
            total = next((r for r in rows if r["kind"] == "total"), None)
            put("total", total["target"], {"value": q["choices"][0]["value"], "unit": "minute"}, ev, text)
            q["resolved_event_id"] = ev["event_id"]
            state.setdefault("decisions", []).append({"event_id": ev["event_id"], "question_id": q["question_id"],
                "task_id": state["task_id"], "instruction_version": state["instruction_version"],
                "affects": q["affects"], "operation": "replace"})
            continue
        previous_total = next((r for r in rows if r["kind"] == "total"), None)
        durations = list(DURATION.finditer(text))
        changed = False
        numeric_labels = set()
        total_count = 0
        for m in durations:
            start = max(text.rfind(c, 0, m.start()) for c in (",", "\n", ".", "!", "?", ";")) + 1
            # Decimal dot is not a clause delimiter: only examine the prefix before the duration.
            prefix = text[start:m.start()]
            value = duration_value(m)
            if value is None:
                unresolved("interpretation_unsupported", ev)
                continue
            total_match = TOTAL.search(prefix)
            # Total role can be marked before the duration or by a following bound.
            # An item label before a bound still denotes that item's limit, not the total.
            leading_bound = UPPER.search(prefix) and _label(prefix) is None
            following_bound = UPPER.match(text[m.end():].lstrip()) and _label(prefix) is None
            if total_match or leading_bound or following_bound:
                total_count += 1
                if total_count > 1:
                    unresolved("interpretation_unsupported", ev)
                put("total", "total", {"value": fmt(value), "unit": "minute"}, ev, text[start:m.end()])
                changed = True
            elif re.search(r"최소\s*$", prefix):
                # Applied below to the named participants, never turned into an item.
                continue
            else:
                label = _label(prefix)
                if not label:
                    unresolved("interpretation_unsupported", ev)
                    continue
                if label in numeric_labels:
                    unresolved("ambiguous_item_identity", ev)
                numeric_labels.add(label)
                item(label, "fixed", fmt(value), ev, text[start:m.end()])
                changed = True
        for ex in EXCLUDE.finditer(text):
            item(ex[1], "excluded", None, ev, ex[0], "release")
            changed = True
        if RELEASE.search(text):
            before = text[:RELEASE.search(text).start()]
            labels = _names(before)
            found = [r for r in rows if r["kind"] == "item" and r["expected_values"]["label"] in labels]
            if not found:
                unresolved("interpretation_unsupported", ev)
            for row in found:
                vals = row["expected_values"]
                item(vals["label"], "adjustable", None, ev, text, "release")
                changed = True
        rem = REMAINDER.search(text)
        unallocated = bool(rem and UNALLOCATED.search(text[rem.end():])
                           and not re.search(r"배분|할당|allocate", text[rem.end():], re.I))
        if unallocated:
            total = next((r for r in rows if r["kind"] == "total"), None)
            put("relation", "allocation", {"kind": "upper_bound", "total_id": total["id"] if total else None,
                "item_ids": [r["id"] for r in rows if r["kind"] == "item" and r["expected_values"]["mode"] != "excluded"],
                "value": None}, ev, text[rem.start():])
            changed = True
        if rem and not unallocated:
            names = _remainder_names(text[rem.end():])
            if not names and not any(r["kind"] == "relation" and r["target"] == "allocation" for r in rows):
                unresolved("user_info_missing", ev)
            for label in names:
                found = next((r for r in rows if r["kind"] == "item" and r["expected_values"]["label"] == label), None)
                if not found:
                    item(label, "adjustable", None, ev, text[rem.start():])
            participants = [r["id"] for r in rows if r["kind"] == "item" and r["expected_values"]["label"] in names]
            total = next((r for r in rows if r["kind"] == "total"), None)
            # A reference to the remainder alone does not replace its bound participants.
            if names:
                put("relation", "allocation", {"kind": "remainder", "total_id": total["id"] if total else None,
                    "item_ids": participants, "value": None}, ev, text[rem.start():])
            if names and EQUAL.search(text):
                put("relation", "equal", {"kind": "equal", "total_id": None,
                    "item_ids": participants, "value": None}, ev, text[rem.start():])
            minimum = next((m for m in durations if re.search(r"최소\s*$", text[:m.start()])), None)
            if names and minimum and duration_value(minimum) is not None:
                put("relation", "min_each", {"kind": "min_each", "total_id": None,
                    "item_ids": participants, "value": fmt(duration_value(minimum))},
                    ev, text[rem.start():])
            changed = True
        elif not unallocated and (UPPER.search(text) or (previous_total is None and durations)):
            total = next((r for r in rows if r["kind"] == "total"), None)
            if total and (UPPER.search(text) or re.search(r"배분|계획|연습|짜|합계|되도록", text)):
                put("relation", "allocation", {"kind": "upper_bound" if UPPER.search(text) else "composition",
                    "total_id": total["id"], "item_ids": [r["id"] for r in rows if r["kind"] == "item"
                        and r["expected_values"]["mode"] != "excluded"], "value": None}, ev, text)
        if not rem:
            for clause in re.split(r"[.!?。]\s*|\n", text):
                minimum = next((m for m in DURATION.finditer(clause) if re.search(r"최소\s*$", clause[:m.start()])), None)
                if not minimum and not EQUAL.search(clause):
                    continue
                named = [r["id"] for r in rows if r["kind"] == "item" and r["expected_values"]["mode"] != "excluded"
                         and r["expected_values"]["label"] in clause]
                if not named and re.search(r"각|모두|전체\s*항목", clause):
                    named = [r["id"] for r in rows if r["kind"] == "item" and r["expected_values"]["mode"] != "excluded"]
                if not named:
                    unresolved("interpretation_unsupported", ev)
                    continue
                if minimum and duration_value(minimum) is not None:
                    put("relation", "min_each", {"kind": "min_each", "total_id": None, "item_ids": named,
                        "value": fmt(duration_value(minimum))}, ev, clause)
                if EQUAL.search(clause):
                    put("relation", "equal", {"kind": "equal", "total_id": None, "item_ids": named, "value": None}, ev, clause)
                changed = True
        if CONTENT.search(text):
            put("answer_content", "answer", {"text": text}, ev, text)
        if not changed and not CONTENT.search(text) and not re.search(r"유지|그대로|계속|같은|검색|인터넷", text):
            unresolved("interpretation_unsupported", ev)
        # A free response to the current question is accepted only when it changes just its total.
        total = next((r for r in rows if r["kind"] == "total"), None)
        for q in state.get("quantity_questions", []):
            if (q.get("allocation") and not q.get("resolved_event_id") and total
                    and q["instruction_version"] == state["instruction_version"] - 1
                    and total["sources"][0]["event_id"] == ev["event_id"]):
                q["resolved_event_id"] = ev["event_id"]
                state.setdefault("decisions", []).append({"event_id": ev["event_id"], "question_id": q["question_id"],
                    "task_id": state["task_id"], "instruction_version": state["instruction_version"],
                    "affects": q["affects"], "operation": "replace"})
    active = {r["id"] for r in rows if r["kind"] == "item" and r["expected_values"]["mode"] != "excluded"}
    for row in list(rows):
        if row["kind"] != "relation":
            continue
        v = row["expected_values"]
        v["item_ids"] = [i for i in v["item_ids"] if i in active]
        if v["kind"] in {"composition", "upper_bound"}:
            v["item_ids"] = [r["id"] for r in rows if r["id"] in active]
        if not v["item_ids"]:
            rows.remove(row)
    snap["instruction_version"] = state["instruction_version"]
    state["effective_requirements"] = snap
    return snap


def derive_checks(snapshot: dict) -> list[dict]:
    """The required set is entirely determined by the effective user projection."""
    return [{"id": r["id"] + ":" + r["kind"], "requirement_ids": [r["id"]],
             "instruction_version": snapshot["instruction_version"], "kind": r["kind"],
             "expected": copy.deepcopy(r["expected_values"])} for r in snapshot["requirements"]]


def analyze(snapshot: dict) -> tuple[str, list[str]]:
    rows = snapshot["requirements"]
    if snapshot["unresolved"]:
        return "insufficient_info", list(dict.fromkeys(x["code"] for x in snapshot["unresolved"]))
    total = next((r for r in rows if r["kind"] == "total"), None)
    items = [r for r in rows if r["kind"] == "item" and r["expected_values"]["mode"] != "excluded"]
    main = next((r for r in rows if r["kind"] == "relation" and r["target"] == "allocation"), None)
    if not total or not items or not main:
        return "insufficient_info", ["user_info_missing"]
    fixed = {r["id"]: number(r["expected_values"]["value"]) for r in items if r["expected_values"]["mode"] == "fixed"}
    target = number(total["expected_values"]["value"])
    # Equalities form components; minima combine with max, rather than being counted twice.
    parent = {r["id"]: r["id"] for r in items}
    def leader(i):
        while parent[i] != i:
            i = parent[i]
        return i
    relations = [r["expected_values"] for r in rows if r["kind"] == "relation"]
    for v in relations:
        if v["kind"] == "equal" and v["item_ids"]:
            for i in v["item_ids"][1:]:
                parent[leader(i)] = leader(v["item_ids"][0])
    groups = {}
    for i in parent:
        groups.setdefault(leader(i), []).append(i)
    minimum, determined = Fraction(), True
    for members in groups.values():
        stated = {fixed[i] for i in members if i in fixed}
        if len(stated) > 1:
            return "requirement_conflict", ["unequal_fixed_values"]
        bound = max([number(v["value"]) for v in relations if v["kind"] == "min_each"
                     and set(v["item_ids"]) & set(members)] or [Fraction()])
        if stated and next(iter(stated)) < bound:
            return "requirement_conflict", ["fixed_below_minimum"]
        determined = determined and bool(stated)
        minimum += (next(iter(stated)) if stated else bound) * len(members)
    if main["expected_values"]["kind"] == "remainder":
        recipients = set(main["expected_values"]["item_ids"])
        if any(r["id"] not in recipients and r["id"] not in fixed for r in items):
            return "insufficient_info", ["unallocated_remainder_base"]
    if minimum > target or (determined and main["expected_values"]["kind"] != "upper_bound" and minimum != target):
        return "requirement_conflict", ["fixed_total_conflict"]
    return "pass", []


def automatic_result(snapshot: dict) -> dict | None:
    rows = snapshot["requirements"]
    total = next((r for r in rows if r["kind"] == "total"), None)
    if not total or analyze(snapshot)[0] != "pass":
        return None
    items = [r for r in rows if r["kind"] == "item" and r["expected_values"]["mode"] != "excluded"]
    fixed = {r["id"]: number(r["expected_values"]["value"]) for r in items if r["expected_values"]["mode"] == "fixed"}
    rest_ids = [r["id"] for r in items if r["id"] not in fixed]
    target = number(total["expected_values"]["value"])
    equal = next((r["expected_values"] for r in rows if r["kind"] == "relation" and r["target"] == "equal"), None)
    if rest_ids:
        if not equal or set(equal["item_ids"]) != set(rest_ids):
            return None
        for rid in rest_ids:
            fixed[rid] = (target - sum(fixed.values(), Fraction())) / len(rest_ids) if rid == rest_ids[0] else fixed[rest_ids[0]]
    return {"items": [{"item_id": r["id"], "value": fmt(fixed[r["id"]])} for r in items],
            "total": fmt(target), "remaining": fmt(target - sum(fixed.values(), Fraction()))}


def validate_result(snapshot: dict, candidate: dict | None, notes: list[dict]) -> tuple[str, list[dict], dict | None]:
    decision, reasons = analyze(snapshot)
    checks = derive_checks(snapshot)
    if decision != "pass":
        return decision, [{**c, "status": decision, "observed": reasons} for c in checks], None
    try:
        result = AllocationResult.model_validate(candidate).model_dump()
        values = {i["item_id"]: number(i["value"]) for i in result["items"]}
        rows = snapshot["requirements"]
        if len(values) != len(result["items"]):
            raise ValueError("item_identity_mismatch")
        # Only explicit current exclusions at exactly zero are absent from the accepted result.
        # Validate schema, values and uniqueness first; never discard unknown or invalid entries.
        excluded_zero = {r["id"] for r in rows if r["kind"] == "item"
                         and r["expected_values"]["mode"] == "excluded" and values.get(r["id"]) == 0}
        result["items"] = [i for i in result["items"] if i["item_id"] not in excluded_zero]
        values = {key: value for key, value in values.items() if key not in excluded_zero}
        items = [r for r in rows if r["kind"] == "item" and r["expected_values"]["mode"] != "excluded"]
        if set(values) != {r["id"] for r in items}:
            raise ValueError("item_identity_mismatch")
        total = number(result["total"])
        remaining = number(result["remaining"])
        if total - sum(values.values(), Fraction()) != remaining:
            raise ValueError("remaining_mismatch")
        note_map = {}
        for note in notes:
            n = AllocationExplanation.model_validate(note).model_dump()
            if n["item_id"] not in values or n["item_id"] in note_map:
                raise ValueError("explanation_identity_mismatch")
            note_map[n["item_id"]] = n
        for check in checks:
            v, kind = check["expected"], check["kind"]
            ok, observed = True, None
            if kind == "item":
                row_id = check["requirement_ids"][0]
                observed = fmt(values[row_id]) if row_id in values else None
                ok = row_id not in values if v["mode"] == "excluded" else v["mode"] != "fixed" or values[row_id] == number(v["value"])
            elif kind == "total":
                observed, ok = fmt(total), total == number(v["value"])
            elif kind == "relation":
                subset = [values[i] for i in v["item_ids"]]
                observed = [fmt(x) for x in subset]
                if v["kind"] in {"composition", "remainder"}:
                    ok = sum(values.values(), Fraction()) == total and remaining == 0
                elif v["kind"] == "upper_bound":
                    ok = sum(values.values(), Fraction()) <= total
                elif v["kind"] == "equal":
                    ok = len(set(subset)) <= 1
                elif v["kind"] == "min_each":
                    ok = all(x >= number(v["value"]) for x in subset)
            elif kind == "answer_content":
                # Only purpose/method templates are supported in this first contract.
                observed = notes
                ok = set(note_map) == set(values) and bool(CONTENT.search(v["text"]))
            check.update(status="pass" if ok else "result_error", observed=observed)
        for i in result["items"]:
            i["value"] = fmt(values[i["item_id"]])
        result.update(total=fmt(total), remaining=fmt(remaining))
        return ("pass" if all(c["status"] == "pass" for c in checks) else "result_error"), checks, result
    except (ValueError, TypeError, ZeroDivisionError, KeyError) as exc:
        return "result_error", [{**c, "status": "result_error", "observed": str(exc)} for c in checks], None


def render(snapshot: dict, result: dict, notes: list[dict]) -> str:
    values = {i["item_id"]: i["value"] for i in result["items"]}
    rows = snapshot["requirements"]
    items = [r for r in rows if r["kind"] == "item" and r["id"] in values]
    lines = [r["expected_values"]["label"] + ": " + values[r["id"]] + "분" for r in items]
    allocated = fmt(sum((number(v) for v in values.values()), Fraction()))
    lines += [f"총 시간: {result['total']}분 · 배분 합계: {allocated}분 · 잔여: {result['remaining']}분",
              "합계는 " + "+".join(values[r["id"]] for r in items) + "=" + allocated + "분입니다."]
    excluded = [r["expected_values"]["label"] for r in rows if r["kind"] == "item" and r["expected_values"]["mode"] == "excluded"]
    if excluded:
        lines.append("제외 항목: " + ", ".join(excluded))
    for note in notes:
        row = next(r for r in items if r["id"] == note["item_id"])
        lines.append(row["expected_values"]["label"] + " — 목적: " + PURPOSES[note["purpose"]]
                     + " 방법: " + METHODS[note["method"]])
    return "\n".join(lines)


class AllocationController:
    """Small adapter over the existing task ledger, CAS and raw event store."""

    def __init__(self, selected: Any, tasks: Any):
        self.selected, self.tasks = selected, tasks
        self.candidate: dict | None = None
        self.notes: list[dict] = []
        self.bundle: dict | None = None
        self.decision = "insufficient_info"
        self.checks: list[dict] = []
        self.output: str | None = None
        self.final_status = "incomplete"

    @property
    def snapshot(self):
        return (self.selected.state or {}).get("effective_requirements") or {
            "instruction_version": self.selected.instruction_version, "requirements": [], "unresolved": []}

    def reconcile(self, state: dict):
        from nanobot.session.history_visibility import is_hidden_history_message
        originals = []
        snapshot = state.get("effective_requirements") or {}
        active_sources = {src["event_id"] for r in snapshot.get("requirements", []) for src in r["sources"]}
        for entry in state["user_constraints"]:
            if snapshot and entry.get("sequence", 0) <= snapshot.get("through_sequence", 0) and entry["event_id"] not in active_sources:
                continue
            events = self.tasks.store.get_events(self.selected.scope, [entry["event_id"]])
            ev = next((e for e in events if e.actor == "user" and e.event_type == "USER_MESSAGE"
                       and e.session_id == state["session_id"] and not e.redacted_at
                       and not is_hidden_history_message(self.tasks.store._metadata_obj(e.metadata_json))), None)
            if ev is None:
                state.setdefault("effective_requirements", {"instruction_version": state["instruction_version"],
                    "requirements": [], "unresolved": [], "through_sequence": 0})["unresolved"].append({
                        "id": _id(), "code": "source_unavailable", "affects": [],
                        "sources": [{"event_id": entry["event_id"], "source_span": entry.get("text", "")}], "question_id": None})
                continue
            originals.append({"event_id": ev.event_id, "sequence": ev.sequence, "text": ev.content or ""})
        ref = self.selected.design.reference if self.selected.design else None
        reduce_requirements(state, originals, response_id=ref.response_id if ref else None)
        if state.get("instruction_status") != "CANCELLED":
            state["status"] = "IN_PROGRESS"
            state["instruction_status"] = "ACTIVE"
        state["blockers"] = [b for b in state.get("blockers", []) if b != "new_user_instruction_requires_reconciliation"]
        state["unfinished"] = [c["id"] for c in derive_checks(state["effective_requirements"])]
        self.selected.completion_reports.clear()

    def initialize(self):
        existing = next((r.get("allocation") for r in (self.selected.state or {}).get("work_records", [])
                         if r.get("request_id") == self.selected.trace.request_id
                         and r.get("instruction_version") == self.selected.instruction_version and r.get("allocation")), None)
        if existing:
            self.bundle = copy.deepcopy(existing)
            self.candidate, self.checks = existing["result"], copy.deepcopy(existing["checks"])
            self.output, self.final_status, self.decision = existing["output"], "completed", "pass"
            return
        d = self.selected.design
        if d:
            self.candidate = d.allocation_result.model_dump() if d.allocation_result else None
            self.notes = [n.model_dump() for n in d.allocation_explanations]
        decision, _ = analyze(self.snapshot)
        if decision in {"requirement_conflict", "insufficient_info"} and d:
            d.route, d.answer = "clarify", None
            d.unresolved = [self.question()]

    def question(self) -> str:
        decision, reasons = analyze(self.snapshot)
        if decision == "requirement_conflict" and reasons != ["fixed_total_conflict"]:
            return "고정 시간과 관계 조건이 충돌합니다. 조정할 조건을 알려 주세요: " + ", ".join(reasons)
        if decision == "requirement_conflict":
            fixed = [r for r in self.snapshot["requirements"] if r["kind"] == "item"
                     and r["expected_values"]["mode"] == "fixed"]
            total = next(r for r in self.snapshot["requirements"] if r["kind"] == "total")
            fixed_sum = fmt(sum((number(r["expected_values"]["value"]) for r in fixed), Fraction()))
            return (f"고정 항목 합은 {fixed_sum}분, 총 시간은 {total['expected_values']['value']}분입니다. "
                    f"1번: 총 시간을 {fixed_sum}분으로 변경하고 항목 유지. "
                    "2번: 조정할 고정 항목과 값을 알려 주세요.")
        return "확인이 필요합니다: " + ", ".join(reasons)

    def submit(self, candidate: dict, notes: list[dict]):
        # Same schema as direct design; no model status/check can turn a failure into pass.
        self.candidate, self.notes, self.bundle = candidate, notes, None
        self.decision, self.checks, result = validate_result(self.snapshot, candidate, notes)
        return {"decision": self.decision, "checks": self.checks, "result": result}

    def external_gaps(self) -> list[str]:
        s = self.selected
        gaps = [*s.missing]
        if s.blocked:
            gaps.append(s.blocked)
        if s.design:
            gaps.extend(s.result_evidence_gaps())
            gaps.extend(s.lookup_unmet())
            gaps.extend(s.compliance_failures())
            required = [c for c in s.design.completion_checks if not s._answer_kind(c) and not s.compliance_check(c)]
            gaps.extend(c for c in required if not s.check_passed(c)[0])
            gaps.extend(s.open_unresolved())
        return [str(g) for g in gaps]

    def finalize(self, content: str | None, stop_reason="completed") -> tuple[str, str]:
        s = self.selected
        if s.control:
            s.control.refresh()
        if (s.state or {}).get("instruction_status") == "CANCELLED":
            self.output, self.final_status = "작업이 취소되었습니다.", "cancelled"
            return self.output, self.final_status
        if s.blocked or (s.state or {}).get("instruction_status") == "RECONCILE":
            self.decision = "insufficient_info"
            self.final_status = "incomplete"
            self.output = "요청을 완료하지 못했습니다: " + (s.blocked or "new_user_instruction_requires_reconciliation")
            return self.output, self.final_status
        if (self.bundle and self.bundle["instruction_version"] == s.instruction_version
                and stop_reason == "completed" and not self.external_gaps()):
            self.output, self.final_status = self.bundle["output"], "completed"
            return self.output, self.final_status
        decision, reasons = analyze(self.snapshot)
        if decision != "pass":
            self.decision = decision
            self.output = self.question()
            self.final_status = "awaiting_confirmation"
            return self.output, self.final_status
        candidate = self.candidate if self.candidate is not None else automatic_result(self.snapshot)
        self.decision, self.checks, result = validate_result(self.snapshot, candidate, self.notes)
        if self.decision == "pass":
            s.answer_text = render(self.snapshot, result, self.notes)
        gaps = self.external_gaps()
        if self.decision != "pass" or gaps or stop_reason != "completed":
            if gaps and self.decision == "pass":
                self.decision = "insufficient_info"
            self.output = "요청을 완료하지 못했습니다: " + self.decision
            if gaps:
                self.output += " (" + ", ".join(gaps) + ")"
            if stop_reason != "completed":
                self.output += " (" + stop_reason + ")"
            self.final_status = "incomplete"
            return self.output, self.final_status
        self.output = render(self.snapshot, result, self.notes)
        self.final_status = "completed"
        result_id = _id()
        for c in self.checks:
            c.update(task_id=s.state["task_id"], result_id=result_id)
            s.check_bindings[c["id"]] = {"requirement_ids": c["requirement_ids"],
                "task_id": s.state["task_id"], "instruction_version": s.instruction_version, "result_id": result_id}
        self.bundle = {"result_id": result_id, "instruction_version": s.instruction_version,
                       "submitted_result": copy.deepcopy(self.candidate),
                       "result": result, "checks": copy.deepcopy(self.checks), "output": self.output,
                       "renderer_version": 1, "response_id": "E" + _id(), "status": "prepared"}
        s.answer_text = self.output
        s.note_instruction_read("completion")
        return self.output, self.final_status

    def report(self):
        return {"overall": self.decision, "basis": {"task_id": (self.selected.state or {}).get("task_id"),
                "instruction_version": self.selected.instruction_version,
                "result_id": (self.bundle or {}).get("result_id")},
                "checks": copy.deepcopy(self.checks), "contract": "time_allocation_v1"}

    def persist(self, content: str, stop_reason: str) -> tuple[str, str, str | None]:
        """Prepare first, then atomically store the exact assistant event and CAS/DONE.

        No successful assistant event is visible if the final CAS/freshness check fails.
        The normal session save subsequently mirrors the same event ID (idempotent).
        """
        s = self.selected
        if s.control:
            s.control.refresh()
        if s.blocked and s.blocked.startswith("allocation_save_failed:"):
            # A retry rechecks version/freshness atomically; the previous I/O error is not a requirement.
            s.blocked = None
        content, stop_reason = self.finalize(content, stop_reason)
        if not s.state:
            return content, stop_reason, None
        state = copy.deepcopy(s.state)
        bundle = copy.deepcopy(self.bundle) if stop_reason == "completed" else None
        previous = next((r for r in state.get("work_records", []) if r.get("request_id") == s.trace.request_id
                         and r.get("instruction_version") == s.instruction_version), None)
        if previous and previous.get("allocation", {}).get("status") in {"committed", "delivered", "delivery_unknown"}:
            latest = self.tasks.events(s.scope, state["session_id"], user_only=True, tail=1)
            if latest and latest[0].event_id == s.current_event.event_id:
                saved = previous["allocation"]
                return saved["output"], "completed", saved["response_id"]
        response_id = bundle["response_id"] if bundle else (previous or {}).get("response_id") or "E" + _id()
        record = {"request_id": s.trace.request_id, "event_id": s.current_event.event_id,
                  "instruction_version": s.instruction_version, "stop_reason": stop_reason, "response_id": response_id,
                  "requirements": {c["id"]: c.get("status", "open") for c in self.checks}}
        records = [r for r in state.get("work_records", []) if r.get("request_id") != s.trace.request_id]
        if bundle:
            record["allocation"] = bundle
        state["work_records"] = [*records, record][-20:]
        if stop_reason == "awaiting_confirmation" and analyze(self.snapshot) == ("requirement_conflict", ["fixed_total_conflict"]):
            existing = next((q for q in state.get("quantity_questions", [])
                             if q.get("allocation") and q["request_id"] == s.trace.request_id), None)
            if not existing:
                fixed = [r for r in self.snapshot["requirements"] if r["kind"] == "item"
                         and r["expected_values"]["mode"] == "fixed"]
                total = next(r for r in self.snapshot["requirements"] if r["kind"] == "total")
                value = fmt(sum((number(r["expected_values"]["value"]) for r in fixed), Fraction()))
                state.setdefault("quantity_questions", []).append({"allocation": True, "question_id": _id(),
                    "task_id": state["task_id"], "instruction_version": s.instruction_version,
                    "request_id": s.trace.request_id, "request_event_id": s.current_event.event_id,
                    "response_id": response_id, "question": content, "affects": [total["id"]],
                    "choices": [{"kind": "total", "value": value, "operation": "replace", "requirement_id": total["id"]},
                                {"kind": "items", "operation": "clarify"}], "relations": []})
        state["status"] = "CANCELLED" if stop_reason == "cancelled" else "IN_PROGRESS"
        try:
            s.state = self.tasks.save(s.scope, state, expected_version=state["version"],
                                     through_event_id=s.current_event.event_id)
            state = copy.deepcopy(s.state)
            record = state["work_records"][-1]
            if bundle:
                state["completed"] = [r for r in state["completed"] if r.get("result_id") != bundle["result_id"]]
                record["allocation"]["status"] = "committed"
                state["status"], state["unfinished"] = "DONE", []
                state["completed"].append({"kind": "allocation_verified", "request_id": s.trace.request_id,
                    "instruction_version": s.instruction_version, "result_id": bundle["result_id"],
                    "response_id": response_id, "calculation_report": self.report()})
            event = RawEvent(event_id=response_id, owner_id=s.scope.owner_id,
                workspace_id=s.scope.workspace_ids[0], agent_id=s.scope.agent_ids[0],
                conversation_id=state["session_id"], session_id=state["session_id"],
                sequence=self.tasks.store.next_sequence(state["session_id"]), ts=self.tasks.store.now_ts(),
                actor="assistant", event_type="ASSISTANT_MESSAGE", content=content,
                content_hash=self.tasks.store._hash_content(content), parent_event_id=s.current_event.event_id)
            s.state = self.tasks.save(s.scope, state, expected_version=state["version"],
                through_event_id=s.current_event.event_id, events=[event])
            if bundle:
                self.bundle["status"] = "committed"
            s.trace.counters["completion_verified"] = int(bool(bundle))
            s.trace.persist(self.tasks.store)
            return content, stop_reason, response_id
        except (ValueError, OSError, sqlite3.Error) as exc:
            s.blocked = "allocation_save_failed:" + type(exc).__name__
            s.trace.count("state_conflicts", "allocation_commit_failed")
            self.final_status, self.output = "incomplete", "요청을 완료하지 못했습니다: allocation_save_failed"
            return self.output, self.final_status, None


class SubmitTimeAllocationTool(Tool):
    name = "submit_time_allocation"
    description = "Submit a time allocation result for the current task/version. Code verifies it; this cannot edit requirements."
    parameters = {"type": "object", "$defs": AllocationResult.model_json_schema().get("$defs", {}), "properties": {
        "result": AllocationResult.model_json_schema(),
        "explanations": {"type": "array", "items": AllocationExplanation.model_json_schema()}},
        "required": ["result"], "additionalProperties": False}

    @property
    def read_only(self):
        return True

    def __init__(self, controller):
        self.controller = controller

    async def execute(self, result: dict, explanations: list[dict] | None = None, **kwargs):
        return ToolResult(json.dumps(self.controller.submit(result, explanations or []), ensure_ascii=False))


def record_delivery(tasks, scope, session_id, task_id, result_id, delivered: bool):
    """Transport acknowledgment only; never changes requirements or promotes stale DONE."""
    state = next((s for s in tasks.list(scope, session_id) if s["task_id"] == task_id), None)
    if not state:
        return
    bundle = next((r["allocation"] for r in state.get("work_records", [])
                   if (r.get("allocation") or {}).get("result_id") == result_id), None)
    if not bundle or bundle["status"] not in {"committed", "delivered", "delivery_unknown"}:
        return
    bundle["status"] = "delivered" if delivered else "delivery_unknown"
    tasks.save(scope, state, expected_version=state["version"])


def normalize_answer_edits(effective, snapshot: dict):
    """Resolve a quantity edit to the answer when its source names a known time target.

    A real external object keeps the existing external evidence/permission path.
    """
    from nanobot.agent.context_relations import external_object
    labels = {r["expected_values"]["label"] for r in snapshot.get("requirements", []) if r["kind"] == "item"}
    for rel in effective.relations:
        if rel.action != "edit" or rel.target_ref or external_object(rel.source_span):
            continue
        for m in DURATION.finditer(rel.source_span):
            prefix = rel.source_span[:m.start()]
            if TOTAL.search(prefix) or _label(prefix) in labels:
                rel.action, rel.target, rel.resolved = "answer", "prior_answer", True
                break
    return effective


def followup_hint(request: str, responses=()) -> bool:
    """Only explicit retention/change/choice/reference syntax can mark a pending update."""
    return bool(RELEASE.search(request)
                or re.fullmatch(r"\s*(?:선택\s*)?[12](?:번)?\s*[.!]?", request)
                or re.search(r"(?:조건|항목|시간).*(?:유지|그대로)|같은\s*조건", request)
                or (DURATION.search(request) and re.search(r"변경|바꾸|바꿔|수정", request))
                or any(response in request for response in responses))
