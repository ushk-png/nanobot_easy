"""Shared evidence data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Answerability(StrEnum):
    """Coarse answerability state for evidence-backed retrieval."""

    ANSWERABLE = "ANSWERABLE"
    PARTIAL = "PARTIAL"
    CONFLICTED = "CONFLICTED"
    STALE_RISK = "STALE_RISK"
    NO_EVIDENCE = "NO_EVIDENCE"
    NEEDS_TOOL = "NEEDS_TOOL"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"


@dataclass(slots=True)
class EvidenceItem:
    """A normalized retrieval result from web, files, memory, skills, or tools."""

    id: str
    source_type: str
    title: str
    url_or_path: str
    content_snippet: str
    timestamp: str | None = None
    author: str | None = None
    trust_level: str = "unknown"
    trust_score: float = 0.0
    freshness: str = "unknown"
    freshness_score: float = 0.0
    relevance: float = 0.0
    independence_key: str = ""
    citations: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EvidencePacket:
    """Compact evidence context for an LLM answer."""

    question: str
    query_type: str
    time_sensitive: bool
    answerability: Answerability
    items: list[EvidenceItem]
    conflicts: list[str] = field(default_factory=list)
    freshness_warnings: list[str] = field(default_factory=list)
    missing_info: list[str] = field(default_factory=list)
