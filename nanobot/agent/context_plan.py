"""t47: the editable execution plan of one task, kept in the existing task record.

Not a planner and not a second agent. User goals/constraints/requirements are read from the
shared instruction state (SelectedContext.user_requirements / required_results); this module only
keeps what the execution loop learned and decided:

- execution facts recorded by code after every tool result (tool, target, outcome, change+version,
  evidence, error kind) and a no-progress identity per attempt;
- the plan the executor revises in its ordinary next call (revise_context_requirements with
  reason=update_plan): plan version, step, next action, cause, change reason, affected items.

Code verifies the structure of a plan update (actions, causes, requirement IDs, cited calls); the
semantic judgment (what was obtained, what failed and why) is the executor's, in the same call that
chooses its next action — no extra model call is made here."""
from __future__ import annotations

import hashlib
import json
from typing import Any

NEXT_ACTIONS = ("continue", "extra_lookup", "revise_plan", "other_method", "ask_user", "final_report")
# Failure cause -> next actions that address it (t47 §5). A cause never permits relaxing a check.
CAUSE_ACTIONS = {
    "info_shortage": {"extra_lookup", "ask_user"},
    "wrong_assumption": {"revise_plan"},
    "transient_error": {"continue"},
    "unsuitable_approach": {"other_method"},
    "partial_error": {"revise_plan", "continue"},
    "evidence_link_missing": {"continue", "final_report"},
    "verifier_defect": {"final_report", "ask_user"},
}
TRANSIENT_RETRY_LIMIT = 2
UNKNOWN_CHANGE_KINDS = {"transport", "timeout"}   # a change whose outcome the response did not tell
NO_PROGRESS_REPEATS = 2          # the same attempt seen this many times with nothing new
_META_ARGS = {"intent_summary", "target", "scope", "reversible"}
_INTERNAL = {"discover_context_capabilities", "resolve_context_need", "record_context_completion",
             "revise_context_requirements", "skill_decision", "skill_search", "skill_registry"}

PLAN_RULES = (
    "Plan update (reason=update_plan; same response as your next action, no extra call). Send only the fields "
    "that changed: a field left out keeps its current value; an update with nothing in it is no_change. A "
    "rejected update returns only its own error and the other actions of the response still run under their "
    "own checks. Short fields only, no reasoning text: obtained (what the last results gave), remaining (requirement IDs still unmet), "
    "plan_valid, next_action (continue | extra_lookup | revise_plan | other_method | ask_user | final_report), "
    "cause when something failed (info_shortage→extra_lookup/ask_user; wrong_assumption→revise_plan naming "
    "the assumption; transient_error→continue citing the failed call, limited retries; unsuitable_approach→"
    "other_method with a different query/source/tool use; partial_error→revise_plan naming the affected "
    "requirement IDs, only those are re-verified; evidence_link_missing→cite the calls already obtained, do "
    "not re-run them; verifier_defect→final_report naming the check, never re-run the work), change_reason. "
    "Requirement IDs come from the execution state; a plan update cannot add, drop or widen user "
    "requirements or permissions. Plan version changes only with a new method/order/depends_on. "
    "A requirement the design missed is added with reason=supplement_requirement, not update_plan.")


_PATH_ARGS = ("path", "file_path")


def identity(name: str, args: dict | None, workspace: Any = None) -> str:
    """t50: the attempt's identity after notation is normalized, so writing the same path another way
    (./a.md, a relative vs absolute path) or re-spelling a web query/URL is the same attempt. The
    content arguments stay as given: a different change (title vs body) is a different identity."""
    actual = {k: v for k, v in (args or {}).items() if k not in _META_ARGS}
    if name in {"web_fetch", "web_search"}:
        from nanobot.agent.context_selection import lookup_identity
        key = lookup_identity(name, actual)
        if key is not None:
            actual = {"lookup": list(key)}
    for k in _PATH_ARGS:
        if isinstance(actual.get(k), str) and actual[k].strip():
            from nanobot.agent.context_evidence import canonical
            actual[k] = canonical(actual[k].strip(), workspace)
    return hashlib.sha256((name + json.dumps(actual, sort_keys=True, default=str, ensure_ascii=False))
                          .encode()).hexdigest()[:16]


def plan_of(selected: Any) -> dict:
    plan = selected.exec_plan
    if not plan:
        plan.update(version=1, step=None, next_action=None, facts=[], failures=[], attempts={},
                    no_progress=[], changes=[], last_change=None, verifier_errors=[], record_errors=[],
                    reverify=[], retired_assumptions=[],
                    # t50: the request version this plan serves (restored only for the same version)
                    instruction_version=getattr(selected, "instruction_version", None))
    return plan


def persist(selected: Any) -> None:
    """Mirror into the existing task record (no separate store)."""
    plan = plan_of(selected)
    public = {k: v for k, v in plan.items() if k != "attempts"}
    if selected.control and selected.state:
        selected.control.save(lambda state: state.update(execution_plan=json.loads(json.dumps(public,
                                                                                             default=str))))


def requirement_ids(selected: Any) -> dict[str, dict]:
    out = {}
    if getattr(selected, "allocation", None):
        out.update({r["id"]: {"kind": "allocation", "target": r["target"]}
                    for r in selected.allocation.snapshot["requirements"]})
    for r in selected.required_results():
        rid = r["kind"] + (":" + str(r["target"]) if r.get("target") else "")
        out[rid] = r
    return out


