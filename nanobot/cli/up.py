"""Python implementation of the nanobot-easy launcher (``nanobot up`` / ``down`` / ``restart``).

This ports the logic that used to live in install-nanobot-easy.sh,
start-nanobot-easy.sh, stop-nanobot-easy.sh and restart-nanobot-easy.sh (and
their .ps1 duplicates) so it exists in one place, is unit-testable, and no
longer drifts between platforms. The .sh/.ps1/.bat scripts now only bootstrap
a venv and then call into this module via ``nanobot up``/``down``/``restart``.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.error import URLError
from urllib.request import urlopen


class UpError(Exception):
    """Raised for launcher failures that should be reported and exit non-zero."""


def repo_root() -> Path:
    """Best-effort repository root: two levels up from the installed ``nanobot`` package."""
    import nanobot

    return Path(nanobot.__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# WebUI build freshness (ported from install-nanobot-easy.sh's
# webui_dist_is_fresh() / ensure_webui_dist())
# ---------------------------------------------------------------------------


def webui_dist_is_fresh(webui_dir: Path, index_html: Path) -> bool:
    """True only if nothing under webui/src (or package.json) is newer than index_html.

    Otherwise a `git pull` that updates the WebUI source would silently keep
    serving a stale build, since a prior install already leaves index.html
    in place.
    """
    if not index_html.exists():
        return False
    index_mtime = index_html.stat().st_mtime
    package_json = webui_dir / "package.json"
    if package_json.exists() and package_json.stat().st_mtime > index_mtime:
        return False
    src_dir = webui_dir / "src"
    if src_dir.is_dir():
        for path in src_dir.rglob("*"):
            if path.is_file():
                try:
                    if path.stat().st_mtime > index_mtime:
                        return False
                except OSError:
                    continue
    return True


def pick_webui_runner() -> str | None:
    for candidate in ("bun", "npm"):
        if shutil.which(candidate):
            return candidate
    return None


def ensure_webui_dist(
    webui_dir: Path,
    dist_index: Path,
    *,
    force: bool = False,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    print_fn: Callable[[str], None] = print,
) -> None:
    if dist_index.exists() and not force:
        if webui_dist_is_fresh(webui_dir, dist_index):
            print_fn(f"Using existing WebUI build: {dist_index.parent}")
            return
        print_fn("WebUI source has changed since the last build; rebuilding...")

    if not (webui_dir / "package.json").exists():
        if not webui_dir.is_dir():
            raise UpError(
                f"No prebuilt WebUI bundle found at {dist_index}, and there is no "
                f"webui/ source checkout at {webui_dir} to build one from.\n"
                "This installed nanobot package is missing its bundled WebUI assets -- "
                "reinstall nanobot-easy from a release wheel/sdist that includes "
                "nanobot/web/dist, or run `nanobot up` from a full source checkout instead."
            )
        raise UpError(f"{webui_dir}/package.json was not found; cannot build WebUI bundle")

    runner = pick_webui_runner()
    if runner is None:
        raise UpError(
            "WebUI build requires Bun or Node.js/npm because editable Python installs do "
            "not run the packaged WebUI build hook.\n"
            "Install one of these, then rerun:\n"
            "  macOS: brew install node\n"
            "  Ubuntu/Debian: sudo apt install -y nodejs npm\n"
            "  Fedora: sudo dnf install -y nodejs npm\n"
            "  Arch: sudo pacman -S --needed nodejs npm\n"
            "  Bun option: https://bun.sh/docs/installation"
        )

    print_fn(f"Building WebUI bundle with {runner}...")
    if runner == "bun":
        _run_checked(run, ["bun", "install"], cwd=webui_dir)
        _run_checked(run, ["bun", "run", "build"], cwd=webui_dir)
    elif (webui_dir / "package-lock.json").exists():
        _run_checked(run, ["npm", "ci"], cwd=webui_dir)
        _run_checked(run, ["npm", "run", "build"], cwd=webui_dir)
    else:
        _run_checked(run, ["npm", "install"], cwd=webui_dir)
        _run_checked(run, ["npm", "run", "build"], cwd=webui_dir)

    if not dist_index.exists():
        raise UpError(f"WebUI build finished but {dist_index} is missing")
    print_fn(f"WebUI build ready: {dist_index.parent}")


def _run_checked(run: Callable[..., subprocess.CompletedProcess], cmd: list[str], *, cwd: Path) -> None:
    result = run(cmd, cwd=str(cwd), shell=(sys.platform == "win32"))
    code = getattr(result, "returncode", 0)
    if code != 0:
        raise UpError(f"{' '.join(cmd)} failed (exit {code})")


# ---------------------------------------------------------------------------
# Config bootstrap (ported from install-nanobot-easy.sh's run_onboard_if_needed())
# ---------------------------------------------------------------------------


def ensure_config(config_path: Path, workspace_path: Path, *, print_fn: Callable[[str], None] = print) -> None:
    """Create a default config with no prompts if one doesn't exist yet.

    First-run setup is handled by the browser onboarding wizard, not a
    terminal wizard -- this calls the same code path as
    `nanobot onboard --config X --workspace Y` (no --wizard).
    """
    if config_path.exists():
        return
    print_fn(f"config not found: {config_path}")
    print_fn("Creating a default config -- finish setup in the browser (WebUI) once nanobot-easy starts.")
    from nanobot.cli.commands import onboard

    onboard(workspace=str(workspace_path), config=str(config_path), wizard=False)


# ---------------------------------------------------------------------------
# Port occupancy (ported from start-nanobot-easy.sh's port_in_use()/pid_on_port()
# Python heredocs -- these were already Python, just embedded as strings)
# ---------------------------------------------------------------------------


def port_in_use(host: str, port: int, *, timeout: float = 0.5) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except OSError:
        return False
    else:
        return True
    finally:
        sock.close()


def pid_on_port(port: int) -> int | None:
    """Best-effort: find the PID of whatever is listening on a port."""
    if sys.platform == "win32":
        return _pid_on_port_windows(port)
    return _pid_on_port_posix(port)


def _pid_on_port_posix(port: int) -> int | None:
    lsof = shutil.which("lsof")
    if lsof:
        try:
            out = subprocess.run(
                [lsof, "-nP", "-t", "-i", f"tcp:{port}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            first_line = out.stdout.strip().splitlines()
            if first_line:
                return int(first_line[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    fuser = shutil.which("fuser")
    if fuser:
        try:
            out = subprocess.run([fuser, f"{port}/tcp"], capture_output=True, text=True, timeout=5)
            token = out.stdout.strip().split()
            if token:
                return int(token[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    return _pid_on_port_proc_net_tcp(port)


def _pid_on_port_proc_net_tcp(port: int) -> int | None:
    """Fallback for environments without lsof/fuser installed (Linux only)."""
    target = f"{port:04X}"
    try:
        with open("/proc/net/tcp") as f:
            next(f)
            inode = None
            for line in f:
                local = line.split()[1]
                if local.split(":")[1] == target:
                    inode = line.split()[9]
                    break
        if inode is None:
            return None
    except OSError:
        return None

    for pid_dir in os.listdir("/proc"):
        if not pid_dir.isdigit():
            continue
        fd_dir = f"/proc/{pid_dir}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    link = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if link == f"socket:[{inode}]":
                    return int(pid_dir)
        except OSError:
            continue
    return None


def _pid_on_port_windows(port: int) -> int | None:
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "TCP":
            continue
        local_addr, state = parts[1], parts[3]
        if state != "LISTENING":
            continue
        if local_addr.rsplit(":", 1)[-1] == str(port):
            try:
                return int(parts[-1])
            except ValueError:
                continue
    return None


def kill_pid(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    time.sleep(1)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    with suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


def stop_hint(pid: int) -> str:
    return f"taskkill /PID {pid} /F" if sys.platform == "win32" else f"kill {pid}"


def process_name(pid: int) -> str | None:
    """Best-effort executable name for a pid, so a forced kill can show what it's about to hit."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            first_line = out.stdout.strip().splitlines()
            if first_line:
                fields = first_line[0].split('","')
                if fields:
                    return fields[0].strip('"') or None
            return None
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        name = out.stdout.strip()
        return name or None
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------------------
# Config JSON reading for host/port (ported from start-nanobot-easy.sh's
# Python heredoc that reads gateway.host/port and channels.websocket.host/port)
# ---------------------------------------------------------------------------


