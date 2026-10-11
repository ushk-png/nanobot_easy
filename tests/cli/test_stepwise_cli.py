"""Stepwise reasoning (Genie method) is applied per agent, explicitly and on record."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from nanobot.cli.stepwise import (
    apply_stepwise,
    find_agent_configs,
    remove_stepwise,
    stepwise_status,
)

NOW = datetime(2026, 10, 11, 6, 0, tzinfo=timezone.utc)


def _config(path: Path, **defaults) -> Path:
    path.write_text(
        json.dumps({"agents": {"defaults": {"model": "xai-grok/grok-4.7", **defaults}}, "channels": {"x": 1}}),
        encoding="utf-8",
    )
    return path


def test_apply_writes_a_dated_block_and_keeps_a_backup(tmp_path: Path) -> None:
    config = _config(tmp_path / "config.genie.json", reasoningEffort="none")

    result = apply_stepwise(config, now=NOW)

    raw = json.loads(config.read_text())
    block = raw["agents"]["defaults"]["stepwise"]
    assert block["enabled"] is True and block["method"] == "genie" and block["layout"] == "auto"
    assert block["appliedAt"].startswith("2026-10-11")
    assert raw["channels"] == {"x": 1}  # the rest of the config is untouched
    assert raw["agents"]["defaults"]["reasoningEffort"] == "none"
    backup = Path(result["backup"])
    assert backup.exists() and "stepwise" not in backup.read_text()


def test_remove_turns_it_off_and_keeps_the_record(tmp_path: Path) -> None:
    config = _config(tmp_path / "config.json")
    apply_stepwise(config, now=NOW)

    remove_stepwise(config, now=NOW)

    block = json.loads(config.read_text())["agents"]["defaults"]["stepwise"]
    assert block["enabled"] is False
    assert block["appliedAt"].startswith("2026-10-11") and block["removedAt"].startswith("2026-10-11")


def test_apply_refuses_a_config_that_would_not_load(tmp_path: Path) -> None:
    config = _config(tmp_path / "config.json")
    with pytest.raises(ValueError):
        apply_stepwise(config, layout="seven_calls")
    bad = tmp_path / "config.bad.json"
    bad.write_text(json.dumps({"agents": {"defaults": {"maxTokens": "lots"}}}))
    with pytest.raises(Exception):
        apply_stepwise(bad)
    assert not list(tmp_path.glob("config.bad.json.bak-stepwise-*"))


def test_status_lists_every_agent_and_flags_the_old_implicit_switch(tmp_path: Path) -> None:
    apply_stepwise(_config(tmp_path / "config.genie.json", reasoningEffort="none"), now=NOW)
    _config(tmp_path / "config.heart.json", reasoningEffort="none")
    _config(tmp_path / "config.json")
    (tmp_path / "config.json.bak-old").write_text("{}")

    paths = find_agent_configs(tmp_path)
    rows = {row["agent"]: row for row in stepwise_status(paths)}

    assert [path.name for path in paths] == ["config.genie.json", "config.heart.json", "config.json"]
    assert rows["genie"]["enabled"] is True and rows["genie"]["legacy_none"] is False
    assert rows["heart"]["enabled"] is False and rows["heart"]["legacy_none"] is True
    assert rows["default"]["enabled"] is False and rows["default"]["legacy_none"] is False


def test_cli_apply_and_status(tmp_path: Path) -> None:
    from nanobot.cli.commands import app

    config = _config(tmp_path / "config.genie.json")
    runner = CliRunner()

    applied = runner.invoke(app, ["stepwise", "apply", "--config", str(config)])
    status = runner.invoke(app, ["stepwise", "status", "--dir", str(tmp_path)])

    assert applied.exit_code == 0, applied.output
    assert "지니 방식 적용" in applied.output
    assert status.exit_code == 0, status.output
    assert "genie" in status.output and "적용됨" in status.output


def _loop(enabled: bool, effort: str | None):
    from nanobot.agent.loop import AgentLoop

    loop = AgentLoop.__new__(AgentLoop)
    loop.reasoning_effort = effort
    loop.stepwise_enabled = enabled
    return loop


def test_reasoning_effort_none_alone_no_longer_turns_it_on() -> None:
    assert _loop(False, "none")._stepwise_enabled() is False
    assert _loop(True, "high")._stepwise_enabled() is True
    loop = _loop(False, "none")
    del loop.stepwise_enabled
    assert loop._stepwise_enabled() is False


def test_config_block_reaches_the_agent_loop(tmp_path: Path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.config.schema import Config

    config = Config.model_validate(
        {"agents": {"defaults": {"stepwise": {"enabled": True, "appliedAt": "2026-10-11T15:00:00+09:00",
                                               "layout": "compact", "reviewRequired": ["rule-judgment"]}}}}
    )
    seen = {}

    def fake_init(self, **kwargs):
        seen.update(kwargs)

    original = AgentLoop.__init__
    AgentLoop.__init__ = fake_init
    try:
        try:
            AgentLoop.from_config(config, bus=SimpleNamespace(), provider=SimpleNamespace())
        except Exception:
            pass  # only the keyword arguments matter here
    finally:
        AgentLoop.__init__ = original

    assert seen["stepwise_enabled"] is True
    assert seen["stepwise_applied_at"] == "2026-10-11T15:00:00+09:00"
    assert seen["stepwise_layout"] == "compact"
    assert seen["stepwise_review_required"] == ["rule-judgment"]
