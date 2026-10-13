"""Opt-in premature final regression tests, with no real side effects."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.hook import AgentHook
from nanobot.agent.premature_final import CORRECTION, INCOMPLETE, is_premature_final
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class Tools:
    execute = AsyncMock(return_value="recorded")

    def get_definitions(self):
        return []

    def get(self, name):
        return type("Tool", (), {"observability": name == "skill_decision"})()


class StreamHook(AgentHook):
    def __init__(self):
        self.ends = []

    def wants_streaming(self):
        return True

    async def on_stream_end(self, context, *, resuming):
        self.ends.append(resuming)


async def run(responses, *, user="Run tests in /repo", enabled=True, limit=4,
              streaming=False, metadata=None, injections=None, goal=None):
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(side_effect=responses)
    provider.chat_stream_with_retry = AsyncMock(side_effect=responses)
    hook = StreamHook() if streaming else AgentHook()
    checkpoints = []

    async def checkpoint(value):
        checkpoints.append(value)

    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": user}], tools=Tools(), model="test",
        max_iterations=limit, max_tool_result_chars=2000, premature_final_guard=enabled,
        premature_final_session_metadata=metadata, hook=hook, checkpoint_callback=checkpoint,
        injection_callback=injections, goal_active_predicate=goal,
    ))
    calls = provider.chat_stream_with_retry if streaming else provider.chat_with_retry
    return result, calls, hook, checkpoints


def response(text="I'll run the tests now.", tool=None):
    return LLMResponse(content=text, tool_calls=[] if not tool else [
        ToolCallRequest(id="call", name=tool, arguments={}),
    ])


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("tool", [None, "skill_decision"])
async def test_correction_before_stream_end(streaming, tool):
    result, calls, hook, checkpoints = await run([
        response(tool=tool), response("Tests passed: 3 passed."),
    ], streaming=streaming)
    assert calls.await_count == 2
    assert result.final_content == "Tests passed: 3 passed."
    assert any(m.get("content") == CORRECTION for m in result.messages)
    assert checkpoints[-1]["phase"] == "final_response"
    assert checkpoints[-1]["assistant_message"]["content"] == result.final_content
    if tool:
        assert any(m.get("role") == "tool" for m in result.messages)
    if streaming:
        assert hook.ends == [True, False]


async def test_partial_work_is_not_completion():
    result, calls, _, _ = await run([
        response("", "read_file"), response("I read the file. I'll run tests now."),
        response("Blocked: test command failed."),
    ])
    assert calls.await_count == 3
    assert result.tools_used == ["read_file"]
    assert result.final_content == "Blocked: test command failed."


@pytest.mark.parametrize("tool", [None, "skill_decision"])
async def test_second_promise_capped(tool):
    result, calls, _, _ = await run([response(tool=tool), response(tool=tool)])
    assert calls.await_count == 2
    assert result.stop_reason == "incomplete"
    assert result.final_content == INCOMPLETE
    assert sum(m.get("content") == CORRECTION for m in result.messages) == 1


@pytest.mark.parametrize("user,text", [
    ("Run tests", "Tests passed."),
    ("Run tests", "Error: test command failed."),
    ("Run tests", "Blocked: I need your approval. I'll run tests after approval."),
    ("Explain how to run tests", "I'll run tests now."),
    ("Plan only; do not execute", "I'll run tests now."),
    ("Run tests", "Tests passed. If you want, I'll run more tests."),
    ("Run stage 2 tests only", "Tests passed. Next stage I'll implement deployment."),
    ("Run stage 2 tests only", "Stage 2 complete. Stage 3 requires approval."),
])
async def test_valid_finals(user, text):
    result, calls, _, _ = await run([response(text, "skill_decision")], user=user)
    assert calls.await_count == 1
    assert result.final_content == text


async def test_disabled_default():
    assert not AgentRunSpec(initial_messages=[], tools=Tools(), model="test", max_iterations=1,
                            max_tool_result_chars=100).premature_final_guard
    result, calls, _, _ = await run([response()], enabled=False)
    assert calls.await_count == 1
    assert result.final_content == "I'll run the tests now."


async def test_no_budget_adds_no_call():
    result, calls, hook, _ = await run([response()], limit=1, streaming=True)
    assert calls.await_count == 1
    assert result.final_content == INCOMPLETE
    assert hook.ends == [False]


async def test_current_stage_not_blanket_blocked():
    result, calls, _, _ = await run([
        response(), response("Stage 2 tests passed. Stage 3 requires approval."),
    ], user="Run stage 2 tests only; stage 3 needs separate approval")
    assert calls.await_count == 2
    assert result.final_content.startswith("Stage 2 tests passed")


def test_focus_does_not_grant_permission_and_bookkeeping_not_work():
    assert not is_premature_final("I'll run tests.", [{"role": "user", "content": "approved"}],
                                  Tools(), [], {"conversation_focus": {"objective": "run tests"}})
    for name in ("skill_decision", "long_task", "read_file"):
        assert is_premature_final("I'll run tests.", [{"role": "user", "content": "Run tests"}],
                                 Tools(), [{"name": name, "status": "ok"}])


async def test_injections_drained_during_correction():
    injections = AsyncMock(side_effect=[[{"role": "user", "content": "Report results only"}], []])
    result, calls, _, _ = await run([response(tool="skill_decision"), response("Tests passed.")],
                                   injections=injections)
    assert calls.await_count == 2
    assert result.had_injections
    assert result.final_content == "Tests passed."


async def test_provider_error():
    result, calls, _, _ = await run([
        LLMResponse(content="Error: provider unavailable", finish_reason="error"),
    ])
    assert calls.await_count == 1
    assert result.stop_reason == "error"


async def test_budget_finalization_promise():
    result, calls, _, _ = await run([response("", "read_file"), response()], limit=1)
    assert calls.await_count == 2  # existing finalization, no correction call
    assert result.stop_reason == "max_iterations"
    assert result.final_content == INCOMPLETE


async def test_goal_continuation_after_observability_answer():
    result, calls, _, _ = await run([
        response("Tests passed.", "skill_decision"), response("Goal complete."),
    ], goal=MagicMock(side_effect=[True, False]))
    assert calls.await_count == 2
    assert result.final_content == "Goal complete."


def test_session_approval_wait_and_current_stage():
    metadata = {"goal_state": {"status": "active", "requires_user_approval": True}}
    assert not is_premature_final("I'll run tests.", [{"role": "user", "content": "Run tests"}],
                                  Tools(), [], metadata)
    assert is_premature_final("I'll run tests.", [{"role": "user", "content": "Run stage 2 tests only"}],
                             Tools(), [], metadata)


@pytest.mark.parametrize("user", ["2단계 실행해 줘", "2단계 수행해 줘"])
async def test_korean_current_work_with_later_approval_boundary(user):
    result, calls, _, _ = await run([
        response("회귀 테스트를 실행하겠습니다. 다음 단계(3)는 승인 전까지 실행하지 않습니다."),
        response("테스트 3개 통과했습니다. 다음 단계(3)는 승인 전까지 실행하지 않습니다."),
    ], user=user)
    assert calls.await_count == 2
    assert result.final_content.startswith("테스트 3개 통과")
