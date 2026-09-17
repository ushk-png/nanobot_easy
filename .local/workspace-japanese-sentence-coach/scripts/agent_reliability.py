#!/usr/bin/env python3
"""Small SQLite-backed reliability primitives for local nanobot agent tasks.

This module is intentionally thin and local-first.  It does not replace the
nanobot runtime, but gives deterministic jobs such as Elle's daily Japanese
sentence generation a transactional place to record:

- effective policy/spec version
- accepted user requests
- execution attempts and logical job ownership
- artifacts and validation evidence
- fake/real delivery intent state

External file writes and Telegram delivery are deliberately outside SQLite
transactions; completed files are registered only after they exist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_DB_PATH = Path("memory/agent_reliability.sqlite3")
DEFAULT_AGENT = "elle"
DEFAULT_TASK = "daily_japanese_sentence"


class ReliabilityError(RuntimeError):
    """Raised when reliability state cannot be safely established."""


@dataclass(frozen=True)
class ExecutionRecord:
    id: int
    logical_key: str
    attempt_no: int
    lease_token: str
    spec: dict[str, Any]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def now_ms() -> int:
    return int(time.time() * 1000)


def connect(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            task TEXT NOT NULL,
            version INTEGER NOT NULL,
            effective_from TEXT NOT NULL,
            timezone TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            created_at_ms INTEGER NOT NULL,
            UNIQUE(agent, task, version)
        );

        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            agent TEXT NOT NULL,
            task TEXT NOT NULL,
            source_message_id TEXT,
            source_ref TEXT,
            request_kind TEXT NOT NULL,
            target_date TEXT,
            timezone TEXT NOT NULL,
            constraints_json TEXT NOT NULL,
            status TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            UNIQUE(agent, task, source_message_id)
        );

        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logical_key TEXT NOT NULL UNIQUE,
            agent TEXT NOT NULL,
            task TEXT NOT NULL,
            target_date TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            lease_token TEXT NOT NULL,
            lease_expires_ms INTEGER,
            spec_json TEXT NOT NULL,
            spec_hash TEXT NOT NULL,
            request_id TEXT,
            policy_version INTEGER,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
            artifact_kind TEXT NOT NULL,
            path TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            content_json TEXT,
            created_at_ms INTEGER NOT NULL,
            UNIQUE(execution_id, artifact_kind, path)
        );

        CREATE TABLE IF NOT EXISTS validations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
            check_name TEXT NOT NULL,
            method_version TEXT NOT NULL,
            expected_json TEXT NOT NULL,
            actual_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('passed','failed','unknown')),
            evidence_json TEXT NOT NULL,
            required INTEGER NOT NULL DEFAULT 1,
            created_at_ms INTEGER NOT NULL,
            UNIQUE(execution_id, check_name)
        );

        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            delivery_key TEXT NOT NULL UNIQUE,
            execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE CASCADE,
            channel TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            component TEXT NOT NULL,
            status TEXT NOT NULL,
            content_sha256 TEXT,
            media_json TEXT NOT NULL,
            fake INTEGER NOT NULL DEFAULT 1,
            attempts INTEGER NOT NULL DEFAULT 0,
            external_message_id TEXT,
            last_error TEXT,
            created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_requests_target ON requests(agent, task, target_date, status);
        CREATE INDEX IF NOT EXISTS idx_executions_status ON executions(agent, task, status);
        CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def ensure_default_policy(
    conn: sqlite3.Connection,
    *,
    sentence_count: int,
    timezone: str = "Asia/Seoul",
    agent: str = DEFAULT_AGENT,
    task: str = DEFAULT_TASK,
) -> int:
    row = conn.execute(
        "SELECT version, policy_json FROM policies WHERE agent=? AND task=? ORDER BY version DESC LIMIT 1",
        (agent, task),
    ).fetchone()
    policy = {
        "daily_new_sentence_count": sentence_count,
        "level": "beginner",
        "default_topic": "travel",
        "must_include_pronunciation": True,
        "audio_required": True,
        "text_first_allowed_on_audio_failure": False,
        "validation_failure_policy": "block_delivery",
        "required_checks": [
            "sentence_count",
            "required_fields",
            "required_words",
            "message_matches_sentences",
            "audio_input_matches_sentences",
        ],
    }
    if row:
        existing = json.loads(row["policy_json"])
        if existing == policy:
            return int(row["version"])
        version = int(row["version"]) + 1
    else:
        version = 1
    conn.execute(
        """
        INSERT INTO policies(agent, task, version, effective_from, timezone, policy_json, created_at_ms)
        VALUES(?,?,?,?,?,?,?)
        """,
        (agent, task, version, "1970-01-01", timezone, canonical_json(policy), now_ms()),
    )
    conn.commit()
    return version


def get_effective_policy(
    conn: sqlite3.Connection,
    *,
    agent: str,
    task: str,
    target_date: str,
) -> tuple[int | None, dict[str, Any]]:
    row = conn.execute(
        """
        SELECT version, policy_json FROM policies
        WHERE agent=? AND task=? AND effective_from<=?
        ORDER BY version DESC LIMIT 1
        """,
        (agent, task, target_date),
    ).fetchone()
    if not row:
        return None, {}
    return int(row["version"]), json.loads(row["policy_json"])


def submit_request(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    agent: str,
    task: str,
    source_message_id: str | None,
    source_ref: str | None,
    request_kind: str,
    target_date: str | None,
    timezone: str,
    constraints: dict[str, Any],
    status: str = "ready",
) -> sqlite3.Row:
    ts = now_ms()
    constraints_text = canonical_json(constraints)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO requests(
                    request_id, agent, task, source_message_id, source_ref, request_kind,
                    target_date, timezone, constraints_json, status, revision,
                    created_at_ms, updated_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    request_id,
                    agent,
                    task,
                    source_message_id,
                    source_ref,
                    request_kind,
                    target_date,
                    timezone,
                    constraints_text,
                    status,
                    1,
                    ts,
                    ts,
                ),
            )
    except sqlite3.IntegrityError:
        # Idempotent same-message handling: return the existing request instead of
        # creating a duplicate.  Conflicting same-message changes are not silently
        # accepted because callers see the stored constraints/status.
        pass
    row = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
    if row is None and source_message_id is not None:
        row = conn.execute(
            "SELECT * FROM requests WHERE agent=? AND task=? AND source_message_id=?",
            (agent, task, source_message_id),
        ).fetchone()
    if row is None:
        raise ReliabilityError(f"request was not stored and no idempotent row exists: {request_id}")
    return row


def import_vocab_request(
    conn: sqlite3.Connection,
    vocab_request: dict[str, Any],
    *,
    target_date: str,
    agent: str = DEFAULT_AGENT,
    task: str = DEFAULT_TASK,
    timezone: str = "Asia/Seoul",
) -> sqlite3.Row:
    rid = str(vocab_request.get("id") or f"{target_date}-vocab-{sha256_text(canonical_json(vocab_request))[:12]}")
    source = vocab_request.get("source")
    constraints = {
        "kind": "vocabulary_sentences",
        "words": vocab_request.get("words", []),
        "sentences": vocab_request.get("sentences", []),
        "source": source,
        "note": vocab_request.get("note"),
    }
    return submit_request(
        conn,
        request_id=rid,
        agent=agent,
        task=task,
        source_message_id=str(vocab_request.get("source_message_id") or rid),
        source_ref=str(source) if source else None,
        request_kind="vocabulary_sentences",
        target_date=target_date,
        timezone=timezone,
        constraints=constraints,
        status=str(vocab_request.get("status") or "ready"),
    )


def begin_execution(
    conn: sqlite3.Connection,
    *,
    agent: str,
    task: str,
    target_date: str,
    spec: dict[str, Any],
    request_id: str | None,
    policy_version: int | None,
    lease_ms: int = 15 * 60 * 1000,
) -> ExecutionRecord:
    logical_key = f"{agent}:{task}:{target_date}"
    spec_text = canonical_json(spec)
    spec_hash = sha256_text(spec_text)
    token = sha256_text(f"{logical_key}:{spec_hash}:{now_ms()}")[:24]
    ts = now_ms()
    with conn:
        existing = conn.execute(
            "SELECT * FROM executions WHERE logical_key=?",
            (logical_key,),
        ).fetchone()
        if existing:
            status = str(existing["status"])
            if status in {"sending", "delivery_confirmed", "delivery_unknown"}:
                raise ReliabilityError(f"execution {logical_key} is already in terminal/delivery state: {status}")
            conn.execute(
                """
                UPDATE executions
                SET status='running', attempt_no=attempt_no+1, lease_token=?, lease_expires_ms=?,
                    spec_json=?, spec_hash=?, request_id=?, policy_version=?, updated_at_ms=?
                WHERE id=?
                """,
                (token, ts + lease_ms, spec_text, spec_hash, request_id, policy_version, ts, existing["id"]),
            )
            row = conn.execute("SELECT * FROM executions WHERE id=?", (existing["id"],)).fetchone()
        else:
            conn.execute(
                """
                INSERT INTO executions(
                    logical_key, agent, task, target_date, status, attempt_no, lease_token,
                    lease_expires_ms, spec_json, spec_hash, request_id, policy_version,
                    created_at_ms, updated_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    logical_key,
                    agent,
                    task,
                    target_date,
                    "running",
                    1,
                    token,
                    ts + lease_ms,
                    spec_text,
                    spec_hash,
                    request_id,
                    policy_version,
                    ts,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM executions WHERE logical_key=?", (logical_key,)).fetchone()
    assert row is not None
    return ExecutionRecord(
        id=int(row["id"]),
        logical_key=str(row["logical_key"]),
        attempt_no=int(row["attempt_no"]),
        lease_token=str(row["lease_token"]),
        spec=json.loads(row["spec_json"]),
    )


def add_artifact(
    conn: sqlite3.Connection,
    execution_id: int,
    *,
    artifact_kind: str,
    path: Path,
    content_json: Any | None = None,
) -> None:
    if not path.exists():
        raise ReliabilityError(f"artifact does not exist: {path}")
    conn.execute(
        """
        INSERT OR REPLACE INTO artifacts(execution_id, artifact_kind, path, content_sha256, content_json, created_at_ms)
        VALUES(?,?,?,?,?,?)
        """,
        (
            execution_id,
            artifact_kind,
            str(path),
            file_sha256(path),
            canonical_json(content_json) if content_json is not None else None,
            now_ms(),
        ),
    )


def add_validation(
    conn: sqlite3.Connection,
    execution_id: int,
    *,
    check_name: str,
    expected: Any,
    actual: Any,
    status: str,
    evidence: Any,
    required: bool = True,
    method_version: str = "agent_reliability.v1",
) -> None:
    if status not in {"passed", "failed", "unknown"}:
        raise ValueError(f"invalid validation status: {status}")
    conn.execute(
        """
        INSERT OR REPLACE INTO validations(
            execution_id, check_name, method_version, expected_json, actual_json,
            status, evidence_json, required, created_at_ms
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            execution_id,
            check_name,
            method_version,
            canonical_json(expected),
            canonical_json(actual),
            status,
            canonical_json(evidence),
            1 if required else 0,
            now_ms(),
        ),
    )


def validate_sentence_bundle(
    *,
    execution_id: int,
    conn: sqlite3.Connection,
    spec: dict[str, Any],
    new_items: list[dict[str, Any]],
    message: str,
    audio_payload: dict[str, Any],
    vocab_coverage: dict[str, Any] | None,
) -> bool:
    expected_count = int(spec.get("sentence_count") or 0)
    passed_all = True

    def record(name: str, expected: Any, actual: Any, ok: bool, evidence: Any) -> None:
        nonlocal passed_all
        passed_all = passed_all and ok
        add_validation(
            conn,
            execution_id,
            check_name=name,
            expected=expected,
            actual=actual,
            status="passed" if ok else "failed",
            evidence=evidence,
            required=True,
        )

    record(
        "sentence_count",
        expected_count,
        len(new_items),
        len(new_items) == expected_count,
        {"sentence_ids": [item.get("id") for item in new_items]},
    )

    missing_fields: list[dict[str, Any]] = []
    for item in new_items:
        miss = [k for k in ("id", "japanese", "pronunciation_ko", "meaning_ko") if not item.get(k)]
        if miss:
            missing_fields.append({"id": item.get("id"), "missing": miss})
    record("required_fields", [], missing_fields, not missing_fields, {"checked": ["id", "japanese", "pronunciation_ko", "meaning_ko"]})

    required_words = list(spec.get("required_words") or [])
    if required_words:
        covered = list((vocab_coverage or {}).get("covered_words") or [])
        missing = [word for word in required_words if word not in covered]
        record(
            "required_words",
            required_words,
            {"covered_words": covered, "missing_words": missing},
            not missing,
            {"vocab_coverage": vocab_coverage},
        )
    else:
        record("required_words", [], [], True, {"reason": "no required words in effective spec"})

    missing_from_message = [item["japanese"] for item in new_items if item.get("japanese") not in message]
    record(
        "message_matches_sentences",
        [item.get("japanese") for item in new_items],
        {"missing_from_message": missing_from_message},
        not missing_from_message,
        {"message_sha256": sha256_text(message)},
    )

    audio_items = audio_payload.get("items") if isinstance(audio_payload, dict) else None
    audio_pairs = []
    if isinstance(audio_items, list):
        audio_pairs = [(x.get("sentence_id"), x.get("japanese"), x.get("korean")) for x in audio_items if isinstance(x, dict)]
    expected_pairs = [(x.get("id"), x.get("japanese"), x.get("meaning_ko")) for x in new_items]
    record(
        "audio_input_matches_sentences",
        expected_pairs,
        audio_pairs,
        audio_pairs == expected_pairs,
        {"audio_item_count": len(audio_pairs)},
    )
    return passed_all


def mark_execution_status(conn: sqlite3.Connection, execution_id: int, status: str) -> None:
    conn.execute(
        "UPDATE executions SET status=?, updated_at_ms=? WHERE id=?",
        (status, now_ms(), execution_id),
    )


def cancel_outbox_for_execution(conn: sqlite3.Connection, execution_id: int, reason: str) -> None:
    conn.execute(
        """
        UPDATE outbox
        SET status='cancelled', last_error=?, updated_at_ms=?
        WHERE execution_id=? AND status NOT IN ('sent_confirmed', 'cancelled')
        """,
        (reason, now_ms(), execution_id),
    )


def create_fake_outbox(
    conn: sqlite3.Connection,
    *,
    execution_id: int,
    channel: str,
    chat_id: str,
    content: str,
    media: list[str],
) -> None:
    # The current reliability path records a delivery intent but intentionally
    # does not call Telegram.  Operational switching can later use the same row.
    key_base = f"{execution_id}:{channel}:{chat_id}:lesson"
    conn.execute(
        """
        INSERT OR REPLACE INTO outbox(
            delivery_key, execution_id, channel, chat_id, component, status,
            content_sha256, media_json, fake, attempts, created_at_ms, updated_at_ms
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            sha256_text(key_base),
            execution_id,
            channel,
            chat_id,
            "lesson_bundle",
            "fake_ready",
            sha256_text(content),
            canonical_json(media),
            1,
            0,
            now_ms(),
            now_ms(),
        ),
    )


def finalize_generation(
    conn: sqlite3.Connection,
    execution_id: int,
    *,
    validated: bool,
    channel: str = "telegram",
    chat_id: str = "8580974491",
    content: str = "",
    media: list[str] | None = None,
) -> None:
    with conn:
        if validated:
            mark_execution_status(conn, execution_id, "fake_delivery_ready")
            create_fake_outbox(
                conn,
                execution_id=execution_id,
                channel=channel,
                chat_id=chat_id,
                content=content,
                media=media or [],
            )
        else:
            mark_execution_status(conn, execution_id, "validation_failed")


def summarize_status(conn: sqlite3.Connection, *, agent: str = DEFAULT_AGENT, task: str = DEFAULT_TASK) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM executions WHERE agent=? AND task=? GROUP BY status ORDER BY status",
        (agent, task),
    ).fetchall()
    outbox = conn.execute("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status ORDER BY status").fetchall()
    return {
        "executions": {row["status"]: int(row["n"]) for row in rows},
        "outbox": {row["status"]: int(row["n"]) for row in outbox},
    }


