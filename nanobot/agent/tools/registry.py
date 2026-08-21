"""Tool registry for dynamic tool management."""

import json
from typing import Any

from nanobot.agent.tools.base import Schema, Tool, ToolResult

INTENT_METADATA_FIELDS = frozenset(("intent_summary", "target", "scope", "reversible"))

# Built-in Agent Tools that always can change local state, external state,
# runtime state, or an ongoing process. The registry injects and validates intent
# metadata for these tool calls without changing each tool's execute() signature.
INTENT_METADATA_REQUIRED_TOOLS = frozenset((
    "apply_patch",
    "edit_file",
    "exec",
    "forget_memory_events",
    "message",
    "run_cli_app",
    "skill_request_approval",
    "write_file",
    "write_stdin",
))

# Some tools are only state-changing for particular actions. Keep their metadata
# fields visible in schemas, but enforce the fields only for changing actions.
INTENT_METADATA_CONDITIONAL_TOOLS = frozenset(("cron", "my"))

_INTENT_METADATA_SCHEMA: dict[str, dict[str, Any]] = {
    "intent_summary": {
        "type": "string",
        "minLength": 1,
        "maxLength": 300,
        "description": "One-line interpretation of the user's requested change/action.",
    },
    "target": {
        "type": "string",
        "minLength": 1,
        "maxLength": 300,
        "description": "Concrete target object, path, job id, channel, process, setting, or artifact ID.",
    },
    "scope": {
        "type": "string",
        "enum": ["once", "persistent"],
        "description": "Whether this action is one-time or creates/changes a lasting rule/state. Default reasoning should prefer 'once'.",
    },
    "reversible": {
        "type": "boolean",
        "description": "Whether the action is reversible or cheap to undo.",
    },
}