def requirement_status(selected: Any) -> dict[str, str]:
    """Code's own view: done only by a passed, current (not stale) completion record bound to it."""
    status = {}
    for rid, req in requirement_ids(selected).items():
        done = False
        for check, report in selected.completion_reports.items():
            if report.get("status") != "passed" or selected.check_stale(check):
                continue
            binding = selected.check_bindings.get(check) or {}
            tgt = binding.get("target") or report.get("target")
            if req.get("target") and tgt and str(tgt) == str(req["target"]):
                done = True
            elif not req.get("target") and req["kind"] == "answer":
                done = done or binding.get("kind") in {"answer", "answer_value", "calculation", None}
        # A reverified requirement lost its earlier passes (popped in update_plan), so any pass
        # counted here was recorded after the change.
        if getattr(selected, "allocation", None) and rid in {r["id"] for r in selected.allocation.snapshot["requirements"]}:
            done = bool(selected.completion_supported() and any(
                rid in c["requirement_ids"] and c["status"] == "pass" for c in selected.allocation.checks))
        status[rid] = "done" if done else "open"
    return status


def _requirement_for(selected: Any, target: str | None) -> str | None:
    for rid, req in requirement_ids(selected).items():
        if target and req.get("target") and str(req["target"]) == str(target):
            return rid
    return None


def record_fact(selected: Any, call: Any, record: dict) -> dict:
    """Hook point 1: code records the execution fact before the executor evaluates it."""
    plan = plan_of(selected)
    ok = record.get("status") == "ok" and not record.get("failure_class")
    target = record.get("resource_id")
    changed = ok and not record.get("read_only", True)
    fact = {"call": call.id, "tool": call.name, "target": target,
            "outcome": "ok" if ok else ("partial" if record.get("status") == "ok" else "error"),
            "changed": changed, "target_version": selected.target_version(target) if target else None,
            "evidence": call.id if ok else None,
            "error_kind": None if ok else (record.get("failure_class") or "tool_error"),
            "requirement": _requirement_for(selected, target),
            "instruction_version": selected.instruction_version}
    if ok and record.get("no_new_information_of"):
        fact["outcome"] = "ok_no_new_information"
    plan["facts"].append(fact)
    del plan["facts"][:-30]
    # No-progress identity: requirement + tool/target/arguments + result/error identity + target version.
    ident = identity(call.name, call.arguments, getattr(selected, "workspace", None))
    result_id = record.get("output_hash") if ok else fact["error_kind"] + ":" + str(record.get("output_hash"))[:12]
    key = "|".join([str(fact["requirement"]), ident, str(result_id)[:24], str(fact["target_version"])[:16]])
    seen = plan["attempts"].setdefault(key, {"count": 0, "calls": []})
    # t55: a successful read of the same identity at the same target content version as an earlier
    # successful one brings nothing new, whatever the tool printed this time (version-based, no wording).
    same_as_before = bool(ok and not changed and fact["target_version"] is not None and any(
        f.get("identity") == ident and str(f["outcome"]).startswith("ok") and not f["changed"]
        and f["target_version"] == fact["target_version"] for f in plan["facts"][:-1]))
    new_evidence = (ok and not record.get("no_new_information_of") and seen["count"] == 0
                    and not same_as_before)
    seen["count"] += 1
    seen["calls"].append(call.id)
    fact["identity"] = ident
    if ok and (new_evidence or changed):
        # t55: the last real progress by code's record (new result identity or a performed change);
        # a model's own "progress" wording never sets this.
        plan["last_progress"] = {"call": call.id, "tool": call.name, "target": target,
                                 "kind": "change" if changed else "new_result",
                                 "requirement": fact["requirement"]}
    if ok and not changed and not record.get("output_hash"):
        # t55: code cannot tell whether this result is new; recorded as a judgment limit, the whole
        # request budget is what bounds such calls.
        plan.setdefault("judgment_limits", {})
        plan["judgment_limits"][call.name] = int(plan["judgment_limits"].get(call.name, 0)) + 1
    if not ok:
        plan["failures"].append({"call": call.id, "tool": call.name, "target": target, "identity": ident,
                                 "error_kind": fact["error_kind"], "requirement": fact["requirement"],
                                 "cause": None, "error_excerpt": record.get("error_excerpt"),
                                 "executed": record.get("executed", True),
                                 # t49: the failure condition, so a retry is judged against what changed.
                                 "target_version": fact["target_version"],
                                 "instruction_version": selected.instruction_version,
                                 "material": _material(plan),
                                 # t50: an interrupted change (status "unknown") is unknown too.
                                 "change_unknown": bool(not record.get("read_only", True)
                                                        and record.get("executed", True)
                                                        and (fact["error_kind"] in UNKNOWN_CHANGE_KINDS
                                                             or record.get("status") == "unknown")),
                                 "fact_index": len(plan["facts"]) - 1})
        del plan["failures"][:-20]
    if seen["count"] >= NO_PROGRESS_REPEATS and not new_evidence and not changed:
        entry = next((e for e in plan["no_progress"] if e["key"] == key), None)
        if entry is None:
            entry = {"key": key, "requirement": fact["requirement"], "tool": call.name, "target": target,
                     "identity": ident, "result": fact["outcome"] if ok else fact["error_kind"], "calls": []}
            plan["no_progress"].append(entry)
            selected.trace.count("no_progress", call.name)
        entry["calls"] = list(seen["calls"])
        entry["count"] = seen["count"]
        # t49: the condition at the latest identical attempt; a retry needs a change against it.
        entry.update(target_version=fact["target_version"], instruction_version=selected.instruction_version,
                     material=_material(plan) - (1 if fact["outcome"] == "ok" else 0), fact_index=len(plan["facts"]) - 1)
    persist(selected)
    return fact


