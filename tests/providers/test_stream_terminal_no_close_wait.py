"""response.completed returns the result at once; the client never waits for the server to close.

Regression for the live defect where a design call finished only when the connection closed
(idle timeout) after response.completed. Covers both Responses transports (xAI and Codex).
"""
from __future__ import annotations

import asyncio

import pytest

import nanobot.providers.openai_codex_provider as codex_provider
from nanobot.providers.call_observation import _cleanup_tasks
from nanobot.providers.xai_grok_provider import _request_xai

from tests.providers.test_response_stream_lifecycle import Events, FakeClient, call, terminal


@pytest.mark.parametrize("transport", ["xai", "codex"])
@pytest.mark.parametrize("close", ["normal", "hold"])
async def test_terminal_event_returns_while_connection_stays_open(monkeypatch, transport, close):
    stream = Events([{"type": "response.output_text.delta", "delta": "a"}, terminal(output=[call()])],
                    hold=True)  # the server never closes the body after response.completed
    client = FakeClient(stream, close)
    if transport == "xai":
        monkeypatch.setattr("nanobot.providers.xai_grok_provider.httpx.AsyncClient", lambda **kw: client)
        request = _request_xai("https://synthetic.invalid", {}, {})
    else:
        monkeypatch.setattr(codex_provider.httpx, "AsyncClient", lambda **kw: client)
        request = codex_provider._request_codex("https://synthetic.invalid", {}, {}, verify=True)
    async with asyncio.timeout(0.2):  # far below any idle timeout
        _, calls, status, _, _ = await request
    assert status == "stop" and calls[0].arguments == {"value": 2}
    assert not stream.reached_eof  # result delivered before EOF / close
    await client.closing.wait()     # disposal runs in the background
    client.release.set()
    stream.gate.set()
    await asyncio.gather(*list(_cleanup_tasks))
    assert client.client_closed
