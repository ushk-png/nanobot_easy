"""Follow-ups resume a waiting case or update facts of a finished one (design 6.2, 7, 13)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from nanobot.reasoning.bridge import (
    _PROCEDURE_PROMPT,
    _REVIEW_PROMPT,
    ReasoningSessions,
    run_reasoning_procedure,
)
from tests.reasoning.test_stepwise_instrumentation import GOAL, STAGE_OUTPUTS

KEY = "telegram:1"


class ScriptProvider:
    """compose answers come from a list; stage answers from per-stage queues."""

    def __init__(self, compose: list[dict[str, Any]], stages: dict[str, list[dict[str, Any]]] | None = None):
        self.compose = list(compose)
        self.stages = {key: list(value) for key, value in (stages or {}).items()}
        self.calls: list[str] = []
        self.payloads: list[dict[str, Any]] = []

    async def chat_with_retry(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        body = json.loads(kwargs["messages"][1]["content"])
        self.payloads.append(body)
        if system == _PROCEDURE_PROMPT:
            self.calls.append("compose")
            content = self.compose.pop(0)
        elif system == _REVIEW_PROMPT:
            self.calls.append("review")
            content = {}
        else:
            stage = body["stage"]
            self.calls.append(stage)
            queue = self.stages.get(stage)
            content = queue.pop(0) if queue else STAGE_OUTPUTS[stage]
        return SimpleNamespace(
            content=json.dumps(content, ensure_ascii=False), usage={}, finish_reason="stop"
        )


def _run(provider: ScriptProvider, sessions: ReasoningSessions, request: str):
    return asyncio.run(
        run_reasoning_procedure(
            provider, model="m", request=request, sessions=sessions, session_key=KEY
        )
    )


REUSE = {"mode": "reuse", "procedure_id": "method-evaluation"}


def test_waiting_case_resumes_with_the_users_reply() -> None:
    sessions = ReasoningSessions()
    asking = {"goal": GOAL, "constraints": [], "questions": ["어느 일정이 고정인가요?"]}
    provider = ScriptProvider([REUSE, {"mode": "resume"}], {"S1": [asking]})

    first = _run(provider, sessions, "일정 충돌 조정 방법이 적합한가요?")
    assert first.status == "waiting_info"
    assert first.stop_reason == "어느 일정이 고정인가요?"
    provider.calls.clear()
    provider.payloads.clear()

    second = _run(provider, sessions, "오전 회의가 고정입니다.")

    assert second.status == "completed"
    assert second.id == first.id
    assert second.path == "resume"
    assert provider.calls == ["compose", "S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    s1_payload = provider.payloads[1]
    assert "[사용자 답변] 오전 회의가 고정입니다." in s1_payload["request"]
    assert provider.payloads[0]["previous_case"]["questions"] == ["어느 일정이 고정인가요?"]


def test_new_fact_reruns_only_the_dependent_steps() -> None:
    sessions = ReasoningSessions()
    update = {
        "mode": "update",
        "facts": [{"id": "f1", "proposition": "두 일정은 겹치지 않음", "status": "confirmed"}],
    }
    provider = ScriptProvider([REUSE, update])

    first = _run(provider, sessions, "일정 충돌 조정 방법이 적합한가요?")
    assert first.status == "completed"
    provider.calls.clear()

    second = _run(provider, sessions, "확인해 보니 두 일정은 겹치지 않아요.")

    assert second.status == "completed"
    assert second.path == "update"
    assert provider.calls == ["compose", "S4", "S5", "S6", "S7"]
    fact = next(item for item in second.facts if item.id == "f1")
    assert fact.proposition == "두 일정은 겹치지 않음"
    assert fact.source == "user"
    assert fact.version == 2


def test_resume_is_rejected_when_nothing_is_waiting() -> None:
    sessions = ReasoningSessions()
    provider = ScriptProvider([REUSE, {"mode": "resume"}, {"mode": "direct"}])

    _run(provider, sessions, "일정 충돌 조정 방법이 적합한가요?")
    second = _run(provider, sessions, "고마워")

    assert second.status == "direct"
    repair = provider.payloads[-1]["repair_defects"]
    assert repair[0].startswith("resume_invalid")


def test_update_requires_a_facts_step() -> None:
    from nanobot.reasoning.bridge import _follow_up_defects
    from nanobot.reasoning.dynamic import validate_dynamic_procedure
    from nanobot.reasoning.models import ReasoningCase

    procedure, _ = validate_dynamic_procedure(
        {
            "purpose": "후보를 고른다.",
            "steps": [
                {"id": "a", "question": "본다.", "outputs": ["x"]},
                {"id": "b", "role": "answer", "question": "답한다.", "inputs": ["a"], "outputs": ["answer"]},
            ],
        },
        request="후보를 골라 주세요",
    )
    assert procedure is not None
    case = ReasoningCase(
        id="c", request="r", procedure_id=procedure.id, procedure_version="3.1", status="completed"
    )

    defects, facts = _follow_up_defects(
        "update", {"facts": [{"proposition": "새 사실"}]}, (case, procedure)
    )

    assert defects and defects[0].startswith("update_invalid")
    assert facts == []


def test_sessions_expire() -> None:
    from nanobot.reasoning.models import ReasoningCase
    from nanobot.reasoning.registry import procedure

    sessions = ReasoningSessions(ttl_s=0)
    case = ReasoningCase(id="c", request="r", procedure_id="method-evaluation", procedure_version="3.1")
    sessions.put(KEY, case, procedure("method-evaluation"))

    assert sessions.get(KEY) is None


def test_agent_loop_keeps_one_session_store_across_turns(monkeypatch) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.reasoning.models import ReasoningCase

    seen: list[tuple[Any, Any]] = []

    async def _fake_run(*_args, **kwargs):
        seen.append((kwargs["sessions"], kwargs["session_key"]))
        return ReasoningCase(
            id="c", request="r", procedure_id="method-evaluation", procedure_version="3.1",
            status="waiting_info", stop_reason="질문",
        )

    class _Runtime:
        async def run_status_changed(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr("nanobot.agent.loop.run_reasoning_procedure", _fake_run)
    loop = AgentLoop.__new__(AgentLoop)
    loop.reasoning_effort = "none"
    loop.stepwise_enabled = True
    loop.workspace = None
    loop.provider = object()
    loop.model = "m"
    loop._runtime_events = lambda: _Runtime()

    def ctx():
        return SimpleNamespace(
            msg=SimpleNamespace(content="이 방법이 적합한지 평가해 주세요"),
            session_key=KEY, visible_run_started_at=None, initial_messages=[],
            final_content=None, tools_used=None, all_messages=None, stop_reason=None,
            had_injections=None, on_progress=None,
        )

    first, second = ctx(), ctx()
    asyncio.run(loop._state_run(first))
    asyncio.run(loop._state_run(second))

    assert seen[0][0] is seen[1][0]
    assert seen[0][1] == KEY
    assert first.final_content == "질문"
    assert first.stop_reason == "waiting_info"
