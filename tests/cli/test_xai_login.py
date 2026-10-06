"""`nanobot provider login/logout xai-grok` honours --config for token storage."""

from __future__ import annotations

import json
import time

import pytest
from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.providers import xai_oauth
from nanobot.providers.xai_oauth import XAIToken


def test_login_with_config_stores_token_next_to_config(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The CLI calls set_config_path(); restore the global after the test.
    monkeypatch.setattr("nanobot.config.loader._current_config_path", None)
    config_path = tmp_path / "instance" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text("{}", encoding="utf-8")
    token = XAIToken(
        access="access-secret",
        refresh="refresh-secret",
        expires=int(time.time() * 1000) + 3_600_000,
        account_id="me@example.com",
    )

    def fake_login(**_kwargs):
        with xai_oauth._token_lock():
            xai_oauth._write_token(token)
        return token

    monkeypatch.setattr(xai_oauth, "login_xai_oauth", fake_login)
    token_path = config_path.parent / "auth" / "xai.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["provider", "login", "xai-grok", "--set-main", "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    assert token_path.exists()
    config_text = config_path.read_text(encoding="utf-8")
    defaults = json.loads(config_text)["agents"]["defaults"]
    assert defaults["provider"] == "xai_grok"
    assert defaults["model"] == "xai-grok/grok-4.5"
    assert defaults["contextWindowTokens"] == 500_000
    assert "access-secret" not in config_text
    assert "refresh-secret" not in config_text
    assert "access-secret" not in result.output

    result = runner.invoke(app, ["provider", "logout", "xai-grok", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert not token_path.exists()
