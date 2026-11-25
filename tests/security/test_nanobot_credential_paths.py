"""File tools and previews must not expose this instance's own credentials (review N06)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.config.loader import set_config_path
from nanobot.security.secrets import is_sensitive_path


@pytest.fixture
def instance(tmp_path):
    config = tmp_path / ".local" / "config.testy.json"
    config.parent.mkdir(parents=True)
    set_config_path(config)
    yield config.parent
    set_config_path(Path.home() / ".nanobot" / "config.json")


@pytest.mark.parametrize(
    "relative",
    ["config.testy.json", "config.json", "env", "auth/xai.json", "auth/anything.json", "auth"],
)
def test_instance_credential_files_are_sensitive(instance, relative) -> None:
    assert is_sensitive_path(instance / relative)


@pytest.mark.parametrize(
    "path",
    [
        "~/.nanobot/config.json",
        "~/.nanobot/auth/xai.json",
        "~/.docker/config.json",
        "~/.config/gh/hosts.yml",
    ],
)
def test_well_known_credential_files_are_sensitive(instance, path) -> None:
    assert is_sensitive_path(Path(path).expanduser())


def test_unrelated_files_with_similar_names_stay_readable(instance, tmp_path) -> None:
    assert not is_sensitive_path(tmp_path / "project" / "config.json")
    assert not is_sensitive_path(instance / "workspace" / "notes.md")
    assert not is_sensitive_path(instance / "workspace" / "env")
    assert not is_sensitive_path(instance / "media" / "photo.png")


def test_symlink_to_a_credential_file_is_still_blocked(instance, tmp_path) -> None:
    link = tmp_path / "innocent.txt"
    link.symlink_to(instance / "env")
    assert is_sensitive_path(link)