def read_gateway_ports(config_path: Path) -> tuple[str, int, str, int]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    gateway = data.get("gateway") or {}
    websocket = (data.get("channels") or {}).get("websocket") or {}
    return (
        gateway.get("host") or "127.0.0.1",
        int(gateway.get("port") or 18790),
        websocket.get("host") or "127.0.0.1",
        int(websocket.get("port") or 8765),
    )


# ---------------------------------------------------------------------------
# Staleness detection (ported from start-nanobot-easy.sh's PID-file block)
# ---------------------------------------------------------------------------


def parse_started_at(value: str) -> datetime:
    return datetime.fromisoformat(value)


def is_gateway_stale(started_at: datetime, *, nanobot_dir: Path, webui_src_dir: Path) -> bool:
    """True if any .py file under nanobot/ or any file under webui/src changed
    since the tracked gateway process started -- it's still running the old
    code it loaded at startup, and needs an actual restart to pick up changes.
    """
    reference = started_at.timestamp()
    if _has_newer_file(nanobot_dir, reference, pattern="*.py"):
        return True
    return _has_newer_file(webui_src_dir, reference, pattern="*")


def _has_newer_file(directory: Path, reference: float, *, pattern: str) -> bool:
    if not directory.is_dir():
        return False
    for path in directory.rglob(pattern):
        if path.is_file():
            try:
                if path.stat().st_mtime > reference:
                    return True
            except OSError:
                continue
    return False


