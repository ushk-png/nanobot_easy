"""Session metadata helpers for sustained goals (e.g. ``long_task`` / ``complete_goal``).

Tools set ``metadata[GOAL_STATE_KEY]``. Reads accept the legacy session key ``thread_goal``
for older sessions. Callers use ``goal_state_runtime_lines``, ``goal_state_ws_blob``, and
``runner_wall_llm_timeout_s`` without importing tool implementations.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, MutableMapping

from nanobot.session.manager import SessionManager

GOAL_STATE_KEY = "goal_state"
# Older builds stored the same JSON blob under this key.
_LEGACY_GOAL_STATE_SESSION_KEY = "thread_goal"
_MAX_OBJECTIVE_IN_RUNTIME = 4000
_MAX_OBJECTIVE_WS = 600
_MANUAL_APPROVAL_MARKERS = (
    "explicit user instruction",
    "explicit user approval",
    "explicit approval",
    "exact explicit approval",
    "only when the user",
    "only after confirming",
    "after confirming",
    "after user confirms",
    "after user approval",
    "wait for user",
    "wait for the user",
    "wait for approval",
    "wait for explicit approval",
    "user confirms",
    "user approval",
    "approval gate",
    "manual approval",
    "implementation approval",
    "approval before",
    "before proceeding",
    "before editing",
    "before editing files",
    "before creating drafts",
    "사용자 승인",
    "사용자 지시",
    "사용자가",
    "명시적",
    "승인",
    "승인 대기",
    "승인 전",
    "승인 전까지",
    "구현 승인",
    "확인 후",
    "확인한 후",
    "진행 문구",
)
_STAGED_WORK_MARKERS = (
    "step",
    "steps",
    "later",
    "next",
    "proceed",
    "continue",
    "단계",
    "다음",
    "진행",
    "수행",
)
_STRONG_MANUAL_APPROVAL_MARKERS = (
    "only after confirming",
    "after user approval",
    "wait for explicit approval",
    "approval gate",
    "implementation approval",
    "before editing files",
    "before creating drafts",
    "승인 대기",
    "승인 전까지",
    "구현 승인",
    "진행 문구",
)


def _session_goal_raw(metadata: Mapping[str, Any] | None) -> Any:
    if not metadata:
        return None
    if GOAL_STATE_KEY in metadata:
        return metadata.get(GOAL_STATE_KEY)
    return metadata.get(_LEGACY_GOAL_STATE_SESSION_KEY)


def discard_legacy_goal_state_key(metadata: MutableMapping[str, Any]) -> None:
    """Remove legacy metadata key after migrating writes to :data:`GOAL_STATE_KEY`."""
    metadata.pop(_LEGACY_GOAL_STATE_SESSION_KEY, None)


def goal_state_raw(metadata: Mapping[str, Any] | None) -> Any:
    """Return the session goal blob under :data:`GOAL_STATE_KEY` or the legacy key."""
    return _session_goal_raw(metadata)


def sustained_goal_active(metadata: Mapping[str, Any] | None) -> bool:
    """True when this session has an active sustained objective (``long_task`` bookkeeping)."""
    goal = parse_goal_state(goal_state_raw(metadata))
    return isinstance(goal, dict) and goal.get("status") == "active"


def sustained_goal_waits_for_user(metadata: Mapping[str, Any] | None) -> bool:
    """True when an active goal is parked behind an explicit user approval gate.

    This is intentionally conservative and only detects staged/manual-approval
    goals. Those goals remain visible in runtime metadata, but they must not
    trigger synthetic long-goal continuation turns that could execute a later
    step without the user's explicit instruction.
    """
    goal = parse_goal_state(goal_state_raw(metadata))
    if not isinstance(goal, dict) or goal.get("status") != "active":
        return False
    if goal.get("user_approval_received") is True:
        return False
    if goal.get("requires_user_approval") is True:
        return True
    objective = str(goal.get("objective") or "").casefold()
    if not objective:
        return False
    return objective_requires_user_approval(objective)


def objective_requires_user_approval(objective: str) -> bool:
    """Detect objectives that are parked behind an explicit user approval gate.

    ``long_task`` goals are free-form model-authored text, so this intentionally
    remains heuristic. The key behavior is conservative execution: when the
    objective itself says later work needs confirmation, internal continuation
    must not keep asking or accidentally proceed.
    """
    text = str(objective or "").casefold()
    if not text:
        return False
    if "그록 빌드로 진행" in text:
        return True
    if any(marker.casefold() in text for marker in _STRONG_MANUAL_APPROVAL_MARKERS):
        return True
    has_manual_gate = any(marker.casefold() in text for marker in _MANUAL_APPROVAL_MARKERS)
    if not has_manual_gate:
        return False
    return any(marker.casefold() in text for marker in _STAGED_WORK_MARKERS)


def message_confirms_sustained_goal(message: str) -> bool:
    """Return true when a user message is an explicit proceed/approval reply."""
    text = " ".join(str(message or "").casefold().split())
    if not text:
        return False
    exact = {
        "그록 빌드로 진행해",
        "그록 빌드로 진행해줘",
        "진행해",
        "진행해줘",
        "계속해",
        "계속 진행해",
        "구현해",
        "구현해줘",
        "승인",
        "승인해",
        "승인해줘",
        "proceed",
        "continue",
        "approved",
        "go ahead",
    }
    if text in exact:
        return True
    return text.endswith("를 수행해줘") or text.endswith("을 수행해줘")


def mark_sustained_goal_user_approval(
    metadata: MutableMapping[str, Any] | None,
    message: str,
) -> bool:
    """Mark an approval-gated goal as approved when the user sends approval.

    Returns true when metadata was changed. This keeps the approval gate
    structural: automatic continuation is blocked before approval and allowed
    again after an explicit proceed reply.
    """
    if metadata is None or not message_confirms_sustained_goal(message):
        return False
    goal = parse_goal_state(goal_state_raw(metadata))
    if not isinstance(goal, dict) or goal.get("status") != "active":
        return False
    if goal.get("user_approval_received") is True:
        return False
    if not sustained_goal_waits_for_user(metadata):
        return False
    goal["user_approval_received"] = True
    goal["requires_user_approval"] = False
    goal["approval_message"] = str(message or "").strip()[:200]
    metadata[GOAL_STATE_KEY] = goal
    discard_legacy_goal_state_key(metadata)
    return True


def sustained_goal_turn(
    metadata: Mapping[str, Any] | None,
    *,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """True when this turn should use sustained-goal runtime limits."""
    if sustained_goal_active(metadata):
        return True
    if not message_metadata:
        return False
    return str(message_metadata.get("original_command") or "").strip() == "/goal"


def parse_goal_state(blob: Any) -> dict[str, Any] | None:
    if blob is None:
        return None
    if isinstance(blob, dict):
        return blob
    if isinstance(blob, str):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def goal_state_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    """Lines appended inside the Runtime Context block when a goal is active."""
    if not metadata:
        return []
    goal = parse_goal_state(_session_goal_raw(metadata))
    if not isinstance(goal, dict) or goal.get("status") != "active":
        return []
    objective = str(goal.get("objective") or "").strip()
    if not objective:
        return ["Goal: active (no objective text stored)."]
    if len(objective) > _MAX_OBJECTIVE_IN_RUNTIME:
        objective = objective[:_MAX_OBJECTIVE_IN_RUNTIME].rstrip() + "\n… (truncated)"
    if sustained_goal_waits_for_user(metadata):
        out = [
            "Goal (waiting for explicit user approval):",
            objective,
            (
                "Do not continue this goal, use tools for it, or repeat an approval prompt "
                "unless the user's current message explicitly approves/proceeds."
            ),
        ]
    else:
        out = ["Goal (active):", objective]
        if goal.get("user_approval_received") is True:
            out.append(
                "A previous explicit approval gate for this goal was approved by the user. "
                "Do not ask for that same approval again. If a later action needs a new "
                "safety approval, ask once for that specific action only."
            )
    hint = str(goal.get("ui_summary") or "").strip()
    if hint:
        out.append(f"Summary: {hint}")
    return out


def goal_state_ws_blob(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """JSON-safe snapshot for WebSocket ``goal_state`` events (one chat_id per frame)."""
    goal = parse_goal_state(_session_goal_raw(metadata)) if metadata else None
    if isinstance(goal, dict) and goal.get("status") == "active":
        objective = str(goal.get("objective") or "").strip()
        if len(objective) > _MAX_OBJECTIVE_WS:
            objective = objective[:_MAX_OBJECTIVE_WS].rstrip() + "…"
        summary = str(goal.get("ui_summary") or "").strip()[:120]
        blob: dict[str, Any] = {
            "active": True,
            "waiting_for_user": sustained_goal_waits_for_user(metadata),
        }
        if summary:
            blob["ui_summary"] = summary
        if objective:
            blob["objective"] = objective
        return blob
    return {"active": False}


def runner_wall_llm_timeout_s(
    sessions: SessionManager,
    session_key: str | None,
    *,
    metadata: Mapping[str, Any] | None = None,
    message_metadata: Mapping[str, Any] | None = None,
) -> float | None:
    """Wall-clock cap for :class:`~nanobot.agent.runner.AgentRunner` when streaming an LLM.

    Returns ``0.0`` to disable ``asyncio.wait_for`` around the request when this is a
    sustained-goal turn; ``None`` means use ``NANOBOT_LLM_TIMEOUT_S``. Pass in-memory
    ``metadata`` when the caller already holds :attr:`~nanobot.session.manager.Session.metadata`
    for this turn.
    """
    meta: Mapping[str, Any] | None = metadata
    if meta is None and session_key:
        meta = sessions.get_or_create(session_key).metadata
    return 0.0 if sustained_goal_turn(meta, message_metadata=message_metadata) else None
