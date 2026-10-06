"""WebUI settings: two-step xAI Grok OAuth login (start -> pending -> complete)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.config.loader import save_config
from nanobot.config.schema import Config
from nanobot.providers import xai_oauth
from nanobot.providers.registry import find_by_name
from nanobot.providers.xai_oauth import XAIToken
from nanobot.webui import settings_api
from nanobot.webui.settings_api import (
    WebUISettingsError,
    _oauth_provider_status,
    complete_oauth_provider,
    login_oauth_provider,
    logout_oauth_provider,
    settings_payload,
)
from nanobot.webui.settings_routes import WebUISettingsRouter

ACCESS = "access-token-secret"
REFRESH = "refresh-token-secret"


class FakeFlow:
    """Stand-in for XAIOAuthLoginFlow: no sockets, no network."""

    def __init__(self) -> None:
        self.authorization_url = "https://auth.x.ai/oauth2/auth?state=fake"
        self.remaining_seconds = 600
        self.expired = False
        self.cancelled = False
        self.token: XAIToken | None = None
        self.codes: list[str | None] = []
        self.error: Exception | None = None

    def complete(self, authorization_code: str | None = None) -> XAIToken | None:
        self.codes.append(authorization_code)
        if self.error is not None:
            raise self.error
        if authorization_code is not None:
            self.token = _token()
        if self.token is not None:
            with xai_oauth._token_lock():
                xai_oauth._write_token(self.token)
        return self.token

    def cancel(self) -> None:
        self.cancelled = True


def _token() -> XAIToken:
    return XAIToken(
        access=ACCESS,
        refresh=REFRESH,
        expires=int(time.time() * 1000) + 3_600_000,
        account_id="me@example.com",
    )


@pytest.fixture
def instance(tmp_path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("nanobot.config.loader._current_config_path", config_path)
    settings_api._clear_xai_webui_oauth_flows()
    flows: list[FakeFlow] = []

    def fake_start(**_kwargs: Any) -> FakeFlow:
        flow = FakeFlow()
        flows.append(flow)
        return flow

    monkeypatch.setattr("nanobot.providers.xai_oauth.start_xai_oauth_login", fake_start)
    yield SimpleNamespace(config_path=config_path, flows=flows)
    settings_api._clear_xai_webui_oauth_flows()


def _providers(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in payload["providers"]}


def test_status_is_signed_out_without_token(instance) -> None:
    status = _oauth_provider_status(find_by_name("xai_grok"))

    assert status == {
        "configured": False,
        "account": None,
        "expires_at": None,
        "login_supported": True,
    }


def test_provider_row_exposes_default_model(instance) -> None:
    row = _providers(settings_payload())["xai_grok"]

    assert row["auth_type"] == "oauth"
    assert row["configured"] is False
    assert row["oauth_login_supported"] is True
    assert row["oauth_login_mode"] == "authorization_url"
    assert row["oauth_default_model"] == "xai-grok/grok-4.5"
    assert row["oauth_default_context_window_tokens"] == 500_000


def test_login_returns_authorization_url_without_blocking(instance) -> None:
    payload = login_oauth_provider({"provider": ["xai_grok"]})

    assert payload["status"] == "authorization_required"
    assert payload["provider"] == "xai_grok"
    assert payload["authorization_url"].startswith("https://auth.x.ai/")
    assert payload["flow_id"]
    assert payload["expires_in"] == 600
    assert "providers" not in payload


def test_complete_is_pending_until_loopback_callback_arrives(instance) -> None:
    start = login_oauth_provider({"provider": ["xai_grok"]})
    query = {"provider": ["xai_grok"], "flow_id": [start["flow_id"]]}

    assert complete_oauth_provider(query) == {
        "status": "pending",
        "provider": "xai_grok",
        "flow_id": start["flow_id"],
    }

    instance.flows[0].token = _token()  # the loopback callback landed
    done = complete_oauth_provider(query)

    row = _providers(done)["xai_grok"]
    assert row["configured"] is True
    assert row["oauth_account"] == "me@example.com"
    # A finished flow is forgotten.
    with pytest.raises(WebUISettingsError) as excinfo:
        complete_oauth_provider(query)
    assert excinfo.value.status == 410


def test_complete_with_pasted_code_finishes_login(instance) -> None:
    start = login_oauth_provider({"provider": ["xai_grok"]})

    done = complete_oauth_provider(
        {"provider": ["xai_grok"], "flow_id": [start["flow_id"]]},
        "pasted-code",
    )

    assert instance.flows[0].codes == ["pasted-code"]
    assert _providers(done)["xai_grok"]["configured"] is True


def test_token_material_never_reaches_payload_or_config(instance) -> None:
    start = login_oauth_provider({"provider": ["xai_grok"]})
    done = complete_oauth_provider(
        {"provider": ["xai_grok"], "flow_id": [start["flow_id"]]},
        "pasted-code",
    )

    assert ACCESS not in repr(done) and REFRESH not in repr(done)
    config_text = instance.config_path.read_text(encoding="utf-8")
    assert ACCESS not in config_text and REFRESH not in config_text
    assert (instance.config_path.parent / "auth" / "xai.json").exists()


def test_complete_rejects_unknown_flow_and_other_providers(instance) -> None:
    with pytest.raises(WebUISettingsError) as missing:
        complete_oauth_provider({"provider": ["xai_grok"], "flow_id": ["nope"]})
    assert missing.value.status == 410

    with pytest.raises(WebUISettingsError):
        complete_oauth_provider({"provider": ["xai_grok"]})
    with pytest.raises(WebUISettingsError):
        complete_oauth_provider({"provider": ["openai_codex"], "flow_id": ["x"]})


def test_expired_flow_is_cancelled_and_reported(instance) -> None:
    start = login_oauth_provider({"provider": ["xai_grok"]})
    instance.flows[0].expired = True

    with pytest.raises(WebUISettingsError) as excinfo:
        complete_oauth_provider({"provider": ["xai_grok"], "flow_id": [start["flow_id"]]})

    assert excinfo.value.status == 410
    assert instance.flows[0].cancelled is True


def test_failed_exchange_drops_the_flow(instance) -> None:
    start = login_oauth_provider({"provider": ["xai_grok"]})
    instance.flows[0].error = xai_oauth.XAIOAuthError("xAI sign-in was not completed: denied")
    query = {"provider": ["xai_grok"], "flow_id": [start["flow_id"]]}

    with pytest.raises(WebUISettingsError) as excinfo:
        complete_oauth_provider(query, "bad-code")

    assert excinfo.value.status == 502
    assert instance.flows[0].cancelled is True
    with pytest.raises(WebUISettingsError):
        complete_oauth_provider(query)


def test_pending_flows_are_capped(instance) -> None:
    for _ in range(settings_api._XAI_WEBUI_OAUTH_MAX_FLOWS + 2):
        login_oauth_provider({"provider": ["xai_grok"]})

    assert len(settings_api._xai_webui_oauth_flows) == settings_api._XAI_WEBUI_OAUTH_MAX_FLOWS
    assert [flow.cancelled for flow in instance.flows[:2]] == [True, True]
    assert instance.flows[-1].cancelled is False


def test_logout_removes_token_and_cancels_pending_flows(instance) -> None:
    start = login_oauth_provider({"provider": ["xai_grok"]})
    complete_oauth_provider({"provider": ["xai_grok"], "flow_id": [start["flow_id"]]}, "code")
    login_oauth_provider({"provider": ["xai_grok"]})  # a second, still pending flow
    token_path = instance.config_path.parent / "auth" / "xai.json"
    assert token_path.exists()

    payload = logout_oauth_provider({"provider": ["xai_grok"]})

    assert not token_path.exists()
    assert _providers(payload)["xai_grok"]["configured"] is False
    assert instance.flows[1].cancelled is True
    assert settings_api._xai_webui_oauth_flows == {}


# --- HTTP routing ----------------------------------------------------------


def _router() -> WebUISettingsRouter:
    return WebUISettingsRouter(
        bus=SimpleNamespace(),
        logger=SimpleNamespace(),
        check_api_token=lambda _request: True,
        parse_query=lambda path: {
            key: [value]
            for key, value in (
                pair.split("=", 1) for pair in path.partition("?")[2].split("&") if "=" in pair
            )
        },
        json_response=lambda payload: ("json", payload),
        error_response=lambda status, message: ("error", status, message),
        runtime_surface="browser",
        runtime_capabilities={},
    )


def _dispatch(router: WebUISettingsRouter, path: str, headers: dict[str, str] | None = None):
    request = SimpleNamespace(path=path, headers=headers or {})
    return asyncio.run(router.dispatch(None, request, path.partition("?")[0]))


def test_routes_run_the_two_step_flow_with_the_code_in_a_header(instance) -> None:
    router = _router()

    kind, start = _dispatch(router, "/api/settings/provider/oauth-login?provider=xai_grok")
    assert kind == "json" and start["status"] == "authorization_required"
    assert "requires_restart" not in start

    complete_path = (
        f"/api/settings/provider/oauth-login/complete?provider=xai_grok&flow_id={start['flow_id']}"
    )
    kind, pending = _dispatch(router, complete_path)
    assert kind == "json" and pending["status"] == "pending"

    kind, done = _dispatch(router, complete_path, {"x-nanobot-oauth-code": "pasted-code"})
    assert kind == "json"
    assert instance.flows[0].codes == [None, "pasted-code"]
    assert _providers(done)["xai_grok"]["configured"] is True


def test_route_rejects_oversized_authorization_code(instance) -> None:
    router = _router()
    _, start = _dispatch(router, "/api/settings/provider/oauth-login?provider=xai_grok")

    response = _dispatch(
        router,
        f"/api/settings/provider/oauth-login/complete?provider=xai_grok&flow_id={start['flow_id']}",
        {"X-Nanobot-OAuth-Code": "x" * (8 * 1024 + 1)},
    )

    assert response[0] == "error" and response[1] == 400
    assert instance.flows[0].codes == []
