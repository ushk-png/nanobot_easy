import json
from pathlib import Path

from nanobot.memory.importer import import_history_jsonl
from nanobot.memory.models import MemoryScope
from nanobot.memory.raw_reader import read_memory_events
from nanobot.memory.search import MemorySearcher
from nanobot.memory.event_store import ConversationEventStore


def test_import_history_jsonl_is_idempotent_and_searchable(tmp_path: Path):
    workspace = tmp_path / "workspace"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    history = memory_dir / "history.jsonl"
    rows = [
        {
            "cursor": 1,
            "timestamp": "2026-08-17 10:00",
            "content": "- [durable] Conversation RAG Memory는 raw event store를 사용한다.",
            "session_key": "telegram:8580974491",
        },
        {
            "cursor": 2,
            "timestamp": "2026-08-17 10:05",
            "content": "- [ephemeral] registry.py 테스트가 통과했다.",
            "session_key": "websocket:abc",
        },
    ]
    history.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")

    report = import_history_jsonl(workspace=workspace)
    assert report.inserted == 2
    assert report.skipped == 0
    assert report.errors == ()

    second = import_history_jsonl(workspace=workspace)
    assert second.inserted == 0
    assert second.skipped == 2

    store = ConversationEventStore(workspace)
    scope = MemoryScope.from_runtime(owner_id="8580974491", workspace_id=str(workspace.resolve()), agent_id=None)
    result = MemorySearcher(store).search(scope=scope, query="Conversation RAG Memory raw event store", limit=5)
    ids = [event.event_id for window in result.windows for event in window.events]
    assert "legacy-history-jsonl:1" in ids
    assert "legacy-history-jsonl:2" not in ids

    raw = read_memory_events(store, scope, ["legacy-history-jsonl:1"])
    assert raw[0].event_type == "CURATED_MEMORY_EDIT"
    assert "raw event store" in (raw[0].content or "")


def test_import_history_jsonl_dry_run_does_not_write(tmp_path: Path):
    workspace = tmp_path / "workspace"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    history = memory_dir / "history.jsonl"
    history.write_text(json.dumps({"cursor": 7, "timestamp": "2026-08-17", "content": "dry run", "session_key": "telegram:chat"}), encoding="utf-8")

    report = import_history_jsonl(workspace=workspace, dry_run=True)
    assert report.inserted == 1

    store = ConversationEventStore(workspace)
    scope = MemoryScope.from_runtime(owner_id="chat", workspace_id=str(workspace.resolve()), agent_id=None)
    assert read_memory_events(store, scope, ["legacy-history-jsonl:7"]) == []
