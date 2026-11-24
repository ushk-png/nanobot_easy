import json
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nanobot.cli import up as up_mod
from nanobot.gateway import GatewayStartOptions, GatewayStatus, RuntimeResult

# ---------------------------------------------------------------------------
# webui_dist_is_fresh
# ---------------------------------------------------------------------------


def test_webui_dist_is_fresh_missing_index_is_not_fresh(tmp_path: Path):
    webui_dir = tmp_path / "webui"
    webui_dir.mkdir()
    index_html = webui_dir.parent / "dist" / "index.html"
    assert up_mod.webui_dist_is_fresh(webui_dir, index_html) is False


def test_webui_dist_is_fresh_true_when_nothing_newer(tmp_path: Path):
    webui_dir = tmp_path / "webui"
    src_dir = webui_dir / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "App.tsx").write_text("x")
    index_html = tmp_path / "dist" / "index.html"
    index_html.parent.mkdir()
    index_html.write_text("<html></html>")
    # index.html built after the source file
    later = time.time() + 5
    os.utime(index_html, (later, later))
    assert up_mod.webui_dist_is_fresh(webui_dir, index_html) is True


def test_webui_dist_is_fresh_false_when_source_changed_after_build(tmp_path: Path):
    webui_dir = tmp_path / "webui"
    src_dir = webui_dir / "src"
    src_dir.mkdir(parents=True)
    src_file = src_dir / "App.tsx"
    src_file.write_text("x")
    index_html = tmp_path / "dist" / "index.html"
    index_html.parent.mkdir()
    index_html.write_text("<html></html>")
    # source file touched after the build -- a `git pull` landed new WebUI code
    later = time.time() + 5
    os.utime(src_file, (later, later))
    assert up_mod.webui_dist_is_fresh(webui_dir, index_html) is False


def test_webui_dist_is_fresh_false_when_package_json_changed(tmp_path: Path):
    webui_dir = tmp_path / "webui"
    webui_dir.mkdir()
    package_json = webui_dir / "package.json"
    package_json.write_text("{}")
    index_html = tmp_path / "dist" / "index.html"
    index_html.parent.mkdir()
    index_html.write_text("<html></html>")
    later = time.time() + 5
    os.utime(package_json, (later, later))
    assert up_mod.webui_dist_is_fresh(webui_dir, index_html) is False


# ---------------------------------------------------------------------------
# port_in_use / pid_on_port
# ---------------------------------------------------------------------------


def test_port_in_use_true_for_bound_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert up_mod.port_in_use("127.0.0.1", port) is True
    finally:
        sock.close()


def test_port_in_use_false_for_free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # release it -- nothing should be listening now
    assert up_mod.port_in_use("127.0.0.1", port) is False


@pytest.mark.skipif(os.name == "nt", reason="posix-only pid lookup path")
def test_pid_on_port_finds_own_pid():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        found = up_mod.pid_on_port(port)
        assert found == os.getpid()
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# read_gateway_ports
# ---------------------------------------------------------------------------


def test_read_gateway_ports_defaults_when_missing(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    assert up_mod.read_gateway_ports(config_path) == ("127.0.0.1", 18790, "127.0.0.1", 8765)


def test_read_gateway_ports_reads_configured_values(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "gateway": {"host": "0.0.0.0", "port": 19000},
                "channels": {"websocket": {"host": "0.0.0.0", "port": 9000}},
            }
        )
    )
    assert up_mod.read_gateway_ports(config_path) == ("0.0.0.0", 19000, "0.0.0.0", 9000)


# ---------------------------------------------------------------------------
# is_gateway_stale
# ---------------------------------------------------------------------------


def test_is_gateway_stale_true_when_source_newer_than_start(tmp_path: Path):
    nanobot_dir = tmp_path / "nanobot"
    nanobot_dir.mkdir()
    webui_src_dir = tmp_path / "webui" / "src"
    webui_src_dir.mkdir(parents=True)

    started_at = datetime.now(UTC) - timedelta(seconds=10)
    py_file = nanobot_dir / "loop.py"
    py_file.write_text("x")  # written "now", after started_at

    assert up_mod.is_gateway_stale(started_at, nanobot_dir=nanobot_dir, webui_src_dir=webui_src_dir) is True


