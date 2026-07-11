from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.delegate import DelegateTool
from nanobot.agent.tools.skill_search import SkillSearchTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults, SubagentProfile
from nanobot.skill_store import SkillStore
from nanobot.webui.skill_manage_api import skill_manage_status_payload, skill_manage_update_payload


def _write_skill(
    root,
    name: str,
    *,
    status: str = "candidate",
    risk_level: str = "low",
    requires_exec: bool = False,
    category: str = "general",
    description: str | None = None,
) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.joinpath("SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"description: {description or f'{name} trigger phrase'}",
                "metadata:",
                "  nanobot:",
                f"    id: {name}-id",
                "    version: 1.0.0",
                f"    status: {status}",
                f"    category: {category}",
                f"    risk_level: {risk_level}",
                f"    requires_exec: {'true' if requires_exec else 'false'}",
                "---",
                "",
                f"# {name}",
                f"Use {name}.",
            ]
        ),
        encoding="utf-8",
    )


def test_system_skill_status_transition_and_update_api_rejected(tmp_path):
    system_dir = tmp_path / "system-skills"
    _write_skill(system_dir, "locked-system", status="system", category="system.test")

    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty-builtin", system_dir=system_dir)

    with pytest.raises(ValueError, match="system skill"):
        skill_manage_status_payload(tmp_path, "locked-system", "deprecate")

    markdown = (system_dir / "locked-system" / "SKILL.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="system skill"):
        skill_manage_update_payload(tmp_path, "locked-system", markdown + "\n", dry_run=True)


@pytest.mark.asyncio
async def test_draft_skills_are_not_exposed_to_skill_search(tmp_path):
    skills = tmp_path / "skills"
    _write_skill(
        skills,
        "draft-only",
        status="draft",
        category="test.draft",
        description="draft-only unique routing phrase",
    )
    _write_skill(
        skills,
        "candidate-visible",
        status="candidate",
        category="test.candidate",
        description="candidate visible routing phrase",
    )

    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty-builtin")

    tool = SkillSearchTool(str(tmp_path))
    tool.set_context(RequestContext(channel="test", chat_id="c1", session_key="test:c1"))
    payload = await tool.execute(
        queries=[{"query": "draft-only unique routing phrase", "top_k": 5}]
    )
    data = json.loads(payload)
    candidate_names = [
        candidate["name"]
        for result in data["results"]
        for candidate in result["candidates"]
    ]

    assert "draft-only" not in candidate_names
    assert all(match.name != "draft-only" for match in store.search("draft-only unique routing phrase", top_k=5))


def test_researcher_profile_registry_has_no_exec_tool(tmp_path):
    from nanobot.agent.subagent import SubagentManager

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    researcher = SubagentProfile(
        description="Research and summarize documents",
        tools=["read_file", "grep", "web_search", "web_fetch"],
        can_spawn=False,
    )
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        profiles={"researcher": researcher},
    )

    tools = mgr._build_tools(profile=researcher, depth=1)

    assert tools.has("read_file")
    assert not tools.has("exec")


@pytest.mark.asyncio
async def test_depth_exceeded_spawn_is_rejected(tmp_path):
    from nanobot.agent.subagent import SubagentManager

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    profile = SubagentProfile(description="Code tasks", tools=["read_file"], can_spawn=True)
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        profiles={"coder": profile},
        max_depth=2,
    )
    mgr.runner.run = MagicMock(return_value=SimpleNamespace(stop_reason="done", final_content="done", error=None))
    tool = SpawnTool(mgr, depth=2)
    tool.set_context(RequestContext(channel="test", chat_id="c1", session_key="test:c1"))

    result = await tool.execute(task="nested task", profile="coder", expected_output="done")

    assert "Cannot spawn" in result
    assert "max subagent depth" in result


@pytest.mark.asyncio
async def test_dependent_delegate_requires_prior_wave_context(tmp_path):
    from nanobot.agent.subagent import SubagentManager

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    profile = SubagentProfile(description="Review tasks", tools=["read_file"], can_spawn=False)
    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=AgentDefaults().max_tool_result_chars,
        profiles={"reviewer": profile},
    )
    mgr.delegate = MagicMock(return_value="should not run")
    tool = DelegateTool(mgr)
    tool.set_context(RequestContext(channel="test", chat_id="c1", session_key="test:c1"))

    result = await tool.execute(
        profile="reviewer",
        task="요약 결과를 바탕으로 사업성 검토를 수행하라",
        expected_output="prior wave summary를 사용한 검토",
        context="",
    )

    assert "prior wave output" in result
    mgr.delegate.assert_not_called()
