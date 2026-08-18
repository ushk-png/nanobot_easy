"""FTS5 lexical index helpers.

Phase 1 keeps lexical index updates synchronous inside
ConversationEventStore.insert_events(). This module documents the component
boundary for future expansion without making FTS a second source of truth.
"""

from __future__ import annotations

FTS_TABLE = "events_fts"
