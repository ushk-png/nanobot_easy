"""Skill registry search tool."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from nanobot.agent.skills import SYSTEM_SKILLS_DIR
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import (
    ArraySchema,
    IntegerSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.skill_store import SkillSearchMatch, SkillStore

_DEFAULT_TOP_K = 5
_MAX_BATCH = 8
_MAX_TOP_K = 10


def _digest_query(query: str, category: str | None = None) -> str:
    raw = f"{category or ''}\n{query}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _lexical_score(query: str, match: SkillSearchMatch) -> float:
    terms = [term for term in query.lower().split() if term]
    if not terms:
        return 0.0
    haystacks = {
        "name": match.name.lower(),
        "description": match.description.lower(),
        "category": match.category.lower(),
    }
    hits = 0
    for term in terms:
        if any(term in value for value in haystacks.values()):
            hits += 1
    score = (hits / len(terms)) * 100.0
    if query.lower() in haystacks["name"]:
        score += 20.0
    elif any(term in haystacks["name"] for term in terms):
        score += 10.0
    if any(term in haystacks["category"] for term in terms):
        score += 5.0
    return min(score, 100.0)


def _rank_score(query: str, match: SkillSearchMatch, *, category: str | None = None) -> float:
    score = _lexical_score(query, match)
    if category and category.lower() == match.category.lower():
        score += 10.0
    score += match.stats_weight
    return max(0.0, min(score, 100.0))


def _match_grade(score: float) -> str:
    if score >= 70:
        return "strong"
    if score >= 35:
        return "moderate"
    return "weak"


def _coerce_top_k(value: Any) -> int:
    try:
        top_k = int(value)
    except (TypeError, ValueError):
        top_k = _DEFAULT_TOP_K
    return max(1, min(_MAX_TOP_K, top_k))


@tool_parameters(
    tool_parameters_schema(
        queries=ArraySchema(
            ObjectSchema(
                query=StringSchema("User task or subtask to route to a skill"),
                category=StringSchema("Optional category hint, e.g. document.review", nullable=True),
                top_k=IntegerSchema(description="Optional max candidates for this query", minimum=1, maximum=_MAX_TOP_K),
                required=["query"],
                additional_properties=False,
            ),
            description="One or more skill search requests. Use a batch for composite-task waves.",
            min_items=1,
            max_items=_MAX_BATCH,
        ),
        required=["queries"],
    )
)
class SkillSearchTool(Tool, ContextAware):
    """Search registered skills by task description."""

    def __init__(self, workspace: str):
        self.workspace = Path(workspace)
        self._session_key: str | None = None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    @property
    def name(self) -> str:
        return "skill_search"

    @property
    def description(self) -> str:
        return (
            "Search the workspace skill registry for candidate skills. "
            "Use this when preloaded skills do not clearly cover a specialized task. "
            "Input is always a batch: queries=[{query, category?, top_k?}]. "
            "Results are grouped per query and include match grades, risk, exec needs, "
            "and skill relations. If all matches are weak, do not force a skill."
        )

    @property
    def read_only(self) -> bool:
        return True

    def set_context(self, ctx: RequestContext) -> None:
        self._session_key = ctx.session_key or f"{ctx.channel}:{ctx.chat_id}"

    async def execute(self, queries: list[dict[str, Any]], **_kwargs: Any) -> str:
        store = SkillStore(self.workspace)
        store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
        outputs: list[dict[str, Any]] = []
        for item in queries[:_MAX_BATCH]:
            query = str(item.get("query") or "").strip()
            category = item.get("category")
            category = str(category).strip() if category is not None else None
            top_k = _coerce_top_k(item.get("top_k", _DEFAULT_TOP_K))
            if not query:
                outputs.append({
                    "query": query,
                    "category": category,
                    "candidates": [],
                    "note": "empty query; no skill search performed",
                })
                continue
            matches = store.search(query, top_k=min(_MAX_TOP_K, top_k * 3), min_status=("candidate", "verified"))
            relations = store.relations_for_names(match.name for match in matches)
            candidates: list[dict[str, Any]] = []
            for match in matches:
                score = _rank_score(query, match, category=category)
                rel = relations.get(match.name, {})
                candidates.append({
                    "name": match.name,
                    "description": match.description,
                    "risk_level": match.risk_level,
                    "requires_exec": match.requires_exec,
                    "category": match.category,
                    "status": match.status,
                    "score": round(score, 2),
                    "match_grade": _match_grade(score),
                    "usage_count": match.usage_count,
                    "success_count": match.success_count,
                    "failure_count": match.failure_count,
                    "routing_failure_count": match.routing_failure_count,
                    "conflicts_with": rel.get("conflicts_with", []),
                    "supersedes": rel.get("supersedes", []),
                    "fallback_to": rel.get("fallback_to", []),
                    "path": match.path,
                })
            candidates.sort(key=lambda row: row["score"], reverse=True)
            candidates = candidates[:top_k]
            conflict_warning = self._conflict_warning(candidates)
            all_weak = bool(candidates) and all(row["match_grade"] == "weak" for row in candidates)
            note = "No applicable skill: all candidates are weak." if all_weak else None
            payload = {
                "query": query,
                "category": category,
                "candidates": candidates,
                "note": note,
                "conflict_warning": conflict_warning,
            }
            outputs.append(payload)
            store.record_trace(
                trace_id=f"skill_search:{uuid4().hex}",
                session_key=self._session_key,
                query_digest=_digest_query(query, category),
                candidates=candidates,
                selected_skill=None,
                selection_reason="cold",
                executed_by="main",
                notes=note or conflict_warning,
            )
        return json.dumps({"results": outputs}, ensure_ascii=False, indent=2)

    @staticmethod
    def _conflict_warning(candidates: list[dict[str, Any]]) -> str | None:
        if len(candidates) < 2:
            return None
        first, second = candidates[0], candidates[1]
        first_conflicts = set(first.get("conflicts_with") or [])
        second_conflicts = set(second.get("conflicts_with") or [])
        if second["name"] not in first_conflicts and first["name"] not in second_conflicts:
            return None
        top_score = max(float(first["score"]), 1.0)
        diff_ratio = abs(float(first["score"]) - float(second["score"])) / top_score
        if diff_ratio < 0.10:
            return (
                f"Top candidates '{first['name']}' and '{second['name']}' conflict "
                "and scores differ by less than 10%; ask the user to confirm."
            )
        return None
