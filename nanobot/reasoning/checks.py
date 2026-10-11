"""Code-owned structural and business-rule checks."""

from __future__ import annotations

import re
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
    structured: bool = False,
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
        defects.extend(answer_omissions(case, structured=structured))

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
    if case.conclusion.exception_rule.strip():
        # Design 5: an offset is allowed only by an explicit rule; S6 reviews that rule.
        unmet = unknown = []
    if unmet and case.conclusion.status in {"suitable", "conditionally_suitable"}:
        defects.append("required failure cannot be offset")
    if unknown and case.conclusion.status == "suitable":
        defects.append("unknown required criterion promoted to suitable")
    if case.conclusion.status == "conditionally_suitable" and not case.conclusion.conditions:
        defects.append("conditional conclusion has no conditions")
    return defects


def answer_omissions(case: ReasoningCase, structured: bool = False) -> list[str]:
    # A blank answer never passes, with or without a conclusion step (design 11).
    if not case.answer.strip():
        return [ANSWER_MISSING]
    selection_gaps = selection_omissions(case)
    if selection_gaps:
        return selection_gaps
    if case.conclusion is None:
        if "conclusion" in case.step_states:
            return ["answer has no conclusion"]
        return []
    defects: list[str] = []
    if structured:
        # Conditions are checked through answer_claims, not through the text.
        return defects
    for condition in case.conclusion.conditions:
        if not condition_covered(condition, case.answer):
            defects.append(f"answer omits condition: {condition}")
    return defects


def selection_omissions(case: ReasoningCase) -> list[str]:
    """A computed selection must be named, and every other candidate accounted for."""

    selection = next(
        (
            output["selection"]
            for output in case.step_outputs.values()
            if isinstance(output.get("selection"), dict) and output["selection"].get("candidates")
        ),
        None,
    )
    if selection is None:
        return []
    defects = [
        f"answer omits candidate: {candidate_id}"
        for candidate_id in selection["candidates"]
        if not _mentions(case.answer, candidate_id)
    ]
    selected = selection.get("selected")
    if selected and not _mentions(case.answer, selected):
        defects.insert(0, f"answer omits the selected candidate: {selected}")
    return defects


def _mentions(text: str, name: str) -> bool:
    if re.fullmatch(r"[0-9A-Za-z]+", name):
        return re.search(rf"(?<![0-9A-Za-z]){re.escape(name)}(?![0-9A-Za-z])", text) is not None
    return name in text


_TERM_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")


def condition_covered(condition: str, answer: str, threshold: float = 0.6) -> bool:
    """A condition is covered when most of its key terms appear in the answer.

    Verbatim matching rejected faithful paraphrases and stopped a finished run.
    A trailing Korean particle is tolerated ("도입비가" matches "도입비").
    """

    terms = _TERM_RE.findall(condition)
    if not terms:
        return True
    hits = sum(1 for term in terms if term in answer or _particle_stripped_in(term, answer))
    return hits / len(terms) >= threshold


def _particle_stripped_in(term: str, answer: str) -> bool:
    return len(term) >= 3 and "가" <= term[-1] <= "힣" and term[:-1] in answer


def _uses_only_unknown(case: ReasoningCase, evidence_ids: list[str]) -> bool:
    facts = {item.id: item for item in case.facts}
    referenced = [facts[item] for item in evidence_ids if item in facts]
    return bool(referenced) and all(item.status == "unknown" for item in referenced)


def _check_step(case: ReasoningCase, result: dict[str, Any]) -> list[str]:
    """S6: code checks over the whole chain, then the model's semantic findings."""

    code = code_chain_defects(case)
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


def code_chain_defects(case: ReasoningCase) -> list[str]:
    """S6 code part: chain structure and fixed rules. Runs whether or not the model reviews."""

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
    return code


CONCLUSION_RULE_PREFIX = "conclusion_rule"
ANSWER_CLAIMS_PREFIX = "answer_claims"


def rule_conclusion_statuses(case: ReasoningCase) -> set[str]:
    """Conclusion statuses the 'required_criteria' rule allows (approval item 4).

    Any required criterion unmet -> unsuitable. Otherwise any required criterion
    unknown or unjudged -> conditionally suitable or deferred. All met -> suitable.
    """

    results = {item.criterion_id: item.result for item in case.judgments}
    required = [item.id for item in case.criteria if item.kind == "required"]
    if any(results.get(item) == "unmet" for item in required):
        return {"unsuitable"}
    if any(results.get(item) != "met" for item in required):
        return {"conditionally_suitable", "deferred"}
    return {"suitable"}


def conclusion_rule_defects(case: ReasoningCase) -> list[str]:
    if case.conclusion is None:
        return []
    allowed = rule_conclusion_statuses(case)
    if case.conclusion.status in allowed or case.conclusion.exception_rule.strip():
        # A claimed exception is a rule code cannot check; it goes to review instead.
        defects: list[str] = []
    else:
        defects = [
            f"{CONCLUSION_RULE_PREFIX}: expected {' or '.join(sorted(allowed))}; "
            f"observed {case.conclusion.status}"
        ]
    case.checks.append(
        _record(case, "code:conclusion_rule", defects, "status derived from judgments by fixed rule")
    )
    return defects