def stale_running_executions(
    conn: sqlite3.Connection,
    *,
    now: int | None = None,
    agent: str = DEFAULT_AGENT,
    task: str = DEFAULT_TASK,
) -> list[dict[str, Any]]:
    ts = now_ms() if now is None else now
    rows = conn.execute(
        """
        SELECT id, logical_key, status, attempt_no, lease_expires_ms, updated_at_ms
        FROM executions
        WHERE agent=? AND task=? AND status='running' AND lease_expires_ms IS NOT NULL AND lease_expires_ms < ?
        ORDER BY lease_expires_ms
        """,
        (agent, task, ts),
    ).fetchall()
    return [dict(row) for row in rows]


def pending_or_unknown_outbox(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, delivery_key, execution_id, channel, chat_id, component, status, attempts, last_error, updated_at_ms
        FROM outbox
        WHERE status NOT IN ('sent_confirmed', 'cancelled')
        ORDER BY updated_at_ms
        """
    ).fetchall()
    return [dict(row) for row in rows]


def health_report(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        **summarize_status(conn),
        "stale_running": stale_running_executions(conn),
        "pending_or_unknown_outbox": pending_or_unknown_outbox(conn),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect local agent reliability state.")
    parser.add_argument("action", choices=["status", "health"])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    conn = connect(args.db)
    if args.action == "status":
        payload = summarize_status(conn)
    else:
        payload = health_report(conn)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
