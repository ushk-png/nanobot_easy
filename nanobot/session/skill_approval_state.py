"""Session metadata helpers for pending skill-draft approval confirmations.

The ``skill_request_approval`` tool sets ``metadata[PENDING_SKILL_APPROVAL_KEY]``
when the agent wants to ask, in plain chat, whether to register a skill draft.
The next inbound message is checked in ``AgentLoop._state_command`` — before the
LLM ever runs for that turn — against :func:`parse_confirmation_reply`. Only a
real, exact yes/no reply from the user resolves it; the LLM never decides on its
own that the user approved, so a prompt-injected "yes" inside pasted content
cannot forge consent (the entire message must equal a short confirmation word).
"""

from __future__ import annotations

import time
from typing import Any, Literal, MutableMapping

PENDING_SKILL_APPROVAL_KEY = "pending_skill_approval"
_DEFAULT_TTL_S = 600  # 10 minutes, matching the pairing-code TTL convention.

_AFFIRMATIVE = {
    "yes", "y", "ok", "okay", "confirm", "confirmed", "approve", "approved", "sure",
    "네", "예", "응", "그래", "그래요", "승인", "승인해줘", "승인합니다", "승인해",
}
_NEGATIVE = {
    "no", "n", "nope", "cancel", "cancelled", "canceled", "deny", "denied", "reject", "rejected",
    "아니", "아니요", "아니오", "취소", "안해", "안할래", "거부", "거절",
}
_STRIP_CHARS = ".!?~ 。！？〜"


def set_pending_skill_approval(
    metadata: MutableMapping[str, Any],
    *,
    name: str,
    source: str,
    draft_id: str | None = None,
    ttl_s: int = _DEFAULT_TTL_S,
) -> None:
    """Record a pending confirmation on the session, overwriting any prior one."""
    metadata[PENDING_SKILL_APPROVAL_KEY] = {
        "name": name,
        "source": source,
        "draft_id": draft_id,
        "expires_at": time.time() + ttl_s,
    }


def get_pending_skill_approval(metadata: MutableMapping[str, Any] | None) -> dict[str, Any] | None:
    """Return the pending confirmation, clearing it in place if expired or malformed."""
    if not metadata:
        return None
    pending = metadata.get(PENDING_SKILL_APPROVAL_KEY)
    if not isinstance(pending, dict) or not pending.get("name"):
        return None
    try:
        expires_at = float(pending.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if expires_at < time.time():
        metadata.pop(PENDING_SKILL_APPROVAL_KEY, None)
        return None
    return pending


def clear_pending_skill_approval(metadata: MutableMapping[str, Any]) -> None:
    metadata.pop(PENDING_SKILL_APPROVAL_KEY, None)


def parse_confirmation_reply(text: str) -> Literal["yes", "no"] | None:
    """Match a short, standalone yes/no reply; ``None`` for anything else.

    Intentionally strict: the whole message (after trimming punctuation) must
    equal a known confirmation word. A long pasted message that happens to
    *contain* "yes" somewhere never matches, so it cannot be misread as consent.
    """
    normalized = text.strip().strip(_STRIP_CHARS).strip().lower()
    if not normalized:
        return None
    if normalized in _AFFIRMATIVE:
        return "yes"
    if normalized in _NEGATIVE:
        return "no"
    return None
