"""Propose a skill-draft approval to the human, in chat.

This tool never approves anything itself. It only records a pending
confirmation on the session; the actual approval happens later, deterministically,
in ``AgentLoop._state_command`` when the user's very next message is an exact
yes/no reply (see ``nanobot.session.skill_approval_state``). This keeps the
governance invariant intact: only a real user reply to a real pending item can
promote a draft, never the model's own judgment about what the user meant.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.context import ContextAware, RequestContext
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.session.skill_approval_state import set_pending_skill_approval
from nanobot.webui.skill_manage_api import skill_manage_pending_approvals_payload


@tool_parameters(
    tool_parameters_schema(
        name=StringSchema(
            "Exact name of the pending skill draft to ask the user about "
            "(must already exist as a draft — check skill_search results, the "
            "composer draft you just created, or an imported skill's name)."
        ),
        required=["name"],
    )
)
class SkillRequestApprovalTool(Tool, ContextAware):
    """Register a pending skill-draft confirmation for this chat session."""

    def __init__(self, workspace: str, sessions: Any):
        self.workspace = Path(workspace)
        self._sessions = sessions
        self._session_key: ContextVar[str | None] = ContextVar(
            "skill_request_approval_session_key", default=None
        )

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        sess = getattr(ctx, "sessions", None)
        assert sess is not None  # guarded by enabled()
        return cls(workspace=ctx.workspace, sessions=sess)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None

    @property
    def name(self) -> str:
        return "skill_request_approval"

    @property
    def description(self) -> str:
        return (
            "Ask the user, in this chat, whether to register a pending skill draft. "
            "Call this instead of running `nanobot skill approve` yourself and instead of "
            "telling the user to type a slash command — this tool sets up a pending "
            "confirmation, and you then ask the question in your own words in the same "
            "reply (e.g. \"<name> 스킬을 등록하려면 승인이 필요합니다. 승인하시겠습니까?\"). "
            "Only the user's very next message, if it is a plain yes/no, actually approves "
            "or cancels it — you never decide this yourself, and calling this tool again "
            "before they answer just resets the question."
        )

    @property
    def read_only(self) -> bool:
        return False

    def set_context(self, ctx: RequestContext) -> None:
        self._session_key.set(ctx.session_key or f"{ctx.channel}:{ctx.chat_id}")

    async def execute(self, name: str, **_kwargs: Any) -> str:
        session_key = self._session_key.get()
        if not session_key:
            return json.dumps(
                {"ok": False, "error": "no active chat session for this request"},
                ensure_ascii=False,
            )
        name = (name or "").strip()
        if not name:
            return json.dumps({"ok": False, "error": "name is required"}, ensure_ascii=False)

        pending = skill_manage_pending_approvals_payload(self.workspace)["pending"]
        match = next((item for item in pending if item["name"] == name), None)
        if match is None:
            available = ", ".join(item["name"] for item in pending) or "(none)"
            return json.dumps(
                {
                    "ok": False,
                    "error": f"no pending draft named '{name}'. Pending drafts: {available}",
                },
                ensure_ascii=False,
            )

        session = self._sessions.get_or_create(session_key)
        set_pending_skill_approval(
            session.metadata,
            name=name,
            source=match["source"],
            draft_id=match.get("draft_id"),
        )
        self._sessions.save(session)
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "note": (
                    "Pending confirmation recorded. Ask the user to confirm now, in your own "
                    "words, in this same reply. Their next message resolves it only if it is a "
                    "plain yes or no — anything else leaves it pending."
                ),
            },
            ensure_ascii=False,
        )
