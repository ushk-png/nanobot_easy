"""t28: one design delivery format (a single submit_context_design call), read only from
the completed response. Real SSE parser, synthetic events, no network."""
from __future__ import annotations

import asyncio
import json

import pytest

from nanobot.agent.context_selection import ContextSelector, select_design_output
from nanobot.config.schema import ContextSelectionConfig
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.call_observation import _cleanup_tasks
from nanobot.providers.openai_responses.parsing import consume_sse_with_reasoning
from tests.agent.test_context_selection import plan  # noqa: F401  (fixtures below)
from tests.agent.test_context_selection import env, event  # noqa: F401

DESIGN = plan().model_dump()
OTHER = plan(answer="5").model_dump()


def fc(args, cid="c1"):
    return {"type": "function_call", "call_id": cid, "id": "fc_" + cid,
            "name": "submit_context_design",
            "arguments": args if isinstance(args, str) else json.dumps(args)}


def msg(text):
    return {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}


def terminal(output, status="completed", usage=True):
    resp = {"status": status, "output": output}
    if usage:
        resp["usage"] = {"input_tokens": 50, "output_tokens": 20, "total_tokens": 70}
    return {"type": "response." + status, "response": resp}


class Events:
    status_code = 200

    def __init__(self, events, hold_after=False, hold_before_terminal=False):
        self.events, self.hold_after, self.hold_before = events, hold_after, hold_before_terminal
        self.gate = asyncio.Event()
        self.waiting = asyncio.Event()

    async def aiter_lines(self):
        for i, event in enumerate(self.events):
            if self.hold_before and i == len(self.events) - 1:
                self.waiting.set()
                await self.gate.wait()
            yield "data: " + json.dumps(event)
            yield ""
        if self.hold_after:
            self.waiting.set()
            await self.gate.wait()  # connection kept open after the terminal event


class SSEProvider(LLMProvider):
    """Runs the real Responses SSE parser on scripted streams, one per call."""

    def __init__(self, streams):
        super().__init__()
        self.streams = list(streams)
        self.calls = 0

    def get_default_model(self):
        return "offline"

    async def chat(self, messages, **kwargs):
        stream = self.streams[self.calls]
        self.calls += 1
        try:
            content, calls, finish, usage, _ = await consume_sse_with_reasoning(stream)
        except RuntimeError as exc:
            return LLMResponse(content="Synthetic: " + str(exc), finish_reason="error")
        return LLMResponse(content=content or None, tool_calls=calls, finish_reason=finish, usage=usage)


async def run(env, streams):
    store, scope, registry, skills = env
    event(env, "2+2?")
    selector = ContextSelector(store, ContextSelectionConfig(enabled=True, design_transient_retries=0))
    provider = SSEProvider(streams)
    selected = await selector.select(provider=provider, model="offline", request="2+2?", scope=scope,
                                     session_id="s", registry=registry, skills=skills,
                                     rules="Mandatory permission limits", request_id="t28")
    return provider, selected


def selections(selected):
    return [k.split(":", 1)[1] for k in selected.trace.reasons if k.startswith("design_output:")] or [
        m.get("status") for m in getattr(selected.trace, "milestones", []) if m.get("phase") == "design_output_selected"]


def no_leftovers():
    me = asyncio.current_task()
    assert [t for t in asyncio.all_tasks() if t is not me and not t.done()] == []
    assert not [t for t in _cleanup_tasks if not t.done()]


def ok(selected, answer="4"):
    assert selected.blocked is None
    assert selected.design.answer == answer


@pytest.mark.asyncio
async def test_normal_single_function_call(env):
    provider, selected = await run(env, [Events([terminal([fc(DESIGN)])])])
    ok(selected)
    assert provider.calls == 1
    assert "design_output:single_function_call" in selected.trace.reasons
    no_leftovers()


@pytest.mark.asyncio
async def test_stream_without_terminal_event_is_no_design(env):
    provider, selected = await run(env, [Events([{"type": "response.output_item.done", "item": fc(DESIGN)}])])
    assert selected.blocked == "design_provider_error" and selected.design is None
    assert provider.calls == 1  # a provider failure is not a received proposal: no repair
    no_leftovers()


@pytest.mark.asyncio
async def test_incomplete_response_is_not_success(env):
    provider, selected = await run(env, [Events([terminal([fc(DESIGN)], status="incomplete")])])
    assert selected.blocked == "design_incomplete_response" and selected.design is None
    assert provider.calls == 1
    no_leftovers()


@pytest.mark.asyncio
async def test_truncated_arguments_are_never_executed(env):
    truncated = json.dumps(DESIGN)[:-25]
    provider, selected = await run(env, [Events([terminal([fc(truncated)])]),
                                         Events([terminal([fc(OTHER)])])])
    assert provider.calls == 2  # received but invalid: the one repair call
    ok(selected, "5")
    no_leftovers()


@pytest.mark.asyncio
async def test_explanation_text_beside_valid_call_is_ignored_and_recorded(env):
    provider, selected = await run(env, [Events([terminal([msg("I will submit the design."), fc(DESIGN)])])])
    ok(selected)
    assert "design_output:explanation_text_ignored" in selected.trace.reasons
    no_leftovers()


