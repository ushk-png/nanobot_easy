"""Fill conversation_memory.db with conversation turns it missed.

Until the event-id fix, a conversation that was cleared (``/new``) or compacted reused the ids
of its earlier messages, and the store ignored the repeated ids, so weeks of later turns never
reached the memory database. The session files still hold them. This copies the user and
assistant messages of every session that the database does not have yet.

Dry run by default. With ``apply`` the database is first copied to ``<name>.bak-<time>``. Running
it again adds nothing (ids derive from the message), and it never changes or deletes an event.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import RawEvent
from nanobot.session.manager import SessionManager

_EVENT_TYPES = {"user": "USER_MESSAGE", "assistant": "ASSISTANT_MESSAGE"}


@dataclass
class BackfillReport:
    applied: bool
    sessions: int = 0
    messages_seen: int = 0
    missing: int = 0
    inserted: int = 0
    per_session: dict[str, int] = field(default_factory=dict)
    backup: Path | None = None


def _text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, dict) and item.get("type") == "image_url":
                parts.append("[image]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def _owner_id(session_key: str) -> str:
    """The chat id the live loop uses as owner (``msg.chat_id``): the key after ``channel:``."""
    return session_key.split(":", 1)[1] if ":" in session_key else session_key


def backfill_missing_events(workspace: Path, *, apply: bool = False) -> BackfillReport:
    workspace = Path(workspace).expanduser()
    store = ConversationEventStore(workspace)
    sessions = SessionManager(workspace)
    workspace_id = str(workspace.resolve())
    report = BackfillReport(applied=apply)
    pending: list[RawEvent] = []

    with store.connect() as conn:
        conn.row_factory = sqlite3.Row
        for info in sessions.list_sessions():
            key = str(info.get("key") or "")
            if not key:
                continue
            session = sessions.get_or_create(key)
            report.sessions += 1
            known = {
                (row["actor"], row["content_hash"], row["ts"])
                for row in conn.execute(
                    "SELECT actor, content_hash, ts FROM events WHERE session_id = ?", (key,)
                )
            }
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS seq FROM events WHERE session_id = ?", (key,)
                ).fetchone()["seq"]
            )
            missing_here = 0
            for index, message in enumerate(session.messages):
                event_type = _EVENT_TYPES.get(str(message.get("role")))
                if event_type is None or message.get("tool_calls"):
                    continue
                content = _text(message)
                if not content.strip():
                    continue
                report.messages_seen += 1
                actor = str(message.get("role"))
                ts = str(message.get("timestamp") or "")
                content_hash = ConversationEventStore._hash_content(content)
                if ts and (actor, content_hash, ts) in known:
                    continue
                digest = hashlib.sha1(f"{key}|{index}|{ts}|{actor}|{content_hash}".encode()).hexdigest()[:16]
                sequence += 1
                missing_here += 1
                pending.append(RawEvent(
                    event_id=f"{key}:backfill:{digest}",
                    owner_id=_owner_id(key),
                    workspace_id=workspace_id,
                    agent_id=None,
                    conversation_id=key,
                    session_id=key,
                    sequence=sequence,
                    ts=ts or ConversationEventStore.now_ts(),
                    actor=actor,
                    event_type=event_type,
                    content=content,
                    metadata_json=ConversationEventStore._json_dumps({"backfilled": True}),
                    content_hash=content_hash,
                ))
            if missing_here:
                report.per_session[key] = missing_here
    report.missing = len(pending)

    if apply and pending:
        backup = store.db_path.with_name(f"{store.db_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(store.db_path, backup)
        report.backup = backup
        store.insert_events(pending)
        with store.connect() as conn:
            ids = [event.event_id for event in pending]
            marks = ",".join("?" for _ in ids)
            report.inserted = conn.execute(
                f"SELECT COUNT(*) FROM events WHERE event_id IN ({marks})", ids
            ).fetchone()[0]
    return report