def is_tool_error_result(name: str, result: Any) -> bool:
    return isinstance(result, ToolResult) and result.is_error


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._cached_definitions: list[dict[str, Any]] | None = None

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool
        self._cached_definitions = None

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)
        self._cached_definitions = None

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    @staticmethod
    def _lookup_key(name: str) -> str:
        """Normalize names for suggestions only; never for execution."""
        return "".join(ch.lower() for ch in name if ch.isalnum())

    def _suggest_name(self, name: str) -> str | None:
        key = self._lookup_key(str(name or ""))
        if not key:
            return None
        matches = [
            registered
            for registered in self._tools
            if self._lookup_key(registered) == key
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    @staticmethod
    def _schema_name(schema: dict[str, Any]) -> str:
        """Extract a normalized tool name from either OpenAI or flat schemas."""
        fn = schema.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if isinstance(name, str):
                return name
        name = schema.get("name")
        return name if isinstance(name, str) else ""

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get tool definitions with stable ordering for cache-friendly prompts.

        Built-in tools are sorted first as a stable prefix, then MCP tools are
        sorted and appended.  The result is cached until the next
        register/unregister call.
        """
        if self._cached_definitions is not None:
            return self._cached_definitions

        definitions = [self._schema_with_intent_metadata(tool) for tool in self._tools.values()]
        builtins: list[dict[str, Any]] = []
        mcp_tools: list[dict[str, Any]] = []
        for schema in definitions:
            name = self._schema_name(schema)
            if name.startswith("mcp_"):
                mcp_tools.append(schema)
            else:
                builtins.append(schema)

        builtins.sort(key=self._schema_name)
        mcp_tools.sort(key=self._schema_name)
        self._cached_definitions = builtins + mcp_tools
        return self._cached_definitions

    @classmethod
    def _has_intent_metadata_schema(cls, name: str) -> bool:
        return name in INTENT_METADATA_REQUIRED_TOOLS or name in INTENT_METADATA_CONDITIONAL_TOOLS

    @classmethod
    def _requires_intent_metadata(cls, name: str, params: dict[str, Any] | None = None) -> bool:
        if name in INTENT_METADATA_REQUIRED_TOOLS:
            return True
        if name == "cron" and isinstance(params, dict):
            return params.get("action") in {"add", "remove"}
        if name == "my" and isinstance(params, dict):
            return params.get("action") == "set"
        return False

    @classmethod
    def _schema_with_intent_metadata(cls, tool: Tool) -> dict[str, Any]:
        schema = tool.to_schema()
        if not cls._has_intent_metadata_schema(tool.name):
            return schema

        fn = schema.get("function")
        if not isinstance(fn, dict):
            return schema
        params = fn.get("parameters")
        if not isinstance(params, dict):
            return schema

        props = params.setdefault("properties", {})
        if not isinstance(props, dict):
            return schema
        props.update({k: dict(v) for k, v in _INTENT_METADATA_SCHEMA.items()})

        required = params.setdefault("required", [])
        if tool.name in INTENT_METADATA_REQUIRED_TOOLS and isinstance(required, list):
            for key in INTENT_METADATA_FIELDS:
                if key not in required:
                    required.append(key)

        desc = fn.get("description")
        suffix = (
            " Before using this state-changing tool, include intent_summary, "
            "target, scope ('once' or 'persistent'), and reversible. If the "
            "target or interpretation is ambiguous, ask/preview instead of "
            "calling the tool."
        )
        if isinstance(desc, str) and suffix not in desc:
            fn["description"] = desc + suffix
        return schema

    @classmethod
    def _validate_intent_metadata(cls, name: str, params: dict[str, Any]) -> list[str]:
        if not cls._requires_intent_metadata(name, params):
            return []
        errors: list[str] = []
        for key in INTENT_METADATA_FIELDS:
            if key not in params:
                errors.append(f"missing required {key} for state-changing tool '{name}'")
                continue
            errors.extend(Schema.validate_json_schema_value(params[key], _INTENT_METADATA_SCHEMA[key], key))
        return errors

    @classmethod
    def _strip_intent_metadata(cls, name: str, params: dict[str, Any]) -> dict[str, Any]:
        if not cls._has_intent_metadata_schema(name):
            return params
        return {k: v for k, v in params.items() if k not in INTENT_METADATA_FIELDS}

    def prepare_call(
        self,
        name: str,
        params: Any,
    ) -> tuple[Tool | None, Any, str | None]:
        """Resolve, cast, and validate one tool call."""
        tool = self._tools.get(name)
        if not tool:
            suggestion = self._suggest_name(str(name))
            hint = f" Did you mean '{suggestion}'? Tool names must match exactly." if suggestion else ""
            return None, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' not found.{hint} Available: {', '.join(self.tool_names)}"
                )
            )

        params = self._coerce_params(tool, params)
        if not isinstance(params, dict):
            return tool, params, (
                ToolResult.error(
                    f"Error: Tool '{name}' parameters must be a JSON object, got "
                    f"{type(params).__name__}. Use named parameters like "
                    'tool_name(param1="value1", param2="value2") matching the tool schema.'
                )
            )

        metadata_errors = self._validate_intent_metadata(name, params)
        execution_params = self._strip_intent_metadata(name, params)

        cast_params = tool.cast_params(execution_params)
        errors = metadata_errors + tool.validate_params(cast_params)
        if errors:
            return tool, cast_params, (
                ToolResult.error(f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors))
            )
        return tool, cast_params, None

    @classmethod
    def _coerce_argument_value(cls, value: Any) -> Any:
        if value is None:
            return {}
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return {}

        if not stripped.startswith(("{", "[")):
            return value

        try:
            parsed = json.loads(stripped)
        except Exception:
            return value

        return parsed

    @classmethod
    def _coerce_params(cls, tool: Tool, params: Any) -> Any:
        params = cls._coerce_argument_value(params)
        return cls._unwrap_arguments_payload(tool, params)

    @classmethod
    def _unwrap_arguments_payload(cls, tool: Tool, params: Any) -> Any:
        if not isinstance(params, dict) or set(params) != {"arguments"}:
            return params
        properties = (tool.parameters or {}).get("properties", {})
        if isinstance(properties, dict) and "arguments" in properties:
            return params
        return cls._coerce_argument_value(params.get("arguments"))

    async def execute(self, name: str, params: Any) -> Any:
        """Execute a tool by name with given parameters."""
        hint = "\n\n[Analyze the error above and try a different approach.]"
        tool, params, error = self.prepare_call(name, params)
        if error:
            return ToolResult.error(str(error) + hint)

        try:
            assert tool is not None  # guarded by prepare_call()
            result = await tool.execute(**params)
            if is_tool_error_result(name, result):
                return ToolResult.error(str(result) + hint)
            return result
        except Exception as e:
            return ToolResult.error(f"Error executing {name}: {str(e)}" + hint)

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
