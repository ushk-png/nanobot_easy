import json

import pytest

from nanobot.config.loader import load_config
from nanobot.config.schema import ApiConfig


def test_load_config_missing_file_uses_defaults(tmp_path) -> None:
    config = load_config(tmp_path / "missing.json")

    assert config.agents.defaults.model


def test_load_config_invalid_json_fails_fast(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{broken json", encoding="utf-8")

    with pytest.raises(ValueError, match="Failed to load config"):
        load_config(config_path)


def test_load_config_invalid_schema_fails_fast(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"tools": {"exec": {"timeout": -1}}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Failed to load config"):
        load_config(config_path)


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_api_config_requires_key_for_wildcard_hosts(host: str) -> None:
    with pytest.raises(ValueError, match="api_key is not set"):
        ApiConfig(host=host)


def test_api_config_allows_wildcard_host_with_key() -> None:
    config = ApiConfig(host="0.0.0.0", api_key="secret")

    assert config.host == "0.0.0.0"
    assert config.api_key == "secret"


# ---- item 7: anything that is not loopback-only needs authentication ----------------------------

_NON_LOOPBACK = ["192.168.0.10", "10.0.0.5", "[::]", "0", "", "myhost.local", "::ffff:0.0.0.0"]
_LOOPBACK = ["127.0.0.1", "localhost", "LOCALHOST", "[::1]", "::1", "127.0.0.2", "::ffff:127.0.0.1"]


@pytest.mark.parametrize("host", _NON_LOOPBACK)
def test_api_config_requires_key_unless_loopback_only(host: str) -> None:
    with pytest.raises(ValueError, match="api_key is not set"):
        ApiConfig(host=host)
    assert ApiConfig(host=host, api_key="secret").host == host


@pytest.mark.parametrize("host", _LOOPBACK)
def test_api_config_allows_loopback_hosts_without_key(host: str) -> None:
    assert ApiConfig(host=host).host == host


def test_api_config_error_names_the_host() -> None:
    with pytest.raises(ValueError, match="192.168.0.10"):
        ApiConfig(host="192.168.0.10")
