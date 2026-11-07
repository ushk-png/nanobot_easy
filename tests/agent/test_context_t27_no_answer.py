"""t27: a turn that ends without the required final answer is never judged a success.

Paths: response (blank / placeholder text), extraction (completed snapshot with no message),
tool loop (max iterations, record-only tool then nothing), termination (verdict)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nanobot.agent.answer_delivery import ANSWER_BODY_INCOMPLETE
from nanobot.agent.context_selection import IntentDesign, SelectedContext
from nanobot.agent.context_trace import ContextTrace
from nanobot.agent.premature_final import INCOMPLETE
from nanobot.providers.openai_responses.parsing import consume_sse_with_reasoning
from nanobot.utils.prompt_templates import render_template
from nanobot.utils.runtime import EMPTY_FINAL_RESPONSE_MESSAGE

MAX_ITER = render_template("agent/max_iterations_message.md", strip=True, max_iterations=5)


def _sel(route="direct", checks=("Answer supplied",), answer="4"):
    design = IntentDesign.model_validate(dict(
        goal="g", request_type="explain", task_relation="new", task_id=None, route=route,
        constraints=[], information_needs=[], capabilities_needed=[], completion_checks=list(checks),
        unresolved=[], answer=answer))
    sel = SelectedContext(trace=ContextTrace("t27"), design=design)
    sel.current_event = SimpleNamespace(content="2+2?")
    return sel


@pytest.mark.parametrize("content,reason", [
    (EMPTY_FINAL_RESPONSE_MESSAGE, "empty_final"),
    (MAX_ITER, "max_iterations"),
    (INCOMPLETE, "incomplete_marker"),
    (ANSWER_BODY_INCOMPLETE, "incomplete_marker"),
    ("", None), ("   \n", None), (None, None),
])
@pytest.mark.parametrize("stop", ["completed", "empty_final_response", "max_iterations"])
def test_placeholder_or_blank_is_never_completed_even_on_a_direct_route(content, reason, stop):
    sel = _sel()
    text, status = sel.verdict(content, stop)
    assert status != "completed"
    assert "required_final_answer_missing" in text
    if reason:
        assert sel.trace.counters["final_answer_missing"] == 1
        assert "final_answer_missing_" + reason in sel.trace.reasons
        assert sel.answer_text != content


def test_a_real_short_answer_on_a_direct_route_still_completes():
    assert _sel().verdict("4", "completed")[1] == "completed"


def test_an_answer_that_mentions_the_phrase_is_not_a_placeholder():
    text = "Note: " + EMPTY_FINAL_RESPONSE_MESSAGE + " — that was yesterday; today 2+2=4."
    assert _sel().verdict(text, "completed")[1] == "completed"


class _Lines:
    status_code = 200

    def __init__(self, events):
        self.events = events

    async def aiter_lines(self):
        for e in self.events:
            yield "data: " + json.dumps(e)
            yield ""


def test_extraction_completed_with_only_reasoning_yields_no_text_and_then_no_success():
    done = {"type": "response.completed", "response": {"status": "completed", "output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]}],
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}}
    text, calls, status, usage, _ = asyncio.run(consume_sse_with_reasoning(_Lines([done])))
    assert text == "" and not calls and status == "stop" and usage["total_tokens"] == 5
    assert _sel(route="execute").verdict(text, status if status != "stop" else "completed")[1] != "completed"


def test_golf_record_only_tool_then_commentary_is_incomplete():
    # Real t24 golf t2: model called skill_decision, then wrote one line about the edit, no timetable.
    from .test_context_t24_replay import replay
    sel = replay("A_golf_s1-t2")
    text, status = sel.verdict("방금 계획을 45분으로 줄이는 수정입니다. 퍼팅 20분은 유지하고, 드라이버는 넣지 않습니다.")
    assert status != "completed"
