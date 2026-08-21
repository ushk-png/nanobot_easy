"""Structured local storage for student-mode learning data."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_key(value: str) -> str:
    return " ".join(value.strip().lower().split())


@dataclass(slots=True)
class StudyLogEntry:
    """One structured study-log row."""

    subject: str
    concept: str
    source: str = ""
    difficulty: str = ""
    student_attempt: str = ""
    next_action: str = ""
    date: str = field(default_factory=_now_iso)


def append_study_log(path: str | Path, entry: StudyLogEntry) -> None:
    """Append one JSONL study-log entry to *path*."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(entry), ensure_ascii=False, separators=(",", ":")) + "\n")


class ReviewQueueStore:
    """JSONL-backed review queue keyed by subject + concept."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    @staticmethod
    def review_key(subject: str, concept: str) -> str:
        """Return the stable dedupe key for one review concept."""
        return f"{_normalize_key(subject)}::{_normalize_key(concept)}"

    def load(self) -> list[dict[str, Any]]:
        """Load queue rows. Malformed blank lines are ignored."""
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        return rows

    def save(self, rows: list[dict[str, Any]]) -> None:
        """Rewrite the queue atomically enough for local single-user use."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def upsert(
        self,
        *,
        subject: str,
        concept: str,
        due_date: str,
        source: str = "",
        registered_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Insert or update one review target.

        Duplicate detection intentionally ignores date. Re-registering the same
        subject/concept appends history to the existing concept row.
        """
        key = self.review_key(subject, concept)
        now = registered_at or _now_iso()
        rows = self.load()
        for row in rows:
            if row.get("key") != key:
                continue
            row["subject"] = subject
            row["concept"] = concept
            row["source"] = source or row.get("source", "")
            row["due_date"] = due_date
            row.setdefault("review_history", []).append({
                "event": "registered",
                "date": now,
                "due_date": due_date,
                "source": source,
            })
            if metadata:
                row.setdefault("metadata", {}).update(metadata)
            self.save(rows)
            return row

        row = {
            "key": key,
            "subject": subject,
            "concept": concept,
            "source": source,
            "due_date": due_date,
            "created_at": now,
            "review_history": [{
                "event": "registered",
                "date": now,
                "due_date": due_date,
                "source": source,
            }],
        }
        if metadata:
            row["metadata"] = metadata
        rows.append(row)
        self.save(rows)
        return row

    def due(self, date: str) -> list[dict[str, Any]]:
        """Return rows whose due date is due on or before YYYY-MM-DD *date*."""
        return [row for row in self.load() if str(row.get("due_date", "")) <= date]
