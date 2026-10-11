"""First-call routing: direct / reuse / new, and conversation context (design 4.1, 4.2)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nanobot.reasoning.bridge import (
    _PROCEDURE_PROMPT,
    _REVIEW_PROMPT,
    recent_conversation,
    run_reasoning_procedure,
)
from tests.reasoning.test_stepwise_instrumentation import STAGE_OUTPUTS

CONVERSATION = [
    {"role": "user", "content": "A와 B 중 내부망용 도구를 고르려고 해."},
    {"role": "assistant", "content": "조건을 알려 주세요."},
]


class RoutingProvider:
    def __init__(self, compose: list[dict[str, Any]]) -> None:
        self.compose = list(compose)
        self.kinds: list[str] = []
        self.bodies: dict[str, list[dict[str, Any]]] = {"compose": [], "review": [], "step": []}

    async def chat_with_retry(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        body = json.loads(kwargs["messages"][1]["content"])
        if system == _PROCEDURE_PROMPT:
            kind, content = "compose", self.compose.pop(0)
        elif system == _REVIEW_PROMPT:
            kind = "review"
            content = {
                "request_items": [{"item": "평가", "step_id": "D2"}],
                "answer_step_id": "D2",
                "defects": [],
            }
        else:
            kind = "step"
            stage = body.get("stage", "")
            content = STAGE_OUTPUTS.get(stage) or {
                key: "답입니다." if key == "answer" else "결과" for key in body.get("expected_outputs") or []
            }
        self.kinds.append(kind)
        self.bodies[kind].append(body)
        return SimpleNamespace(
            content=json.dumps(content, ensure_ascii=False),
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            finish_reason="stop",
        )


def _run(provider: RoutingProvider, tmp_path: Path, conversation=None):
    return asyncio.run(
        run_reasoning_procedure(
            provider,
            model="m",
            request="그럼 B가 맞는지 평가해 줘",
            workspace=tmp_path,
            conversation=conversation,
        )
    )


def _last_record(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "memory" / "dynamic-reasoning.jsonl").read_text().splitlines()[-1])


def test_direct_mode_runs_no_procedure(tmp_path: Path) -> None:
    provider = RoutingProvider([{"mode": "direct"}])

    case = _run(provider, tmp_path)

    assert case.status == "direct"
    assert provider.kinds == ["compose"]
    assert _last_record(tmp_path)["path"] == "direct"


def test_reuse_runs_the_approved_procedure_without_review(tmp_path: Path) -> None:
    provider = RoutingProvider([{"mode": "reuse", "procedure_id": "method-evaluation"}])

    case = _run(provider, tmp_path)

    assert case.status == "completed"
    assert case.procedure_id == "method-evaluation"
    assert "review" not in provider.kinds
    assert provider.kinds.count("compose") == 1
    assert _last_record(tmp_path)["path"] == "reuse"
    catalog = provider.bodies["compose"][0]["approved_procedures"]
    assert "method-evaluation" in {item["id"] for item in catalog}


def test_unknown_reuse_id_is_sent_back_for_repair(tmp_path: Path) -> None:
    provider = RoutingProvider(
        [
            {"mode": "reuse", "procedure_id": "made-up"},
            {"mode": "reuse", "procedure_id": "method-evaluation"},
        ]
    )

    case = _run(provider, tmp_path)

    assert case.procedure_id == "method-evaluation"
    assert provider.bodies["compose"][1]["repair_defects"][0].startswith("unknown_procedure")


def test_new_procedure_gets_conversation_in_compose_review_and_request_steps(tmp_path: Path) -> None:
    provider = RoutingProvider(
        [
            {
                "mode": "new",
                "purpose": "B가 내부망 조건에 맞는지 평가한다.",
                "steps": [
                    {"id": "D1", "question": "조건 충족을 본다.", "inputs": ["request"], "outputs": ["verdict"]},
                    {"id": "D2", "role": "answer", "question": "평가를 답한다.", "inputs": ["D1"], "outputs": ["answer"]},
                ],
            }
        ]
    )

    case = _run(provider, tmp_path, conversation=CONVERSATION)

    assert case.status == "completed"
    assert case.path == "new"
    assert provider.bodies["compose"][0]["conversation"] == CONVERSATION
    assert provider.bodies["review"][0]["conversation"] == CONVERSATION
    d1, d2 = provider.bodies["step"]
    assert d1["conversation"] == CONVERSATION
    assert "conversation" not in d2


def test_recent_conversation_drops_system_tools_and_current_message() -> None:
    messages = [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "첫 질문"},
        {"role": "assistant", "content": [{"type": "text", "text": "첫 답"}]},
        {"role": "tool", "content": "tool output"},
        {"role": "user", "content": "x" * 900},
        {"role": "assistant", "content": "둘째 답"},
        {"role": "user", "content": "현재 요청"},
    ]

    turns = recent_conversation(messages, max_turns=3)

    assert [item["role"] for item in turns] == ["assistant", "user", "assistant"]
    assert turns[0]["content"] == "첫 답"
    assert len(turns[1]["content"]) == 500
    assert all(item["content"] != "현재 요청" for item in turns)


def test_agent_loop_hands_direct_requests_to_the_normal_loop(monkeypatch) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.reasoning.models import ReasoningCase

    seen: dict[str, Any] = {}

    async def _fake_run(*_args, **kwargs):
        seen.update(kwargs)
        return ReasoningCase(
            id="d", request="r", procedure_id="direct", procedure_version="-", status="direct"
        )

    async def _normal(*_args, **_kwargs):
        seen["normal"] = True
        return "일반 답", ["web_search"], [], "completed", False

    class _Runtime:
        async def run_status_changed(self, *_args, **_kwargs) -> None:
            return None

    async def _continue(_ctx) -> None:
        return None

    monkeypatch.setattr("nanobot.agent.loop.run_reasoning_procedure", _fake_run)
    monkeypatch.setattr("nanobot.agent.loop.turn_continuation.maybe_continue_turn", _continue)
    monkeypatch.setattr(AgentLoop, "_run_agent_loop", _normal)
    loop = AgentLoop.__new__(AgentLoop)
    loop.reasoning_effort = "none"
    loop.stepwise_enabled = True
    loop.workspace = None
    loop.provider = object()
    loop.model = "m"
    loop._runtime_events = lambda: _Runtime()
    ctx = SimpleNamespace(
        msg=SimpleNamespace(content="보고서 검토해 줘", channel="telegram", chat_id="1", metadata={}),
        session_key="telegram:1",
        visible_run_started_at=None,
        initial_messages=[{"role": "user", "content": "이전"}, {"role": "assistant", "content": "답"}, {"role": "user", "content": "보고서 검토해 줘"}],
        on_progress=None,
        on_stream=None,
        on_stream_end=None,
        on_retry_wait=None,
        session=None,
        pending_queue=None,
        ephemeral=False,
        run_extra_hooks_for_ephemeral=False,
        hooks=[],
        tools=None,
        final_content=None,
        tools_used=None,
        all_messages=None,
        stop_reason=None,
        had_injections=None,
    )

    asyncio.run(loop._state_run(ctx))

    assert "procedure_id" not in seen  # keywords no longer pick a procedure
    assert seen["conversation"] == [{"role": "user", "content": "이전"}, {"role": "assistant", "content": "답"}]
    assert seen["normal"] is True
    assert ctx.final_content == "일반 답"
    assert ctx.tools_used == ["web_search"]
