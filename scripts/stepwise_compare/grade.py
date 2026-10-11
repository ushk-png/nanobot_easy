"""Automatic grading for the stepwise comparison. Rules fixed before any run.

Every configuration is graded on the final user-facing text, so plain model
answers and pipeline answers are judged the same way. Pipeline runs are also
checked for agreement between the structured result and that text.

Selection questions
- selected: in each sentence with a selection verb and no negation, the
  candidate id nearest before the verb (else the first after it). One
  distinct id -> that id. None, and a "nothing selected" phrase -> "none".
  Otherwise -> "ambiguous".
- correct: selected == expected.
- required_violation: selected is a candidate that fails a confirmed
  required condition (case "violating").
- unknown_as_confirmed: selected is a candidate whose only gap is an
  unconfirmed value (case "unknown_only"), or a sentence names such a
  candidate's unconfirmed attribute with a positive claim and no
  uncertainty wording.
- reasons_complete: every candidate id appears in the text.

Evaluation questions
- verdict class from the text: unsuitable > conditional > suitable
  (first matching pattern group); else "unclear".
- correct: class == expected.
- required_violation: expected unsuitable but class suitable or
  conditional (a required failure offset), or expected suitable but class
  unsuitable (a preferred condition treated as required).
- unknown_as_confirmed: a sentence with the unknown attribute and a
  positive claim but no uncertainty or negation wording.

consistent (pipeline only): structured selection / conclusion status maps
to the same label as the text.
"""

from __future__ import annotations

import re
from typing import Any

_SELECT = ("선정합니다", "선정한다", "선정했습니다", "선택합니다", "선택한다", "고릅니다", "추천합니다",
           "선정 결과는", "선정:", "선택:", "를 선정", "을 선정", "로 선정", "으로 선정", "를 선택", "을 선택")
_NEGATION = ("않", "제외", "탈락", "불가", "못", "없어", "없으므로", "없기", "아닙")
_NONE = ("선정하지 않", "선정할 수 없", "선정할 제품이 없", "선정할 후보가 없", "없어 선정", "선정 보류",
         "선정을 보류", "충족하는 후보가 없", "충족하는 제품이 없", "충족하는 솔루션이 없", "선택할 수 없",
         "고를 수 없", "해당하는 후보가 없", "적합한 후보가 없", "선정 대상이 없", "고르지 않")
_UNCERTAIN = ("미확인", "확인되지", "확인 안", "확인이 안", "알 수 없", "불명", "여부가", "여부는", "확인 필요",
              "확인되어야", "확인해야", "확인 전", "모르")
_POSITIVE = ("가능합니다", "지원합니다", "충족합니다", "제공합니다", "있습니다", "충족한다", "제공한다",
             "가능하다", "지원한다", "갖췄", "갖춥니다")
_UNSUITABLE = re.compile(r"부적합|적합하지\s*않|적합하지\s*못|사용할\s*수\s*없|권장하지\s*않")
_CONDITIONAL = re.compile(r"조건부|보류|확정할\s*수\s*없|판단할\s*수\s*없|확인\s*후|확인되면")
_SUITABLE = re.compile(r"적합")


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。])\s+|\n+", text)
    return [part.strip() for part in parts if part.strip()]


def _id_positions(sentence: str, candidates: list[str]) -> list[tuple[int, str]]:
    found = []
    for name in candidates:
        for match in re.finditer(rf"(?<![0-9A-Za-z]){re.escape(name)}(?![0-9A-Za-z])", sentence):
            found.append((match.start(), name))
    return sorted(found)


def selected_label(text: str, candidates: list[str]) -> str:
    picks: set[str] = set()
    for sentence in sentences(text):
        verb = next((sentence.find(item) for item in _SELECT if item in sentence), -1)
        if verb < 0 or any(item in sentence for item in _NEGATION):
            continue
        ids = _id_positions(sentence, candidates)
        before = [name for position, name in ids if position < verb]
        after = [name for position, name in ids if position >= verb]
        if before:
            picks.add(before[-1])
        elif after:
            picks.add(after[0])
    if len(picks) == 1:
        return picks.pop()
    if not picks and any(item in text for item in _NONE):
        return "none"
    return "ambiguous"


def verdict_label(text: str) -> str:
    if _UNSUITABLE.search(text):
        return "unsuitable"
    if _CONDITIONAL.search(text):
        return "conditional"
    if _SUITABLE.search(text):
        return "suitable"
    return "unclear"


def _claims_unknown_as_known(text: str, claims: list[dict[str, str]]) -> bool:
    for claim in claims:
        for sentence in sentences(text):
            subject_ok = not claim["subject"] or re.search(
                rf"(?<![0-9A-Za-z]){re.escape(claim['subject'])}(?![0-9A-Za-z])", sentence
            )
            if not subject_ok or claim["attr"] not in sentence:
                continue
            if any(item in sentence for item in _UNCERTAIN) or any(item in sentence for item in _NEGATION):
                continue
            if any(item in sentence for item in _POSITIVE):
                return True
    return False


_STATUS_TO_CLASS = {
    "suitable": "suitable",
    "conditionally_suitable": "conditional",
    "deferred": "conditional",
    "unsuitable": "unsuitable",
}


def grade(case: dict[str, Any], text: str, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    text = text or ""
    result: dict[str, Any] = {}
    if case["kind"] == "selection":
        label = selected_label(text, case["candidates"])
        result["label"] = label
        result["correct"] = label == case["expected"]
        result["required_violation"] = label in case["violating"]
        result["unknown_as_confirmed"] = label in case["unknown_only"] or _claims_unknown_as_known(
            text, case["unknown_claims"]
        )
        result["reasons_complete"] = all(
            re.search(rf"(?<![0-9A-Za-z]){re.escape(name)}(?![0-9A-Za-z])", text) for name in case["candidates"]
        )
        if structured and "selected" in structured:
            computed = structured["selected"] or "none"
            result["consistent"] = computed == label
    else:
        label = verdict_label(text)
        result["label"] = label
        result["correct"] = label == case["expected"]
        expected = case["expected"]
        result["required_violation"] = (expected == "unsuitable" and label in {"suitable", "conditional"}) or (
            expected == "suitable" and label == "unsuitable"
        )
        result["unknown_as_confirmed"] = _claims_unknown_as_known(text, case["unknown_claims"])
        result["reasons_complete"] = None
        if structured and structured.get("conclusion_status"):
            result["consistent"] = _STATUS_TO_CLASS.get(structured["conclusion_status"]) == label
    result.setdefault("consistent", None)
    return result
