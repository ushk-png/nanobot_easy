import json
from pathlib import Path

import pytest

from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.skill_request_approval import SkillRequestApprovalTool
from nanobot.session.manager import SessionManager
from nanobot.session.skill_approval_state import get_pending_skill_approval
from nanobot.skill_store import SkillStore


def _write_draft_skill(root: Path, name: str) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n\n# {name}\nUse this skill.\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_sets_pending_approval_for_a_materialized_draft(tmp_path: Path) -> None:
    _write_draft_skill(tmp_path, "gcalcli-calendar")
    SkillStore(tmp_path).reindex(builtin_dir=tmp_path / "empty_builtin")

    sessions = SessionManager(tmp_path)
    tool = SkillRequestApprovalTool(workspace=str(tmp_path), sessions=sessions)
    tool.set_context(RequestContext(channel="cli", chat_id="direct", session_key="cli:direct"))

    raw = await tool.execute(name="gcalcli-calendar")
    payload = json.loads(raw)
    assert payload["ok"] is True

    session = sessions.get_or_create("cli:direct")
    pending = get_pending_skill_approval(session.metadata)
    assert pending is not None
    assert pending["name"] == "gcalcli-calendar"
    assert pending["source"] == "file"


@pytest.mark.asyncio
async def test_unknown_name_does_not_set_pending_state(tmp_path: Path) -> None:
    sessions = SessionManager(tmp_path)
    tool = SkillRequestApprovalTool(workspace=str(tmp_path), sessions=sessions)
    tool.set_context(RequestContext(channel="cli", chat_id="direct", session_key="cli:direct"))

    raw = await tool.execute(name="does-not-exist")
    payload = json.loads(raw)
    assert payload["ok"] is False

    session = sessions.get_or_create("cli:direct")
    assert get_pending_skill_approval(session.metadata) is None


@pytest.mark.asyncio
async def test_composer_draft_records_draft_id(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    store.create_skill_draft(
        name="standup-notes",
        description="Summarize standup notes.",
        trigger="standup notes",
        method="# Standup Notes\n\n## Method\n1. Summarize.\n",
        category="notes.standup",
    )

    sessions = SessionManager(tmp_path)
    tool = SkillRequestApprovalTool(workspace=str(tmp_path), sessions=sessions)
    tool.set_context(RequestContext(channel="cli", chat_id="direct", session_key="cli:direct"))

    raw = await tool.execute(name="standup-notes")
    payload = json.loads(raw)
    assert payload["ok"] is True

    session = sessions.get_or_create("cli:direct")
    pending = get_pending_skill_approval(session.metadata)
    assert pending is not None
    assert pending["source"] == "composed"
    assert pending["draft_id"]


def test_tool_requires_a_sessions_manager_to_register(tmp_path: Path) -> None:
    registry = ToolRegistry()
    ctx_without_sessions = type("Ctx", (), {"workspace": str(tmp_path)})()
    ToolLoader().load(ctx_without_sessions, registry)
    assert not registry.has("skill_request_approval")

    registry_with_sessions = ToolRegistry()
    ctx_with_sessions = type(
        "Ctx", (), {"workspace": str(tmp_path), "sessions": SessionManager(tmp_path)}
    )()
    ToolLoader().load(ctx_with_sessions, registry_with_sessions)
    assert registry_with_sessions.has("skill_request_approval")