def note_record_error(selected: Any, call: Any, text: str) -> None:
    """A completion/verification record that errored (rejection or verifier exception) – kept apart
    from execution facts: a checker error never denies a performed change."""
    plan = plan_of(selected)
    check = str((call.arguments or {}).get("check") or "")
    plan["record_errors"].append({"call": call.id, "check": check, "error": str(text)[:300]})
    del plan["record_errors"][:-20]
    persist(selected)


def _material(plan: dict) -> int:
    """Count of successful calls that produced new information so far (kept in records only)."""
    return sum(1 for f in plan["facts"] if f["outcome"] == "ok")


def _later_facts(plan: dict, entry: dict) -> list[dict]:
    """t50: facts recorded after the entry's attempt, located by call ID (fact positions shift when old
    facts are trimmed; an attempt trimmed away means every kept fact is later)."""
    calls = entry.get("calls") or ([entry["call"]] if entry.get("call") else [])
    last = calls[-1] if calls else None
    pos = max((i for i, f in enumerate(plan["facts"]) if f["call"] == last), default=None)
    return plan["facts"] if pos is None else plan["facts"][pos + 1:]


def _relevant_material(plan: dict, entry: dict) -> list[dict]:
    """t50: new successful information that bears on this failure: the same target or the same
    requirement, from another attempt. Unrelated material elsewhere is not a basis for a retry."""
    target, req = entry.get("target"), entry.get("requirement")
    if not target and not req:
        return []
    return [f for f in _later_facts(plan, entry) if f["outcome"] == "ok" and f.get("identity") != entry.get("identity")
            and ((target and f["target"] == target) or (req and f.get("requirement") == req))]


def retry_conditions(selected: Any, entry: dict) -> list[str]:
    """t49: what changed since a failed / no-progress attempt, verified by code (a model's declaration
    alone changes nothing). Different arguments or a different tool are a different identity and are
    never blocked here."""
    plan = plan_of(selected)
    met = []
    target = entry.get("target")
    if target and selected.target_version(target) != entry.get("target_version"):
        met.append("target_changed")
    if _relevant_material(plan, entry):
        met.append("material_obtained")
    if selected.instruction_version != entry.get("instruction_version", selected.instruction_version):
        met.append("instruction_or_permission_changed")
    if entry.get("result") in UNKNOWN_CHANGE_KINDS or entry.get("error_kind") in UNKNOWN_CHANGE_KINDS:
        later = _later_facts(plan, entry)
        tries = sum(1 for f in plan["failures"] if f["identity"] == entry["identity"])
        if any(f["tool"] == entry["tool"] and f["outcome"] == "ok" for f in later) and tries < TRANSIENT_RETRY_LIMIT + 1:
            met.append("transient_resolved")
    return met


def blocked_repeat(selected: Any, name: str, params: dict | None) -> str | None:
    """t54: a model that keeps re-requesting a refused action stops in finite steps. Each refusal of the
    same identity is counted (records kept, never reset by other work); past NO_PROGRESS_REPEATS refusals
    the request stops with no_progress_repeat. A retry the conditions allow is not a refusal."""
    reason = _blocked_repeat(selected, name, params)
    if reason is None:
        return None
    plan = plan_of(selected)
    ident = str(identity(name, params if isinstance(params, dict) else {}, getattr(selected, "workspace", None)))
    refused = plan.setdefault("refused_repeats", {})
    refused[ident] = int(refused.get(ident, 0)) + 1
    if refused[ident] > NO_PROGRESS_REPEATS:
        selected.stop_requested = "no_progress_repeat"
        selected.trace.count("plan_gate", "no_progress_repeat_stop")
        budget = getattr(selected.trace, "budget", None)
        if budget is not None:
            budget.stop("no_progress_repeat")
    return reason


