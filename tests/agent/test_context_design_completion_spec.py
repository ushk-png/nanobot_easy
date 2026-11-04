"""Patch 6: code-side completion_spec restore and skill-aware unsafe_direct; offline only."""
from __future__ import annotations

import json

import pytest

from nanobot.agent.context_selection import (
    CompletionSpec,
    ContextSelector,
    design_wire_payload,
    normalize_completion_specs,
    validate_meaning,
)
from nanobot.config.schema import ContextSelectionConfig

from .test_context_selection import env as env
from .test_context_selection import event, plan, select

CHECKS = ["답변이 Q1, Q2, Q3 번호를 모두 포함한다", "앱을 설치하거나 파일을 만들지 않는다"]


def spec(check, **kw):
    return CompletionSpec(check=check, result_kind=kw.pop("result_kind", "semantic"), **kw)


def test_exact_spec_is_kept_unchanged():
    design = plan(completion_checks=list(CHECKS), completion_specs=[spec(CHECKS[0])])
    assert normalize_completion_specs(design) == {"canonicalized": 0, "dropped_inert": 0, "added_checks": 0}
    assert [s.check for s in design.completion_specs] == [CHECKS[0]]
    assert design.completion_checks == CHECKS


def test_light_paraphrase_is_canonicalized_to_the_single_matching_check():
    design = plan(completion_checks=list(CHECKS), completion_specs=[
        spec("답변이 Q1, Q2, Q3 번호를 모두 포함한다.", result_kind="file_content", target="a.md")])
    counts = normalize_completion_specs(design)
    assert counts["canonicalized"] == 1
    assert design.completion_specs[0].check == CHECKS[0]
    assert design.completion_specs[0].target == "a.md"
    assert design.completion_checks == CHECKS


def test_inert_semantic_spec_is_dropped_without_touching_checks():
    # Observed live (grok-4.7): a summary paraphrase of several checks, semantic, no target.
    design = plan(completion_checks=list(CHECKS), completion_specs=[
        spec("Q1~Q3가 각각 장단점 또는 폴더 구성을 담고 설치·파일 생성을 하지 않음",
             expected_values={"labels": ["Q1", "Q2", "Q3"]})])
    counts = normalize_completion_specs(design)
    assert counts == {"canonicalized": 0, "dropped_inert": 1, "added_checks": 0}
    assert design.completion_specs == []
    assert design.completion_checks == CHECKS


def test_typed_unmatched_spec_adds_its_check_never_loosens():
    design = plan(completion_checks=list(CHECKS), completion_specs=[
        spec("pytest passes on tests/test_x.py", result_kind="test_result")])
    counts = normalize_completion_specs(design)
    assert counts["added_checks"] == 1
    assert design.completion_checks == [*CHECKS, "pytest passes on tests/test_x.py"]
    payload = {"request": "q", "sources": [], "capabilities": [], "task_candidates": [],
               "references": {}, "known_material_clues": []}
    assert "unknown_completion_spec" not in validate_meaning(design, payload)


def _payload(request="개인 메모 방식을 비교해줘", skills=("answer-comparison",)):
    return {"request": request, "sources": [{"event_id": "cur", "actor": "user", "content": request}],
            "capabilities": [{"name": "exec"}, *({"name": s} for s in skills)],
            "skill_capabilities": list(skills), "task_candidates": [], "references": {},
            "known_material_clues": []}


def test_direct_with_only_skill_capabilities_is_not_unsafe():
    design = plan(request_type="compare", capabilities_needed=["answer-comparison"],
                  answer="Q1. ... Q2. ... Q3. ... 완전한 비교 답변")
    assert "unsafe_direct" not in validate_meaning(design, _payload())


def test_direct_with_a_tool_capability_stays_unsafe():
    design = plan(request_type="compare", capabilities_needed=["answer-comparison", "exec"],
                  answer="Q1. ... 완전한 비교 답변")
    assert "unsafe_direct" in validate_meaning(design, _payload())


def test_without_skill_metadata_every_capability_counts_as_before():
    payload = _payload()
    payload.pop("skill_capabilities")
    design = plan(request_type="compare", capabilities_needed=["answer-comparison"],
                  answer="Q1. ... 완전한 비교 답변")
    assert "unsafe_direct" in validate_meaning(design, payload)


def test_action_request_still_unsafe_even_with_skill_only_capabilities():
    design = plan(request_type="compare", capabilities_needed=["answer-comparison"],
                  answer="완전한 답변")
    # Still never accepted as direct: validator-class signal, rerouted to execute in design().
    assert {"direct_action_word", "relation_requires_work"} & set(
        validate_meaning(design, _payload(request="파일 수정해줘")))


def test_skill_metadata_is_never_sent_to_the_designer(env):
    payload = ContextSelector(env[0], ContextSelectionConfig()).minimal_input(
        "2+2?", env[1], "s", env[2], env[3])
    names = {c["name"] for c in payload["capabilities"]}
    assert set(payload["skill_capabilities"]) <= names
    assert "skill_capabilities" not in design_wire_payload(payload)


@pytest.mark.asyncio
async def test_spec_mismatch_and_skill_capability_need_no_repair_call(env):
    skill = sorted(s["name"] for s in env[3].list_skills())[0]
    event(env, "2+2?")
    design = plan(completion_checks=["Answer supplied"], capabilities_needed=[skill],
                  completion_specs=[spec("Q1~Q3 all present, nothing installed")])
    _, provider, selected = await select(env, design)
    assert selected.blocked is None
    assert len(provider.requests) == 1                      # no repair call
    assert selected.design.completion_specs == []
    assert selected.design.completion_checks == ["Answer supplied"]
    report = json.dumps(selected.trace.report())
    assert "completion_spec_dropped_inert" in report
    assert "design_recoveries" not in report
