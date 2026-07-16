"""Registry-backed skill management payloads for the WebUI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.skills import SYSTEM_SKILLS_DIR
from nanobot.skill_store import (
    SkillDraftContent,
    SkillDraftResult,
    SkillStore,
    SkillUpdateAssessment,
    parse_skill_markdown,
    parse_skill_package_files,
    parse_skill_package_zip,
    row_to_skill_payload,
)

_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def _normalize_header(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _status_value(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"running", "active", "up", "ok", "실행중"}:
        return "running"
    if normalized in {"stopped", "inactive", "down", "중지"}:
        return "stopped"
    return normalized or "unknown"


def _parse_markdown_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if not cells:
        return None
    if all(set(cell) <= {"-", ":", " "} for cell in cells):
        return None
    return cells


def installed_tools_payload(workspace_path: Path) -> list[dict[str, Any]]:
    """Parse the read-only external tool ledger.

    This deliberately does not perform health checks. The ledger may include
    status/last-checked fields written by usage or setup skills, and the WebUI
    only displays those last recorded values.
    """

    path = workspace_path / "tools" / "installed.md"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    header: list[str] | None = None
    rows: list[dict[str, Any]] = []
    fallback_keys = ["name", "version", "path", "installed_at", "source"]
    for line in lines:
        cells = _parse_markdown_table_row(line)
        if cells is None:
            continue
        normalized = [_normalize_header(cell) for cell in cells]
        if header is None:
            if any(key in normalized for key in ("name", "tool", "version", "installed_at", "installed")):
                header = normalized
                continue
            header = fallback_keys[: len(cells)]
        values = dict(zip(header, cells, strict=False))
        name = values.get("name") or values.get("tool") or values.get("명칭") or cells[0]
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "description": values.get("description") or values.get("desc") or values.get("summary") or "",
                "installed_at": values.get("installed_at") or values.get("installed") or values.get("date") or "",
                "version": values.get("version") or "",
                "status": _status_value(values.get("status") or values.get("state") or ""),
                "last_checked_at": values.get("last_checked_at") or values.get("last_checked") or None,
                "path": values.get("path") or values.get("location") or "",
                "source": values.get("source") or values.get("url") or "",
            }
        )
    return rows


def _risk_at_least(value: str, threshold: str) -> bool:
    return _RISK_ORDER.get(str(value).lower(), 0) >= _RISK_ORDER.get(str(threshold).lower(), 0)


def _assessment_payload(assessment: SkillUpdateAssessment) -> dict[str, Any]:
    return {
        "kind": assessment.kind,
        "reasons": assessment.reasons,
        "changed_fields": assessment.changed_fields,
        "current_status": assessment.current_status,
        "next_status": assessment.next_status,
        "requires_revalidation": assessment.requires_revalidation,
    }


def _policy_payload(policy: Any | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    return {
        "min_routing_passes": int(getattr(policy, "min_routing_passes", 7)),
        "security_risk_at_least": str(getattr(policy, "security_risk_at_least", "medium")),
        "security_block_at_least": str(getattr(policy, "security_block_at_least", "high")),
        "duplicate_score_at_least": float(getattr(policy, "duplicate_score_at_least", 0.8)),
    }


def _draft_governance(draft: SkillDraftResult, policy: Any | None) -> dict[str, Any]:
    resolved = _policy_payload(policy) or {
        "min_routing_passes": 7,
        "security_risk_at_least": "medium",
        "security_block_at_least": "high",
        "duplicate_score_at_least": 0.8,
    }
    review = draft.review_json
    red_flags = [item for item in review.get("red_flags", []) if isinstance(item, dict)]
    blocking: list[dict[str, Any]] = []
    requires_confirmation: list[dict[str, Any]] = []

    security_level = str(review.get("security_risk_level") or "").lower()
    for flag in red_flags:
        if str(flag.get("kind") or "").lower() == "security":
            security_level = str(flag.get("severity") or flag.get("level") or security_level).lower()
            break
    if security_level:
        flag = {
            "kind": "security",
            "severity": security_level,
            "message": "Security review requires attention.",
        }
        if _risk_at_least(security_level, str(resolved["security_block_at_least"])):
            blocking.append(flag)
        elif _risk_at_least(security_level, str(resolved["security_risk_at_least"])):
            requires_confirmation.append(flag)

    routing = review.get("routing_test") if isinstance(review.get("routing_test"), dict) else {}
    passed = routing.get("passed")
    total = routing.get("total")
    if isinstance(passed, int) and isinstance(total, int) and total > 0 and passed < int(resolved["min_routing_passes"]):
        requires_confirmation.append(
            {
                "kind": "routing",
                "passed": passed,
                "total": total,
                "message": f"Routing test passed {passed}/{total}.",
            }
        )

    duplicate = review.get("duplicate") if isinstance(review.get("duplicate"), dict) else {}
    score = duplicate.get("score")
    if isinstance(score, int | float) and float(score) >= float(resolved["duplicate_score_at_least"]):
        requires_confirmation.append(
            {
                "kind": "duplicate",
                "score": float(score),
                "message": "Duplicate check found a close neighboring skill.",
            }
        )

    return {
        "can_register": not blocking and not requires_confirmation,
        "requires_confirmation": bool(requires_confirmation),
        "blocked": bool(blocking),
        "blocking": blocking,
        "confirmations": requires_confirmation,
    }


def _draft_payload(draft: SkillDraftResult, *, policy: Any | None = None) -> dict[str, Any]:
    payload = {
        "draft_id": draft.draft_id,
        "name": draft.name,
        "status": draft.status,
        "markdown": draft.markdown,
        "review": draft.review_json,
        "routing_cases": draft.routing_cases_json,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
    }
    resolved_policy = _policy_payload(policy)
    if resolved_policy is not None:
        payload["policy"] = resolved_policy
    payload["governance"] = _draft_governance(draft, policy)
    return payload


def skill_manage_list_payload(workspace_path: Path) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    drafts = store.list_skill_drafts()
    status_counts = store.status_counts()
    if drafts:
        status_counts["draft"] = status_counts.get("draft", 0) + len(drafts)
    return {
        "skills": store.managed_list(include_deprecated=True),
        "drafts": [_draft_payload(draft) for draft in drafts],
        "installed_tools": installed_tools_payload(workspace_path),
        "status_counts": status_counts,
    }


def skill_manage_search_payload(
    workspace_path: Path,
    query: str,
    *,
    top_k: int = 10,
) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    query = query.strip()
    return {
        "query": query,
        "matches": store.managed_search(query, top_k=top_k) if query else [],
    }


def skill_manage_audit_payload(workspace_path: Path) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    return {"audit": store.audit_catalog().to_dict()}


def skill_manage_detail_payload(
    workspace_path: Path,
    name: str,
    *,
    trace_limit: int = 10,
) -> dict[str, Any] | None:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    return store.managed_detail(name, trace_limit=trace_limit)


def skill_manage_status_payload(
    workspace_path: Path,
    name: str,
    action: str,
) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    normalized = action.strip().lower()
    if normalized in {"approve", "register", "candidate"}:
        row = store.approve_draft(name)
    elif normalized in {"promote", "verify", "verified"}:
        row = store.promote(name)
    elif normalized in {"deprecate", "deprecated"}:
        row = store.deprecate_skill(name)
    elif normalized in {"reject", "rejected"}:
        row = store.reject_skill(name)
    else:
        raise ValueError(f"invalid status action {action!r}")
    return {"skill": row_to_skill_payload(row), "action": normalized}


def skill_manage_update_payload(
    workspace_path: Path,
    name: str,
    markdown: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    if dry_run:
        row = store.get_skill(name)
        return {
            "assessment": _assessment_payload(store.classify_skill_update(name, markdown)),
            "skill": row_to_skill_payload(row) if row is not None else None,
            "dry_run": True,
        }
    result = store.update_skill_markdown(name, markdown, system_dir=SYSTEM_SKILLS_DIR)
    return {
        "assessment": _assessment_payload(result.assessment),
        "skill": row_to_skill_payload(result.row) if result.row is not None else None,
        "dry_run": False,
    }


def skill_manage_compose_draft_payload(
    workspace_path: Path,
    values: dict[str, Any],
    *,
    policy: Any | None = None,
    content: SkillDraftContent | None = None,
) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    draft = store.create_skill_draft(
        name=str(values.get("name") or ""),
        description=str(values.get("description") or ""),
        trigger=str(values.get("trigger") or values.get("triggers") or ""),
        method=str(values.get("method") or ""),
        category=str(values.get("category") or "general"),
        risk_level=str(values.get("risk_level") or values.get("riskLevel") or "low"),
        requires_exec=bool(values.get("requires_exec") or values.get("requiresExec") or False),
        required_tools=[str(item) for item in values.get("required_tools", []) if str(item)]
        if isinstance(values.get("required_tools"), list)
        else [],
        install_sources=[str(item) for item in values.get("install_sources", []) if str(item)]
        if isinstance(values.get("install_sources"), list)
        else [],
        content=content,
    )
    return {"draft": _draft_payload(draft, policy=policy)}


def skill_manage_import_payload(workspace_path: Path, markdown: str) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    parsed = parse_skill_markdown(markdown)
    fields = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else {}
    name = str(fields.get("name") or "")
    validation = parsed.get("validation") if isinstance(parsed.get("validation"), dict) else {}
    errors = [str(item) for item in validation.get("errors", []) if str(item)]
    warnings = [str(item) for item in validation.get("warnings", []) if str(item)]
    if name and store.get_skill(name) is not None:
        errors.append(f"skill '{name}' already exists")
    if name and (workspace_path / "skills" / name).exists():
        errors.append(f"skill directory already exists: {name}")
    parsed["validation"] = {"errors": errors, "warnings": warnings}
    return {"import": parsed}


def skill_manage_import_package_payload(
    workspace_path: Path,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    parsed = parse_skill_package_files(files)
    fields = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else {}
    name = str(fields.get("name") or "")
    validation = parsed.get("validation") if isinstance(parsed.get("validation"), dict) else {}
    errors = [str(item) for item in validation.get("errors", []) if str(item)]
    warnings = [str(item) for item in validation.get("warnings", []) if str(item)]
    if name and store.get_skill(name) is not None:
        errors.append(f"skill '{name}' already exists")
    if name and (workspace_path / "skills" / name).exists():
        errors.append(f"skill directory already exists: {name}")
    parsed["validation"] = {"errors": errors, "warnings": warnings}
    return {"import": parsed}


def skill_manage_import_package_zip_payload(
    workspace_path: Path,
    data_b64: str,
) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    parsed = parse_skill_package_zip(data_b64)
    fields = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else {}
    name = str(fields.get("name") or "")
    validation = parsed.get("validation") if isinstance(parsed.get("validation"), dict) else {}
    errors = [str(item) for item in validation.get("errors", []) if str(item)]
    warnings = [str(item) for item in validation.get("warnings", []) if str(item)]
    if name and store.get_skill(name) is not None:
        errors.append(f"skill '{name}' already exists")
    if name and (workspace_path / "skills" / name).exists():
        errors.append(f"skill directory already exists: {name}")
    parsed["validation"] = {"errors": errors, "warnings": warnings}
    return {"import": parsed}


def skill_manage_import_draft_payload(
    workspace_path: Path,
    values: dict[str, Any],
    *,
    policy: Any | None = None,
) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    method = str(values.get("method") or "")
    review = values.get("review") if isinstance(values.get("review"), dict) else {}
    validation = values.get("validation") if isinstance(values.get("validation"), dict) else {}
    red_flags = [
        {
            "kind": "import",
            "severity": "medium",
            "message": str(message),
        }
        for message in validation.get("errors", [])
        if str(message)
    ]
    review = {
        "status": "ready",
        "summary": "Imported skill draft. Review external content before registration.",
        "security_risk_level": str(values.get("risk_level") or values.get("riskLevel") or "low"),
        **review,
        "red_flags": [*red_flags, *[item for item in review.get("red_flags", []) if isinstance(item, dict)]],
        "import": {
            "preserved_method": True,
            "estimated_fields": values.get("estimated_fields") or values.get("estimatedFields") or [],
            "warnings": validation.get("warnings", []),
            "package_files": values.get("package_files") or values.get("packageFiles") or [],
        },
    }
    trigger = str(values.get("trigger") or values.get("triggers") or "")
    triggers = [line.strip() for line in trigger.splitlines() if line.strip()]
    routing_cases = [
        {"query": item, "expected": str(values.get("name") or "")}
        for item in triggers[:10]
    ]
    package_routing_cases = values.get("routing_cases") or values.get("routingCases")
    if isinstance(package_routing_cases, list):
        routing_cases = [
            {
                "query": str(item.get("query") or ""),
                "expected": str(item.get("expected") or values.get("name") or ""),
            }
            for item in package_routing_cases
            if isinstance(item, dict) and str(item.get("query") or "").strip()
        ] or routing_cases
    package_attachments = values.get("attachments")
    if not isinstance(package_attachments, list):
        package_attachments = []
    draft = store.create_skill_draft(
        name=str(values.get("name") or ""),
        description=str(values.get("description") or ""),
        trigger=trigger,
        method=method,
        category=str(values.get("category") or "general"),
        risk_level=str(values.get("risk_level") or values.get("riskLevel") or "low"),
        requires_exec=bool(values.get("requires_exec") or values.get("requiresExec") or False),
        required_tools=[str(item) for item in values.get("required_tools", []) if str(item)]
        if isinstance(values.get("required_tools"), list)
        else [],
        install_sources=[str(item) for item in values.get("install_sources", []) if str(item)]
        if isinstance(values.get("install_sources"), list)
        else [],
        content=SkillDraftContent(
            method=method,
            review=review,
            routing_cases=routing_cases,
            attachments=[
                {"path": str(item.get("path") or ""), "content": str(item.get("content") or "")}
                for item in package_attachments
                if isinstance(item, dict)
            ],
        ),
    )
    return {"draft": _draft_payload(draft, policy=policy)}


def skill_manage_start_draft_payload(
    workspace_path: Path,
    values: dict[str, Any],
    *,
    policy: Any | None = None,
) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    draft = store.start_skill_draft(
        name=str(values.get("name") or ""),
        description=str(values.get("description") or ""),
        trigger=str(values.get("trigger") or values.get("triggers") or ""),
        method=str(values.get("method") or ""),
        category=str(values.get("category") or "general"),
        risk_level=str(values.get("risk_level") or values.get("riskLevel") or "low"),
        requires_exec=bool(values.get("requires_exec") or values.get("requiresExec") or False),
    )
    return {"draft": _draft_payload(draft, policy=policy)}


def skill_manage_complete_draft(
    workspace_path: Path,
    draft_id: str,
    *,
    content: SkillDraftContent | None = None,
    error: str | None = None,
) -> SkillDraftResult:
    store = SkillStore(workspace_path)
    return store.complete_skill_draft(draft_id, content=content, error=error)


def skill_manage_draft_payload(
    workspace_path: Path,
    draft_id: str,
    *,
    policy: Any | None = None,
) -> dict[str, Any] | None:
    store = SkillStore(workspace_path)
    draft = store.get_skill_draft(draft_id)
    if draft is None:
        return None
    return {"draft": _draft_payload(draft, policy=policy)}


def skill_manage_approve_draft_payload(
    workspace_path: Path,
    draft_id: str,
    *,
    policy: Any | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    draft = store.get_skill_draft(draft_id)
    if draft is None:
        raise KeyError(f"draft not found: {draft_id}")
    governance = _draft_governance(draft, policy)
    if governance["blocked"]:
        raise PermissionError("draft has non-overridable red flags")
    confirmations = [
        item for item in governance.get("confirmations", []) if isinstance(item, dict)
    ]
    has_duplicate_confirmation = any(
        str(item.get("kind") or "").lower() == "duplicate" for item in confirmations
    )
    if has_duplicate_confirmation:
        differentiation = (approval or {}).get("differentiation")
        relations = (approval or {}).get("relations")
        has_differentiation = bool(str(differentiation or "").strip())
        has_relation = False
        if isinstance(relations, dict):
            has_relation = any(
                bool(relations.get(key))
                for key in ("conflicts_with", "supersedes", "fallback_to")
            )
        if not has_differentiation or not has_relation:
            raise ValueError(
                "duplicate draft requires trigger differentiation and relation wiring"
            )
    elif governance["requires_confirmation"] and not str((approval or {}).get("reason") or "").strip():
        raise ValueError("override reason is required for red-flagged draft")
    draft, row = store.approve_composed_draft(draft_id, system_dir=SYSTEM_SKILLS_DIR)
    return {
        "draft": _draft_payload(draft, policy=policy),
        "skill": row_to_skill_payload(row) if row is not None else None,
    }


def skill_manage_discard_draft_payload(workspace_path: Path, draft_id: str) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    deleted = store.delete_skill_draft(draft_id)
    if not deleted:
        raise KeyError(f"draft not found: {draft_id}")
    return {"draft_id": draft_id, "deleted": True}


def skill_manage_pending_approvals_payload(workspace_path: Path) -> dict[str, Any]:
    """List everything a human could approve right now, from any front-end.

    Merges the two draft surfaces: composer drafts not yet materialized
    (``skill_drafts`` table) and workspace SKILL.md files already indexed
    with ``status=draft`` (e.g. a copied-in external skill or a hand-written
    file). Chat, WebUI, and CLI all resolve approval targets against this
    same list so "which name can I approve" never diverges between them.
    """
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    composed = [
        {
            "name": draft.name,
            "source": "composed",
            "status": draft.status,
            "draft_id": draft.draft_id,
            "updated_at": draft.updated_at,
        }
        for draft in store.list_skill_drafts()
        if draft.status == "ready"
    ]
    materialized = [
        {
            "name": row["name"],
            "source": "file",
            "status": row["status"],
            "draft_id": None,
            "updated_at": row["updated_at"],
        }
        for row in store.list_skills(include_deprecated=False)
        if row["status"] == "draft"
    ]
    return {"pending": composed + materialized}


def skill_manage_chat_approve_payload(
    workspace_path: Path,
    name: str,
    *,
    policy: Any | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Resolve *name* against either draft surface and approve it.

    This is the shared entry point for chat-based approval: it performs the
    same lookup a human would do in the WebUI draft inbox, then delegates to
    the existing governance-checked approval paths. It never bypasses the
    duplicate-differentiation or red-flag gates in :func:`_draft_governance` —
    a chat one-liner can supply a free-text override *reason* for a
    confirmable flag, but a duplicate-neighbor flag still requires the
    structured relation wiring only the WebUI form collects, and a blocking
    red flag cannot be overridden from here at all.
    """
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)

    row = store.get_skill(name)
    if row is not None and row["status"] == "draft":
        approved = store.approve_draft(name)
        return {"source": "file", "skill": row_to_skill_payload(approved)}

    composed_matches = [
        draft
        for draft in store.list_skill_drafts()
        if draft.name == name and draft.status == "ready"
    ]
    if composed_matches:
        draft_id = composed_matches[0].draft_id
        approval = {"reason": reason} if reason else None
        payload = skill_manage_approve_draft_payload(
            workspace_path, draft_id, policy=policy, approval=approval
        )
        return {"source": "composed", **payload}

    raise KeyError(f"no pending draft named '{name}'")


