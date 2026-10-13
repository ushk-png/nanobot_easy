"""Offline regression for the v3.4 entry point and narrow assistance contracts."""
import asyncio
import time
from types import SimpleNamespace

from nanobot.agent.tools.registry import ToolRegistry
from nanobot.reasoning.agent_tool import ReasoningAssistTool
from nanobot.reasoning.contract import normalize_procedure, validate_normalized_procedure


def tool():
    loop = SimpleNamespace(_current_iteration=1)
    return ReasoningAssistTool(loop, None, time.monotonic())


def test_confirmed_calculation():
    import json
    result = asyncio.run(tool().execute("calculate", {}, "sum", [2, 3], "confirmed", "kg"))
    assert json.loads(result)["result"] == 5


def test_unknown_not_promoted():
    assert asyncio.run(tool().execute("calculate", {}, values=[1])).is_error


def test_invalid_numeric_inputs():
    for numbers in ([True], [float("inf")], []):
        assert asyncio.run(tool().execute("calculate", {}, values=numbers, data_status="confirmed")).is_error


def test_assistance_not_repeated():
    assist = tool()
    asyncio.run(assist.execute("calculate", {}, values=[1], data_status="confirmed"))
    assert asyncio.run(assist.execute("calculate", {}, values=[1], data_status="confirmed")).is_error


def test_nonselection_requirement_and_paraphrased_purpose():
    definition = {"purpose": "Interpret causal evidence", "requirements": [{"id": "R1", "statement": "원인 설명"}],
                  "steps": [{"id": "A", "question": "근거 정리", "inputs": ["request", "R1"], "outputs": ["evidence"], "role": "work"},
                            {"id": "B", "question": "설명 작성", "inputs": ["A"], "outputs": ["answer"], "role": "answer"}]}
    normalized, _, defects = normalize_procedure(definition, request="왜 발생했나", known_goal="원인 조사")
    assert not defects
    selected, defects = validate_normalized_procedure(normalized, request="왜 발생했나")
    assert selected and not defects


def test_general_loop_first_without_classifier(tmp_path, monkeypatch):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.events import InboundMessage
    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = tmp_path
    loop.stepwise_enabled = True
    loop.tools = ToolRegistry()
    calls = []
    async def status(*args, **kwargs):
        pass
    async def general(messages, **kwargs):
        calls.append(kwargs["tools"].tool_names)
        return "안녕하세요", [], messages, "completed", False
    async def forbidden(*args, **kwargs):
        raise AssertionError("preclassification must not run")
    loop._run_agent_loop = general
    loop._run_stepwise = forbidden
    loop._runtime_events = lambda: SimpleNamespace(run_status_changed=status)
    monkeypatch.setattr("nanobot.agent.loop.turn_continuation.maybe_continue_turn", status)
    ctx = SimpleNamespace(msg=InboundMessage(channel="telegram", sender_id="u", chat_id="1", content="안녕"),
        session_key="telegram:1", visible_run_started_at=None, initial_messages=[{"role": "user", "content": "안녕"}],
        on_progress=None, on_stream=None, on_stream_end=None, on_retry_wait=None, session=None,
        pending_queue=None, ephemeral=False, run_extra_hooks_for_ephemeral=False, hooks=None, tools=None)
    assert asyncio.run(loop._state_run(ctx)) == "ok"
    assert calls == [["reasoning_assist"]]
    assert not loop.tools.has("reasoning_assist")
