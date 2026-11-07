"""Routing and executor delivery for multi-clue research guidance."""

import json

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.skills import BUILTIN_SKILLS_DIR
from nanobot.skill_store import SkillStore
from nanobot.utils.prompt_templates import render_template


def test_complex_research_routes_real_questions_and_preserves_neighbors(tmp_path):
    # A real neighboring critique skill also uses generic Korean words such as
    # "특정" and "찾아줘". They must not outweigh the research domain clues.
    rival = tmp_path / "skills" / "adversarial-review" / "SKILL.md"
    rival.parent.mkdir(parents=True)
    rival.write_text(
        '---\nname: adversarial-review\n'
        'description: 특정 주장이나 계획의 약점 찾아줘, 비판해줘 요청에 사용하는 검토.\n'
        '---\n\n## When To Use\n\n- 약점 찾아줘\n- 비판해줘\n'
    )
    store = SkillStore(tmp_path)
    store.reindex()
    store.approve_draft("adversarial-review")
    cases = json.loads(
        (BUILTIN_SKILLS_DIR / "complex-research" / "routing_cases.json").read_text()
    )["cases"]
    cases.append({"query": "이 계획의 약점 찾아줘. 비판해줘.", "expected": "adversarial-review"})
    failures = []
    for case in cases:
        matches = store.search(case["query"], top_k=3)
        actual = matches[0].name if matches else None
        if actual != case["expected"]:
            failures.append((case["query"], case["expected"], actual))
    assert not failures, failures


@pytest.mark.parametrize("method", ["build_system_prompt", "build_selected_system_prompt"])
@pytest.mark.parametrize("names", [[], ["complex-research"], ["research-brief"]])
def test_research_guidance_reaches_both_executor_paths(tmp_path, method, names):
    builder = ContextBuilder(tmp_path)
    prompt = getattr(builder, method)(skill_names=names)
    contract = render_template("agent/tool_contract.md")
    assert contract and prompt.count(contract) == 1
    if names:
        skill = builder.skills.load_skills_for_context(names)
        assert skill and prompt.count(skill) == 1
    else:
        # Routine lookup needs the common contract, not either full research method.
        for name in ("complex-research", "research-brief"):
            skill = builder.skills.load_skills_for_context([name])
            assert skill and skill not in prompt
