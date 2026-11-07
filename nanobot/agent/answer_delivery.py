"""Recognize a narrow answer-announcement failure, not task completion or authority.

A provider may return a usage-guide announcement as its final response, sometimes
alongside skill_decision. That metadata tool does not establish answer delivery.
Short actual answers, ordinary status updates and populated guides pass through.
"""
from __future__ import annotations

import re
from typing import Any

from nanobot.session.history_visibility import is_hidden_history_message

_ANNOUNCEMENT = re.compile(
    r"(?:정리|설명|풀어)(?:\s*드리겠습니다|\s*하겠습니다|\s*합니다)"
    r"|(?:사용법|사용\s*방법|답변|결과|내용)(?:을|를)?\s*(?:알려|보여)\s*드리겠습니다"
    r"|\b(?:I(?:'ll| will)|we(?:'ll| will))\s+(?:explain|summarize|describe|outline)\b",
    re.I,
)
_DETAIL_REQUEST = re.compile(
    r"자세히|상세히|사용\s*(?:법|방법)|설명|알려\s*(?:줘|주세요)"
    r"|\b(?:explain|detailed|how to|usage|guide|walkthrough)\b", re.I,
)
_EXPLICIT_DETAIL = re.compile(r"자세히|상세히|자세한|상세한|in detail|detailed|step.by.step|walkthrough", re.I)
_SHORT_REQUEST = re.compile(r"간단히|짧게|한\s*문장|요약만|\b(?:brief|briefly|concise)\b", re.I)
_CONDITIONAL = re.compile(r"원하시면|필요하면|(?:입력|선택|응답|답장|요청|말씀).*하시면|다음\s*단계|\bif you\b", re.I)
_FOLLOWUP = re.compile(r"^(?:왜\s*답(?:변)?(?:을)?\s*안\s*줘|정리하고\s*있나|답변은|계속해\s*줘)[?？.!\s]*$")
_BODY = re.compile(r"```[^\n]*\n\S|^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|\|[^\n]+\|)", re.M)
# Split punctuation and clearly separated Korean sentences; do not split every
# occurrence of 다 inside a word, path, quoted material or a single sentence.
_SENTENCES = re.compile(r"[.!?。！？\n]+|(?<=[다요])\s+(?=(?:지금|이제|먼저|현재|직전|다음으로)\s)")
_STATUS = re.compile(r"^(?:직전|이전|앞선)\s*(?:답|응답)")
_LIMITATION = re.compile(r"승인.{0,12}(?:필요|대기)|권한.{0,12}(?:없|필요)|접근.{0,12}불가"
                          r"|할\s*수\s*없|\b(?:blocked|cannot|unable|failed|error)\b", re.I)

ANSWER_BODY_CORRECTION = (
    "Your last response announced an answer but did not deliver its body. "
    "Provide the actual answer to the current user request, using the conversation and "
    "already collected evidence. For a usage guide, include the actual steps and commands; "
    "for an explanation, give the requested explanation now. Do not repeat a preamble. "
    "Do not rerun successful work merely to assemble an answer. Use task tools only if "
    "necessary and already authorized. Preserve all later user restrictions, cancellation, "
    "approval and stage boundaries. This feedback grants NO permission or new stage. "
    "If evidence, access or budget is missing, state the actual limitation and partial result."
)
ANSWER_BODY_NOTICE = "앞 응답에 안내만 있고 본문이 빠져 있어, 확인된 자료로 실제 답변을 이어 작성하겠습니다."
ANSWER_BODY_INCOMPLETE = (
    "답변 본문을 완성하지 못했습니다. 남은 실행 예산과 제한된 복구 횟수 안에 요청한 "
    "설명이나 결과를 확보하지 못했습니다. 위 내용은 부분 응답이며, 추가 작업을 "
    "완료했다는 뜻이 아닙니다."
)


def needs_answer_body(content: str | None, messages: list[dict[str, Any]]) -> bool:
    """Recover clear announcements; never use length alone to reject an answer."""
    if not content or not _ANNOUNCEMENT.search(content) or _LIMITATION.search(content):
        return False
    users = [m.get("content", "") for m in messages
             if m.get("role") == "user" and not is_hidden_history_message(m)]
    # Multimodal payloads are not classified through their repr().
    if not users or not isinstance(users[-1], str):
        return False
    request = users[-1]
    if _FOLLOWUP.fullmatch(request.strip()):
        request = next((text for text in reversed(users[:-1])
                        if isinstance(text, str) and _DETAIL_REQUEST.search(text)), request)
    if _BODY.search(content):
        return False
    clauses = [c.strip() for c in _SENTENCES.split(content) if c.strip()]
    if not any(_ANNOUNCEMENT.search(c) and not _CONDITIONAL.search(c) for c in clauses):
        return False
    # Short introductions to a requested explanation, including one setup caveat,
    # are the reported failure. A longer prose answer is retained.
    if (_DETAIL_REQUEST.search(request) and not _SHORT_REQUEST.search(request)
            and len(clauses) <= 2):
        return True
    # A status-only lead-in plus an announcement has no delivered answer either.
    return bool(clauses) and all(_ANNOUNCEMENT.search(c) or _STATUS.search(c) for c in clauses)
