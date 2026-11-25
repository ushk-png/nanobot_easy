"""Suite-wide guards."""

from __future__ import annotations

import os
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _tiktoken_encoding_available() -> bool:
    """True when the cl100k_base encoding loads (network, or a cached copy)."""
    try:
        import tiktoken

        tiktoken.get_encoding("cl100k_base")
    except Exception:
        return False
    return True


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_tiktoken_encoding: needs the real cl100k_base encoding; skipped when it "
        "cannot be loaded (offline without a cached copy)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    marked = [item for item in items if item.get_closest_marker("requires_tiktoken_encoding")]
    if not marked or _tiktoken_encoding_available():
        return
    skip = pytest.mark.skip(reason="cl100k_base tiktoken encoding unavailable (offline, no cache)")
    for item in marked:
        item.add_marker(skip)
