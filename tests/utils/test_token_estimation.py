import json

import pytest

from nanobot.utils import helpers
from nanobot.utils.helpers import estimate_prompt_tokens, estimate_prompt_tokens_chain


class _NoCounterProvider:
    pass


class _BrokenCounterProvider:
    def estimate_prompt_tokens(self, messages, tools=None, model=None):
        raise RuntimeError("counter unavailable")


@pytest.mark.requires_tiktoken_encoding
def test_estimate_prompt_tokens_chain_falls_back_without_provider_counter() -> None:
    tokens, source = estimate_prompt_tokens_chain(
        _NoCounterProvider(),
        "test-model",
        [{"role": "user", "content": "hello"}],
    )

    assert tokens > 0
    assert source == "tiktoken"


@pytest.mark.requires_tiktoken_encoding
def test_estimate_prompt_tokens_chain_falls_back_when_provider_counter_fails() -> None:
    tokens, source = estimate_prompt_tokens_chain(
        _BrokenCounterProvider(),
        "test-model",
        [{"role": "user", "content": "hello"}],
    )

    assert tokens > 0
    assert source == "tiktoken"


def test_estimate_prompt_tokens_caches_tools_encoding(monkeypatch) -> None:
    helpers._get_token_encoding.cache_clear()
    helpers._TOOLS_TOKEN_CACHE.clear()

    class FakeEncoding:
        def __init__(self) -> None:
            self.encoded: list[str] = []

        def encode(self, text: str) -> list[int]:
            self.encoded.append(text)
            return list(range(max(1, len(text) // 4)))

    fake_encoding = FakeEncoding()
    get_encoding_calls = 0

    def fake_get_encoding(name: str) -> FakeEncoding:
        nonlocal get_encoding_calls
        assert name == "cl100k_base"
        get_encoding_calls += 1
        return fake_encoding

    monkeypatch.setattr(helpers.tiktoken, "get_encoding", fake_get_encoding)
    tools = [{"type": "function", "function": {"name": "demo", "description": "cached"}}]
    messages = [{"role": "user", "content": "hello"}]

    first = estimate_prompt_tokens(messages, tools)
    second = estimate_prompt_tokens(messages, tools)

    assert first == second
    assert get_encoding_calls == 1
    rendered_tools = "\n" + json.dumps(tools, ensure_ascii=False)
    assert fake_encoding.encoded.count(rendered_tools) == 1


def test_estimate_prompt_tokens_recomputes_when_tool_items_change(monkeypatch) -> None:
    helpers._get_token_encoding.cache_clear()
    helpers._TOOLS_TOKEN_CACHE.clear()

    class FakeEncoding:
        def __init__(self) -> None:
            self.encoded: list[str] = []

        def encode(self, text: str) -> list[int]:
            self.encoded.append(text)
            return list(range(max(1, len(text) // 4)))

    fake_encoding = FakeEncoding()
    monkeypatch.setattr(helpers.tiktoken, "get_encoding", lambda _name: fake_encoding)

    tools = [{"type": "function", "function": {"name": "before"}}]
    messages = [{"role": "user", "content": "hello"}]
    estimate_prompt_tokens(messages, tools)

    tools[0] = {"type": "function", "function": {"name": "after"}}
    estimate_prompt_tokens(messages, tools)

    before_tools = "\n" + json.dumps(
        [{"type": "function", "function": {"name": "before"}}], ensure_ascii=False
    )
    after_tools = "\n" + json.dumps(tools, ensure_ascii=False)
    assert before_tools in fake_encoding.encoded
    assert after_tools in fake_encoding.encoded


# ---- item 9: an unavailable encoding must not turn the prompt estimate into 0 -------------------


def _break_encoding(monkeypatch) -> None:
    def _offline(name: str):
        raise ConnectionError("offline: no cached encoding")

    helpers._get_token_encoding.cache_clear()
    helpers._TOOLS_TOKEN_CACHE.clear()
    monkeypatch.setattr(helpers.tiktoken, "get_encoding", _offline)
    monkeypatch.setattr(helpers, "_TOKEN_FALLBACK_WARNED", False, raising=False)


_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant. " * 20},
    {"role": "user", "content": "hello there, please summarize the report"},
]


def test_prompt_estimate_without_encoding_is_a_positive_char_estimate(monkeypatch) -> None:
    _break_encoding(monkeypatch)
    try:
        tokens = estimate_prompt_tokens(_MESSAGES)
        text_chars = sum(len(m["content"]) for m in _MESSAGES)
        assert tokens > 0
        assert tokens >= text_chars // 4
        tools = [{"type": "function", "function": {"name": "demo", "description": "x" * 400}}]
        assert estimate_prompt_tokens(_MESSAGES, tools) >= tokens + 100
    finally:
        helpers._get_token_encoding.cache_clear()


def test_prompt_estimate_chain_without_encoding_is_not_none(monkeypatch) -> None:
    _break_encoding(monkeypatch)
    try:
        tokens, source = estimate_prompt_tokens_chain(_NoCounterProvider(), "m", _MESSAGES)
        assert (tokens, source) != (0, "none")
        assert tokens > 0
        assert source != "tiktoken"  # the label must not claim a real tokenizer count
    finally:
        helpers._get_token_encoding.cache_clear()


def test_prompt_estimate_fallback_warns_only_once(monkeypatch) -> None:
    from loguru import logger

    _break_encoding(monkeypatch)
    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING")
    try:
        for _ in range(5):
            estimate_prompt_tokens(_MESSAGES)
            estimate_prompt_tokens_chain(_NoCounterProvider(), "m", _MESSAGES)
    finally:
        logger.remove(sink)
        helpers._get_token_encoding.cache_clear()
    warnings = [m for m in seen if "token" in m.lower() and "estimate" in m.lower()]
    assert len(warnings) == 1
    assert "hello there" not in warnings[0]


def test_char_estimate_counts_korean_and_japanese_near_one_token_per_character(monkeypatch) -> None:
    """Measured against cl100k_base: ko 1762, ja 1240 tokens for these texts (ratio 0.97-1.05)."""
    ko = "수정 한도를 올린 뒤 확인하면 지급으로 기록되는 문제가 남아 있습니다. 업로드한 SQL을 그대로 실행합니다. " * 40
    ja = "これはテストの文章です。日本語の例文を作成して、意味を確認してください。" * 40
    en = "The quick brown fox jumps over the lazy dog while the agent reads the memory file. " * 40
    _break_encoding(monkeypatch)
    try:
        for text, real in ((ko, 1762), (ja, 1240)):
            estimate = estimate_prompt_tokens([{"role": "user", "content": text}]) - 4
            assert 0.9 * real <= estimate <= 1.15 * real, (estimate, real)
            assert helpers.estimate_message_tokens({"role": "user", "content": text}) - 8 >= 0.9 * real
        # English keeps the ~4 chars/token rule.
        assert helpers._char_token_estimate(en) == len(en) // 4
    finally:
        helpers._get_token_encoding.cache_clear()
