"""Code-owned structural and business-rule checks."""

from __future__ import annotations

from typing import Any

from nanobot.reasoning.models import CheckRecord, ReasoningCase

ANSWER_MISSING = "answer missing"


def _record(case: ReasoningCase, kind: str, defects: list[str], limit: str) -> CheckRecord:
    return CheckRecord(
        id=f"check-{len(case.checks) + 1}",
        target_version=f"{case.procedure_id}:{case.procedure_version}",
        kind=kind,
        result="fail" if defects else "pass",
        limit=limit,
        defects=defects,
    )


def check_stage(
    case: ReasoningCase,
    stage: str,
    *,
    declared_outputs: list[str] | None = None,
    result: dict[str, Any] | None = None,
) -> list[str]:
    """Run the checks that gate a stage. Returns defect messages.

    Every step gets at least one real check; nothing passes unchecked (design 6.2).
    """

    defects: list[str] = []
    if not stage:
        # Work steps: the output contract is the check.
        produced = result or {}
        for key in declared_outputs or []:
            if produced.get(key) is None:
                defects.append(f"declared output missing: {key}")
        case.checks.append(
            _record(case, "code:outputs", defects, "declared outputs present; not their correctness")
        )
        return defects
    if stage == "S6":
        return _check_step(case, result or {})
    if stage == "S1":
        if not case.goal.strip():
            defects.append("goal missing")
    elif stage == "S2":
        if not case.facts:
            defects.append("facts missing")
    elif stage == "S3":
        if not any(item.kind == "required" for item in case.criteria):
            defects.append("required criterion missing")
        if any(item.linked_goal != case.goal for item in case.criteria):
            defects.append("criterion is not linked to the goal")
    elif stage == "S4":
        covered = {item.criterion_id for item in case.judgments}
        missing = [item.id for item in case.criteria if item.id not in covered]
        if missing:
            defects.append(f"criteria without judgment: {', '.join(missing)}")
        known_facts = {item.id for item in case.facts}
        for judgment in case.judgments:
            if judgment.result != "unknown" and not judgment.evidence_ids:
                defects.append(f"{judgment.id} has no evidence")
            unknown_refs = [item for item in judgment.evidence_ids if item not in known_facts]
            if unknown_refs:
                defects.append(f"{judgment.id} references unknown facts")
            if judgment.result == "met" and _uses_only_unknown(case, judgment.evidence_ids):
                defects.append(f"{judgment.id} promotes unknown evidence to met")
    elif stage == "S5" and case.conclusion is not None:
        defects.extend(conclusion_conflicts(case))
    elif stage == "S7":
        defects.extend(answer_omissions(case))

    record = _record(
        case,
        f"code:{stage}",
        defects,
        "structural and fixed business rules only; not factual truth",
    )
    case.checks.append(record)
    return defects


def conclusion_conflicts(case: ReasoningCase) -> list[str]:
    if case.conclusion is None:
        return ["conclusion missing"]
    required = {item.id for item in case.criteria if item.kind == "required"}
    judgments = {item.criterion_id: item for item in case.judgments}
    unmet = [
        criterion_id
        for criterion_id in required
        if judgments.get(criterion_id) and judgments[criterion_id].result == "unmet"
    ]
    unknown = [
        criterion_id
        for criterion_id in required
        if judgments.get(criterion_id) and judgments[criterion_id].result == "unknown"
    ]
    defects: list[str] = []
    if unmet and case.conclusion.status in {"suitable", "conditionally_suitable"}:
        defects.append("required failure cannot be offset")
    if unknown and case.conclusion.status == "suitable":
        defects.append("unknown required criterion promoted to suitable")
    if case.conclusion.status == "conditionally_suitable" and not case.conclusion.conditions:
        defects.append("conditional conclusion has no conditions")
    return defects


def answer_omissions(case: ReasoningCase) -> list[str]:
    # A blank answer never passes, with or without a conclusion step (design 11).
    if not case.answer.strip():
        return [ANSWER_MISSING]
    if case.conclusion is None:
        if "conclusion" in case.step_states:
            return ["answer has no conclusion"]
        return []
    defects: list[str] = []
    for condition in case.conclusion.conditions:
        if condition not in case.answer:
            defects.append(f"answer omits condition: {condition}")
    return defects


def _uses_only_unknown(case: ReasoningCase, evidence_ids: list[str]) -> bool:
    facts = {item.id: item for item in case.facts}
    referenced = [facts[item] for item in evidence_ids if item in facts]
    return bool(referenced) and all(item.status == "unknown" for item in referenced)


def _check_step(case: ReasoningCase, result: dict[str, Any]) -> list[str]:
    """S6: code checks over the whole chain, then the model's semantic findings."""

    code: list[str] = []
    if case.criteria:
        covered = {item.criterion_id for item in case.judgments}
        missing = [item.id for item in case.criteria if item.id not in covered]
        if missing:
            code.append(f"criteria without judgment: {', '.join(missing)}")
    if case.conclusion is not None:
        known = {item.id for item in case.judgments}
        unknown = [item for item in case.conclusion.judgment_ids if item not in known]
        if unknown:
            code.append(f"conclusion cites unknown judgments: {', '.join(unknown)}")
        code.extend(conclusion_conflicts(case))
    case.checks.append(
        _record(case, "code:S6", code, "chain structure and fixed rules only; not factual truth")
    )
    semantic: list[str] = []
    found = result.get("defects")
    if not isinstance(found, list):
        semantic.append("semantic check result missing")
    else:
        for item in found:
            issue = str(item.get("issue") or "") if isinstance(item, dict) else str(item)
            if issue.strip():
                semantic.append(f"semantic: {issue.strip()}")
    case.checks.append(
        _record(case, "llm:S6", semantic, "same-model review; may share errors, not a proof")
    )
    return code + semantic
