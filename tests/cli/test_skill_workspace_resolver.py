"""Unit tests for the skill-CLI workspace resolver.

The resolver deterministically picks which runtime workspace ``nanobot skill
…`` should operate against so that bare invocations no longer silently target
the default ``~/.nanobot/workspace`` while the user is inside a project that
has its own workspace. Tests inject ``env`` and ``cwd`` so nothing touches the
real process environment or the caller's home directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.cli.commands import (
    _WORKSPACE_ENV_VAR,
    _cwd_looks_like_project,
    _resolve_skill_workspace,
)


def _make_workspace(root: Path, rel: str = ".") -> Path:
    """Create ``<root>/<rel>/.skillstore/skillstore.db`` so it looks real."""
    ws = (root / rel).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    store_dir = ws / ".skillstore"
    store_dir.mkdir(parents=True, exist_ok=True)
    (store_dir / "skillstore.db").write_bytes(b"")
    return ws


# ---------------------------------------------------------------------------
# Priority order — explicit arg wins over env, env over discovery, all over default
# ---------------------------------------------------------------------------


def test_resolver_prefers_explicit_arg_over_env_and_cwd(tmp_path):
    _make_workspace(tmp_path, ".local/workspace")   # would be discoverable
    env = {_WORKSPACE_ENV_VAR: str(tmp_path / "envdir")}
    ws, source = _resolve_skill_workspace("/tmp/cli_arg_ws", env=env, cwd=tmp_path)
    assert ws == "/tmp/cli_arg_ws"
    assert source == "cli-arg"


def test_resolver_prefers_env_over_cwd_discovery(tmp_path):
    _make_workspace(tmp_path, ".local/workspace")   # would be discoverable
    env = {_WORKSPACE_ENV_VAR: "/some/env/workspace"}
    ws, source = _resolve_skill_workspace(None, env=env, cwd=tmp_path)
    assert ws == "/some/env/workspace"
    assert source == "env"


# ---------------------------------------------------------------------------
# Discovery — cwd itself, .local/workspace, and parent-dir walk
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", [".", ".local/workspace", ".nanobot/workspace", "workspace"])
def test_resolver_discovers_workspace_marker_in_cwd(tmp_path, rel):
    ws = _make_workspace(tmp_path, rel)
    resolved, source = _resolve_skill_workspace(None, env={}, cwd=tmp_path)
    assert Path(resolved) == ws
    assert source == "discovered"


def test_resolver_discovers_workspace_marker_in_parent_dir(tmp_path):
    ws = _make_workspace(tmp_path, ".local/workspace")
    nested = tmp_path / "nanobot" / "cli"
    nested.mkdir(parents=True)
    resolved, source = _resolve_skill_workspace(None, env={}, cwd=nested)
    assert Path(resolved) == ws
    assert source == "discovered"


# ---------------------------------------------------------------------------
# Default fallback — no marker anywhere → None + source="default"
# ---------------------------------------------------------------------------


def test_resolver_falls_back_to_default_when_no_marker(tmp_path):
    # No workspace layout under tmp_path.
    resolved, source = _resolve_skill_workspace(None, env={}, cwd=tmp_path)
    assert resolved is None                    # caller uses its own default
    assert source == "default"


# ---------------------------------------------------------------------------
# Project detection — used to decide whether to print the warning banner
# ---------------------------------------------------------------------------


def test_cwd_looks_like_project_detects_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    assert _cwd_looks_like_project(tmp_path) is True


def test_cwd_does_not_look_like_project_when_empty(tmp_path):
    assert _cwd_looks_like_project(tmp_path) is False


def test_cwd_looks_like_project_walks_up_to_marker(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert _cwd_looks_like_project(nested) is True
