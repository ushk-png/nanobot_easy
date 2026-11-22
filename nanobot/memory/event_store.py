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

from nanobot.memory.entity_extract import extract_entities
from nanobot.memory.models import MemoryScope, RawEvent
from nanobot.session.lifecycle import ensure_session_writable

MEMORY_TOOL_NAMES = frozenset({"search_memory", "read_memory_events"})

DEFAULT_DB_NAME = "conversation_memory.db"
# Owner of sections derived from the workspace's MEMORY.md. That document is already part of
# every prompt served from this workspace, so its sections are visible to any runtime scope
# of the same workspace/agent, only when a caller opts in (include_documents=True).
DOC_OWNER = "__workspace_memory_document__"
CHUNK_CHARS = 4000
CHUNK_OVERLAP = 200
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

    def connect(self, timeout: float = 5.0) -> sqlite3.Connection:
        ensure_session_writable(self.workspace)
        conn = sqlite3.connect(self.db_path, timeout=timeout)
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
            self._ensure_document_tables(conn)

    @staticmethod
    def _ensure_document_tables(conn: sqlite3.Connection) -> None:
        """Additive migration: MEMORY.md sync state, past-version marker and forget tombstones."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        if "superseded_at" not in columns:
            try:
                conn.execute("ALTER TABLE events ADD COLUMN superseded_at TEXT")
            except sqlite3.OperationalError as exc:  # concurrent first open already added it
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_doc_sources (
              workspace_id  TEXT NOT NULL,
              source        TEXT NOT NULL,
              salt          TEXT NOT NULL,
              file_sha256   TEXT,
              stat_key      TEXT,
              state         TEXT NOT NULL,
              error         TEXT,
              synced_at     TEXT,
              PRIMARY KEY(workspace_id, source)
            );
            CREATE TABLE IF NOT EXISTS memory_doc_tombstones (
              workspace_id  TEXT NOT NULL,
              source        TEXT NOT NULL,
              section_key   TEXT NOT NULL,
              mode          TEXT NOT NULL,
              created_at    TEXT NOT NULL,
              PRIMARY KEY(workspace_id, source, section_key)
            );
            CREATE TABLE IF NOT EXISTS memory_doc_hidden (
              owner_id      TEXT NOT NULL,
              workspace_id  TEXT NOT NULL,
              section_key   TEXT NOT NULL,
              created_at    TEXT NOT NULL,
              PRIMARY KEY(owner_id, workspace_id, section_key)
            );
            """
        )

    @staticmethod
    def owner_sql(scope: MemoryScope, include_documents: bool, alias: str = "", *,
                  current_only: bool = True) -> tuple[str, list[Any]]:
        """Owner clause; documents add only current MEMORY.md sections of the same workspace."""
        col = f"{alias or 'events'}."
        if not include_documents:
            return f"{col}owner_id = ?", [scope.owner_id]
        current = f" AND {col}superseded_at IS NULL" if current_only else ""
        return (f"({col}owner_id = ? OR ({col}owner_id = ? AND {col}event_type = 'CURATED_MEMORY_EDIT'"
                f"{current} AND NOT EXISTS (SELECT 1 FROM memory_doc_hidden h WHERE h.owner_id = ?"
                f" AND h.workspace_id = {col}workspace_id"
                f" AND substr(h.section_key, 1, 24) = substr({col}event_id, 7, 24))))",
                [scope.owner_id, DOC_OWNER, scope.owner_id])

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

    def insert_events(self, events: Iterable[RawEvent], *, connection=None) -> None:
        from contextlib import nullcontext

        with (nullcontext(connection) if connection is not None else self.connect()) as conn:
            with (nullcontext() if connection is not None else conn):
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
        pieces = [piece for chunk in chunks if chunk.strip() for piece in cls._split_long(chunk)]
        # Each document section is searchable by its inherited heading without changing raw text.
        heading = cls._metadata_obj(event.metadata_json).get("heading")
        if event.owner_id == DOC_OWNER and isinstance(heading, str) and heading:
            pieces.extend(cls._split_long(heading))
        return pieces

    @staticmethod
    def _split_long(chunk: str) -> list[str]:
        """Index long paragraphs/lines in overlapping windows instead of dropping the tail."""
        if len(chunk) <= CHUNK_CHARS:
            return [chunk]
        step = CHUNK_CHARS - CHUNK_OVERLAP
        return [chunk[i:i + CHUNK_CHARS] for i in range(0, len(chunk) - CHUNK_OVERLAP, step)]

    def _index_event_chunks_conn(self, conn: sqlite3.Connection, event: RawEvent) -> None:
        self._delete_event_chunks_conn(conn, event.event_id)
        for ordinal, text in enumerate(self._chunks_for_event(event), start=1):
            chunk_id = f"{event.event_id}#chunk-{ordinal}"
            cur = conn.execute(
                "INSERT INTO event_chunks(chunk_id, event_id, ordinal, text) VALUES (?, ?, ?, ?)",
                (chunk_id, event.event_id, ordinal, text),
            )
            conn.execute("INSERT INTO event_chunks_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, text))

    @staticmethod
    def _delete_event_chunks_conn(conn: sqlite3.Connection, event_id: str) -> None:
        conn.execute(
            "DELETE FROM event_chunks_fts WHERE rowid IN "
            "(SELECT c.chunk_rowid FROM event_chunks c "
            "JOIN event_chunks_fts_docsize d ON d.id = c.chunk_rowid WHERE c.event_id = ?)",
            (event_id,),
        )
        conn.execute("DELETE FROM event_chunks WHERE event_id = ?", (event_id,))

    def _delete_event_indexes_conn(
        self, conn: sqlite3.Connection, event_rowid: int, event_id: str
    ) -> None:
        # External-content FTS also exposes unindexed source rows. Its docsize
        # table records actual membership, including indexes from older policies.
        if conn.execute(
            "SELECT 1 FROM events_fts_docsize WHERE id = ?", (event_rowid,)
        ).fetchone():
            conn.execute("DELETE FROM events_fts WHERE rowid = ?", (event_rowid,))
        self._delete_event_chunks_conn(conn, event_id)

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
            if row["superseded_at"]:  # past MEMORY.md versions: raw-readable by ID, not indexed
                continue
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

    def get_events(self, scope: MemoryScope, event_ids: Iterable[str], *,
                   include_documents: bool = False) -> list[RawEvent]:
        ids = [str(eid) for eid in event_ids if str(eid).strip()]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        owner, owner_params = self.owner_sql(scope, include_documents, current_only=False)
        params: list[Any] = [*ids, *owner_params, *scope.workspace_ids]
        agent_sql, agent_params = self._agent_scope_sql(scope)
        sql = f"""
            SELECT * FROM events
            WHERE event_id IN ({placeholders})
              AND {owner}
              AND workspace_id IN ({','.join('?' for _ in scope.workspace_ids)})
              {agent_sql}
              AND redacted_at IS NULL
            ORDER BY ts, sequence
        """
        with self.connect() as conn:
            rows = conn.execute(sql, [*params, *agent_params]).fetchall()
        by_id = {row["event_id"]: self._row_to_event(row) for row in rows}
        return [by_id[eid] for eid in ids if eid in by_id]

    def hide_document_events(self, scope: MemoryScope, event_ids: Iterable[str]) -> int:
        """Hide shared sections only for this owner; never purge another user's shared source."""
        events = self.get_events(scope, event_ids, include_documents=True)
        documents = [event for event in events if event.owner_id == DOC_OWNER]
        with self.connect() as conn:
            with conn:
                for event in documents:
                    key = self._metadata_obj(event.metadata_json).get("section_key")
                    if key:
                        conn.execute(
                            "INSERT OR IGNORE INTO memory_doc_hidden VALUES (?, ?, ?, ?)",
                            (scope.owner_id, event.workspace_id, key, self.now_ts()),
                        )
        return len(documents)

    def forget_events(self, scope: MemoryScope, event_ids: Iterable[str], *, purge: bool = False,
                      include_documents: bool = False) -> int:
        ids = [str(eid) for eid in event_ids if str(eid).strip()]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        now = self.now_ts()
        agent_sql, agent_params = self._agent_scope_sql(scope)
        with self.connect() as conn:
            if purge:
                conn.execute("PRAGMA secure_delete = ON")
            with conn:
                owners = [scope.owner_id, DOC_OWNER] if include_documents else [scope.owner_id]
                rows = conn.execute(
                    f"""SELECT event_rowid, event_id, owner_id, workspace_id, metadata_json FROM events
                        WHERE event_id IN ({placeholders})
                        AND owner_id IN ({','.join('?' for _ in owners)})
                        AND workspace_id IN ({','.join('?' for _ in scope.workspace_ids)})
                        {agent_sql}
                        AND (redacted_at IS NULL OR
                            (? AND (content IS NOT NULL OR metadata_json IS NOT NULL
                                    OR content_hash IS NOT NULL)))""",
                    [*ids, *owners, *scope.workspace_ids, *agent_params, purge],
                ).fetchall()
                count = 0
                for row in rows:
                    if row["owner_id"] == DOC_OWNER:
                        self._tombstone_document_section_conn(conn, row, "purge" if purge else "forget", now)
                    self._delete_event_indexes_conn(conn, row["event_rowid"], row["event_id"])
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

    def _tombstone_document_section_conn(self, conn: sqlite3.Connection, row: sqlite3.Row,
                                         mode: str, now: str) -> None:
        """Keyed hash only: the section text is never copied; resync will not re-add it."""
        key = self._metadata_obj(row["metadata_json"]).get("section_key")
        if key:
            conn.execute(
                "INSERT INTO memory_doc_tombstones(workspace_id, source, section_key, mode, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(workspace_id, source, section_key) DO UPDATE SET mode = "
                "CASE WHEN excluded.mode = "
                "'purge' THEN 'purge' ELSE memory_doc_tombstones.mode END",
                (row["workspace_id"], self._metadata_obj(row["metadata_json"]).get("source"),
                 key, mode, now))

    def purge_session(self, session_id: str) -> int:
        """Remove a chat's raw events, indexes and derived tasks in one transaction.

        Curated Markdown summaries and external backups have separate lifecycles.
        This API is for the authenticated owner of this workspace database.
        """
        with self.connect() as conn:
            conn.execute("PRAGMA secure_delete = ON")
            rows = conn.execute(
                "SELECT event_rowid, event_id, content FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchall()
            # Task projections reference tasks without an ON DELETE CASCADE.
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_context_versions'"
            ).fetchone():
                conn.execute(
                    "DELETE FROM task_context_versions WHERE session_id = ?",
                    (session_id,),
                )
            for row in rows:
                self._delete_event_indexes_conn(conn, row["event_rowid"], row["event_id"])
                conn.execute("DELETE FROM tasks WHERE source_event_id = ?", (row["event_id"],))
            conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
            return len(rows)

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
