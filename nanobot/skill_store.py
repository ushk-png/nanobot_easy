"""SQLite-backed skill registry and trace store."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
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

    def set_status(self, name: str, status: str) -> None:
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
