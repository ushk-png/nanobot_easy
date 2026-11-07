"""Correction records reach every next-LLM input (selective context only).

A correction is a separate SYSTEM_EVENT (``kind=assistant_correction``, id ``correction:<orig>``,
``parent_event_id=<orig>``). The original event is never edited. Every consumer that shows an earlier
assistant answer to a model uses the latest valid state:

* recent dialogue / acquired evidence records (``overlay_records``),
* memory tool output: search_memory, read_memory_events (``correction_notes`` appended; originals
  stay verbatim above it),
* session messages for legacy-fallback replay and summaries (``annotate_session``: a
  ``correction`` field next to the unchanged ``content``; history replay and consolidation prefer it).

Without correction events (legacy mode never writes them) all functions are no-ops.
"""
from __future__ import annotations

from typing import Any

CORRECTION_EVENT_TYPE = "SYSTEM_EVENT"
CORRECTION_KIND = "assistant_correction"
CORRECTION_PREFIX = "correction:"


def _meta(event: Any) -> dict:
    from nanobot.memory.event_store import ConversationEventStore
    return ConversationEventStore._metadata_obj(getattr(event, "metadata_json", None))


def is_correction(event: Any) -> bool:
    return (event.event_type == CORRECTION_EVENT_TYPE and bool(event.parent_event_id)
            and _meta(event).get("kind") == CORRECTION_KIND)


def corrections_for(store: Any, scope: Any, event_ids: list[str]) -> dict[str, dict]:
    """{original_event_id: {correction_event_id, answer, corrected_status, original_status}} (scoped read)."""
    ids = [str(i) for i in dict.fromkeys(event_ids) if i and not str(i).startswith(CORRECTION_PREFIX)]
    if not ids or store is None or scope is None:
        return {}
    try:
        found = store.get_events(scope, [CORRECTION_PREFIX + i for i in ids])
    except Exception:
        return {}
    out = {}
    for event in found:
        meta = _meta(event)
        if is_correction(event) and meta.get("answer"):
            out[event.parent_event_id] = {"correction_event_id": event.event_id, "answer": meta["answer"],
                                          "corrected_status": meta.get("corrected_status", "completed"),
                                          "original_status": meta.get("original_status", "incomplete")}
    return out


def overlay_records(store: Any, scope: Any, records: list[dict]) -> int:
    """Event dicts shown to a model: content = corrected answer, verdict = corrected_complete."""
    fixes = corrections_for(store, scope, [r.get("event_id") for r in records
                                           if r.get("actor") == "assistant" and r.get("verdict") is None])
    for record in records:
        fix = fixes.get(record.get("event_id"))
        if fix:
            record.update(content=fix["answer"], verdict="corrected_complete",
                          correction_event_id=fix["correction_event_id"],
                          superseded_status=fix["original_status"])
    return len(fixes)


def correction_notes(store: Any, scope: Any, event_ids: list[str]) -> str:
    """Text section for tool output: originals above are verbatim; this is their latest valid state."""
    fixes = corrections_for(store, scope, event_ids)
    if not fixes:
        return ""
    lines = ["", "Corrections (latest valid state; the originals above are unchanged records):"]
    for original, fix in fixes.items():
        lines += [f"[{original}] corrected by [{fix['correction_event_id']}]: status "
                  f"{fix['original_status']} -> {fix['corrected_status']}. Valid answer:", fix["answer"]]
    return "\n".join(lines)


def annotate_session(store: Any, scope: Any, session: Any) -> int:
    """Attach ``correction`` to corrected assistant session messages; ``content`` is kept."""
    messages = [m for m in getattr(session, "messages", []) if m.get("role") == "assistant"
                and m.get("memory_event_id") and not m.get("correction")]
    fixes = corrections_for(store, scope, [m["memory_event_id"] for m in messages])
    for message in messages:
        fix = fixes.get(message["memory_event_id"])
        if fix:
            message["correction"] = {"event_id": fix["correction_event_id"], "answer": fix["answer"],
                                     "status": "corrected_complete"}
    return len(fixes)


def effective_message_content(message: dict) -> Any:
    """Content a model should see for a session message: the correction's answer when present."""
    fix = message.get("correction")
    if isinstance(fix, dict) and fix.get("answer"):
        return fix["answer"]
    return message.get("content", "")
