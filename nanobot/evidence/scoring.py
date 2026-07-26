"""Rule-based scoring for general-purpose evidence."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from nanobot.evidence.types import EvidenceItem

_OFFICIAL_HOST_RE = re.compile(
    r"(^|\.)("
    r"gov|mil|edu|int|who\.int|oecd\.org|w3\.org|ietf\.org|iso\.org|"
    r"openai\.com|microsoft\.com|apple\.com|google\.com|github\.com"
    r")$",
    re.I,
)
_RESEARCH_HOST_RE = re.compile(r"(^|\.)(arxiv\.org|nature\.com|science\.org|acm\.org|ieee\.org|springer\.com)$", re.I)
_MEDIA_HOST_RE = re.compile(r"(^|\.)(reuters\.com|apnews\.com|bbc\.com|nytimes\.com|wsj\.com|ft\.com|bloomberg\.com)$", re.I)
_WIKI_HOST_RE = re.compile(r"(^|\.)(wikipedia\.org)$", re.I)
_BLOG_HOST_RE = re.compile(r"(^|\.)(medium\.com|substack\.com|wordpress\.com|blogspot\.com)$", re.I)
_FORUM_HOST_RE = re.compile(r"(^|\.)(reddit\.com|stackoverflow\.com|stackexchange\.com|quora\.com|news\.ycombinator\.com)$", re.I)


def normalize_independence_key(url_or_path: str) -> str:
    """Return a coarse origin key for duplicate/independence grouping."""
    parsed = urlparse(url_or_path)
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or url_or_path.split("#", 1)[0]


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse common timestamp/date formats into an aware datetime."""
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    for candidate in (raw, raw.replace("Z", "+00:00")):
        try:
            dt = datetime.fromisoformat(candidate)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        dt = parsedate_to_datetime(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


class SourceScorer:
    """Assign source trust scores using general-domain heuristics."""

    def score(self, item: EvidenceItem) -> EvidenceItem:
        host = normalize_independence_key(item.url_or_path)
        if item.source_type == "system":
            level, score = "system", 1.0
        elif item.source_type == "file":
            level, score = "local_file", 0.88
        elif item.source_type == "attachment":
            level, score = "user_attachment", 0.82
        elif _OFFICIAL_HOST_RE.search(host):
            level, score = "official", 0.9
        elif _RESEARCH_HOST_RE.search(host):
            level, score = "research", 0.84
        elif _MEDIA_HOST_RE.search(host):
            level, score = "reputable_media", 0.74
        elif _WIKI_HOST_RE.search(host):
            level, score = "wiki", 0.58
        elif _BLOG_HOST_RE.search(host):
            level, score = "blog", 0.42
        elif _FORUM_HOST_RE.search(host):
            level, score = "forum", 0.35
        else:
            level, score = "unknown_web", 0.5 if item.source_type == "web" else 0.45
        item.trust_level = level
        item.trust_score = score
        item.independence_key = item.independence_key or host
        item.citations = item.citations or [item.id]
        return item


class FreshnessPolicy:
    """Apply lightweight freshness flags for time-sensitive retrieval."""

    def __init__(self, stale_after_days: int = 180) -> None:
        self.stale_after_days = stale_after_days

    def score(self, item: EvidenceItem, *, time_sensitive: bool, now: datetime | None = None) -> EvidenceItem:
        if not time_sensitive:
            item.freshness = "not_time_sensitive"
            item.freshness_score = 1.0
            return item
        parsed = parse_timestamp(item.timestamp)
        if parsed is None:
            item.freshness = "unknown_date"
            item.freshness_score = 0.4
            item.flags.append("STALE_RISK")
            return item
        current = now or datetime.now(UTC)
        age_days = max(0, (current - parsed.astimezone(UTC)).days)
        if age_days > self.stale_after_days:
            item.freshness = f"stale:{age_days}d"
            item.freshness_score = 0.25
            item.flags.append("STALE_RISK")
        else:
            item.freshness = f"fresh:{age_days}d"
            item.freshness_score = 1.0
        return item


def dedupe_by_independence(items: Iterable[EvidenceItem]) -> list[EvidenceItem]:
    """Keep the strongest item for each origin/independence key."""
    best: dict[str, EvidenceItem] = {}
    for item in items:
        key = item.independence_key or normalize_independence_key(item.url_or_path) or item.id
        previous = best.get(key)
        if previous is None:
            best[key] = item
            continue
        prev_score = previous.trust_score + previous.freshness_score + previous.relevance
        item_score = item.trust_score + item.freshness_score + item.relevance
        if item_score > prev_score:
            best[key] = item
    return list(best.values())
