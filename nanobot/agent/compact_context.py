"""Turn-local, opt-in deferral of execution instructions and tool schemas."""

from nanobot.agent.tool_guidance import compose, tool_groups
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry


class ActivateTools(Tool):
    name = "activate_tools"
    description = (
        "Load execution instructions and tools before external work. Select only needed "
        "tool_names from the catalog; call again to add tools as work changes. Omit "
        "tool_names to load all tools when the needed capability is unclear. "
        "Not needed for self-contained answers."
    )
    parameters = {}
    read_only = True
    concurrency_safe = False

    def __init__(self, registry, guidance, system_message, identity=None):
        self.registry = registry
        self.guidance = guidance
        self.identity = identity
        self.system_message = system_message
        names = sorted(registry.available_names())
        self.parameters = {
            "type": "object",
            "properties": {"tool_names": {
                "type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "string", "enum": names},
                "description": "Only tools needed for this work, e.g. read_file and write_file for save-and-verify. Omit to load all.",
            }},
            "additionalProperties": False,
        }
        if not names:
            self.parameters["properties"] = {}

    async def execute(self, **kwargs):
        names = kwargs.get("tool_names")
        if names is not None:
            unknown = set(names) - self.registry.available_names()
            if unknown:
                return ToolResult.error("Unavailable tools: " + ", ".join(sorted(unknown)))
        if not self.registry.activated:
            self.registry.selected = set(names) if names is not None else None
        elif names is None:
            self.registry.selected = None
        elif self.registry.selected is not None:
            self.registry.selected.update(names)
        # Restore at system priority, outside tool-result truncation/offloading. Rebuilt from
        # the preserved full guidance and the selected set each time, never appended to.
        selected = self.registry.selected
        groups = None if selected is None else tool_groups(sorted(selected), self.registry.base.get)
        self.system_message["content"] = compose(self.guidance, groups, self.identity)
        self.registry.activated = True
        loaded = ", ".join(sorted(self.registry.selected)) if self.registry.selected is not None else "all available tools"
        return (f"Execution guidance restored; loaded {loaded}. Continue execution and verification. "
                "Call activate_tools again if another tool is needed.")


class CompactTools:
    """Keep the shared registry unchanged; activation lasts for this turn only."""

    def __init__(self, base, guidance, system_message, identity=None):
        self.base = base
        self.activated = False
        self.selected: set[str] | None = None
        self.entry = ToolRegistry()
        self.entry.register(ActivateTools(self, guidance, system_message, identity))

    def __getattr__(self, name):
        return getattr(self.base, name)

    def available_names(self):
        return {ToolRegistry._schema_name(d) for d in self.base.get_definitions()}

    def get_definitions(self):
        if not self.activated:
            return self.entry.get_definitions()
        definitions = self.base.get_definitions()
        if self.selected is None:
            return definitions
        return [*self.entry.get_definitions(), *[
            d for d in definitions if ToolRegistry._schema_name(d) in self.selected]]

    def _loaded(self, name):
        return self.activated and (self.selected is None or name in self.selected)

    def get(self, name):
        if name == "activate_tools":
            return self.entry.get(name)
        return self.base.get(name) if self._loaded(name) else None

    def prepare_call(self, name, params):
        if name == "activate_tools":
            return self.entry.prepare_call(name, params)
        if self._loaded(name):
            return self.base.prepare_call(name, params)
        return None, params, ToolResult.error(
            f"Tool '{name}' is not loaded. Call activate_tools with the required tool_names first.")

    async def execute(self, name, params):
        if name == "activate_tools":
            return await self.entry.execute(name, params)
        if self._loaded(name):
            return await self.base.execute(name, params)
        return self.prepare_call(name, params)[2]