def _blocked_repeat(selected: Any, name: str, params: dict | None) -> str | None:
    """Hook point 3 (before execution): an attempt already marked no-progress, or a change already
    applied (its target still at the version that change produced), is not executed again. Other
    work is not halted by the flag."""
    plan = plan_of(selected)
    ident = identity(name, params if isinstance(params, dict) else {}, getattr(selected, "workspace", None))
    if name in _INTERNAL:
        if name == "record_context_completion" and isinstance(params, dict):
            if any(v["check"] == params.get("check") for v in plan["verifier_errors"]):
                selected.trace.count("plan_gate", "verifier_defect_recorded")
                return ("This check was recorded as a verifier defect; recording it again repeats the "
                        "same work. Report the performed result and the verifier error.")
        return None
    # t49: an unknown change outcome (timeout / lost response) is confirmed by a read before any repeat.
    for failure in reversed(plan["failures"]):
        if failure["identity"] == ident and failure.get("change_unknown"):
            later = _later_facts(plan, failure)
            if not any(f["target"] == failure["target"] and f["outcome"].startswith("ok") and not f["changed"]
                       for f in later):
                selected.trace.count("plan_gate", "unknown_change_unconfirmed")
                return ("The previous " + name + " on " + str(failure["target"]) + " (call " + failure["call"]
                        + ") ended without a known outcome (" + str(failure["error_kind"]) + "); it may have "
                        "been applied. Read the current state of the target first, then decide.")
            break
    for entry in plan["no_progress"]:
        if entry["identity"] == ident:
            met = retry_conditions(selected, entry)
            if met:
                selected.trace.count("plan_gate", "retry_allowed:" + met[0])
                continue
            selected.trace.count("plan_gate", "no_progress_repeat")
            return ("No progress: " + name + " with the same target/arguments returned the same "
                    + str(entry["result"]) + " " + str(entry.get("count")) + " times (calls "
                    + ", ".join(entry["calls"]) + ") for requirement " + str(entry["requirement"])
                    + ". Nothing has changed since then. A retry is allowed after: different arguments "
                    "(not another notation of the same ones), new material for this target or requirement, "
                    "permission obtained, a change of the target, a resolved transient error, or a different "
                    "approach; otherwise report the blocked reason.")
    for fact in reversed(plan["facts"]):
        if fact.get("identity") == ident and fact["changed"] and fact["target"]:
            # Same work = same identity, succeeded, target still at the version it produced, same
            # instruction version. Anything else is a new change and is not blocked here.
            if (fact["outcome"] == "ok" and selected.target_version(fact["target"]) == fact["target_version"]
                    and fact.get("instruction_version") == selected.instruction_version):
                selected.trace.count("plan_gate", "applied_change_repeat")
                return ("This exact change was already applied in call " + fact["call"] + " and "
                        + str(fact["target"]) + " is still at that version. Do not re-execute it: cite "
                        "that call as evidence (record_context_completion), or change only the part "
                        "that is wrong.")
            break
    return None


_OBSERVED_CAUSE = {"transient_error": {"transport"}, "info_shortage": {"no_results", "fetch_rejected"}}


def record_hypothesis(selected: Any, cause: str, progress: dict) -> dict:
    """t49: a model-assigned cause is a hypothesis, kept apart from code-recorded facts. It is marked
    consistent only when the recorded failure kind shows the same thing; otherwise "unconfirmed",
    which means gather information. A hypothesis never blocks a method by itself."""
    plan = plan_of(selected)
    cited = str(progress.get("failed_call") or "")
    failure = next((f for f in plan["failures"] if f["call"] == cited), None)
    kinds = _OBSERVED_CAUSE.get(cause)
    if failure is None:
        state = "unconfirmed"
    elif kinds is not None:
        state = "consistent_with_record" if failure["error_kind"] in kinds else "contradicted_by_record"
    else:
        state = "unconfirmed"
    hyp = {"cause": cause, "by": "executor", "call": cited or None, "status": state,
           "text": str(progress.get("cause_hypothesis") or "")[:200] or None}
    if failure is not None:
        failure["cause_hypothesis"] = hyp
    plan.setdefault("hypotheses", []).append(hyp)
    del plan["hypotheses"][:-10]
    if state != "consistent_with_record":
        selected.trace.count("cause_hypothesis", state)
    return hyp