def test_is_gateway_stale_false_when_nothing_changed_since_start(tmp_path: Path):
    nanobot_dir = tmp_path / "nanobot"
    nanobot_dir.mkdir()
    webui_src_dir = tmp_path / "webui" / "src"
    webui_src_dir.mkdir(parents=True)
    (nanobot_dir / "loop.py").write_text("x")

    started_at = datetime.now(UTC) + timedelta(seconds=10)  # process started after the file was written

    assert up_mod.is_gateway_stale(started_at, nanobot_dir=nanobot_dir, webui_src_dir=webui_src_dir) is False


def test_is_gateway_stale_ignores_non_py_files_under_nanobot_dir(tmp_path: Path):
    nanobot_dir = tmp_path / "nanobot"
    nanobot_dir.mkdir()
    webui_src_dir = tmp_path / "webui" / "src"
    webui_src_dir.mkdir(parents=True)
    (nanobot_dir / "notes.txt").write_text("x")  # not *.py -- shouldn't trigger staleness

    started_at = datetime.now(UTC) - timedelta(seconds=10)

    assert up_mod.is_gateway_stale(started_at, nanobot_dir=nanobot_dir, webui_src_dir=webui_src_dir) is False


# ---------------------------------------------------------------------------
# ensure_config
# ---------------------------------------------------------------------------


def test_ensure_config_creates_default_when_missing(tmp_path: Path, monkeypatch):
    config_path = tmp_path / ".local" / "config.json"
    workspace_path = tmp_path / ".local" / "workspace"
    # onboard() calls set_config_path(), which otherwise leaks global state
    # (nanobot.config.loader._current_config_path) into later tests.
    monkeypatch.setattr("nanobot.config.loader.set_config_path", lambda _path: None)

    messages: list[str] = []
    up_mod.ensure_config(config_path, workspace_path, print_fn=messages.append)

    assert config_path.exists()
    data = json.loads(config_path.read_text())
    assert "agents" in data
    assert any("config not found" in m for m in messages)


def test_ensure_config_is_noop_when_config_exists(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    workspace_path = tmp_path / "workspace"

    calls: list[str] = []
    up_mod.ensure_config(config_path, workspace_path, print_fn=calls.append)

    assert calls == []  # no "config not found" message, no onboard() invocation
    assert config_path.read_text() == "{}"  # left untouched


# ---------------------------------------------------------------------------
# is_ancestor_pid
# ---------------------------------------------------------------------------


def test_is_ancestor_pid_true_for_self():
    pid = os.getpid()
    assert up_mod.is_ancestor_pid(pid, start_pid=pid) is True


def test_is_ancestor_pid_false_for_unrelated_pid():
    # PID 1 (init) is never an ancestor of this test process in a normal
    # sandboxed/container test run where pytest isn't PID 1 itself.
    if os.getpid() == 1:
        pytest.skip("test process is PID 1")
    assert up_mod.is_ancestor_pid(999999, start_pid=os.getpid()) is False


# ---------------------------------------------------------------------------
# run_up / run_down orchestration (GatewayRuntime faked out)
# ---------------------------------------------------------------------------


class FakeRuntime:
    def __init__(self, status: GatewayStatus, *, stop_result: RuntimeResult | None = None):
        self._status = status
        self._stop_result = stop_result
        self.start_options: GatewayStartOptions | None = None
        self.stopped = False

    def status(self) -> GatewayStatus:
        return self._status

    def start_background(self, options: GatewayStartOptions) -> RuntimeResult:
        self.start_options = options
        return RuntimeResult(True, "gateway_started_background", self._status)

    def stop(self) -> RuntimeResult:
        self.stopped = True
        return self._stop_result or RuntimeResult(True, "gateway_stopped", self._status)

    def read_log_tail(self, *, tail: int = 80) -> list[str]:
        return []


def _config(tmp_path: Path) -> Path:
    config_path = tmp_path / ".local" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"gateway": {"port": 0}, "channels": {"websocket": {"port": 0}}}))
    return config_path


