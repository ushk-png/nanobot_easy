"""Instances that share a config folder must not share WebUI settings (review A32)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanobot.config.loader import get_config_path, set_config_path
from nanobot.webui import sidebar_state, workspaces


@pytest.fixture(autouse=True)
def _restore_config_path():
    original = get_config_path()
    yield
    set_config_path(original)


def _use(config: Path, monkeypatch, webui: Path) -> None:
    set_config_path(config)
    monkeypatch.setattr("nanobot.webui.workspaces.get_webui_dir", lambda: webui)
    monkeypatch.setattr("nanobot.webui.sidebar_state.get_webui_dir", lambda: webui)


def test_each_instance_gets_its_own_state_file(tmp_path, monkeypatch) -> None:
    webui = tmp_path / ".local" / "webui"
    webui.mkdir(parents=True)
    main, testy = tmp_path / ".local" / "config.json", tmp_path / ".local" / "config.testy.json"

    _use(main, monkeypatch, webui)
    assert workspaces.webui_workspace_state_path() == webui / "workspace-state.json"  # legacy name kept
    assert sidebar_state.webui_sidebar_state_path() == webui / "sidebar-state.json"

    _use(testy, monkeypatch, webui)
    assert workspaces.webui_workspace_state_path() == webui / "workspace-state.testy.json"
    assert sidebar_state.webui_sidebar_state_path() == webui / "sidebar-state.testy.json"


def test_a_new_instance_file_starts_as_a_copy_and_then_diverges(tmp_path, monkeypatch) -> None:
    webui = tmp_path / ".local" / "webui"
    webui.mkdir(parents=True)
    shared = webui / "sidebar-state.json"
    shared.write_text(json.dumps({"pinned": ["keep-me"]}))

    _use(tmp_path / ".local" / "config.testy.json", monkeypatch, webui)
    own = sidebar_state.webui_sidebar_state_path()
    assert json.loads(own.read_text()) == {"pinned": ["keep-me"]}  # nothing the user set disappears
    own.write_text(json.dumps({"pinned": ["testy-only"]}))
    assert json.loads(shared.read_text()) == {"pinned": ["keep-me"]}  # main is unaffected


def test_access_mode_changed_in_one_instance_does_not_reach_the_other(tmp_path, monkeypatch) -> None:
    webui = tmp_path / ".local" / "webui"
    webui.mkdir(parents=True)
    _use(tmp_path / ".local" / "config.testy.json", monkeypatch, webui)
    state = workspaces.default_webui_workspace_state()
    state["default_access_mode"] = "full"
    workspaces.webui_workspace_state_path().write_text(json.dumps(state))

    _use(tmp_path / ".local" / "config.json", monkeypatch, webui)
    other = workspaces.webui_workspace_state_path()
    assert not other.exists() or json.loads(other.read_text()).get("default_access_mode") != "full"