def update_plan(selected: Any, progress: dict) -> dict | str:
    """Hook point 3 (decision): verify and apply the executor's short evaluation. Returns the new
    public plan view, or an error string.

    t50: only changed fields are needed; a field left out keeps its current value (method, order,
    depends_on, step, next action). An update with no field is "no_change" (not an error). Every
    check runs before anything is applied, so a rejected update changes nothing and is reported
    with its own error only (no reinterpretation, no regenerated plan)."""
    plan = plan_of(selected)
    if progress is None:
        progress = {}
    if not isinstance(progress, dict):
        return "progress must be an object"
    given = {k: v for k, v in progress.items()
             if v is not None and v != "" and v != [] and v != {} and not (k == "plan_valid" and v is True)}
    if not given:
        return {"no_change": True, "plan_version": plan["version"], "next_action": plan["next_action"],
                "requirements": requirement_status(selected), "last_change": plan["last_change"]}
    explicit = "next_action" in given
    if explicit and given["next_action"] not in NEXT_ACTIONS:
        return "next_action must be one of " + ", ".join(NEXT_ACTIONS)
    # A one-off transition (revise_plan / other_method) is not carried into the next update.
    action = given["next_action"] if explicit else (
        plan["next_action"] if plan["next_action"] not in {None, "revise_plan", "other_method"} else "continue")
    reqs = requirement_ids(selected)
    remaining = [str(r) for r in given["remaining"]] if "remaining" in given else None
    affected = [str(r) for r in given.get("affected") or []]
    unknown = [r for r in (remaining or []) + affected if r not in reqs]
    if unknown:
        return ("unknown requirement IDs " + ", ".join(unknown) + "; a plan update cannot add or change user "
                "requirements. Current IDs: " + ", ".join(reqs))
    cause = given.get("cause")
    plan_valid = bool(progress.get("plan_valid", True))
    reason = str(given.get("change_reason") or "").strip()
    if cause is not None and cause not in CAUSE_ACTIONS:
        return "cause must be one of " + ", ".join(CAUSE_ACTIONS)
    if (not plan_valid or action in {"revise_plan", "other_method"}) and (not cause or not reason):
        return "a plan or method change needs a cause and a change_reason"
    if cause and action not in CAUSE_ACTIONS[cause]:
        return ("cause " + cause + " is addressed by " + "/".join(sorted(CAUSE_ACTIONS[cause]))
                + ", not " + action)
    # t49/t50: the version moves only when method, order or dependencies actually change; fields not
    # sent keep the current plan's values.
    previous = plan.get("shape") or {"method": "", "order": [], "depends_on": []}
    shape = dict(previous)
    if "method" in given:
        shape["method"] = str(given["method"])[:200]
    for key in ("order", "depends_on"):
        if key in given:
            if not isinstance(given[key], list):
                return key + " must be a list"
            shape[key] = [str(x)[:80] for x in given[key]][:12]
    changed = any(shape[k] for k in shape) and shape != previous
    if action in {"revise_plan", "other_method"} and not changed:
        return (action + " needs the new method, order or depends_on, different from the current plan "
                + json.dumps(previous, ensure_ascii=False))
    # Validation of cause-specific citations (nothing applied yet).
    detail: dict = {}
    cited = str(given.get("failed_call") or "")
    failure = None
    if cause == "transient_error":
        failure = next((f for f in plan["failures"] if f["call"] == cited), None)
        if failure is None or failure["error_kind"] != "transport":
            return "a retry needs failed_call citing a transport failure of this task"
        tries = sum(1 for f in plan["failures"] if f["identity"] == failure["identity"])
        if tries >= TRANSIENT_RETRY_LIMIT:
            return ("transport failure repeated " + str(tries) + " times for this attempt; the retry "
                    "limit is reached. Use another allowed source or report the transport failure.")
        detail["retry_of"] = cited
    if cause == "unsuitable_approach":
        failure = next((f for f in plan["failures"] if f["call"] == cited), None) or next(
            (f for f in reversed(plan["facts"]) if f["call"] == cited), None)
        if failure is None:
            return "other_method needs failed_call citing the attempt being replaced"
        # t49: no permanent ban on the replaced attempt; repeating it is judged by retry_conditions.
        detail["replaced"] = cited
    assumption = None
    if cause == "wrong_assumption":
        assumption = str(given.get("assumption") or "").strip()
        active = [a.get("text") for a in (selected.state or {}).get("assumptions", [])] + list(
            selected.settled_unresolved)
        if not assumption or assumption not in active:
            return "wrong_assumption must name an active assumption verbatim: " + json.dumps(active, ensure_ascii=False)
    if cause in {"partial_error", "wrong_assumption"} and not affected:
        return cause + " needs the affected requirement IDs (only those are re-verified)"
    if cause == "evidence_link_missing":
        calls = [str(c) for c in given.get("evidence_calls") or []]
        good = [c for c in calls if (o := selected.observations.get(c)) and o.get("status") == "ok"
                and not o.get("failure_class")]
        if not calls or good != calls:
            return "evidence_link_missing needs evidence_calls citing successful calls already made"
        detail["evidence_calls"] = calls
    check = str(given.get("check") or "")
    if cause == "verifier_defect" and not any(e["check"] == check for e in plan["record_errors"]):
        return "verifier_defect needs a check whose record attempt errored in this task"
    # Apply (every check above passed). t50: the executor's cause is kept only as a hypothesis
    # (record_hypothesis below); it is never written into the recorded failure or execution fact.
    if cause == "wrong_assumption":
        selected.settled_unresolved.pop(assumption, None)
        if selected.state:
            selected.state["assumptions"] = [a for a in selected.state.get("assumptions", [])
                                             if a.get("text") != assumption]
        plan["retired_assumptions"].append(assumption)
        detail["retired_assumption"] = assumption
    if cause in {"partial_error", "wrong_assumption"}:
        for rid in affected:
            if rid not in plan["reverify"]:
                plan["reverify"].append(rid)
        # Only checks bound to the affected requirements lose their pass; others stay.
        for chk in list(selected.completion_reports):
            binding = selected.check_bindings.get(chk) or {}
            tgt = binding.get("target") or selected.completion_reports[chk].get("target")
            if any(reqs[r].get("target") and str(reqs[r]["target"]) == str(tgt) for r in affected) or (
                    any(not reqs[r].get("target") for r in affected) and not tgt):
                # t50: the earlier pass is kept as history (it was observed); it just stops counting.
                plan.setdefault("reverified_reports", []).append(
                    {"check": chk, "report": selected.completion_reports.pop(chk), "cause_hypothesis": cause})
                del plan["reverified_reports"][:-10]
                selected.trace.count("plan_reverify", cause)
        detail["affected"] = affected
    if cause == "verifier_defect":
        plan["verifier_errors"].append({"check": check, "calls": [e["call"] for e in plan["record_errors"]
                                                                  if e["check"] == check]})
        detail["verifier_defect"] = check
    if cause:
        detail["cause_hypothesis"] = record_hypothesis(selected, cause, progress)
    status = requirement_status(selected)
    for rid in [r for r in plan["reverify"] if status.get(r) == "done"]:
        plan["reverify"].remove(rid)
    if changed:
        plan["shape"] = shape
        detail["shape_from"] = previous
        before = plan["version"]
        plan["version"] += 1
        plan["last_change"] = {"from_version": before, "to_version": plan["version"], "cause": cause,
                               "action": action, "reason": reason[:300], **detail,
                               "kept_done": [r for r, s in status.items() if s == "done"],
                               "instruction_version": selected.instruction_version}
        plan["changes"].append(plan["last_change"])
        del plan["changes"][:-10]
        selected.trace.count("plan_revisions", cause or action)
    plan["step"] = str(given.get("step") or "")[:200] or plan["step"]
    plan["next_action"] = action
    if "obtained" in given:
        plan["obtained"] = str(given["obtained"])[:300]
    persist(selected)
    disagree = [] if remaining is None else [
        r for r, s in status.items() if s == "open" and r not in remaining and action != "ask_user"]
    hyp = detail.get("cause_hypothesis")
    return {"plan_version": plan["version"], "next_action": action, "requirements": status,
            "kept_fields": sorted(k for k in ("method", "order", "depends_on", "next_action", "step")
                                  if k not in given),
            "cause_status": (hyp["status"] + (": the records do not show this cause; gather information "
                             "before relying on it" if hyp["status"] != "consistent_with_record" else ""))
            if hyp else None,
            "still_open_per_records": disagree, "last_change": plan["last_change"]}


