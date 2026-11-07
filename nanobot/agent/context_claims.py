"""t52: change claims in a final answer, checked against this turn's real execution records.

A claim is a sentence of the final answer that says a state change happened (saved / modified /
deleted / committed ...). The sentence detector is only a CANDIDATE hint; what decides is the
binding of the claim to a target and the classification of that target from the real records of
this request (observations captured by the executor, completion reports), never from ids the model
writes into the answer. Quoted text is content, not a claim; a negated sentence is not a claim.

Statuses of a target (this request only):
  verified                 a successful change, and a passed completion report or a later successful
                           read of the same target in this request
  done_unverified          a successful change without such a confirmation
  verified_without_change  no change, but a passed report / read confirms the target this request
  unknown                  the change ran and its outcome was lost (transport / timeout)
  not_done                 refused, failed, or no attempt in this request
  prior_turn_only          only an earlier request changed it (never this turn's work)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from nanobot.agent.context_relations import _FILE, _QUOTED

UNKNOWN_KINDS = {"transport", "timeout"}
SUPPORTED = {"verified", "done_unverified"}

_QUOTES = re.compile(_QUOTED.pattern + r"|『[^』\n]*』|《[^》\n]*》")
# Past-tense change predicates (Korean stems + past endings; English participles). A hint only.
_CHANGE_STEM = (r"저장|기록|작성|생성|수정|변경|반영|적용|삭제|제거|커밋|푸시|덮어\s?썼|덮어\s?쓰|업데이트|추가|"
                r"남겨|만들|고치|고쳤|바꾸|바꿨|지웠|써\s?두|써\s?놓|넣어")
_PAST = r"(?:했|하였|해\s?(?:두었|뒀|놓았|놨)|었|았|두었|뒀|놓았|놨|됐|되었|함\b|완료)"
_CLAIM = re.compile(rf"(?:{_CHANGE_STEM})\S{{0,6}}?\s?{_PAST}|"
                    r"\b(?:saved|wrote|written|created|modified|updated|deleted|removed|committed|pushed|overwrote)\b",
                    re.I)
_NEG = re.compile(r"않|못|없|아직|안\s?(?:했|됐|되)|말고|대신|실패|보류|불가|\b(?:not|n't|never|failed|unable)\b",
                  re.I)
_SENT = re.compile(r"(?<=[.!?。])\s+|\n+")


def claim_sentences(text: str) -> list[str]:
    """Candidate claim sentences outside quotes and not negated (hint, not a decision)."""
    out = []
    for raw in _SENT.split(text or ""):
        blank = _QUOTES.sub(lambda m: " " * len(m.group(0)), raw)
        if not raw.strip() or not _CLAIM.search(blank) or _NEG.search(blank):
            continue
        out.append(raw.strip())
    return out


def _same(resource: str | None, target: str) -> bool:
    if not resource:
        return False
    r = str(resource).split(":", 1)[-1] if str(resource).startswith("file_change:") else str(resource)
    return r == target or Path(r).name == Path(target).name


def this_request(o: dict, rid: str) -> bool:
    """An observation of this request (prior-segment observations are flagged historical)."""
    return not o.get("historical") and o.get("request_id", rid) == rid


def classify(selected: Any, target: str) -> tuple[str, list[str]]:
    """Status of one target from this request's real records, plus the record ids it rests on."""
    rid = selected.trace.request_id
    this = [{**o, "tool_call_id": cid} for cid, o in selected.observations.items() if this_request(o, rid)]
    changes = [o for o in this if not o.get("read_only", True) and _same(o.get("resource_id"), target)]
    ok = [o for o in changes if o.get("status") == "ok" and o.get("executed", True) is not False]
    order = list(selected.observations)
    reports = [r for r in selected.completion_reports.values() if r.get("status") == "passed"]

    def confirmed(after: str | None) -> list[str]:
        ids = []
        for r in reports:
            cited = [c for c in r.get("tool_call_ids", []) if c in selected.observations
                     and this_request(selected.observations[c], rid)]
            if any(_same(selected.observations[c].get("resource_id"), target) for c in cited):
                ids.extend(cited)
        start = order.index(after) + 1 if after in order else 0
        ids += [cid for cid in order[start:] if (o := selected.observations[cid]).get("read_only")
                and this_request(o, rid) and o.get("status") == "ok"
                and _same(o.get("resource_id"), target) and after is not None]
        return list(dict.fromkeys(ids))

    if ok:
        last = ok[-1]["tool_call_id"]
        ids = confirmed(last)
        return ("verified", list(dict.fromkeys([last, *ids]))) if ids else ("done_unverified", [last])
    unknown = [o for o in changes if o.get("failure_class") in UNKNOWN_KINDS and o.get("executed", True) is not False]
    if unknown:
        return "unknown", [o["tool_call_id"] for o in unknown]
    if not changes:
        ids = confirmed(None)
        if ids:
            return "verified_without_change", ids
        prior = [e["tool_call_id"] for e in (selected.state or {}).get("executions", []) if e.get("status") == "ok"
                 and e.get("request_id") and e.get("request_id") != rid and _same(e.get("target"), target)]
        # Session file changes before this request (TOOL_RESULT / FILE_EDIT events, any task).
        prior += [a["event_id"] for a in (getattr(selected, "artifacts", None) or [])
                  if a.get("kind") in {"created", "modified"} and a.get("full_path")
                  and _same(a.get("full_path"), target)]
        if prior:
            return "prior_turn_only", list(dict.fromkeys(prior))
    return "not_done", [o["tool_call_id"] for o in changes]


