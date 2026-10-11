"""LLM adapter for workflow steps.

The adapter keeps provider calls provider-agnostic and validates structured JSON
responses at runtime. It deliberately does not use the external LLM relay.
"""
from __future__ import annotations

import json
from typing import Any

import json_repair

from nanobot.providers.base import LLMProvider


class WorkflowLLMError(RuntimeError):
    pass


class WorkflowLLMAdapter:
    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def call_json(
        self,
        *,
        system: str,
        payload: dict[str, Any],
        max_tokens: int = 2048,
        temperature: float = 0.0,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        if reasoning_effort is None:
            # Carry the configured setting; chat() alone would drop it.
            generation = getattr(self.provider, "generation", None)
            reasoning_effort = getattr(generation, "reasoning_effort", None)
        response = await self.provider.chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            tools=None,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
        if response.finish_reason == "error":
            raise WorkflowLLMError(response.content or "workflow llm call failed")
        text = response.content or ""
        try:
            parsed = json.loads(text)
        except Exception:
            try:
                parsed = json_repair.loads(text)
            except Exception as exc:
                raise WorkflowLLMError(f"workflow llm returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise WorkflowLLMError("workflow llm JSON response must be an object")
        return parsed