def retry_context(selected: Any) -> dict | None:
    """t49: after an execution error or no-progress, what the next call must see explicitly: affected
    requirements, the actual result/error, results that can be reused, and what must differ before the
    same work is tried again."""
    plan = plan_of(selected)
    last = plan["facts"][-1] if plan["facts"] else None
    stuck = plan["no_progress"][-1] if plan["no_progress"] else None
    if not last or (last["outcome"] == "ok" and not (stuck and last["call"] in stuck.get("calls", []))):
        return None
    failure = next((f for f in reversed(plan["failures"]) if f["call"] == last["call"]), None) or {}
    reusable = [f["call"] for f in plan["facts"] if f["outcome"] == "ok" and f["call"] != last["call"]][-6:]
    return {"after_call": last["call"], "affected_requirements": [r for r in [last.get("requirement")] if r]
            or list(requirement_ids(selected)),
            "actual_result": {"tool": last["tool"], "target": last["target"], "outcome": last["outcome"],
                              "error_kind": last.get("error_kind"), "error": failure.get("error_excerpt"),
                              "executed": failure.get("executed", True)},
            "reusable_results": reusable,
            "cause_hypothesis": failure.get("cause_hypothesis"),
            "retry_requires": failure.get("retry_requires") or [
                "different arguments (not only another notation of the same path/query)",
                "new material for this target or requirement, or permission obtained", "target content changed",
                "transient error resolved (cite evidence)", "a different approach"]}


def state_view(selected: Any) -> dict:
    """Hook point 2: compact current-state input for the next executor call (not the full log).

    t49 priority order, nothing dropped silently: (1) current requirements and prohibitions,
    (2) instruction version and target IDs, (3) remaining requirements, (4) changes actually performed,
    (5) last failure and the evidence for the next action; then plan/recent detail. Long items are
    shortened with a reference ID (event_id / call id in the task record) instead of being removed."""
    plan = plan_of(selected)
    status = requirement_status(selected)
    plan["reverify"] = [r for r in plan["reverify"] if status.get(r) != "done"]
    open_reqs = {r for r, s in status.items() if s == "open"}
    open_targets = {str(r.get("target")) for rid, r in requirement_ids(selected).items() if rid in open_reqs}
    relevant = [f for f in plan["failures"] if f["requirement"] in open_reqs or f["requirement"] is None
                or str(f.get("target")) in open_targets]
    reqs = selected.user_requirements() if selected.design else {"constraints": []}

    def short(text: str, ref: str | None, limit: int = 300):
        text = str(text or "")
        return text if len(text) <= limit else {"text": text[:limit] + "…", "ref": ref}

    constraints = [short(c, None) for c in reqs.get("constraints") or []] + [
        short(c.get("text"), c.get("event_id")) for c in (selected.state or {}).get("user_constraints", [])
        if c.get("text")]
    prohibitions, targets = [], {}
    if selected.design:
        for r in selected.relations().relations:
            if r.necessity == "forbidden":
                prohibitions.append({"action": r.action, "span": short(r.source_span, None, 160)})
            elif r.bound_id or r.target_ref:
                targets[r.action + ":" + (r.source_span or "")[:40]] = r.bound_id or r.target_ref
    intended = getattr(selected, "intended_target", None)
    unresolved = [{"code": c.get("code"), "detail": short(c.get("detail") or c.get("text") or "", None, 160)}
                  for c in (selected.unresolved_conditions() if selected.design else [])]
    changes = [{"call": f["call"], "tool": f["tool"], "target": f["target"]} for f in plan["facts"] if f["changed"]]
    return {"request": short(selected.request_text, "request", 2000),
            # (1) requirements and prohibitions – all of them
            "valid_constraints": constraints,
            "prohibitions": prohibitions,
            "unresolved_conditions": unresolved,
            "requirement_supplements": [s.model_dump() for s in getattr(selected, "requirement_supplements", [])],
            # (2) instruction version and target IDs
            "instruction_version": selected.instruction_version,
            "target_ids": targets,
            "intended_target": intended,
            # (3) remaining requirements
            "requirements": status,
            "remaining": sorted(open_reqs),
            "reverify": plan["reverify"],
            # (4) changes actually performed (all; facts are bounded only by the record's own window)
            "changes_performed": changes,
            # (5) last failure and the evidence for the next action
            "retry_context": retry_context(selected),
            "failed_attempts": relevant[-3:],
            "older_failure_refs": [f["call"] for f in relevant[:-3]],
            "no_progress": [{k: e.get(k) for k in ("requirement", "tool", "target", "result", "calls")}
                            for e in plan["no_progress"]],
            "plan": {"version": plan["version"], "step": plan["step"], "next_action": plan["next_action"],
                     "last_change": plan["last_change"]},
            "recent_results": [{k: f[k] for k in ("call", "tool", "target", "outcome", "changed", "error_kind",
                                                  "target_version") if f.get(k) is not None}
                               for f in plan["facts"][-4:]],
            "active_assumptions": [short(a.get("text"), a.get("event_id"), 200)
                                   for a in (selected.state or {}).get("assumptions", [])],
            "verifier_errors": plan["verifier_errors"],
            "cause_hypotheses": plan.get("hypotheses", [])[-3:],
            "target_versions": {t: (selected.target_version(t) or "")[:12] for t in open_targets if t != "None"}}


