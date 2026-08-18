"""Raw memory event reader for verbatim recall."""

from __future__ import annotations

from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope, RawEvent
from nanobot.utils.helpers import estimate_message_tokens, truncate_text_to_tokens


def read_memory_events(
    store: ConversationEventStore,
    scope: MemoryScope,
    event_ids: list[str],
    *,
    max_total_tokens: int | None = None,
) -> list[RawEvent]:
    events = store.get_events(scope, event_ids)
    if max_total_tokens is None:
        return events
    kept: list[RawEvent] = []
    used = 0
    for event in events:
        content = event.content or ""
        try:
            tokens = estimate_message_tokens(content)
        except Exception:
            tokens = max(1, len(content) // 4)
        if kept and used + tokens > max_total_tokens:
            break
        if tokens > max_total_tokens:
            clipped = truncate_text_to_tokens(content, max_total_tokens)
            event = RawEvent(**{**event.__dict__, "content": clipped})
            tokens = max_total_tokens
        kept.append(event)
        used += tokens
    return kept


def format_raw_events(events: list[RawEvent]) -> str:
    if not events:
        return "No scoped raw memory events found, or the content was forgotten/purged."
    lines = ["Raw Memory Events"]
    for event in events:
        lines.extend([
            "",
            f"[{event.event_id}] {event.ts} {event.event_type} actor={event.actor}",
            event.content if event.content is not None else "[content unavailable: forgotten, purged, or legacy summary only]",
        ])
    return "\n".join(lines)
