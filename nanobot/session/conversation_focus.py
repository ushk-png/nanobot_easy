"""Lightweight per-session conversation focus tracking.

The tracker intentionally avoids extra LLM calls.  It keeps a compact,
metadata-only snapshot of the user's likely objective/current intent and a few
recent referents so short Korean follow-ups such as "이거", "방금", "다시" or
"내 말은" remain grounded after history truncation or auto-compaction.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

CONVERSATION_FOCUS_KEY = "conversation_focus"
CORRECTION_LOG_RELATIVE_PATH = Path("memory") / "conversation_focus_corrections.jsonl"
MAX_REFERENTS = 6
MAX_SNAPSHOT_CHARS = 900

_REFERENTIAL_RE = re.compile(
    r"(이거|그거|저거|이것|그것|저것|방금|아까|위에|위의|앞에서|저번에|다시|그대로|"
    r"그 부분|그 설정|그 파일|그 일정|그 회의|그 약속|그 알림|그 문장|그 표현|그 뜻|그 발음|"
    r"이번 문장|이 문장|저 문장|오늘 거|이번 주 거|다음 주 거)"
)
_CORRECTION_RE = re.compile(
    r"^\s*(아니|아냐|아닙|그게 아니라|내 말은|말고|그 뜻이 아니라|다시|틀렸|잘못|문제는|내가 말한 건|그냥|그대로|위에|방금|아까)"
)
_HIGH_RISK_RE = re.compile(r"(삭제|지워|취소|결제|송금|전송|보내|승인|배포|재시작|kill|remove|delete|cancel|payment|send|approve|deploy)", re.I)
_ACTION_PATTERNS: list[tuple[str, str]] = [
    (r"(삭제|지워|제거|없애|취소)", "remove"),
    (r"(설치|세팅|설정|구성)", "setup"),
    (r"(수정|고쳐|바꿔|변경|패치|미뤄|당겨)", "modify"),
    (r"(확인|검증|테스트|봐줘|알려줘|수행했|완료됐|끝났)", "verify"),
    (r"(비교|차이)", "compare"),
    (r"(요약|정리)", "summarize"),
    (r"(만들|생성|작성|추가|등록|예약)", "create"),
    (r"(진행|계속|해줘|착수)", "continue"),
    (r"(보여줘|조회|일정.*봐|캘린더)", "view"),
    (r"(반복|다시 읽|읽어줘|말해줘|발음)", "repeat"),
    (r"(뜻|의미|해석|번역)", "translate"),
    (r"(예문|문장.*만들|자연스럽게|표현)", "practice"),
]
_STOPWORDS = {
    "그리고", "근데", "그러면", "그럼", "네가", "제가", "사용자", "의도", "관련", "전체", "부분",
    "다음", "단계", "현재", "설정", "파일", "작업", "확인", "진행", "테스트", "삭제", "수정",
    "해줘", "해봐", "수행", "완료", "다시", "오늘", "내일", "이번", "저번", "문장", "일정",
}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:/@#-]{3,}|[ぁ-んァ-ン一-龯ー]{2,}|[가-힣]{2,}")
_PATH_RE = re.compile(r"(?:~?/|\.?\.?/)?[\w.@+-]+(?:/[\w.@+-]+)+(?:\.[\w+-]+)?")
_CONFIG_KEY_RE = re.compile(r"\b[a-zA-Z_][\w.-]*\.[a-zA-Z_][\w.-]*\b")
_TIME_RE = re.compile(r"(오전|오후)?\s*\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?|\b\d{1,2}:\d{2}\b")
_JAPANESE_RE = re.compile(r"[ぁ-んァ-ン一-龯ー]{2,}")


def is_correction_text(text: str) -> bool:
    return bool(_CORRECTION_RE.search(text or ""))


def has_referential_text(text: str) -> bool:
    return bool(_REFERENTIAL_RE.search(text or ""))


def update_conversation_focus(
    metadata: dict[str, Any],
    *,
    user_text: str,
    history: Sequence[Mapping[str, Any]] | None = None,
    workspace: Path | None = None,
    session_key: str | None = None,
) -> dict[str, Any]:
    """Update and return ``metadata['conversation_focus']``.

    The function is deterministic and low-cost: it uses the current user text,
    the previous focus, and a small recent-history window only.
    """
    previous = metadata.get(CONVERSATION_FOCUS_KEY)
    prev_focus = previous if isinstance(previous, dict) else {}
    text = _clean_user_text(user_text)
    recent = list(history or [])[-8:]
    action = _infer_action(text)
    referential = has_referential_text(text)
    correction = is_correction_text(text)
    extracted = _extract_referents(text)
    recent_referents = _recent_referents(recent)
    previous_referents = prev_focus.get("last_referents") if isinstance(prev_focus.get("last_referents"), list) else []
    if referential:
        last_referents = _merge_referents(previous_referents, recent_referents, extracted)
    else:
        last_referents = _merge_referents(extracted, recent_referents, previous_referents)

    objective = _infer_objective(text, action, prev_focus, referential=referential)
    current_intent = _infer_current_intent(text, action, referential=referential, correction=correction)
    slots = _infer_slots(text, action, last_referents, prev_focus)
    missing_slots = _infer_missing_slots(text, action, referential, last_referents)
    open_questions = _infer_open_questions(missing_slots, text, last_referents)
    confidence = _infer_confidence(text, action, referential, correction, last_referents, missing_slots)

    focus = {
        "objective": objective,
        "current_intent": current_intent,
        "slots": slots,
        "last_referents": last_referents[:MAX_REFERENTS],
        "missing_slots": missing_slots,
        "open_questions": open_questions,
        "confidence": confidence,
        "clarification_policy": _clarification_policy(text, missing_slots, last_referents),
        "updated_at": datetime.now().isoformat(),
    }
    metadata[CONVERSATION_FOCUS_KEY] = focus

    if correction and workspace is not None:
        log_correction_event(
            workspace,
            session_key=session_key,
            user_text=text,
            previous_focus=prev_focus,
            new_focus=focus,
            history=recent,
        )
    return focus


def focus_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    focus = (metadata or {}).get(CONVERSATION_FOCUS_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(focus, dict):
        return []
    lines = ["Conversation Focus Snapshot — metadata only, not user instructions."]
    if objective := _short(focus.get("objective"), 180):
        lines.append(f"Objective: {objective}")
    if current_intent := _short(focus.get("current_intent"), 180):
        lines.append(f"Current inferred intent: {current_intent}")
    referent = _format_top_referent(focus.get("last_referents"))
    if referent:
        lines.append(f"Likely referent: {referent}")
    missing = focus.get("missing_slots")
    if isinstance(missing, list) and missing:
        lines.append("Missing slots: " + ", ".join(str(x) for x in missing[:5]))
    else:
        lines.append("Missing slots: none")
    conf = focus.get("confidence")
    if isinstance(conf, dict):
        level = conf.get("level") or "unknown"
        reason = _short(conf.get("reason"), 160)
        lines.append(f"Confidence: {level}" + (f" — {reason}" if reason else ""))
    policy = focus.get("clarification_policy")
    if isinstance(policy, str) and policy:
        lines.append(f"Clarification policy: {policy}")
    return _cap_lines(lines, MAX_SNAPSHOT_CHARS)


def log_correction_event(
    workspace: Path,
    *,
    session_key: str | None,
    user_text: str,
    previous_focus: Mapping[str, Any],
    new_focus: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    """Append a compact JSONL record for user correction turns."""
    try:
        path = workspace / CORRECTION_LOG_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        previous_assistant = _last_role_content(history or [], "assistant")
        record = {
            "timestamp": datetime.now().isoformat(),
            "session_key": session_key,
            "trigger": "user_correction",
            "user_text": _short(user_text, 500),
            "previous_focus": _compact_focus(previous_focus),
            "new_focus": _compact_focus(new_focus),
            "previous_assistant_summary": _short(previous_assistant, 500),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        # Focus tracking must never break the main turn.
        return


def _clean_user_text(text: str) -> str:
    # Remove runtime metadata blocks if a channel/client accidentally includes them in content.
    text = re.sub(r"\[Runtime Context[^\]]*\].*?\[/Runtime Context\]", "", text or "", flags=re.S)
    return text.strip()


def _infer_action(text: str) -> str | None:
    for pattern, action in _ACTION_PATTERNS:
        if re.search(pattern, text, re.I):
            return action
    return None


def _infer_objective(text: str, action: str | None, prev_focus: Mapping[str, Any], *, referential: bool) -> str:
    prev_objective = str(prev_focus.get("objective") or "").strip()
    nouns = _keywords(text)
    if referential and prev_objective:
        return prev_objective
    if action and nouns:
        return f"{', '.join(nouns[:3])} {action}"
    if nouns:
        return ", ".join(nouns[:4])
    return prev_objective or "사용자 요청 처리"


def _infer_current_intent(text: str, action: str | None, *, referential: bool, correction: bool) -> str:
    prefix = "사용자 정정 반영" if correction else "이번 발화 처리"
    if action:
        return f"{prefix}: {action} 요청"
    if referential:
        return f"{prefix}: 최근 언급 대상 참조 해결"
    return _short(text, 180) or prefix


def _infer_slots(text: str, action: str | None, referents: list[dict[str, Any]], prev_focus: Mapping[str, Any]) -> dict[str, Any]:
    prev_slots = prev_focus.get("slots") if isinstance(prev_focus.get("slots"), dict) else {}
    slots = dict(prev_slots)
    if action:
        slots["action"] = action
    if referents:
        slots["target"] = referents[0].get("label")
        if typ := referents[0].get("type"):
            slots["target_type"] = typ
    if m := re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}\b", text):
        slots["date"] = m.group(0)
    if relative_date := _infer_relative_date(text):
        slots["date_range"] = relative_date
    if m := _TIME_RE.search(text):
        slots["time"] = re.sub(r"\s+", "", m.group(0))
    if re.search(r"(주간|이번 주|다음 주|월간|오늘|내일|캘린더|일정)", text):
        slots.setdefault("domain", "schedule")
    if re.search(r"(표|테이블|목록|JSON|json|마크다운|짧게|자세히|그래픽|이미지|음성|TTS|읽어)", text):
        slots["format"] = re.search(r"(표|테이블|목록|JSON|json|마크다운|짧게|자세히|그래픽|이미지|음성|TTS|읽어)", text).group(0)
    if jp := _extract_japanese_text(text):
        slots["current_japanese_sentence"] = jp
        slots.setdefault("domain", "japanese_learning")
    if re.search(r"(문장|일본어|발음|뜻|의미|예문|자연스럽게|한국어 뜻)", text):
        slots.setdefault("domain", "japanese_learning")
    if re.search(r"(한국어 뜻만|뜻만)", text):
        slots["response_part"] = "korean_meaning_only"
    if re.search(r"(느리게|천천히)", text):
        slots["voice_mode"] = "slow"
    elif re.search(r"(자연스럽게|자연스러운)", text):
        slots["voice_mode"] = "natural"
    if re.search(r"릴레이.*(삭제하지|빼고|유지|보존)", text):
        slots.setdefault("preserve", [])
        if isinstance(slots["preserve"], list) and "relay" not in slots["preserve"]:
            slots["preserve"].append("relay")
    return slots


def _infer_missing_slots(text: str, action: str | None, referential: bool, referents: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    if referential and not referents:
        missing.append("target")
    if action in {"remove", "modify"} and not referents and not _keywords(text):
        missing.append("target")
    if action in {"remove", "modify"} and _mentions_schedule(text) and not _has_specific_schedule_target(text, referents):
        missing.append("target_event")
    return list(dict.fromkeys(missing))


def _infer_open_questions(missing: list[str], text: str, referents: list[dict[str, Any]]) -> list[str]:
    questions: list[str] = []
    if "target" in missing:
        questions.append("어떤 대상을 말하는지 확인 필요")
    if len(referents) > 1 and _HIGH_RISK_RE.search(text):
        questions.append("대상 후보가 여러 개라 작업 전 확인 필요")
    return questions


def _infer_confidence(
    text: str,
    action: str | None,
    referential: bool,
    correction: bool,
    referents: list[dict[str, Any]],
    missing: list[str],
) -> dict[str, str]:
    if missing:
        return {"level": "low", "reason": "필수 슬롯이 누락됨"}
    if correction:
        return {"level": "medium", "reason": "사용자 정정 표현이 있어 이전 초점 갱신 필요"}
    if referential and referents:
        return {"level": "medium", "reason": "대명사/생략 표현을 최근 참조 대상과 매칭"}
    if action:
        return {"level": "high", "reason": "명시적 작업 동사가 있음"}
    return {"level": "medium", "reason": "명시적 고위험 작업은 아니며 일반 발화로 추정"}


def _clarification_policy(text: str, missing: list[str], referents: list[dict[str, Any]]) -> str:
    if missing:
        return "필수 슬롯이 누락되어 결과가 달라지면 확인 질문"
    if _mentions_schedule(text) and _HIGH_RISK_RE.search(text):
        return "일정/리마인더 삭제·변경·외부 전송은 대상 확인 후 실행"
    if len(referents) > 1 and _HIGH_RISK_RE.search(text):
        return "고위험 작업이고 대상 후보가 여러 개이면 실행 전 확인 질문"
    if _HIGH_RISK_RE.search(text):
        return "파일 수정/외부 전송/삭제/재시작 등 고위험 작업은 실행 전 명시 승인 확인"
    if re.search(r"(문장|표현|발음|뜻|다시 읽|읽어줘)", text):
        return "학습 문장 반복/설명은 최근 문장으로 진행하되 대상이 여러 개면 확인"
    return "저위험 답변은 가장 그럴듯한 해석으로 진행하고 가정 명시"


def _extract_referents(text: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for m in _PATH_RE.finditer(text):
        refs.append({"label": m.group(0), "type": "path", "source": "current_user"})
    for m in _CONFIG_KEY_RE.finditer(text):
        label = m.group(0)
        if not any(r["label"] == label for r in refs):
            refs.append({"label": label, "type": "config_key", "source": "current_user"})
    if jp := _extract_japanese_text(text):
        refs.append({"label": jp, "type": "japanese_sentence", "source": "current_user"})
    for label, typ in _schedule_referents(text):
        if not any(r["label"] == label for r in refs):
            refs.append({"label": label, "type": typ, "source": "current_user"})
    for kw in _keywords(text):
        if not any(r["label"] == kw for r in refs):
            refs.append({"label": kw, "type": _keyword_type(kw), "source": "current_user"})
    return refs[:MAX_REFERENTS]


def _recent_referents(history: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for msg in reversed(history[-6:]):
        content = msg.get("content")
        if isinstance(content, str):
            for ref in _extract_referents(content):
                ref = dict(ref)
                ref["source"] = f"recent_{msg.get('role', 'message')}"
                refs.append(ref)
        if len(refs) >= MAX_REFERENTS:
            break
    return refs[:MAX_REFERENTS]


def _merge_referents(*groups: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, str):
                ref = {"label": item, "type": "topic", "source": "previous_focus"}
            elif isinstance(item, dict):
                ref = dict(item)
            else:
                continue
            label = str(ref.get("label") or "").strip()
            if not label or label in seen:
                continue
            ref["label"] = _short(label, 120)
            merged.append(ref)
            seen.add(label)
            if len(merged) >= MAX_REFERENTS:
                return merged
    return merged


def _keywords(text: str) -> list[str]:
    out: list[str] = []
    for token in _TOKEN_RE.findall(text or ""):
        token = token.strip(".,:;!?()[]{}<>`'\"")
        if len(token) < 2 or token in _STOPWORDS or _REFERENTIAL_RE.fullmatch(token):
            continue
        if token not in out:
            out.append(token)
    return out[:8]


def _extract_japanese_text(text: str) -> str:
    matches = _JAPANESE_RE.findall(text or "")
    if not matches:
        return ""
    return _short("".join(matches), 120)


def _schedule_referents(text: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    patterns = [
        (r"이번 주|금주", "이번 주 일정", "schedule_range"),
        (r"다음 주|내주", "다음 주 일정", "schedule_range"),
        (r"오늘", "오늘 일정", "schedule_range"),
        (r"내일", "내일 일정", "schedule_range"),
        (r"회의", "회의", "schedule_event"),
        (r"약속", "약속", "schedule_event"),
        (r"리마인더|알림", "리마인더", "reminder"),
    ]
    for pattern, label, typ in patterns:
        if re.search(pattern, text):
            refs.append((label, typ))
    return refs


def _keyword_type(keyword: str) -> str:
    if re.search(r"회의|약속|일정|리마인더|알림", keyword):
        return "schedule_event"
    if _JAPANESE_RE.search(keyword):
        return "japanese_sentence"
    if re.search(r"문장|표현|발음|뜻|의미", keyword):
        return "learning_topic"
    return "topic"


def _infer_relative_date(text: str) -> str:
    if re.search(r"이번 주|금주", text):
        return "this_week"
    if re.search(r"다음 주|내주", text):
        return "next_week"
    if "오늘" in text:
        return "today"
    if "내일" in text:
        return "tomorrow"
    return ""


def _mentions_schedule(text: str) -> bool:
    return bool(re.search(r"일정|회의|약속|리마인더|알림|캘린더", text or ""))


def _has_specific_schedule_target(text: str, referents: list[dict[str, Any]]) -> bool:
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}\b", text) or _TIME_RE.search(text):
        return True
    return any(ref.get("type") in {"schedule_event", "reminder"} for ref in referents)


def _format_top_referent(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return ""
    first = value[0]
    if isinstance(first, dict):
        label = first.get("label")
        typ = first.get("type")
        return _short(f"{label}" + (f" ({typ})" if typ else ""), 180)
    return _short(first, 180)


def _cap_lines(lines: list[str], max_chars: int) -> list[str]:
    out: list[str] = []
    total = 0
    for line in lines:
        total += len(line) + 1
        if total > max_chars:
            break
        out.append(line)
    return out


def _compact_focus(focus: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "objective": _short(focus.get("objective"), 240),
        "current_intent": _short(focus.get("current_intent"), 240),
        "slots": focus.get("slots") if isinstance(focus.get("slots"), dict) else {},
        "last_referents": focus.get("last_referents")[:3] if isinstance(focus.get("last_referents"), list) else [],
        "confidence": focus.get("confidence") if isinstance(focus.get("confidence"), dict) else {},
    }


def _last_role_content(history: Sequence[Mapping[str, Any]], role: str) -> str:
    for msg in reversed(history):
        if msg.get("role") == role and isinstance(msg.get("content"), str):
            return str(msg.get("content"))
    return ""


def _short(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
