"""SQLite-backed skill registry and trace store."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from nanobot.agent.skills import _STRIP_SKILL_FRONTMATTER, BUILTIN_SKILLS_DIR

SkillStatus = Literal["system", "draft", "candidate", "verified", "deprecated", "rejected"]
RelationKind = Literal["conflicts", "supersedes", "fallback"]
TraceSelectionReason = Literal["direct", "hot", "cold", "composite", "none"]
TraceExecutedBy = Literal["main"]

VALID_STATUSES: set[str] = {
    "system",
    "draft",
    "candidate",
    "verified",
    "deprecated",
    "rejected",
}
VALID_RELATIONS: set[str] = {"conflicts", "supersedes", "fallback"}
DEFAULT_WORKSPACE_STATUS = "candidate"
DEFAULT_BUILTIN_STATUS = "verified"


@dataclass(frozen=True)
class SkillRelation:
    src_name: str
    dst_name: str
    kind: str


@dataclass(frozen=True)
class SkillRecord:
    id: str
    name: str
    version: str
    status: str
    risk_level: str
    category: str
    requires_exec: bool
    path: str
    source: str
    description: str = ""
    when_to_use: str = ""
    when_not_to_use: str = ""
    required_tools: list[str] = field(default_factory=list)
    content_hash: str = ""
    search_text: str = ""


@dataclass(frozen=True)
class ReindexResult:
    skills: int
    relations: int
    db_path: Path


@dataclass(frozen=True)
class SkillLifecycleFinding:
    row: sqlite3.Row
    action: str
    reason: str


@dataclass(frozen=True)
class RoutingTestRow:
    query: str
    expected: str
    actual: str
    ok: bool


@dataclass(frozen=True)
class RoutingTestResult:
    rows: list[RoutingTestRow]

    @property
    def passed(self) -> int:
        return sum(1 for row in self.rows if row.ok)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def accuracy(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclass(frozen=True)
class SkillTraceRecord:
    trace_id: str
    ts: str
    session_key: str | None
    query_digest: str | None
    candidates: list[dict[str, Any]]
    selected_skill: str | None
    selection_reason: str
    executed_by: str | None
    wave_no: int | None
    gate_result: str | None
    user_feedback: str | None
    notes: str | None


@dataclass(frozen=True)
class SkillUpdateAssessment:
    kind: Literal["noop", "minor", "major"]
    reasons: list[str]
    changed_fields: list[str]
    current_status: str
    next_status: str
    requires_revalidation: bool


@dataclass(frozen=True)
class SkillUpdateResult:
    assessment: SkillUpdateAssessment
    row: sqlite3.Row | None = None


@dataclass(frozen=True)
class SkillDraftResult:
    draft_id: str
    name: str
    status: str
    markdown: str
    review_json: dict[str, Any]
    routing_cases_json: list[dict[str, Any]]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SkillDraftContent:
    """Composer-produced draft content before persistence."""

    method: str = ""
    review: dict[str, Any] = field(default_factory=dict)
    routing_cases: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SkillSearchMatch:
    name: str
    description: str
    status: str
    risk_level: str
    requires_exec: bool
    category: str
    score: float
    path: str
    usage_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    routing_failure_count: int = 0

    @property
    def success_rate(self) -> float | None:
        attempts = self.success_count + self.failure_count
        if attempts == 0:
            return None
        return self.success_count / attempts

    @property
    def stats_weight(self) -> float:
        if self.usage_count <= 0:
            return 0.0
        success_rate = self.success_rate
        quality = 0.0 if success_rate is None else (success_rate - 0.5) * 20.0
        usage_boost = min(10.0, math.log1p(self.usage_count) * 2.0)
        routing_penalty = min(25.0, self.routing_failure_count * 5.0)
        return quality + usage_boost - routing_penalty


def success_rate_for_row(row: sqlite3.Row) -> float | None:
    attempts = int(row["success_count"]) + int(row["failure_count"])
    if attempts == 0:
        return None
    return int(row["success_count"]) / attempts


def row_to_skill_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "status": row["status"],
        "risk_level": row["risk_level"],
        "category": row["category"],
        "requires_exec": bool(row["requires_exec"]),
        "path": row["path"],
        "source": row["source"],
        "description": row["description"],
        "when_to_use": row["when_to_use"],
        "when_not_to_use": row["when_not_to_use"],
        "required_tools": json.loads(row["required_tools_json"] or "[]"),
        "usage_count": int(row["usage_count"]),
        "success_count": int(row["success_count"]),
        "failure_count": int(row["failure_count"]),
        "routing_failure_count": int(row["routing_failure_count"]),
        "success_rate": success_rate_for_row(row),
        "content_hash": row["content_hash"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def skillstore_path(workspace: Path) -> Path:
    return workspace / ".skillstore" / "skillstore.db"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _parse_frontmatter(content: str) -> dict[str, Any]:
    if not content.startswith("---"):
        return {}
    match = _STRIP_SKILL_FRONTMATTER.match(content)
    if not match:
        return {}
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _body_without_frontmatter(content: str) -> str:
    if not content.startswith("---"):
        return content.strip()
    match = _STRIP_SKILL_FRONTMATTER.match(content)
    if not match:
        return content.strip()
    return content[match.end():].strip()


def _nanobot_meta(frontmatter: dict[str, Any]) -> dict[str, Any]:
    raw = frontmatter.get("metadata") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    payload = raw.get("nanobot", raw.get("openclaw", {}))
    return payload if isinstance(payload, dict) else {}


def _metadata_id(name: str, path: Path, meta: dict[str, Any]) -> str:
    explicit = meta.get("id")
    if explicit:
        return str(explicit)
    digest = hashlib.sha256(f"{name}:{path}".encode("utf-8")).hexdigest()[:32]
    return f"skill-{digest}"


_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def _validate_skill_name(name: str) -> str:
    normalized = name.strip()
    if not _SKILL_NAME_RE.fullmatch(normalized):
        raise ValueError("skill name must be 1-80 chars of letters, numbers, hyphen, or underscore")
    return normalized


def _render_skill_markdown(
    *,
    name: str,
    description: str,
    category: str,
    risk_level: str,
    requires_exec: bool,
    triggers: list[str],
    method: str,
) -> str:
    frontmatter = {
        "name": name,
        "description": description or name,
        "metadata": {
            "nanobot": {
                "id": str(uuid.uuid4()),
                "version": "0.1.0",
                "category": category or "general",
                "risk_level": risk_level or "low",
                "requires_exec": bool(requires_exec),
                "required_tools": [],
                "triggers": triggers,
            }
        },
    }
    return "---\n" + yaml.safe_dump(frontmatter, sort_keys=False).strip() + "\n---\n\n" + (
        method.strip() or "# Method\nDescribe the skill procedure."
    ).strip() + "\n"


def _draft_materials(
    *,
    name: str,
    description: str,
    trigger: str,
    method: str,
    category: str,
    risk_level: str,
    requires_exec: bool,
    content: SkillDraftContent | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any], list[dict[str, str]]]:
    triggers = [item.strip() for item in trigger.splitlines() if item.strip()]
    if not triggers and trigger.strip():
        triggers = [trigger.strip()]
    method = content.method if content and content.method.strip() else method
    markdown = _render_skill_markdown(
        name=name,
        description=description,
        category=category,
        risk_level=risk_level,
        requires_exec=requires_exec,
        triggers=triggers,
        method=method,
    )
    if content and content.review:
        review = dict(content.review)
        review.setdefault("status", "ready")
        review.setdefault("summary", "Composer review completed.")
    else:
        review_red_flags: list[dict[str, object]] = []
        if risk_level in {"medium", "high"}:
            review_red_flags.append(
                {
                    "kind": "security",
                    "severity": risk_level,
                    "message": f"Draft declares {risk_level} risk.",
                }
            )
        review = {
            "status": "ready",
            "summary": "Composer placeholder draft. Full LLM review is not connected yet.",
            "security_risk_level": risk_level,
            "red_flags": review_red_flags,
        }
    routing_cases = content.routing_cases if content and content.routing_cases else [
        {"query": item, "expected": name}
        for item in triggers[:10]
    ] or [{"query": description or name, "expected": name}]
    routing_cases = [
        {
            "query": str(item.get("query") or "").strip(),
            "expected": str(item.get("expected") or name).strip() or name,
        }
        for item in routing_cases
        if isinstance(item, dict) and str(item.get("query") or "").strip()
    ] or [{"query": description or name, "expected": name}]
    input_json = {
        "name": name,
        "description": description,
        "trigger": trigger,
        "method": method,
        "category": category,
        "risk_level": risk_level,
        "requires_exec": requires_exec,
    }
    return input_json, markdown, review, routing_cases


def _status_for(source: str, frontmatter: dict[str, Any], meta: dict[str, Any]) -> str:
    status = str(meta.get("status") or frontmatter.get("status") or "").strip().lower()
    if not status:
        status = DEFAULT_BUILTIN_STATUS if source == "builtin" else DEFAULT_WORKSPACE_STATUS
    if status not in VALID_STATUSES:
        status = DEFAULT_WORKSPACE_STATUS
    return status


def _relation_names(meta: dict[str, Any], top: dict[str, Any], *keys: str) -> list[str]:
    for key in keys:
        values = _json_list(meta.get(key))
        if values:
            return values
        values = _json_list(top.get(key))
        if values:
            return values
    return []


def _skill_from_file(path: Path, *, source: str, default_status: str | None = None) -> tuple[SkillRecord, list[SkillRelation]]:
    content = path.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter(content)
    meta = _nanobot_meta(frontmatter)
    name = str(frontmatter.get("name") or path.parent.name)
    description = str(frontmatter.get("description") or name)
    version = str(meta.get("version") or frontmatter.get("version") or "0.1.0")
    status = default_status or _status_for(source, frontmatter, meta)
    risk_level = str(meta.get("risk_level") or frontmatter.get("risk_level") or "low")
    category = str(meta.get("category") or frontmatter.get("category") or "general")
    requires_exec = bool(meta.get("requires_exec") or frontmatter.get("requires_exec") or False)
    when_to_use = _json_text(frontmatter.get("when_to_use") or meta.get("when_to_use"))
    when_not_to_use = _json_text(frontmatter.get("when_not_to_use") or meta.get("when_not_to_use"))
    required_tools = _json_list(meta.get("required_tools") or frontmatter.get("required_tools"))
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    search_text = "\n".join(
        part
        for part in (
            name,
            description,
            when_to_use,
            when_not_to_use,
            " ".join(_json_list(meta.get("triggers") or frontmatter.get("triggers"))),
        )
        if part
    )

    record = SkillRecord(
        id=_metadata_id(name, path, meta),
        name=name,
        version=version,
        status=status,
        risk_level=risk_level,
        category=category,
        requires_exec=requires_exec,
        path=str(path),
        source=source,
        description=description,
        when_to_use=when_to_use,
        when_not_to_use=when_not_to_use,
        required_tools=required_tools,
        content_hash=content_hash,
        search_text=search_text,
    )
    relations: list[SkillRelation] = []
    for dst in _relation_names(meta, frontmatter, "conflicts_with", "conflictsWith"):
        relations.append(SkillRelation(name, dst, "conflicts"))
    for dst in _relation_names(meta, frontmatter, "supersedes"):
        relations.append(SkillRelation(name, dst, "supersedes"))
    for dst in _relation_names(meta, frontmatter, "fallback_to", "fallbackTo"):
        relations.append(SkillRelation(name, dst, "fallback"))
    return record, relations


def discover_skill_files(
    workspace: Path,
    *,
    builtin_dir: Path | None = None,
    system_dir: Path | None = None,
) -> list[tuple[Path, str, str | None]]:
    """Return skill files in loader precedence order.

    Workspace skills shadow built-in skills by name. A separate system
    directory, when present, is indexed with immutable ``system`` status.
    """
    entries: list[tuple[Path, str, str | None]] = []
    seen: set[str] = set()

    def _add(base: Path, source: str, default_status: str | None = None, *, shadow: bool = True) -> None:
        if not base.exists():
            return
        for path in sorted(base.glob("*/SKILL.md")):
            name = path.parent.name
            if shadow and name in seen:
                continue
            entries.append((path, source, default_status))
            seen.add(name)

    _add(workspace / "skills", "workspace")
    if system_dir is not None:
        _add(system_dir, "system", "system", shadow=False)
    _add(builtin_dir or BUILTIN_SKILLS_DIR, "builtin")
    return entries


def _validate_supersedes_cycles(records: list[SkillRecord], relations: list[SkillRelation]) -> None:
    known = {record.name for record in records}
    graph: dict[str, list[str]] = {name: [] for name in known}
    for relation in relations:
        if relation.kind == "supersedes" and relation.dst_name in known:
            graph.setdefault(relation.src_name, []).append(relation.dst_name)

    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(name: str, stack: list[str]) -> None:
        if name in visited:
            return
        if name in visiting:
            cycle = " -> ".join([*stack, name])
            raise ValueError(f"supersedes cycle detected: {cycle}")
        visiting.add(name)
        for nxt in graph.get(name, []):
            _visit(nxt, [*stack, name])
        visiting.remove(name)
        visited.add(name)

    for name in graph:
        _visit(name, [])


class SkillStore:
    """Registry, relation, and trace storage for workspace skills."""

    def __init__(self, workspace: Path, db_path: Path | None = None) -> None:
        self.workspace = workspace
        self.db_path = db_path or skillstore_path(workspace)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    category TEXT NOT NULL,
                    requires_exec INTEGER NOT NULL DEFAULT 0,
                    path TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    when_to_use TEXT NOT NULL DEFAULT '',
                    when_not_to_use TEXT NOT NULL DEFAULT '',
                    required_tools_json TEXT NOT NULL DEFAULT '[]',
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    routing_failure_count INTEGER NOT NULL DEFAULT 0,
                    content_hash TEXT NOT NULL DEFAULT '',
                    search_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS skill_relations (
                    src_id TEXT NOT NULL,
                    dst_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (src_id, dst_id, kind),
                    FOREIGN KEY (src_id) REFERENCES skills(id) ON DELETE CASCADE,
                    FOREIGN KEY (dst_id) REFERENCES skills(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    session_key TEXT,
                    query_digest TEXT,
                    candidates_json TEXT NOT NULL DEFAULT '[]',
                    selected_skill TEXT,
                    selection_reason TEXT NOT NULL,
                    executed_by TEXT,
                    wave_no INTEGER,
                    gate_result TEXT,
                    user_feedback TEXT,
                    notes TEXT
                );
                CREATE TABLE IF NOT EXISTS skill_drafts (
                    draft_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    markdown TEXT NOT NULL,
                    review_json TEXT NOT NULL DEFAULT '{}',
                    routing_cases_json TEXT NOT NULL DEFAULT '[]',
                    created_by TEXT,
                    approved_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    approved_at TEXT
                );
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS skill_search_fts
                    USING fts5(skill_id UNINDEXED, name, description, search_text)
                    """
                )
            except sqlite3.OperationalError:
                # Some embedded SQLite builds omit FTS5. Search falls back to LIKE.
                pass

    def reindex(
        self,
        *,
        builtin_dir: Path | None = None,
        system_dir: Path | None = None,
    ) -> ReindexResult:
        discovered = discover_skill_files(
            self.workspace,
            builtin_dir=builtin_dir,
            system_dir=system_dir,
        )
        records: list[SkillRecord] = []
        relations: list[SkillRelation] = []
        for path, source, default_status in discovered:
            record, record_relations = _skill_from_file(
                path,
                source=source,
                default_status=default_status,
            )
            records.append(record)
            relations.extend(record_relations)
        _validate_supersedes_cycles(records, relations)
        self.replace_index(records, relations)
        return ReindexResult(skills=len(records), relations=len(relations), db_path=self.db_path)

    def needs_reindex(
        self,
        *,
        builtin_dir: Path | None = None,
        system_dir: Path | None = None,
    ) -> bool:
        if not self.db_path.exists():
            return True
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM skills").fetchone()
            if row is None or int(row["count"]) == 0:
                return True
            db_mtime = self.db_path.stat().st_mtime
            for path, _source, _status in discover_skill_files(
                self.workspace,
                builtin_dir=builtin_dir,
                system_dir=system_dir,
            ):
                if path.stat().st_mtime > db_mtime:
                    return True
        return False

    def ensure_index(
        self,
        *,
        builtin_dir: Path | None = None,
        system_dir: Path | None = None,
    ) -> ReindexResult | None:
        if not self.needs_reindex(builtin_dir=builtin_dir, system_dir=system_dir):
            return None
        return self.reindex(builtin_dir=builtin_dir, system_dir=system_dir)

    def replace_index(self, records: list[SkillRecord], relations: list[SkillRelation]) -> None:
        now = _utc_now()
        by_name = {record.name: record for record in records}
        with self._connect() as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            existing_created = {
                row["id"]: row["created_at"]
                for row in conn.execute("SELECT id, created_at FROM skills")
            }
            existing_status = {
                row["id"]: row["status"]
                for row in conn.execute("SELECT id, status FROM skills")
            }
            counters = {
                row["id"]: row
                for row in conn.execute(
                    """
                    SELECT id, usage_count, success_count, failure_count, routing_failure_count
                    FROM skills
                    """
                )
            }
            conn.execute("DELETE FROM skill_relations")
            conn.execute("DELETE FROM skills")
            self._clear_fts(conn)
            for record in records:
                status = (
                    existing_status.get(record.id)
                    if existing_status.get(record.id) in VALID_STATUSES
                    else record.status
                )
                if record.status == "system":
                    status = "system"
                row_counts = counters.get(record.id)
                conn.execute(
                    """
                    INSERT INTO skills (
                        id, name, version, status, risk_level, category, requires_exec,
                        path, source, description, when_to_use, when_not_to_use,
                        required_tools_json, usage_count, success_count, failure_count,
                        routing_failure_count, content_hash, search_text, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.name,
                        record.version,
                        status,
                        record.risk_level,
                        record.category,
                        int(record.requires_exec),
                        record.path,
                        record.source,
                        record.description,
                        record.when_to_use,
                        record.when_not_to_use,
                        json.dumps(record.required_tools),
                        int(row_counts["usage_count"]) if row_counts else 0,
                        int(row_counts["success_count"]) if row_counts else 0,
                        int(row_counts["failure_count"]) if row_counts else 0,
                        int(row_counts["routing_failure_count"]) if row_counts else 0,
                        record.content_hash,
                        record.search_text,
                        existing_created.get(record.id, now),
                        now,
                    ),
                )
                self._insert_fts(conn, record)
            for relation in relations:
                if relation.kind not in VALID_RELATIONS:
                    continue
                src = by_name.get(relation.src_name)
                dst = by_name.get(relation.dst_name)
                if src is None or dst is None:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO skill_relations (src_id, dst_id, kind, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (src.id, dst.id, relation.kind, now),
                )

    def _clear_fts(self, conn: sqlite3.Connection) -> None:
        if self._has_fts(conn):
            conn.execute("DELETE FROM skill_search_fts")

    def _insert_fts(self, conn: sqlite3.Connection, record: SkillRecord) -> None:
        if self._has_fts(conn):
            conn.execute(
                """
                INSERT INTO skill_search_fts (skill_id, name, description, search_text)
                VALUES (?, ?, ?, ?)
                """,
                (record.id, record.name, record.description, record.search_text),
            )

    @staticmethod
    def _has_fts(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='skill_search_fts'"
        ).fetchone()
        return row is not None

    def list_skills(self, *, include_deprecated: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM skills"
        params: tuple[Any, ...] = ()
        if not include_deprecated:
            query += " WHERE status NOT IN ('deprecated', 'rejected')"
        query += " ORDER BY source DESC, name"
        with self._connect() as conn:
            return list(conn.execute(query, params))

    def get_skill(self, name: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()

    def managed_list(self, *, include_deprecated: bool = True) -> list[dict[str, Any]]:
        return [row_to_skill_payload(row) for row in self.list_skills(include_deprecated=include_deprecated)]

    def managed_search(self, query: str, *, top_k: int = 10) -> list[dict[str, Any]]:
        return [
            {
                "name": match.name,
                "description": match.description,
                "status": match.status,
                "risk_level": match.risk_level,
                "requires_exec": match.requires_exec,
                "category": match.category,
                "score": match.score,
                "stats_weight": match.stats_weight,
                "path": match.path,
                "usage_count": match.usage_count,
                "success_count": match.success_count,
                "failure_count": match.failure_count,
                "routing_failure_count": match.routing_failure_count,
                "success_rate": match.success_rate,
            }
            for match in self.search(query, top_k=top_k, min_status=("candidate", "verified"))
        ]

    def recent_traces_for_skill(self, name: str, *, limit: int = 10) -> list[SkillTraceRecord]:
        limit = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM traces
                WHERE selected_skill = ?
                   OR candidates_json LIKE ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (name, f"%{name}%", limit),
            ).fetchall()
        traces: list[SkillTraceRecord] = []
        for row in rows:
            try:
                candidates = json.loads(row["candidates_json"] or "[]")
            except json.JSONDecodeError:
                candidates = []
            if not isinstance(candidates, list):
                candidates = []
            traces.append(
                SkillTraceRecord(
                    trace_id=row["trace_id"],
                    ts=row["ts"],
                    session_key=row["session_key"],
                    query_digest=row["query_digest"],
                    candidates=[item for item in candidates if isinstance(item, dict)],
                    selected_skill=row["selected_skill"],
                    selection_reason=row["selection_reason"],
                    executed_by=row["executed_by"],
                    wave_no=row["wave_no"],
                    gate_result=row["gate_result"],
                    user_feedback=row["user_feedback"],
                    notes=row["notes"],
                )
            )
        return traces

    def managed_detail(self, name: str, *, trace_limit: int = 10) -> dict[str, Any] | None:
        row = self.get_skill(name)
        if row is None:
            return None
        raw_markdown = ""
        path = Path(row["path"])
        try:
            if path.is_file():
                raw_markdown = path.read_text(encoding="utf-8")
        except OSError:
            raw_markdown = ""
        return {
            "skill": row_to_skill_payload(row),
            "raw_markdown": raw_markdown,
            "relations": self.relations_for_names([name]).get(
                name,
                {"conflicts_with": [], "supersedes": [], "fallback_to": []},
            ),
            "traces": [trace.__dict__ for trace in self.recent_traces_for_skill(name, limit=trace_limit)],
        }

    def classify_skill_update(self, name: str, new_markdown: str) -> SkillUpdateAssessment:
        row = self.get_skill(name)
        if row is None:
            raise KeyError(f"skill not found: {name}")
        if row["status"] == "system":
            raise ValueError(f"system skill '{name}' cannot be edited")

        path = Path(row["path"]).resolve(strict=False)
        workspace_skills = (self.workspace / "skills").resolve(strict=False)
        try:
            path.relative_to(workspace_skills)
        except ValueError as exc:
            raise ValueError(f"skill '{name}' is not an editable workspace skill") from exc
        if path.name != "SKILL.md":
            raise ValueError(f"skill '{name}' does not point at SKILL.md")

        old_markdown = path.read_text(encoding="utf-8")
        if old_markdown == new_markdown:
            return SkillUpdateAssessment(
                kind="noop",
                reasons=[],
                changed_fields=[],
                current_status=row["status"],
                next_status=row["status"],
                requires_revalidation=False,
            )

        old_frontmatter = _parse_frontmatter(old_markdown)
        new_frontmatter = _parse_frontmatter(new_markdown)
        new_name = str(new_frontmatter.get("name") or path.parent.name)
        if new_name != name:
            raise ValueError("SKILL.md frontmatter name must not change")

        old_meta = _nanobot_meta(old_frontmatter)
        new_meta = _nanobot_meta(new_frontmatter)
        changed_fields: list[str] = []
        reasons: list[str] = []
        major = False

        def _changed(label: str, old_value: Any, new_value: Any, *, major_field: bool = False) -> None:
            nonlocal major
            if old_value == new_value:
                return
            changed_fields.append(label)
            if major_field:
                major = True
                reasons.append(f"{label} changed")

        _changed("description", old_frontmatter.get("description"), new_frontmatter.get("description"))
        _changed("when_to_use", old_frontmatter.get("when_to_use"), new_frontmatter.get("when_to_use"))
        _changed("when_not_to_use", old_frontmatter.get("when_not_to_use"), new_frontmatter.get("when_not_to_use"))
        _changed("triggers", old_meta.get("triggers") or old_frontmatter.get("triggers"), new_meta.get("triggers") or new_frontmatter.get("triggers"))
        _changed("category", old_meta.get("category") or old_frontmatter.get("category"), new_meta.get("category") or new_frontmatter.get("category"))
        _changed("relations.conflicts_with", old_meta.get("conflicts_with") or old_frontmatter.get("conflicts_with"), new_meta.get("conflicts_with") or new_frontmatter.get("conflicts_with"))
        _changed("relations.supersedes", old_meta.get("supersedes") or old_frontmatter.get("supersedes"), new_meta.get("supersedes") or new_frontmatter.get("supersedes"))
        _changed("relations.fallback_to", old_meta.get("fallback_to") or old_frontmatter.get("fallback_to"), new_meta.get("fallback_to") or new_frontmatter.get("fallback_to"))
        _changed("risk_level", old_meta.get("risk_level") or old_frontmatter.get("risk_level"), new_meta.get("risk_level") or new_frontmatter.get("risk_level"), major_field=True)
        _changed("requires_exec", old_meta.get("requires_exec") or old_frontmatter.get("requires_exec"), new_meta.get("requires_exec") or new_frontmatter.get("requires_exec"), major_field=True)
        _changed("required_tools", old_meta.get("required_tools") or old_frontmatter.get("required_tools"), new_meta.get("required_tools") or new_frontmatter.get("required_tools"), major_field=True)
        if _body_without_frontmatter(old_markdown) != _body_without_frontmatter(new_markdown):
            changed_fields.append("method")
            major = True
            reasons.append("method body changed")

        kind: Literal["noop", "minor", "major"]
        if major:
            kind = "major"
        elif changed_fields:
            kind = "minor"
            reasons.append("metadata/trigger-only change")
        else:
            kind = "minor"
            changed_fields.append("frontmatter")
            reasons.append("frontmatter formatting changed")

        current_status = str(row["status"])
        next_status = "candidate" if kind == "major" and current_status == "verified" else current_status
        return SkillUpdateAssessment(
            kind=kind,
            reasons=reasons,
            changed_fields=changed_fields,
            current_status=current_status,
            next_status=next_status,
            requires_revalidation=kind == "major",
        )

    def update_skill_markdown(
        self,
        name: str,
        new_markdown: str,
        *,
        system_dir: Path | None = None,
    ) -> SkillUpdateResult:
        assessment = self.classify_skill_update(name, new_markdown)
        if assessment.kind == "noop":
            return SkillUpdateResult(assessment=assessment, row=self.get_skill(name))
        row = self.get_skill(name)
        if row is None:
            raise KeyError(f"skill not found: {name}")
        path = Path(row["path"])
        old_markdown = path.read_text(encoding="utf-8")
        path.write_text(new_markdown, encoding="utf-8")
        try:
            self.reindex(system_dir=system_dir)
            if assessment.next_status != assessment.current_status:
                self.set_status(name, assessment.next_status)
        except Exception:
            path.write_text(old_markdown, encoding="utf-8")
            self.reindex(system_dir=system_dir)
            raise
        updated = self.get_skill(name)
        return SkillUpdateResult(assessment=assessment, row=updated)

    @staticmethod
    def _draft_from_row(row: sqlite3.Row) -> SkillDraftResult:
        try:
            review = json.loads(row["review_json"] or "{}")
        except json.JSONDecodeError:
            review = {}
        try:
            routing_cases = json.loads(row["routing_cases_json"] or "[]")
        except json.JSONDecodeError:
            routing_cases = []
        return SkillDraftResult(
            draft_id=row["draft_id"],
            name=row["name"],
            status=row["status"],
            markdown=row["markdown"],
            review_json=review if isinstance(review, dict) else {},
            routing_cases_json=[item for item in routing_cases if isinstance(item, dict)]
            if isinstance(routing_cases, list)
            else [],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def create_skill_draft(
        self,
        *,
        name: str,
        description: str,
        trigger: str = "",
        method: str = "",
        category: str = "general",
        risk_level: str = "low",
        requires_exec: bool = False,
        created_by: str = "webui",
        content: SkillDraftContent | None = None,
    ) -> SkillDraftResult:
        name = _validate_skill_name(name)
        if self.get_skill(name) is not None:
            raise ValueError(f"skill '{name}' already exists")
        skill_dir = self.workspace / "skills" / name
        if skill_dir.exists():
            raise ValueError(f"skill directory already exists: {name}")
        input_json, markdown, review, routing_cases = _draft_materials(
            name=name,
            description=description,
            trigger=trigger,
            method=method,
            category=category,
            risk_level=risk_level,
            requires_exec=requires_exec,
            content=content,
        )
        now = _utc_now()
        draft_id = f"draft-{uuid.uuid4().hex}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO skill_drafts (
                    draft_id, name, status, input_json, markdown, review_json,
                    routing_cases_json, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    name,
                    "ready",
                    json.dumps(input_json),
                    markdown,
                    json.dumps(review),
                    json.dumps(routing_cases),
                    created_by,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM skill_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        assert row is not None
        return self._draft_from_row(row)

    def start_skill_draft(
        self,
        *,
        name: str,
        description: str,
        trigger: str = "",
        method: str = "",
        category: str = "general",
        risk_level: str = "low",
        requires_exec: bool = False,
        created_by: str = "webui",
    ) -> SkillDraftResult:
        name = _validate_skill_name(name)
        if self.get_skill(name) is not None:
            raise ValueError(f"skill '{name}' already exists")
        skill_dir = self.workspace / "skills" / name
        if skill_dir.exists():
            raise ValueError(f"skill directory already exists: {name}")
        input_json = {
            "name": name,
            "description": description,
            "trigger": trigger,
            "method": method,
            "category": category,
            "risk_level": risk_level,
            "requires_exec": requires_exec,
        }
        review = {
            "status": "composing",
            "summary": "Composer is generating the draft.",
            "red_flags": [],
        }
        now = _utc_now()
        draft_id = f"draft-{uuid.uuid4().hex}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO skill_drafts (
                    draft_id, name, status, input_json, markdown, review_json,
                    routing_cases_json, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_id,
                    name,
                    "composing",
                    json.dumps(input_json),
                    "",
                    json.dumps(review),
                    "[]",
                    created_by,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM skill_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        assert row is not None
        return self._draft_from_row(row)

    def complete_skill_draft(
        self,
        draft_id: str,
        *,
        content: SkillDraftContent | None = None,
        error: str | None = None,
    ) -> SkillDraftResult:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"draft not found: {draft_id}")
        if row["status"] not in {"composing", "failed"}:
            return self._draft_from_row(row)
        now = _utc_now()
        if error:
            review = {
                "status": "failed",
                "summary": error,
                "red_flags": [{"kind": "composer", "severity": "medium", "message": error}],
            }
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE skill_drafts
                    SET status = ?, review_json = ?, updated_at = ?
                    WHERE draft_id = ?
                    """,
                    ("failed", json.dumps(review), now, draft_id),
                )
                updated = conn.execute(
                    "SELECT * FROM skill_drafts WHERE draft_id = ?",
                    (draft_id,),
                ).fetchone()
            assert updated is not None
            return self._draft_from_row(updated)

        try:
            values = json.loads(row["input_json"] or "{}")
        except json.JSONDecodeError:
            values = {}
        input_json, markdown, review, routing_cases = _draft_materials(
            name=str(values.get("name") or row["name"]),
            description=str(values.get("description") or ""),
            trigger=str(values.get("trigger") or ""),
            method=str(values.get("method") or ""),
            category=str(values.get("category") or "general"),
            risk_level=str(values.get("risk_level") or "low"),
            requires_exec=bool(values.get("requires_exec") or False),
            content=content,
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE skill_drafts
                SET status = ?, input_json = ?, markdown = ?, review_json = ?,
                    routing_cases_json = ?, updated_at = ?
                WHERE draft_id = ?
                """,
                (
                    "ready",
                    json.dumps(input_json),
                    markdown,
                    json.dumps(review),
                    json.dumps(routing_cases),
                    now,
                    draft_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM skill_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        assert updated is not None
        return self._draft_from_row(updated)

    def get_skill_draft(self, draft_id: str) -> SkillDraftResult | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        return self._draft_from_row(row) if row is not None else None

    def list_skill_drafts(self, *, include_approved: bool = False) -> list[SkillDraftResult]:
        query = "SELECT * FROM skill_drafts"
        params: tuple[str, ...] = ()
        if not include_approved:
            query += " WHERE status != ?"
            params = ("approved",)
        query += " ORDER BY updated_at DESC, created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._draft_from_row(row) for row in rows]

    def approve_composed_draft(
        self,
        draft_id: str,
        *,
        approved_by: str = "webui",
        system_dir: Path | None = None,
    ) -> tuple[SkillDraftResult, sqlite3.Row | None]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"draft not found: {draft_id}")
        if row["status"] == "approved":
            return self._draft_from_row(row), self.get_skill(row["name"])
        if row["status"] not in {"ready"}:
            raise ValueError(f"cannot approve draft in status {row['status']}")
        name = _validate_skill_name(row["name"])
        skill_dir = self.workspace / "skills" / name
        skill_file = skill_dir / "SKILL.md"
        routing_file = skill_dir / "routing_cases.json"
        if skill_file.exists() or self.get_skill(name) is not None:
            raise ValueError(f"skill '{name}' already exists")

        skill_dir.mkdir(parents=True, exist_ok=False)
        wrote_skill = False
        try:
            skill_file.write_text(row["markdown"], encoding="utf-8")
            wrote_skill = True
            routing_cases = json.loads(row["routing_cases_json"] or "[]")
            if isinstance(routing_cases, list) and routing_cases:
                routing_file.write_text(
                    json.dumps({"cases": routing_cases}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            self.reindex(system_dir=system_dir)
            now = _utc_now()
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE skill_drafts
                    SET status = ?, approved_by = ?, approved_at = ?, updated_at = ?
                    WHERE draft_id = ?
                    """,
                    ("approved", approved_by, now, now, draft_id),
                )
                updated_draft = conn.execute(
                    "SELECT * FROM skill_drafts WHERE draft_id = ?",
                    (draft_id,),
                ).fetchone()
            assert updated_draft is not None
            return self._draft_from_row(updated_draft), self.get_skill(name)
        except Exception:
            if wrote_skill:
                with contextlib.suppress(OSError):
                    if routing_file.exists():
                        routing_file.unlink()
                    if skill_file.exists():
                        skill_file.unlink()
                    skill_dir.rmdir()
            self.reindex(system_dir=system_dir)
            raise

    def status_counts(self) -> dict[str, int]:
        rows = self.list_skills(include_deprecated=True)
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts

    def set_status(self, name: str, status: str) -> None:
        """Low-level status setter.

        Prefer approve_draft/promote/deprecate_skill/reject_skill for governance
        transitions. This method remains for indexing compatibility and tests.
        """
        status = status.strip().lower()
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}")
        if status == "system":
            raise ValueError("status=system can only be assigned by system skill indexing")
        with self._connect() as conn:
            row = conn.execute("SELECT id, status FROM skills WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise KeyError(f"skill not found: {name}")
            if row["status"] == "system":
                raise ValueError(f"system skill '{name}' status cannot be changed")
            conn.execute(
                "UPDATE skills SET status = ?, updated_at = ? WHERE id = ?",
                (status, _utc_now(), row["id"]),
            )

    def _transition_status(
        self,
        name: str,
        target: str,
        *,
        allowed_from: set[str],
        idempotent_from: set[str] | None = None,
    ) -> sqlite3.Row:
        if target not in VALID_STATUSES or target == "system":
            raise ValueError(f"invalid target status {target!r}")
        idempotent = idempotent_from or {target}
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM skills WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise KeyError(f"skill not found: {name}")
            current = str(row["status"])
            if current == "system":
                raise ValueError(f"system skill '{name}' status cannot be changed")
            if current in idempotent:
                return row
            if current not in allowed_from:
                raise ValueError(f"cannot transition skill '{name}' from {current} to {target}")
            conn.execute(
                "UPDATE skills SET status = ?, updated_at = ? WHERE id = ?",
                (target, _utc_now(), row["id"]),
            )
            updated = conn.execute("SELECT * FROM skills WHERE id = ?", (row["id"],)).fetchone()
            assert updated is not None
            return updated

    def approve_draft(self, name: str) -> sqlite3.Row:
        """Register a human-approved draft as candidate.

        This is the shared governance path for CLI approve and future Web UI
        "Register" actions. It intentionally does not mark a skill verified.
        """
        return self._transition_status(
            name,
            "candidate",
            allowed_from={"draft"},
            idempotent_from={"candidate"},
        )

    def promote(self, name: str) -> sqlite3.Row:
        """Promote an operationally proven candidate to verified."""
        return self._transition_status(
            name,
            "verified",
            allowed_from={"candidate"},
            idempotent_from={"verified"},
        )

    def deprecate_skill(self, name: str) -> sqlite3.Row:
        """Mark a non-system skill as deprecated."""
        return self._transition_status(
            name,
            "deprecated",
            allowed_from={"draft", "candidate", "verified", "rejected"},
        )

    def reject_skill(self, name: str) -> sqlite3.Row:
        """Reject a non-system skill."""
        return self._transition_status(
            name,
            "rejected",
            allowed_from={"draft", "candidate", "verified", "deprecated"},
        )

    def hot_path_report(
        self,
        *,
        min_usage: int = 5,
        min_success_rate: float = 0.80,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        rows = [
            row for row in self.list_skills(include_deprecated=False)
            if row["status"] in {"candidate", "verified"}
            and row["source"] != "system"
            and row["risk_level"] == "low"
            and not bool(row["requires_exec"])
            and int(row["usage_count"]) >= min_usage
            and (success_rate_for_row(row) or 0.0) >= min_success_rate
            and int(row["routing_failure_count"]) == 0
        ]
        rows.sort(
            key=lambda row: (
                int(row["usage_count"]),
                success_rate_for_row(row) or 0.0,
                row["name"],
            ),
            reverse=True,
        )
        return rows[:limit]

    def lifecycle_report(
        self,
        *,
        min_usage: int = 5,
        max_success_rate: float = 0.50,
        min_routing_failures: int = 3,
        apply_deprecate: bool = False,
    ) -> list[SkillLifecycleFinding]:
        findings: list[SkillLifecycleFinding] = []
        for row in self.list_skills(include_deprecated=False):
            if row["status"] == "system":
                continue
            usage = int(row["usage_count"])
            routing_failures = int(row["routing_failure_count"])
            rate = success_rate_for_row(row)
            if usage < min_usage and routing_failures < min_routing_failures:
                continue
            if routing_failures >= min_routing_failures and (rate is None or rate <= max_success_rate):
                findings.append(
                    SkillLifecycleFinding(
                        row=row,
                        action="deprecate",
                        reason="routing failures plus low/unknown success rate",
                    )
                )
            elif rate is not None and rate <= max_success_rate:
                findings.append(SkillLifecycleFinding(row=row, action="review", reason="low success rate"))
            elif routing_failures >= min_routing_failures:
                findings.append(SkillLifecycleFinding(row=row, action="review", reason="routing failures"))

        if apply_deprecate:
            for finding in findings:
                if finding.action == "deprecate":
                    self.deprecate_skill(finding.row["name"])
        return findings

    def run_routing_test(
        self,
        cases: list[dict[str, object]],
        *,
        top_k: int = 3,
        min_status: Iterable[str] = ("candidate", "verified"),
    ) -> RoutingTestResult:
        rows: list[RoutingTestRow] = []
        for item in cases:
            query = str(item.get("query") or "").strip()
            expected = str(item.get("expected") or item.get("skill") or "").strip()
            if not query or not expected:
                rows.append(RoutingTestRow(query=query, expected=expected, actual="", ok=False))
                continue
            matches = self.search(query, top_k=top_k, min_status=min_status)
            actual = matches[0].name if matches else ""
            rows.append(RoutingTestRow(query=query, expected=expected, actual=actual, ok=actual == expected))
        return RoutingTestResult(rows=rows)

    def relations_for_names(self, names: Iterable[str]) -> dict[str, dict[str, list[str]]]:
        names_tuple = tuple(dict.fromkeys(names))
        if not names_tuple:
            return {}
        placeholders = ",".join("?" for _ in names_tuple)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT src.name AS src_name, dst.name AS dst_name, r.kind
                FROM skill_relations r
                JOIN skills src ON src.id = r.src_id
                JOIN skills dst ON dst.id = r.dst_id
                WHERE src.name IN ({placeholders})
                """,
                names_tuple,
            ).fetchall()
        result: dict[str, dict[str, list[str]]] = {
            name: {"conflicts_with": [], "supersedes": [], "fallback_to": []}
            for name in names_tuple
        }
        key_for_kind = {
            "conflicts": "conflicts_with",
            "supersedes": "supersedes",
            "fallback": "fallback_to",
        }
        for row in rows:
            key = key_for_kind.get(row["kind"])
            if key:
                result.setdefault(row["src_name"], {"conflicts_with": [], "supersedes": [], "fallback_to": []})[
                    key
                ].append(row["dst_name"])
        return result

    def search(self, query: str, *, top_k: int = 5, min_status: Iterable[str] | None = None) -> list[SkillSearchMatch]:
        statuses = tuple(min_status or ("candidate", "verified"))
        if not statuses:
            return []
        with self._connect() as conn:
            if self._has_fts(conn):
                try:
                    rows = conn.execute(
                        f"""
                        SELECT s.*, bm25(skill_search_fts) AS score
                        FROM skill_search_fts
                        JOIN skills s ON s.id = skill_search_fts.skill_id
                        WHERE skill_search_fts MATCH ?
                          AND s.status IN ({",".join("?" for _ in statuses)})
                        ORDER BY score
                        LIMIT ?
                        """,
                        (query, *statuses, top_k),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                else:
                    if rows:
                        return [self._match_from_row(row, score=-float(row["score"])) for row in rows]
            terms = [term for term in query.split() if len(term) >= 2] or [query]
            clauses = []
            params: list[Any] = [*statuses]
            for term in terms[:8]:
                clauses.append("(name LIKE ? OR description LIKE ? OR search_text LIKE ?)")
                like = f"%{term}%"
                params.extend([like, like, like])
            params.append(top_k)
            rows = conn.execute(
                f"""
                SELECT * FROM skills
                WHERE status IN ({",".join("?" for _ in statuses)})
                  AND ({" OR ".join(clauses)})
                ORDER BY name
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            return [self._match_from_row(row, score=1.0) for row in rows]

    @staticmethod
    def _match_from_row(row: sqlite3.Row, *, score: float) -> SkillSearchMatch:
        return SkillSearchMatch(
            name=row["name"],
            description=row["description"],
            status=row["status"],
            risk_level=row["risk_level"],
            requires_exec=bool(row["requires_exec"]),
            category=row["category"],
            score=score,
            path=row["path"],
            usage_count=int(row["usage_count"]),
            success_count=int(row["success_count"]),
            failure_count=int(row["failure_count"]),
            routing_failure_count=int(row["routing_failure_count"]),
        )

    def record_skill_outcome(
        self,
        name: str,
        *,
        gate_result: str | None = None,
        user_feedback: str | None = None,
    ) -> None:
        """Update usage counters for one selected skill."""
        gate = (gate_result or "").strip().lower()
        feedback = (user_feedback or "").strip().lower()
        success_inc = 1 if gate == "ok" else 0
        failure_inc = 1 if gate == "error" else 0
        routing_failure_inc = 1 if feedback in {"routing_failure", "wrong_skill"} else 0
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE skills
                SET usage_count = usage_count + 1,
                    success_count = success_count + ?,
                    failure_count = failure_count + ?,
                    routing_failure_count = routing_failure_count + ?,
                    updated_at = ?
                WHERE name = ? AND status != 'system'
                """,
                (success_inc, failure_inc, routing_failure_inc, _utc_now(), name),
            )

    def record_trace(
        self,
        *,
        trace_id: str,
        session_key: str | None = None,
        query_digest: str | None = None,
        candidates: list[dict[str, Any]] | None = None,
        selected_skill: str | None = None,
        selection_reason: str = "none",
        executed_by: str | None = None,
        wave_no: int | None = None,
        gate_result: str | None = None,
        user_feedback: str | None = None,
        notes: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO traces (
                    trace_id, ts, session_key, query_digest, candidates_json,
                    selected_skill, selection_reason, executed_by, wave_no,
                    gate_result, user_feedback, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    _utc_now(),
                    session_key,
                    query_digest,
                    json.dumps(candidates or [], ensure_ascii=False),
                    selected_skill,
                    selection_reason,
                    executed_by,
                    wave_no,
                    gate_result,
                    user_feedback,
                    notes,
                ),
            )
        if selected_skill:
            self.record_skill_outcome(
                selected_skill,
                gate_result=gate_result,
                user_feedback=user_feedback,
            )