def _routing_test_payload(result: Any) -> dict[str, Any]:
    return {
        "passed": result.passed,
        "total": result.total,
        "accuracy": result.accuracy,
        "rows": [
            {
                "query": row.query,
                "expected": row.expected,
                "actual": row.actual,
                "ok": row.ok,
            }
            for row in result.rows
        ],
    }


def _load_routing_cases(path: Path) -> list[dict[str, object]]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("cases", [])
    if not isinstance(data, list):
        raise ValueError("routing cases must be a list or {cases: [...]}")
    return [item for item in data if isinstance(item, dict)]


def skill_manage_routing_test_payload(
    workspace_path: Path,
    name: str,
    *,
    top_k: int = 3,
) -> dict[str, Any]:
    store = SkillStore(workspace_path)
    store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)
    row = store.get_skill(name)
    if row is None:
        raise KeyError(f"skill not found: {name}")
    if row["status"] == "system":
        raise ValueError(f"system skill '{name}' cannot run managed routing tests")
    skill_path = Path(row["path"]).resolve(strict=False)
    workspace_skills = (workspace_path / "skills").resolve(strict=False)
    try:
        skill_path.relative_to(workspace_skills)
    except ValueError as exc:
        raise ValueError(f"skill '{name}' is not a workspace skill") from exc
    cases_path = skill_path.parent / "routing_cases.json"
    if not cases_path.is_file():
        return {
            "available": False,
            "cases_path": str(cases_path),
            "passed": 0,
            "total": 0,
            "accuracy": 0.0,
            "rows": [],
        }
    cases = _load_routing_cases(cases_path)
    result = store.run_routing_test(cases, top_k=top_k)
    payload = _routing_test_payload(result)
    payload.update({"available": True, "cases_path": str(cases_path)})
    return payload
