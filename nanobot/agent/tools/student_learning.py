"""Student-mode learning data tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.path_utils import resolve_workspace_path
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.config.paths import get_workspace_path
from nanobot.student import ReviewQueueStore, StudyLogEntry, append_study_log


def _student_path(ctx: Any, attr: str, default: str) -> str:
    student_mode = getattr(ctx, "student_mode", None)
    return str(getattr(student_mode, attr, default) or default)


@tool_parameters(
    tool_parameters_schema(
        action=StringSchema(
            "Action to perform.",
            enum=["log_study", "upsert_review", "due_reviews"],
        ),
        subject=StringSchema("Subject, e.g. biology or math."),
        concept=StringSchema("Concept name, e.g. osmosis."),
        source=StringSchema("Optional local source label or material reference."),
        difficulty=StringSchema("Optional difficulty label for study logs."),
        student_attempt=StringSchema("Optional student attempt text for study logs."),
        next_action=StringSchema("Optional next action for study logs."),
        due_date=StringSchema("YYYY-MM-DD due date for review queue actions."),
        date=StringSchema("YYYY-MM-DD date for due_reviews; defaults to caller-provided due_date."),
        required=["action"],
    )
)
class StudentLearningTool(Tool):
    """Structured local student-mode study log and review queue operations."""

    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        study_log_path: str = "study_log.jsonl",
        review_queue_path: str = "review_queue.jsonl",
    ) -> None:
        self.workspace = Path(workspace).expanduser() if workspace is not None else get_workspace_path()
        self.study_log_path = study_log_path
        self.review_queue_path = review_queue_path

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        return cls(
            workspace=ctx.workspace,
            study_log_path=_student_path(ctx, "study_log_path", "study_log.jsonl"),
            review_queue_path=_student_path(ctx, "review_queue_path", "review_queue.jsonl"),
        )

    @property
    def name(self) -> str:
        return "student_learning"

    @property
    def description(self) -> str:
        return (
            "Safely write or read structured student-mode local learning data. "
            "Use this instead of generic file writes for study logs and spaced-review queues."
        )

    def _resolve(self, path: str) -> Path:
        return resolve_workspace_path(
            path,
            self.workspace,
            self.workspace,
            [],
            [],
            include_media_dir=False,
        )

    async def execute(
        self,
        action: str,
        subject: str = "",
        concept: str = "",
        source: str = "",
        difficulty: str = "",
        student_attempt: str = "",
        next_action: str = "",
        due_date: str = "",
        date: str = "",
        **_kwargs: Any,
    ) -> str:
        if action == "log_study":
            if not subject.strip() or not concept.strip():
                return ToolResult.error("Error: log_study requires subject and concept")
            append_study_log(
                self._resolve(self.study_log_path),
                StudyLogEntry(
                    subject=subject,
                    concept=concept,
                    source=source,
                    difficulty=difficulty,
                    student_attempt=student_attempt,
                    next_action=next_action,
                ),
            )
            return json.dumps({"ok": True, "action": action}, ensure_ascii=False)

        store = ReviewQueueStore(self._resolve(self.review_queue_path))
        if action == "upsert_review":
            if not subject.strip() or not concept.strip() or not due_date.strip():
                return ToolResult.error("Error: upsert_review requires subject, concept, and due_date")
            row = store.upsert(
                subject=subject,
                concept=concept,
                source=source,
                due_date=due_date,
            )
            return json.dumps({"ok": True, "review": row}, ensure_ascii=False)

        if action == "due_reviews":
            target_date = (date or due_date).strip()
            if not target_date:
                return ToolResult.error("Error: due_reviews requires date or due_date")
            return json.dumps(
                {"ok": True, "reviews": store.due(target_date)},
                ensure_ascii=False,
            )

        return ToolResult.error(f"Error: unsupported student_learning action {action!r}")
