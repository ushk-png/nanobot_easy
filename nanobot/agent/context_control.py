"""Versioned instruction boundaries for selective turns, independent of task notes."""
from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from nanobot.agent.tools.base import ToolResult
from nanobot.session.history_visibility import is_hidden_history_message

_CANCEL = re.compile(r"(?:취소|중단)(?=\s*(?:해|하|했|할|[.!?\n]|$))|멈춰|그만|\b(?:cancel|stop|abort)\b", re.I)
_NEGATED_CANCEL = re.compile(r"(?:취소|중단)[^.!?\n]{0,8}(?:하지|말|금지)|(?:do not|don't|never)\s+(?:cancel|stop|abort)", re.I)
_EXPLANATION = re.compile(r"방법|어떻게|\bhow\b", re.I)
_CHANGE = re.compile(r"대신|바꾸|바꿔|변경|제외|취소하고|이제|만\s*(?:설명|진행|해)|\b(?:instead|replace|change|only)\b", re.I)
_RESUME = re.compile(r"재개|다시\s*(?:시작|진행)|취소\s*철회|\bresume\b", re.I)
_READ_ONLY = re.compile(r"읽기만|검토만|읽기\s*전용|\bread.?only\b|review only|no changes", re.I)
_ACTIONS = {
    "change": r"수정|변경|저장|쓰기|\b(?:modify|edit|write|save|change)\b",
    "delete": r"삭제|\b(?:delete|remove)\b", "restart": r"재시작|\brestart\b",
    "send": r"발송|전송|\bsend\b", "deploy": r"배포|\bdeploy\b",
    "install": r"설치|\binstall\b", "create": r"생성|만들|\bcreate\b",
    "execute": r"실행|\b(?:run|execute)\b",
}


def instruction_policy(text: str) -> dict:
    # Quoted files/code are evidence, not fresh user authority.
    direct = re.split(r"```", text)[0]
    direct = "\n".join(line for line in direct.splitlines() if not line.lstrip().startswith(">"))
    denied = set()
    for kind, words in _ACTIONS.items():
        if re.search(rf"(?:{words})[^.!?\n]{{0,20}}(?:하지|금지|않|안돼)|"
                     rf"(?:do not|don't|never|no)[^.!?\n]{{0,20}}(?:{words})", direct, re.I):
            denied.add(kind)
    if _READ_ONLY.search(direct):
        denied.update(_ACTIONS)
    cancelled = any(_CANCEL.search(clause) and not _EXPLANATION.search(clause) and not _NEGATED_CANCEL.search(clause)
                    for clause in re.split(r"[.!?\n]", direct))
    replace_task = (cancelled and bool(_CHANGE.search(direct))) or bool(re.search(
        r"목표[^.!?\n]{0,20}(?:바꿔|변경)|\b(?:instead|replace the task)\b", direct, re.I))
    return {"denied_actions": sorted(denied), "cancel": cancelled,
            "replace_task": replace_task, "replace": bool(_CHANGE.search(direct)),
            "explicit_resume": bool(_RESUME.search(direct))}


def goal_conflict(goal: str, text: str) -> bool:
    policy = instruction_policy(text)
    goal_policy = instruction_policy(goal)
    return any(re.search(_ACTIONS[kind], goal, re.I) and kind not in goal_policy["denied_actions"]
               for kind in policy["denied_actions"])


def tool_action(name: str) -> str:
    return {"message": "send", "cron": "create", "write_file": "change", "edit_file": "change",
            "apply_patch": "change", "exec": "execute", "run_cli_app": "execute",
            "forget_memory_events": "delete"}.get(name, "change")


