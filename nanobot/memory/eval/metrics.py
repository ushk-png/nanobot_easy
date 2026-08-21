"""Metrics for memory retrieval evaluation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalMetrics:
    recall_at_5: float
    recall_at_20: float
    mrr: float
    source_precision: float
    question_count: int


def compute_retrieval_metrics(results: list[dict]) -> RetrievalMetrics:
    evaluated = [row for row in results if row.get("gold_event_ids")]
    if not evaluated:
        return RetrievalMetrics(0.0, 0.0, 0.0, 0.0, 0)
    r5 = 0.0
    r20 = 0.0
    rr = 0.0
    precision = 0.0
    for row in evaluated:
        gold = [str(x) for x in row.get("gold_event_ids", [])]
        retrieved = [str(x) for x in row.get("retrieved_event_ids", [])]
        gold_set = set(gold)
        top5 = set(retrieved[:5])
        top20 = set(retrieved[:20])
        r5 += len(gold_set & top5) / len(gold_set)
        r20 += len(gold_set & top20) / len(gold_set)
        for idx, event_id in enumerate(retrieved, start=1):
            if event_id in gold_set:
                rr += 1.0 / idx
                break
        precision += (len(gold_set & set(retrieved)) / len(retrieved)) if retrieved else 0.0
    n = len(evaluated)
    return RetrievalMetrics(r5 / n, r20 / n, rr / n, precision / n, n)
