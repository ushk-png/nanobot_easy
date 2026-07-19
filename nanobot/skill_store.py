"""SQLite-backed skill registry and trace store."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import math
import re
import secrets
import sqlite3
import uuid
import zipfile
from collections.abc import Callable, Iterable
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
DEFAULT_WORKSPACE_STATUS = "draft"
DEFAULT_BUILTIN_STATUS = "verified"
_MAX_PACKAGE_FILES = 80
_MAX_PACKAGE_FILE_BYTES = 256 * 1024
_MAX_PACKAGE_TOTAL_BYTES = 2 * 1024 * 1024
_DANGEROUS_PACKAGE_SUFFIXES = {
    ".bat",
    ".cmd",
    ".exe",
    ".js",
    ".mjs",
    ".ps1",
    ".py",
    ".rb",
    ".sh",
    ".zsh",
}
_ROUTING_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "between",
    "but",
    "by",
    "do",
    "does",
    "for",
    "from",
    "give",
    "how",
    "i",
    "in",
    "into",
    "is",
    "it",
    "me",
    "of",
    "on",
    "or",
    "should",
    "tell",
    "the",
    "these",
    "this",
    "to",
    "what",
    "when",
    "which",
    "with",
}


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
    install_sources: list[str] = field(default_factory=list)
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
class SkillAuditReport:
    generated_at: str
    report_path: str
    summary: dict[str, int]
    attention: list[dict[str, Any]]
    reference: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "report_path": self.report_path,
            "summary": self.summary,
            "attention": self.attention,
            "reference": self.reference,
        }


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
    intent_summary: str | None
    candidates: list[dict[str, Any]]
    selected_skill: str | None
    selection_reason: str
    executed_by: str | None
    wave_no: int | None
    duration_ms: int | None
    gate_result: str | None
    user_feedback: str | None
    notes: str | None


@dataclass(frozen=True)
class RelayClientRecord:
    client_id: str
    tool_name: str
    key_id: str
    model_preset: str
    status: str
    created_at: str
    updated_at: str
    expires_at: str | None = None
    last_used_at: str | None = None
    last_used_ip: str | None = None


@dataclass(frozen=True)
class IssuedRelayClient:
    record: RelayClientRecord
    token: str


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
    attachments: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class SkillSearchMatch:
    name: str
    description: str
    when_to_use: str
    when_not_to_use: str
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


def _cosine_similarity(left: list[float], right: list[float]) -> float | None:
    if not left or not right or len(left) != len(right):
        return None
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm <= 0 or right_norm <= 0:
        return None
    return dot / (left_norm * right_norm)


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
        "install_sources": json.loads(row["install_sources_json"] or "[]"),
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


def _short_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


_RELAY_TOKEN_PREFIX = "nbrelay"
_RELAY_HASH_ITERATIONS = 200_000


def _relay_secret_hash(secret: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        _RELAY_HASH_ITERATIONS,
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(_RELAY_HASH_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
            base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
        )
    )


def _b64decode_unpadded(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _verify_relay_secret(secret: str, verifier: str) -> bool:
    try:
        scheme, iterations_text, salt_text, digest_text = verifier.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = _b64decode_unpadded(salt_text)
        expected = _b64decode_unpadded(digest_text)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def parse_relay_token(token: str) -> tuple[str, str] | None:
    """Return ``(key_id, secret)`` from an opaque relay token."""
    value = (token or "").strip()
    prefix = f"{_RELAY_TOKEN_PREFIX}_"
    if not value.startswith(prefix):
        return None
    rest = value[len(prefix):]
    if "_" not in rest:
        return None
    key_id, secret = rest.split("_", 1)
    if not key_id or not secret:
        return None
    return key_id, secret


def _safe_relay_client_id(value: str) -> str:
    client = re.sub(r"[^A-Za-z0-9_.-]+", "-", (value or "").strip()).strip(".-")
    if not client:
        raise ValueError("relay client id is required")
    if len(client) > 80:
        raise ValueError("relay client id is too long")
    return client


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


def _routing_terms(text: str) -> set[str]:
    terms = {
        term
        for term in re.findall(r"[a-z0-9][a-z0-9_-]*|[가-힣]{2,}", text.lower())
        if len(term) >= 2 and term not in _ROUTING_STOPWORDS
    }
    return terms


def _phrase_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in re.split(r"[\n,;|]+", text.lower()):
        line = " ".join(re.findall(r"[a-z0-9][a-z0-9_-]*|[가-힣]{2,}", raw))
        # Phrase evidence needs at least two words; a lone word is term-level
        # evidence and is already scored (and capped) as such.
        if len(line) >= 8 and " " in line:
            lines.append(line)
    return lines


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


def _parse_frontmatter_strict(content: str, path: Path) -> dict[str, Any]:
    if not content.startswith("---"):
        return {}
    match = _STRIP_SKILL_FRONTMATTER.match(content)
    if not match:
        raise ValueError(f"{path}: malformed YAML frontmatter delimiter")
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: malformed YAML frontmatter: {exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"{path}: YAML frontmatter must be a mapping")
    return parsed


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
    required_tools: list[str] | None = None,
    install_sources: list[str] | None = None,
) -> str:
    nanobot_meta = {
        "id": str(uuid.uuid4()),
        "version": "0.1.0",
        "category": category or "general",
        "risk_level": risk_level or "low",
        "requires_exec": bool(requires_exec),
        "required_tools": required_tools or [],
        "triggers": triggers,
    }
    if install_sources:
        nanobot_meta["install_sources"] = install_sources
    frontmatter = {
        "name": name,
        "description": description or name,
        "metadata": {"nanobot": nanobot_meta},
    }
    return "---\n" + yaml.safe_dump(frontmatter, sort_keys=False).strip() + "\n---\n\n" + (
        method.strip() or "# Method\nDescribe the skill procedure."
    ).strip() + "\n"


def parse_skill_markdown(content: str, *, source_name: str = "pasted skill") -> dict[str, Any]:
    """Parse pasted skill content into the nanobot draft shape.

    This is intentionally deterministic and LLM-free. It is used by WebUI
    import flows before any draft persistence happens, so malformed or
    incomplete external content becomes form feedback instead of a live skill.
    """

    warnings: list[str] = []
    errors: list[str] = []
    estimated_fields: list[str] = []
    path = Path(source_name)
    try:
        frontmatter = _parse_frontmatter_strict(content, path)
    except ValueError as exc:
        frontmatter = {}
        errors.append(str(exc))
    meta = _nanobot_meta(frontmatter)
    body = _body_without_frontmatter(content)
    first_heading = re.search(r"^#{1,2}\s+(.+?)\s*$", body, flags=re.MULTILINE)
    inferred_name = re.sub(
        r"[^A-Za-z0-9_-]+",
        "-",
        (first_heading.group(1) if first_heading else "imported-skill").strip().lower(),
    ).strip("-")
    if not inferred_name:
        inferred_name = "imported-skill"

    name = str(frontmatter.get("name") or meta.get("name") or inferred_name)
    try:
        name = _validate_skill_name(name)
    except ValueError as exc:
        errors.append(str(exc))
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:80] or "imported-skill"
        estimated_fields.append("name")

    description = str(frontmatter.get("description") or meta.get("description") or "").strip()
    if not description:
        description = name
        estimated_fields.append("description")
        warnings.append("description was not declared; using the skill name.")

    category = str(meta.get("category") or frontmatter.get("category") or "").strip()
    if not category:
        category = "general"
        estimated_fields.append("category")
    risk_level = str(meta.get("risk_level") or frontmatter.get("risk_level") or "").strip().lower()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "low"
        estimated_fields.append("risk_level")
    requires_exec_raw = meta.get("requires_exec", frontmatter.get("requires_exec"))
    if requires_exec_raw is None:
        requires_exec = False
        estimated_fields.append("requires_exec")
    else:
        requires_exec = bool(requires_exec_raw)

    triggers = _json_list(meta.get("triggers") or frontmatter.get("triggers"))
    if not triggers:
        trigger_section = _markdown_section_body(body, "triggers", "trigger utterances")
        triggers = [
            line.strip().lstrip("-*").strip()
            for line in trigger_section.splitlines()
            if line.strip().lstrip("-*").strip()
        ]
    when_to_use = _json_text(frontmatter.get("when_to_use") or meta.get("when_to_use")) or (
        _markdown_section_body(body, "when to use")
    )
    when_not_to_use = _json_text(frontmatter.get("when_not_to_use") or meta.get("when_not_to_use")) or (
        _markdown_section_body(body, "when not to use")
    )
    required_tools = _json_list(meta.get("required_tools") or frontmatter.get("required_tools"))
    install_sources = _json_list(meta.get("install_sources") or frontmatter.get("install_sources"))

    validation_meta = dict(meta)
    if name.lower().endswith(("-setup", "-usage")):
        validation_meta.setdefault("external_tool", True)
    try:
        _validate_external_tool_shape(
            name=name,
            frontmatter=frontmatter,
            meta=validation_meta,
            body=body,
            category=category,
            risk_level=risk_level,
            requires_exec=requires_exec,
            install_sources=install_sources,
        )
    except ValueError as exc:
        errors.append(str(exc))

    normalized_frontmatter = {
        "name": name,
        "description": description,
        "metadata": {
            "nanobot": {
                "id": str(meta.get("id") or uuid.uuid4()),
                "version": str(meta.get("version") or frontmatter.get("version") or "0.1.0"),
                "category": category,
                "risk_level": risk_level,
                "requires_exec": requires_exec,
                "required_tools": required_tools,
                "triggers": triggers,
            }
        },
    }
    if install_sources:
        normalized_frontmatter["metadata"]["nanobot"]["install_sources"] = install_sources
    for key in ("conflicts_with", "supersedes", "fallback_to"):
        values = _relation_names(meta, frontmatter, key, key.replace("_", ""))
        if values:
            normalized_frontmatter["metadata"]["nanobot"][key] = values

    normalized_markdown = (
        "---\n"
        + yaml.safe_dump(normalized_frontmatter, sort_keys=False, allow_unicode=True).strip()
        + "\n---\n\n"
        + (body.strip() or "# Method\nDescribe the skill procedure.")
        + "\n"
    )
    return {
        "mode": "frontmatter" if frontmatter else "deterministic",
        "fields": {
            "name": name,
            "description": description,
            "trigger": "\n".join(triggers),
            "method": body.strip(),
            "category": category,
            "risk_level": risk_level,
            "requires_exec": requires_exec,
            "required_tools": required_tools,
            "install_sources": install_sources,
        },
        "sections": {
            "when_to_use": when_to_use,
            "when_not_to_use": when_not_to_use,
            "method": _markdown_section_body(body, "method"),
            "failure_rules": _markdown_section_body(body, "failure rules", "failure rule"),
        },
        "normalized_markdown": normalized_markdown,
        "estimated_fields": sorted(set(estimated_fields)),
        "validation": {"errors": errors, "warnings": warnings},
        "preserved_method": True,
    }


def _normalize_package_relpath(path: str) -> str:
    raw = str(path or "").replace("\\", "/").strip().lstrip("/")
    if not raw:
        raise ValueError("package file path is empty")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"invalid package file path: {path}")
    if any(part.startswith(".") and part not in {".gitkeep"} for part in parts):
        # Hidden paths are not needed for skill packages and often contain
        # editor, VCS, or OS metadata.
        raise ValueError(f"hidden package paths are not allowed: {path}")
    return "/".join(parts)


def _package_file_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if not isinstance(content, str):
        raise ValueError("package file content must be text")
    size = len(content.encode("utf-8"))
    if size > _MAX_PACKAGE_FILE_BYTES:
        raise ValueError(f"package file is too large: {item.get('path')}")
    return content


def _package_root_for_skill(paths: list[str]) -> str:
    skill_paths = [path for path in paths if path.endswith("SKILL.md")]
    if not skill_paths:
        raise ValueError("package must include SKILL.md")
    return min(((path.rsplit("/", 1)[0] if "/" in path else "") for path in skill_paths), key=len)


def _strip_package_root(path: str, root: str) -> str:
    if not root:
        return path
    prefix = f"{root}/"
    if not path.startswith(prefix):
        raise ValueError(f"package file is outside the SKILL.md root: {path}")
    return path[len(prefix):]


def _attachment_warnings(attachments: list[dict[str, str]]) -> list[str]:
    warnings: list[str] = []
    for item in attachments:
        rel = item["path"]
        suffix = Path(rel).suffix.lower()
        if suffix in _DANGEROUS_PACKAGE_SUFFIXES:
            warnings.append(f"{rel} may execute code; review before registration.")
    return warnings


def parse_skill_package_files(
    files: list[dict[str, Any]],
    *,
    source_name: str = "skill package",
) -> dict[str, Any]:
    """Parse a folder/zip-like skill package into the import preview shape.

    The package must contain one ``SKILL.md``. Other text files are preserved as
    draft attachments and materialized only after human approval.
    """

    if not files:
        raise ValueError("package has no files")
    if len(files) > _MAX_PACKAGE_FILES:
        raise ValueError(f"package has too many files; max {_MAX_PACKAGE_FILES}")

    normalized: dict[str, str] = {}
    total_size = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("package files must be objects")
        rel = _normalize_package_relpath(str(item.get("path") or item.get("name") or ""))
        if rel in normalized:
            raise ValueError(f"duplicate package file path: {rel}")
        content = _package_file_text(item)
        total_size += len(content.encode("utf-8"))
        if total_size > _MAX_PACKAGE_TOTAL_BYTES:
            raise ValueError(f"package is too large; max {_MAX_PACKAGE_TOTAL_BYTES} bytes")
        normalized[rel] = content

    root = _package_root_for_skill(list(normalized))
    stripped: dict[str, str] = {}
    for rel, content in normalized.items():
        stripped[_strip_package_root(rel, root)] = content
    if "SKILL.md" not in stripped:
        raise ValueError("package must include SKILL.md at a single package root")

    parsed = parse_skill_markdown(stripped["SKILL.md"], source_name=f"{source_name}/SKILL.md")
    routing_cases: list[dict[str, Any]] = []
    if "routing_cases.json" in stripped:
        try:
            raw_cases = json.loads(stripped["routing_cases.json"])
            if isinstance(raw_cases, dict):
                raw_cases = raw_cases.get("cases", [])
            if isinstance(raw_cases, list):
                routing_cases = [item for item in raw_cases if isinstance(item, dict)]
        except json.JSONDecodeError:
            parsed["validation"]["errors"].append("routing_cases.json is not valid JSON")

    attachments = [
        {"path": rel, "content": content}
        for rel, content in sorted(stripped.items())
        if rel not in {"SKILL.md", "routing_cases.json"}
    ]
    warnings = list(parsed["validation"].get("warnings", []))
    warnings.extend(_attachment_warnings(attachments))
    parsed["validation"]["warnings"] = warnings
    parsed["mode"] = "package"
    parsed["package"] = {
        "root": root,
        "files": [
            {
                "path": item["path"],
                "size": len(item["content"].encode("utf-8")),
                "role": _package_file_role(item["path"]),
            }
            for item in attachments
        ],
        "attachments": attachments,
        "routing_cases": routing_cases,
    }
    return parsed


def parse_skill_package_zip(
    data_b64: str,
    *,
    source_name: str = "skill package zip",
) -> dict[str, Any]:
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception as exc:
        raise ValueError("zip package must be base64 encoded") from exc
    if len(raw) > _MAX_PACKAGE_TOTAL_BYTES:
        raise ValueError(f"zip package is too large; max {_MAX_PACKAGE_TOTAL_BYTES} bytes")
    files: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.file_size > _MAX_PACKAGE_FILE_BYTES:
                    raise ValueError(f"zip member is too large: {info.filename}")
                with archive.open(info) as handle:
                    content = handle.read().decode("utf-8")
                files.append({"path": info.filename, "content": content})
    except UnicodeDecodeError as exc:
        raise ValueError("zip package may contain only UTF-8 text files") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid zip package") from exc
    return parse_skill_package_files(files, source_name=source_name)


def _package_file_role(path: str) -> str:
    lowered = path.lower()
    if lowered.startswith("templates/"):
        return "template"
    if lowered.startswith("examples/"):
        return "example"
    if lowered.startswith("scripts/") or Path(lowered).suffix in _DANGEROUS_PACKAGE_SUFFIXES:
        return "script"
    if lowered.startswith("references/") or lowered.startswith("docs/"):
        return "reference"
    if lowered.endswith(".json"):
        return "data"
    return "reference"


def _validated_draft_attachments(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("draft attachments must be a list")
    if len(raw) > _MAX_PACKAGE_FILES:
        raise ValueError(f"draft has too many attachments; max {_MAX_PACKAGE_FILES}")
    total_size = 0
    attachments: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("draft attachment must be an object")
        rel = _normalize_package_relpath(str(item.get("path") or ""))
        if rel in {"SKILL.md", "routing_cases.json"}:
            raise ValueError(f"draft attachment cannot replace managed file: {rel}")
        if rel in seen:
            raise ValueError(f"duplicate draft attachment: {rel}")
        seen.add(rel)
        content = str(item.get("content") or "")
        size = len(content.encode("utf-8"))
        if size > _MAX_PACKAGE_FILE_BYTES:
            raise ValueError(f"draft attachment is too large: {rel}")
        total_size += size
        if total_size > _MAX_PACKAGE_TOTAL_BYTES:
            raise ValueError(f"draft attachments are too large; max {_MAX_PACKAGE_TOTAL_BYTES} bytes")
        attachments.append({"path": rel, "content": content})
    return attachments


def _draft_materials(
    *,
    name: str,
    description: str,
    trigger: str,
    method: str,
    category: str,
    risk_level: str,
    requires_exec: bool,
    required_tools: list[str] | None = None,
    install_sources: list[str] | None = None,
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
        required_tools=required_tools,
        install_sources=install_sources,
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
        "required_tools": required_tools or [],
        "install_sources": install_sources or [],
    }
    if content and content.attachments:
        input_json["attachments"] = [
            {"path": item["path"], "content": item["content"]}
            for item in content.attachments
            if item.get("path") and "content" in item
        ]
    return input_json, markdown, review, routing_cases


def _merge_duplicate_review(
    review: dict[str, Any],
    *,
    duplicate: dict[str, Any] | None,
) -> dict[str, Any]:
    if not duplicate:
        return review
    current = review.get("duplicate") if isinstance(review.get("duplicate"), dict) else {}
    current_score = current.get("score")
    incoming_score = duplicate.get("score")
    if isinstance(current_score, int | float) and isinstance(incoming_score, int | float):
        if float(current_score) >= float(incoming_score):
            return review
    merged = dict(review)
    merged["duplicate"] = duplicate
    if float(duplicate.get("score") or 0.0) >= 0.8:
        flags = [item for item in merged.get("red_flags", []) if isinstance(item, dict)]
        if not any(str(item.get("kind") or "").lower() == "duplicate" for item in flags):
            nearest = duplicate.get("nearest") if isinstance(duplicate.get("nearest"), dict) else {}
            flags.append(
                {
                    "kind": "duplicate",
                    "severity": "medium",
                    "message": (
                        "Proposed skill overlaps existing skill "
                        f"{nearest.get('name') or 'unknown'}; trigger differentiation is required."
                    ),
                }
            )
        merged["red_flags"] = flags
    return merged


def _status_for(source: str, frontmatter: dict[str, Any], meta: dict[str, Any]) -> str:
    if source == "workspace":
        # The registry is the single source of truth for lifecycle state. A
        # workspace SKILL.md cannot self-declare its status: runtime-written
        # files always enter as draft and only a human approval (CLI/WebUI)
        # moves them to candidate, so they stay out of skill_search until then.
        return DEFAULT_WORKSPACE_STATUS
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


_SECTION_RE = re.compile(r"^#{1,3}\s+(.+?)\s*$", re.MULTILINE)


def _markdown_sections(body: str) -> set[str]:
    return {match.group(1).strip().lower() for match in _SECTION_RE.finditer(body)}


def _markdown_section_body(body: str, *titles: str) -> str:
    """Return the content of the first heading whose title starts with one of ``titles``.

    Title matching is case-insensitive and tolerates suffixes such as
    "When to use (trigger phrases)". The section ends at the next heading.
    """
    matches = list(_SECTION_RE.finditer(body))
    wanted = tuple(title.lower() for title in titles)
    for index, match in enumerate(matches):
        heading = match.group(1).strip().lower()
        if not heading.startswith(wanted):
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        return body[start:end].strip()
    return ""


def _validate_external_tool_shape(
    *,
    name: str,
    frontmatter: dict[str, Any],
    meta: dict[str, Any],
    body: str,
    category: str,
    risk_level: str,
    requires_exec: bool,
    install_sources: list[str],
) -> None:
    normalized_name = name.lower()
    sections = _markdown_sections(body)
    always = bool(frontmatter.get("always") or meta.get("always"))
    external_tool = bool(meta.get("external_tool") or str(category).startswith("external."))

    if external_tool and normalized_name.endswith("-setup"):
        if always:
            raise ValueError(f"{name}: setup skills cannot be always-on or Hot Path")
        if risk_level != "high":
            raise ValueError(f"{name}: setup skills must declare risk_level=high")
        if not requires_exec:
            raise ValueError(f"{name}: setup skills must declare requires_exec=true")
        if not install_sources:
            raise ValueError(f"{name}: setup skills must declare metadata.nanobot.install_sources")
        missing = [section for section in ("install", "verify", "uninstall") if section not in sections]
        if missing:
            raise ValueError(f"{name}: setup skills must include sections: {', '.join(missing)}")
        lowered = body.lower()
        if "curl" in lowered and "| bash" in lowered:
            raise ValueError(f"{name}: setup skills cannot use curl | bash install patterns")
        if re.search(r"\bsudo\b|npm\s+install\s+-g|pip\s+install\s+--user|/usr/local|/opt/homebrew", lowered):
            raise ValueError(f"{name}: setup skills cannot require global or sudo installs")

    if external_tool and normalized_name.endswith("-usage"):
        method_match = re.search(r"^#{1,3}\s+method\s*$", body, flags=re.IGNORECASE | re.MULTILINE)
        method_tail = body[method_match.end() :] if method_match else body
        first_lines = "\n".join(line.strip().lower() for line in method_tail.splitlines()[:8])
        if not re.search(r"\b(which|healthcheck|health check|--version|installed|existence|check.*install)", first_lines):
            raise ValueError(f"{name}: usage skills must start Method with an installation/existence check")


def _skill_from_file(path: Path, *, source: str, default_status: str | None = None) -> tuple[SkillRecord, list[SkillRelation]]:
    content = path.read_text(encoding="utf-8")
    frontmatter = _parse_frontmatter_strict(content, path)
    meta = _nanobot_meta(frontmatter)
    body = _body_without_frontmatter(content)
    name = str(frontmatter.get("name") or path.parent.name)
    description = str(frontmatter.get("description") or name)
    version = str(meta.get("version") or frontmatter.get("version") or "0.1.0")
    status = default_status or _status_for(source, frontmatter, meta)
    risk_level = str(meta.get("risk_level") or frontmatter.get("risk_level") or "low")
    category = str(meta.get("category") or frontmatter.get("category") or "general")
    requires_exec = bool(meta.get("requires_exec") or frontmatter.get("requires_exec") or False)
    when_to_use = _json_text(frontmatter.get("when_to_use") or meta.get("when_to_use")) or (
        _markdown_section_body(body, "when to use")
    )
    when_not_to_use = _json_text(frontmatter.get("when_not_to_use") or meta.get("when_not_to_use")) or (
        _markdown_section_body(body, "when not to use")
    )
    required_tools = _json_list(meta.get("required_tools") or frontmatter.get("required_tools"))
    install_sources = _json_list(meta.get("install_sources") or frontmatter.get("install_sources"))
    _validate_external_tool_shape(
        name=name,
        frontmatter=frontmatter,
        meta=meta,
        body=body,
        category=category,
        risk_level=risk_level,
        requires_exec=requires_exec,
        install_sources=install_sources,
    )
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    # Positive trigger surface only. when_to_use/when_not_to_use are scored
    # from their own columns with matching polarity; mixing when_not_to_use
    # into the shared index would reward negative evidence.
    search_text = "\n".join(
        part
        for part in (
            name,
            description,
            "\n".join(_json_list(meta.get("triggers") or frontmatter.get("triggers"))),
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
        install_sources=install_sources,
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

    def _skill_files(base: Path) -> list[Path]:
        direct: list[Path] = []
        scoped: list[Path] = []
        for skill_dir in sorted(base.iterdir(), key=lambda item: item.name):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if skill_file.exists():
                direct.append(skill_file)
                continue
            if skill_dir.name.startswith("@"):
                for scoped_dir in sorted(skill_dir.iterdir(), key=lambda item: item.name):
                    if scoped_dir.is_dir() and (scoped_dir / "SKILL.md").exists():
                        scoped.append(scoped_dir / "SKILL.md")

        files: list[Path] = []
        local_seen: set[str] = set()
        for skill_file in [*direct, *scoped]:
            name = skill_file.parent.name
            if name in local_seen:
                continue
            local_seen.add(name)
            files.append(skill_file)
        return files

    def _add(base: Path, source: str, default_status: str | None = None, *, shadow: bool = True) -> None:
        if not base.exists():
            return
        for path in _skill_files(base):
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
                    install_sources_json TEXT NOT NULL DEFAULT '[]',
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
                    intent_summary TEXT,
                    candidates_json TEXT NOT NULL DEFAULT '[]',
                    selected_skill TEXT,
                    selection_reason TEXT NOT NULL,
                    executed_by TEXT,
                    wave_no INTEGER,
                    duration_ms INTEGER,
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
                CREATE TABLE IF NOT EXISTS skill_vectors (
                    skill_id TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS skill_query_vectors (
                    query_digest TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    vector_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (query_digest, model)
                );
                CREATE TABLE IF NOT EXISTS relay_clients (
                    client_id TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    key_id TEXT NOT NULL UNIQUE,
                    secret_hash TEXT NOT NULL,
                    model_preset TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    last_used_at TEXT,
                    last_used_ip TEXT
                );
                """
            )
            try:
                conn.execute("ALTER TABLE skills ADD COLUMN install_sources_json TEXT NOT NULL DEFAULT '[]'")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE traces ADD COLUMN duration_ms INTEGER")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE traces ADD COLUMN intent_summary TEXT")
            except sqlite3.OperationalError:
                pass
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
        embedding_fn: Callable[[list[str]], list[list[float]]] | None = None,
        embedding_model: str | None = None,
        embedding_dimensions: int | None = None,
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
        if embedding_fn and embedding_model:
            self.replace_vectors(
                records,
                embedding_fn=embedding_fn,
                embedding_model=embedding_model,
                embedding_dimensions=embedding_dimensions,
            )
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
            conn.execute("DELETE FROM skill_vectors")
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
                        required_tools_json, install_sources_json, usage_count, success_count, failure_count,
                        routing_failure_count, content_hash, search_text, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        json.dumps(record.install_sources),
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

    def replace_vectors(
        self,
        records: list[SkillRecord],
        *,
        embedding_fn: Callable[[list[str]], list[list[float]]],
        embedding_model: str,
        embedding_dimensions: int | None = None,
    ) -> None:
        texts = [record.search_text or record.description or record.name for record in records]
        vectors = embedding_fn(texts)
        if len(vectors) != len(records):
            raise ValueError("embedding_fn returned a different number of vectors than records")
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("DELETE FROM skill_vectors")
            for record, vector in zip(records, vectors, strict=True):
                clean = [float(value) for value in vector]
                dimensions = int(embedding_dimensions or len(clean))
                if dimensions != len(clean):
                    raise ValueError(
                        f"embedding dimension mismatch for {record.name}: "
                        f"expected {dimensions}, got {len(clean)}"
                    )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO skill_vectors (
                        skill_id, model, dimensions, vector_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        embedding_model,
                        dimensions,
                        json.dumps(clean),
                        now,
                    ),
                )

    def get_cached_query_vector(self, query_digest: str, *, embedding_model: str) -> list[float] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT vector_json FROM skill_query_vectors
                WHERE query_digest = ? AND model = ?
                """,
                (query_digest, embedding_model),
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["vector_json"] or "[]")
        except json.JSONDecodeError:
            return None
        if not isinstance(data, list):
            return None
        return [float(value) for value in data]

    def set_cached_query_vector(
        self,
        query_digest: str,
        *,
        embedding_model: str,
        vector: list[float],
        embedding_dimensions: int | None = None,
    ) -> None:
        clean = [float(value) for value in vector]
        dimensions = int(embedding_dimensions or len(clean))
        if dimensions != len(clean):
            raise ValueError(
                f"query embedding dimension mismatch: expected {dimensions}, got {len(clean)}"
            )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO skill_query_vectors (
                    query_digest, model, dimensions, vector_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    query_digest,
                    embedding_model,
                    dimensions,
                    json.dumps(clean),
                    _utc_now(),
                ),
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

    def audit_catalog(self, *, write_report: bool = True) -> SkillAuditReport:
        """Run an advisory, deterministic skill catalog conformance audit."""
        rows = self.list_skills(include_deprecated=True)
        relation_pairs = self._relation_pairs()
        attention: list[dict[str, Any]] = []
        reference: list[dict[str, Any]] = []
        category_counts: dict[str, int] = {}

        for row in rows:
            category = str(row["category"] or "")
            category_counts[category] = category_counts.get(category, 0) + 1
            frontmatter = self._frontmatter_for_row(row)
            meta = _nanobot_meta(frontmatter)
            path = str(row["path"] or "")
            name = str(row["name"])
            missing = self._missing_required_frontmatter(frontmatter, meta)
            if missing:
                attention.append(
                    {
                        "code": "missing_frontmatter_fields",
                        "severity": "attention",
                        "skill_names": [name],
                        "message": f"{name} is missing required frontmatter fields: {', '.join(missing)}",
                        "fields": missing,
                        "path": path,
                    }
                )

            if self._category_format_violation(category):
                reference.append(
                    {
                        "code": "category_format",
                        "severity": "reference",
                        "skill_names": [name],
                        "message": f"{name} uses nonconforming category '{category or '(empty)'}'.",
                        "category": category,
                        "path": path,
                    }
                )

            residue_reasons = self._residue_reasons(row, frontmatter, meta)
            if residue_reasons:
                reference.append(
                    {
                        "code": "residue_suspect",
                        "severity": "reference",
                        "skill_names": [name],
                        "message": f"{name} should be reviewed for catalog residue.",
                        "reasons": residue_reasons,
                        "path": path,
                    }
                )

            if self._missing_routing_cases(row):
                reference.append(
                    {
                        "code": "missing_routing_cases",
                        "severity": "reference",
                        "skill_names": [name],
                        "message": f"{name} has no routing_cases.json next to SKILL.md.",
                        "path": path,
                    }
                )

        general_count = category_counts.get("general", 0)
        if general_count:
            reference.append(
                {
                    "code": "general_category_count",
                    "severity": "reference",
                    "skill_names": [
                        str(row["name"])
                        for row in rows
                        if str(row["category"] or "") == "general"
                    ],
                    "message": f"{general_count} skill(s) use the general category.",
                    "category": "general",
                    "count": general_count,
                }
            )

        attention.extend(self._unwired_similarity_clusters(rows, relation_pairs))
        generated_at = _utc_now()
        report_path = str(self.db_path.parent / "audit-report.json")
        summary = {
            "skills": len(rows),
            "attention": len(attention),
            "reference": len(reference),
            "general_category": general_count,
            "missing_routing_cases": sum(1 for item in reference if item["code"] == "missing_routing_cases"),
            "missing_frontmatter_fields": sum(1 for item in attention if item["code"] == "missing_frontmatter_fields"),
            "unwired_similarity_clusters": sum(1 for item in attention if item["code"] == "unwired_similarity_cluster"),
        }
        report = SkillAuditReport(
            generated_at=generated_at,
            report_path=report_path,
            summary=summary,
            attention=attention,
            reference=reference,
        )
        if write_report:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            Path(report_path).write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return report

    def _frontmatter_for_row(self, row: sqlite3.Row) -> dict[str, Any]:
        path = Path(str(row["path"] or ""))
        try:
            return _parse_frontmatter(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except OSError:
            return {}

    def _missing_required_frontmatter(self, frontmatter: dict[str, Any], meta: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        if not (meta.get("id") or frontmatter.get("id")):
            missing.append("metadata.nanobot.id")
        if not (meta.get("version") or frontmatter.get("version")):
            missing.append("metadata.nanobot.version")
        if not (meta.get("category") or frontmatter.get("category")):
            missing.append("metadata.nanobot.category")
        if not (meta.get("risk_level") or frontmatter.get("risk_level")):
            missing.append("metadata.nanobot.risk_level")
        if "requires_exec" not in meta and "requires_exec" not in frontmatter:
            missing.append("metadata.nanobot.requires_exec")
        return missing

    def _category_format_violation(self, category: str) -> bool:
        if not category:
            return True
        raw_parts = category.split(".")
        parts = [part for part in raw_parts if part]
        return len(parts) != len(raw_parts) or len(parts) > 2

    def _residue_reasons(self, row: sqlite3.Row, frontmatter: dict[str, Any], meta: dict[str, Any]) -> list[str]:
        reasons: list[str] = []
        name = str(row["name"] or "")
        description = str(frontmatter.get("description") or row["description"] or "").strip()
        if not description or description == name:
            reasons.append("description_missing_or_name_only")
        triggers = _json_list(meta.get("triggers") or frontmatter.get("triggers"))
        when_to_use = str(frontmatter.get("when_to_use") or meta.get("when_to_use") or row["when_to_use"] or "").strip()
        if not triggers and not when_to_use:
            reasons.append("trigger_guidance_missing")
        if name.lower() in {"my", "test", "sample", "tmp", "temp"}:
            reasons.append("suspicious_name")
        if row["status"] == "verified" and int(row["usage_count"] or 0) == 0 and row["source"] == "workspace":
            reasons.append("verified_without_usage")
        return reasons

    def _missing_routing_cases(self, row: sqlite3.Row) -> bool:
        if row["status"] in {"system", "draft", "deprecated", "rejected"}:
            return False
        path = Path(str(row["path"] or ""))
        if not path.name:
            return False
        return not (path.parent / "routing_cases.json").is_file()

    def _relation_pairs(self) -> set[tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT src.name AS src_name, dst.name AS dst_name
                FROM skill_relations r
                JOIN skills src ON src.id = r.src_id
                JOIN skills dst ON dst.id = r.dst_id
                """
            ).fetchall()
        pairs: set[tuple[str, str]] = set()
        for row in rows:
            src = str(row["src_name"])
            dst = str(row["dst_name"])
            pairs.add((src, dst))
            pairs.add((dst, src))
        return pairs

    def _unwired_similarity_clusters(
        self,
        rows: list[sqlite3.Row],
        relation_pairs: set[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        candidates = [
            row for row in rows
            if row["status"] in {"candidate", "verified"} and row["source"] != "system"
        ]
        edges: dict[str, set[str]] = {}
        for index, left in enumerate(candidates):
            for right in candidates[index + 1:]:
                if (left["name"], right["name"]) in relation_pairs:
                    continue
                common = self._similarity_keys(left) & self._similarity_keys(right)
                if not common:
                    continue
                if self._has_mutual_boundary_note(left, right):
                    continue
                left_name = str(left["name"])
                right_name = str(right["name"])
                edges.setdefault(left_name, set()).add(right_name)
                edges.setdefault(right_name, set()).add(left_name)

        clusters: list[dict[str, Any]] = []
        visited: set[str] = set()
        by_name = {str(row["name"]): row for row in candidates}
        for name in sorted(edges):
            if name in visited:
                continue
            stack = [name]
            names: set[str] = set()
            keys: set[str] = set()
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                names.add(current)
                row = by_name.get(current)
                if row is not None:
                    keys.update(self._similarity_keys(row))
                stack.extend(sorted(edges.get(current, set()) - visited))
            if len(names) < 2:
                continue
            clusters.append(
                {
                    "code": "unwired_similarity_cluster",
                    "severity": "attention",
                    "skill_names": sorted(names),
                    "message": "Similar catalog cluster has no explicit relation wiring for at least one pair.",
                    "cluster_keys": sorted(keys),
                }
            )
        return clusters

    def _similarity_keys(self, row: sqlite3.Row) -> set[str]:
        category = str(row["category"] or "").lower()
        name = str(row["name"] or "").lower()
        keys: set[str] = set()
        parts = [part for part in category.split(".") if part and part != "general"]
        if len(parts) >= 2 and parts[-1] not in {"tool", "web"}:
            keys.add(parts[-1].replace("_", "-"))
        if len(parts) == 2 and parts[0] in {"decision"}:
            keys.add(f"root:{parts[0]}")
        if name.endswith("-setup"):
            keys.add(f"setup:{name.removesuffix('-setup')}")
        if name.endswith("-usage"):
            keys.add(f"setup:{name.removesuffix('-usage')}")
        tokens = {
            token
            for token in re.split(r"[^a-z0-9]+", f"{name} {category}")
            if token
        }
        aliases = {
            "comparison": "compare",
            "comparing": "compare",
            "options": "compare",
            "pros": "decision",
            "cons": "decision",
        }
        signal_tokens = {
            "compare",
            "decision",
            "review",
            "summary",
            "summarize",
            "diagnosis",
            "howto",
        }
        for token in tokens:
            normalized = aliases.get(token, token)
            if normalized in signal_tokens:
                keys.add(normalized)
        return keys

    def _has_mutual_boundary_note(self, left: sqlite3.Row, right: sqlite3.Row) -> bool:
        left_name = str(left["name"])
        right_name = str(right["name"])
        try:
            left_text = Path(str(left["path"])).read_text(encoding="utf-8").lower()
            right_text = Path(str(right["path"])).read_text(encoding="utf-8").lower()
        except OSError:
            return False
        return right_name.lower() in left_text and left_name.lower() in right_text

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

    def _duplicate_review_for_draft(
        self,
        *,
        name: str,
        description: str,
        trigger: str,
        category: str,
        requires_exec: bool,
    ) -> dict[str, Any] | None:
        query = "\n".join(part for part in [description, trigger, category] if part.strip())
        if not query.strip():
            return None
        candidates = self.search(query, top_k=8, min_status=("candidate", "verified"))
        checked: list[dict[str, Any]] = []
        best: tuple[float, SkillSearchMatch] | None = None
        normalized_category = category.strip().lower()
        for match in candidates:
            if match.name == name:
                continue
            same_category = bool(normalized_category and match.category.lower() == normalized_category)
            same_root = bool(
                normalized_category
                and match.category.lower().split(".", 1)[0] == normalized_category.split(".", 1)[0]
            )
            if same_category:
                duplicate_score = min(1.0, max(0.0, match.score / 65.0))
            elif same_root:
                duplicate_score = min(0.79, max(0.0, match.score / 120.0))
            else:
                duplicate_score = min(0.69, max(0.0, match.score / 160.0))
            if bool(match.requires_exec) != bool(requires_exec):
                duplicate_score *= 0.6
            checked.append(
                {
                    "name": match.name,
                    "category": match.category,
                    "score": round(duplicate_score, 4),
                    "retrieval_score": round(match.score, 4),
                    "description": match.description,
                }
            )
            if best is None or duplicate_score > best[0]:
                best = (duplicate_score, match)
        if best is None:
            return None
        score, match = best
        classification = "duplicate" if score >= 0.8 else "new"
        return {
            "score": round(score, 4),
            "classification": classification,
            "differentiation_required": score >= 0.8,
            "nearest": {
                "name": match.name,
                "category": match.category,
                "description": match.description,
                "reason": (
                    "same category and high retrieval similarity"
                    if score >= 0.8
                    else "nearest neighbor below duplicate threshold"
                ),
            },
            "checked": checked[:5],
        }

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
                    intent_summary=row["intent_summary"] if "intent_summary" in row.keys() else None,
                    candidates=[item for item in candidates if isinstance(item, dict)],
                    selected_skill=row["selected_skill"],
                    selection_reason=row["selection_reason"],
                    executed_by=row["executed_by"],
                    wave_no=row["wave_no"],
                    duration_ms=row["duration_ms"] if "duration_ms" in row.keys() else None,
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
        required_tools: list[str] | None = None,
        install_sources: list[str] | None = None,
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
            required_tools=required_tools,
            install_sources=install_sources,
            content=content,
        )
        review = _merge_duplicate_review(
            review,
            duplicate=self._duplicate_review_for_draft(
                name=name,
                description=description,
                trigger=trigger,
                category=category,
                requires_exec=requires_exec,
            ),
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
        review = _merge_duplicate_review(
            review,
            duplicate=self._duplicate_review_for_draft(
                name=str(values.get("name") or row["name"]),
                description=str(values.get("description") or ""),
                trigger=str(values.get("trigger") or ""),
                category=str(values.get("category") or "general"),
                requires_exec=bool(values.get("requires_exec") or False),
            ),
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

    def delete_skill_draft(self, draft_id: str) -> bool:
        """Discard a composer draft that was never materialized as a file.

        This is a plain row delete, not a status transition: an unapproved
        draft has no file and no registry row, so there is nothing to
        "reject" in the governance sense (contrast with :meth:`reject_skill`,
        which marks an existing skill's registry row rejected). An approved
        draft is the provenance record for a real, registered skill, so it
        cannot be discarded through this path.

        Returns ``False`` if the draft does not exist (idempotent).
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM skill_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "approved":
                raise ValueError(f"cannot discard an approved draft: {draft_id}")
            conn.execute("DELETE FROM skill_drafts WHERE draft_id = ?", (draft_id,))
            return True

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
        try:
            input_json = json.loads(row["input_json"] or "{}")
        except json.JSONDecodeError:
            input_json = {}
        attachments = _validated_draft_attachments(input_json.get("attachments"))

        skill_dir.mkdir(parents=True, exist_ok=False)
        wrote_skill = False
        written_attachments: list[Path] = []
        try:
            skill_file.write_text(row["markdown"], encoding="utf-8")
            wrote_skill = True
            routing_cases = json.loads(row["routing_cases_json"] or "[]")
            if isinstance(routing_cases, list) and routing_cases:
                routing_file.write_text(
                    json.dumps({"cases": routing_cases}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            for attachment in attachments:
                rel = attachment["path"]
                target = skill_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(attachment["content"], encoding="utf-8")
                written_attachments.append(target)
            self.reindex(system_dir=system_dir)
            # Indexing always lands workspace files as draft; this call is the
            # human approval, so promote the registry row explicitly.
            self._transition_status(
                name,
                "candidate",
                allowed_from={"draft"},
                idempotent_from={"candidate"},
            )
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
                    for target in reversed(written_attachments):
                        if target.exists():
                            target.unlink()
                    for parent in sorted(
                        {target.parent for target in written_attachments},
                        key=lambda item: len(item.parts),
                        reverse=True,
                    ):
                        with contextlib.suppress(OSError):
                            parent.rmdir()
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

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_status: Iterable[str] | None = None,
        query_vector: list[float] | None = None,
    ) -> list[SkillSearchMatch]:
        statuses = tuple(min_status or ("candidate", "verified"))
        if not statuses:
            return []
        with self._connect() as conn:
            fts_scores: dict[str, float] = {}
            if self._has_fts(conn):
                try:
                    rows = conn.execute(
                        f"""
                        SELECT s.id, bm25(skill_search_fts) AS score
                        FROM skill_search_fts
                        JOIN skills s ON s.id = skill_search_fts.skill_id
                        WHERE skill_search_fts MATCH ?
                          AND s.status IN ({",".join("?" for _ in statuses)})
                        ORDER BY score
                        """,
                        (query, *statuses),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                else:
                    fts_scores = {str(row["id"]): -float(row["score"]) for row in rows}
            semantic_scores = self._semantic_scores(conn, query_vector, statuses)
            rows = conn.execute(
                f"""
                SELECT * FROM skills
                WHERE status IN ({",".join("?" for _ in statuses)})
                """,
                statuses,
            ).fetchall()
            ranked = [
                self._match_from_row(
                    row,
                    score=self._hybrid_score(
                        self._routing_score(
                            row,
                            query,
                            fts_score=fts_scores.get(str(row["id"]), 0.0),
                        ),
                        semantic_scores.get(str(row["id"])),
                    ),
                )
                for row in rows
            ]
            ranked = [match for match in ranked if match.score > 0]
            ranked.sort(key=lambda item: (item.score + item.stats_weight, item.name), reverse=True)
            return ranked[:top_k]

    @staticmethod
    def _hybrid_score(lexical_score: float, semantic_score: float | None) -> float:
        if semantic_score is None:
            return lexical_score
        return max(lexical_score, semantic_score * 0.9)

    def _semantic_scores(
        self,
        conn: sqlite3.Connection,
        query_vector: list[float] | None,
        statuses: tuple[str, ...],
    ) -> dict[str, float]:
        if not query_vector:
            return {}
        placeholders = ",".join("?" for _ in statuses)
        try:
            rows = conn.execute(
                f"""
                SELECT v.skill_id, v.vector_json
                FROM skill_vectors v
                JOIN skills s ON s.id = v.skill_id
                WHERE s.status IN ({placeholders})
                """,
                statuses,
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        scores: dict[str, float] = {}
        for row in rows:
            try:
                vector = json.loads(row["vector_json"] or "[]")
            except json.JSONDecodeError:
                continue
            if not isinstance(vector, list):
                continue
            similarity = _cosine_similarity(query_vector, [float(value) for value in vector])
            if similarity is None:
                continue
            scores[str(row["skill_id"])] = max(0.0, min(100.0, ((similarity + 1.0) / 2.0) * 100.0))
        return scores

    def category_matches(
        self,
        category: str,
        *,
        min_status: Iterable[str] | None = None,
    ) -> list[SkillSearchMatch]:
        """Return candidate skills whose category matches the LLM-provided hint."""
        normalized = (category or "").strip().lower()
        if not normalized:
            return []
        statuses = tuple(min_status or ("candidate", "verified"))
        if not statuses:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM skills
                WHERE status IN ({",".join("?" for _ in statuses)})
                  AND (lower(category) = ? OR lower(category) LIKE ?)
                """,
                (*statuses, normalized, f"{normalized}.%"),
            ).fetchall()
        return [self._match_from_row(row, score=60.0) for row in rows]

    @staticmethod
    def _routing_score(row: sqlite3.Row, query: str, *, fts_score: float = 0.0) -> float:
        query_lower = " ".join(re.findall(r"[a-z0-9][a-z0-9_-]*|[가-힣]{2,}", query.lower()))
        query_terms = _routing_terms(query)
        if not query_terms:
            return fts_score

        name = str(row["name"] or "")
        description = str(row["description"] or "")
        when_to_use = str(row["when_to_use"] or "")
        when_not_to_use = str(row["when_not_to_use"] or "")
        search_text = str(row["search_text"] or "")

        # Full-text rank is a retrieval hint; cap it so raw bm25 magnitudes
        # cannot outvote the card-evidence weights below.
        score = min(fts_score, 20.0)
        name_terms = _routing_terms(name.replace("-", " "))
        description_terms = _routing_terms(description)
        when_terms = _routing_terms(when_to_use)
        search_terms = _routing_terms(search_text)
        when_not_terms = _routing_terms(when_not_to_use)

        score += len(query_terms & name_terms) * 16.0
        score += len(query_terms & description_terms) * 5.0
        score += len(query_terms & when_terms) * 5.0
        score += len(query_terms & search_terms) * 3.0
        score -= len(query_terms & when_not_terms) * 8.0

        compact_name = name.lower().replace("-", " ")
        if compact_name and compact_name in query_lower:
            # Specificity bonus: matching a multi-word name verbatim is far
            # stronger evidence than a single word that doubles as a common verb.
            score += 40.0 if len(name_terms) >= 2 else 10.0
        # Phrase evidence must respect polarity: when_not_to_use lines are
        # negative trigger surface, so they may never add to the score.
        for phrase in _phrase_lines(search_text):
            if phrase in query_lower:
                score += 80.0
            elif query_lower in phrase and len(query_lower) >= 12:
                score += 40.0
            else:
                phrase_terms = _routing_terms(phrase)
                if phrase_terms and phrase_terms <= query_terms:
                    score += 30.0
        for phrase in _phrase_lines(when_not_to_use):
            if phrase in query_lower:
                score -= 60.0
            else:
                phrase_terms = _routing_terms(phrase)
                if phrase_terms and phrase_terms <= query_terms:
                    score -= 25.0

        if bool(row["requires_exec"]):
            exec_cues = {"fix", "run", "execute", "inspect", "patch", "calculate", "analyze", "load", "compute"}
            if query_terms & exec_cues:
                score += 12.0
            else:
                score -= 4.0
        return score

    @staticmethod
    def _match_from_row(row: sqlite3.Row, *, score: float) -> SkillSearchMatch:
        return SkillSearchMatch(
            name=row["name"],
            description=row["description"],
            when_to_use=row["when_to_use"],
            when_not_to_use=row["when_not_to_use"],
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

    @staticmethod
    def _relay_client_from_row(row: sqlite3.Row) -> RelayClientRecord:
        return RelayClientRecord(
            client_id=row["client_id"],
            tool_name=row["tool_name"],
            key_id=row["key_id"],
            model_preset=row["model_preset"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            last_used_at=row["last_used_at"],
            last_used_ip=row["last_used_ip"],
        )

    def issue_relay_client(
        self,
        *,
        client_id: str,
        tool_name: str | None = None,
        model_preset: str = "default",
        expires_at: str | None = None,
        replace: bool = False,
    ) -> IssuedRelayClient:
        """Create a relay PSK for one external tool client.

        The returned token is the only copy of the secret. The database stores
        a PBKDF2 verifier plus a key id so relay auth can verify without
        retaining raw provider or relay credentials.
        """
        client = _safe_relay_client_id(client_id)
        tool = (tool_name or client).strip() or client
        preset = (model_preset or "default").strip() or "default"
        key_id = "rly" + secrets.token_hex(8)
        secret = secrets.token_urlsafe(32)
        token = f"{_RELAY_TOKEN_PREFIX}_{key_id}_{secret}"
        now = _utc_now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT status FROM relay_clients WHERE client_id = ?",
                (client,),
            ).fetchone()
            if existing and not replace:
                raise ValueError(
                    f"relay client {client!r} already exists; use rotate or revoke first"
                )
            if existing and replace:
                conn.execute(
                    """
                    UPDATE relay_clients
                    SET tool_name = ?, key_id = ?, secret_hash = ?, model_preset = ?,
                        status = 'active', updated_at = ?, expires_at = ?,
                        last_used_at = NULL, last_used_ip = NULL
                    WHERE client_id = ?
                    """,
                    (
                        tool,
                        key_id,
                        _relay_secret_hash(secret),
                        preset,
                        now,
                        expires_at,
                        client,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO relay_clients (
                        client_id, tool_name, key_id, secret_hash, model_preset,
                        status, created_at, updated_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        client,
                        tool,
                        key_id,
                        _relay_secret_hash(secret),
                        preset,
                        now,
                        now,
                        expires_at,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM relay_clients WHERE client_id = ?",
                (client,),
            ).fetchone()
        return IssuedRelayClient(self._relay_client_from_row(row), token)

    def list_relay_clients(self) -> list[RelayClientRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM relay_clients
                ORDER BY status = 'active' DESC, client_id
                """
            ).fetchall()
        return [self._relay_client_from_row(row) for row in rows]

    def revoke_relay_client(self, client_id: str) -> RelayClientRecord:
        client = _safe_relay_client_id(client_id)
        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM relay_clients WHERE client_id = ?",
                (client,),
            ).fetchone()
            if row is None:
                raise KeyError(f"relay client {client!r} not found")
            conn.execute(
                """
                UPDATE relay_clients
                SET status = 'revoked', updated_at = ?
                WHERE client_id = ?
                """,
                (now, client),
            )
            row = conn.execute(
                "SELECT * FROM relay_clients WHERE client_id = ?",
                (client,),
            ).fetchone()
        return self._relay_client_from_row(row)

    def verify_relay_token(
        self,
        token: str,
        *,
        remote_ip: str | None = None,
    ) -> RelayClientRecord | None:
        parsed = parse_relay_token(token)
        if parsed is None:
            return None
        key_id, secret = parsed
        now = _utc_now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM relay_clients WHERE key_id = ?",
                (key_id,),
            ).fetchone()
            if row is None or row["status"] != "active":
                return None
            expires_at = row["expires_at"]
            if expires_at and expires_at <= now:
                return None
            if not _verify_relay_secret(secret, row["secret_hash"]):
                return None
            conn.execute(
                """
                UPDATE relay_clients
                SET last_used_at = ?, last_used_ip = ?, updated_at = ?
                WHERE key_id = ?
                """,
                (now, remote_ip, now, key_id),
            )
            row = conn.execute(
                "SELECT * FROM relay_clients WHERE key_id = ?",
                (key_id,),
            ).fetchone()
        return self._relay_client_from_row(row)

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
        intent_summary: str | None = None,
        candidates: list[dict[str, Any]] | None = None,
        selected_skill: str | None = None,
        selection_reason: str = "none",
        executed_by: str | None = None,
        wave_no: int | None = None,
        duration_ms: int | None = None,
        gate_result: str | None = None,
        user_feedback: str | None = None,
        notes: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO traces (
                    trace_id, ts, session_key, query_digest, intent_summary, candidates_json,
                    selected_skill, selection_reason, executed_by, wave_no,
                    duration_ms, gate_result, user_feedback, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    _utc_now(),
                    session_key,
                    query_digest,
                    _short_text(intent_summary, 300) if intent_summary else None,
                    json.dumps(candidates or [], ensure_ascii=False),
                    selected_skill,
                    selection_reason,
                    executed_by,
                    wave_no,
                    duration_ms,
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
