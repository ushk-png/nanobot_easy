"""Parse Responses API SSE streams and SDK response objects."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, AsyncGenerator

import httpx
from loguru import logger

from nanobot.providers.base import LLMResponse, ToolCallRequest, parse_tool_arguments
from nanobot.providers.call_observation import (
    observe,
    observe_stream_event,
    schedule_cleanup,
    stream_event_log,
)

FINISH_REASON_MAP = {
    "completed": "stop",
    "incomplete": "length",
    "failed": "error",
    "cancelled": "error",
}


def map_finish_reason(status: str | None) -> str:
    """Map a Responses API status string to a Chat-Completions-style finish_reason."""
    return FINISH_REASON_MAP.get(status or "completed", "stop")


def _usage_from_response_obj(response: Any) -> dict[str, int]:
    usage_raw = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    if not usage_raw:
        return {}
    if not isinstance(usage_raw, dict):
        dump = getattr(usage_raw, "model_dump", None)
        usage_raw = dump() if callable(dump) else vars(usage_raw)
    prompt_tokens = int(usage_raw.get("input_tokens") or usage_raw.get("prompt_tokens") or 0)
    completion_tokens = int(
        usage_raw.get("output_tokens") or usage_raw.get("completion_tokens") or 0
    )
    total_tokens = int(usage_raw.get("total_tokens") or prompt_tokens + completion_tokens)
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    # Responses API detail fields (OpenAI and xAI share these names).
    output_details = usage_raw.get("output_tokens_details")
    if isinstance(output_details, dict) and output_details.get("reasoning_tokens") is not None:
        usage["reasoning_tokens"] = int(output_details.get("reasoning_tokens") or 0)
    input_details = usage_raw.get("input_tokens_details")
    if isinstance(input_details, dict) and input_details.get("cached_tokens") is not None:
        usage["cached_tokens"] = int(input_details.get("cached_tokens") or 0)
    if usage_raw.get("num_server_side_tools_used") is not None:
        usage["server_side_tools_used"] = int(usage_raw.get("num_server_side_tools_used") or 0)
    return usage


def _parse_tool_call_arguments(args_raw: Any, name: str | None) -> Any:
    parsed = parse_tool_arguments(args_raw)
    if parsed == args_raw and isinstance(args_raw, str) and args_raw.strip():
        logger.warning(
            "Failed to parse tool call arguments for '{}': {}",
            name,
            f"[omitted; {len(args_raw)} characters]",
        )
    return parsed


def _tool_arguments_source(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return "{}"


async def iter_sse(response: httpx.Response) -> AsyncGenerator[dict[str, Any], None]:
    """Yield parsed JSON events from a Responses API SSE stream."""
    buffer: list[str] = []

    def _flush() -> dict[str, Any] | None:
        data_lines = [line[5:].strip() for line in buffer if line.startswith("data:")]
        buffer.clear()
        if not data_lines:
            return None
        data = "\n".join(data_lines).strip()
        if not data or data == "[DONE]":
            return None
        try:
            return json.loads(data)
        except Exception:
            logger.warning("Failed to parse SSE event JSON ({} characters)", len(data))
            return None

    async for line in response.aiter_lines():
        if line == "":
            if buffer:
                event = _flush()
                if event is not None:
                    yield event
            continue
        buffer.append(line)

    # Flush any remaining buffer at EOF (#10)
    if buffer:
        event = _flush()
        if event is not None:
            yield event


async def consume_sse(
    response: httpx.Response,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolCallRequest], str]:
    """Consume a Responses API SSE stream into ``(content, tool_calls, finish_reason)``."""
    content, tool_calls, finish_reason, _, _ = await consume_sse_with_reasoning(
        response,
        on_content_delta=on_content_delta,
        on_tool_call_delta=on_tool_call_delta,
    )
    return content, tool_calls, finish_reason


async def consume_sse_with_reasoning(
    response: httpx.Response,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolCallRequest], str, dict[str, int], str | None]:
    """Consume a Responses API SSE stream, including visible reasoning summaries."""
    return await _consume_response_events(iter_sse(response), on_content_delta, on_tool_call_delta, on_reasoning_delta)


async def _consume_response_events(events, on_content_delta=None, on_tool_call_delta=None, on_reasoning_delta=None):
    # Diagnostics only: one content-free stream_event_log milestone per stream,
    # emitted on every exit (including a deadline cancellation). Never affects results.
    log = stream_event_log()
    if log is None:
        return await _consume_events(events, on_content_delta, on_tool_call_delta, on_reasoning_delta)
    status = "error"
    try:
        result = await _consume_events(events, on_content_delta, on_tool_call_delta, on_reasoning_delta, log)
        status = "completed"
        return result
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except RuntimeError as exc:
        status = "eof" if "missing_terminal_event" in str(exc) else "failed"
        raise
    finally:
        log.emit(status)


async def _consume_events(events, on_content_delta=None, on_tool_call_delta=None, on_reasoning_delta=None,
                          log=None):
    content = ""
    tool_calls = []
    tool_call_buffers = {}
    tool_call_args_emitted = set()
    reasoning_content = None
    streamed_reasoning = False
    seen_events = 0
    first_output = False
    iterator = aiter(events)
    while True:
        observe("stream_receive_wait_started", event_sequence=seen_events + 1)
        try:
            event = await anext(iterator)
        except StopAsyncIteration:
            observe("stream_receive_wait_finished", event_sequence=seen_events + 1, status="eof")
            break
        except asyncio.CancelledError:
            observe("stream_receive_wait_finished", event_sequence=seen_events + 1, status="cancelled")
            raise
        except Exception:
            observe("stream_receive_wait_finished", event_sequence=seen_events + 1, status="error")
            raise
        observe("stream_receive_wait_finished", event_sequence=seen_events + 1, status="received")
        seen_events += 1
        if log is not None:
            log.record(event)
        with observe_stream_event(event, seen_events):
            if seen_events == 1:
                observe("first_event", event_type=event.get("type"), bytes=len(json.dumps(event).encode()))
            if not first_output and (
                    event.get("type") in {"response.output_text.delta", "response.function_call_arguments.delta",
                                          "response.output_item.done"}
                    or (event.get("type") == "response.completed" and (event.get("response") or {}).get("output"))):
                first_output = True
                observe("first_output", event_type=event.get("type"), bytes=len(json.dumps(event).encode()))
            event_type = event.get("type")
            if event_type == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    call_id = item.get("call_id")
                    if not call_id:
                        continue
                    arguments = item.get("arguments")
                    tool_call_buffers[call_id] = {
                        "id": item.get("id") or "fc_0",
                        "name": item.get("name"),
                        "arguments": "" if arguments is None else arguments,
                    }
                    if on_tool_call_delta:
                        await on_tool_call_delta({
                            "call_id": str(call_id),
                            "name": str(item.get("name") or ""),
                            "arguments_delta": "",
                        })
            elif event_type == "response.output_text.delta":
                delta_text = event.get("delta") or ""
                content += delta_text
                if on_content_delta and delta_text:
                    await on_content_delta(delta_text)
            elif event_type == "response.reasoning_summary_text.delta":
                delta_text = event.get("delta") or ""
                if delta_text:
                    reasoning_content = (reasoning_content or "") + delta_text
                    streamed_reasoning = True
                    if on_reasoning_delta:
                        await on_reasoning_delta(delta_text)
            elif event_type == "response.reasoning_summary_text.done":
                text = event.get("text") or ""
                if text and not streamed_reasoning and not reasoning_content:
                    reasoning_content = text
                    if on_reasoning_delta:
                        await on_reasoning_delta(text)
            elif event_type == "response.reasoning_summary_part.done":
                part = event.get("part") or {}
                text = part.get("text") if part.get("type") == "summary_text" else None
                if text and not streamed_reasoning and not reasoning_content:
                    reasoning_content = text
                    if on_reasoning_delta:
                        await on_reasoning_delta(text)
            elif event_type == "response.function_call_arguments.delta":
                call_id = event.get("call_id")
                if call_id and call_id in tool_call_buffers:
                    delta = event.get("delta") or ""
                    current = tool_call_buffers[call_id].get("arguments")
                    if not isinstance(current, str):
                        current = ""
                    tool_call_buffers[call_id]["arguments"] = current + delta
                    if on_tool_call_delta and delta:
                        await on_tool_call_delta({
                            "call_id": str(call_id),
                            "name": str(tool_call_buffers[call_id].get("name") or ""),
                            "arguments_delta": str(delta),
                        })
            elif event_type == "response.function_call_arguments.done":
                call_id = event.get("call_id")
                if call_id and call_id in tool_call_buffers:
                    arguments = event.get("arguments")
                    tool_call_buffers[call_id]["arguments"] = arguments
                    if on_tool_call_delta:
                        tool_call_args_emitted.add(str(call_id))
                        await on_tool_call_delta({
                            "call_id": str(call_id),
                            "name": str(tool_call_buffers[call_id].get("name") or ""),
                            "arguments": "" if arguments is None else str(arguments),
                        })
            elif event_type == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    call_id = item.get("call_id")
                    if not call_id:
                        continue
                    buf = tool_call_buffers.get(call_id) or {}
                    args_raw = _tool_arguments_source(item.get("arguments"), buf.get("arguments"))
                    if on_tool_call_delta and str(call_id) not in tool_call_args_emitted:
                        tool_call_args_emitted.add(str(call_id))
                        await on_tool_call_delta({
                            "call_id": str(call_id),
                            "name": str(buf.get("name") or item.get("name") or ""),
                            "arguments": str(args_raw),
                        })
                    args = _parse_tool_call_arguments(
                        args_raw,
                        item.get("name") or buf.get("name"),
                    )
                    tool_calls = [call for call in tool_calls if call.id.split("|", 1)[0] != call_id]
                    tool_calls.append(
                        ToolCallRequest(
                            id=f"{call_id}|{item.get('id') or buf.get('id') or 'fc_0'}",
                            name=item.get("name") or buf.get("name") or "",
                            arguments=args,
                        )
                    )
                elif item.get("type") == "reasoning" and not reasoning_content:
                    summary = _extract_reasoning_summary_from_output([item])
                    if summary:
                        reasoning_content = summary
                        if on_reasoning_delta:
                            await on_reasoning_delta(summary)
            elif event_type in {"response.completed", "response.incomplete", "response.cancelled"}:
                response_obj = event.get("response") or {}
                status = response_obj.get("status") or event_type.split(".", 1)[1]
                observe("terminal_event", event_type=event_type, status=status)
                if status not in FINISH_REASON_MAP:
                    raise RuntimeError("Response failed: invalid_terminal_status")
                if status in {"failed", "cancelled"}:
                    raise RuntimeError("Response failed: " + status)
                finish_reason = map_finish_reason(status)
                usage = _usage_from_response_obj(response_obj)
                # A nonempty final snapshot is authoritative. Never parse a partial
                # arguments buffer instead, or append final output to delta output.
                if response_obj.get("output"):
                    final = parse_response_output(response_obj)
                    content, tool_calls = final.content or "", final.tool_calls
                    reasoning_content = final.reasoning_content or reasoning_content
                elif not reasoning_content:
                    reasoning_content = _extract_reasoning_summary_from_output(response_obj.get("output") or []) or None
                if not response_obj.get("output") and any(
                        cid not in {c.id.split("|", 1)[0] for c in tool_calls} for cid in tool_call_buffers):
                    raise RuntimeError("Response failed: incomplete_tool_result")
                observe("result_extracted", status=finish_reason, count=len(tool_calls),
                        bytes=len(content.encode()) + sum(len(str(c.arguments).encode()) for c in tool_calls),
                        result_hash=hashlib.sha256(json.dumps(
                            {"content": content, "tools": [c.arguments for c in tool_calls]},
                            sort_keys=True).encode()).hexdigest())
                return content, tool_calls, finish_reason, usage, reasoning_content
            elif event_type in {"error", "response.failed"}:
                observe("terminal_event", event_type=event_type, status="failed")
                detail = event.get("error") or event.get("message") or "provider_error"
                if not isinstance(detail, str) or not detail.replace("_", "").isalnum() or len(detail) > 80:
                    detail = "provider_error"
                raise RuntimeError("Response failed: " + detail)

    observe("stream_eof", status="missing_terminal_event", count=seen_events)
    raise RuntimeError("Response failed: missing_terminal_event")


def _extract_reasoning_summary_from_output(output: Any) -> str | None:
    parts: list[str] = []
    for item in output or []:
        if not isinstance(item, dict):
            dump = getattr(item, "model_dump", None)
            item = dump() if callable(dump) else vars(item)
        if item.get("type") != "reasoning":
            continue
        for summary in item.get("summary") or []:
            if not isinstance(summary, dict):
                dump = getattr(summary, "model_dump", None)
                summary = dump() if callable(dump) else vars(summary)
            if summary.get("type") == "summary_text" and summary.get("text"):
                parts.append(summary["text"])
    return "".join(parts) or None


def parse_response_output(response: Any) -> LLMResponse:
    """Parse an SDK ``Response`` object into an ``LLMResponse``."""
    if not isinstance(response, dict):
        dump = getattr(response, "model_dump", None)
        response = dump() if callable(dump) else vars(response)

    output = response.get("output") or []
    content_parts: list[str] = []
    tool_calls: list[ToolCallRequest] = []
    reasoning_content: str | None = None

    for item in output:
        if not isinstance(item, dict):
            dump = getattr(item, "model_dump", None)
            item = dump() if callable(dump) else vars(item)

        item_type = item.get("type")
        if item_type == "message":
            for block in item.get("content") or []:
                if not isinstance(block, dict):
                    dump = getattr(block, "model_dump", None)
                    block = dump() if callable(dump) else vars(block)
                if block.get("type") == "output_text":
                    content_parts.append(block.get("text") or "")
        elif item_type == "reasoning":
            for s in item.get("summary") or []:
                if not isinstance(s, dict):
                    dump = getattr(s, "model_dump", None)
                    s = dump() if callable(dump) else vars(s)
                if s.get("type") == "summary_text" and s.get("text"):
                    reasoning_content = (reasoning_content or "") + s["text"]
        elif item_type == "function_call":
            call_id = item.get("call_id") or ""
            item_id = item.get("id") or "fc_0"
            args_raw = _tool_arguments_source(item.get("arguments"))
            args = _parse_tool_call_arguments(args_raw, item.get("name"))
            tool_calls.append(ToolCallRequest(
                id=f"{call_id}|{item_id}",
                name=item.get("name") or "",
                arguments=args,
            ))

    usage = _usage_from_response_obj(response)

    status = response.get("status")
    finish_reason = map_finish_reason(status)

    return LLMResponse(
        content="".join(content_parts) or None,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        reasoning_content=reasoning_content if isinstance(reasoning_content, str) else None,
    )


def _sdk_event_dict(value: Any) -> Any:
    """Normalize SDK event objects without dropping final response snapshots."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _sdk_event_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sdk_event_dict(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        data = dump()
        if isinstance(data, dict):
            return _sdk_event_dict(data)
    # Supports lightweight SDK-like objects and existing integration adapters.
    keys = ("type", "response", "item", "delta", "arguments", "call_id", "name", "id", "status",
            "output", "content", "text", "summary", "usage", "input_tokens", "output_tokens",
            "total_tokens", "input_tokens_details", "output_tokens_details", "error", "message")
    attributes = vars(value)
    return {k: _sdk_event_dict(v) for k in keys if
            isinstance(v := attributes.get(k), (str, int, float, bool, dict, list, tuple))
            or (v is not None and k in {"response", "item", "usage"})}


async def consume_sdk_stream(
    stream: Any,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    *,
    close: Callable | None = None,
) -> tuple[str, list[ToolCallRequest], str, dict[str, int], str | None]:
    """Use the same terminal/extraction contract for SDK and raw SSE events."""
    async def events():
        async for event in stream:
            yield _sdk_event_dict(event)

    try:
        return await _consume_response_events(events(), on_content_delta, on_tool_call_delta)
    finally:
        closer = close or getattr(stream, "close", None) or getattr(stream, "aclose", None)
        if callable(closer):
            schedule_cleanup(closer)
