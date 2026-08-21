from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import AgentDefaults
from nanobot.session.manager import Session
from nanobot.skill_store import SkillStore


def _write_skill(workspace: Path, name: str, *, description: str, body: str) -> None:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join([
            "---",
            f"name: {name}",
            f"description: {description}",
            "metadata:",
            "  nanobot:",
            f"    id: test-{name}",
            "    version: 1.0.0",
            "    category: document.meeting",
            "    risk_level: low",
            "    requires_exec: false",
            "triggers:",
            "  - meeting minutes",
            "---",
            "",
            body,
        ]),
        encoding="utf-8",
    )


def _loop(workspace: Path, **kwargs) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace, model="test-model", **kwargs)


def test_agent_defaults_keep_proactive_skill_cards_off() -> None:
    defaults = AgentDefaults()

    assert defaults.proactive_skill_cards is False
    assert defaults.proactive_card_min_score == 35
    assert defaults.proactive_method_inline is False


def test_proactive_skill_cards_not_injected_when_disabled(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "meeting-minutes",
        description="meeting minutes structured notes",
        body="# Meeting Minutes\n\n## Method\n\nWrite decisions and action items.",
    )
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    store.approve_draft("meeting-minutes")

    loop = _loop(tmp_path, proactive_skill_cards=False)
    session = Session(key="cli:test")
    msg = InboundMessage(channel="cli", chat_id="test", sender_id="user", content="meeting minutes")

    messages = loop._build_initial_messages(msg, session, [], None)

    assert ContextBuilder._SKILL_CANDIDATES_TAG not in str(messages[-1]["content"])


def test_proactive_skill_cards_injected_when_enabled(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "meeting-minutes",
        description="meeting minutes structured notes",
        body="# Meeting Minutes\n\n## Method\n\nWrite decisions and action items.",
    )
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    store.approve_draft("meeting-minutes")

    loop = _loop(tmp_path, proactive_skill_cards=True, proactive_card_min_score=1)
    session = Session(key="cli:test")
    msg = InboundMessage(channel="cli", chat_id="test", sender_id="user", content="meeting minutes")

    messages = loop._build_initial_messages(msg, session, [], None)
    content = str(messages[-1]["content"])

    assert ContextBuilder._SKILL_CANDIDATES_TAG in content
    assert "meeting-minutes" in content
    assert "retrieval hints only" in content


def test_proactive_skill_cards_inline_method_only_for_strong_unambiguous_match(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "meeting-minutes",
        description="meeting minutes structured notes",
        body="# Meeting Minutes\n\n## Method\n\nWrite decisions and action items.",
    )
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    store.approve_draft("meeting-minutes")

    loop = _loop(
        tmp_path,
        proactive_skill_cards=True,
        proactive_card_min_score=1,
        proactive_method_inline=True,
    )
    session = Session(key="cli:test")
    msg = InboundMessage(channel="cli", chat_id="test", sender_id="user", content="meeting minutes")

    messages = loop._build_initial_messages(msg, session, [], None)
    content = str(messages[-1]["content"])

    assert "Method excerpt" in content
    assert "Write decisions and action items." in content


def test_proactive_skill_cards_skipped_below_threshold(tmp_path: Path) -> None:
    _write_skill(
        tmp_path,
        "meeting-minutes",
        description="meeting minutes structured notes",
        body="# Meeting Minutes\n\n## Method\n\nWrite decisions and action items.",
    )
    store = SkillStore(tmp_path)
    store.reindex(builtin_dir=tmp_path / "empty_builtin")
    store.approve_draft("meeting-minutes")

    loop = _loop(tmp_path, proactive_skill_cards=True, proactive_card_min_score=101)
    session = Session(key="cli:test")
    msg = InboundMessage(channel="cli", chat_id="test", sender_id="user", content="meeting minutes")

    messages = loop._build_initial_messages(msg, session, [], None)

    assert ContextBuilder._SKILL_CANDIDATES_TAG not in str(messages[-1]["content"])
