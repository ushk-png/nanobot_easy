"""SQLite persistence for workflow tasks."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from nanobot.workflow.schema import WorkflowEnvelope, WorkflowPrincipal


class WorkflowStore:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.root = self.workspace / ".workflow"
        self.path = self.root / "workflow.db"
        self.root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_tasks (
                    task_id TEXT PRIMARY KEY,
                    principal_json TEXT NOT NULL,
                    definition_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    delivery_state TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    trace_json TEXT NOT NULL,
                    question_id TEXT,
                    resume_next TEXT,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    updated_at REAL NOT NULL DEFAULT (strftime('%s','now')),
                    lease_until REAL DEFAULT 0,
                    delivered_at REAL DEFAULT 0
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_workflow_principal ON workflow_tasks(principal_json)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_workflow_state ON workflow_tasks(state)")

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _loads(text: str) -> Any:
        return json.loads(text) if text else None

    def upsert_task(
        self,
        *,
        task_id: str,
        principal: WorkflowPrincipal,
        definition_id: str,
        envelope: WorkflowEnvelope,
        context: dict[str, Any],
        data: dict[str, Any] | None = None,
        trace: list[dict[str, Any]] | None = None,
        question_id: str | None = None,
        resume_next: str | None = None,
    ) -> None:
        state = envelope.state or "RUNNING"
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO workflow_tasks (
                    task_id, principal_json, definition_id, state, delivery_state,
                    envelope_json, context_json, data_json, trace_json, question_id,
                    resume_next, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))
                ON CONFLICT(task_id) DO UPDATE SET
                    state=excluded.state,
                    delivery_state=excluded.delivery_state,
                    envelope_json=excluded.envelope_json,
                    context_json=excluded.context_json,
                    data_json=excluded.data_json,
                    trace_json=excluded.trace_json,
                    question_id=excluded.question_id,
                    resume_next=excluded.resume_next,
                    updated_at=strftime('%s','now')
                """,
                (
                    task_id,
                    self._dumps(principal.model_dump()),
                    definition_id,
                    state,
                    envelope.delivery_state,
                    self._dumps(envelope.model_dump()),
                    self._dumps(context),
                    self._dumps(data or {}),
                    self._dumps(trace or []),
                    question_id,
                    resume_next,
                ),
            )

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, principal: WorkflowPrincipal, *, states: set[str] | None = None) -> list[dict[str, Any]]:
        principal_json = self._dumps(principal.model_dump())
        sql = "SELECT * FROM workflow_tasks WHERE principal_json=?"
        args: list[Any] = [principal_json]
        if states:
            placeholders = ",".join("?" for _ in states)
            sql += f" AND state IN ({placeholders})"
            args.extend(sorted(states))
        sql += " ORDER BY updated_at DESC"
        with self._connect() as con:
            rows = con.execute(sql, args).fetchall()
        return [self._row_to_task(row) for row in rows]

    def update_envelope(self, task_id: str, envelope: WorkflowEnvelope) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE workflow_tasks SET state=?, delivery_state=?, envelope_json=?, updated_at=strftime('%s','now') WHERE task_id=?",
                (envelope.state, envelope.delivery_state, self._dumps(envelope.model_dump()), task_id),
            )

    def list_expired_leases(self, *, now: float, states: set[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM workflow_tasks WHERE lease_until <= ?"
        args: list[Any] = [now]
        if states:
            placeholders = ",".join("?" for _ in states)
            sql += f" AND state IN ({placeholders})"
            args.extend(sorted(states))
        sql += " ORDER BY updated_at ASC"
        with self._connect() as con:
            rows = con.execute(sql, args).fetchall()
        return [self._row_to_task(row) for row in rows]

    def set_lease(self, task_id: str, *, lease_until: float) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE workflow_tasks SET lease_until=?, updated_at=strftime('%s','now') WHERE task_id=?",
                (lease_until, task_id),
            )

    def mark_delivered(self, task_id: str, *, handed_to_channel: bool = True) -> None:
        state = "handed_to_channel" if handed_to_channel else "failed"
        task = self.get_task(task_id)
        if not task:
            return
        envelope = WorkflowEnvelope.model_validate(task["envelope"])
        envelope.delivery_state = state  # type: ignore[assignment]
        with self._connect() as con:
            con.execute(
                "UPDATE workflow_tasks SET delivery_state=?, envelope_json=?, delivered_at=strftime('%s','now'), updated_at=strftime('%s','now') WHERE task_id=?",
                (state, self._dumps(envelope.model_dump()), task_id),
            )

    def _row_to_task(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "task_id": row["task_id"],
            "principal": self._loads(row["principal_json"]),
            "definition_id": row["definition_id"],
            "state": row["state"],
            "delivery_state": row["delivery_state"],
            "envelope": self._loads(row["envelope_json"]),
            "context": self._loads(row["context_json"]),
            "data": self._loads(row["data_json"]),
            "trace": self._loads(row["trace_json"]),
            "question_id": row["question_id"],
            "resume_next": row["resume_next"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "lease_until": row["lease_until"],
            "delivered_at": row["delivered_at"],
        }