def answer_claim_check(case: ReasoningCase, claims: Any) -> tuple[list[str], list[str]]:
    """Structured agreement between answer and verdict. Returns (defects, template additions).

    Checks the answer's declared status and coverage against code-held judgments,
    by criterion id, never by matching text fragments. A declared reference does not
    prove the text says it well (approval item 4).
    """

    if not isinstance(claims, dict):
        defects = [f"{ANSWER_CLAIMS_PREFIX} output has an invalid shape"]
        case.checks.append(_record(case, "code:answer_claims", defects, _CLAIMS_LIMIT))
        return defects, []
    defects: list[str] = []
    additions: list[str] = []
    if case.conclusion is not None and claims.get("status") != case.conclusion.status:
        defects.append(
            f"{ANSWER_CLAIMS_PREFIX}: answer states {claims.get('status')}; "
            f"conclusion is {case.conclusion.status}"
        )
    covers = {str(item) for item in claims.get("covers") or []}
    disclosed = {str(item) for item in claims.get("unknown_disclosed") or []}
    criteria = {item.id: item for item in case.criteria}
    for judgment in case.judgments:
        criterion = criteria.get(judgment.criterion_id)
        if criterion is None or criterion.kind != "required" or judgment.result == "met":
            continue
        if judgment.result == "unmet" and criterion.id not in covers:
            defects.append(f"{ANSWER_CLAIMS_PREFIX}: unmet required criterion {criterion.id} not explained")
    missing_unknown = [
        criteria[item.criterion_id]
        for item in case.judgments
        if item.result == "unknown"
        and item.criterion_id in criteria
        and criteria[item.criterion_id].kind == "required"
        and item.criterion_id not in disclosed
    ]
    if missing_unknown:
        # Template-fixable: code states the unconfirmed conditions itself.
        names = ", ".join(item.statement for item in missing_unknown)
        additions.append(f"다음 필수 조건은 아직 확인되지 않았습니다: {names}.")
    included = {int(item) for item in claims.get("conditions_included") or [] if str(item).isdigit()}
    conditions = case.conclusion.conditions if case.conclusion else []
    missing_conditions = [text for index, text in enumerate(conditions) if index not in included]
    if missing_conditions:
        additions.append(f"적용 조건: {'; '.join(missing_conditions)}.")
    case.checks.append(_record(case, "code:answer_claims", defects, _CLAIMS_LIMIT))
    return defects, additions


_CLAIMS_LIMIT = "model-declared status and criterion references; the wording itself is not verified"


def selection_claim_defects(case: ReasoningCase, claims: Any) -> list[str]:
    """The answer must declare the computed selection and account for every other candidate."""

    selection = _computed_selection(case)
    if selection is None:
        return []
    if not isinstance(claims, dict):
        defects = [f"{ANSWER_CLAIMS_PREFIX} output has an invalid shape"]
    else:
        defects = []
        selected = selection.get("selected")
        if (claims.get("selected") or None) != selected:
            defects.append(
                f"{ANSWER_CLAIMS_PREFIX}: answer selects {claims.get('selected')}; computed {selected}"
            )
        others = {item for item in selection["candidates"] if item != selected}
        excluded = {str(item) for item in claims.get("excluded") or []}
        missing = sorted(others - excluded)
        if missing:
            defects.append(f"{ANSWER_CLAIMS_PREFIX}: no exclusion reason for {', '.join(missing)}")
    case.checks.append(_record(case, "code:answer_claims", defects, _CLAIMS_LIMIT))
    return defects


def _computed_selection(case: ReasoningCase) -> dict[str, Any] | None:
    return next(
        (
            output["selection"]
            for output in case.step_outputs.values()
            if isinstance(output.get("selection"), dict) and output["selection"].get("candidates")
        ),
        None,
    )


def selection_template_answer(case: ReasoningCase) -> str | None:
    """Answer written by code from the computed selection (fallback; always consistent)."""

    selection = _computed_selection(case)
    if selection is None:
        return None
    selected = selection.get("selected")
    lines: list[str] = []
    if selected:
        objective = selection.get("objective") or {}
        basis = (
            f" 필수 조건을 충족한 후보 중 {objective['attr']} 기준으로 "
            f"{'가장 큰' if objective.get('direction', 'max') == 'max' else '가장 작은'} 후보입니다."
            if objective.get("attr")
            else " 필수 조건을 모두 충족한 후보입니다."
        )
        lines.append(f"{selected}를 선정합니다.{basis}")
        if selection.get("ties"):
            lines.append(f"동률 후보: {', '.join(selection['ties'])}.")
    else:
        lines.append("필수 조건을 확인된 값으로 모두 충족하는 후보가 없어 선정하지 않습니다.")
    for candidate_id, result in selection["candidates"].items():
        if candidate_id == selected:
            continue
        reasons = [f"{item['condition']} 미충족(값: {item['value']})" for item in result["failed"]]
        reasons += [f"{item['condition'].split(' ')[0]} 미확인" for item in result["unknown"]]
        if not reasons and result.get("status") == "eligible":
            reasons.append("필수 조건은 충족하지만 선택 기준에서 앞서지 않음")
        lines.append(f"{candidate_id}: {', '.join(reasons)}.")
    return "\n".join(lines)
