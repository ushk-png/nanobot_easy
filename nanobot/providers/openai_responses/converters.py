"""Convert Chat Completions messages/tools to Responses API format."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from nanobot.providers.base import tool_arguments_json_for_replay


def convert_messages(
    messages: list[dict[str, Any]],
    *,
    reasoning_scope: tuple[str, str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Convert Chat Completions messages to Responses API input items.

    Returns ``(system_prompt, input_items)`` where *system_prompt* is extracted
    from any ``system`` role message and *input_items* is the Responses API
    ``input`` array. Opaque reasoning is replayed only for an explicitly matching
    ``reasoning_scope`` (provider, requested model).
    """
    system_prompt = ""
    input_items: list[dict[str, Any]] = []
    used_item_ids: set[str] = set()
    # Images a tool returned travel as a user message right after the run of tool outputs: a
    # function_call_output is text, and a JSON-dumped data URL would be read as ~100k tokens of
    # text instead of as a picture.
    tool_images: list[dict[str, Any]] = []

    def flush_tool_images() -> None:
        if tool_images:
            input_items.append({
                "role": "user",
                "content": [{"type": "input_text", "text": "Image(s) returned by the tool call(s) above:"},
                            *tool_images],
            })
            tool_images.clear()

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")
        if role != "tool":
            flush_tool_images()

        if role == "system":
            system_prompt = content if isinstance(content, str) else ""
            continue

        if role == "user":
            input_items.append(convert_user_message(content))
            continue

        if role == "assistant":
            state = msg.get("provider_state")
            if (reasoning_scope and isinstance(state, dict)
                    and (state.get("provider"), state.get("model")) == reasoning_scope):
                # Encrypted reasoning is opaque: preserve the full native item and ID.
                # Other providers/models must never receive this replay state.
                for item in state.get("reasoning_items") or []:
                    if not isinstance(item, dict) or item.get("type") != "reasoning":
                        continue
                    if not item.get("encrypted_content"):
                        continue
                    item_id = item.get("id")
                    if item_id and item_id in used_item_ids:
                        continue
                    if item_id:
                        used_item_ids.add(item_id)
                    input_items.append(deepcopy(item))
            if isinstance(content, str) and content:
                message_id = _unique_item_id(f"msg_{idx}", used_item_ids)
                input_items.append({
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                    "status": "completed", "id": message_id,
                })
            for tool_call in msg.get("tool_calls", []) or []:
                fn = tool_call.get("function") or {}
                call_id, item_id = split_tool_call_id(tool_call.get("id"))
                response_item_id = _unique_item_id(item_id or f"fc_{idx}", used_item_ids)
                input_items.append({
                    "type": "function_call",
                    "id": response_item_id,
                    "call_id": call_id or f"call_{idx}",
                    "name": fn.get("name"),
                    "arguments": tool_arguments_json_for_replay(fn.get("arguments")),
                })
            continue

        if role == "tool":
            call_id, _ = split_tool_call_id(msg.get("tool_call_id"))
            if isinstance(content, str):
                output_text = content
            elif isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "image_url" for part in content
            ):
                texts = [str(part.get("text", "")) for part in content
                         if isinstance(part, dict) and part.get("type") == "text"]
                images = [
                    {"type": "input_image", "image_url": url, "detail": "auto"}
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "image_url"
                    and (url := (part.get("image_url") or {}).get("url"))
                ]
                tool_images.extend(images)
                output_text = "\n".join(t for t in texts if t) or "(image returned; shown below)"
                if images:
                    output_text += "\n(The image is attached in the next message.)"
            else:
                output_text = json.dumps(content, ensure_ascii=False)
            input_items.append({"type": "function_call_output", "call_id": call_id, "output": output_text})

    flush_tool_images()
    return system_prompt, input_items


def convert_user_message(content: Any) -> dict[str, Any]:
    """Convert a user message's content to Responses API format.

    Handles plain strings, ``text`` blocks -> ``input_text``, and
    ``image_url`` blocks -> ``input_image``.
    """
    if isinstance(content, str):
        return {"role": "user", "content": [{"type": "input_text", "text": content}]}
    if isinstance(content, list):
        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                converted.append({"type": "input_text", "text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url")
                if url:
                    converted.append({"type": "input_image", "image_url": url, "detail": "auto"})
        if converted:
            return {"role": "user", "content": converted}
    return {"role": "user", "content": [{"type": "input_text", "text": ""}]}


def convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function-calling tool schema to Responses API flat format."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        converted.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or "",
            "parameters": params if isinstance(params, dict) else {},
        })
    return converted


def _unique_item_id(item_id: str, used: set[str]) -> str:
    """Return a Responses input item id that is unique within one request."""
    if item_id not in used:
        used.add(item_id)
        return item_id

    suffix = 2
    while f"{item_id}_{suffix}" in used:
        suffix += 1
    unique = f"{item_id}_{suffix}"
    used.add(unique)
    return unique


def split_tool_call_id(tool_call_id: Any) -> tuple[str, str | None]:
    """Split a compound ``call_id|item_id`` string.

    Returns ``(call_id, item_id)`` where *item_id* may be ``None``.
    """
    if isinstance(tool_call_id, str) and tool_call_id:
        if "|" in tool_call_id:
            call_id, item_id = tool_call_id.split("|", 1)
            return call_id, item_id or None
        return tool_call_id, None
    return "call_0", None
