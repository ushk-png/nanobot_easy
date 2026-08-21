from pathlib import Path

from nanobot.memory.eval.metrics import compute_retrieval_metrics
from nanobot.memory.eval.runner import _flatten_event_ids, run
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import EventWindow, MemoryScope, RawEvent, SearchResult


def test_eval_runner_accepts_question_list_yaml(tmp_path: Path):
    workspace = tmp_path / "workspace"
    store = ConversationEventStore(workspace)
    scope = MemoryScope.from_runtime(owner_id="u1", workspace_id=str(workspace), agent_id=None)
    event = store.append_event(
        scope=scope,
        conversation_id="c1",
        session_id="s1",
        actor="user",
        event_type="USER_MESSAGE",
        content="registry.py intent metadata enforcement",
    )
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        f'- id: Q1\n  question: "registry.py intent metadata"\n  gold_event_ids: ["{event.event_id}"]\n',
        encoding="utf-8",
    )

    report = run(questions, baseline="search")

    assert report["question_count"] == 1
    assert report["evaluated_question_count"] == 1
    assert report["metrics"]["recall_at_5"] == 0.0  # default eval scope has no access to tmp workspace event


def test_eval_runner_config_reports_display_and_retrieved_tokens(tmp_path: Path):
    workspace = tmp_path / "workspace"
    store = ConversationEventStore(workspace)
    scope = MemoryScope.from_runtime(owner_id="u1", workspace_id=str(workspace), agent_id=None)
    event = store.append_event(
        scope=scope,
        conversation_id="c1",
        session_id="s1",
        actor="user",
        event_type="USER_MESSAGE",
        content="Conversation RAG Memory chunk indexing",
    )
    config = tmp_path / "eval_config.yaml"
    report_path = tmp_path / "report.json"
    config.write_text(
        "\n".join(
            [
                f"workspace: {workspace}",
                "owner_id: u1",
                f"workspace_id: {workspace}",
                f"report_path: {report_path}",
                "questions:",
                "  - id: Q1",
                "    question: Conversation RAG Memory chunk indexing",
                f"    gold_event_ids: ['{event.event_id}']",
            ]
        ),
        encoding="utf-8",
    )

    report = run(config, baseline="search")

    assert report_path.exists()
    assert report["metrics"]["recall_at_5"] == 1.0
    assert report["context_tokens"]["display_total"] > 0
    assert report["context_tokens"]["retrieved_total"] > 0
    assert report["results"][0]["display_context_tokens"] <= report["results"][0]["retrieved_context_tokens"] + 200


def test_metrics_ignore_unevaluated_questions():
    metrics = compute_retrieval_metrics(
        [
            {"gold_event_ids": [], "retrieved_event_ids": ["noise"]},
            {"gold_event_ids": ["a"], "retrieved_event_ids": ["a", "b"]},
        ]
    )

    assert metrics.question_count == 1
    assert metrics.recall_at_5 == 1.0


def test_eval_flattens_matched_event_ids_before_context_events():
    hit = RawEvent(
        event_id="hit",
        owner_id="u1",
        workspace_id="main",
        agent_id=None,
        conversation_id="c1",
        session_id="s1",
        sequence=2,
        ts="2026-01-01T00:00:00Z",
        actor="user",
        event_type="USER_MESSAGE",
        content="hit",
    )
    ctx = RawEvent(
        event_id="ctx",
        owner_id="u1",
        workspace_id="main",
        agent_id=None,
        conversation_id="c1",
        session_id="s1",
        sequence=1,
        ts="2026-01-01T00:00:00Z",
        actor="assistant",
        event_type="ASSISTANT_MESSAGE",
        content="context",
    )
    result = SearchResult(
        windows=(EventWindow(score=1.0, session_id="s1", conversation_id="c1", events=(ctx, hit), matched_event_ids=("hit",)),),
        query="hit",
        total_candidates=1,
        context_tokens=10,
    )

    assert _flatten_event_ids(result) == ["hit", "ctx"]
