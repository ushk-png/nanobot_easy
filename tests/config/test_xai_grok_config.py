"""xAI Grok OAuth request settings survive a config save/load round trip."""

import json

from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import Config


def test_xai_grok_proxy_and_extra_body_round_trip(tmp_path) -> None:
    path = tmp_path / "config.json"
    config = Config.model_validate(
        {
            "providers": {
                "xaiGrok": {
                    "proxy": "http://127.0.0.1:7890",
                    "extraBody": {"parallel_tool_calls": False},
                    "apiKey": "must-not-be-saved",
                },
                "openaiCodex": {"proxy": "http://127.0.0.1:7891"},
            }
        }
    )

    save_config(config, path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["providers"]["xaiGrok"] == {
        "proxy": "http://127.0.0.1:7890",
        "extraBody": {"parallel_tool_calls": False},
    }
    assert raw["providers"]["openaiCodex"] == {"proxy": "http://127.0.0.1:7891"}
    assert "must-not-be-saved" not in path.read_text(encoding="utf-8")

    reloaded = load_config(path)
    assert reloaded.providers.xai_grok.proxy == "http://127.0.0.1:7890"
    assert reloaded.providers.xai_grok.extra_body == {"parallel_tool_calls": False}


def test_unconfigured_xai_grok_is_not_written(tmp_path) -> None:
    path = tmp_path / "config.json"

    save_config(Config(), path)

    assert "xaiGrok" not in json.loads(path.read_text(encoding="utf-8")).get("providers", {})
