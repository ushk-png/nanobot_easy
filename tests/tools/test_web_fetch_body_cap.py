"""web_fetch must stop reading a response that is over the limit, whatever the headers say (C09).

A local loopback server streams a chunked text body and an image far larger than the limits;
SSRF checks are relaxed for loopback only inside this test.
"""

from __future__ import annotations

import asyncio
import functools
import json

import pytest

import nanobot.agent.tools.web as web
from nanobot.security import network


@pytest.fixture
def loopback_allowed(monkeypatch):
    monkeypatch.setattr(web, "_validate_url_safe", functools.partial(network.validate_url_target, allow_loopback=True))
    monkeypatch.setattr(web, "_resolve_url_safe", functools.partial(network.resolve_url_target, allow_loopback=True))
    monkeypatch.setattr(web, "_pinned_dns_transport", lambda: network.PinnedDNSAsyncTransport(allow_loopback=True))


def _handler(kind: str, total: int, sent: dict[str, int]):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        if kind == "chunked":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n")
        else:
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: {total}\r\n\r\n".encode()
            )
        chunk = b"a" * 65536
        written = 0
        try:
            while written < total:
                writer.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n" if kind == "chunked" else chunk)
                written += len(chunk)
                await writer.drain()
            if kind == "chunked":
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        sent[kind] = written
        writer.close()

    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,limit_attr", [("chunked", "_MAX_FETCH_BYTES"), ("image", "_MAX_IMAGE_BYTES")])
async def test_oversized_bodies_are_cut_off_and_reported(loopback_allowed, monkeypatch, kind, limit_attr) -> None:
    limit = 1_000_000
    monkeypatch.setattr(web.WebFetchTool, limit_attr, limit)
    sent: dict[str, int] = {}
    server = await asyncio.start_server(_handler(kind, 40 * limit, sent), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        tool = web.WebFetchTool(config=web.WebFetchConfig(use_jina_reader=False))
        result = await tool.execute(url=f"http://127.0.0.1:{port}/x")
    finally:
        server.close()
    await asyncio.sleep(0.2)
    payload = json.loads(result) if isinstance(result, str) else None
    assert payload is not None and "too large" in payload["error"].lower(), str(result)[:200]
    assert sent[kind] < 10 * limit  # the client hung up long before the 40x body was delivered
