"""A cut or failed Responses stream is a transient error and must be retried (review B31)."""

from __future__ import annotations

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.openai_codex_provider import _codex_error_response
from nanobot.providers.openai_responses.parsing import consume_sse
from nanobot.providers.xai_grok_provider import _xai_error_response


@pytest.mark.parametrize("detail", ["missing_terminal_event", "server_error", "rate_limit_exceeded"])
@pytest.mark.parametrize("convert", [_xai_error_response, _codex_error_response])
def test_stream_failures_are_transient(convert, detail) -> None:
    response = convert(RuntimeError("Response failed: " + detail))
    assert LLMProvider._is_transient_response(response) is True


@pytest.mark.parametrize("detail", ["invalid_request_error", "provider_error", "context_length_exceeded"])
def test_rejected_requests_are_not_retried(detail) -> None:
    response = LLMResponse(content=f"Error calling xAI (RuntimeError): Response failed: {detail}",
                           finish_reason="error")
    assert LLMProvider._is_transient_response(response) is False


class _Response:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


@pytest.mark.asyncio
async def test_dict_error_detail_keeps_its_code() -> None:
    import json

    event = {"type": "response.failed", "error": {"code": "server_error", "message": "boom with secret"}}
    response = _Response([f"data: {json.dumps(event)}", ""])
    with pytest.raises(RuntimeError) as raised:
        await consume_sse(response)
    assert str(raised.value) == "Response failed: server_error"
    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_retry_after_is_capped_in_standard_mode(monkeypatch) -> None:
    """Review B22: Retry-After: 3600 used to hold the turn for an hour (up to 3 times)."""
    import asyncio

    class Provider(LLMProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(content="Error: rate limited", finish_reason="error",
                                   error_status_code=429, error_retry_after_s=3600.0)
            return LLMResponse(content="ok")

        def get_default_model(self) -> str:
            return "m"

    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *args, **kwargs):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    response = await Provider().chat_with_retry(messages=[{"role": "user", "content": "hi"}])
    assert response.content == "ok"
    assert sum(slept) <= LLMProvider._STANDARD_MAX_RETRY_AFTER + 1