def test_run_up_already_running_skips_start_and_opens_browser(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    workspace_path = tmp_path / ".local" / "workspace"
    (tmp_path / "webui").mkdir()
    dist_index = tmp_path / "nanobot" / "web" / "dist" / "index.html"
    dist_index.parent.mkdir(parents=True)
    dist_index.write_text("<html></html>")

    status = GatewayStatus(
        running=True,
        pid=4242,
        state_path=tmp_path / "gateway.json",
        log_path=tmp_path / "gateway.log",
        started_at=datetime.now(UTC).isoformat(),
        reason="running",
    )
    fake = FakeRuntime(status)
    opened: list[str] = []
    monkeypatch.setattr(up_mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(up_mod, "_runtime_for", lambda *_a, **_kw: fake)
    monkeypatch.setattr(up_mod, "open_browser_if_available", opened.append)

    code = up_mod.run_up(
        config_path=str(config_path), workspace_path=str(workspace_path), print_fn=lambda *_: None
    )

    assert code == 0
    assert fake.start_options is None  # never actually started a new process
    assert opened  # browser was still opened


def test_run_up_stale_code_refuses_to_stop_its_own_ancestor(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    workspace_path = tmp_path / ".local" / "workspace"
    (tmp_path / "webui").mkdir()
    package = tmp_path / "nanobot"
    package.mkdir()
    (package / "changed.py").write_text("x = 1\n")
    dist_index = package / "web" / "dist" / "index.html"
    dist_index.parent.mkdir(parents=True)
    dist_index.write_text("<html></html>")

    status = GatewayStatus(
        running=True,
        pid=os.getpid(),
        state_path=tmp_path / "gateway.json",
        log_path=tmp_path / "gateway.log",
        started_at="2000-01-01T00:00:00+00:00",
        reason="running",
    )
    fake = FakeRuntime(status)
    monkeypatch.delenv("NANOBOT_ALLOW_SELF_STOP", raising=False)
    monkeypatch.setattr(up_mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(up_mod, "_runtime_for", lambda *_a, **_kw: fake)
    monkeypatch.setattr(up_mod, "is_ancestor_pid", lambda _pid: True)

    messages: list[str] = []
    code = up_mod.run_up(
        config_path=str(config_path), workspace_path=str(workspace_path), print_fn=messages.append
    )

    assert code == 2
    assert fake.stopped is False
    assert any("own process tree" in message for message in messages)


def test_run_up_reports_port_conflict_without_starting(tmp_path: Path, monkeypatch):
    config_path = tmp_path / ".local" / "config.json"
    config_path.parent.mkdir(parents=True)
    workspace_path = tmp_path / ".local" / "workspace"
    (tmp_path / "webui").mkdir()
    dist_index = tmp_path / "nanobot" / "web" / "dist" / "index.html"
    dist_index.parent.mkdir(parents=True)
    dist_index.write_text("<html></html>")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    config_path.write_text(
        json.dumps({"gateway": {"host": "127.0.0.1", "port": port}, "channels": {"websocket": {"port": port + 1}}})
    )

    not_running = GatewayStatus(
        running=False, pid=None, state_path=tmp_path / "s.json", log_path=tmp_path / "l.log", reason="not_started"
    )
    fake = FakeRuntime(not_running)
    monkeypatch.setattr(up_mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(up_mod, "_runtime_for", lambda *_a, **_kw: fake)

    try:
        messages: list[str] = []
        code = up_mod.run_up(config_path=str(config_path), workspace_path=str(workspace_path), print_fn=messages.append)
    finally:
        sock.close()

    assert code == 1
    assert fake.start_options is None
    assert any("ports look busy" in m for m in messages)


def test_run_down_falls_back_to_port_when_not_tracked(tmp_path: Path, monkeypatch):
    config_path = _config(tmp_path)
    workspace_path = tmp_path / ".local" / "workspace"

    not_running = GatewayStatus(
        running=False, pid=None, state_path=tmp_path / "s.json", log_path=tmp_path / "l.log", reason="not_started"
    )
    fake = FakeRuntime(not_running)
    monkeypatch.setattr(up_mod, "_runtime_for", lambda *_a, **_kw: fake)
    monkeypatch.setattr(up_mod, "_stop_by_port_fallback", lambda *_a, **_kw: True)

    code = up_mod.run_down(config_path=str(config_path), workspace_path=str(workspace_path), print_fn=lambda *_: None)
    assert code == 0


def test_companion_specs_only_for_primary_config(tmp_path: Path):
    local = tmp_path / ".local"
    local.mkdir()
    (local / "config.genie.json").write_text("{}")
    assert up_mod.companion_gateway_specs(local / "config.genie.json") == ()
    specs = up_mod.companion_gateway_specs(local / "config.json")
    assert [item[0] for item in specs] == ["genie"]
    assert specs[0][3] == 18795


def test_restart_restarts_companions_only_after_primary_success(tmp_path: Path, monkeypatch):
    config_path = tmp_path / ".local" / "config.json"
    workspace_path = tmp_path / ".local" / "workspace"
    calls = []
    monkeypatch.setattr(up_mod, "run_down", lambda **_kwargs: 0)
    monkeypatch.setattr(up_mod, "run_up", lambda **_kwargs: 0)
    monkeypatch.setattr(up_mod, "_runtime_for", lambda *_a, **_kw: type("R", (), {"status": lambda self: type("S", (), {"running": False, "pid": None})()})())
    monkeypatch.setattr(up_mod, "restart_companion_gateways", lambda path, print_fn: calls.append(path.name))

    code = up_mod.run_restart(config_path=str(config_path), workspace_path=str(workspace_path), print_fn=lambda *_: None)
    assert code == 0
    assert calls == ["config.json"]

    calls.clear()
    monkeypatch.setattr(up_mod, "run_up", lambda **_kwargs: 1)
    code = up_mod.run_restart(config_path=str(config_path), workspace_path=str(workspace_path), print_fn=lambda *_: None)
    assert code == 1
    assert calls == []


# ---------------------------------------------------------------------------
# restart must come back on the port the gateway runs on, and never stop it into a failure
# ---------------------------------------------------------------------------


def _persona_config(tmp_path: Path, *, gateway_port: int, websocket_port: int = 0) -> tuple[Path, Path]:
    config_path = tmp_path / ".local" / "config.testy.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({
        "gateway": {"port": gateway_port}, "channels": {"websocket": {"port": websocket_port}},
    }))
    return config_path, tmp_path / ".local" / "workspace-testy"


def _running(port: int | None = 18797, pid: int = 4242) -> GatewayStatus:
    return GatewayStatus(running=True, pid=pid, state_path=Path("s"), log_path=Path("l"), port=port)


def test_restart_starts_the_new_gateway_on_the_port_the_old_one_used(tmp_path: Path, monkeypatch):
    # Several persona configs share gateway.port; the real port was given on the command line.
    config_path, workspace = _persona_config(tmp_path, gateway_port=18793)
    ports: list[int | None] = []
    monkeypatch.setattr(up_mod, "_runtime_for", lambda *_a, **_kw: FakeRuntime(_running(18797)))
    monkeypatch.setattr(up_mod, "is_ancestor_pid", lambda *_a, **_kw: False)
    monkeypatch.setattr(up_mod, "port_in_use", lambda *_a: False)
    monkeypatch.setattr(up_mod, "run_down", lambda **_kw: 0)
    monkeypatch.setattr(up_mod, "run_up", lambda **kw: ports.append(kw.get("gateway_port")) or 0)
    monkeypatch.setattr(up_mod, "restart_companion_gateways", lambda *_a, **_kw: None)

    assert up_mod.run_restart(config_path=str(config_path), workspace_path=str(workspace),
                              print_fn=lambda *_: None) == 0

    assert ports == [18797]


def test_run_up_uses_the_port_it_was_given_over_the_config(tmp_path: Path, monkeypatch):
    config_path, workspace = _persona_config(tmp_path, gateway_port=18793)
    fake = FakeRuntime(GatewayStatus(running=False, pid=None, state_path=Path("s"), log_path=Path("l")))
    monkeypatch.setattr(up_mod, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(up_mod, "_runtime_for", lambda *_a, **_kw: fake)
    monkeypatch.setattr(up_mod, "port_in_use", lambda *_a: False)
    monkeypatch.setattr(up_mod, "wait_for_health", lambda *_a, **_kw: True)
    monkeypatch.setattr(up_mod, "open_browser_if_available", lambda *_a: None)
    (tmp_path / "nanobot" / "web" / "dist").mkdir(parents=True)
    (tmp_path / "nanobot" / "web" / "dist" / "index.html").write_text("x")
    monkeypatch.setattr(up_mod, "ensure_webui_dist", lambda *_a, **_kw: None)

    up_mod.run_up(config_path=str(config_path), workspace_path=str(workspace),
                  print_fn=lambda *_: None, gateway_port=18797)

    assert fake.start_options is not None and fake.start_options.port == 18797


def test_restart_does_not_stop_the_gateway_when_another_process_holds_the_new_port(
    tmp_path: Path, monkeypatch,
):
    config_path, workspace = _persona_config(tmp_path, gateway_port=18793)
    stopped: list[str] = []
    messages: list[str] = []
    monkeypatch.setattr(up_mod, "_runtime_for", lambda *_a, **_kw: FakeRuntime(_running(18797, pid=4242)))
    monkeypatch.setattr(up_mod, "is_ancestor_pid", lambda *_a, **_kw: False)
    monkeypatch.setattr(up_mod, "port_in_use", lambda _host, port: port == 18797)
    monkeypatch.setattr(up_mod, "pid_on_port", lambda _port: 999)       # someone else
    monkeypatch.setattr(up_mod, "run_down", lambda **_kw: stopped.append("down") or 0)
    monkeypatch.setattr(up_mod, "run_up", lambda **_kw: stopped.append("up") or 0)

    code = up_mod.run_restart(config_path=str(config_path), workspace_path=str(workspace),
                              print_fn=messages.append)

    assert code == 1 and stopped == []
    assert any("aborted" in line and "999" in line for line in messages)


def test_restart_proceeds_when_the_port_is_held_by_the_gateway_being_restarted(tmp_path: Path, monkeypatch):
    config_path, workspace = _persona_config(tmp_path, gateway_port=18793)
    steps: list[str] = []
    monkeypatch.setattr(up_mod, "_runtime_for", lambda *_a, **_kw: FakeRuntime(_running(18797, pid=4242)))
    monkeypatch.setattr(up_mod, "is_ancestor_pid", lambda *_a, **_kw: False)
    monkeypatch.setattr(up_mod, "port_in_use", lambda *_a: True)
    monkeypatch.setattr(up_mod, "pid_on_port", lambda _port: 4242)      # the gateway itself
    monkeypatch.setattr(up_mod, "run_down", lambda **_kw: steps.append("down") or 0)
    monkeypatch.setattr(up_mod, "run_up", lambda **_kw: steps.append("up") or 0)
    monkeypatch.setattr(up_mod, "restart_companion_gateways", lambda *_a, **_kw: None)

    assert up_mod.run_restart(config_path=str(config_path), workspace_path=str(workspace),
                              print_fn=lambda *_: None) == 0
    assert steps == ["down", "up"]


def test_restart_does_not_stop_the_gateway_when_the_config_needs_an_unset_env_var(tmp_path: Path, monkeypatch):
    config_path, workspace = _persona_config(tmp_path, gateway_port=18793)
    data = json.loads(config_path.read_text())
    data["channels"]["telegram"] = {"enabled": True, "token": "${NANOBOT_TEST_UNSET_TOKEN}"}
    config_path.write_text(json.dumps(data))
    monkeypatch.delenv("NANOBOT_TEST_UNSET_TOKEN", raising=False)
    steps: list[str] = []
    messages: list[str] = []
    monkeypatch.setattr(up_mod, "_runtime_for", lambda *_a, **_kw: FakeRuntime(_running(18797)))
    monkeypatch.setattr(up_mod, "is_ancestor_pid", lambda *_a, **_kw: False)
    monkeypatch.setattr(up_mod, "run_down", lambda **_kw: steps.append("down") or 0)
    monkeypatch.setattr(up_mod, "run_up", lambda **_kw: steps.append("up") or 0)

    code = up_mod.run_restart(config_path=str(config_path), workspace_path=str(workspace),
                              print_fn=messages.append)

    assert code == 1 and steps == []
    assert any("aborted" in line and "NANOBOT_TEST_UNSET_TOKEN" in line for line in messages)
