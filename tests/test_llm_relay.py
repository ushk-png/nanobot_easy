"""Tests for the PSK-authenticated direct LLM relay."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from nanobot.api import relay as relay_api
from nanobot.api.relay import create_relay_app
from nanobot.config.schema import Config, ModelPresetConfig
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.providers.factory import ProviderSnapshot
from nanobot.skill_store import SkillStore

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)


@pytest_asyncio.fixture
async def aiohttp_client():
    clients: list[TestClient] = []

    async def _make_client(app):
        client = TestClient(TestServer(app))
        await client.start_server()
        clients.append(client)
        return client

    try:
        yield _make_client
    finally:
        for client in clients:
            await client.close()


@dataclass
class FakeProvider:
    calls: list[dict]

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            content="relay ok",
            tool_calls=[
                ToolCallRequest(id="call_1", name="do_work", arguments={"x": 1}),
            ],
            finish_reason="tool_calls",
            usage={"prompt_tokens": 3, "completion_tokens": 2},
        )

    async def chat_stream_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        on_content_delta = kwargs.get("on_content_delta")
        if on_content_delta:
            await on_content_delta("relay")
            await on_content_delta(" stream")
        return LLMResponse(content="relay stream", finish_reason="stop")


def _config(tmp_path: Path) -> Config:
    config = Config()
    config.agents.defaults.workspace = str(tmp_path)
    config.model_presets["relay-coding"] = ModelPresetConfig(
        model="openai-codex/gpt-5.5",
        provider="openai_codex",
    )
    return config


def test_relay_client_tokens_are_hashed_and_revocable(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    issued = store.issue_relay_client(
        client_id="grok-build",
        model_preset="relay-coding",
    )

    assert issued.token.startswith("nbrelay_")
    assert store.verify_relay_token(issued.token).client_id == "grok-build"

    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT secret_hash FROM relay_clients WHERE client_id = 'grok-build'"
        ).fetchone()
    assert row is not None
    assert issued.token not in row[0]
    assert row[0].startswith("pbkdf2_sha256$")

    store.revoke_relay_client("grok-build")
    assert store.verify_relay_token(issued.token) is None


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_relay_requires_psk_and_rejects_arbitrary_model(
    tmp_path: Path,
    aiohttp_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SkillStore(tmp_path)
    issued = store.issue_relay_client(client_id="grok-build", model_preset="relay-coding")
    provider = FakeProvider(calls=[])
    config = _config(tmp_path)

    monkeypatch.setattr(
        relay_api,
        "build_provider_snapshot",
        lambda _config, preset_name=None: ProviderSnapshot(
            provider=provider,
            model="openai-codex/gpt-5.5",
            context_window_tokens=200_000,
            signature=("test", preset_name),
        ),
    )
    client = await aiohttp_client(create_relay_app(config, store))

    missing = await client.get("/v1/models")
    assert missing.status == 401

    wrong_model = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {issued.token}"},
        json={"model": "some-other-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert wrong_model.status == 400


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_relay_calls_provider_directly_and_preserves_tool_calls(
    tmp_path: Path,
    aiohttp_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SkillStore(tmp_path)
    issued = store.issue_relay_client(client_id="grok-build", model_preset="relay-coding")
    provider = FakeProvider(calls=[])
    config = _config(tmp_path)

    monkeypatch.setattr(
        relay_api,
        "build_provider_snapshot",
        lambda _config, preset_name=None: ProviderSnapshot(
            provider=provider,
            model="openai-codex/gpt-5.5",
            context_window_tokens=200_000,
            signature=("test", preset_name),
        ),
    )
    client = await aiohttp_client(create_relay_app(config, store))

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {issued.token}"},
        json={
            "model": "nanobot-relay",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "do_work"}}],
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["choices"][0]["message"]["content"] == "relay ok"
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "do_work"
    assert body["usage"]["total_tokens"] == 5
    assert provider.calls[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert provider.calls[0]["model"] == "openai-codex/gpt-5.5"


@pytest.mark.skipif(not HAS_AIOHTTP, reason="aiohttp not installed")
@pytest.mark.asyncio
async def test_relay_streams_sse(
    tmp_path: Path,
    aiohttp_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SkillStore(tmp_path)
    issued = store.issue_relay_client(client_id="grok-build", model_preset="relay-coding")
    provider = FakeProvider(calls=[])
    config = _config(tmp_path)

    monkeypatch.setattr(
        relay_api,
        "build_provider_snapshot",
        lambda _config, preset_name=None: ProviderSnapshot(
            provider=provider,
            model="openai-codex/gpt-5.5",
            context_window_tokens=200_000,
            signature=("test", preset_name),
        ),
    )
    client = await aiohttp_client(create_relay_app(config, store))

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {issued.token}"},
        json={
            "model": "openai-codex/gpt-5.5",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status == 200
    text = await resp.text()
    assert "relay" in text
    assert "stream" in text
    assert "data: [DONE]" in text
    # Ensure every non-DONE data line is parseable JSON.
    for line in text.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            json.loads(line.removeprefix("data: "))
