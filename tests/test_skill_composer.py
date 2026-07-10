from __future__ import annotations

from typing import Any

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.skill_composer import compose_skill_draft_with_llm


class FakeComposerProvider(LLMProvider):
    def __init__(self, content: str) -> None:
        super().__init__()
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return "fake/composer"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "reasoning_effort": reasoning_effort,
                "tool_choice": tool_choice,
            }
        )
        return LLMResponse(content=self.content)


@pytest.mark.asyncio
async def test_compose_skill_draft_with_llm_returns_safe_content(tmp_path):
    provider = FakeComposerProvider(
        """
        {
          "method": "# Method\\n1. Inspect the request.\\n2. Return findings.",
          "review": {
            "summary": "Looks useful.",
            "security_risk_level": "low",
            "red_flags": []
          },
          "routing_cases": [
            {"query": "review renewal notes", "expected": "renewal-review"},
            {"query": "summarize a movie", "expected": "none"}
          ]
        }
        """
    )

    content = await compose_skill_draft_with_llm(
        provider,
        model="fake/composer",
        values={
            "name": "renewal-review",
            "description": "Review renewal notes.",
            "trigger": "review renewal notes",
        },
        workspace=tmp_path,
    )

    assert "# Method" in content.method
    assert content.review["status"] == "ready"
    assert content.review["summary"] == "Looks useful."
    assert content.routing_cases == [
        {"query": "review renewal notes", "expected": "renewal-review"},
        {"query": "summarize a movie", "expected": "none"},
    ]
    assert provider.calls[0]["model"] == "fake/composer"
    assert provider.calls[0]["temperature"] == 0.2
