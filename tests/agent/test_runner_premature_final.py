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


# Representative seven-card body; the reported incident's closing line is verbatim.
JAPANESE_CARDS = "\n".join([
    "1. 今日はいい天気ですね。", "2. 駅まで歩いて行きます。",
    "3. コーヒーを一杯ください。", "4. 日本語を勉強しています。",
    "5. 明日は何をしますか？", "6. もう一度お願いします。",
    "7. お会いできてうれしいです。",
])


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("user", ["오늘 학습문장 보여줘", "오늘 학습문장 확인해 줘"])
async def test_japanese_cards_with_conditional_next_step_are_final(streaming, user):
    text = JAPANESE_CARDS + "\n확인하신 뒤 **다음**을 입력하시면 한국어 뜻만 보여 드리겠습니다."
    result, calls, hook, checkpoints = await run([response(text)], user=user, streaming=streaming)
    assert calls.await_count == 1
    assert result.final_content == text
    assert not any(m.get("content") == CORRECTION for m in result.messages)
    assert checkpoints[-1]["assistant_message"]["content"] == text
    if streaming:
        assert hook.ends == [False]


@pytest.mark.parametrize("condition", [
    "입력하시면", "입력하면", "입력해 주시면", "선택하시면",
    "응답하시면", "답장해주면", "요청하시면", "말씀해 주시면",
])
def test_conditional_user_steps_do_not_promise_current_work(condition):
    text = f"확인하신 뒤 다음을 {condition} 한국어 뜻을 보여 드리겠습니다."
    assert not is_premature_final(text, [{"role": "user", "content": "학습문장 확인해 줘"}], Tools(), [])


@pytest.mark.parametrize("text", [
    "확인하고 한국어 뜻을 보여 드리겠습니다.",
    "테스트가 끝나면 결과를 확인하겠습니다.",
    "다음을 입력하시면 뜻을 보여 드리겠습니다. 지금 파일을 확인하겠습니다.",
    "다음을 입력하시면 뜻을 보여 드리겠습니다。지금 파일을 확인하겠습니다。",
    "다음을 입력하시면 뜻을 보여 드리겠습니다！테스트를 실행하겠습니다！",
    "다음을 입력하시면 뜻을 보여 드리겠습니다？파일을 검증하겠습니다？",
])
def test_true_promises_still_detected_after_conditional_user_step(text):
    assert is_premature_final(text, [{"role": "user", "content": "학습문장 확인해 줘"}], Tools(), [])
