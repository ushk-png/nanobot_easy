"""t51 local harness: the MOCK model only supplies the design and the executor's tool calls/answers.
Everything else (next-turn input assembly, requirement restoration, gating, tool execution, verdict,
task record) runs through the real AgentLoop / ContextSelector / AgentRunner code."""
import copy
import json

from nanobot.agent.context_selection import ContextSelector, IntentDesign
from nanobot.config.schema import ContextSelectionConfig
from nanobot.memory.task_context import TaskContextStore
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


def design(**overrides):
    data = dict(goal="request", request_type="other", task_relation="new", task_id=None, route="direct",
                constraints=[], information_needs=[], capabilities_needed=[], completion_checks=[],
                unresolved=[], answer=None)
    data.update(overrides)
    return IntentDesign.model_validate(data)


def design_response(d):
    return LLMResponse(content=None, tool_calls=[ToolCallRequest(
        id="design", name="submit_context_design", arguments=d.model_dump())], usage={})


def calls(*items):
    return LLMResponse(content=None, usage={}, tool_calls=[
        ToolCallRequest(id=i, name=n, arguments=a) for i, n, a in items])


def final(text):
    return LLMResponse(content=text, usage={})


class Mock(LLMProvider):
    """Two queues: design calls (submit_context_design offered) and executor calls. An unexpected call
    raises (it is never answered with a canned success)."""

    def __init__(self):
        super().__init__()
        self.designs, self.steps, self.requests = [], [], []

    def get_default_model(self):
        return "MOCK"

    async def chat(self, messages, **kw):
        tools = kw.get("tools") or []
        is_design = any((t.get("function") or {}).get("name") == "submit_context_design" for t in tools)
        self.requests.append({"design": is_design, "messages": copy.deepcopy(messages), "tools": tools})
        queue = self.designs if is_design else self.steps
        if not queue:
            raise AssertionError("unexpected %s call" % ("design" if is_design else "executor"))
        step = queue.pop(0)
        return step(messages) if callable(step) else step


class Session:
    def __init__(self, tmp_path, monkeypatch):
        from nanobot.agent.loop import AgentLoop
        from nanobot.bus.queue import MessageBus
        self.mock, self.selected = Mock(), []
        self.loop = AgentLoop(bus=MessageBus(), provider=self.mock, workspace=tmp_path,
                              context_selection=ContextSelectionConfig(enabled=True))
        original = ContextSelector.select
        sink = self.selected

        async def spy(selector, *a, **k):            # observes only; returns the real object unchanged
            out = await original(selector, *a, **k)
            sink.append(out)
            return out
        monkeypatch.setattr(ContextSelector, "select", spy)
        self.tmp = tmp_path

    async def turn(self, text, design_obj=None, steps=()):
        from nanobot.bus.events import InboundMessage
        if design_obj is not None:
            self.mock.designs.append(design_response(design_obj))
        self.mock.steps.extend(steps)
        msg = InboundMessage(channel="cli", sender_id="owner", chat_id="s", content=text)
        self.msg = msg
        out = await self.loop._process_message(msg)
        return out.content if out else None

    def tasks(self):
        scope = self.loop._memory_scope_for_message(self.msg)
        return TaskContextStore(self.loop.conversation_memory).list(scope, "cli:s")

    def design_payloads(self):
        return [json.loads(r["messages"][1]["content"]) for r in self.mock.requests if r["design"]]

    def executor_requests(self):
        return [r for r in self.mock.requests if not r["design"]]