def answer_fix_feedback(selected: Any, answer: str) -> str | None:
    """Hook point 3 (final answer): code found answer checks failing in the delivered answer (e.g. a
    computed total). Record the failure as a fact and return feedback naming only the failed checks,
    so the executor's ordinary next call fixes just those parts. None when nothing answer-level failed."""
    from nanobot.agent.context_selection import computed_mismatch
    if not selected.design or not str(answer or "").strip():
        return None
    saved, selected.answer_text = selected.answer_text, answer
    try:
        failed = selected.answer_check_failures()
    finally:
        selected.answer_text = saved
    if not failed:
        return None
    plan = plan_of(selected)
    reasons = {}
    for check in failed:
        spec = selected.spec_for(check) if hasattr(selected, "spec_for") else next(
            (c for c in selected.design.completion_specs if c.check == check), None)
        if spec is not None and spec.result_kind == "computed":
            reasons[check] = computed_mismatch(spec.expected_values or {}, answer) or "mismatch"
        else:
            reasons[check] = "not satisfied in the answer"
    call = "answer#" + str(sum(1 for f in plan["failures"] if f["tool"] == "final_answer") + 1)
    plan["failures"].append({"call": call, "tool": "final_answer", "target": None, "identity": identity(
        "final_answer", {"answer": answer}), "error_kind": "answer_check_mismatch", "requirement": "answer",
        "cause": "partial_error", "checks": reasons})
    if "answer" not in plan["reverify"]:
        plan["reverify"].append("answer")
    selected.trace.count("answer_fix_requests", "answer_check_mismatch")
    persist(selected)
    return ("Answer checks failed in code: " + json.dumps(reasons, ensure_ascii=False)
            + ". Fix only these parts of the answer; keep every other part and constraint unchanged; do not "
            "redo lookups or changes. Execution state: " + json.dumps(state_view(selected), ensure_ascii=False,
                                                                  default=str))


