"""Track file-read state for read-before-edit warnings and read deduplication."""

from __future__ import annotations

import hashlib
import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ReadState:
    mtime: float
    offset: int
    limit: int | None
    content_hash: str | None
    can_dedup: bool


def _hash_file(p: str) -> str | None:
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class FileStates:
    """Per-session read/write tracker.

    Owns its own state dict so read-dedup ("File unchanged since last read")
    and read-before-edit warnings stay scoped to one agent session and do
    not leak across sessions sharing this process.
    """

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state: dict[str, ReadState] = {}

    def record_read(self, path: str | Path, offset: int = 1, limit: int | None = None,
                    *, content: bytes | None = None) -> None:
        """Record that a file was read (called after successful read).

        Pass the bytes the caller already read as ``content`` to hash them directly
        instead of re-opening and re-reading the file from disk for this alone.
        """
        p = str(Path(path).resolve())
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            return
        self._state[p] = ReadState(
            mtime=mtime,
            offset=offset,
            limit=limit,
            content_hash=_hash_bytes(content) if content is not None else _hash_file(p),
            can_dedup=True,
        )

    def record_write(self, path: str | Path) -> None:
        """Record that a file was written (updates mtime in state)."""
        p = str(Path(path).resolve())
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            self._state.pop(p, None)
            return
        self._state[p] = ReadState(
            mtime=mtime,
            offset=1,
            limit=None,
            content_hash=_hash_file(p),
            can_dedup=False,
        )

    def check_read(self, path: str | Path) -> str | None:
        """Check if a file has been read and is fresh.

        Returns None if OK, or a warning string.
        When mtime changed but file content is identical (e.g. touch, editor save),
        the check passes to avoid false-positive staleness warnings.
        """
        p = str(Path(path).resolve())
        entry = self._state.get(p)
        if entry is None:
            return "Warning: file has not been read yet. Read it first to verify content before editing."
        try:
            current_mtime = os.path.getmtime(p)
        except OSError:
            return None
        if current_mtime != entry.mtime:
            if entry.content_hash and _hash_file(p) == entry.content_hash:
                entry.mtime = current_mtime
                return None
            return "Warning: file has been modified since last read. Re-read to verify content before editing."
        # mtime unchanged - still check content hash to detect quick modifications
        if entry.content_hash and _hash_file(p) != entry.content_hash:
            return "Warning: file has been modified since last read. Re-read to verify content before editing."
        return None

    def is_unchanged(self, path: str | Path, offset: int = 1, limit: int | None = None) -> bool:
        """Return True if file was previously read with same params and content is unchanged."""
        p = str(Path(path).resolve())
        entry = self._state.get(p)
        if entry is None:
            return False
        if not entry.can_dedup:
            return False
        if entry.offset != offset or entry.limit != limit:
            return False
        try:
            current_mtime = os.path.getmtime(p)
        except OSError:
            return False
        if current_mtime != entry.mtime:
            # mtime changed - check if content also changed
            current_hash = _hash_file(p)
            if current_hash != entry.content_hash:
                # Content actually changed - don't dedup
                entry.can_dedup = False
                return False
            # Content identical despite mtime change (e.g. touch) - mark as not dedupable to force full read next time
            entry.can_dedup = False
            return True
        # mtime unchanged - content must be identical
        return True

    def get(self, path: str | Path) -> ReadState | None:
        """Return the raw ReadState entry for a path, or None."""
        return self._state.get(str(Path(path).resolve()))

    def clear(self) -> None:
        """Clear all tracked state (useful for testing)."""
        self._state.clear()


class FileStateStore:
    """Lookup table for per-session file read/write state."""

    __slots__ = ("_states_by_key",)

    def __init__(self) -> None:
        self._states_by_key: dict[str, tuple[Any, FileStates]] = {}

    def for_session(self, session_key: str | None, epoch: Any = 0) -> FileStates:
        """Read/write state of one session.

        ``epoch`` identifies the part of the history the model can still see. When it changes
        (``/new``, trimming, compaction), earlier reads are no longer in the model's context, so
        the tracking starts fresh: a repeated read returns the content again instead of a
        "file unchanged" stub that points at text the model no longer has. Only the latest
        state per session is kept, which also bounds memory.
        """
        key = session_key or "__default__"
        entry = self._states_by_key.get(key)
        if entry is None or entry[0] != epoch:
            entry = (epoch, FileStates())
            self._states_by_key[key] = entry
        return entry[1]

    def clear(self) -> None:
        self._states_by_key.clear()


_current_file_states: ContextVar[FileStates | None] = ContextVar(
    "nanobot_file_states",
    default=None,
)


def current_file_states(default: FileStates) -> FileStates:
    """Return the FileStates bound to the current agent task, or a fallback."""
    return _current_file_states.get() or default


def bind_file_states(file_states: FileStates) -> Token[FileStates | None]:
    """Bind file read/write state for the current async task."""
    return _current_file_states.set(file_states)


def reset_file_states(token: Token[FileStates | None]) -> None:
    _current_file_states.reset(token)


# Module-level default instance, retained for backward compatibility with
# tests and callers that reach in directly. Per-session callers should hold
# their own FileStates instance instead of touching this one.
_default = FileStates()


def record_read(path: str | Path, offset: int = 1, limit: int | None = None,
                *, content: bytes | None = None) -> None:
    _default.record_read(path, offset=offset, limit=limit, content=content)


def record_write(path: str | Path) -> None:
    _default.record_write(path)


def check_read(path: str | Path) -> str | None:
    return _default.check_read(path)


def is_unchanged(path: str | Path, offset: int = 1, limit: int | None = None) -> bool:
    return _default.is_unchanged(path, offset=offset, limit=limit)


def clear() -> None:
    _default.clear()


# Legacy attribute for callers that reached into the module-level dict
# directly (filesystem.py used to do this). Kept as a property-like accessor
# so existing imports keep working.
def __getattr__(name: str):
    if name == "_state":
        return _default._state
    raise AttributeError(name)
