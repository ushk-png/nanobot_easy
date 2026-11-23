"""File-change result check guidance: where it applies, where it must not, and that its rules stay intact.

Offline: only prompt composition is exercised (no model call), so these tests show that the instruction reaches
the prompts that need it, not that a model follows it.
"""
from __future__ import annotations

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.shell import ExecTool
from nanobot.utils.prompt_templates import render_template
from tests.agent.test_tool_scoped_guidance import activate

KEY = "- For a file you create or change, check the result against the request"
RULES = (
    "not against tool success or file existence",
    "the exact path the user named",
    "the required content, items and format",
    "and anything the user said to keep",
    "Reuse evidence you hold",
    "after a `write_file` that succeeded with a known outcome, the content you passed is the evidence",
    "after a successful `edit_file` or `apply_patch`, use its result and change summary when they show the requirement is met",
    "read again only the range they do not cover",
    "including a part to keep that the change could have touched",
    "A read made before a later change",
    "a change from an earlier turn",
    "a same-named file at another path is not evidence",
    "fix only that part within the approved scope",
    "Report what you could not check",
    "never claim success the evidence does not show",
    "Create no extra files for checking",
    "do not redo a successful change",
    "A plain save of given text needs only the tool result",
    "A cancellation, scope change or permission limit outranks the check",
)


def bullet() -> str:
    text = render_template("agent/tool_contract.md")
    start = text.index(KEY)
    return text[start:text.index("\n", start)]


def test_the_bullet_is_in_the_contract_once_and_keeps_every_rule():
    contract = render_template("agent/tool_contract.md")
    assert contract.count(KEY) == 1
    for rule in RULES:
        assert rule in bullet(), rule


def test_the_bullet_is_small():
    assert len(bullet()) < 1200


def test_existing_verification_and_boundary_rules_are_untouched():
    contract = render_template("agent/tool_contract.md")
    assert "After meaningful changes, verify with the smallest reliable check" in contract
    assert "Before reporting completion, inspect the diff and run relevant checks" in contract
    assert "Respect safety and workspace-boundary errors as real limits" in contract


def test_the_full_prompt_carries_it_and_a_simple_compact_turn_does_not(tmp_path):
    builder = ContextBuilder(tmp_path)
    assert builder.build_system_prompt(channel="telegram").count(KEY) == 1
    assert KEY not in builder.build_compact_system_prompt(channel="telegram")          # no activation, no procedure


@pytest.mark.parametrize("names,expected", [
    (["write_file"], True),
    (["read_file", "write_file"], True),
    (["exec"], True),                                  # commands can change files too
    (None, True),                                      # everything loaded
    (["read_file"], False),                            # reading only: nothing to check afterwards
    (["web_search", "web_fetch"], False),
    (["search_memory", "read_memory_events"], False),
])
async def test_activation_loads_it_only_with_tools_that_can_change_files(tmp_path, names, expected):
    _, system, _ = await activate(tmp_path, names, extra=(ExecTool(working_dir=str(tmp_path)),))
    assert (KEY in system["content"]) is expected
    assert system["content"].count(KEY) <= 1
