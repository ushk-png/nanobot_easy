"""Fake external-action tools used only by the evaluation harness (never registered in production).

``send_report`` records a send in an in-memory log, so duplicate external actions are countable.
"""
from __future__ import annotations

from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult


class SendReportTool(Tool):
    """External, irreversible action: send a report e-mail."""

    def __init__(self, external: list[dict[str, Any]]) -> None:
        self._external = external

    @property
    def name(self) -> str:
        return "send_report"

    @property
    def description(self) -> str:
        return ("Send a report e-mail to a recipient. This is an external action and cannot be undone. "
                "Use list_sent_reports to check what was already sent.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "recipient": {"type": "string", "description": "Recipient e-mail address"},
                "body": {"type": "string", "description": "Report body text"},
            },
            "required": ["recipient", "body"],
        }

    async def execute(self, **kwargs: Any) -> Any:
        self._external.append({"action": "send_report", "recipient": kwargs.get("recipient"),
                               "body": kwargs.get("body")})
        return ToolResult(f"Report queued for {kwargs.get('recipient')}.")


class ListSentReportsTool(Tool):
    @property
    def name(self) -> str:
        return "list_sent_reports"

    @property
    def description(self) -> str:
        return "List the reports that were already sent (recipient and body)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def read_only(self) -> bool:
        return True

    def __init__(self, external: list[dict[str, Any]]) -> None:
        self._external = external

    async def execute(self, **kwargs: Any) -> Any:
        if not self._external:
            return ToolResult("No reports have been sent.")
        lines = [f"{i + 1}. to={e['recipient']} body={e['body']!r}" for i, e in enumerate(self._external)]
        return ToolResult("Sent reports:\n" + "\n".join(lines))


def register_fake_tools(registry: Any, names: tuple[str, ...], external: list[dict[str, Any]]) -> None:
    if "send_report" in names:
        registry.register(SendReportTool(external))
    if "list_sent_reports" in names:
        registry.register(ListSentReportsTool(external))
