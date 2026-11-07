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


def _direct_text(text: str) -> str:
    # Quoted files/code are evidence, not fresh user authority.
    from nanobot.agent.context_relations import _blank_quotes
    direct = re.split(r"```", text or "")[0]
    direct = "\n".join(line for line in direct.splitlines() if not line.lstrip().startswith(">"))
    return _blank_quotes(direct)  # "'파일을 삭제하지 마세요'를 다듬어 줘" quotes text, not an instruction


_GRANT_KIND = {"edit": "change", "write": "change", "delete": "delete", "send": "send",
               "deploy": "deploy", "run": "execute"}


def granted_actions(text: str) -> set[str]:
    """Action kinds a user message explicitly requests on an external target (direct text only)."""
    from nanobot.agent.context_relations import parse_relations
    direct = _direct_text(text)
    return {_GRANT_KIND[r.action] for r in parse_relations(direct)
            if r.necessity == "required" and r.action in _GRANT_KIND
            and r.target not in {"answer", "prior_answer"}}


def granted_targets(text: str) -> dict[str, list[str]]:
    """Per granted kind, the named targets the permission covers ("*" when it names none)."""
    from nanobot.agent.context_relations import _FILE, parse_relations
    out: dict[str, set[str]] = {}
    for r in parse_relations(_direct_text(text)):
        if r.necessity == "required" and r.action in _GRANT_KIND and r.target not in {"answer", "prior_answer"}:
            named = _FILE.search(r.target_ref or "") or _FILE.search(r.source_span or "")
            # t39: the target as the user wrote it (full relative/absolute path), never its basename;
            # it is resolved to one canonical identifier against the workspace when compared.
            out.setdefault(_GRANT_KIND[r.action], set()).add(named.group(0) if named else "*")
    return {k: sorted(v) for k, v in out.items()}


def effective_scope(entries: list[tuple]) -> dict[str, set[str]]:
    """Replay user turns in order. Each denied kind maps to the targets a LATER user permission
    re-opened for it; a permission naming no target re-opens the whole kind; a later denial resets."""
    scope: dict[str, set[str]] = {}
    for entry in sorted(entries, key=lambda e: e[0]):
        _seq, deny, grant = entry[:3]
        targets = entry[3] if len(entry) > 3 else {k: ["*"] for k in grant}
        for kind in deny:
            scope[kind] = set()
        for kind in grant - deny:
            if kind not in scope:
                continue
            allowed = set(targets.get(kind) or ["*"])
            if "*" in allowed:
                scope.pop(kind)
            else:
                scope[kind] |= allowed
    return scope


def effective_denials(entries: list[tuple[int, set[str], set[str]]]) -> set[str]:
    """Replay user turns in order: a later explicit request for a kind supersedes only that kind's denial."""
    denied: set[str] = set()
    for _seq, deny, grant in sorted(entries, key=lambda e: e[0]):
        denied |= deny
        denied -= (grant - deny)
    return denied


def instruction_policy(text: str) -> dict:
    direct = _direct_text(text)
    denied = set()
    for kind, words in _ACTIONS.items():
        if re.search(rf"(?:{words})[^.!?\n]{{0,20}}(?:하지|금지|않|안돼)|"
                     rf"\b(?:do not|don't|never|no)\b[^.!?\n]{{0,20}}(?:{words})", direct, re.I):
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


# Prohibition wording the designer check used for these kinds besides instruction_policy ("배포 제외").
_EXTRA_DENIAL = {kind: rf"(?:{_ACTIONS[kind]}).{{0,20}}(?:하지|금지|제외|않)|(?:never|do not|don't).{{0,20}}(?:{_ACTIONS[kind]})"
                 for kind in ("deploy", "restart", "delete", "send")}


def scope_entry(sequence: int, text: str) -> tuple:
    """One user instruction as (sequence, denied kinds, granted kinds, granted targets)."""
    denied = set(instruction_policy(text)["denied_actions"])
    direct = _direct_text(text)
    denied |= {kind for kind, pattern in _EXTRA_DENIAL.items() if re.search(pattern, direct, re.I)}
    return (sequence, denied, granted_actions(text), granted_targets(text))


def instruction_state(entries: list[tuple]) -> tuple[dict[str, set[str]], set[str]]:
    """The one current-valid instruction state, shared by design validation and authorize.

    Order comes only from real positions (event sequence / message position; entry[0] is None when
    it is not known). No order is invented (t36): an unordered denial and a grant of the same kind
    (or an unordered grant of a kind that is otherwise denied) leave that kind ORDER-UNKNOWN instead
    of letting either win. Returns (scope, order_unknown): scope maps each kind still denied to the
    targets a later permission re-opened; order_unknown kinds need the user's confirmation before a
    related change, and do not block unrelated explanations or lookups."""
    ordered = [e for e in entries if e[0] is not None]
    unordered = [e for e in entries if e[0] is None]
    scope = effective_scope(ordered)
    granted_somewhere = {k for e in ordered for k in (e[2] - e[1])}
    unknown: set[str] = set()
    for i, entry in enumerate(unordered):
        deny, grant = entry[1], entry[2] - entry[1]
        others = [o for j, o in enumerate(unordered) if j != i]
        for kind in deny:
            if kind in scope and not scope[kind]:
                continue                                   # denied anyway, whatever the order
            if kind in granted_somewhere or kind in scope or any(kind in (o[2] - o[1]) for o in others):
                unknown.add(kind)
            else:
                scope[kind] = set()                        # the only instruction on that kind
        for kind in grant:
            if kind in scope or any(kind in o[1] for o in others):
                unknown.add(kind)
    for kind in unknown:
        scope.pop(kind, None)
    return scope, unknown


