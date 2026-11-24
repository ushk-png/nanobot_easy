"""Suite-wide guards."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _no_stray_files_in_cwd():
    """Fail if the suite leaves new files or directories in the working directory.

    A test that builds a path from a MagicMock (e.g. ``workspace = MagicMock()``) writes
    relative junk such as ``<MagicMock id=...>/`` into the cwd instead of failing loudly.
    """
    cwd = Path(os.getcwd())

    def snapshot() -> set[str]:
        return {p.name for p in cwd.iterdir() if not p.name.startswith(".") and p.name != "__pycache__"}

    before = snapshot()
    yield
    added = sorted(snapshot() - before)
    assert not added, f"tests left stray entries in {cwd}: {added}"
