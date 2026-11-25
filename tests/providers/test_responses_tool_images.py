"""A tool-returned image reaches the model as an image, not as base64 text (review B32)."""

from __future__ import annotations

import json

from nanobot.providers.openai_responses import convert_messages
from nanobot.utils.helpers import build_image_content_blocks


def _conversation(blocks, extra_tool=None):
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "look"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1|fc_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "call_2|fc_2", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_1|fc_1", "name": "read_file", "content": blocks},
        {"role": "tool", "tool_call_id": "call_2|fc_2", "name": "grep", "content": extra_tool or "no matches"},
    ]
    return convert_messages(messages)[1]


def test_image_is_sent_as_an_input_image_and_the_output_stays_small() -> None:
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300_000
    blocks = build_image_content_blocks(raw, "image/png", "/ws/photo.png", "(Image file: photo.png)")
    items = _conversation(blocks)

    outputs = [item for item in items if item.get("type") == "function_call_output"]
    assert len(outputs) == 2
    for item in outputs:
        assert "data:image" not in item["output"] and "/ws/photo.png" not in item["output"]
        assert len(item["output"]) < 200
    assert "photo.png" in outputs[0]["output"]

    # Both tool outputs come first; the picture follows as a user message.
    kinds = [item.get("type") or item.get("role") for item in items]
    assert kinds[-3:] == ["function_call_output", "function_call_output", "user"]
    images = [part for part in items[-1]["content"] if part["type"] == "input_image"]
    assert len(images) == 1 and images[0]["image_url"].startswith("data:image/png;base64,")
    assert "_meta" not in json.dumps(items[-1])


def test_text_only_tool_content_is_unchanged() -> None:
    items = _conversation([{"type": "text", "text": "plain"}], extra_tool="ok")
    outputs = [item["output"] for item in items if item.get("type") == "function_call_output"]
    assert outputs[1] == "ok"
    assert json.loads(outputs[0]) == [{"type": "text", "text": "plain"}]
    assert items[-1].get("type") == "function_call_output"  # no extra user message
