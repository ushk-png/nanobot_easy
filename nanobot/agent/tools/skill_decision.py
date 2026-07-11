"""Trace the agent's skill routing decision for a turn."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema
from nanobot.skill_store import SkillStore


def _digest_text(text: str, skill_name: str | None = None) -> str:
    raw = f"{skill_name or ''}\n{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


@tool_parameters(
    tool_parameters_schema(
        decision=StringSchema(
            "Routing decision. Use 'hot' for a preloaded Active Skill, 'cold' for a skill_search "
            "candidate, or 'none' when no skill card fits.",
            enum=["hot", "cold", "none"],
        ),
        skill_name=StringSchema(
            "Selected skill name. Leave empty when decision is 'none'.",
            nullable=True,
        ),
        rationale=StringSchema(
            "Brief reason based on the user's actual instruction and the selected or rejected skill cards.",
            max_length=800,
        ),
        wave_no=IntegerSchema(
            description=(
                "Composite-task wave number for this routing decision. Set this for subtasks "
                "inside composite-task so traces can prove wave ordering."
            ),
            minimum=1,
            nullable=True,
        ),
        required=["decision", "rationale"],
    )
)
class SkillDecisionTool(Tool, ContextAware):
    """Record the final skill selection decision for observability."""

    def __init__(self, workspace: str):
        self.workspace = Path(workspace)
        self._session_key: str | None = None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(workspace=ctx.workspace)

    @property
    def name(self) -> str:
        return "skill_decision"

    @property
    def description(self) -> str:
        return (
            "Record the final skill routing decision after reading Active Skill cards or "
            "skill_search candidate cards. This is an observability tool, not a router. "
            "Call it before answering when you apply an Active Skill, apply a skill_search "
            "candidate, or intentionally choose no skill after skill_search."
        )

    @property
    def read_only(self) -> bool:
        return False

    def set_context(self, ctx: RequestContext) -> None:
        self._session_key = ctx.session_key or f"{ctx.channel}:{ctx.chat_id}"

    async def execute(
        self,
        decision: str,
        rationale: str,
        skill_name: str | None = None,
        wave_no: int | None = None,
        **_kwargs: Any,
    ) -> str:
        started = time.perf_counter()
        normalized = (decision or "").strip().lower()
        if normalized not in {"hot", "cold", "none"}:
            return json.dumps({"ok": False, "error": "decision must be hot, cold, or none"}, ensure_ascii=False)
        selected = (skill_name or "").strip() or None
        if normalized == "none":
            selected = None
        elif not selected:
            return json.dumps({"ok": False, "error": "skill_name is required unless decision is none"}, ensure_ascii=False)
        try:
            normalized_wave_no = int(wave_no) if wave_no is not None else None
        except (TypeError, ValueError):
            normalized_wave_no = None
        if normalized_wave_no is not None and normalized_wave_no < 1:
            normalized_wave_no = None

        SkillStore(self.workspace).record_trace(
            trace_id=f"skill_decision:{uuid4().hex}",
            session_key=self._session_key,
            query_digest=_digest_text(rationale or "", selected),
            candidates=[{"name": selected, "match_grade": normalized, "score": None}] if selected else [],
            selected_skill=selected,
            selection_reason=normalized,
            executed_by="main",
            wave_no=normalized_wave_no,
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            notes=(rationale or "").strip() or None,
        )
        return json.dumps(
            {"ok": True, "decision": normalized, "selected_skill": selected, "wave_no": normalized_wave_no},
            ensure_ascii=False,
        )
