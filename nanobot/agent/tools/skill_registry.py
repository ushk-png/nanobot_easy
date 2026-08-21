"""Read-only skill registry inspection tool."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from nanobot.agent.skills import BUILTIN_SKILLS_DIR, SYSTEM_SKILLS_DIR
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware
from nanobot.agent.tools.schema import BooleanSchema, IntegerSchema, StringSchema, tool_parameters_schema
from nanobot.skill_store import SkillStore

_VALID_STATUSES = ("system", "draft", "candidate", "verified", "deprecated", "rejected")
_VALID_SOURCES = ("workspace", "builtin", "system")
_MAX_LIMIT = 200


def _coerce_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 100
    return max(1, min(_MAX_LIMIT, limit))


@tool_parameters(
    tool_parameters_schema(
        status=StringSchema(
            "Optional registry status filter.",
            enum=_VALID_STATUSES,
            nullable=True,
        ),
        source=StringSchema(
            "Optional skill source filter.",
            enum=_VALID_SOURCES,
            nullable=True,
        ),
        include_deprecated=BooleanSchema(
            description="Whether to include deprecated/rejected skills when no status filter is provided.",
            default=True,
        ),
        limit=IntegerSchema(
            description="Maximum number of skill rows to return.",
            minimum=1,
            maximum=_MAX_LIMIT,
        ),
    )
)
class SkillRegistryTool(Tool, ContextAware):
    """Inspect the skill registry, not the skills directory layout."""

    def __init__(self, workspace: str):
        self.workspace = Path(workspace)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    @property
    def name(self) -> str:
        return "skill_registry"

    @property
    def description(self) -> str:
        return (
            "List registered skills and their lifecycle status from the workspace Skill Registry. "
            "Use this for questions about installed, approved, candidate, verified, deprecated, "
            "system, or available skill status. The registry database is the source of truth; "
            "do not infer skill names or status from workspace/skills directory names because "
            "scoped package folders such as @scope/name are not skill names."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        status: str | None = None,
        source: str | None = None,
        include_deprecated: bool = True,
        limit: int = 100,
        **_kwargs: Any,
    ) -> str:
        normalized_status = str(status).strip() if status is not None else None
        normalized_source = str(source).strip() if source is not None else None
        if normalized_status and normalized_status not in _VALID_STATUSES:
            return self.error(f"Invalid status: {normalized_status}")
        if normalized_source and normalized_source not in _VALID_SOURCES:
            return self.error(f"Invalid source: {normalized_source}")

        store = SkillStore(self.workspace)
        store.ensure_index(builtin_dir=BUILTIN_SKILLS_DIR, system_dir=SYSTEM_SKILLS_DIR)
        rows = store.managed_list(include_deprecated=True)
        summary_status = Counter(str(row.get("status") or "") for row in rows)
        summary_source = Counter(str(row.get("source") or "") for row in rows)

        filtered = rows
        if normalized_status:
            filtered = [row for row in filtered if row.get("status") == normalized_status]
        elif not include_deprecated:
            filtered = [
                row for row in filtered
                if row.get("status") not in {"deprecated", "rejected"}
            ]
        if normalized_source:
            filtered = [row for row in filtered if row.get("source") == normalized_source]

        capped = filtered[:_coerce_limit(limit)]
        skills = [
            {
                "name": row.get("name"),
                "status": row.get("status"),
                "source": row.get("source"),
                "category": row.get("category"),
                "risk_level": row.get("risk_level"),
                "requires_exec": bool(row.get("requires_exec")),
                "usage_count": int(row.get("usage_count") or 0),
                "success_count": int(row.get("success_count") or 0),
                "failure_count": int(row.get("failure_count") or 0),
                "routing_failure_count": int(row.get("routing_failure_count") or 0),
                "path": row.get("path"),
            }
            for row in capped
        ]
        payload = {
            "workspace": str(self.workspace),
            "db_path": str(store.db_path),
            "source_of_truth": "workspace/.skillstore/skillstore.db",
            "summary": {
                "total": len(rows),
                "by_status": dict(sorted(summary_status.items())),
                "by_source": dict(sorted(summary_source.items())),
                "filtered": len(filtered),
                "returned": len(skills),
            },
            "skills": skills,
            "note": (
                "Use these registry names and statuses for skill status answers. "
                "Do not derive skill names from parent directories under workspace/skills."
            ),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
