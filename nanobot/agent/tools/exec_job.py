"""Detached background jobs for long-running exec commands (installs, upgrades)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_context, current_request_session_key
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema

_IS_WINDOWS = os.name == "nt"
JOB_DIR_NAME = ".nanobot/exec-jobs"
DEFAULT_TAIL_LINES = 40
MAX_TAIL_LINES = 400
_TAIL_READ_BYTES = 64_000


def job_log_dir(workspace: str | os.PathLike[str]) -> Path:
    root = Path(workspace).expanduser().resolve()
    directory = (root / JOB_DIR_NAME).resolve()
    directory.relative_to(root)  # Reject a log-directory symlink escaping the workspace.
    return directory


@dataclass(slots=True)
class ExecJob:
    job_id: str
    command: str
    cwd: str
    pid: int
    log_path: Path
    meta_path: Path
    started_at: float
    timeout: int | None
    owner_session_key: str | None = None
    process: asyncio.subprocess.Process | None = None
    returncode: int | None = None
    finished_at: float | None = None
    timed_out: bool = False
    killed: bool = False
    watcher: asyncio.Task | None = field(default=None, repr=False)
    # Chat that started the job: where its completion is announced once no turn is waiting on it.
    origin: dict[str, str] | None = None

    @property
    def state(self) -> str:
        if self.returncode is None:
            return "running"
        if self.timed_out:
            return "timed_out"
        if self.killed:
            return "killed"
        return "exited"

    def meta(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "command": self.command[:500], "cwd": self.cwd,
            "pid": self.pid, "log": str(self.log_path), "started_at": self.started_at,
            "timeout": self.timeout, "owner_session_key": self.owner_session_key,
            "state": self.state, "exit_code": self.returncode,
            "finished_at": self.finished_at,
        }


def _current_origin() -> dict[str, str] | None:
    ctx = current_request_context()
    if ctx is None or not ctx.channel or not ctx.chat_id or ctx.channel == "system":
        return None
    origin = {"channel": ctx.channel, "chat_id": str(ctx.chat_id),
              "session_key": ctx.session_key or f"{ctx.channel}:{ctx.chat_id}"}
    if ctx.message_id:
        origin["message_id"] = str(ctx.message_id)
    return origin


def _write_meta(job: ExecJob) -> None:
    with suppress(OSError):
        job.meta_path.write_text(json.dumps(job.meta(), ensure_ascii=False), encoding="utf-8")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def read_tail(path: Path, lines: int) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_READ_BYTES))
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


class ExecJobManager:
    """Tracks detached jobs started by exec(background=true).

    Jobs run in their own process group with output redirected to a log file
    under the workspace, so they survive a slow model turn and their result can
    be checked later. A hard timeout still applies unless configured to 0.
    """

    def __init__(self, *, max_running: int = 4, max_tracked: int = 50) -> None:
        self.max_running = max_running
        self.max_tracked = max_tracked
        self._jobs: dict[str, ExecJob] = {}
        self._lock = asyncio.Lock()
        self._announcer: Callable[[ExecJob], Awaitable[None]] | None = None

    def set_announcer(self, announcer: Callable[[ExecJob], Awaitable[None]] | None) -> None:
        """Register what runs when a job ends on its own, so the agent that started it is woken."""
        self._announcer = announcer

    async def start(
        self,
        *,
        command: str,
        display_command: str,
        cwd: str,
        env: dict[str, str],
        timeout: int | None,
        shell_program: str | None,
        login: bool,
        log_dir: Path,
        owner_session_key: str | None = None,
    ) -> ExecJob:
        from nanobot.agent.tools.shell import ExecTool

        async with self._lock:
            running = sum(1 for job in self._jobs.values() if job.returncode is None)
            if running >= self.max_running:
                raise RuntimeError(f"maximum running background jobs reached ({self.max_running})")
            log_dir.mkdir(parents=True, exist_ok=True)
            job_id = uuid.uuid4().hex[:12]
            log_path = log_dir / f"{job_id}.log"
            with log_path.open("ab") as log_fh:
                process = await ExecTool._spawn(
                    command, cwd, env, shell_program, login,
                    stdout=log_fh, stderr=asyncio.subprocess.STDOUT,
                )
            job = ExecJob(
                job_id=job_id, command=display_command, cwd=cwd, pid=process.pid,
                log_path=log_path, meta_path=log_dir / f"{job_id}.json",
                started_at=time.time(), timeout=timeout,
                owner_session_key=owner_session_key, process=process,
                origin=_current_origin(),
            )
            self._jobs[job_id] = job
            self._prune_locked()
        _write_meta(job)
        job.watcher = asyncio.create_task(self._watch(job))
        logger.info("exec background job {} started pid={} timeout={}", job_id, job.pid, timeout)
        return job

    async def _watch(self, job: ExecJob) -> None:
        process = job.process
        if process is None:
            return
        try:
            if job.timeout:
                try:
                    await asyncio.wait_for(process.wait(), timeout=job.timeout)
                except asyncio.TimeoutError:
                    job.timed_out = True
                    await self._kill(job)
            else:
                await process.wait()
        except asyncio.CancelledError:
            return
        job.returncode = process.returncode
        job.finished_at = time.time()
        _write_meta(job)
        logger.info("exec background job {} finished state={} exit={}", job.job_id, job.state, job.returncode)
        await self._announce(job)

    async def _announce(self, job: ExecJob) -> None:
        # A job the model killed is already known to it. Every other ending (exit, failure, timeout) may happen
        # after the turn that started the job is over, when nothing would ever read the log again.
        if self._announcer is None or job.origin is None or job.killed:
            return
        try:
            await self._announcer(job)
        except Exception:
            logger.exception("exec background job {} completion could not be announced", job.job_id)

    async def _kill(self, job: ExecJob) -> None:
        process = job.process
        if process is None or process.returncode is not None:
            return
        if not _IS_WINDOWS and process.pid:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5.0)

    def _prune_locked(self) -> None:
        finished = [jid for jid, job in self._jobs.items() if job.returncode is not None]
        while len(self._jobs) > self.max_tracked and finished:
            self._jobs.pop(finished.pop(0), None)

    def _visible(self, job_id: str, owner_session_key: str | None) -> ExecJob | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if job.owner_session_key != owner_session_key:
            return None
        return job

    async def kill(self, job_id: str, owner_session_key: str | None = None) -> ExecJob | None:
        job = self._visible(job_id, owner_session_key)
        if job is None:
            return None
        if job.returncode is None:
            job.killed = True
            await self._kill(job)
            if job.watcher is not None:
                with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(job.watcher), timeout=5.0)
        return job

    def get(self, job_id: str, owner_session_key: str | None = None) -> ExecJob | None:
        return self._visible(job_id, owner_session_key)

    def list(self, owner_session_key: str | None = None) -> list[ExecJob]:
        return [job for jid, job in sorted(self._jobs.items(), key=lambda item: item[1].started_at)
                if self._visible(jid, owner_session_key) is not None]


DEFAULT_EXEC_JOB_MANAGER = ExecJobManager()

_NOTICE_TAIL_LINES = 25
_NOTICE_TAIL_CHARS = 3000


def exec_job_notice(job: ExecJob):
    """The inbound message that tells the owning chat a background job has ended.

    It reuses the sub-agent result channel: the loop routes it into the running turn when there is one, otherwise
    it starts a turn, and a repeated id is not persisted twice.
    """
    from nanobot.bus.events import InboundMessage
    from nanobot.utils.prompt_templates import render_template

    origin = job.origin or {}
    ended = job.finished_at or time.time()
    if job.timed_out:
        outcome = f"hit its {job.timeout}s limit and was terminated"
    elif job.returncode == 0:
        outcome = "finished successfully"
    else:
        outcome = f"failed with exit code {job.returncode}"
    tail = read_tail(job.log_path, _NOTICE_TAIL_LINES)[-_NOTICE_TAIL_CHARS:]
    content = render_template(
        "agent/exec_job_notice.md",
        job_id=job.job_id, outcome=outcome, state=job.state, exit_code=job.returncode,
        elapsed=f"{max(0.0, ended - job.started_at):.0f}", command=" ".join(job.command.split())[:200],
        log_path=str(job.log_path), tail=tail or "(no output)",
    )
    metadata: dict[str, Any] = {
        "injected_event": "subagent_result", "subagent_task_id": f"exec-job-{job.job_id}",
    }
    if origin.get("message_id"):
        metadata["origin_message_id"] = origin["message_id"]
    return InboundMessage(
        channel="system", sender_id="subagent",
        chat_id=f"{origin.get('channel', 'cli')}:{origin.get('chat_id', 'direct')}",
        content=content, session_key_override=origin.get("session_key"), metadata=metadata,
    )


def format_job(job: ExecJob, tail_lines: int) -> str:
    now = job.finished_at or time.time()
    lines = [
        f"job_id: {job.job_id}",
        f"state: {job.state}",
        f"exit_code: {job.returncode if job.returncode is not None else 'n/a (still running)'}",
        f"elapsed: {max(0.0, now - job.started_at):.1f}s",
        f"pid: {job.pid}",
        f"log: {job.log_path}",
        f"command: {' '.join(job.command.split())[:200]}",
    ]
    if job.timed_out:
        lines.append(f"The job hit its {job.timeout}s limit and was terminated; "
                     "it may have partially completed. Verify actual state before retrying.")
    tail = read_tail(job.log_path, tail_lines)
    lines.append(f"--- last {tail_lines} log lines ---")
    lines.append(tail or "(no output yet)")
    result = ToolResult("\n".join(lines))
    result.execution_target = f"exec-job://{job.job_id}"
    result.job_evidence = {"target": result.execution_target,
                           "state": job.state, "exit_code": job.returncode}
    return result


def format_untracked(job_id: str, log_dir: Path, tail_lines: int,
                     owner_session_key: str | None = None) -> str | None:
    """Report a job started by an earlier runtime process from its metadata file."""
    try:
        meta_path = (log_dir / f"{job_id}.json").resolve()
        meta_path.relative_to(log_dir.resolve())
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or meta.get("owner_session_key") != owner_session_key:
            return None
        log_path = Path(str(meta.get("log") or "")).resolve()
        log_path.relative_to(log_dir.resolve())
    except (OSError, ValueError):
        return None
    if meta.get("exit_code") is not None:
        state = meta.get("state") or "exited"
        exit_code = meta.get("exit_code")
    elif isinstance(meta.get("pid"), int) and _pid_alive(meta["pid"]):
        state, exit_code = "running (untracked: started by an earlier runtime)", "unknown"
    else:
        state, exit_code = "unknown (process gone; exit code was not recorded)", "unknown"
    lines = [f"job_id: {job_id}", f"state: {state}", f"exit_code: {exit_code}",
             f"log: {meta.get('log')}",
             f"command: {' '.join(str(meta.get('command') or '').split())[:200]}",
             f"--- last {tail_lines} log lines ---",
             read_tail(log_path, tail_lines) or "(no output)"]
    if not state.startswith(("exited", "timed_out", "killed")):
        lines.append("Verify the actual state (versions, processes) before relying on this job.")
    result = ToolResult("\n".join(lines))
    result.execution_target = f"exec-job://{job_id}"
    result.job_evidence = {"target": result.execution_target,
                           "state": state, "exit_code": exit_code}
    return result


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "status: state, exit code and log tail of one job; list: jobs of this "
            "session; kill: terminate a running job (its process group).",
            enum=["status", "list", "kill"],
        ),
        job_id=StringSchema("Job id returned by exec(background=true). Required for status/kill.",
                            nullable=True),
        tail_lines=IntegerSchema(
            DEFAULT_TAIL_LINES,
            description=f"Log lines to return (default {DEFAULT_TAIL_LINES}, max {MAX_TAIL_LINES}).",
            minimum=1,
            maximum=MAX_TAIL_LINES,
            nullable=True,
        ),
        required=["action"],
    )
)
class ExecJobTool(Tool):
    """Check or stop background jobs started with exec(background=true)."""

    _scopes = {"core", "subagent"}
    config_key = "exec"

    @classmethod
    def config_cls(cls):
        from nanobot.agent.tools.shell import ExecToolConfig

        return ExecToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.exec.enable

    def __init__(self, *, manager: ExecJobManager | None = None, workspace: str | None = None) -> None:
        self._manager = manager or DEFAULT_EXEC_JOB_MANAGER
        self._workspace = workspace

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    @property
    def name(self) -> str:
        return "exec_job"

    @property
    def description(self) -> str:
        return (
            "Check or stop a background job started with exec(background=true). "
            "action=status returns running/exited, the exit code and the log tail; "
            "action=list lists this session's jobs; action=kill terminates a running "
            "job. A job that is still running is not finished: check again later "
            "instead of starting the same command again."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self,
        action: str,
        job_id: str | None = None,
        tail_lines: int | None = None,
        **kwargs: Any,
    ) -> str:
        owner = current_request_session_key()
        lines = min(max(tail_lines or DEFAULT_TAIL_LINES, 1), MAX_TAIL_LINES)
        if action == "list":
            jobs = self._manager.list(owner)
            if not jobs:
                return "No background jobs."
            return "\n".join(
                f"{job.job_id} | {job.state} | exit={job.returncode} | "
                f"{' '.join(job.command.split())[:120]}"
                for job in jobs
            )
        if action not in {"status", "kill"}:
            return ToolResult.error(f"Error: unknown action {action!r}; use status, list or kill.")
        if not job_id:
            return ToolResult.error("Error: job_id is required for status and kill.")
        if action == "kill":
            job = await self._manager.kill(job_id, owner)
            if job is None:
                return ToolResult.error(f"Error: background job not found: {job_id!r}")
            return format_job(job, lines)
        job = self._manager.get(job_id, owner)
        if job is not None:
            return format_job(job, lines)
        if self._workspace and "/" not in job_id and "\\" not in job_id and ".." not in job_id:
            try:
                directory = job_log_dir(self._workspace)
            except ValueError:
                return ToolResult.error("Error: background job logs are outside the workspace")
            untracked = format_untracked(job_id, directory, lines, owner)
            if untracked is not None:
                return untracked
        return ToolResult.error(f"Error: background job not found: {job_id!r}")
