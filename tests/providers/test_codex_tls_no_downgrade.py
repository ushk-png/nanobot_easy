"""A TLS failure must never be retried without verification (review B30)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import nanobot.providers.openai_codex_provider as codex


@pytest.mark.asyncio
async def test_certificate_failure_is_not_retried_without_verification(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_request(url, headers, body, verify, **kwargs):
        calls.append({"verify": verify, "bearer": headers.get("Authorization", "").startswith("Bearer ")})
        raise Exception("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate")

    monkeypatch.setattr(codex, "_request_codex", fake_request)
    monkeypatch.setattr(
        codex, "get_codex_token", lambda proxy=None: SimpleNamespace(access="PLACEHOLDER", account_id="acct")
    )

    response = await codex.OpenAICodexProvider().chat(messages=[{"role": "user", "content": "hi"}])

    assert calls == [{"verify": True, "bearer": True}]  # one attempt, verified, never downgraded
    assert response.finish_reason == "error"
    assert "certificate verification failed" in (response.content or "").lower()
