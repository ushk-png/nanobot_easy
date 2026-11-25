"""Codewhale tool: coding runs stay detached instead of dying at the exec cap."""

from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from pydantic import Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_session_key
from nanobot.agent.tools.exec_job import (
    DEFAULT_EXEC_JOB_MANAGER,
    ExecJobManager,
    ExecJobTool,
    permission_stall,
)
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema
from nanobot.config_base import Base

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_VALUE_FLAGS = frozenset({
    "-C", "--workspace", "-w", "--config", "--profile", "--model", "--provider",
    "--base-url", "--api-key", "--permission-mode", "--sandbox-mode",
    "--approval-policy", "--set", "--verbosity", "--log-level", "--telemetry",
    "--max-subagents", "--enable", "--disable", "--conversation-mode",
    "-p", "--prompt", "-r", "--resume", "--session-id", "--sandbox",
    "--output-format", "--max-turns",
})
_SANDBOX_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_SHELL_BREAKS = frozenset({";", "&&", "||", "|"})
_DETACH_NOTE = (
    "Codewhale keeps running after this reply. The 600s foreground exec cap "
    "does not apply and this run is not killed for taking too long. "
    "Poll the same job. Do not start a second run for this task. "
    "If it has already stopped, resume that session id instead of launching a new one."
)

_STALL_NOTE = (
    "Codewhale was denied permission to run commands, so it stopped without doing the work; its exit code 0 "
    "is not a result. Nobody can approve a prompt in a non-interactive run. Do not ask the user to approve it: "
    "start the task again with this tool, which runs with automatic approvals."
)


class CodewhaleToolConfig(Base):
    """Codewhale tool configuration."""

    enable: bool = False
    binary: str = ""
    # Hard limit in seconds for a coding run. 0 means do not kill the process.
    timeout: int = Field(default=0, ge=0)
    # `codewhale exec` has nobody to approve a prompt: without --auto every shell command is denied and the run
    # ends with exit code 0 having done nothing.
    auto_approve: bool = True
    # Sandbox policy passed as --sandbox (for example workspace-write). Empty keeps Codewhale's own default.
    sandbox: str = ""


def _is_single_command(command: str) -> bool:
    """No unquoted ``;``, ``&``, ``&&``, ``||``, ``|`` or newline (so ``sleep 9;codewhale exec`` fails)."""
    if "\n" in command:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return not any(set(token) <= set(";&|") for token in lexer)
    except ValueError:
        return False


