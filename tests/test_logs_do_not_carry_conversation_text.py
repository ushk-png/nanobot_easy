"""Info logs must not copy what users said or asked for (review N23, A08, A40)."""

from __future__ import annotations

import pytest
from loguru import logger

SECRET = "my-private-sentence-about-health"


@pytest.fixture
def captured():
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="DEBUG")
    yield lines
    logger.remove(sink)


@pytest.mark.asyncio
async def test_api_request_log_has_the_length_not_the_text(captured, tmp_path) -> None:
    from unittest.mock import AsyncMock, MagicMock

    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    from nanobot.api.server import create_app

    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="ok")
    agent._last_usage = {}
    client = TestClient(TestServer(create_app(agent, model_name="m", api_key="k")))
    await client.start_server()
    try:
        await client.post(
            "/v1/chat/completions", headers={"Authorization": "Bearer k"},
            json={"messages": [{"role": "user", "content": SECRET}]},
        )
    finally:
        await client.close()
    assert any("API request" in line for line in captured)
    assert not any(SECRET in line for line in captured)


def test_custom_image_request_log_has_no_prompt(captured) -> None:
    import inspect

    from nanobot.providers import image_generation

    source = inspect.getsource(image_generation)
    assert 'body={}", self.api_base, body)' not in source  # the whole body (prompt) is not logged
