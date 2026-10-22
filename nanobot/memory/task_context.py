"""Versioned, scoped task projections. Raw events remain the source of truth."""
from __future__ import annotations

import json
import uuid
from typing import Any

from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope, RawEvent


class TaskContextStore:
    """Additive sidecar for the existing tasks table; compare-and-swap updates."""

    def __init__(self, store: ConversationEventStore):
        self.store = store
        with store.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS task_context_versions (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    version INTEGER NOT NULL,
                    owner_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    agent_id TEXT,
                    session_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(task_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_task_context_session
                    ON task_context_versions(owner_id, workspace_id, session_id);
                CREATE TABLE IF NOT EXISTS context_selection_runs (
                    request_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    metrics TEXT NOT NULL
                );
            """)

    def events(self, scope: MemoryScope, session_id: str, *, after: int = 0,
               user_only: bool = False, tail: int | None = None) -> list[RawEvent]:
        agent_sql, agent_params = self.store._agent_scope_sql(scope)
        sql = f"""SELECT * FROM events WHERE owner_id=?
            AND workspace_id IN ({','.join('?' for _ in scope.workspace_ids)})
            {agent_sql} AND session_id=? AND sequence>? AND redacted_at IS NULL"""
        params: list[Any] = [scope.owner_id, *scope.workspace_ids, *agent_params, session_id, after]
        if user_only:
            sql += " AND event_type='USER_MESSAGE'"
        sql += " ORDER BY sequence DESC, event_rowid DESC"
        if tail is not None:
            sql += " LIMIT ?"
            params.append(tail)
        with self.store.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self.store._row_to_event(row) for row in reversed(rows)]

    def list(self, scope: MemoryScope, session_id: str) -> list[dict[str, Any]]:
        agent_sql, agent_params = self.store._agent_scope_sql(scope)
        with self.store.connect() as conn:
            rows = conn.execute(f"""SELECT payload FROM task_context_versions v
                WHERE owner_id=? AND workspace_id IN ({','.join('?' for _ in scope.workspace_ids)})
                {agent_sql} AND session_id=? AND version=(
                    SELECT MAX(version) FROM task_context_versions WHERE task_id=v.task_id)
                ORDER BY rowid DESC""",
                [scope.owner_id, *scope.workspace_ids, *agent_params, session_id]).fetchall()
        return [json.loads(row[0]) for row in rows]

    def create(self, scope: MemoryScope, session_id: str, event: RawEvent,
               goal: str, checks: list[str]) -> dict[str, Any]:
        # Goal/checks are proposals. Only the exact user message carries user authority.
        existing = next((t for t in self.list(scope, session_id)
                         if t.get("origin_event_id") == event.event_id), None)
        if existing:
            return existing
        task_id = uuid.uuid4().hex
        state = dict(task_id=task_id, session_id=session_id, version=0, goal=goal,
                     completion_checks=checks, status="OPEN", coverage="partial",
                     user_constraints=[], proposals=[], decisions=[], unfinished=checks,
                     blockers=[], completed=[], supersessions=[], last_event_id=None,
                     last_sequence=0, origin_event_id=event.event_id)
        with self.store.connect() as conn:
            conn.execute("INSERT INTO tasks (task_id, title, status, source_event_id, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
                         (task_id, goal, "OPEN", event.event_id, self.store.now_ts(), "runtime"))
        return self.save(scope, state, expected_version=0)

    def save(self, scope: MemoryScope, state: dict[str, Any], *,
             expected_version: int) -> dict[str, Any]:
        state = {**state, "version": expected_version + 1}
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT MAX(version) FROM task_context_versions WHERE task_id=?",
                               (state["task_id"],)).fetchone()
            if int(row[0] or 0) != expected_version:
                raise ValueError("task_version_conflict")
            conn.execute("INSERT INTO task_context_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (state["task_id"], state["version"], scope.owner_id,
                          scope.workspace_ids[0], scope.agent_ids[0], state["session_id"],
                          json.dumps(state, ensure_ascii=False)))
            conn.execute("UPDATE tasks SET status=?, updated_at=?, updated_by=? WHERE task_id=?",
                         (state["status"], self.store.now_ts(), "runtime", state["task_id"]))
        return state

    @staticmethod
    def incorporate_users(state: dict[str, Any], events: list[RawEvent]) -> dict[str, Any]:
        state = json.loads(json.dumps(state))
        known = {item["event_id"] for item in state["user_constraints"]}
        for event in events:
            if event.actor != "user" or event.event_id in known:
                continue
            # Preserve amendments verbatim and in order. Never infer approval from a model.
            state["user_constraints"].append({"event_id": event.event_id,
                                               "sequence": event.sequence,
                                               "text": event.content, "kind": "user_original"})
            known.add(event.event_id)
        return state