def fix_route(selected: Any, answer: str, budget_left: bool) -> tuple[str | None, str | None]:
    """t49: what the single in-loop fix call (0071) may be used for. Returns (kind, feedback); feedback
    is None when no fix call is made.

    t50: a target/permission condition no longer stops an answer fix (it holds changes only); an open item
    shown to decide the answer -> clarification, no fix call; verifier internal defect -> report the
    actual result and the verifier error separately, no fix call; budget exhausted -> no extra call;
    calc/answer content error -> fix that part; evidence-link error -> fix only the linking.
    The fix call is an ordinary executor iteration: it counts toward max_iterations and the run's
    call/time budget. Keeping an actual change on a verifier error preserves the execution fact only;
    it never satisfies a requirement by itself."""
    if not selected.design:
        return None, None
    if getattr(selected, "allocation", None):
        a = selected.allocation
        if a.decision == "result_error" and budget_left:
            return "answer", ("Correct the structured result with submit_time_allocation; do not edit user requirements. "
                              + json.dumps(a.report(), ensure_ascii=False))
        return None, None
    plan = plan_of(selected)
    # t50: an unresolved condition holds only the work it affects. Target / reference / permission /
    # evidence conditions concern changes (mutation_gate), not the answer's own values, so they do not
    # stop an answer fix; the question for them is still asked in the final report. A design entry
    # shown to decide the answer holds the answer fix; one whose scope is unknown is named in the
    # feedback so values that depend on it are left open, not fixed by guess.
    conditions = selected.unresolved_conditions()
    if any("answer" in (c.get("affects") or []) for c in conditions):
        selected.trace.count("fix_route", "clarify_answer_affected")
        return "clarify", None
    if hasattr(selected, "requirement_conflicts") and selected.requirement_conflicts():
        # t60: the user's current requirements contradict each other: ask, do not fix by guess.
        selected.trace.count("fix_route", "requirement_conflict_clarify")
        return "clarify", None
    if hasattr(selected, "unverified_relation_gaps") and selected.unverified_relation_gaps():
        # t63: the model sent an essential relation only partly and the user's text does not recover it:
        # no rewrite can verify it and it is not a user question -> report incomplete, no fix call.
        selected.trace.count("fix_route", "unverified_relation_gap")
        return "unverified_relation", None
    if not budget_left:
        selected.trace.count("fix_route", "budget_exhausted")
        return "budget", None
    claims = selected.claim_failures() if hasattr(selected, "claim_failures") else []
    if claims:
        # t52 (4A/4B): the answer states a change this request's records do not support. If the
        # permission or target of that change is unclear, ask; otherwise return to the loop once with
        # the request, its required results and this turn's change records (within the budget).
        # Unclear permission/target stated by the user or the design (not the absence of a requirement,
        # which the supplement path settles) -> ask instead of returning to the loop.
        held = [c for c in conditions if c.get("code") in {"target_unresolved", "reference_unresolved",
                                                            "design_unresolved"}]
        if held or any(c["status"] == "target_unbound" for c in claims):
            selected.trace.count("fix_route", "claim_clarify")
            return "clarify", None
        from nanobot.agent.context_claims import this_request
        selected.trace.count("fix_route", "change_claim")
        rid = selected.trace.request_id
        changes = [{"tool_call_id": cid, "name": o.get("name"), "status": o.get("status"),
                    "failure_class": o.get("failure_class"), "executed": o.get("executed", True),
                    "target": o.get("resource_id")}
                   for cid, o in selected.observations.items()
                   if this_request(o, rid) and not o.get("read_only", True)]
        return "change_claim", (
            "Your answer states changes that this request's execution records do not support: "
            + json.dumps([{"sentence": c["sentence"], "target": c["target"], "record_status": c["status"]}
                          for c in claims], ensure_ascii=False)
            + ". User request: " + json.dumps(selected.request_text, ensure_ascii=False)
            + ". Required results now: " + json.dumps(selected.required_results(), ensure_ascii=False, default=str)
            + ". This request's change records: " + json.dumps(changes, ensure_ascii=False, default=str)
            + ". If the request requires this change and no current requirement covers it, add it with "
            "revise_context_requirements (reason=supplement_requirement, citing the request span); a "
            "supplement grants no permission and the pre-execution checks still apply. Then perform and "
            "verify it with the normal tools. If it cannot be done, report the actual state and what remains; "
            "only rewording the claim does not complete the request.")
    missing_calc = selected.calc_spec_missing() if hasattr(selected, "calc_spec_missing") else []
    if missing_calc:
        # t59: calculation needed, no spec: return once to the loop (within budget) to state the
        # calculation from the user's requirements; the answer is then compared in code.
        selected.trace.count("fix_route", "calc_spec_missing")
        from nanobot.agent.context_selection import CALC_SUPPLEMENT_RULES
        rejected = {c: v for c, v in (selected.spec_rejections() if hasattr(selected, "spec_rejections") else {}).items()
                    if c in missing_calc}
        return "calc_spec", (
            "계산 검증 필요·명세 없음: these checks need a calculated result but have no computed spec: "
            + json.dumps(missing_calc, ensure_ascii=False)
            + ((". First-design spec rejected (contradicts the current requirements; the user's requirement "
                "is clear, do not ask the user): " + json.dumps(rejected, ensure_ascii=False)) if rejected else "")
            + ". Call revise_context_requirements(reason=supplement_calculation, information_needs=[], "
            "completion_checks=[], calculation={...}). " + CALC_SUPPLEMENT_RULES
            + " User requirement texts: " + json.dumps(selected.user_requirement_texts(), ensure_ascii=False)
            + ". Carried conditions: " + json.dumps(selected.carried_report(), ensure_ascii=False)
            + ". Then give the final answer (corrected if the comparison is not consistent). If the "
            "calculation cannot be stated from the user's words, say that verification is incomplete.")
    feedback = answer_fix_feedback(selected, answer)
    if feedback:
        unknown = [c["text"] for c in conditions if c.get("code") == "design_unresolved" and c.get("scope") == "unknown"]
        if unknown:
            selected.trace.count("fix_route", "answer_with_unscoped_conditions")
            feedback += (" Open items whose effect is not established: " + json.dumps(unknown, ensure_ascii=False)
                         + ". A value that depends on one of them (amount, unit, period, target) is not fixed by "
                         "guess: leave it open and name the item as a question; fix the values that do not depend "
                         "on them.")
        return "answer", feedback
    defects = {v["check"] for v in plan["verifier_errors"]}
    links = [e for e in plan["record_errors"] if e["check"] not in defects]
    if links:
        calls = [f["call"] for f in plan["facts"] if f["outcome"] == "ok"][-8:]
        if calls:
            plan["reverify"].extend(c for c in dict.fromkeys(e["check"] for e in links) if c not in plan["reverify"])
            selected.trace.count("fix_route", "evidence_link")
            persist(selected)
            return "evidence_link", (
                "Completion records were rejected: " + json.dumps([{"check": e["check"], "error": e["error"][:160]}
                                                                    for e in links[-3:]], ensure_ascii=False)
                + ". Fix only the evidence links (cite these successful calls where they apply: " + ", ".join(calls)
                + "); do not redo lookups or changes. Re-check only these checks and those the fix affects. "
                "Execution state: " + json.dumps(state_view(selected), ensure_ascii=False, default=str))
    if defects:
        selected.trace.count("fix_route", "verifier_defect_report")
        return "verifier_defect", None
    return None, None
