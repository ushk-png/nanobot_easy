"""PSK-authenticated OpenAI-compatible relay for external tools.

Unlike ``nanobot.api.server``, this module does not enter AgentLoop. It calls
the configured provider directly so coding tools can use nanobot as a local
LLM backend without receiving nanobot memory, skills, tools, or agent prompts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

from aiohttp import web
from loguru import logger

from nanobot.config.schema import Config
from nanobot.providers.factory import ProviderSnapshot, build_provider_snapshot
from nanobot.skill_store import RelayClientRecord, SkillStore


def _error_json(status: int, message: str, err_type: str = "invalid_request_error") -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": err_type, "code": status}},
        status=status,
    )


def _usage_payload(usage: dict[str, int] | None) -> dict[str, int]:
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    total = int(usage.get("total_tokens", 0) or 0) or prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _message_payload(response) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": response.content or "",
    }
    if response.tool_calls:
        message["tool_calls"] = [call.to_openai_tool_call() for call in response.tool_calls]
        if not response.content:
            message["content"] = None
    return message


def _chat_response(response, *, model: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": _message_payload(response),
                "finish_reason": response.finish_reason or "stop",
            }
        ],
        "usage": _usage_payload(response.usage),
    }


def _sse(payload: dict[str, Any] | str) -> bytes:
    if isinstance(payload, str):
        return f"data: {payload}\n\n".encode("utf-8")
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def _content_chunk(text: str, *, chunk_id: str, model: str) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }


def _tool_delta_chunk(delta: dict[str, Any], *, chunk_id: str, model: str) -> dict[str, Any]:
    index = int(delta.get("index") or 0)
    tool_call: dict[str, Any] = {
        "index": index,
        "type": "function",
        "function": {},
    }
    call_id = str(delta.get("call_id") or "")
    if call_id:
        tool_call["id"] = call_id
    name = str(delta.get("name") or "")
    if name:
        tool_call["function"]["name"] = name
    args = str(delta.get("arguments_delta") or "")
    if args:
        tool_call["function"]["arguments"] = args
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {"tool_calls": [tool_call]}, "finish_reason": None}],
    }


def _final_chunk(*, chunk_id: str, model: str, finish_reason: str) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason or "stop"}],
    }


def _bearer_token(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    return token or None


def _remote_ip(request: web.Request) -> str | None:
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return None


def _request_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    sanitized: list[dict[str, Any]] = []
    allowed = {
        "role",
        "content",
        "name",
        "tool_call_id",
        "tool_calls",
    }
    for msg in messages:
        if not isinstance(msg, dict):
            raise ValueError("messages entries must be objects")
        role = msg.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"unsupported message role: {role!r}")
        sanitized.append({k: v for k, v in msg.items() if k in allowed})
    return sanitized


def _requested_model_allowed(requested: str | None, *, client: RelayClientRecord, model: str) -> bool:
    if not requested:
        return True
    allowed = {model, client.model_preset, client.client_id, "nanobot-relay"}
    return requested in allowed


class RelayRuntime:
    """Small provider snapshot cache keyed by model preset name."""

    def __init__(self, config: Config, store: SkillStore) -> None:
        self.config = config
        self.store = store
        self._snapshots: dict[str, ProviderSnapshot] = {}

    def snapshot_for(self, preset_name: str) -> ProviderSnapshot:
        key = "" if preset_name in {"", "default"} else preset_name
        if key not in self._snapshots:
            self._snapshots[key] = build_provider_snapshot(
                self.config,
                preset_name=None if not key else key,
            )
        return self._snapshots[key]

    def authenticate(self, request: web.Request) -> RelayClientRecord | None:
        token = _bearer_token(request)
        if not token:
            return None
        return self.store.verify_relay_token(token, remote_ip=_remote_ip(request))


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "nanobot-relay"})


async def handle_models(request: web.Request) -> web.Response:
    runtime: RelayRuntime = request.app["relay_runtime"]
    client = runtime.authenticate(request)
    if client is None:
        return _error_json(401, "Missing or invalid relay token", "authentication_error")
    try:
        snapshot = runtime.snapshot_for(client.model_preset)
    except Exception as exc:
        logger.exception("Relay model resolution failed for {}", client.client_id)
        return _error_json(500, f"Relay model resolution failed: {exc}", "server_error")
    return web.json_response(
        {
            "object": "list",
            "data": [
                {
                    "id": snapshot.model,
                    "object": "model",
                    "created": 0,
                    "owned_by": "nanobot-relay",
                },
                {
                    "id": "nanobot-relay",
                    "object": "model",
                    "created": 0,
                    "owned_by": "nanobot-relay",
                },
            ],
        }
    )


async def handle_chat_completions(request: web.Request) -> web.StreamResponse:
    runtime: RelayRuntime = request.app["relay_runtime"]
    client = runtime.authenticate(request)
    if client is None:
        return _error_json(401, "Missing or invalid relay token", "authentication_error")
    try:
        body = await request.json()
    except Exception:
        return _error_json(400, "Invalid JSON body")
    if not isinstance(body, dict):
        return _error_json(400, "JSON body must be an object")

    try:
        messages = _request_messages(body)
    except ValueError as exc:
        return _error_json(400, str(exc))

    try:
        snapshot = runtime.snapshot_for(client.model_preset)
    except Exception as exc:
        logger.exception("Relay model resolution failed for {}", client.client_id)
        return _error_json(500, f"Relay model resolution failed: {exc}", "server_error")

    requested_model = body.get("model")
    if requested_model is not None and not isinstance(requested_model, str):
        return _error_json(400, "model must be a string")
    if not _requested_model_allowed(requested_model, client=client, model=snapshot.model):
        return _error_json(
            400,
            f"Relay client '{client.client_id}' is bound to model preset "
            f"'{client.model_preset}' ({snapshot.model}); arbitrary model selection is not allowed.",
        )

    tools = body.get("tools")
    if tools is not None and not isinstance(tools, list):
        return _error_json(400, "tools must be a list when provided")
    tool_choice = body.get("tool_choice")
    max_tokens = body.get("max_tokens")
    temperature = body.get("temperature")
    reasoning_effort = body.get("reasoning_effort")
    stream = bool(body.get("stream", False))
    timeout = float(request.app["relay_timeout"])

    provider = snapshot.provider
    retry_mode = runtime.config.agents.defaults.provider_retry_mode

    if stream:
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)
        chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        async def on_content_delta(text: str) -> None:
            if text:
                await queue.put(_sse(_content_chunk(text, chunk_id=chunk_id, model=snapshot.model)))

        async def on_tool_call_delta(delta: dict[str, Any]) -> None:
            await queue.put(_sse(_tool_delta_chunk(delta, chunk_id=chunk_id, model=snapshot.model)))

        async def run_provider() -> None:
            try:
                result = await asyncio.wait_for(
                    provider.chat_stream_with_retry(
                        messages=messages,
                        tools=tools,
                        model=snapshot.model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        reasoning_effort=reasoning_effort,
                        tool_choice=tool_choice,
                        retry_mode=retry_mode,
                        on_content_delta=on_content_delta,
                        on_tool_call_delta=on_tool_call_delta,
                    ),
                    timeout=timeout,
                )
                await queue.put(_sse(_final_chunk(
                    chunk_id=chunk_id,
                    model=snapshot.model,
                    finish_reason=result.finish_reason,
                )))
                await queue.put(_sse("[DONE]"))
            except Exception:
                logger.exception("Relay streaming request failed for {}", client.client_id)
                await queue.put(_sse({
                    "error": {
                        "message": "Relay streaming request failed",
                        "type": "server_error",
                    }
                }))
            finally:
                await queue.put(None)

        task = asyncio.create_task(run_provider())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                await response.write(item)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        return response

    try:
        result = await asyncio.wait_for(
            provider.chat_with_retry(
                messages=messages,
                tools=tools,
                model=snapshot.model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
                retry_mode=retry_mode,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return _error_json(504, f"Relay request timed out after {timeout}s")
    except Exception:
        logger.exception("Relay request failed for {}", client.client_id)
        return _error_json(500, "Relay request failed", "server_error")
    return web.json_response(_chat_response(result, model=snapshot.model))


def create_relay_app(config: Config, store: SkillStore) -> web.Application:
    app = web.Application(client_max_size=config.relay.max_request_bytes)
    app["relay_runtime"] = RelayRuntime(config, store)
    app["relay_timeout"] = config.relay.timeout
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    return app


async def run_relay_server(
    config: Config,
    store: SkillStore,
    *,
    on_started: Callable[[str, int], None] | None = None,
) -> None:
    from aiohttp import web

    app = create_relay_app(config, store)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.relay.host, config.relay.port)
    await site.start()
    if on_started:
        on_started(config.relay.host, config.relay.port)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
