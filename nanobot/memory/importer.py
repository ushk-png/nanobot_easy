"""Import legacy memory files into the raw-event conversation memory DB.

The importer preserves legacy records as historical evidence. Summaries from
``history.jsonl`` are not treated as the original conversation transcript; they
are imported as ``CURATED_MEMORY_EDIT`` events with source metadata so retrieval
can distinguish them from newly captured raw USER/ASSISTANT/TOOL events.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope, RawEvent


@dataclass(frozen=True)
class ImportReport:
    source_path: str
    inserted: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "inserted": self.inserted,
            "skipped": self.skipped,
            "errors": list(self.errors),
        }


def _owner_from_session_key(session_key: str | None, fallback: str) -> str:
    if session_key and ":" in session_key:
        tail = session_key.split(":", 1)[1].strip()
        if tail:
            return tail
    return fallback


def _event_id_for_history_cursor(cursor: Any) -> str:
    safe = str(cursor).strip() or "unknown"
    return f"legacy-history-jsonl:{safe}"


def _normalize_ts(value: Any) -> str:
    text = str(value or "").strip()
    return text or ConversationEventStore.now_ts()


def _load_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any] | None, str | None]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                yield line_no, None, f"line {line_no}: invalid JSON: {exc}"
                continue
            if not isinstance(data, dict):
                yield line_no, None, f"line {line_no}: expected object, got {type(data).__name__}"
                continue
            yield line_no, data, None


def import_history_jsonl(
    *,
    workspace: str | Path,
    history_path: str | Path | None = None,
    db_path: str | Path | None = None,
    owner_id: str | None = None,
    workspace_id: str | None = None,
    agent_id: str | None = None,
    dry_run: bool = False,
) -> ImportReport:
    """Import Dream-managed ``memory/history.jsonl`` rows as scoped events.

    The importer is idempotent. Each row uses ``legacy-history-jsonl:{cursor}``
    as event_id, so re-running skips existing rows.
    """
    workspace_path = Path(workspace).expanduser().resolve()
    source = Path(history_path).expanduser() if history_path else workspace_path / "memory" / "history.jsonl"
    source = source.resolve()
    if not source.exists():
        return ImportReport(source_path=str(source), errors=(f"source not found: {source}",))

    store = ConversationEventStore(workspace_path, db_path=db_path)
    effective_workspace_id = str(Path(workspace_id).expanduser().resolve()) if workspace_id else str(workspace_path)
    inserted = 0
    skipped = 0
    errors: list[str] = []
    events: list[RawEvent] = []

    for line_no, row, error in _load_jsonl(source):
        if error:
            errors.append(error)
            continue
        assert row is not None
        cursor = row.get("cursor", line_no)
        event_id = _event_id_for_history_cursor(cursor)
        session_key = str(row.get("session_key") or "legacy-history")
        row_owner_id = str(owner_id or _owner_from_session_key(session_key, "legacy"))
        scope = MemoryScope.from_runtime(
            owner_id=row_owner_id,
            workspace_id=effective_workspace_id,
            agent_id=agent_id,
        )
        if store.get_events(scope, [event_id]):
            skipped += 1
            continue
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            errors.append(f"line {line_no}: missing non-empty content")
            continue
        metadata = {
            "source": "memory/history.jsonl",
            "source_path": str(source),
            "cursor": cursor,
            "session_key": session_key,
            "legacy_summary": True,
            "note": "Imported legacy Dream history summary; not raw conversation transcript.",
        }
        event = RawEvent(
            event_id=event_id,
            owner_id=scope.owner_id,
            workspace_id=scope.workspace_ids[0],
            agent_id=scope.agent_ids[0],
            conversation_id=session_key,
            session_id=session_key,
            sequence=int(cursor) if isinstance(cursor, int) or str(cursor).isdigit() else line_no,
            ts=_normalize_ts(row.get("timestamp")),
            actor="dream",
            event_type="CURATED_MEMORY_EDIT",
            content=content,
            metadata_json=ConversationEventStore._json_dumps(metadata),
            parent_event_id=None,
            content_hash=ConversationEventStore._hash_content(content),
        )
        events.append(event)

    if not dry_run and events:
        store.insert_events(events)
    inserted = len(events)
    if dry_run:
        skipped += 0
    return ImportReport(source_path=str(source), inserted=inserted, skipped=skipped, errors=tuple(errors))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m nanobot.memory")
    sub = parser.add_subparsers(dest="cmd")
    hist = sub.add_parser("history-jsonl", help="Import workspace memory/history.jsonl into conversation_memory.db")
    hist.add_argument("--workspace", type=Path, default=Path("."))
    hist.add_argument("--history-path", type=Path)
    hist.add_argument("--db-path", type=Path)
    hist.add_argument("--owner-id")
    hist.add_argument("--workspace-id")
    hist.add_argument("--agent-id")
    hist.add_argument("--dry-run", action="store_true")

    cleanup = sub.add_parser(
        "redact-memory-tool-results",
        help="Redact stored search_memory/read_memory_events TOOL_RESULT payloads and rebuild derived indexes.",
    )
    cleanup.add_argument("--workspace", type=Path, default=Path("."))
    cleanup.add_argument("--db-path", type=Path)
    cleanup.add_argument(
        "--keep-content",
        action="store_true",
        help="Only rebuild derived indexes; do not purge memory-tool result content.",
    )

    rebuild = sub.add_parser("rebuild-derived-indexes", help="Rebuild conversation-memory FTS/chunk/entity indexes from stored events.")
    rebuild.add_argument("--workspace", type=Path, default=Path("."))
    rebuild.add_argument("--db-path", type=Path)

    args = parser.parse_args(argv)
    if args.cmd == "history-jsonl":
        report = import_history_jsonl(
            workspace=args.workspace,
            history_path=args.history_path,
            db_path=args.db_path,
            owner_id=args.owner_id,
            workspace_id=args.workspace_id,
            agent_id=args.agent_id,
            dry_run=args.dry_run,
        )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    elif args.cmd == "redact-memory-tool-results":
        store = ConversationEventStore(args.workspace, db_path=args.db_path)
        count = store.redact_memory_tool_results(purge_content=not args.keep_content)
        print(json.dumps({"redacted_memory_tool_result_events": count}, ensure_ascii=False, indent=2))
    elif args.cmd == "rebuild-derived-indexes":
        store = ConversationEventStore(args.workspace, db_path=args.db_path)
        store.rebuild_derived_indexes()
        print(json.dumps({"rebuilt_derived_indexes": True}, ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