def instruction_scope(entries: list[tuple]) -> dict[str, set[str]]:
    """Kinds still denied (see instruction_state); order-unknown kinds are reported separately."""
    return instruction_state(entries)[0]


def goal_conflict_in_scope(goal: str, scope: dict[str, set[str]]) -> bool:
    """The design goal positively names an action kind the current instructions still deny, on a
    target no later permission re-opened. A granted kind/target is not a conflict."""
    from nanobot.agent.context_relations import _FILE
    goal_denied = set(instruction_policy(goal)["denied_actions"])
    names = {target_identifier(m.group(0), None) for m in _FILE.finditer(goal or "")}
    for kind, allowed in scope.items():
        if kind in goal_denied or not re.search(_ACTIONS[kind], goal or "", re.I):
            continue
        if names and names <= {target_identifier(a, None) for a in allowed if a != "*"}:
            continue
        return True
    return False


def target_identifier(ref: str, workspace: Any) -> str:
    """t39: one normalized full identifier per target (workspace-relative paths resolved, ``~``
    expanded, aliases resolved). Same-name files in different directories stay different."""
    from nanobot.agent.context_evidence import canonical
    return canonical(str(ref), workspace)


def target_permitted(kind: str, target: str | None, scope: dict[str, set[str]], workspace: Any) -> bool:
    """Action kind + full target identifier + workspace scope against the current instruction state."""
    if kind not in scope:
        return True
    if not target:
        return False
    ident = target_identifier(target, workspace)
    if workspace and not ident.startswith(target_identifier(".", workspace).rstrip("/") + "/"):
        return False                                      # outside the permitted workspace scope
    return ident in {target_identifier(a, workspace) for a in scope[kind] if a != "*"}


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
        if getattr(self.selected, "allocation", None):
            # Task linkage is settled at selection. In-flight events invalidate delivery,
            # but must not be silently merged into this task's requirement ledger.
            def hold(state):
                refs = state.setdefault("pending_instruction_refs", [])
                if event.event_id not in refs:
                    refs.append(event.event_id)
                state["status"], state["instruction_status"] = "BLOCKED", "RECONCILE"
            self.save(hold)
            self.selected.completion_reports.clear()
            return
        policy = instruction_policy(event.content or "")
        policy["granted_actions"] = sorted(granted_actions(event.content or ""))
        policy["granted_targets"] = granted_targets(event.content or "")

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
            if not policy["cancel"]:
                # t39: one instruction state. A follow-up permission/denial is stored where design
                # validation, execution checks and completion all read it (user_constraints), not
                # only in the execution-side policy list.
                state.setdefault("user_constraints", []).append({"event_id": event.event_id,
                    "sequence": event.sequence, "text": event.content, "kind": "user_original",
                    "instruction_version": state["instruction_version"]})
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

    def authorize(self, name: str, target: str | None = None) -> str | None:
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
        entries, seen = [], set()
        for instruction in state.get("instructions", []):
            pol = instruction.get("policy", {})
            seen.add(instruction.get("event_id"))
            seq = instruction.get("sequence")
            entries.append((int(seq) if seq is not None else None, set(pol.get("denied_actions", [])),
                            set(pol.get("granted_actions", [])), pol.get("granted_targets")
                            or {k: ["*"] for k in pol.get("granted_actions", [])}))
        for item in state.get("user_constraints", []):
            if item.get("event_id") in seen:
                continue
            seq = item.get("sequence")
            entries.append(scope_entry(int(seq) if seq is not None else None, item.get("text", "")))
        scope, order_unknown = instruction_state(entries)
        kind = tool_action(name)
        if kind in order_unknown or (name in {"exec", "run_cli_app"} and order_unknown):
            # Conflicting instructions whose order is not known: hold this change and ask.
            return ("Conflicting user instructions about this kind of action have no known order; "
                    "confirm with the user which one applies before changing anything.")
        note = getattr(self.selected, "note_instruction_read", None)
        if callable(note):
            note("execution_check")
        self.last_check = {"task_id": state.get("task_id"), "instruction_version": self.version,
                           "action": kind, "target": target_identifier(target, getattr(self.selected, "workspace", None)) if target else None}
        if (not target_permitted(kind, target, scope, getattr(self.selected, "workspace", None))
                or (name in {"exec", "run_cli_app"} and scope)):
            # A later permission lifts a denial only for the action kind and the target it names.
            return "Latest applicable user constraints forbid this state-changing action."
        return self.selected.evidence_error(for_mutation=True)

    def execution(self, call, status, **extra):
        def update(state):
            facts = state.setdefault("executions", [])
            entry = next((f for f in facts if f["tool_call_id"] == call.id), None)
            if entry is None:
                entry = {"tool_call_id": call.id, "name": call.name,
                         "instruction_version": self.version, "request_id": self.selected.trace.request_id}
                path = (call.arguments or {}).get("path") or (call.arguments or {}).get("file_path")
                if path:   # t52: the execution record names its target (claims of a later turn)
                    entry["target"] = str(path)
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
            error = self.authorize(call.name, (call.arguments or {}).get("path")
                                   if isinstance(call.arguments, dict) else None)
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