@pytest.mark.asyncio
async def test_identical_text_copy_is_ignored(env):
    provider, selected = await run(env, [Events([terminal([msg(json.dumps(DESIGN)), fc(DESIGN)])])])
    ok(selected)
    assert "design_output:identical_text_duplicate_ignored" in selected.trace.reasons


@pytest.mark.asyncio
async def test_partial_json_text_beside_valid_call_is_not_executed(env):
    provider, selected = await run(env, [Events([terminal([msg(json.dumps(OTHER)[:40]), fc(DESIGN)])])])
    ok(selected)  # the function call is the format; partial text is never parsed into a design
    assert "design_output:text_unparsed_json_ignored" in selected.trace.reasons


@pytest.mark.asyncio
async def test_conflicting_text_and_call_are_not_merged_or_switched(env):
    # t35: the single function call is the official result; differing text is recorded, never
    # merged, never switched to, and never a repair trigger.
    provider, selected = await run(env, [Events([terminal([msg(json.dumps(OTHER)), fc(DESIGN)])])])
    assert provider.calls == 1
    ok(selected)  # the call's design (answer "4"), not the text's answer "5"
    assert "design_output:text_design_differs_ignored" in selected.trace.reasons
    no_leftovers()


@pytest.mark.asyncio
async def test_conflicting_function_calls_are_rejected(env):
    provider, selected = await run(env, [Events([terminal([fc(DESIGN, "a"), fc(OTHER, "b")])]),
                                         Events([terminal([fc(OTHER)])])])
    assert provider.calls == 2
    assert "design_output:design_conflicting_function_calls" in selected.trace.reasons
    ok(selected, "5")


@pytest.mark.asyncio
async def test_text_only_design_is_not_silently_adopted(env):
    provider, selected = await run(env, [Events([terminal([msg(json.dumps(OTHER))])]),
                                         Events([terminal([fc(DESIGN)])])])
    assert provider.calls == 2
    assert "design_output:design_text_instead_of_function" in selected.trace.reasons
    ok(selected)  # the text design "5" was never executed


@pytest.mark.asyncio
async def test_connection_left_open_after_completed_does_not_delay(env):
    stream = Events([terminal([fc(DESIGN)])], hold_after=True)
    async with asyncio.timeout(1.0):
        provider, selected = await run(env, [stream])
    ok(selected)
    stream.gate.set()


@pytest.mark.asyncio
async def test_cancel_just_before_completion_propagates_and_leaves_nothing(env):
    stream = Events([{"type": "response.output_item.done", "item": fc(DESIGN)},
                     terminal([fc(DESIGN)])], hold_before_terminal=True)
    task = asyncio.create_task(run(env, [stream]))
    await stream.waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    no_leftovers()


@pytest.mark.asyncio
async def test_cancel_just_after_completion_keeps_result(env):
    task = asyncio.create_task(run(env, [Events([terminal([fc(DESIGN)])])]))
    provider, selected = await task
    assert task.cancel() is False
    ok(selected)
    no_leftovers()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_usage", [True, False])
async def test_usage_present_or_missing_is_recorded_not_invented(env, with_usage):
    provider, selected = await run(env, [Events([terminal([fc(DESIGN)], usage=with_usage)])])
    ok(selected)
    usage = selected.trace.calls[-1].get("provider_usage")
    if with_usage:
        assert usage and usage.get("completion_tokens", usage.get("output_tokens")) == 20
    else:
        assert usage is None  # unknown, never 0


@pytest.mark.asyncio
async def test_partial_events_and_final_snapshot_are_not_assembled_twice(env):
    args = json.dumps(DESIGN)
    events = [{"type": "response.output_item.added", "item": {**fc(""), "arguments": ""}},
              {"type": "response.function_call_arguments.delta", "item_id": "fc_c1", "call_id": "c1",
               "delta": args[:30]},
              {"type": "response.function_call_arguments.delta", "item_id": "fc_c1", "call_id": "c1",
               "delta": args[30:]},
              {"type": "response.output_text.delta", "delta": "note"},
              terminal([fc(DESIGN)])]
    provider, selected = await run(env, [Events(events)])
    ok(selected)
    assert "design_output:single_function_call" in selected.trace.reasons  # snapshot had no text


def test_selection_rule_unit():
    from nanobot.providers.base import ToolCallRequest
    r = lambda content, *calls: LLMResponse(content=content, tool_calls=[
        ToolCallRequest(id=str(i), name=n, arguments=a) for i, (n, a) in enumerate(calls)])
    assert select_design_output(r(None, ("submit_context_design", DESIGN)))[1] == "single_function_call"
    assert select_design_output(r(None, ("submit_context_design", DESIGN), ("submit_context_design", DESIGN)))[1] \
        == "identical_duplicate_function_calls"
    assert select_design_output(r(None, ("write_file", {})))[2] == ["design_format_error"]
    assert select_design_output(r("x"))[0] is None
