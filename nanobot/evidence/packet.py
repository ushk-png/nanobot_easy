"""Evidence packet construction and formatting."""

from __future__ import annotations

import re
from collections.abc import Iterable

from nanobot.evidence.scoring import FreshnessPolicy, SourceScorer, dedupe_by_independence
from nanobot.evidence.types import Answerability, EvidenceItem, EvidencePacket

_TIME_SENSITIVE_RE = re.compile(
    r"\b(today|latest|current|recent|news|price|version|release|schedule|law|regulation)\b|"
    r"(오늘|최신|최근|현재|뉴스|가격|버전|릴리즈|일정|법|규정)",
    re.I,
)


def is_time_sensitive_query(query: str) -> bool:
    """Return whether a query likely needs freshness handling."""
    return bool(_TIME_SENSITIVE_RE.search(query or ""))


def classify_query_type(query: str, *, time_sensitive: bool) -> str:
    lowered = (query or "").lower()
    if time_sensitive:
        return "freshness"
    if any(word in lowered for word in ("compare", " vs ", "difference", "비교", "차이")):
        return "comparison"
    if any(word in lowered for word in ("why", "explain", "설명", "왜")):
        return "explanation"
    return "fact_lookup"


class EvidencePacketBuilder:
    """Build compact, source-aware evidence packets from retrieval results."""

    def __init__(
        self,
        *,
        source_scorer: SourceScorer | None = None,
        freshness_policy: FreshnessPolicy | None = None,
    ) -> None:
        self.source_scorer = source_scorer or SourceScorer()
        self.freshness_policy = freshness_policy or FreshnessPolicy()

    def build(
        self,
        *,
        question: str,
        items: Iterable[EvidenceItem],
        time_sensitive: bool | None = None,
    ) -> EvidencePacket:
        sensitive = is_time_sensitive_query(question) if time_sensitive is None else time_sensitive
        scored: list[EvidenceItem] = []
        for item in items:
            scored_item = self.source_scorer.score(item)
            scored_item = self.freshness_policy.score(scored_item, time_sensitive=sensitive)
            scored.append(scored_item)
        deduped = dedupe_by_independence(scored)
        deduped.sort(
            key=lambda item: (
                item.trust_score * 0.45 + item.freshness_score * 0.25 + item.relevance * 0.30
            ),
            reverse=True,
        )
        warnings = [
            f"{item.id}: {item.title or item.url_or_path} has {item.freshness}"
            for item in deduped
            if "STALE_RISK" in item.flags
        ]
        answerability = self._answerability(deduped, freshness_warnings=warnings, time_sensitive=sensitive)
        missing = []
        if answerability == Answerability.NO_EVIDENCE:
            missing.append("No retrieval evidence was found.")
        elif answerability == Answerability.STALE_RISK:
            missing.append("Current enough evidence was not confirmed.")
        return EvidencePacket(
            question=question,
            query_type=classify_query_type(question, time_sensitive=sensitive),
            time_sensitive=sensitive,
            answerability=answerability,
            items=deduped,
            freshness_warnings=warnings,
            missing_info=missing,
        )

    @staticmethod
    def _answerability(
        items: list[EvidenceItem],
        *,
        freshness_warnings: list[str],
        time_sensitive: bool,
    ) -> Answerability:
        if not items:
            return Answerability.NO_EVIDENCE
        independent = {item.independence_key or item.id for item in items}
        has_reliable = any(item.trust_score >= 0.7 for item in items)
        if time_sensitive and len(freshness_warnings) == len(items):
            return Answerability.STALE_RISK
        if len(independent) == 1 and not has_reliable:
            return Answerability.PARTIAL
        return Answerability.ANSWERABLE if has_reliable or len(independent) >= 2 else Answerability.PARTIAL

    @staticmethod
    def format(packet: EvidencePacket, *, max_items: int = 10) -> str:
        """Render an EvidencePacket as model-readable text."""
        lines = [
            "# Evidence Packet",
            f"Question: {packet.question}",
            f"Query Type: {packet.query_type}",
            f"Time Sensitive: {str(packet.time_sensitive).lower()}",
            f"Answerability: {packet.answerability.value}",
            "",
            "Sources:",
        ]
        for item in packet.items[:max_items]:
            flags = f" flags={','.join(item.flags)}" if item.flags else ""
            title = item.title or "(untitled)"
            lines.append(
                f"- [{item.id}] {title} | {item.url_or_path} | trust={item.trust_level}:{item.trust_score:.2f} "
                f"| freshness={item.freshness}:{item.freshness_score:.2f} | relevance={item.relevance:.2f}{flags}"
            )
            if item.content_snippet:
                lines.append(f"  snippet: {item.content_snippet}")
        if packet.freshness_warnings:
            lines.extend(["", "Freshness Warnings:"])
            lines.extend(f"- {warning}" for warning in packet.freshness_warnings)
        if packet.conflicts:
            lines.extend(["", "Conflicts:"])
            lines.extend(f"- {conflict}" for conflict in packet.conflicts)
        if packet.missing_info:
            lines.extend(["", "Missing Info:"])
            lines.extend(f"- {info}" for info in packet.missing_info)
        lines.extend([
            "",
            "Instructions:",
            "- Use only the sources above for factual claims from this search.",
            "- Mark single-source or stale-risk claims instead of overstating certainty.",
            "- If Answerability is NO_EVIDENCE or STALE_RISK, say what cannot be confirmed.",
        ])
        return "\n".join(lines)
