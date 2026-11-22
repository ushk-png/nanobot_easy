"""Privacy operations for conversation memory."""

from __future__ import annotations

from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope


def forget_events(store: ConversationEventStore, scope: MemoryScope, event_ids: list[str],
                  *, include_documents: bool = False) -> int:
    """Exclude events from retrieval while preserving minimal historical metadata."""
    return store.forget_events(scope, event_ids, purge=False, include_documents=include_documents)


def purge_events(store: ConversationEventStore, scope: MemoryScope, event_ids: list[str],
                 *, include_documents: bool = False) -> int:
    """Remove payloads and derived indexes for events within the current scope."""
    return store.forget_events(scope, event_ids, purge=True, include_documents=include_documents)
