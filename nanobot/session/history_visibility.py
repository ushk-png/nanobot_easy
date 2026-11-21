"""Visibility helpers for persisted session history messages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from nanobot.session.automation_turns import is_automation_history_message

HIDDEN_HISTORY_META = "_hidden_history"


def _has_hidden_history_marker(message: Mapping[str, Any] | None) -> bool:
    if not message:
        return False
    marker = message.get(HIDDEN_HISTORY_META)
    return marker is True or isinstance(marker, Mapping)


def is_hidden_history_message(message: Mapping[str, Any] | None) -> bool:
    """True for persisted messages that should not be shown as chat turns."""
    return _has_hidden_history_marker(message) or is_automation_history_message(message)


def is_task_control_message(message: Mapping[str, Any]) -> bool:
    """Identify obsolete per-request guidance without hiding literal user text."""
    return (
        message.get("role") == "user"
        and _has_hidden_history_marker(message)
        and (message.get("_task_control") is True
             or str(message.get("content", "")).startswith(
                 ("[Task evidence control;", "Completion defects (all): ")))
    )


def task_result_for_replay(message: Mapping[str, Any]) -> Any:
    """Keep old control-call identities and outcomes; read proofs stay in the task ledger."""
    content = message.get("content", "")
    if message.get("role") != "tool" or message.get("name") != "update_task":
        return content
    try:
        payload = json.loads(content)
    except (TypeError, ValueError, RecursionError):
        return content
    if (not isinstance(payload, dict) or not isinstance(payload.get("request_id"), str)
            or not isinstance(payload.get("evidence"), list) or not payload["evidence"]):
        return content
    payload["evidence"] = []
    return json.dumps(payload, ensure_ascii=False)
