"""Phase 1 lexical/entity conversation memory search."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable

from nanobot.memory.entity_extract import exact_terms_from_query
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import EventWindow, MemoryScope, RawEvent, SearchResult
from nanobot.utils.helpers import estimate_message_tokens, truncate_text_to_tokens

DISPLAY_TOKEN_BUDGET = 2000
FORMATTED_RESULT_CHAR_BUDGET = 12_000


def _quote_fts(value: str) -> str:
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _safe_fts_query(query: str) -> str:
    terms = [part for part in re.split(r"\s+", query.strip()) if part]
    if not terms:
        return ""
    # Keep the original phrase and individual terms. Quoting avoids FTS syntax
    # surprises from file paths, CJK punctuation, or code identifiers.
    parts = [_quote_fts(query.strip())]
    parts.extend(_quote_fts(term) for term in terms[:8] if term != query.strip())
    return " OR ".join(parts)


def _token_count(text: str | None) -> int:
    if not text:
        return 0
    try:
        return estimate_message_tokens(text)
    except Exception:
        return max(1, len(text) // 4)


def _like_terms(query: str, exact: Iterable[str] | None = None) -> list[str]:
    terms: list[str] = []
    for value in [*(exact or []), *re.split(r"\s+", query.strip())]:
        term = str(value).strip().strip("`'\".,;:()[]{}")
        if not term or len(term) < 2:
            continue
        if term not in terms:
            terms.append(term)
    if query.strip() and query.strip() not in terms:
        terms.insert(0, query.strip())
    return terms[:8]


class MemorySearcher:
    """Scope-bound lexical/entity search with bounded evidence windows."""

    def __init__(self, store: ConversationEventStore) -> None:
        self.store = store

    def search(
        self,
        *,
        scope: MemoryScope,
        query: str,
        exact: Iterable[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        event_types: Iterable[str] | None = None,
        order: str = "relevance",
        limit: int = 10,
        max_context_tokens: int = 4000,
        exclude_event_ids: Iterable[str] | None = None,
    ) -> SearchResult:
        limit = min(max(int(limit or 10), 1), 50)
        budget = min(max(int(max_context_tokens or 4000), 500), 20_000)
        excluded = {str(eid) for eid in (exclude_event_ids or []) if str(eid).strip()}
        candidates = self._candidate_hits(
            scope=scope,
            query=query,
            exact=exact,
            after=after,
            before=before,
            event_types=event_types,
            excluded=excluded,
            candidate_limit=max(20, limit * 3),
            include_recent=(order == "recency"),
        )
        if order == "recency":
            candidates.sort(key=lambda item: item[1].ts, reverse=True)
        else:
            candidates.sort(key=lambda item: item[0], reverse=True)
        windows = self._build_windows(scope, candidates[: max(20, limit * 2)], excluded=excluded)
        final: list[EventWindow] = []
        used_tokens = 0
        for window in windows:
            window_tokens = sum(_token_count(event.content) + 12 for event in window.events)
            if final and used_tokens + window_tokens > budget:
                break
            final.append(window)
            used_tokens += window_tokens
            if len(final) >= limit:
                break
        return SearchResult(
            windows=tuple(final),
            query=query,
            total_candidates=len(candidates),
            context_tokens=used_tokens,
            truncated=len(windows) > len(final),
        )

    def _candidate_hits(
        self,
        *,
        scope: MemoryScope,
        query: str,
        exact: Iterable[str] | None,
        after: str | None,
        before: str | None,
        event_types: Iterable[str] | None,
        excluded: set[str],
        candidate_limit: int,
        include_recent: bool,
    ) -> list[tuple[float, RawEvent]]:
        scores: dict[str, float] = {}
        events: dict[str, RawEvent] = {}
        with self.store.connect() as conn:
            for score, row in self._chunk_hits(conn, scope, query, after, before, event_types, excluded, candidate_limit):
                event = ConversationEventStore._row_to_event(row)
                scores[event.event_id] = max(scores.get(event.event_id, 0.0), score)
                events[event.event_id] = event
            for score, row in self._fts_hits(conn, scope, query, after, before, event_types, excluded, candidate_limit):
                event = ConversationEventStore._row_to_event(row)
                scores[event.event_id] = max(scores.get(event.event_id, 0.0), score)
                events[event.event_id] = event
            for score, row in self._like_hits(conn, scope, query, exact, after, before, event_types, excluded, candidate_limit):
                event = ConversationEventStore._row_to_event(row)
                scores[event.event_id] = max(scores.get(event.event_id, 0.0), score)
                events[event.event_id] = event
            for score, row in self._entity_hits(conn, scope, query, exact, after, before, event_types, excluded, candidate_limit):
                event = ConversationEventStore._row_to_event(row)
                scores[event.event_id] = max(scores.get(event.event_id, 0.0), score)
                events[event.event_id] = event
            if include_recent or not scores:
                for score, row in self._recent_hits(conn, scope, after, before, event_types, excluded, min(10, candidate_limit)):
                    event = ConversationEventStore._row_to_event(row)
                    scores[event.event_id] = max(scores.get(event.event_id, 0.0), score)
                    events[event.event_id] = event
        return [(scores[eid], event) for eid, event in events.items()]

    def _base_where(
        self,
        scope: MemoryScope,
        after: str | None,
        before: str | None,
        event_types: Iterable[str] | None,
        excluded: set[str],
        *,
        alias: str = "e",
    ) -> tuple[str, list[Any]]:
        clauses = [
            f"{alias}.owner_id = ?",
            f"{alias}.workspace_id IN ({','.join('?' for _ in scope.workspace_ids)})",
            f"{alias}.redacted_at IS NULL",
            (
                f"NOT ({alias}.event_type = 'TOOL_RESULT' AND "
                f"({alias}.metadata_json LIKE '%\"name\": \"search_memory\"%' OR "
                f"{alias}.metadata_json LIKE '%\"name\": \"read_memory_events\"%' OR "
                f"{alias}.metadata_json LIKE '%\"tool_name\": \"search_memory\"%' OR "
                f"{alias}.metadata_json LIKE '%\"tool_name\": \"read_memory_events\"%'))"
            ),
        ]
        params: list[Any] = [scope.owner_id, *scope.workspace_ids]
        agent_sql, agent_params = ConversationEventStore._agent_scope_sql(scope)
        if agent_sql:
            scoped_agent_sql = agent_sql.strip()
            if scoped_agent_sql.upper().startswith("AND "):
                scoped_agent_sql = scoped_agent_sql[4:]
            clauses.append(scoped_agent_sql.replace("agent_id", f"{alias}.agent_id"))
            params.extend(agent_params)
        if after:
            clauses.append(f"{alias}.ts >= ?")
            params.append(after)
        if before:
            clauses.append(f"{alias}.ts <= ?")
            params.append(before)
        types = [str(t) for t in (event_types or []) if str(t).strip()]
        if types:
            clauses.append(f"{alias}.event_type IN ({','.join('?' for _ in types)})")
            params.extend(types)
        if excluded:
            clauses.append(f"{alias}.event_id NOT IN ({','.join('?' for _ in excluded)})")
            params.extend(sorted(excluded))
        return " AND ".join(clauses), params

    def _chunk_hits(self, conn: sqlite3.Connection, scope: MemoryScope, query: str, after: str | None, before: str | None, event_types: Iterable[str] | None, excluded: set[str], limit: int) -> list[tuple[float, sqlite3.Row]]:
        fts_query = _safe_fts_query(query)
        if not fts_query:
            return []
        where, params = self._base_where(scope, after, before, event_types, excluded)
        sql = f"""
            SELECT e.*, bm25(event_chunks_fts) AS rank, c.chunk_id, c.ordinal, c.text AS chunk_text
            FROM event_chunks_fts
            JOIN event_chunks c ON c.chunk_rowid = event_chunks_fts.rowid
            JOIN events e ON e.event_id = c.event_id
            WHERE event_chunks_fts MATCH ? AND {where}
            ORDER BY rank LIMIT ?
        """
        try:
            rows = conn.execute(sql, [fts_query, *params, limit]).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[tuple[float, sqlite3.Row]] = []
        now = datetime.now(timezone.utc)
        for idx, row in enumerate(rows):
            type_bias = -0.10 if row["event_type"] == "CURATED_MEMORY_EDIT" else 0.0
            score = 0.70 + max(0.0, (limit - idx) / max(limit, 1)) * 0.15 + type_bias + self._recency_bonus(row["ts"], now)
            out.append((score, row))
        return out

    def _fts_hits(self, conn: sqlite3.Connection, scope: MemoryScope, query: str, after: str | None, before: str | None, event_types: Iterable[str] | None, excluded: set[str], limit: int) -> list[tuple[float, sqlite3.Row]]:
        fts_query = _safe_fts_query(query)
        if not fts_query:
            return []
        where, params = self._base_where(scope, after, before, event_types, excluded)
        sql = f"""
            SELECT e.*, bm25(events_fts) AS rank
            FROM events_fts
            JOIN events e ON e.event_rowid = events_fts.rowid
            WHERE events_fts MATCH ? AND {where}
            ORDER BY rank LIMIT ?
        """
        try:
            rows = conn.execute(sql, [fts_query, *params, limit]).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[tuple[float, sqlite3.Row]] = []
        now = datetime.now(timezone.utc)
        for idx, row in enumerate(rows):
            type_bias = -0.05 if row["event_type"] == "CURATED_MEMORY_EDIT" else 0.05
            score = 0.70 + max(0.0, (limit - idx) / max(limit, 1)) * 0.15 + type_bias + self._recency_bonus(row["ts"], now)
            out.append((score, row))
        return out

    def _like_hits(self, conn: sqlite3.Connection, scope: MemoryScope, query: str, exact: Iterable[str] | None, after: str | None, before: str | None, event_types: Iterable[str] | None, excluded: set[str], limit: int) -> list[tuple[float, sqlite3.Row]]:
        terms = _like_terms(query, exact)
        if not terms:
            return []
        where, params = self._base_where(scope, after, before, event_types, excluded)
        like_clauses = " OR ".join("e.content LIKE ?" for _ in terms)
        rows = conn.execute(
            f"""
            SELECT e.*
            FROM events e
            WHERE {where} AND e.content IS NOT NULL AND ({like_clauses})
            ORDER BY e.ts DESC LIMIT ?
            """,
            [*params, *[f"%{term}%" for term in terms], limit],
        ).fetchall()
        now = datetime.now(timezone.utc)
        out: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            content = str(row["content"] or "").casefold()
            matched = sum(1 for term in terms if term.casefold() in content)
            density = matched / max(len(terms), 1)
            type_bias = -0.05 if row["event_type"] == "CURATED_MEMORY_EDIT" else 0.10
            score = 0.65 + min(0.25, density * 0.25) + type_bias + self._recency_bonus(row["ts"], now)
            out.append((score, row))
        return out

    def _entity_hits(self, conn: sqlite3.Connection, scope: MemoryScope, query: str, exact: Iterable[str] | None, after: str | None, before: str | None, event_types: Iterable[str] | None, excluded: set[str], limit: int) -> list[tuple[float, sqlite3.Row]]:
        terms = exact_terms_from_query(query, exact)
        if not terms:
            return []
        where, params = self._base_where(scope, after, before, event_types, excluded)
        placeholders = ",".join("?" for _ in terms)
        sql = f"""
            SELECT DISTINCT e.*
            FROM entities ent
            JOIN events e ON e.event_id = ent.event_id
            WHERE ent.value IN ({placeholders}) AND {where}
            ORDER BY e.ts DESC LIMIT ?
        """
        rows = conn.execute(sql, [*sorted(terms), *params, limit]).fetchall()
        out: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            type_bias = -0.05 if row["event_type"] == "CURATED_MEMORY_EDIT" else 0.05
            out.append((0.70 + type_bias + self._recency_bonus(row["ts"]), row))
        return out

    def _recent_hits(self, conn: sqlite3.Connection, scope: MemoryScope, after: str | None, before: str | None, event_types: Iterable[str] | None, excluded: set[str], limit: int) -> list[tuple[float, sqlite3.Row]]:
        where, params = self._base_where(scope, after, before, event_types, excluded)
        rows = conn.execute(f"SELECT e.* FROM events e WHERE {where} ORDER BY e.ts DESC, e.sequence DESC LIMIT ?", [*params, limit]).fetchall()
        return [(0.15 + self._recency_bonus(row["ts"]), row) for row in rows]

    @staticmethod
    def _recency_bonus(ts: str, now: datetime | None = None) -> float:
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            current = now or datetime.now(timezone.utc)
            days = max(0.0, (current - parsed).total_seconds() / 86400)
            return min(0.10, 0.10 / math.sqrt(days + 1))
        except Exception:
            return 0.0

    def _build_windows(self, scope: MemoryScope, candidates: list[tuple[float, RawEvent]], *, excluded: set[str]) -> list[EventWindow]:
        if not candidates:
            return []
        by_session: dict[str, list[tuple[int, int, float, str]]] = {}
        for score, event in candidates:
            before, after = self._window_radius(event)
            by_session.setdefault(event.session_id, []).append((max(1, event.sequence - before), event.sequence + after, score, event.event_id))
        merged: list[EventWindow] = []
        with self.store.connect() as conn:
            for session_id, ranges in by_session.items():
                ranges.sort(key=lambda item: item[0])
                compact: list[tuple[int, int, float, set[str]]] = []
                for start, end, score, hit_id in ranges:
                    if compact and start <= compact[-1][1] + 1:
                        old_start, old_end, old_score, hit_ids = compact[-1]
                        hit_ids.add(hit_id)
                        compact[-1] = (old_start, max(old_end, end), max(old_score, score), hit_ids)
                    else:
                        compact.append((start, end, score, {hit_id}))
                for start, end, score, hit_ids in compact:
                    rows = conn.execute(
                        """
                        SELECT * FROM events
                        WHERE session_id = ? AND sequence BETWEEN ? AND ?
                          AND owner_id = ?
                          AND workspace_id IN ({})
                          AND redacted_at IS NULL
                        ORDER BY sequence
                        """.format(",".join("?" for _ in scope.workspace_ids)),
                        [session_id, start, end, scope.owner_id, *scope.workspace_ids],
                    ).fetchall()
                    events = [
                        ConversationEventStore._row_to_event(row)
                        for row in rows
                        if row["event_id"] not in excluded
                        and not ConversationEventStore._is_memory_tool_result(ConversationEventStore._row_to_event(row))
                    ]
                    events = self._include_tool_pairs(conn, scope, events, excluded)
                    if events:
                        merged.append(EventWindow(score=score, session_id=session_id, conversation_id=events[0].conversation_id, events=tuple(events), matched_event_ids=tuple(sorted(hit_ids))))
        merged.sort(key=lambda window: window.score, reverse=True)
        return merged

    @staticmethod
    def _window_radius(event: RawEvent) -> tuple[int, int]:
        """Return compact context radius for a hit.

        Imported Dream summaries and tool results can be very large; returning
        broad +/-3 windows around them makes search output explode even when the
        token budget is small. Keep raw conversational hits contextual, but make
        curated summaries and tool evidence self-contained unless explicitly read
        later with read_memory_events.
        """
        if event.event_type == "CURATED_MEMORY_EDIT":
            return (0, 0)
        if event.event_type in {"TOOL_CALL", "TOOL_RESULT"}:
            return (1, 1)
        return (2, 2)

    def _include_tool_pairs(self, conn: sqlite3.Connection, scope: MemoryScope, events: list[RawEvent], excluded: set[str]) -> list[RawEvent]:
        ids = {event.event_id for event in events}
        extra: list[RawEvent] = []
        for event in events:
            if event.parent_event_id and event.parent_event_id not in ids and event.parent_event_id not in excluded:
                rows = conn.execute("SELECT * FROM events WHERE event_id = ? AND owner_id = ? AND redacted_at IS NULL", (event.parent_event_id, scope.owner_id)).fetchall()
                extra.extend(
                    event
                    for row in rows
                    for event in [ConversationEventStore._row_to_event(row)]
                    if not ConversationEventStore._is_memory_tool_result(event)
                )
        all_events = {event.event_id: event for event in [*events, *extra] if not ConversationEventStore._is_memory_tool_result(event)}
        return sorted(all_events.values(), key=lambda event: (event.session_id, event.sequence))


def format_search_result(result: SearchResult) -> str:
    if not result.windows:
        return "Memory Search Result\nNo matching scoped memory events found."
    rows, omitted_events = _compact_rows_for_display(result.windows, result.query)
    rows, budget_omitted = _apply_row_display_budget(rows, DISPLAY_TOKEN_BUDGET)
    omitted_events += budget_omitted
    display_tokens = sum(_token_count(row["snippet"]) + 24 for row in rows)
    lines = [
        "Memory Search Result",
        f"query: {result.query}",
        f"candidates: {result.total_candidates}",
        f"context_tokens_estimate: {display_tokens}",
        f"retrieved_context_tokens_estimate: {result.context_tokens}",
        f"display_token_budget: {DISPLAY_TOKEN_BUDGET}",
        "mode: compact_snippets",
    ]
    for idx, row in enumerate(rows, start=1):
        lines.append("")
        lines.append(f"Result {idx}")
        lines.append(f"event_id: {row['event_id']}")
        lines.append(f"timestamp: {row['timestamp']}")
        lines.append(f"event_type: {row['event_type']}")
        lines.append(f"actor: {row['actor']}")
        lines.append(f"session: {row['session_id']}")
        lines.append(f"score: {row['score']:.2f}")
        lines.append(f"snippet: {row['snippet']}")
        if row.get("truncated"):
            lines.append("[snippet_truncated=true; use read_memory_events for raw content]")
    if omitted_events:
        lines.append(f"\n[compacted_events_omitted={omitted_events}; use read_memory_events for raw content]")
    if result.truncated:
        lines.append("\n[additional results omitted by context budget]")
    rendered = "\n".join(lines)
    if len(rendered) <= FORMATTED_RESULT_CHAR_BUDGET:
        return rendered
    suffix = "\n[formatted_search_result_truncated=true; use read_memory_events for raw content]"
    return rendered[: max(0, FORMATTED_RESULT_CHAR_BUDGET - len(suffix))].rstrip() + suffix


def _compact_rows_for_display(windows: tuple[EventWindow, ...], query: str) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    omitted = 0
    seen: set[str] = set()
    terms = [part for part in re.split(r"\s+", query.strip()) if part]
    for window in windows:
        matched = set(window.matched_event_ids)
        for event in window.events:
            if event.event_id in seen:
                omitted += 1
                continue
            seen.add(event.event_id)
            snippet, truncated = _event_snippet(event, terms)
            rows.append(
                {
                    "event_id": event.event_id,
                    "timestamp": event.ts,
                    "event_type": event.event_type,
                    "actor": event.actor,
                    "session_id": event.session_id,
                    "score": window.score,
                    "snippet": snippet,
                    "truncated": truncated,
                    "matched": event.event_id in matched,
                }
            )
    rows.sort(key=lambda row: (not row["matched"], -float(row["score"]), row["timestamp"]))
    return rows, omitted


def _event_snippet(event: RawEvent, terms: list[str], *, max_tokens: int = 90) -> tuple[str, bool]:
    content = (event.content or "").strip()
    if not content:
        return "", False
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    haystack_terms = [term.casefold() for term in terms if term]
    selected: list[str] = []
    for line in lines:
        folded = line.casefold()
        if any(term in folded for term in haystack_terms):
            selected.append(line)
        if len(selected) >= 2:
            break
    if not selected:
        selected = lines[:2]
    snippet = " / ".join(selected)
    truncated = _token_count(content) > max_tokens or len(selected) < len(lines)
    if _token_count(snippet) > max_tokens:
        snippet = truncate_text_to_tokens(snippet, max_tokens)
        truncated = True
    return snippet, truncated


def _apply_row_display_budget(rows: list[dict[str, Any]], budget: int) -> tuple[list[dict[str, Any]], int]:
    emitted: list[dict[str, Any]] = []
    used = 0
    omitted = 0
    for row in rows:
        cost = _token_count(row["snippet"]) + 24
        if emitted and used + cost > budget:
            omitted += 1
            continue
        if not emitted and used + cost > budget:
            remaining = max(1, budget - used - 24)
            row = {**row, "snippet": truncate_text_to_tokens(row["snippet"], remaining), "truncated": True}
            cost = _token_count(row["snippet"]) + 24
        emitted.append(row)
        used += cost
    return emitted, omitted


def _display_token_limit(event: RawEvent) -> int:
    if event.event_type == "CURATED_MEMORY_EDIT":
        return 320
    if event.event_type in {"TOOL_CALL", "TOOL_RESULT"}:
        return 220
    return 500


def _compact_windows_for_display(windows: tuple[EventWindow, ...]) -> tuple[list[EventWindow], int]:
    compacted: list[EventWindow] = []
    omitted_events = 0
    for window in windows:
        matched = set(window.matched_event_ids)
        if not matched:
            compacted.append(window)
            continue
        keep: list[RawEvent] = []
        events = list(window.events)
        for idx, event in enumerate(events):
            if event.event_id in matched:
                keep.append(event)
                prev_idx = idx - 1
                next_idx = idx + 1
                if event.event_type not in {"CURATED_MEMORY_EDIT", "TOOL_CALL", "TOOL_RESULT"}:
                    if prev_idx >= 0:
                        keep.append(events[prev_idx])
                    if next_idx < len(events):
                        keep.append(events[next_idx])
                elif event.event_type == "TOOL_RESULT" and event.parent_event_id:
                    keep.extend(candidate for candidate in events if candidate.event_id == event.parent_event_id)
        ordered: list[RawEvent] = []
        seen: set[str] = set()
        for event in sorted(keep, key=lambda item: item.sequence):
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            ordered.append(event)
        omitted = max(0, len(events) - len(ordered))
        omitted_events += omitted
        compacted.append(replace(window, events=tuple(ordered), truncated=window.truncated or omitted > 0))
    return compacted, omitted_events


def _apply_display_budget(windows: list[EventWindow], budget: int) -> tuple[list[EventWindow], int]:
    emitted: list[EventWindow] = []
    used = 0
    omitted_events = 0
    for window in windows:
        kept_events: list[RawEvent] = []
        any_event_truncated = False
        for event in window.events:
            content = event.content or ""
            token_limit = _display_token_limit(event)
            if _token_count(content) > token_limit:
                content = truncate_text_to_tokens(content, token_limit)
                content = f"{content}\n[truncated=true; use read_memory_events for raw content]"
                any_event_truncated = True
            event_cost = _token_count(content) + 12
            if kept_events and used + event_cost > budget:
                omitted_events += 1
                continue
            if not kept_events and used + event_cost > budget:
                if emitted:
                    omitted_events += len(window.events)
                    kept_events = []
                    break
                remaining = max(1, budget - used - 12)
                content = truncate_text_to_tokens(content, remaining)
                event_cost = _token_count(content) + 12
            kept_events.append(replace(event, content=content))
            used += event_cost
        if kept_events:
            emitted.append(replace(window, events=tuple(kept_events), truncated=window.truncated or len(kept_events) < len(window.events) or any_event_truncated))
        elif window.events:
            omitted_events += len(window.events)
    return emitted, omitted_events
