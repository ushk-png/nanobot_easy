"""One unreadable file must not fail a search over the whole tree (review N08)."""

from __future__ import annotations

import os

import pytest

from nanobot.agent.tools.search import GrepTool


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions and a non-root user")
async def test_grep_skips_an_unreadable_file_and_reports_it(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("needle here\n")
    (tmp_path / "b.txt").write_text("needle too\n")
    locked = tmp_path / "locked.txt"
    locked.write_text("needle hidden\n")
    os.chmod(locked, 0)
    try:
        result = await GrepTool(workspace=tmp_path, allowed_dir=tmp_path).execute(
            pattern="needle", path=".", output_mode="files_with_matches"
        )
    finally:
        os.chmod(locked, 0o600)
    text = str(result)
    assert "a.txt" in text and "b.txt" in text
    assert "locked.txt" not in text
    assert "skipped 1 unreadable" in text
    assert not getattr(result, "is_error", False)
