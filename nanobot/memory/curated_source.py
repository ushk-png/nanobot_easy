"""Derive searchable sections from the workspace's canonical memory/MEMORY.md.

MEMORY.md stays the source of truth; the event store only holds a derived, rebuildable copy.
Sync is lazy (run by the memory tools) and cheap: a single ``stat`` plus one indexed SELECT when
nothing changed, a read+hash when the stat changed, and one short write transaction only when
the content hash changed. No watcher, no rehash per agent-loop iteration, no model calls.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from nanobot.memory.entity_extract import extract_entities
from nanobot.memory.event_store import DOC_OWNER, ConversationEventStore
from nanobot.memory.models import RawEvent

SOURCE = "memory/MEMORY.md"
SESSION_ID = "memory-md"
ACTOR = "memory_md"
PAST_VERSION_CAP = 200
LOCK_TIMEOUT_S = 0.5
DOC_NOTE = "curated memory document section; not a raw conversation"

_HEADING = re.compile(r"^#{1,6}\s+\S")
_TOP_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])\s+\S")


@dataclass(frozen=True)
class Section:
    line_start: int
    line_end: int
    heading: str
    text: str


@dataclass
class DocSyncStatus:
    """Outcome of one sync attempt. Only ``verified`` states may expose rows as current."""

    state: str  # fresh | missing | empty | unverified
    changed: bool = False
    error: str | None = None
    sections: int | None = None
    file_sha256: str | None = None
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.state in {"fresh", "missing", "empty"}

    def note(self) -> str:
        if self.state == "fresh":
            return ""
        if self.state == "missing":
            return f"Curated memory document {SOURCE}: file not found; no current sections."
        if self.state == "empty":
            return f"Curated memory document {SOURCE}: file is empty/template; no current sections."
        return (f"Curated memory document {SOURCE}: freshness UNVERIFIED ({self.error}); its sections "
                "were excluded, so this result is incomplete for curated memory, not 'no memory'.")


def split_sections(text: str) -> list[Section]:
    """Lossless sectioning: every non-blank line lands in exactly one section, spans are exact."""
    lines = text.splitlines()
    sections: list[Section] = []
    start: int | None = None
    heading = ""
    current_heading = ""
    only_heading = False
    last_nonblank = 0

    def close() -> None:
        if start is not None and last_nonblank >= start:
            sections.append(Section(start, last_nonblank, heading,
                                    "\n".join(lines[start - 1:last_nonblank])))

    previous_blank = True
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            previous_blank = True
            continue
        is_heading = bool(_HEADING.match(line))
        starts = (start is None or is_heading
                  or (_TOP_BULLET.match(line) and not only_heading)
                  or (previous_blank and not only_heading and not line[:1].isspace()))
        if starts:
            close()
            start = number
            if is_heading:
                current_heading = line.lstrip("#").strip()
            heading = current_heading
            only_heading = is_heading
        elif not is_heading:
            only_heading = False
        last_nonblank = number
        previous_blank = False
    close()
    return sections


def _section_key(salt: str, text: str) -> str:
    # IDs identify exact section content, including significant trailing whitespace.
    return hmac.new(salt.encode(), text.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _stat_key(st: os.stat_result) -> str:
    return f"{st.st_size}:{st.st_mtime_ns}:{st.st_ctime_ns}:{st.st_ino}"


def _is_template(text: str) -> bool:
    from nanobot.utils.helpers import load_bundled_template

    template = load_bundled_template(SOURCE)
    return template is not None and text.strip() == template.strip()


def sync_memory_md(store: ConversationEventStore, workspace_id: str, path: Path | None = None,
                   *, lock_timeout: float = LOCK_TIMEOUT_S) -> DocSyncStatus:
    """Bring derived sections in line with MEMORY.md, or report why freshness is unverified."""
    path = path or Path(workspace_id) / "memory" / "MEMORY.md"
    try:
        root = store.workspace.resolve()
        if Path(workspace_id).resolve() != root or not path.resolve().is_relative_to(root):
            return DocSyncStatus("unverified", error="memory source is outside this workspace")
    except (OSError, RuntimeError):
        return DocSyncStatus("unverified", error="memory source path cannot be verified")
    try:
        return _sync(store, workspace_id, path, lock_timeout)
    except sqlite3.Error as exc:  # locked/busy/corrupt: never block or fake "no memory"
        return DocSyncStatus("unverified", error=f"memory db: {type(exc).__name__}: {exc}")


def _stat(path: Path) -> tuple[os.stat_result | None, str | None]:
    try:
        return path.stat(), None
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"stat failed: {type(exc).__name__}"


def _sync(store: ConversationEventStore, workspace_id: str, path: Path, timeout: float) -> DocSyncStatus:
    st, err = _stat(path)
    if err:
        return DocSyncStatus("unverified", error=err)
    with closing(store.connect(timeout=timeout)) as conn:
        row = conn.execute("SELECT * FROM memory_doc_sources WHERE workspace_id = ? AND source = ?",
                           (workspace_id, SOURCE)).fetchone()
        stat_key = _stat_key(st) if st else "missing"
        def unchanged() -> bool:
            after, error = _stat(path)
            return (not error and path.resolve().is_relative_to(store.workspace.resolve())
                    and (_stat_key(after) if after else "missing") == stat_key)

        if row is not None and row["stat_key"] == stat_key and row["state"] != "unverified":
            if not unchanged():
                return DocSyncStatus("unverified", error="MEMORY.md changed during sync; retry")
            return DocSyncStatus(row["state"], file_sha256=row["file_sha256"])
        if st is None and row is None:
            return DocSyncStatus("missing")  # never registered: nothing to write
        data: bytes | None = None
        if st is not None:
            try:
                data = path.read_bytes()
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return DocSyncStatus("unverified", error="MEMORY.md is not valid UTF-8")
            except FileNotFoundError:
                return DocSyncStatus("unverified", error="MEMORY.md vanished during read; retry")
            except OSError as exc:
                return DocSyncStatus("unverified", error=f"read failed: {type(exc).__name__}")
        else:
            text = ""
        sha = hashlib.sha256(data).hexdigest() if data is not None else None
        state = "missing" if st is None else ("empty" if not text.strip() or _is_template(text) else "fresh")
        if not unchanged():
            return DocSyncStatus("unverified", error="MEMORY.md changed during sync; retry")
        if row is not None and row["file_sha256"] == sha and row["state"] == state:
            with conn:  # touch/same bytes: record new stat only
                conn.execute("UPDATE memory_doc_sources SET stat_key = ? WHERE workspace_id = ? AND source = ?",
                             (stat_key, workspace_id, SOURCE))
            return DocSyncStatus(state, file_sha256=sha)
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            # The file may have changed between read and lock (external editor, Dream, another sync).
            if not unchanged():
                conn.execute("ROLLBACK")
                return DocSyncStatus("unverified", error="MEMORY.md changed during sync; retry")
            latest = conn.execute("SELECT file_sha256, state FROM memory_doc_sources WHERE workspace_id = ? "
                                  "AND source = ?", (workspace_id, SOURCE)).fetchone()
            if latest is not None and latest["file_sha256"] == sha and latest["state"] == state:
                conn.execute("UPDATE memory_doc_sources SET stat_key = ? WHERE workspace_id = ? AND source = ?",
                             (stat_key, workspace_id, SOURCE))  # an overlapping sync already applied it
                conn.execute("COMMIT")
                return DocSyncStatus(state, file_sha256=sha)
            counts = _apply(store, conn, workspace_id, text if state == "fresh" else "", sha, state, stat_key)
            if not unchanged():
                conn.execute("ROLLBACK")
                return DocSyncStatus("unverified", error="MEMORY.md changed during sync; retry")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return DocSyncStatus(state, changed=True, file_sha256=sha, sections=counts["current"], counts=counts)


def _apply(store: ConversationEventStore, conn: sqlite3.Connection, workspace_id: str, text: str,
           sha: str | None, state: str, stat_key: str) -> dict[str, int]:
    now = store.now_ts()
    row = conn.execute("SELECT salt FROM memory_doc_sources WHERE workspace_id = ? AND source = ?",
                       (workspace_id, SOURCE)).fetchone()
    salt = row["salt"] if row else secrets.token_hex(16)
    tombstones = {r["section_key"] for r in conn.execute(
        "SELECT section_key FROM memory_doc_tombstones WHERE workspace_id = ? AND source = ?",
        (workspace_id, SOURCE))}
    existing = {r["event_id"]: r for r in conn.execute(
        "SELECT event_rowid, event_id, superseded_at, redacted_at, metadata_json FROM events "
        "WHERE owner_id = ? AND workspace_id = ? AND session_id = ?", (DOC_OWNER, workspace_id, SESSION_ID))}
    counts = {"current": 0, "inserted": 0, "kept": 0, "restored": 0, "superseded": 0,
              "suppressed": 0, "pruned": 0}
    desired: set[str] = set()
    occurrences: dict[str, int] = {}
    for section in split_sections(text):
        key = _section_key(salt, section.text)
        occurrences[key] = occurrences.get(key, 0) + 1
        event_id = f"memmd-{key[:24]}-{occurrences[key]}"
        if key in tombstones:
            counts["suppressed"] += 1
            continue
        meta = {"source": SOURCE, "file_sha256": sha, "section_key": key, "line_start": section.line_start,
                "line_end": section.line_end, "heading": section.heading, "status": "current",
                "kind": DOC_NOTE}
        old = existing.get(event_id)
        if old is not None and old["redacted_at"]:
            counts["suppressed"] += 1  # forgotten/purged rows are never un-redacted
            continue
        desired.add(event_id)
        counts["current"] += 1
        event = RawEvent(event_id=event_id, owner_id=DOC_OWNER, workspace_id=workspace_id, agent_id=None,
                         conversation_id=SESSION_ID, session_id=SESSION_ID, sequence=section.line_start,
                         ts=now, actor=ACTOR, event_type="CURATED_MEMORY_EDIT", content=section.text,
                         metadata_json=json.dumps(meta, ensure_ascii=False, sort_keys=True),
                         content_hash=ConversationEventStore._hash_content(section.text))
        if old is None:
            store.insert_events([event], connection=conn)
            counts["inserted"] += 1
            continue
        conn.execute("UPDATE events SET metadata_json = ?, sequence = ?, superseded_at = NULL WHERE event_id = ?",
                     (event.metadata_json, section.line_start, event_id))
        old_heading = ConversationEventStore._metadata_obj(old["metadata_json"]).get("heading")
        if old["superseded_at"] or old_heading != section.heading:
            store._index_event_chunks_conn(conn, event)
            conn.execute("DELETE FROM entities WHERE event_id = ?", (event_id,))
            for kind, value in extract_entities(event.content, event.metadata_json):
                conn.execute("INSERT OR IGNORE INTO entities(event_id, kind, value) VALUES (?, ?, ?)",
                             (event_id, kind, value))
            counts["restored" if old["superseded_at"] else "kept"] += 1
        else:
            counts["kept"] += 1
    for event_id, old in existing.items():
        if event_id in desired or old["superseded_at"] or old["redacted_at"]:
            continue
        meta = ConversationEventStore._metadata_obj(old["metadata_json"])
        meta["status"] = "superseded"
        conn.execute("UPDATE events SET superseded_at = ?, metadata_json = ? WHERE event_id = ?",
                     (now, json.dumps(meta, ensure_ascii=False, sort_keys=True), event_id))
        store._delete_event_indexes_conn(conn, old["event_rowid"], event_id)
        conn.execute("DELETE FROM entities WHERE event_id = ?", (event_id,))
        counts["superseded"] += 1
    stale = conn.execute(
        "SELECT event_rowid, event_id FROM events WHERE owner_id = ? AND workspace_id = ? AND session_id = ? "
        "AND superseded_at IS NOT NULL AND redacted_at IS NULL ORDER BY superseded_at DESC, event_rowid DESC "
        "LIMIT -1 OFFSET ?", (DOC_OWNER, workspace_id, SESSION_ID, PAST_VERSION_CAP)).fetchall()
    for old in stale:
        store._delete_event_indexes_conn(conn, old["event_rowid"], old["event_id"])
        conn.execute("DELETE FROM events WHERE event_rowid = ?", (old["event_rowid"],))
        counts["pruned"] += 1
    conn.execute(
        "INSERT INTO memory_doc_sources(workspace_id, source, salt, file_sha256, stat_key, state, error, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL, ?) ON CONFLICT(workspace_id, source) DO UPDATE SET "
        "file_sha256 = excluded.file_sha256, stat_key = excluded.stat_key, state = excluded.state, "
        "error = NULL, synced_at = excluded.synced_at",
        (workspace_id, SOURCE, salt, sha, stat_key, state, now))
    return counts
