"""Lossless model-copy reduction of old, exactly repeated reference-tool output."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from nanobot.session.history_visibility import is_hidden_history_message

MIN_DUPLICATE_CHARS = 2000
# File/execution/memory originals and unknown tools retain their evidence unchanged.
REFERENCE_TOOLS = frozenset({"web_search", "web_fetch", "list_dir", "find_files", "grep"})


def project_old_tool_duplicates(messages: list[dict[str, Any]],
                                historical_ids: frozenset[str]) -> list[dict[str, Any]]:
    """Keep the newest historical copy; fold only results before the two recent turns.

    Never change calls, user messages, persisted history, or current-run outputs. A reference
    is emitted only when identical content with the same tool and exact arguments remains
    in this final model input. No disk writes, summarization or recovery calls are needed.
    """
    if not historical_ids:
        return messages
    users = [i for i, message in enumerate(messages)
             if message.get("role") == "user" and not is_hidden_history_message(message)]
    if len(users) < 3:
        return messages
    cutoff = users[-2]
    calls = {}
    call_counts: Counter[str] = Counter()
    result_counts = Counter(message.get("tool_call_id") for message in messages
                            if message.get("role") == "tool"
                            and isinstance(message.get("tool_call_id"), str))
    candidates = []
    latest = {}
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                    continue
                call_counts[call["id"]] += 1
                function = call.get("function")
                if isinstance(function, dict):
                    calls[call["id"]] = (function.get("name"), function.get("arguments"))
        if message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        name = message.get("name")
        content = message.get("content")
        status = message.get("execution_status")
        if (not isinstance(call_id, str) or call_id not in historical_ids or not 0 < len(call_id) <= 128
                or not isinstance(name, str) or name not in REFERENCE_TOOLS or not isinstance(content, str)
                or len(content) < MIN_DUPLICATE_CHARS
                or message.get("read_only") is False
                or (status is not None and (not isinstance(status, str) or status not in {"ok", "success"}))
                or any(key in message for key in ("read_evidence", "memory_evidence", "memory_event_id",
                                                   "source_event_id", "evidence_id"))
                or hasattr(content, "read_evidence") or hasattr(content, "memory_evidence")):
            continue
        declaration = calls.get(call_id)
        if not declaration or declaration[0] != name or not isinstance(declaration[1], str):
            continue
        key = (name, declaration[1], hashlib.sha256(content.encode("utf-8")).digest())
        candidates.append((index, call_id, key))
        latest[key] = (index, call_id)

    updated = messages
    for index, call_id, key in candidates:
        retained, retained_id = latest[key]
        if (index >= cutoff or call_id not in historical_ids or index == retained
                or call_counts[call_id] != 1 or result_counts[call_id] != 1
                or call_counts[retained_id] != 1 or result_counts[retained_id] != 1
                or messages[index]["content"] != messages[retained]["content"]):
            continue
        if updated is messages:
            updated = list(messages)
        # A later hook may edit raw history; keep the referenced full string in this copy.
        if updated[retained] is messages[retained]:
            updated[retained] = dict(messages[retained])
        updated[index] = {**messages[index], "content": (
            f"[Duplicate prior {key[0]} output omitted; identical full content remains in "
            f"tool_call_id={json.dumps(retained_id)} in this input. "
            "This reference is not a new execution or fresh evidence.]")}
    return updated
