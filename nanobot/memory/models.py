"""Data models for scoped raw-event conversation memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MemoryScope:
    """Runtime-enforced memory scope.

    Agent tools may choose search terms, but owner/workspace/agent access is bound
    by runtime and must not be widened from tool parameters.
    """

    owner_id: str
    workspace_ids: tuple[str, ...]
    agent_ids: tuple[str | None, ...] = (None,)

    @classmethod
    def from_runtime(
        cls,
        *,
        owner_id: str | None,
        workspace_id: str | None,
        agent_id: str | None,
    ) -> "MemoryScope":
        owner = str(owner_id or "local")
        workspace = str(workspace_id or "main")
        return cls(owner_id=owner, workspace_ids=(workspace,), agent_ids=(agent_id, None))


@dataclass(frozen=True)
class RawEvent:
    event_id: str
    owner_id: str
    workspace_id: str
    agent_id: str | None
    conversation_id: str
    session_id: str
    sequence: int
    ts: str
    actor: str
    event_type: str
    content: str | None = None
    metadata_json: str | None = None
    parent_event_id: str | None = None
    content_hash: str | None = None
    redacted_at: str | None = None


@dataclass(frozen=True)
class EventWindow:
    score: float
    session_id: str
    conversation_id: str
    events: tuple[RawEvent, ...]
    matched_event_ids: tuple[str, ...] = field(default_factory=tuple)
    truncated: bool = False

    @property
    def event_ids(self) -> list[str]:
        return [event.event_id for event in self.events]


@dataclass(frozen=True)
class SearchResult:
    windows: tuple[EventWindow, ...]
    query: str
    total_candidates: int
    context_tokens: int
    truncated: bool = False


def event_to_dict(event: RawEvent, *, include_content: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "event_id": event.event_id,
        "timestamp": event.ts,
        "event_type": event.event_type,
        "actor": event.actor,
        "session_id": event.session_id,
        "conversation_id": event.conversation_id,
        "sequence": event.sequence,
        "parent_event_id": event.parent_event_id,
        "redacted": bool(event.redacted_at),
    }
    if include_content:
        data["content"] = event.content
    return data