def change_targets(selected: Any) -> list[str]:
    """Targets this request is about: real change records, required change results, and files the
    request names that this request did not only read. Candidates for an unnamed claim."""
    rid = selected.trace.request_id
    out = [str(o["resource_id"]) for o in selected.observations.values()
           if this_request(o, rid) and not o.get("read_only", True) and o.get("resource_id")]
    try:
        out += [r["target"] for r in selected.required_results() if r.get("target")
                and r["kind"] in {"file_change", "external_change"}]
    except Exception:
        pass
    read = {Path(str(o.get("resource_id"))).name for o in selected.observations.values()
            if this_request(o, rid) and o.get("read_only")}
    for name in _FILE.findall(_QUOTES.sub(" ", selected.request_text or "")):
        if Path(name).name not in read:
            out.append(name)
    names: dict[str, str] = {}
    for t in out:
        names.setdefault(Path(t).name, t)
    return list(names.values())


def check_claims(selected: Any, text: str) -> list[dict]:
    """Every candidate claim with its bound target and record-based status."""
    if not text or not getattr(selected, "trace", None):
        return []
    candidates = change_targets(selected)
    instruction = getattr(selected, "instruction_version", None)
    out = []
    for sentence in claim_sentences(text):
        blank = _QUOTES.sub(" ", sentence)
        named = list(dict.fromkeys(_FILE.findall(blank)))
        if not named:
            if len(candidates) == 1:
                named = [candidates[0]]
            elif not candidates:
                continue   # no change target in this request at all: prose about the answer itself
            elif all(classify(selected, c)[0] in SUPPORTED for c in candidates):
                named = list(candidates)   # an unnamed claim over several targets, each one supported
            else:
                out.append({"sentence": sentence[:200], "target": None, "status": "target_unbound",
                            "records": [], "candidates": [Path(c).name for c in candidates],
                            "instruction_version": instruction})
                continue
        for target in named:
            status, records = classify(selected, target)
            out.append({"sentence": sentence[:200], "target": Path(target).name, "status": status,
                        "records": records, "instruction_version": instruction,
                        "requirement": next((r["kind"] for r in _safe_required(selected)
                                             if r.get("target") and _same(r["target"], target)), None)})
    return out


def _safe_required(selected: Any) -> list[dict]:
    try:
        return selected.required_results()
    except Exception:
        return []


def unsupported(claims: list[dict]) -> list[dict]:
    return [c for c in claims if c["status"] not in SUPPORTED]


_LABEL = {"verified": "수행 및 확인 완료", "done_unverified": "수행했으나 확인 미완료",
          "verified_without_change": "변경 없이 확인됨(이번 요청에서 변경하지 않음)",
          "unknown": "결과 불명(실행 응답 유실)", "not_done": "수행되지 않음",
          "prior_turn_only": "이번 요청에서는 수행되지 않음(이전 요청의 기록만 있음)",
          "target_unbound": "대상 불명"}


RETRACT_OK = SUPPORTED | {"verified_without_change"}


def status_block(selected: Any, claims: list[dict]) -> str:
    """The status lines, assembled from records only (never from the answer's wording)."""
    targets: dict[str, tuple[str, list[str]]] = {}
    for t in change_targets(selected):
        targets[Path(t).name] = classify(selected, t)
    for c in claims:
        if c.get("target"):
            targets.setdefault(c["target"], (c["status"], c["records"]))
    lines = [f"- {name}: {_LABEL[s]}" + (f" (기록: {', '.join(ids)})" if ids else "")
             for name, (s, ids) in targets.items()]
    return "[실행 상태(기록 기준)]\n" + "\n".join(lines) if lines else ""


def scrub(text: str, claims: list[dict]) -> str:
    """Remove unsupported claim sentences from a shown candidate (never rewrite them to a negative)."""
    for c in unsupported(claims):
        if not c.get("sentence"):   # a carried retraction flag has no sentence in this text
            continue
        text = text.replace(c["sentence"], "[기록으로 확인되지 않은 변경 주장 생략]")
    return text
