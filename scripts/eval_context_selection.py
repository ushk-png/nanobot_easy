"""Offline, synthetic input-size comparison. No network or paid model is used.

Run: PYTHONPATH=. .venv/bin/python scripts/eval_context_selection.py
This is a diagnostic example, not evidence of real-model accuracy or completion rate.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.context_governance import ContextGovernanceConfig, ContextGovernor
from nanobot.agent.context_selection import ContextSelector, IntentDesign
from nanobot.agent.context_trace import ContextTrace, active_context_trace
from nanobot.agent.skills import SkillsLoader
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ContextSelectionConfig
from nanobot.memory.event_store import ConversationEventStore
from nanobot.memory.models import MemoryScope
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.session.manager import Session


class OfflineProvider(LLMProvider):
    def __init__(self, design=None):
        super().__init__()
        self.design = design

    def get_default_model(self):
        return "offline-fixture"

    async def chat(self, messages, **kwargs):
        if self.design:
            return LLMResponse(content=None, tool_calls=[ToolCallRequest(
                "design", "submit_context_design", self.design.model_dump())])
        return LLMResponse(content="4")


class FixtureSkills(SkillsLoader):
    def list_skills(self, filter_unavailable=True):
        return []


class FixtureRead(Tool):
    name = "read_file"
    description = "Read local files using the existing workspace permission checks"
    parameters = {"type": "object", "properties": {"path": {"type": "string"}},
                  "required": ["path"]}
    read_only = True

    async def execute(self, path, **kwargs):
        raise AssertionError("This direct-answer fixture never reads a file")


async def evaluate(workspace: Path) -> dict:
    store = ConversationEventStore(workspace)
    scope = MemoryScope.from_runtime(owner_id="fixture", workspace_id=str(workspace), agent_id=None)
    session = Session(key="fixture:session")
    registry = ToolRegistry()
    registry.register(FixtureRead())
    for index in range(30):
        user = f"Inspect synthetic checkpoint {index}; deployment is forbidden."
        session.messages.append({"role": "user", "content": user})
        store.append_event(scope=scope, conversation_id=session.key, session_id=session.key,
                           actor="user", event_type="USER_MESSAGE", content=user)
        call = {"id": f"c{index}", "type": "function", "function": {
            "name": "read_file", "arguments": '{"path":"fixture.txt"}'}}
        session.messages.append({"role": "assistant", "content": "Inspecting checkpoint",
                                 "tool_calls": [call]})
        session.messages.append({"role": "tool", "name": "read_file", "tool_call_id": f"c{index}",
                                 "content": f"checkpoint={index}; value=2\n" * 150})
        session.messages.append({"role": "assistant", "content": "Inspection remains unfinished."})
        store.append_event(scope=scope, conversation_id=session.key, session_id=session.key,
                           actor="assistant", event_type="ASSISTANT_MESSAGE",
                           content="Inspection remains unfinished.")
    request = "2+2?"
    design = IntentDesign.model_validate({"goal": "Answer the self-contained arithmetic question",
        "request_type": "explain", "task_relation": "new", "task_id": None,
        "route": "direct", "constraints": [], "information_needs": [],
        "capabilities_needed": [], "completion_checks": ["Return 4"], "unresolved": [],
        "answer": "4"})
    builder = ContextBuilder(workspace)
    legacy_provider = OfflineProvider()
    legacy_messages = builder.build_messages(history=session.get_history(), current_message=request,
                                              include_memory_recent_history=False)
    legacy_messages = ContextGovernor().prepare_for_model(ContextGovernanceConfig(
        provider=legacy_provider, model="offline-fixture", tools=registry, workspace=workspace,
        session_key=session.key, max_tool_result_chars=16000, context_window_tokens=200000),
        legacy_messages, set())
    legacy_trace = ContextTrace("fixture-legacy", stage="legacy")
    token = active_context_trace.set(legacy_trace)
    try:
        await legacy_provider.chat_with_retry(messages=legacy_messages, tools=registry.get_definitions())
    finally:
        active_context_trace.reset(token)
    store.append_event(scope=scope, conversation_id=session.key, session_id=session.key,
                       actor="user", event_type="USER_MESSAGE", content=request)
    selector = ContextSelector(store, ContextSelectionConfig(enabled=True))
    skills = FixtureSkills(workspace)
    minimal = selector.minimal_input(request, scope, session.key, registry, skills)
    selected = await selector.select(provider=OfflineProvider(design), model="offline-fixture",
        request=request, scope=scope, session_id=session.key, registry=registry, skills=skills,
        rules=builder.build_selected_system_prompt(), request_id="fixture-candidate")
    selector.finish(selected, [], stop_reason="completed", had_injections=False)
    old, new = legacy_trace.report(), selected.trace.report()
    return {"fixture": "self-contained question after 30 synthetic tool turns",
        "no_paid_calls": True, "quality_or_completion_rate_proven": False,
        "token_kind": "estimates only; provider usage absent",
        "legacy_total_estimated_tokens": old["estimated_total_tokens"],
        "candidate_total_estimated_tokens_including_design": new["estimated_total_tokens"],
        "candidate_design_calls": len(new["calls"]), "candidate_execution_calls": 0,
        "minimal_input": {k: v for k, v in minimal.items() if k != "capabilities"},
        "capabilities": minimal["capabilities"], "design_output": design.model_dump(),
        "candidate_metrics": new}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="nanobot-context-eval-") as folder:
        print(json.dumps(asyncio.run(evaluate(Path(folder))), ensure_ascii=False, indent=2))