def is_codewhale_agent_command(command: str, *, standalone: bool = False) -> bool:
    """True when a shell command starts a Codewhale coding run (`codewhale exec`).

    With ``standalone`` the whole command must be that one invocation (no ``;``, ``&&``, ``||``
    or pipes). The exec tool uses it to lift the timeout: a segment such as
    ``sleep 999999; codewhale exec x`` must not make the entire command unbounded.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if standalone and not _is_single_command(command):
        return False
    starts = {0}
    for index, token in enumerate(tokens):
        if token in _SHELL_BREAKS:
            starts.add(index + 1)
    return any(
        start < len(tokens)
        and Path(tokens[start]).name == "codewhale"
        and _launches_agent(tokens[start + 1:])
        for start in starts
    )


def _launches_agent(rest: list[str]) -> bool:
    index = 0
    while index < len(rest):
        item = rest[index]
        if item in _VALUE_FLAGS:
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        return item == "exec"
    return False


def build_codewhale_command(
    binary: str,
    workspace: str,
    prompt: str,
    session_id: str | None = None,
    *,
    auto: bool = True,
    sandbox: str = "",
) -> str:
    """Build a quoted non-interactive Codewhale command. No API key is added."""
    parts = [shlex.quote(binary), "-C", shlex.quote(workspace), "exec"]
    if auto:
        parts.append("--auto")
    if sandbox:
        if not _SANDBOX_RE.fullmatch(sandbox):
            raise ValueError(f"unsupported sandbox policy: {sandbox!r}")
        parts.extend(["--sandbox", sandbox])
    if session_id:
        parts.extend(["--resume", shlex.quote(session_id)])
    if prompt.startswith("-"):
        parts.append("--")
    parts.append(shlex.quote(prompt))
    return " ".join(parts)


def _same_path_key(path: str) -> str | None:
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return None


def _annotate(result: ToolResult, note: str) -> ToolResult:
    updated = ToolResult(f"{result}\n{note}", is_error=result.is_error)
    for name in ("execution_target", "job_evidence"):
        if hasattr(result, name):
            setattr(updated, name, getattr(result, name))
    return updated


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "run: start or resume a coding task. status: check one detached job.",
            enum=["run", "status"],
            nullable=True,
        ),
        prompt=StringSchema(
            "What to change, including paths, constraints, and how to verify. Required for run.",
            nullable=True,
        ),
        workspace=StringSchema(
            "Repository root Codewhale may edit. Defaults to this agent's workspace.",
            nullable=True,
        ),
        session_id=StringSchema(
            "Resume this Codewhale session instead of starting a new one.",
            nullable=True,
        ),
        job_id=StringSchema("Job id returned by run. Required for status.", nullable=True),
        tail_lines=IntegerSchema(
            80,
            description="Log lines to include for status (1-200).",
            minimum=1,
            maximum=200,
            nullable=True,
        ),
    )
)
class CodewhaleTool(Tool):
    """Run Codewhale for code changes and keep the process past the exec cap."""

    _scopes = {"core", "subagent"}
    config_key = "codewhale"

    @classmethod
    def config_cls(cls):
        return CodewhaleToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        cfg = getattr(ctx.config, "codewhale", None)
        return bool(cfg and cfg.enable)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        cfg = ctx.config.codewhale
        exec_cfg = getattr(ctx.config, "exec", None)
        return cls(
            workspace=Path(ctx.workspace),
            binary=cfg.binary,
            timeout=cfg.timeout,
            auto_approve=cfg.auto_approve,
            sandbox=cfg.sandbox,
            restrict_to_workspace=bool(ctx.config.restrict_to_workspace),
            webui_allow_local_service_access=bool(
                getattr(ctx.config, "webui_allow_local_service_access", True)
            ),
            exec_deny_patterns=list(getattr(exec_cfg, "deny_patterns", None) or []),
            exec_sandbox=getattr(exec_cfg, "sandbox", "") or "",
            exec_path_prepend=getattr(exec_cfg, "path_prepend", "") or "",
            exec_path_append=getattr(exec_cfg, "path_append", "") or "",
            exec_allowed_env_keys=list(getattr(exec_cfg, "allowed_env_keys", None) or []),
        )

    def __init__(
        self,
        *,
        workspace: Path,
        binary: str = "",
        timeout: int = 0,
        restrict_to_workspace: bool = False,
        webui_allow_local_service_access: bool = True,
        job_manager: ExecJobManager | None = None,
        auto_approve: bool = True,
        sandbox: str = "",
        exec_deny_patterns: list[str] | None = None,
        exec_sandbox: str = "",
        exec_path_prepend: str = "",
        exec_path_append: str = "",
        exec_allowed_env_keys: list[str] | None = None,
    ) -> None:
        self.auto_approve = auto_approve
        # Operator exec protections (config.exec). exec_sandbox is the exec tool's sandbox,
        # unrelated to Codewhale's own --sandbox policy in `sandbox`.
        self.exec_deny_patterns = list(exec_deny_patterns or [])
        self.exec_sandbox = exec_sandbox
        self.exec_path_prepend = exec_path_prepend
        self.exec_path_append = exec_path_append
        self.exec_allowed_env_keys = list(exec_allowed_env_keys or [])
        # An agent confined to its workspace must not hand Codewhale a shell without a sandbox.
        self.sandbox = sandbox or ("workspace-write" if restrict_to_workspace and auto_approve else "")
        self.workspace = Path(workspace)
        self.binary = binary
        self.hard_timeout = None if timeout <= 0 else timeout
        self.restrict_to_workspace = restrict_to_workspace
        self.webui_allow_local_service_access = webui_allow_local_service_access
        self._manager = job_manager or DEFAULT_EXEC_JOB_MANAGER

    @property
    def name(self) -> str:
        return "codewhale"

    @property
    def description(self) -> str:
        return (
            "Implement or modify code with the local Codewhale agent. "
            "Use this instead of apply_patch, edit_file, or write_file when it is available. "
            "The run is detached and is not killed at the 600-second exec cap. "
            "Poll action=status with the returned job_id. Do not start a second run "
            "while one is still going. If a run stopped early, call run again with its "
            "session_id to resume. Do not put API keys or secrets in the prompt."
        )

    def _resolve_binary(self) -> str | None:
        if self.binary:
            path = Path(self.binary).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
            return None
        return shutil.which("codewhale")

    def _running_codewhale_jobs(self) -> list[tuple[Any, str | None]]:
        """Running codewhale jobs of every owner, with their `-C` workspace (None when unparsable)."""
        found = []
        for job in self._manager.list_all():
            if job.returncode is not None:
                continue
            try:
                tokens = shlex.split(job.command)
            except ValueError:
                tokens = None
            if tokens is None:
                tokens = job.command.split()
                if tokens and Path(tokens[0]).name == "codewhale":
                    found.append((job, None))  # unparsable: never treated as the same workspace
                continue
            if tokens and Path(tokens[0]).name == "codewhale":
                workspace = None
                if "-C" in tokens[1:]:
                    index = tokens.index("-C", 1)
                    if index + 1 < len(tokens):
                        workspace = tokens[index + 1]
                found.append((job, workspace))
        return found

    def _running_job_id(self) -> str | None:
        """Id of any running codewhale job, whoever owns it."""
        jobs = self._running_codewhale_jobs()
        return jobs[0][0].job_id if jobs else None

    def _blocking_job(self, target: str) -> tuple[Any, bool] | None:
        """A running codewhale job that forbids a new run: (job, owned by this session).

        One run at a time per session (any workspace), and one run per workspace across sessions.
        """
        owner = current_request_session_key()
        target_path = _same_path_key(target)
        for job, workspace in self._running_codewhale_jobs():
            if job.owner_session_key == owner:
                return job, True
        for job, workspace in self._running_codewhale_jobs():
            if workspace is not None and target_path is not None and _same_path_key(workspace) == target_path:
                return job, False
        return None

    async def _jobs(self) -> ExecJobTool:
        return ExecJobTool(manager=self._manager, workspace=str(self.workspace))

    async def execute(
        self,
        action: str | None = None,
        prompt: str | None = None,
        workspace: str | None = None,
        session_id: str | None = None,
        job_id: str | None = None,
        tail_lines: int | None = None,
        **kwargs: Any,
    ) -> str:
        del kwargs
        selected = action or "run"
        if selected == "status":
            if not job_id:
                return ToolResult.error("Error: status requires job_id.")
            jobs = await self._jobs()
            result = await jobs.execute(action="status", job_id=job_id, tail_lines=tail_lines or 80)
            if isinstance(result, ToolResult) and result.is_error:
                return result
            text = str(result)
            if "state: running" in text:
                note = _DETACH_NOTE
            elif permission_stall(text):
                note = _STALL_NOTE
            elif "state: timed_out" in text or "state: killed" in text:
                note = "This Codewhale run stopped. Resume it with session_id. Do not start the same task from scratch."
            else:
                note = "Check the diff and tests before treating the coding task as done."
            return _annotate(result if isinstance(result, ToolResult) else ToolResult(str(result)), note)

        if selected != "run":
            return ToolResult.error("Error: action must be run or status.")
        if not prompt or not prompt.strip():
            return ToolResult.error("Error: run requires a prompt.")
        if session_id and not _SESSION_ID_RE.fullmatch(session_id):
            return ToolResult.error("Error: session_id has unsupported characters.")
        target = str(Path(workspace).expanduser()) if workspace else str(self.workspace)
        blocking = self._blocking_job(target)
        if blocking is not None:
            job, own = blocking
            if not own:
                # Another session's job: do not reveal its id or log (exec_job owner isolation).
                return ToolResult.error("Error: 다른 대화에서 같은 작업공간을 수정하는 코드웨일 작업이 실행 중입니다.")
            return ToolResult.error(
                f"Error: codewhale job {job.job_id} is still running. "
                "Poll it with action=status. Do not start another run."
            )
        binary = self._resolve_binary()
        if not binary:
            configured = self.binary or "codewhale on PATH"
            return ToolResult.error(f"Error: Codewhale binary is not executable: {configured}")
        try:
            command = build_codewhale_command(
                binary, target, prompt.strip(), session_id, auto=self.auto_approve, sandbox=self.sandbox,
            )
        except ValueError as exc:
            return ToolResult.error(f"Error: {exc}")
        from nanobot.agent.tools.shell import ExecTool

        # allow_patterns are deliberately not passed: an exec allow-list never names `codewhale ...`,
        # so it would block every run instead of narrowing it.
        runner = ExecTool(
            working_dir=str(self.workspace),
            timeout=60,
            background_timeout=0,
            restrict_to_workspace=self.restrict_to_workspace,
            webui_allow_local_service_access=self.webui_allow_local_service_access,
            job_manager=self._manager,
            deny_patterns=self.exec_deny_patterns,
            sandbox=self.exec_sandbox,
            path_prepend=self.exec_path_prepend,
            path_append=self.exec_path_append,
            allowed_env_keys=self.exec_allowed_env_keys,
        )
        prepared = runner._prepare_command(command, target, background=True)
        if isinstance(prepared, str):
            return prepared
        prepared.timeout = self.hard_timeout
        result = await runner._execute_background(prepared, command)
        return _annotate(result, _DETACH_NOTE)
