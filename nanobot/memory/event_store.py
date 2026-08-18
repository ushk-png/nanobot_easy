"""SQLite raw event store for conversation memory."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

MEMORY_TOOL_NAMES = frozenset({"search_memory", "read_memory_events"})

from nanobot.memory.entity_extract import extract_entities
from nanobot.memory.models import MemoryScope, RawEvent

DEFAULT_DB_NAME = "conversation_memory.db"
EVENT_TYPES = frozenset({
    "USER_MESSAGE",
    "ASSISTANT_MESSAGE",
    "SYSTEM_EVENT",
    "TOOL_CALL",
    "TOOL_RESULT",
    "FILE_EDIT",
    "FILE_CREATED",
    "FILE_DELETED",
    "COMMAND_EXECUTED",
    "APPROVAL",
    "REJECTION",
    "SESSION_START",
    "SESSION_END",
    "TASK_FILE_EDIT",
    "CURATED_MEMORY_EDIT",
})


class ConversationEventStore:
    """Workspace-local raw event store with synchronous FTS/entity updates."""

    def __init__(self, workspace: str | Path, db_path: str | Path | None = None) -> None:
        self.workspace = Path(workspace)
        self.db_path = Path(db_path) if db_path else self.workspace / "memory" / DEFAULT_DB_NAME
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                  event_rowid      INTEGER PRIMARY KEY AUTOINCREMENT,
                  event_id         TEXT NOT NULL UNIQUE,
                  owner_id         TEXT NOT NULL,
                  workspace_id     TEXT NOT NULL,
                  agent_id         TEXT,
                  conversation_id  TEXT NOT NULL,
                  session_id       TEXT NOT NULL,
                  sequence         INTEGER NOT NULL,
                  ts               TEXT NOT NULL,
                  actor            TEXT NOT NULL,
                  event_type       TEXT NOT NULL,
                  content          TEXT,
                  metadata_json    TEXT,
                  parent_event_id  TEXT,
                  content_hash     TEXT,
                  redacted_at      TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_scope
                  ON events(owner_id, workspace_id, agent_id);
                CREATE INDEX IF NOT EXISTS idx_events_conv_seq
                  ON events(conversation_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_events_session_seq
                  ON events(session_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_events_ts
                  ON events(ts);
                CREATE INDEX IF NOT EXISTS idx_events_type_ts
                  ON events(event_type, ts);
                CREATE TABLE IF NOT EXISTS entities (
                  event_id TEXT NOT NULL,
                  kind     TEXT NOT NULL,
                  value    TEXT NOT NULL,
                  PRIMARY KEY(event_id, kind, value),
                  FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_entities_value
                  ON entities(value);
                CREATE TABLE IF NOT EXISTS event_chunks (
                  chunk_rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                  chunk_id    TEXT NOT NULL UNIQUE,
                  event_id    TEXT NOT NULL,
                  ordinal     INTEGER NOT NULL,
                  text        TEXT NOT NULL,
                  FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_event_chunks_event
                  ON event_chunks(event_id, ordinal);
                CREATE TABLE IF NOT EXISTS tasks (
                  task_id         TEXT PRIMARY KEY,
                  title           TEXT NOT NULL,
                  status          TEXT NOT NULL,
                  source_event_id TEXT,
                  updated_at      TEXT,
                  updated_by      TEXT
                );
                """
            )
            self._ensure_fts(conn)
            self._ensure_chunk_fts(conn)

    @staticmethod
    def _ensure_fts(conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                  content,
                  content='events',
                  content_rowid='event_rowid',
                  tokenize='trigram'
                )
                """
            )
        except sqlite3.OperationalError:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
                  content,
                  content='events',
                  content_rowid='event_rowid'
                )
                """
            )

    @staticmethod
    def _ensure_chunk_fts(conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS event_chunks_fts USING fts5(
                  text,
                  content='event_chunks',
                  content_rowid='chunk_rowid',
                  tokenize='trigram'
                )
                """
            )
        except sqlite3.OperationalError:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS event_chunks_fts USING fts5(
                  text,
                  content='event_chunks',
                  content_rowid='chunk_rowid'
                )
                """
            )

    @staticmethod
    def now_ts() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _hash_content(content: str | None) -> str | None:
        if content is None:
            return None
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _json_dumps(value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    def next_sequence(self, session_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS seq FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return int(row["seq"])

    def append_event(
        self,
        *,
        scope: MemoryScope,
        conversation_id: str,
        session_id: str,
        actor: str,
        event_type: str,
        content: str | None = None,
        metadata: Any | None = None,
        parent_event_id: str | None = None,
        ts: str | None = None,
        event_id: str | None = None,
        sequence: int | None = None,
    ) -> RawEvent:
        if event_type not in EVENT_TYPES:
            event_type = "SYSTEM_EVENT"
        metadata_json = self._json_dumps(metadata)
        event = RawEvent(
            event_id=event_id or f"E{uuid.uuid4().hex[:16]}",
            owner_id=scope.owner_id,
            workspace_id=scope.workspace_ids[0],
            agent_id=scope.agent_ids[0],
            conversation_id=conversation_id,
            session_id=session_id,
            sequence=sequence or self.next_sequence(session_id),
            ts=ts or self.now_ts(),
            actor=actor,
            event_type=event_type,
            content=content,
            metadata_json=metadata_json,
            parent_event_id=parent_event_id,
            content_hash=self._hash_content(content),
        )
        self.insert_events([event])
        return event

    def insert_events(self, events: Iterable[RawEvent]) -> None:
        with self.connect() as conn:
            with conn:
                for event in events:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO events (
                          event_id, owner_id, workspace_id, agent_id,
                          conversation_id, session_id, sequence, ts, actor,
                          event_type, content, metadata_json, parent_event_id,
                          content_hash, redacted_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_id,
                            event.owner_id,
                            event.workspace_id,
                            event.agent_id,
                            event.conversation_id,
                            event.session_id,
                            event.sequence,
                            event.ts,
                            event.actor,
                            event.event_type,
                            event.content,
                            event.metadata_json,
                            event.parent_event_id,
                            event.content_hash,
                            event.redacted_at,
                        ),
                    )
                    if cur.rowcount <= 0:
                        continue
                    rowid = conn.execute(
                        "SELECT event_rowid FROM events WHERE event_id = ?",
                        (event.event_id,),
                    ).fetchone()["event_rowid"]
                    if self._should_index_event(event):
                        conn.execute(
                            "INSERT INTO events_fts(rowid, content) VALUES (?, ?)",
                            (rowid, event.content),
                        )
                    self._index_event_chunks_conn(conn, event)
                    if not self._is_memory_tool_result(event):
                        for kind, value in extract_entities(event.content, event.metadata_json):
                            conn.execute(
                                "INSERT OR IGNORE INTO entities(event_id, kind, value) VALUES (?, ?, ?)",
                                (event.event_id, kind, value),
                            )

    @staticmethod
    def _metadata_obj(metadata_json: str | None) -> dict[str, Any]:
        if not metadata_json:
            return {}
        try:
            data = json.loads(metadata_json)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    @classmethod
    def _tool_name_for_event(cls, event: RawEvent) -> str | None:
        metadata = cls._metadata_obj(event.metadata_json)
        name = metadata.get("name") or metadata.get("tool_name")
        return str(name) if name else None

    @classmethod
    def _is_memory_tool_result(cls, event: RawEvent) -> bool:
        return event.event_type == "TOOL_RESULT" and cls._tool_name_for_event(event) in MEMORY_TOOL_NAMES

    @classmethod
    def _should_index_event(cls, event: RawEvent) -> bool:
        return (
            bool(event.content)
            and not event.redacted_at
            and not cls._is_memory_tool_result(event)
            and event.event_type != "CURATED_MEMORY_EDIT"
        )

    @classmethod
    def _chunks_for_event(cls, event: RawEvent) -> list[str]:
        if event.event_type != "CURATED_MEMORY_EDIT" or not event.content or event.redacted_at:
            return []
        lines = [line.strip() for line in event.content.splitlines()]
        chunks: list[str] = []
        current: list[str] = []
        bullet_re = re.compile(r"^(?:[-*•]|\d+[.)]|\[[^\]]+\])\s+")
        for line in lines:
            if not line:
                if current:
                    chunks.append("\n".join(current).strip())
                    current = []
                continue
            if bullet_re.match(line) and current:
                chunks.append("\n".join(current).strip())
                current = [line]
            else:
                current.append(line)
        if current:
            chunks.append("\n".join(current).strip())
        if not chunks:
            chunks = [event.content]
        return [chunk[:4000] for chunk in chunks if chunk.strip()]

    def _index_event_chunks_conn(self, conn: sqlite3.Connection, event: RawEvent) -> None:
        conn.execute("DELETE FROM event_chunks_fts WHERE rowid IN (SELECT chunk_rowid FROM event_chunks WHERE event_id = ?)", (event.event_id,))
        conn.execute("DELETE FROM event_chunks WHERE event_id = ?", (event.event_id,))
        for ordinal, text in enumerate(self._chunks_for_event(event), start=1):
            chunk_id = f"{event.event_id}#chunk-{ordinal}"
            cur = conn.execute(
                "INSERT INTO event_chunks(chunk_id, event_id, ordinal, text) VALUES (?, ?, ?, ?)",
                (chunk_id, event.event_id, ordinal, text),
            )
            conn.execute("INSERT INTO event_chunks_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, text))

    def rebuild_derived_indexes(self) -> None:
        """Rebuild FTS/entities from stored events, honoring current index policy."""
        with self.connect() as conn:
            with conn:
                self._rebuild_derived_indexes_conn(conn)

    def _rebuild_derived_indexes_conn(self, conn: sqlite3.Connection) -> None:
        conn.execute("DROP TABLE IF EXISTS events_fts")
        conn.execute("DROP TABLE IF EXISTS event_chunks_fts")
        self._ensure_fts(conn)
        self._ensure_chunk_fts(conn)
        conn.execute("DELETE FROM entities")
        conn.execute("DELETE FROM event_chunks")
        rows = conn.execute("SELECT * FROM events ORDER BY event_rowid").fetchall()
        for row in rows:
            event = self._row_to_event(row)
            if self._should_index_event(event):
                conn.execute(
                    "INSERT INTO events_fts(rowid, content) VALUES (?, ?)",
                    (row["event_rowid"], event.content),
                )
            self._index_event_chunks_conn(conn, event)
            if not self._is_memory_tool_result(event):
                for kind, value in extract_entities(event.content, event.metadata_json):
                    conn.execute(
                        "INSERT OR IGNORE INTO entities(event_id, kind, value) VALUES (?, ?, ?)",
                        (event.event_id, kind, value),
                    )

    def redact_memory_tool_results(self, *, purge_content: bool = True) -> int:
        """Remove stored content/index entries for memory-tool TOOL_RESULT events."""
        now = self.now_ts()
        count = 0
        with self.connect() as conn:
            with conn:
                rows = conn.execute("SELECT * FROM events WHERE event_type = 'TOOL_RESULT'").fetchall()
                for row in rows:
                    event = self._row_to_event(row)
                    if not self._is_memory_tool_result(event):
                        continue
                    conn.execute("DELETE FROM entities WHERE event_id = ?", (event.event_id,))
                    if purge_content and event.content is not None:
                        metadata = self._metadata_obj(event.metadata_json)
                        metadata.setdefault("content_redaction_reason", "memory_tool_result_not_raw_memory")
                        metadata.setdefault("original_content_hash", event.content_hash)
                        conn.execute(
                            "UPDATE events SET content = NULL, metadata_json = ?, content_hash = NULL, redacted_at = ? WHERE event_id = ?",
                            (self._json_dumps(metadata), now, event.event_id),
                        )
                    count += 1
                if count:
                    self._rebuild_derived_indexes_conn(conn)
        return count

    def get_events(self, scope: MemoryScope, event_ids: Iterable[str]) -> list[RawEvent]:
        ids = [str(eid) for eid in event_ids if str(eid).strip()]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        params: list[Any] = [*ids, scope.owner_id, *scope.workspace_ids]
        agent_sql, agent_params = self._agent_scope_sql(scope)
        sql = f"""
            SELECT * FROM events
            WHERE event_id IN ({placeholders})
              AND owner_id = ?
              AND workspace_id IN ({','.join('?' for _ in scope.workspace_ids)})
              {agent_sql}
              AND redacted_at IS NULL
            ORDER BY ts, sequence
        """
        with self.connect() as conn:
            rows = conn.execute(sql, [*params, *agent_params]).fetchall()
        by_id = {row["event_id"]: self._row_to_event(row) for row in rows}
        return [by_id[eid] for eid in ids if eid in by_id]

    def forget_events(self, scope: MemoryScope, event_ids: Iterable[str], *, purge: bool = False) -> int:
        ids = [str(eid) for eid in event_ids if str(eid).strip()]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        now = self.now_ts()
        with self.connect() as conn:
            with conn:
                rows = conn.execute(
                    f"SELECT event_rowid, event_id FROM events WHERE event_id IN ({placeholders}) AND owner_id = ?",
                    [*ids, scope.owner_id],
                ).fetchall()
                count = 0
                for row in rows:
                    conn.execute("DELETE FROM events_fts WHERE rowid = ?", (row["event_rowid"],))
                    conn.execute("DELETE FROM event_chunks_fts WHERE rowid IN (SELECT chunk_rowid FROM event_chunks WHERE event_id = ?)", (row["event_id"],))
                    conn.execute("DELETE FROM event_chunks WHERE event_id = ?", (row["event_id"],))
                    conn.execute("DELETE FROM entities WHERE event_id = ?", (row["event_id"],))
                    if purge:
                        conn.execute(
                            "UPDATE events SET content = NULL, metadata_json = NULL, content_hash = NULL, redacted_at = ? WHERE event_id = ?",
                            (now, row["event_id"]),
                        )
                    else:
                        conn.execute("UPDATE events SET redacted_at = ? WHERE event_id = ?", (now, row["event_id"]))
                    count += 1
                return count

    @staticmethod
    def _agent_scope_sql(scope: MemoryScope) -> tuple[str, list[Any]]:
        if not scope.agent_ids:
            return "", []
        parts: list[str] = []
        params: list[Any] = []
        if any(agent is None for agent in scope.agent_ids):
            parts.append("agent_id IS NULL")
        named = [agent for agent in scope.agent_ids if agent is not None]
        if named:
            parts.append(f"agent_id IN ({','.join('?' for _ in named)})")
            params.extend(named)
        return "AND (" + " OR ".join(parts) + ")", params

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> RawEvent:
        return RawEvent(**{field: row[field] for field in RawEvent.__dataclass_fields__})