class TaskControl:
    def __init__(self, selected: Any, tasks: Any = None):
        self.selected, self.tasks = selected, tasks
        self.pending: list[dict] = []
        self.active: str | None = None
        self.seen = (selected.current_event.sequence if selected.current_event else 0)
        self.lock = asyncio.Lock()
        self.execution_lock = asyncio.Lock()

    @property
    def version(self):
        return int((self.selected.state or {}).get("instruction_version", 1))

    @property
    def cancelled(self):
        state = self.selected.state or {}
        return state.get("instruction_status") == "CANCELLED" or state.get("status") == "CANCELLED"

    def save(self, change):
        selected = self.selected
        if not selected.state:
            return
        for _ in range(3):
            state = selected.state
            if self.tasks and selected.scope:
                state = next((s for s in self.tasks.list(selected.scope, state["session_id"])
                              if s["task_id"] == state["task_id"]), state)
            change(state)
            try:
                selected.state = (self.tasks.save(selected.scope, state, expected_version=state["version"])
                                  if self.tasks and selected.scope else state)
                return
            except ValueError:
                continue
        selected.blocked = "task_version_conflict"

    def refresh(self):
        if not self.tasks or not self.selected.state:
            return
        latest = next((s for s in self.tasks.list(self.selected.scope, self.selected.state["session_id"])
                       if s["task_id"] == self.selected.state["task_id"]), None)
        if latest:
            if latest["version"] > self.selected.state["version"]:
                self.selected.trace.count("state_conflicts", "newer_state_merged_without_overwrite")
            self.selected.state = latest
        for event in self.tasks.events(self.selected.scope, self.selected.state["session_id"],
                                       after=self.seen, user_only=True):
            self.ingest_event(event)

    def ingest_event(self, event):
        if event.sequence <= self.seen:
            return
        self.seen = event.sequence
        policy = instruction_policy(event.content or "")

        def update(state):
            ids = {item["event_id"] for item in state.get("instructions", [])}
            if event.event_id in ids:
                return
            state.setdefault("instructions", []).append({"event_id": event.event_id,
                "sequence": event.sequence, "kind": "user_instruction", "policy": policy})
            state["instruction_version"] = int(state.get("instruction_version", 1)) + 1
            state["last_event_id"], state["last_sequence"] = event.event_id, event.sequence
            state["instruction_status"] = "CANCELLED" if policy["cancel"] else "RECONCILE"
            state["status"] = "CANCELLED" if policy["cancel"] else "BLOCKED"
            if not state.get("blockers"):
                state["blockers"] = ["new_user_instruction_requires_reconciliation"]
            if policy["cancel"]:
                facts = state.get("executions", [])
                timing = "during_execution" if self.active else (
                    "after_completion" if any(e.get("status") == "ok" for e in facts) else "before_execution")
                state.setdefault("cancellations", []).append({"event_id": event.event_id,
                    "instruction_version": state["instruction_version"], "timing": timing,
                    "active_tool_call_id": self.active, "undo_performed": False,
                    "arrived_at": event.ts, "accepted_at": datetime.now(timezone.utc).isoformat()})
                state.setdefault("user_constraints", []).append({"event_id": event.event_id,
                    "sequence": event.sequence, "text": event.content, "kind": "user_original"})
            else:
                state.setdefault("pending_instruction_refs", []).append(event.event_id)
        self.save(update)
        self.selected.completion_reports.clear()

    def ingest_messages(self, messages: list[dict]):
        if not self.tasks or not self.selected.state:
            return
        for message in messages:
            text = message.get("content")
            if message.get("role") != "user" or is_hidden_history_message(message):
                continue
            if isinstance(text, list):
                text = "\n".join(b.get("text", "") for b in text if isinstance(b, dict))
            if not isinstance(text, str):
                continue
            if message.get("message_id") and not message.get("memory_event_id"):
                message["memory_event_id"] = "inbound:" + self.selected.state["session_id"] + ":" + str(message["message_id"])
            known_id = message.get("memory_event_id")
            if known_id:
                known = self.tasks.store.get_events(self.selected.scope, [known_id])
                if known:
                    self.ingest_event(known[0])
                    continue
            events = self.tasks.events(self.selected.scope, self.selected.state["session_id"], user_only=True)
            event = next((e for e in reversed(events) if e.content == text and e.sequence > self.seen), None)
            if event is None:
                event = self.tasks.store.append_event(scope=self.selected.scope,
                    conversation_id=self.selected.state["session_id"], session_id=self.selected.state["session_id"],
                    event_id=message.get("memory_event_id") or uuid.uuid4().hex, actor="user",
                    event_type="USER_MESSAGE", content=text)
            message["memory_event_id"] = event.event_id
            self.ingest_event(event)

    def authorize(self, name: str) -> str | None:
        self.refresh()
        if self.cancelled:
            return "Task cancelled; new mutations are blocked. Completed effects remain recorded."
        state = self.selected.state or {}
        if state.get("instruction_status") == "RECONCILE":
            return "New user instruction requires versioned reconciliation before mutation."
        if self.selected.blocked:
            return self.selected.blocked
        if state.get("blockers"):
            return "Task has unresolved blockers: " + ", ".join(state["blockers"])
        for instruction in state.get("instructions", []):
            if tool_action(name) in instruction.get("policy", {}).get("denied_actions", []):
                return "Latest versioned user instruction forbids this action."
        for item in state.get("user_constraints", []):
            denied = instruction_policy(item.get("text", ""))["denied_actions"]
            if tool_action(name) in denied or (name in {"exec", "run_cli_app"} and denied):
                return "Latest applicable user constraints forbid this state-changing action."
        return self.selected.evidence_error(for_mutation=True)

    def execution(self, call, status, **extra):
        def update(state):
            facts = state.setdefault("executions", [])
            entry = next((f for f in facts if f["tool_call_id"] == call.id), None)
            if entry is None:
                entry = {"tool_call_id": call.id, "name": call.name,
                         "instruction_version": self.version, "request_id": self.selected.trace.request_id}
                facts.append(entry)
            entry.update(status=status, **extra)
            if status == "running":
                entry.setdefault("started_at", datetime.now(timezone.utc).isoformat())
            elif status != "prepared":
                entry["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.save(update)

    async def run_mutation(self, call, tool, operation, drain):
        async with self.execution_lock:
            return await self._run_mutation(call, tool, operation, drain)

    async def _run_mutation(self, call, tool, operation, drain):
        # No suspension between the final version check and dispatch. Acceptance of a
        # later cancellation is linearized after this start and recorded accordingly.
        async with self.lock:
            messages = await drain()
            self.pending.extend(messages)
            self.refresh()
            previous = next((entry for entry in (self.selected.state or {}).get("executions", [])
                             if entry["tool_call_id"] == call.id), None)
            if previous:
                record = self.selected.observations.get(call.id, {})
                if previous["status"] == "ok" and record.get("_text") is not None:
                    return ToolResult(record["_text"]), {"name": call.name, "status": "ok", "reused": True}, None
                return ToolResult.error("Previous execution exists; inspect its recorded outcome before retry."), {
                    "name": call.name, "status": "blocked"}, None
            error = self.authorize(call.name)
            if error or messages:
                return ToolResult.error(error or "New instruction superseded prepared action"), {
                    "name": call.name, "status": "blocked"}, None
            version = self.version
            self.execution(call, "prepared", start_instruction_version=version)
            job = asyncio.create_task(operation())

        async def watch():
            while not job.done():
                await asyncio.sleep(0.02)
                self.pending.extend(await drain())
                self.refresh()
                if self.cancelled:
                    return

        watcher = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait({job, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done and not job.done() and self.cancelled:
                self.execution(call, "running", cancellation_requested=True)
                cancel = getattr(tool, "request_cancel", None)
                if callable(cancel):
                    returned = cancel(call.id)
                    if inspect.isawaitable(returned):
                        await returned
                elif getattr(tool, "supports_cancellation", False):
                    job.cancel()
            try:
                outcome = await asyncio.shield(job)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                self.execution(call, "unknown", interrupted=True, effects_require_verification=True)
                return ToolResult.error("Tool interrupted on cancellation; effects require verification."), {
                    "name": call.name, "status": "unknown"}, None
            result, event, error = outcome
            self.execution(call, event.get("status", "unknown"), cancellation_after_start=self.cancelled)
            self.active = None
            self.pending.extend(await drain())
            self.refresh()
            return outcome
        except asyncio.CancelledError:
            self.save(lambda s: s.update(instruction_status="CANCELLED", status="CANCELLED"))
            if getattr(tool, "supports_cancellation", False):
                job.cancel()
            self.execution(call, "unknown", effects_require_verification=True, cancellation_requested=True)

            def late(done):
                if done.cancelled():
                    return
                try:
                    result, event, _ = done.result()
                    self.selected.capture(call, result, event.get("status", "unknown"), tool)
                    self.execution(call, event.get("status", "unknown"), late_result=True,
                                   cancellation_after_start=True)
                except BaseException:
                    self.execution(call, "unknown", late_result=True)
            job.add_done_callback(late)
            raise
        finally:
            self.active = None
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
