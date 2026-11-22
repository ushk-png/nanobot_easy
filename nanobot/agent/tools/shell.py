"""Shell execution tool."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from loguru import logger
from pydantic import Field

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import current_request_session_key
from nanobot.agent.tools.exec_job import DEFAULT_EXEC_JOB_MANAGER, job_log_dir
from nanobot.agent.tools.exec_session import (
    DEFAULT_EXEC_SESSION_MANAGER,
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_YIELD_MS,
    MAX_OUTPUT_CHARS,
    MAX_YIELD_MS,
    clamp_session_int,
    format_session_poll,
)
from nanobot.agent.tools.sandbox import wrap_command
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.config.paths import get_media_dir
from nanobot.config_base import Base
from nanobot.security.workspace_access import current_scope_allows_loopback, current_tool_workspace
from nanobot.security.workspace_policy import is_path_within

_IS_WINDOWS = sys.platform == "win32"


# Policy note appended to recoverable workspace-boundary guard errors.
_WORKSPACE_BOUNDARY_NOTE = (
    "\n\nNote: this is a hard policy boundary, not a transient failure. "
    "Do NOT retry with shell tricks (symlinks, base64 piping, alternative "
    "tools, working_dir overrides). If the user genuinely needs this "
    "resource, tell them you cannot reach it under the current "
    "restrict_to_workspace policy and ask how to proceed."
)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""
    enable: bool = True
    timeout: int = Field(default=60, ge=0)  # Hard timeout (s); 0 = no limit. Not capped by the per-call max.
    # Hard timeout (s) for background=true jobs when the call gives none; 0 = no limit.
    background_timeout: int = Field(default=1800, ge=0)
    path_prepend: str = ""
    path_append: str = ""
    sandbox: str = ""
    allowed_env_keys: list[str] = Field(default_factory=list)
    allow_patterns: list[str] = Field(default_factory=list)
    deny_patterns: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class _PreparedCommand:
    command: str
    cwd: str
    env: dict[str, str]
    timeout: int | None
    shell_program: str | None
    login: bool


@tool_parameters(
    tool_parameters_schema(
        command=StringSchema("The shell command to execute"),
        cmd=StringSchema("Compatibility alias for command"),
        working_dir=StringSchema("Optional working directory for the command"),
        workdir=StringSchema("Compatibility alias for working_dir"),
        timeout=IntegerSchema(
            60,
            description=(
                "Timeout in seconds. Increase for long-running commands "
                "like compilation (default 60, max 600). With background=true "
                "it is the job's hard limit (default 1800, max 3600)."
            ),
            minimum=1,
            maximum=3600,
        ),
        background=BooleanSchema(
            description=(
                "Start the command as a detached background job and return a "
                "job_id immediately. Use for installs, upgrades and other "
                "commands that may run for minutes; check it with exec_job."
            ),
            default=False,
            nullable=True,
        ),
        shell=StringSchema(
            (
                "Override the Windows shell only when needed. Omit to use "
                "PowerShell by default (pwsh when available, else powershell). "
                "Pass 'cmd' only for cmd.exe syntax or cmd built-ins."
                if _IS_WINDOWS
                else "Override the Unix shell only when needed. Omit to use "
                "bash by default. Pass 'sh' for POSIX sh or 'zsh' for "
                "zsh-specific syntax."
            ),
            nullable=True,
        ),
        login=BooleanSchema(
            description="Whether to run bash/zsh with login shell semantics (default false).",
            default=False,
            nullable=True,
        ),
        yield_time_ms=IntegerSchema(
            description=(
                "Optional milliseconds to wait before returning output. "
                "When set, a still-running command returns a session_id that "
                "can be polled or written to with write_stdin. Omit this field "
                "to keep one-shot exec behavior."
            ),
            minimum=0,
            maximum=MAX_YIELD_MS,
            nullable=True,
        ),
        max_output_chars=IntegerSchema(
            description=(
                "Maximum output characters to return when yield_time_ms is used "
                "(default 10000, max 50000)."
            ),
            minimum=1000,
            maximum=MAX_OUTPUT_CHARS,
            nullable=True,
        ),
        max_output_tokens=IntegerSchema(
            description=(
                "Compatibility alias for max_output_chars. The current runtime "
                "uses a character budget."
            ),
            minimum=1000,
            maximum=MAX_OUTPUT_CHARS,
            nullable=True,
        ),
    )
)
class ExecTool(Tool):
    """Tool to execute shell commands."""
    _scopes = {"core", "subagent"}

    config_key = "exec"

    @classmethod
    def config_cls(cls):
        return ExecToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.exec.enable

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        cfg = ctx.config.exec
        return cls(
            working_dir=ctx.workspace,
            timeout=cfg.timeout,
            restrict_to_workspace=ctx.config.restrict_to_workspace,
            webui_allow_local_service_access=ctx.config.webui_allow_local_service_access,
            sandbox=cfg.sandbox,
            path_prepend=cfg.path_prepend,
            path_append=cfg.path_append,
            allowed_env_keys=cfg.allowed_env_keys,
            allow_patterns=cfg.allow_patterns,
            deny_patterns=cfg.deny_patterns,
            background_timeout=cfg.background_timeout,
        )

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        webui_allow_local_service_access: bool = True,
        allow_local_preview_access: bool | None = None,
        sandbox: str = "",
        path_prepend: str = "",
        path_append: str = "",
        allowed_env_keys: list[str] | None = None,
        session_manager: Any | None = None,
        job_manager: Any | None = None,
        background_timeout: int = 1800,
    ):
        self.timeout = timeout
        self.background_timeout = background_timeout
        self.working_dir = working_dir
        self.sandbox = sandbox
        self.deny_patterns = (deny_patterns or []) + [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format(?!=)\b",   # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
            # Block writes to nanobot internal state files (#2989).
            # history.jsonl / .dream_cursor are managed by append_history();
            # direct writes corrupt the cursor format and crash /dream.
            r">>?\s*\S*(?:history\.jsonl|\.dream_cursor)",            # > / >> redirect
            r"\btee\b[^|;&<>]*(?:history\.jsonl|\.dream_cursor)",     # tee / tee -a
            r"\b(?:cp|mv)\b(?:\s+[^\s|;&<>]+)+\s+\S*(?:history\.jsonl|\.dream_cursor)",  # cp/mv target
            r"\bdd\b[^|;&<>]*\bof=\S*(?:history\.jsonl|\.dream_cursor)",  # dd of=
            r"\bsed\s+-i[^|;&<>]*(?:history\.jsonl|\.dream_cursor)",  # sed -i
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        if allow_local_preview_access is not None:
            webui_allow_local_service_access = allow_local_preview_access
        self.webui_allow_local_service_access = webui_allow_local_service_access
        self.path_prepend = path_prepend
        self.path_append = path_append
        self.allowed_env_keys = allowed_env_keys or []
        self._session_manager = session_manager or DEFAULT_EXEC_SESSION_MANAGER
        self._job_manager = job_manager or DEFAULT_EXEC_JOB_MANAGER

    supports_cancellation = True

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600
    _MAX_BACKGROUND_TIMEOUT = 3600
    _MAX_OUTPUT = 10_000

    # Safety ceiling on bytes read from each stream, independent of timeout and of
    # max_output_chars (which truncates the already-decoded text afterward). Without
    # this, process.communicate() buffers stdout/stderr in memory with no size limit,
    # so a command that sustains high-volume output for the full timeout window can
    # accumulate hundreds of MB before truncation ever applies. Far above any real
    # max_output_chars value, so normal commands are unaffected.
    _MAX_DRAIN_BYTES = 20_000_000

    # Kernel device files safe as stdio redirect targets (#3599).
    _BENIGN_DEVICE_PATHS: frozenset[str] = frozenset({
        "/dev/null",
        "/dev/zero",
        "/dev/full",
        "/dev/random",
        "/dev/urandom",
        "/dev/stdin",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/tty",
    })

    @property
    def description(self) -> str:
        platform_note = (
            "On Windows, use PowerShell syntax by default; pass shell='cmd' "
            "only for cmd-specific commands. "
            if _IS_WINDOWS
            else "On Unix, commands run through bash by default; pass shell='sh' "
            "or shell='zsh' when needed. "
        )
        return (
            "Execute a shell command and return its output. "
            "Use this for tests, builds, package commands, git commands, and "
            "other process execution. Prefer read_file/find_files/grep for "
            "inspection and apply_patch/write_file/edit_file for file changes "
            "instead of cat, shell find/grep, echo, or sed. "
            "Use -y or --yes flags to avoid interactive prompts. "
            f"{platform_note}"
            "For long-running or interactive commands, pass yield_time_ms; "
            "if the command keeps running, exec returns a session_id that can "
            "be polled or written to with write_stdin. For installs, upgrades "
            "or other commands that may take minutes, pass background=true and "
            "check the job with exec_job instead of raising timeout. Output is "
            "truncated at 10 000 chars; timeout defaults to 60s."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self, command: str | None = None, cmd: str | None = None,
        working_dir: str | None = None, workdir: str | None = None,
        timeout: int | None = None, shell: str | None = None,
        login: bool | None = None, yield_time_ms: int | None = None,
        max_output_chars: int | None = None,
        max_output_tokens: int | None = None,
        background: bool | None = None,
        **kwargs: Any,
    ) -> str:
        command = command or cmd
        working_dir = working_dir or workdir
        if not command:
            return ToolResult.error("Error: Missing command. Provide command or cmd.")
        if max_output_chars is None:
            max_output_chars = max_output_tokens

        prepared = self._prepare_command(
            command, working_dir, timeout, shell, login, background=bool(background),
        )
        if isinstance(prepared, str):
            return prepared

        if background:
            return await self._execute_background(prepared, str(command))

        if yield_time_ms is not None:
            return await self._execute_session(prepared, yield_time_ms, max_output_chars)

        from nanobot.agent.task_note import active_task_note

        note = active_task_note.get()
        artifacts = []
        if note is not None and note.update is not None:
            from nanobot.agent.artifact_evidence import snapshot_writes

            access = current_tool_workspace(
                self.working_dir, restrict_to_workspace=self.restrict_to_workspace,
                sandbox_restricts_workspace=bool(self.sandbox),
            )
            artifacts = snapshot_writes(note.update.targets, access.project_path, access.allowed_root)

        try:
            process = await self._spawn(
                prepared.command,
                prepared.cwd,
                prepared.env,
                prepared.shell_program,
                prepared.login,
            )

            try:
                stdout, stderr, overflowed = await asyncio.wait_for(
                    self._drain_streams(process.stdout, process.stderr, self._MAX_DRAIN_BYTES),
                    timeout=prepared.timeout,
                )
            except asyncio.TimeoutError:
                info = await self._kill_process(process)
                self.last_termination = dict(info or {}, command=str(command)[:200])
                return ToolResult.error(timeout_error_text(prepared.timeout)
                                        + termination_note(self.last_termination))
            except asyncio.CancelledError:
                # t55: the outcome of the stop is kept (confirmed or not) for the caller's record.
                self.last_termination = dict(await self._kill_process(process), command=str(command)[:200])
                raise

            if overflowed:
                # The command is still producing output past _MAX_DRAIN_BYTES; stop
                # reading and kill it rather than let it keep running unread (its
                # pipe would otherwise fill and it would block, not exit on its own).
                info = await self._kill_process(process)
                self.last_termination = dict(info or {}, command=str(command)[:200])
            else:
                await process.wait()

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\nExit code: {process.returncode}")
            if overflowed:
                output_parts.append(
                    f"\nOutput exceeded the {self._MAX_DRAIN_BYTES:,}-byte safety limit; "
                    "the command was terminated. Outcome unknown: verify actual state "
                    "before retrying." + termination_note(self.last_termination)
                )

            result = "\n".join(output_parts) if output_parts else "(no output)"

            max_len = clamp_session_int(max_output_chars, self._MAX_OUTPUT, 1000, MAX_OUTPUT_CHARS)
            if len(result) > max_len:
                half = max_len // 2
                result = (
                    result[:half]
                    + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
                    + result[-half:]
                )

            from nanobot.agent.context_evidence import active_evidence_capture, test_command

            output = ToolResult(result, is_error=process.returncode != 0)
            output.execution_evidence = {"exit_code": process.returncode}
            if active_evidence_capture.get() or active_task_note.get() is not None:
                evidence_text = stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")
                passed = re.search(r"\b(\d+) passed\b", evidence_text)
                unittest_count = re.search(r"Ran (\d+) tests?", evidence_text)
                count = int(passed[1]) if passed else int(unittest_count[1]) if unittest_count else 0
                ran = test_command(command) and count > 0
                output.execution_evidence = {"exit_code": process.returncode,
                    "result_kind": "test_result" if ran else "execution_result",
                    "tests_executed": ran, "test_counts": {"observed_executed": count}}
            if process.returncode == 0 and artifacts:
                from nanobot.agent.artifact_evidence import changed_writes

                output.write_evidence = await asyncio.to_thread(
                    changed_writes, artifacts, access.project_path, access.allowed_root,
                )
            return output

        except Exception as e:
            return ToolResult.error(f"Error executing command: {str(e)}")

    async def _execute_session(
        self,
        prepared: _PreparedCommand,
        yield_time_ms: int | None,
        max_output_chars: int | None,
    ) -> str:
        try:
            session_id, poll = await self._session_manager.start(
                command=prepared.command,
                cwd=prepared.cwd,
                env=prepared.env,
                timeout=prepared.timeout,
                shell_program=prepared.shell_program,
                login=prepared.login,
                yield_time_ms=clamp_session_int(yield_time_ms, DEFAULT_YIELD_MS, 0, MAX_YIELD_MS),
                owner_session_key=current_request_session_key(),
                max_output_chars=clamp_session_int(
                    max_output_chars,
                    DEFAULT_MAX_OUTPUT_CHARS,
                    1000,
                    MAX_OUTPUT_CHARS,
                ),
            )
            result = format_session_poll(session_id, poll)
            return ToolResult.error(result) if poll.timed_out else result
        except Exception as exc:
            return ToolResult.error(f"Error executing command: {exc}")

    async def _execute_background(self, prepared: _PreparedCommand, display_command: str) -> str:
        try:
            job = await self._job_manager.start(
                command=prepared.command,
                display_command=display_command,
                cwd=prepared.cwd,
                env=prepared.env,
                timeout=prepared.timeout,
                shell_program=prepared.shell_program,
                login=prepared.login,
                log_dir=job_log_dir(self.working_dir or prepared.cwd),
                owner_session_key=current_request_session_key(),
            )
        except Exception as exc:
            return ToolResult.error(f"Error starting background job: {exc}")
        limit = f"{prepared.timeout}s" if prepared.timeout else "none"
        result = ToolResult(
            f"Background job started. job_id: {job.job_id}\n"
            f"pid: {job.pid}\nlog: {job.log_path}\ntimeout: {limit}\n"
            "The command keeps running after this reply. Check it with "
            f"exec_job(action='status', job_id='{job.job_id}') before reporting "
            "completion; do not start the same command again while it runs."
        )
        result.execution_target = f"exec-job://{job.job_id}"
        return result

    def _resolve_timeout(self, timeout: int | None) -> int | None:
        """Resolve the effective hard timeout in seconds (None = no limit).

        A per-call timeout supplied by the model stays capped at _MAX_TIMEOUT so
        the LLM cannot request unbounded execution. The config-level default
        (self.timeout) may exceed that cap, and 0 disables the limit entirely
        for trusted long-running tasks (#3595).
        """
        if timeout:
            return min(timeout, self._MAX_TIMEOUT)
        if self.timeout and self.timeout > 0:
            return self.timeout
        return None

    def _resolve_background_timeout(self, timeout: int | None) -> int | None:
        """Hard limit for a background job (None = no limit), capped like foreground."""
        if timeout:
            return min(timeout, self._MAX_BACKGROUND_TIMEOUT)
        if self.background_timeout and self.background_timeout > 0:
            return self.background_timeout
        return None

    def _prepare_command(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        shell: str | None = None,
        login: bool | None = None,
        *,
        background: bool = False,
    ) -> _PreparedCommand | str:
        access = current_tool_workspace(
            self.working_dir,
            restrict_to_workspace=self.restrict_to_workspace,
            sandbox_restricts_workspace=bool(self.sandbox),
        )
        workspace_root = str(access.project_path) if access.project_path is not None else self.working_dir
        cwd = working_dir or workspace_root or os.getcwd()

        # Prevent an LLM-supplied working_dir from escaping the configured
        # workspace when restrict_to_workspace is enabled (#2826). Without
        # this, a caller can pass working_dir="/etc" and then all absolute
        # paths under /etc would pass the _guard_command check that anchors
        # on cwd.
        if access.restrict_to_workspace and workspace_root:
            try:
                requested = Path(cwd).expanduser().resolve()
                resolved_root = Path(workspace_root).expanduser().resolve()
            except Exception:
                return ToolResult.error(
                    "Error: working_dir could not be resolved"
                    + _WORKSPACE_BOUNDARY_NOTE
                )
            if not is_path_within(requested, resolved_root):
                return ToolResult.error(
                    "Error: working_dir is outside the configured workspace"
                    + _WORKSPACE_BOUNDARY_NOTE
                )

        guard_error = self._guard_command(
            command,
            cwd,
            restrict_to_workspace=access.restrict_to_workspace,
            workspace_root=workspace_root,
        )
        if guard_error:
            return guard_error

        if self.sandbox:
            if _IS_WINDOWS:
                logger.warning(
                    "Sandbox '{}' is not supported on Windows; running unsandboxed",
                    self.sandbox,
                )
            else:
                workspace = workspace_root or cwd
                command = wrap_command(self.sandbox, command, workspace, cwd)
                cwd = str(Path(workspace).resolve())

        effective_timeout = (
            self._resolve_background_timeout(timeout) if background
            else self._resolve_timeout(timeout)
        )
        env = self._build_env()

        if self.path_prepend or self.path_append:
            if _IS_WINDOWS:
                env["PATH"] = self._compose_path(env.get("PATH", ""))
            else:
                command = self._wrap_path_export(command, env)

        shell_program, shell_error = self._resolve_shell(shell)
        if shell_error:
            return shell_error

        return _PreparedCommand(
            command=command,
            cwd=cwd,
            env=env,
            timeout=effective_timeout,
            shell_program=shell_program,
            login=False if login is None else login,
        )

    def _compose_path(self, current_path: str) -> str:
        parts = []
        if self.path_prepend:
            parts.append(self.path_prepend)
        if current_path:
            parts.append(current_path)
        if self.path_append:
            parts.append(self.path_append)
        return os.pathsep.join(parts)

    def _wrap_path_export(self, command: str, env: dict[str, str]) -> str:
        segments = []
        if self.path_prepend:
            env["NANOBOT_PATH_PREPEND"] = self.path_prepend
            segments.append("$NANOBOT_PATH_PREPEND")
        segments.append("$PATH")
        if self.path_append:
            env["NANOBOT_PATH_APPEND"] = self.path_append
            segments.append("$NANOBOT_PATH_APPEND")
        path_expr = os.pathsep.join(segments)
        return f'export PATH="{path_expr}"; {command}'

    @staticmethod
    async def _spawn(
        command: str, cwd: str, env: dict[str, str],
        shell_program: str | None = None,
        login: bool = False,
        *,
        stdin: int = asyncio.subprocess.DEVNULL,
        stdout: Any = asyncio.subprocess.PIPE,
        stderr: Any = asyncio.subprocess.PIPE,
    ) -> asyncio.subprocess.Process:
        """Launch *command* in a platform-appropriate shell."""
        if _IS_WINDOWS:
            # Default to PowerShell so single-line and multi-line commands
            # share the same shell semantics.  cmd.exe is reachable via the
            # explicit shell="cmd" parameter (see _resolve_shell).
            default_program = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
            program = shell_program or default_program
            program_name = PureWindowsPath(program).name.lower()
            if program_name in ("cmd", "cmd.exe"):
                cmd_env = {**env, "COMSPEC": program}
                return await asyncio.create_subprocess_shell(
                    command,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=cwd,
                    env=cmd_env,
                )
            command = ExecTool._normalize_powershell_command(command)
            command = f"{command}\nif ($LASTEXITCODE -ne $null) {{ exit $LASTEXITCODE }}"
            return await asyncio.create_subprocess_exec(
                program, "-NoProfile", "-NonInteractive", "-Command", command,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                cwd=cwd,
                env=env,
            )
        shell_program = shell_program or shutil.which("bash") or "/bin/bash"
        args = [shell_program]
        shell_name = Path(shell_program).name.lower()
        if login and shell_name in {"bash", "bash.exe", "zsh", "zsh.exe"}:
            args.append("-l")
        args.extend(["-c", command])
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            start_new_session=not _IS_WINDOWS,
            env=env,
        )

    @staticmethod
    def _normalize_powershell_command(command: str) -> str:
        stripped = command.lstrip()
        if not stripped or stripped[0] not in {"'", '"'}:
            return command

        quote = stripped[0]
        end = stripped.find(quote, 1)
        if end == -1 or end + 1 >= len(stripped) or not stripped[end + 1].isspace():
            return command

        executable = stripped[1:end]
        looks_like_windows_executable = (
            bool(re.match(r"^[A-Za-z]:[\\/]", executable))
            or executable.startswith(r"\\")
            or executable.lower().endswith((".exe", ".cmd", ".bat", ".ps1"))
        )
        if not looks_like_windows_executable:
            return command

        leading = command[: len(command) - len(stripped)]
        return f"{leading}& {stripped}"

    @staticmethod
    def _resolve_shell(shell: str | None) -> tuple[str | None, str | None]:
        if not shell:
            return None, None
        if "\0" in shell or "\n" in shell or "\r" in shell:
            return None, ToolResult.error("Error: shell contains invalid characters")
        if _IS_WINDOWS:
            win_allowed = {"powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe"}
            path = Path(shell).expanduser()
            if path.is_absolute():
                name = path.name.lower()
                if name not in win_allowed:
                    return None, ToolResult.error(
                        f"Error: unsupported shell {shell!r}. "
                        "Allowed: powershell, pwsh, cmd"
                    )
                if not path.is_file():
                    return None, ToolResult.error(f"Error: shell is not found: {shell}")
                return str(path), None
            if "/" in shell or "\\" in shell:
                return None, ToolResult.error("Error: shell must be a shell name or absolute path")
            if shell.lower() not in win_allowed:
                return None, ToolResult.error(
                    f"Error: unsupported shell {shell!r}. "
                    "Allowed: powershell, pwsh, cmd"
                )
            if shell.lower() in ("cmd", "cmd.exe"):
                resolved = os.environ.get("COMSPEC") or shutil.which("cmd") or "cmd"
                return resolved, None
            resolved = shutil.which(shell) or shell
            return resolved, None
        allowed = {"sh", "bash", "zsh"}
        path = Path(shell).expanduser()
        if path.is_absolute():
            if path.name not in allowed:
                return None, ToolResult.error(f"Error: unsupported shell {shell!r}. Allowed: bash, sh, zsh")
            if not path.is_file() or not os.access(path, os.X_OK):
                return None, ToolResult.error(f"Error: shell is not executable: {shell}")
            return str(path), None
        if "/" in shell or "\\" in shell:
            return None, ToolResult.error("Error: shell must be a shell name or absolute path")
        if shell not in allowed:
            return None, ToolResult.error(f"Error: unsupported shell {shell!r}. Allowed: bash, sh, zsh")
        resolved = shutil.which(shell)
        if not resolved:
            return None, ToolResult.error(f"Error: shell not found: {shell}")
        return resolved, None

    @staticmethod
    async def _drain_streams(
        stdout_reader: asyncio.StreamReader | None,
        stderr_reader: asyncio.StreamReader | None,
        cap: int,
    ) -> tuple[bytes, bytes, bool]:
        """Read both subprocess streams concurrently, up to `cap` bytes each.

        Reads chunk-by-chunk instead of draining one stream to EOF before the
        other (as separate per-stream loops run under asyncio.gather would): a
        stream that overflows fast (e.g. stdout) but whose sibling stays open and
        idle (e.g. an empty stderr, since the process is still running) must still
        be detected immediately -- not block until the sibling's own EOF, which
        only arrives once the (not-yet-killed) process exits.

        Returns (stdout, stderr, overflowed). Reaching EOF on both streams before
        the cap is hit signals both write ends closed -- normally because the
        process exited, the same signal process.communicate() itself waits on.
        """
        buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        totals: dict[str, int] = {"stdout": 0, "stderr": 0}
        readers: dict[str, asyncio.StreamReader] = {
            name: reader
            for name, reader in (("stdout", stdout_reader), ("stderr", stderr_reader))
            if reader is not None
        }
        pending: dict[str, asyncio.Task] = {
            name: asyncio.ensure_future(reader.read(65536)) for name, reader in readers.items()
        }
        overflowed = False
        try:
            while pending and not overflowed:
                done, _ = await asyncio.wait(pending.values(), return_when=asyncio.FIRST_COMPLETED)
                for name in list(pending):
                    task = pending[name]
                    if task not in done:
                        continue
                    del pending[name]
                    chunk = task.result()
                    if not chunk:
                        continue  # This stream hit EOF; drop it from pending, keep draining the other.
                    totals[name] += len(chunk)
                    if totals[name] > cap:
                        buffers[name] += chunk[: len(chunk) - (totals[name] - cap)]
                        overflowed = True
                        break
                    buffers[name] += chunk
                    pending[name] = asyncio.ensure_future(readers[name].read(65536))
        finally:
            for task in pending.values():
                task.cancel()
        return bytes(buffers["stdout"]), bytes(buffers["stderr"]), overflowed

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> dict:
        """Kill a subprocess and reap it to prevent zombies.

        Unix commands run in their own session, so a timeout also signals the
        child process group. Killing only the shell would leave pytest workers
        and other grandchildren alive.
        """
        if not _IS_WINDOWS and process.pid:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        process.kill()
        try:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5.0)
        finally:
            if not _IS_WINDOWS:
                try:
                    os.waitpid(process.pid, os.WNOHANG)
                except (ProcessLookupError, ChildProcessError) as e:
                    logger.debug("Process already reaped or not found: {}", e)
        # t55: confirm only what this tool started - its own process and its process group (the pid is
        # the group id: start_new_session). Bounded check; no search by name.
        confirmed = process.returncode is not None
        if confirmed and not _IS_WINDOWS and process.pid:
            confirmed = False
            for _ in range(20):
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    confirmed = True
                    break
                except PermissionError:
                    break
                await asyncio.sleep(0.05)
        return {"pid": process.pid, "pgid": None if _IS_WINDOWS else process.pid, "confirmed": confirmed}

    def _build_env(self) -> dict[str, str]:
        """Build a minimal environment for subprocess execution.

        On Unix, only HOME/LANG/TERM are passed by default. If callers request
        ``login=True``, bash/zsh may source the user's profile and add PATH or
        other variables.

        On Windows, ``cmd.exe`` has no login-profile mechanism, so a curated
        set of system variables (including PATH) is forwarded.  API keys and
        other secrets are still excluded.
        """
        if _IS_WINDOWS:
            sr = os.environ.get("SYSTEMROOT", r"C:\Windows")
            env = {
                "SYSTEMROOT": sr,
                "COMSPEC": os.environ.get("COMSPEC", f"{sr}\\system32\\cmd.exe"),
                "USERPROFILE": os.environ.get("USERPROFILE", ""),
                "HOMEDRIVE": os.environ.get("HOMEDRIVE", "C:"),
                "HOMEPATH": os.environ.get("HOMEPATH", "\\"),
                "TEMP": os.environ.get("TEMP", f"{sr}\\Temp"),
                "TMP": os.environ.get("TMP", f"{sr}\\Temp"),
                "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
                "PATH": os.environ.get("PATH", f"{sr}\\system32;{sr}"),
                "PYTHONUNBUFFERED": "1",
                "APPDATA": os.environ.get("APPDATA", ""),
                "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
                "ProgramData": os.environ.get("ProgramData", ""),
                "ProgramFiles": os.environ.get("ProgramFiles", ""),
                "ProgramFiles(x86)": os.environ.get("ProgramFiles(x86)", ""),
                "ProgramW6432": os.environ.get("ProgramW6432", ""),
            }
            for key in self.allowed_env_keys:
                val = os.environ.get(key)
                if val is not None:
                    env[key] = val
            return env
        home = os.environ.get("HOME", "/tmp")
        env = {
            "HOME": home,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": os.environ.get("TERM", "dumb"),
            "PYTHONUNBUFFERED": "1",
        }
        for key in self.allowed_env_keys:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        return env

    def _guard_command(
        self,
        command: str,
        cwd: str,
        *,
        restrict_to_workspace: bool | None = None,
        workspace_root: str | None = None,
    ) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()
        lifecycle_error = self._gateway_lifecycle_guard(cmd)
        if lifecycle_error:
            return lifecycle_error

        # allow_patterns take priority over deny_patterns so that users can
        # exempt specific commands (e.g. "rm -rf" inside a build directory)
        # from the hardcoded deny list via configuration.
        explicitly_allowed = bool(self.allow_patterns) and any(
            re.fullmatch(p, lower) for p in self.allow_patterns
        )
        if not explicitly_allowed:
            for pattern in self.deny_patterns:
                if re.search(pattern, lower):
                    return ToolResult.error("Error: Command blocked by deny pattern filter")

            if self.allow_patterns:
                return ToolResult.error("Error: Command blocked by allowlist filter (not in allowlist)")

        from nanobot.security.network import command_url_error
        network_error = command_url_error(
            cmd,
            allow_loopback=current_scope_allows_loopback(
                enabled=self.webui_allow_local_service_access,
            ),
        )
        if network_error:
            # The runner turns this marker into a non-retryable security hint.
            return ToolResult.error(network_error)

        should_restrict = self.restrict_to_workspace if restrict_to_workspace is None else restrict_to_workspace
        if should_restrict:
            if "..\\" in cmd or "../" in cmd:
                return ToolResult.error(
                    "Error: Command blocked by safety guard (path traversal detected)"
                    + _WORKSPACE_BOUNDARY_NOTE
                )

            cwd_path = Path(cwd).resolve()
            resolved_workspace = (
                Path(workspace_root).expanduser().resolve()
                if workspace_root
                else None
            )

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    # Match against the un-resolved path first.  On Linux,
                    # /dev/stderr is a symlink to /proc/self/fd/2 and
                    # ``Path.resolve()`` would mask the device-file intent.
                    if self._is_benign_device_path(expanded):
                        continue
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue

                if self._is_benign_device_path(str(p)):
                    continue

                media_path = get_media_dir().resolve()
                allowed = (
                    is_path_within(p, cwd_path)
                    or is_path_within(p, media_path)
                )
                if not allowed and resolved_workspace is not None:
                    allowed = is_path_within(p, resolved_workspace)
                if p.is_absolute() and not allowed:
                    return ToolResult.error(
                        "Error: Command blocked by safety guard (path outside working dir)"
                        + _WORKSPACE_BOUNDARY_NOTE
                    )

        return None

    @staticmethod
    def _gateway_lifecycle_guard(command: str) -> str | None:
        """Block commands that would kill the gateway currently running this tool.

        Restart scripts can schedule a detached restart safely. Directly killing
        the gateway from inside its own exec tool cancels the tool call before
        the following start command can reliably run.
        """
        current_pid = str(os.getpid())
        if re.search(
            rf"(?:^|[;&|]\s*)(?:\S*/)?kill(?:\s+-[A-Za-z0-9]+|\s+-\d+)*"
            rf"(?:\s+[^;&|]*)?\s+{re.escape(current_pid)}(?:\s|$|[;&|])",
            command,
        ):
            return ToolResult.error(
                "Error: Command blocked by safety guard (cannot kill the current nanobot gateway from inside exec; use `nanobot restart` for detached restart)"
            )
        if re.search(
            r"(?:^|[;&|]\s*)pkill\b[^;&|]*\s+-f\b[^;&|]*(?:nanobot|gateway)",
            command,
            re.I,
        ):
            return ToolResult.error(
                "Error: Command blocked by safety guard (cannot pkill nanobot/gateway processes from inside exec; use `nanobot restart`)"
            )
        if re.search(
            r"(?:^|[;&|]\s*)killall\b[^;&|]*(?:nanobot|python)",
            command,
            re.I,
        ):
            return ToolResult.error(
                "Error: Command blocked by safety guard (cannot killall nanobot runtime processes from inside exec; use `nanobot restart`)"
            )
        return None

    @classmethod
    def _is_benign_device_path(cls, path: str) -> bool:
        """Return True for kernel device files that should never be workspace-blocked."""
        if path in cls._BENIGN_DEVICE_PATHS:
            return True
        return path.startswith("/dev/fd/")

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        # Windows: match drive-root paths like `C:\` as well as `C:\path\to\file`, and UNC paths like `\\server\share`
        # Require path content after the colon: Python's `lambda d: ...` is
        # not a drive path. Bare drive operands are checked as shell tokens below.
        win_paths = re.findall(
            r"(?<![A-Za-z%])(?:[A-Za-z]:[^\s\"'|><;]+|\\\\[^\s\"'|><;]+(?:\\[^\s\"'|><;]+)*)",
            command
        )
        try:
            args = shlex.shlex(command, posix=not _IS_WINDOWS, punctuation_chars=";&|")
            args.whitespace_split = True
            win_paths.extend(arg.strip("\"'") for arg in args
                             if re.fullmatch(r"[\"']?[A-Za-z]:[\"']?", arg))
        except ValueError:
            # Malformed shell quoting must not hide a possible bare drive.
            win_paths.extend(re.findall(r"(?<![A-Za-z%])[A-Za-z]:(?=\s|$)", command))
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command) # POSIX: /absolute only
        home_paths = re.findall(r"(?:^|[\s>'\"])(~[^\s\"'>;|<]*)", command) # POSIX/Windows home shortcut: ~
        return win_paths + posix_paths + home_paths


def termination_note(info: dict | None) -> str:
    """t55: text for a stopped local process whose end could not be confirmed ('' when confirmed)."""
    if not info or info.get("confirmed"):
        return ""
    return ("종료 확인 불가·결과 불명: pid=" + str(info.get("pid")) + " pgid=" + str(info.get("pgid"))
            + " (" + str(info.get("command") or "")[:80] + "); 관련 대상을 다시 변경하기 전에 상태를 확인해야 합니다.")


def timeout_error_text(timeout: int | None) -> str:
    """Foreground timeout result: the work may have finished; verify before any retry."""
    return (
        f"Error: Command timed out after {timeout} seconds; termination was requested.\n"
        "Outcome unknown: the command may have partially or fully completed before it "
        "was stopped (e.g. an install or update may already be applied). Do not rerun "
        "it automatically. First verify the actual state (installed version, running "
        "processes, files, exit status/logs) with a short read-only check, then decide. "
        "For commands that need more time, start them with background=true and check "
        "them with exec_job."
    )
