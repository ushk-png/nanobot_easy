"""Offline replay of announcement-only main-agent answers; no paid model calls."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.answer_delivery import (
    ANSWER_BODY_CORRECTION,
    ANSWER_BODY_INCOMPLETE,
    ANSWER_BODY_NOTICE,
    needs_answer_body,
)
from nanobot.agent.hook import AgentHook
from nanobot.agent.premature_final import CORRECTION, is_premature_final
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

GUIDE = "使い方を説明します。\n1. `tool --help` で利用可能なコマンドを確認します。\n2. テスト用ディレクトリから起動します。"
INTRO = "この端末にインストールされたツールを確認してから説明します。"
KO_INTRO = "지금 설치된 도구 기준으로 사용법을 정리합니다."
CAVEAT_INTRO = "이 환경에 설치된 버전 기준으로 정리합니다. 기본 명령만 실행하면 새 설정이 적용되지 않습니다."


class Tools:
    def __init__(self):
        self.executed = []

    def get_definitions(self):
        return []

    def get(self, name):
        return SimpleNamespace(observability=name == "skill_decision")

    async def execute(self, name, params):
        self.executed.append(name)
        return "current test version=2" if name == "read_file" else "recorded"


class StreamHook(AgentHook):
    def __init__(self):
        self.ends = []

    def wants_streaming(self):
        return True

    async def on_stream_end(self, context, *, resuming):
        self.ends.append(resuming)


async def replay(responses, *, messages=None, streaming=False, limit=6, enabled=True):
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(side_effect=responses)
    provider.chat_stream_with_retry = AsyncMock(side_effect=responses)
    notices = AsyncMock()
    tools = Tools()
    hook = StreamHook() if streaming else AgentHook()
    result = await AgentRunner(provider).run(AgentRunSpec(
        initial_messages=messages or [{"role": "user", "content": "도구 사용 방법 자세히 알려 줘"}],
        model="offline", tools=tools, max_iterations=limit, max_tool_result_chars=2000,
        premature_final_guard=enabled, progress_callback=notices, hook=hook))
    return result, tools, notices, hook, (provider.chat_stream_with_retry if streaming else provider.chat_with_retry)


def response(text, tool=None):
    return LLMResponse(content=text, tool_calls=[] if tool is None else [
        ToolCallRequest(id="call-" + tool, name=tool, arguments={})])


@pytest.mark.parametrize("streaming", [False, True])
async def test_help_lookup_then_announcement_recovers_actual_guide_without_rerunning_tools(streaming):
    result, tools, notices, hook, calls = await replay([
        response("확인 중입니다.", "read_file"), response(CAVEAT_INTRO), response(GUIDE),
    ], streaming=streaming)
    assert result.final_content == GUIDE
    assert result.stop_reason == "completed"
    assert calls.await_count == 3
    assert tools.executed == ["read_file"]
    assert notices.await_args.args[0] == ANSWER_BODY_NOTICE
    assert any(m.get("content") == ANSWER_BODY_CORRECTION and m.get("_hidden_history")
               for m in result.messages)
    # The reported incident was NOT the execution-promise guard.
    assert not any(m.get("content") == CORRECTION for m in result.messages)
    if streaming:
        assert hook.ends[-2:] == [True, False]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("followup", ["왜 답을 안줘", "정리하고 있나"])
async def test_routing_metadata_with_preamble_is_not_the_final_answer(streaming, followup):
    messages = [{"role": "user", "content": "도구 사용 방법 자세히 알려 줘"},
                {"role": "assistant", "content": CAVEAT_INTRO},
                {"role": "user", "content": followup}]
    result, tools, _, _, calls = await replay([
        response("직전 답이 확인 단계에서 끊겼습니다. " + KO_INTRO, "skill_decision"),
        response(GUIDE),
    ], messages=messages, streaming=streaming)
    assert calls.await_count == 2
    assert result.final_content == GUIDE
    assert tools.executed == ["skill_decision"]
    assert any(m.get("tool_call_id") == "call-skill_decision" for m in result.messages)


async def test_second_announcement_still_continues_before_delivering_actual_answer():
    result, _, notices, _, calls = await replay([
        response(KO_INTRO), response(KO_INTRO), response(GUIDE)])
    assert calls.await_count == 3
    assert notices.await_count == 2
    assert result.final_content == GUIDE


async def test_repeated_failure_is_bounded_and_reported_without_claiming_work_completed():
    result, tools, notices, _, calls = await replay([response(KO_INTRO)] * 3)
    assert calls.await_count == 3
    assert notices.await_count == 2
    assert result.stop_reason == "incomplete"
    assert result.final_content.startswith(KO_INTRO)
    assert ANSWER_BODY_INCOMPLETE in result.final_content
    assert tools.executed == []


async def test_no_budget_is_explicit_without_additional_call():
    result, _, notices, _, calls = await replay([response(KO_INTRO)], limit=1)
    assert calls.await_count == 1 and notices.await_count == 0
    assert result.stop_reason == "incomplete"
    assert ANSWER_BODY_INCOMPLETE in result.final_content


@pytest.mark.parametrize("user_request,text", [
    ("자세히 알려 줘", "현재 버전은 2입니다."),
    ("간단히 알려 줘", "간단히 정리합니다. 설정 파일은 프로젝트 루트에 둡니다."),
    ("자세히 알려 줘", "다음처럼 정리합니다.\n1. 도구를 설치합니다.\n2. 설정 파일을 읽습니다."),
    ("자세히 알려 줘", "정리합니다.\n```sh\ntool --help\n```"),
    ("자세히 알려 줘", "정리합니다. 첫 번째 항목입니다. 두 번째 항목입니다. 세 번째 항목입니다."),
    ("학습문장 확인해 줘", "다음을 입력하시면 한국어 뜻을 설명드리겠습니다."),
    ("계획만 제안해 줘", "다음 단계는 원하시면 설명하겠습니다."),
    ("진행상황 알려 줘", "자료를 읽는 중이며 아직 검증 전입니다."),
])
async def test_substantive_short_answers_guides_conditions_and_status_are_preserved(user_request, text):
    result, _, notices, _, calls = await replay([response(text, "skill_decision")],
        messages=[{"role": "user", "content": user_request}])
    assert result.final_content == text
    assert calls.await_count == 1
    assert notices.await_count == 0


async def test_explanation_preamble_with_skill_decision_continues_to_answer_body():
    preamble = "프랭크, ‘2, 5, 6’은 오늘 새 문장 2번·5번·6번으로 이해했습니다. 하나씩 쉽게 풀어 드리겠습니다."
    answer = "2번 문장 구조는 이유와 거절입니다.\n5번 문장은 질문입니다.\n6번 문장은 취향을 말합니다."
    result, tools, notices, _, calls = await replay([
        response(preamble, "skill_decision"), response(answer),
    ], messages=[{"role": "user", "content": "2,5,6 설명해 줘"}])
    assert result.final_content == answer
    assert calls.await_count == 2
    assert tools.executed == ["skill_decision"]
    assert notices.await_args.args[0] == ANSWER_BODY_NOTICE


def test_reported_text_does_not_match_execution_promise_guard():
    messages = [{"role": "user", "content": "도구 사용 방법 자세히 알려 줘"}]
    for text in [KO_INTRO, CAVEAT_INTRO]:
        assert not is_premature_final(text, messages, Tools(), [])
        assert needs_answer_body(text, messages)


def test_korean_unpunctuated_future_sentence_preserves_conditional_boundary():
    text = "다음을 입력하시면 뜻을 보여 드리겠습니다 지금 파일을 확인하겠습니다"
    assert is_premature_final(text, [{"role": "user", "content": "파일 확인해 줘"}], Tools(), [])


async def test_internal_runs_do_not_inherit_main_answer_recovery():
    result, _, notices, _, calls = await replay([response(KO_INTRO)], enabled=False)
    assert result.final_content == KO_INTRO
    assert calls.await_count == 1
    assert notices.await_count == 0


async def test_answer_recovery_can_continue_into_actual_tools_within_existing_scope():
    result, tools, _, _, calls = await replay([
        response(KO_INTRO), response("", "read_file"), response(GUIDE)])
    assert calls.await_count == 3
    assert tools.executed == ["read_file"]
    assert result.final_content == GUIDE
    correction = next(m["content"] for m in result.messages if m.get("content") == ANSWER_BODY_CORRECTION)
    assert "NO permission" in correction and "stage boundaries" in correction


async def test_real_access_blocker_is_preserved_without_recovery():
    text = "사용법을 정리합니다. 권한이 없어 필수 자료를 읽을 수 없습니다."
    result, tools, notices, _, calls = await replay([response(text)])
    assert calls.await_count == 1
    assert result.final_content == text
    assert tools.executed == []
    assert notices.await_count == 0


def test_reported_short_texts_are_not_truncated_by_repetition_guard():
    from nanobot.agent.repetition_guard import RepeatedSentenceGuard
    for text in [KO_INTRO, CAVEAT_INTRO]:
        guard = RepeatedSentenceGuard()
        assert guard.feed(text) == text
        assert not guard.stopped
        assert guard.finish(text) == text


async def test_budget_finalization_cannot_report_an_announcement_as_a_delivered_answer():
    result, tools, notices, _, calls = await replay([
        response("", "read_file"), response(KO_INTRO)], limit=1)
    assert calls.await_count == 2  # existing budget finalization; no new recovery call
    assert tools.executed == ["read_file"]
    assert notices.await_count == 0
    assert result.stop_reason == "max_iterations"
    assert ANSWER_BODY_INCOMPLETE in result.final_content


def test_answer_recovery_feedback_does_not_become_a_persisted_human_instruction():
    from nanobot.agent.loop import AgentLoop
    from nanobot.session.manager import Session
    loop = AgentLoop.__new__(AgentLoop)
    loop.max_tool_result_chars = 2000
    session = Session(key="offline")
    loop._save_turn(session, [
        {"role": "user", "content": "사용법을 자세히 알려 줘"},
        {"role": "assistant", "content": KO_INTRO},
        {"role": "user", "content": ANSWER_BODY_CORRECTION,
         "_hidden_history": True, "_answer_delivery_feedback": True},
        {"role": "assistant", "content": GUIDE},
    ], 0)
    assert [m["role"] for m in session.messages] == ["user", "assistant", "assistant"]
    assert not any(m.get("content") == ANSWER_BODY_CORRECTION for m in session.messages)
    assert session.messages[1]["content"] == KO_INTRO  # actual model output remains intact
