"""Command-line evaluation runner for conversation memory retrieval."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

import yaml

from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.eval.metrics import compute_retrieval_metrics
from nanobot.memory.models import MemoryScope
from nanobot.memory.search import MemorySearcher, format_search_result
from nanobot.utils.helpers import estimate_message_tokens


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _coerce_questions(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        questions = data.get("questions", [])
        return [row for row in questions if isinstance(row, dict)] if isinstance(questions, list) else []
    return []


def _load_questions(path: Path) -> list[dict[str, Any]]:
    return _coerce_questions(_load_yaml(path) or [])


def _flatten_event_ids(result) -> list[str]:
    ids: list[str] = []
    # Evaluate first-stage retrieval by matched IDs before nearby context rows.
    # Context rows are useful for answering, but they should not push the actual
    # lexical/entity hit out of Recall@K.
    for window in result.windows:
        for event_id in window.matched_event_ids:
            if event_id not in ids:
                ids.append(event_id)
    for window in result.windows:
        for event in window.events:
            if event.event_id not in ids:
                ids.append(event.event_id)
    return ids


def _token_count(text: str) -> int:
    try:
        return estimate_message_tokens(text)
    except Exception:
        return max(1, len(text) // 4)


def run(config: Path | None = None, *, baseline: str = "search") -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    inline_questions: list[dict[str, Any]] | None = None
    if config and config.exists():
        loaded = _load_yaml(config)
        if isinstance(loaded, list):
            inline_questions = _coerce_questions(loaded)
        elif isinstance(loaded, dict):
            cfg = loaded
    workspace = Path(cfg.get("workspace") or ".")
    db_path = cfg.get("db_path")
    owner_id = str(cfg.get("owner_id") or "eval")
    workspace_id = str(cfg.get("workspace_id") or workspace.resolve())
    agent_id = cfg.get("agent_id")
    questions_value = cfg.get("questions")
    if inline_questions is not None:
        questions = inline_questions
    elif isinstance(questions_value, list):
        questions = _coerce_questions(questions_value)
    else:
        questions_path = Path(questions_value or Path(__file__).with_name("questions_dev.yaml"))
        questions = _load_questions(questions_path)
    scope = MemoryScope.from_runtime(owner_id=owner_id, workspace_id=workspace_id, agent_id=agent_id)
    store = ConversationEventStore(workspace, db_path=db_path)
    searcher = MemorySearcher(store)
    rows: list[dict[str, Any]] = []
    with store.connect() as conn:
        all_event_ids = [row["event_id"] for row in conn.execute("SELECT event_id FROM events WHERE owner_id = ?", (owner_id,)).fetchall()]
    for q in questions:
        result = None
        if baseline == "random":
            retrieved = random.sample(all_event_ids, min(len(all_event_ids), 20)) if all_event_ids else []
            retrieved_context_tokens = 0
            display_context_tokens = 0
            candidates = len(all_event_ids)
        else:
            result = searcher.search(scope=scope, query=str(q.get("question") or ""), exact=q.get("exact"), limit=20)
            retrieved = _flatten_event_ids(result)
            retrieved_context_tokens = result.context_tokens
            display_context_tokens = _token_count(format_search_result(result))
            candidates = result.total_candidates
        gold = [str(x) for x in q.get("gold_event_ids", [])]
        rows.append(
            {
                "id": q.get("id"),
                "question": q.get("question"),
                "gold_event_ids": gold,
                "retrieved_event_ids": retrieved,
                "evaluated": bool(gold),
                "retrieved_context_tokens": retrieved_context_tokens,
                "display_context_tokens": display_context_tokens,
                "context_tokens": display_context_tokens,
                "candidates": candidates,
            }
        )
    metrics = compute_retrieval_metrics(rows)
    evaluated_count = sum(1 for row in rows if row["evaluated"])
    total_context_tokens = sum(int(row.get("display_context_tokens") or 0) for row in rows)
    total_retrieved_context_tokens = sum(int(row.get("retrieved_context_tokens") or 0) for row in rows)
    report = {
        "baseline": baseline,
        "question_count": len(rows),
        "evaluated_question_count": evaluated_count,
        "unevaluated_question_count": len(rows) - evaluated_count,
        "metrics": metrics.__dict__,
        "context_tokens": {
            "display_total": total_context_tokens,
            "display_average": (total_context_tokens / len(rows)) if rows else 0.0,
            "retrieved_total": total_retrieved_context_tokens,
            "retrieved_average": (total_retrieved_context_tokens / len(rows)) if rows else 0.0,
        },
        "results": rows,
    }
    report_path = Path(cfg.get("report_path") or workspace / "memory" / "eval_report.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m nanobot.memory.eval")
    sub = parser.add_subparsers(dest="cmd")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--config", type=Path)
    run_parser.add_argument("--baseline", choices=["random", "search"], default="search")
    args = parser.parse_args(argv)
    if args.cmd == "run":
        print(json.dumps(run(args.config, baseline=args.baseline), ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
