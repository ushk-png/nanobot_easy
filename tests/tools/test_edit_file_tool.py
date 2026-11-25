"""edit_file must never lose lines while reporting success (review N01)."""

from __future__ import annotations

import pytest

from nanobot.agent.tools.filesystem import EditFileTool


@pytest.mark.asyncio
async def test_replace_all_with_overlapping_trim_matches_never_drops_lines(tmp_path) -> None:
    """Review N01: overlapping fallback windows were spliced with stale offsets."""
    target = tmp_path / "x.py"
    tool = EditFileTool(workspace=tmp_path, allowed_dir=tmp_path)

    target.write_text("  a\n  a\n  a\nkeep\n", encoding="utf-8")
    result = await tool.execute(path=str(target), old_text="a\na", new_text="X", replace_all=True)
    assert "Successfully edited" in str(result)
    # Two lines became one; the third `a` and `keep` must both survive.
    assert target.read_text(encoding="utf-8") == "  X\n  a\nkeep\n"

    target.write_text("  a\n  a\n  a\nkeep\n", encoding="utf-8")
    result = await tool.execute(
        path=str(target), old_text="a\na", new_text="X", replace_all=True, expected_replacements=2
    )
    assert "expected 2 replacements but would make 1" in str(result)
    assert target.read_text(encoding="utf-8") == "  a\n  a\n  a\nkeep\n"


def test_loose_matches_never_overlap() -> None:
    from nanobot.agent.tools.filesystem import _find_matches

    matches = _find_matches("  a\n  a\n  a\nkeep\n", "a\na")
    assert [(m.start, m.end) for m in matches] == [(0, 7)]
    for left, right in zip(matches, matches[1:]):
        assert left.end <= right.start


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["sessions", "Sessions", "SESSIONS", ".SkillStore", "CRON"])
async def test_hardened_mode_hides_internal_state_in_any_letter_case(tmp_path, spelling) -> None:
    """Review N04: on a case-insensitive filesystem another spelling reached the same folder."""
    from nanobot.agent.tools.filesystem import ReadFileTool

    workspace = tmp_path.resolve()
    reader = ReadFileTool(workspace=workspace, allowed_dir=workspace, lock_workspace=True)
    result = await reader.execute(path=f"{spelling}/state.json")
    assert "Internal runtime state is unavailable" in str(result)