# ---------------------------------------------------------------------------
# Browser / health check
# ---------------------------------------------------------------------------


def open_browser_if_available(url: str) -> None:
    if os.environ.get("NANOBOT_OPEN_BROWSER", "1") == "0":
        return
    try:
        webbrowser.open(url)
    except Exception:
        pass


def wait_for_health(
    url: str,
    *,
    timeout_s: float = 10.0,
    interval_s: float = 0.5,
    is_alive: Callable[[], bool] = lambda: True,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    deadline = now() + timeout_s
    while now() < deadline:
        if not is_alive():
            return False
        try:
            with urlopen(url, timeout=1) as resp:  # noqa: S310 - local health check only
                if 200 <= resp.status < 300:
                    return True
        except URLError:
            pass
        except OSError:
            pass
        sleep(interval_s)
    return False


# ---------------------------------------------------------------------------
# Ancestor-process guard (ported from stop/restart-nanobot-easy.sh's
# is_ancestor_pid()) -- refuses to let a gateway agent tool stop/restart its
# own process tree inline, since that would kill the caller mid-execution.
# ---------------------------------------------------------------------------


def is_ancestor_pid(candidate_pid: int, *, start_pid: int | None = None) -> bool:
    pid = start_pid if start_pid is not None else os.getpid()
    seen: set[int] = set()
    ppid_of = _ppid_of_windows if sys.platform == "win32" else _ppid_of_posix
    while pid and pid not in (0, 1) and pid not in seen:
        seen.add(pid)
        if pid == candidate_pid:
            return True
        pid = ppid_of(pid)
    return False


def _ppid_of_posix(pid: int) -> int | None:
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        text = out.stdout.strip()
        return int(text) if text else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _ppid_of_windows(pid: int) -> int | None:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    th32cs_snapprocess = 0x00000002
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    if snapshot == -1:
        return None
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return None
        while True:
            if entry.th32ProcessID == pid:
                return entry.th32ParentProcessID
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                return None
    finally:
        kernel32.CloseHandle(snapshot)


# ---------------------------------------------------------------------------
# nanobot up / down / restart
# ---------------------------------------------------------------------------


def _runtime_for(cfg_path: Path, ws_path: Path):
    from nanobot.gateway import GatewayRuntime, GatewayRuntimePaths

    return GatewayRuntime(
        paths=GatewayRuntimePaths.for_instance(
            data_dir=cfg_path.parent,
            workspace=str(ws_path),
            config_path=str(cfg_path),
        )
    )


def run_up(*, config_path: str, workspace_path: str, print_fn: Callable[[str], None] = print) -> int:
    from nanobot.gateway import GatewayStartOptions

    cfg_path = Path(config_path).expanduser().resolve(strict=False)
    ws_path = Path(workspace_path).expanduser().resolve(strict=False)
    root = repo_root()
    webui_dir = root / "webui"
    dist_index = root / "nanobot" / "web" / "dist" / "index.html"

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    ws_path.mkdir(parents=True, exist_ok=True)

    try:
        ensure_config(cfg_path, ws_path, print_fn=print_fn)
    except Exception as exc:  # onboard() failures, disk errors, etc.
        print_fn(f"Error: {exc}")
        return 1
    if not cfg_path.exists():
        print_fn(f"config still not found after setup: {cfg_path}")
        return 1

    try:
        ensure_webui_dist(webui_dir, dist_index, print_fn=print_fn)
    except UpError as exc:
        print_fn(f"Error: {exc}")
        return 1
    if not dist_index.exists():
        print_fn(f"WebUI bundle still not found after install: {dist_index}")
        return 1

    gateway_host, gateway_port, webui_host, webui_port = read_gateway_ports(cfg_path)
    webui_url = f"http://{webui_host}:{webui_port}/"

    runtime = _runtime_for(cfg_path, ws_path)
    status = runtime.status()

    if status.running:
        stale = False
        if status.started_at:
            try:
                started = parse_started_at(status.started_at)
                stale = is_gateway_stale(started, nanobot_dir=root / "nanobot", webui_src_dir=webui_dir / "src")
            except ValueError:
                stale = False
        if stale:
            print_fn(f"nanobot-easy gateway is running (pid={status.pid}) but the code has changed since it started.")
            print_fn("restarting so the update actually takes effect...")
            stop_result = runtime.stop()
            if not stop_result.ok:
                print_fn(f"could not stop the running gateway (pid={status.pid}): {stop_result.message}")
                print_fn(f"stop it manually, e.g.: {stop_hint(status.pid)}")
                return 1
        else:
            print_fn(f"nanobot-easy gateway is already running: pid={status.pid}")
            print_fn(f"log: {status.log_path}")
            print_fn(f"webui: {webui_url}")
            open_browser_if_available(webui_url)
            return 0

    # The tracked-process check above says nothing is running, but something
    # may still be bound to our ports (e.g. a previous run that was
    # force-killed, or an unrelated process). Starting anyway would just
    # crash with a confusing "address already in use" error.
    if port_in_use(gateway_host, gateway_port) or port_in_use(webui_host, webui_port):
        print_fn("nanobot-easy gateway ports look busy, but no tracked process is running:")
        print_fn(f"  {gateway_host}:{gateway_port} (health) / {webui_host}:{webui_port} (websocket)")
        blocking_pid = pid_on_port(gateway_port) or pid_on_port(webui_port)
        if blocking_pid:
            print_fn(f"  likely culprit: pid={blocking_pid}")
            print_fn(f"  stop it with: {stop_hint(blocking_pid)}")
        else:
            print_fn("  could not identify the process automatically.")
        print_fn(f"  or set a different port in {cfg_path} (gateway.port / channels.websocket.port).")
        return 1

    result = runtime.start_background(
        GatewayStartOptions(port=gateway_port, workspace=str(ws_path), config_path=str(cfg_path))
    )
    if not result.ok:
        print_fn(f"nanobot-easy gateway failed to start: {result.message}")
        for line in runtime.read_log_tail(tail=80):
            print_fn(line)
        return 1

    health_url = f"http://{gateway_host}:{gateway_port}/health"
    ready = wait_for_health(health_url, is_alive=lambda: runtime.status().running)

    if ready:
        print_fn(f"nanobot-easy gateway started: pid={result.status.pid}")
        print_fn(f"config: {cfg_path}")
        print_fn(f"workspace: {ws_path}")
        print_fn(f"health: {health_url}")
        print_fn(f"webui: {webui_url}")
        print_fn(f"log: {result.status.log_path}")
        open_browser_if_available(webui_url)
        return 0

    if not runtime.status().running:
        print_fn("nanobot-easy gateway exited during startup. Recent log:")
        for line in runtime.read_log_tail(tail=80):
            print_fn(line)
        return 1

    print_fn(f"nanobot-easy gateway started but health check did not become ready yet: pid={result.status.pid}")
    print_fn(f"config: {cfg_path}")
    print_fn(f"workspace: {ws_path}")
    print_fn(f"webui: {webui_url}")
    print_fn(f"log: {result.status.log_path}")
    return 0


def run_down(*, config_path: str, workspace_path: str, print_fn: Callable[[str], None] = print) -> int:
    cfg_path = Path(config_path).expanduser().resolve(strict=False)
    ws_path = Path(workspace_path).expanduser().resolve(strict=False)

    runtime = _runtime_for(cfg_path, ws_path)
    status = runtime.status()

    if not status.running:
        print_fn("nanobot-easy gateway is not running: no tracked process")
        if _stop_by_port_fallback(cfg_path, print_fn=print_fn):
            return 0
        print_fn("nothing found on the configured ports either.")
        return 0

    if os.environ.get("NANOBOT_ALLOW_SELF_STOP", "0") != "1" and is_ancestor_pid(status.pid):
        print_fn("refusing to stop nanobot-easy gateway from inside its own process tree")
        print_fn("use `nanobot restart`, which schedules a detached restart safely")
        return 2

    print_fn(f"stopping nanobot-easy gateway: pid={status.pid}")
    result = runtime.stop()
    if result.ok:
        print_fn("nanobot-easy gateway stopped")
        print_fn(f"log: {status.log_path}")
        return 0
    print_fn(f"gateway did not stop cleanly: {result.message}")
    return 1


def _stop_by_port_fallback(cfg_path: Path, *, print_fn: Callable[[str], None] = print) -> bool:
    if not cfg_path.exists():
        return False
    try:
        gw_host, gw_port, ws_host, ws_port = read_gateway_ports(cfg_path)
    except (OSError, ValueError):
        return False
    found = False
    for host, port in ((gw_host, gw_port), (ws_host, ws_port)):
        if port_in_use(host, port):
            found = True
            blocking_pid = pid_on_port(port)
            if blocking_pid:
                name = process_name(blocking_pid) or "unknown"
                print_fn(f"found an untracked process on {host}:{port}: pid={blocking_pid} ({name})")
                print_fn(f"stopping it: pid={blocking_pid}")
                kill_pid(blocking_pid)
            else:
                print_fn(f"something is listening on {host}:{port} but its pid could not be determined.")
                print_fn(f"install lsof or fuser, or find it manually with: ss -ltnp | grep -E ':{port}\\b'")
    return found


def run_restart(*, config_path: str, workspace_path: str, print_fn: Callable[[str], None] = print) -> int:
    cfg_path = Path(config_path).expanduser().resolve(strict=False)
    ws_path = Path(workspace_path).expanduser().resolve(strict=False)

    runtime = _runtime_for(cfg_path, ws_path)
    status = runtime.status()

    if os.environ.get("NANOBOT_DETACHED_RESTART", "0") != "1" and status.running and is_ancestor_pid(status.pid):
        delay = float(os.environ.get("NANOBOT_DETACHED_RESTART_DELAY", "5"))
        print_fn("restart requested from inside nanobot gateway; scheduling detached restart")
        _schedule_detached_restart(str(cfg_path), str(ws_path), delay=delay, log_path=status.log_path)
        print_fn(f"detached restart scheduled; log: {status.log_path}")
        return 0

    down_code = run_down(config_path=str(cfg_path), workspace_path=str(ws_path), print_fn=print_fn)
    if down_code not in (0,):
        return down_code
    return run_up(config_path=str(cfg_path), workspace_path=str(ws_path), print_fn=print_fn)


def _schedule_detached_restart(config_path: str, workspace_path: str, *, delay: float, log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "import os, subprocess, sys, time\n"
        f"time.sleep({delay!r})\n"
        "env = os.environ.copy()\n"
        "env['NANOBOT_DETACHED_RESTART'] = '1'\n"
        "env['NANOBOT_ALLOW_SELF_STOP'] = '1'\n"
        "subprocess.run(\n"
        "    [sys.executable, '-m', 'nanobot', 'restart', '--config', "
        f"{config_path!r}, '--workspace', {workspace_path!r}],\n"
        "    env=env,\n"
        ")\n"
    )
    kwargs: dict = {}
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    with log_path.open("a", encoding="utf-8") as log_handle:
        subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
