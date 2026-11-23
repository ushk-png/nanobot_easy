"""The request-understanding guidance reaches both the compact and the full system prompt."""
from pathlib import Path

from nanobot.agent.context import ContextBuilder

SNIPPET = Path(__file__).parents[2] / "nanobot/templates/agent/_snippets/request_understanding.md"
KEY = "## Understanding the Request"


def _prompts(tmp_path):
    builder = ContextBuilder(tmp_path)
    return (builder.build_system_prompt(channel="telegram"),
            builder.build_compact_system_prompt(channel="telegram"))


def test_both_prompts_carry_the_guidance_exactly_once(tmp_path):
    full, compact = _prompts(tmp_path)

    assert full.count(KEY) == 1
    assert compact.count(KEY) == 1


def test_guidance_covers_scope_one_question_assumptions_and_unattended_runs(tmp_path):
    _, compact = _prompts(tmp_path)

    for phrase in ("delivers findings only and changes nothing",
                   "ask one\nshort question and stop",
                   "state\nthe assumption in one short line",
                   "Do not ask for what a lookup can find",
                   "unattended"):
        assert phrase in compact, phrase


def test_guidance_stays_small_because_the_compact_prompt_is_sent_every_turn():
    assert len(SNIPPET.read_text(encoding="utf-8")) < 1200
