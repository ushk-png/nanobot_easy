"""Tests for AgentLoop._resolve_pending_skill_confirmation.

This is the deterministic gate that turns a user's plain yes/no reply into an
actual skill-draft approval — it must never involve the LLM. These tests call
it directly, the same way tests/command/test_skill_command.py calls cmd_skill
directly, so they exercise the real approval wiring without needing a mocked
LLM turn.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ModelPresetConfig
from nanobot.session.manager import Session
from nanobot.session.skill_approval_state import (
    get_pending_skill_approval,
    set_pending_skill_approval,
)
from nanobot.skill_store import SkillStore


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = MagicMock()
    provider.generation.max_tokens = 4096
    provider.generation.temperature = 0.1
    provider.generation.reasoning_effort = None
    return provider


def _make_loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=8000,
        model_presets={
            "default": ModelPresetConfig(
                model="test-model",
                max_tokens=4096,
                context_window_tokens=8000,
            ),
        },
    )


def _write_draft_skill(root: Path, name: str) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n---\n\n# {name}\nUse this skill.\n",
        encoding="utf-8",
    )


def _msg(content: str) -> InboundMessage:
    return InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=content)


@pytest.mark.asyncio
async def test_no_pending_state_falls_through(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = Session(key="cli:direct")

    result = await loop._resolve_pending_skill_confirmation(_msg("yes"), session, "yes")
    assert result is None


@pytest.mark.asyncio
async def test_pending_state_ignores_unrelated_reply(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = Session(key="cli:direct")
    set_pending_skill_approval(session.metadata, name="gcalcli-calendar", source="file")

    result = await loop._resolve_pending_skill_confirmation(
        _msg("what's the weather"), session, "what's the weather"
    )
    assert result is None
    # Ambiguous replies must not clear a still-pending confirmation.
    assert get_pending_skill_approval(session.metadata) is not None


@pytest.mark.asyncio
async def test_yes_reply_approves_materialized_draft(tmp_path: Path) -> None:
    _write_draft_skill(tmp_path, "gcalcli-calendar")
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    assert store.get_skill("gcalcli-calendar")["status"] == "draft"

    loop = _make_loop(tmp_path)
    session = Session(key="cli:direct")
    set_pending_skill_approval(session.metadata, name="gcalcli-calendar", source="file")

    result = await loop._resolve_pending_skill_confirmation(_msg("네"), session, "네")

    assert result is not None
    assert "Approved" in result.content
    assert "gcalcli-calendar" in result.content
    assert get_pending_skill_approval(session.metadata) is None
    assert store.get_skill("gcalcli-calendar")["status"] == "candidate"


@pytest.mark.asyncio
async def test_no_reply_cancels_without_approving(tmp_path: Path) -> None:
    _write_draft_skill(tmp_path, "gcalcli-calendar")
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")

    loop = _make_loop(tmp_path)
    session = Session(key="cli:direct")
    set_pending_skill_approval(session.metadata, name="gcalcli-calendar", source="file")

    result = await loop._resolve_pending_skill_confirmation(_msg("아니요"), session, "아니요")

    assert result is not None
    assert "Cancelled" in result.content
    assert get_pending_skill_approval(session.metadata) is None
    assert store.get_skill("gcalcli-calendar")["status"] == "draft"


@pytest.mark.asyncio
async def test_expired_pending_state_falls_through_even_on_yes(tmp_path: Path) -> None:
    _write_draft_skill(tmp_path, "gcalcli-calendar")
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")

    loop = _make_loop(tmp_path)
    session = Session(key="cli:direct")
    set_pending_skill_approval(session.metadata, name="gcalcli-calendar", source="file", ttl_s=-1)

    result = await loop._resolve_pending_skill_confirmation(_msg("yes"), session, "yes")

    assert result is None
    assert store.get_skill("gcalcli-calendar")["status"] == "draft"


@pytest.mark.asyncio
async def test_yes_reply_approves_composer_draft_and_materializes_file(tmp_path: Path) -> None:
    store = SkillStore(tmp_path)
    draft = store.create_skill_draft(
        name="standup-notes",
        description="Summarize standup notes.",
        trigger="standup notes",
        method="# Standup Notes\n\n## Method\n1. Summarize.\n",
        category="notes.standup",
    )

    loop = _make_loop(tmp_path)
    session = Session(key="cli:direct")
    set_pending_skill_approval(
        session.metadata, name="standup-notes", source="composed", draft_id=draft.draft_id
    )

    result = await loop._resolve_pending_skill_confirmation(_msg("ok"), session, "ok")

    assert result is not None
    assert "Approved" in result.content
    assert (tmp_path / "skills" / "standup-notes" / "SKILL.md").exists()
    assert store.get_skill("standup-notes")["status"] == "candidate"


@pytest.mark.asyncio
async def test_yes_reply_for_a_since_deleted_draft_reports_not_pending(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    session = Session(key="cli:direct")
    # Nothing was ever created on disk or in the registry for this name.
    set_pending_skill_approval(session.metadata, name="ghost-draft", source="file")

    result = await loop._resolve_pending_skill_confirmation(_msg("yes"), session, "yes")

    assert result is not None
    assert "no longer pending" in result.content
    assert get_pending_skill_approval(session.metadata) is None
