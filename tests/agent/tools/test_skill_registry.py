import json
from pathlib import Path

import pytest

from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.skill_registry import SkillRegistryTool
from nanobot.skill_store import SkillStore


def _write_skill(root: Path, rel: str, *, name: str) -> None:
    skill_dir = root / "skills" / rel
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: {name} description
metadata:
  nanobot:
    id: test-{name}
    version: 1.0.0
    category: test.skill
    risk_level: low
    requires_exec: false
---

# {name}

## When to use
Use for {name}.

## Method
1. Do the thing.
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_skill_registry_lists_registry_names_not_scoped_parent_dirs(tmp_path: Path) -> None:
    _write_skill(tmp_path, "@steipete/obsidian", name="obsidian")
    SkillStore(tmp_path).reindex(builtin_dir=tmp_path / "empty", system_dir=tmp_path / "empty-system")

    raw = await SkillRegistryTool(str(tmp_path)).execute()
    payload = json.loads(raw)

    names = {row["name"] for row in payload["skills"]}
    assert "obsidian" in names
    assert "@steipete" not in names
    assert payload["summary"]["by_status"]["draft"] == 1
    assert payload["source_of_truth"] == "workspace/.skillstore/skillstore.db"


@pytest.mark.asyncio
async def test_skill_registry_filters_status_and_source(tmp_path: Path) -> None:
    _write_skill(tmp_path, "active-one", name="active-one")
    _write_skill(tmp_path, "old-one", name="old-one")
    SkillStore(tmp_path).reindex(builtin_dir=tmp_path / "empty", system_dir=tmp_path / "empty-system")

    raw = await SkillRegistryTool(str(tmp_path)).execute(status="draft", source="workspace", limit=1)
    payload = json.loads(raw)

    assert len(payload["skills"]) == 1
    assert payload["skills"][0]["status"] == "draft"
    assert payload["skills"][0]["source"] == "workspace"
    assert payload["summary"]["filtered"] == 2
    assert payload["summary"]["returned"] == 1


def test_skill_registry_tool_is_registered(tmp_path: Path) -> None:
    registry = ToolRegistry()
    ctx = type("Ctx", (), {"workspace": str(tmp_path)})()

    ToolLoader().load(ctx, registry)

    assert registry.has("skill_registry")
