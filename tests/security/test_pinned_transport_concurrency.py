"""The DNS-pinning transport must not make unrelated requests wait for each other (review C02)."""

from __future__ import annotations

import asyncio
import socket
import time

import httpx
import pytest

from nanobot.security import network
from nanobot.security.network import PinnedDNSAsyncTransport, pin_resolved_url_dns


async def _server(delay: float):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(delay)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_a_slow_response_does_not_hold_up_a_fast_request_to_another_server() -> None:
    slow, slow_port = await _server(2.0)
    fast, fast_port = await _server(0.0)
    try:
        async with httpx.AsyncClient(transport=PinnedDNSAsyncTransport(allow_loopback=True), trust_env=False) as one, \
                   httpx.AsyncClient(transport=PinnedDNSAsyncTransport(allow_loopback=True), trust_env=False) as two:
            slow_task = asyncio.create_task(one.get(f"http://127.0.0.1:{slow_port}/"))
            await asyncio.sleep(0.2)
            started = time.monotonic()
            response = await two.get(f"http://127.0.0.1:{fast_port}/")
            assert response.status_code == 200
            assert time.monotonic() - started < 1.0, "the fast request waited for the slow one"
            await slow_task
    finally:
        slow.close()
        fast.close()


@pytest.mark.asyncio
async def test_dns_lookup_runs_off_the_event_loop(monkeypatch) -> None:
    ticks: list[float] = []

    async def heartbeat() -> None:
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    def slow_resolve(url, *, allow_loopback=False):
        time.sleep(0.5)  # a slow resolver
        return False, "blocked for the test", ()

    monkeypatch.setattr(network, "resolve_url_target", slow_resolve)
    task = asyncio.create_task(heartbeat())
    transport = PinnedDNSAsyncTransport()
    with pytest.raises(httpx.RequestError):
        await transport.handle_async_request(httpx.Request("GET", "https://example.test/"))
    task.cancel()
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert gaps and max(gaps) < 0.3, "the event loop was blocked while resolving"


def test_overlapping_pins_for_different_hosts_restore_the_real_resolver(monkeypatch) -> None:
    sentinel = socket.getaddrinfo
    with pin_resolved_url_dns("https://one.test/", ("93.184.216.34",)):
        with pin_resolved_url_dns("https://two.test/", ("1.1.1.1",)):
            assert socket.getaddrinfo("one.test", 443)[0][4][0] == "93.184.216.34"
            assert socket.getaddrinfo("two.test", 443)[0][4][0] == "1.1.1.1"
        # leaving the inner pin must not undo the outer one
        assert socket.getaddrinfo("one.test", 443)[0][4][0] == "93.184.216.34"
        with pytest.raises(socket.gaierror):  # two.test is no longer pinned; `.test` never resolves
            socket.getaddrinfo("two.test", 443)
    assert socket.getaddrinfo is sentinel
