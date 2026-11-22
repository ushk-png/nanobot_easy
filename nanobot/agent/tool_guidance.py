"""Compose activation guidance for the selected built-in tool groups (compact path only).

The scoped text is always derived from the preserved full guidance of the turn: sections of
the built-in identity/tool contract/memory guidance whose tool groups were not selected are
left out; everything else (user files, memory, skills, summaries, common rules) is kept
byte-for-byte. Any unclassified tool or an unrecognised layout returns the full guidance.
"""

from __future__ import annotations

from nanobot.agent.memory_context import MEMORY_TOOL_GUIDANCE
from nanobot.utils.prompt_templates import render_template

# Exact built-in tool names -> group. Names outside this table (MCP, plugins, unknown) are
# unclassified and fall back to the full guidance.
TOOL_GROUPS = {
    **dict.fromkeys(("read_file", "list_dir", "find_files", "grep"), "files_read"),
    **dict.fromkeys(("write_file", "edit_file", "apply_patch"), "files_write"),
    **dict.fromkeys(("exec", "exec_job", "write_stdin", "list_exec_sessions"), "exec"),
    "run_cli_app": "cli_app",
    **dict.fromkeys(("web_search", "web_fetch"), "web"),
    **dict.fromkeys(("search_memory", "read_memory_events", "forget_memory_events"), "memory"),
    **dict.fromkeys(("skill_search", "skill_decision", "skill_registry",
                     "skill_request_approval"), "skills"),
    "message": "message",
    "cron": "schedule",
    **dict.fromkeys(("spawn", "delegate"), "delegate"),
    # Built-ins whose rules live only in the always-kept common text.
    **dict.fromkeys(("generate_image", "long_task", "complete_goal", "workflow", "my",
                     "student_learning", "genie_study_review"), "common"),
}

# "## Heading" sections of identity.md / tool_contract.md -> groups that need them.
# Headings not listed (runtime, workspace, platform, permission gates, general contract,
# topic memory and its trailing reply/untrusted rules) are always kept.
SECTION_GROUPS = {
    "Search & Discovery": {"files_read", "exec"},
    "Discovery and Reading": {"files_read"},
    # Commands can change code too; retain the existing inspect/edit/verify workflow.
    "File and Coding Workflows": {"files_write", "exec"},
    "Process Execution": {"exec"},
    "CLI App Attachments": {"cli_app"},
    "Web and External Information": {"web"},
    "Messaging and Media": {"message"},
    "Scheduling and Background Work": {"schedule", "files_write"},
}

# "## Skill Use" bullets (by exact opening words) -> groups. Unlisted bullets are kept: they
# hold the common rules (skill creation/approval/promotion boundaries, evidence vs command,
# clarification, continuing execution, direct answers).
SKILL_BULLET_GROUPS = {
    "- `skill_decision` defaults to": {"skills"},
    "- For questions about installed skills": {"skills", "files_read"},
    "- If a specialized Active Skill only partially": {"skills"},
    "- For a request combining multiple distinct": {"skills", "delegate"},
    "- When the composite-task skill applies": {"skills", "delegate"},
    "- Call `skill_search` only when": {"skills"},
    "- When calling `skill_search`": {"skills"},
    "- Structured wording alone": {"skills"},
    "- After `skill_search`": {"skills"},
    "- Default to doing low-risk": {"delegate"},
    "- When spawning or delegating": {"delegate"},
}

ACTIVATION_NOTE = (
    "## Loaded Tool Notes\n\n"
    "- Tool-specific notes above cover only the loaded tools; the common rules above apply to "
    "all work. Call activate_tools again to add tools when the work needs them; their notes "
    "are included then. Omit tool_names to load all tools."
)

_MEMORY_FAILURE_REFERENCE = (
    "For a persistent memory backend failure, follow the bounded retry and stopping rules "
    "in Conversation Memory Retrieval."
)
_MEMORY_FAILURE_RULE = (
    "For a memory backend failure, retry at most once if potentially transient; "
    "otherwise report retrieval unavailable and leave the fact unverified."
)


def tool_groups(names, lookup) -> set[str] | None:
    """Groups of built-in tools, or None when any tool is not a classified built-in."""
    groups = set()
    for name in names:
        tool = lookup(name)
        module = type(tool).__module__ if tool is not None else ""
        if (name not in TOOL_GROUPS or not module.startswith("nanobot.agent.tools.")
                or module in {"nanobot.agent.tools.mcp", "nanobot.agent.tools.loader"}):
            return None
        groups.add(TOOL_GROUPS[name])
    return groups


def _sections(text: str) -> list[tuple[str | None, str]]:
    """Split at '## ' lines; joining the parts returns the original text exactly."""
    parts: list[tuple[str | None, str]] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("## ") or not parts:
            heading = line[3:].strip() if line.startswith("## ") else None
            parts.append((heading, line))
        else:
            parts[-1] = (parts[-1][0], parts[-1][1] + line)
    return parts


def _skill_bullets(section: str, groups: set[str]) -> str:
    kept, keep = [], True
    for line in section.splitlines(keepends=True):
        if line.startswith("- "):
            need = next((g for prefix, g in SKILL_BULLET_GROUPS.items()
                         if line.startswith(prefix)), None)
            keep = need is None or bool(need & groups)
        elif not line.startswith(" ") and line.strip():
            keep = True
        if keep:
            kept.append(line)
    return "".join(kept)


def _scope(text: str, groups: set[str]) -> str:
    out = []
    for heading, body in _sections(text):
        if heading == "Skill Use":
            body = _skill_bullets(body, groups)
        elif heading in SECTION_GROUPS and not SECTION_GROUPS[heading] & groups:
            continue
        out.append(body)
    return "".join(out)


def compose(full: str, groups: set[str] | None, identity: str | None = None) -> str:
    """Full guidance with built-in sections for unselected groups left out."""
    contract = render_template("agent/tool_contract.md")
    if groups is None or full.count(contract) != 1:
        return full
    scoped_contract = _scope(contract, groups)
    if "memory" not in groups:
        # Keep the rule self-contained when its detailed section is not loaded.
        scoped_contract = scoped_contract.replace(_MEMORY_FAILURE_REFERENCE, _MEMORY_FAILURE_RULE)
    scoped = full.replace(contract, scoped_contract.rstrip("\n") + "\n\n" + ACTIVATION_NOTE)
    if MEMORY_TOOL_GUIDANCE and "memory" not in groups and scoped.count(MEMORY_TOOL_GUIDANCE) == 1:
        # Drop the memory-tool section together with its "---" joiner; nothing else moves.
        for joined in ("\n\n---\n\n" + MEMORY_TOOL_GUIDANCE, MEMORY_TOOL_GUIDANCE):
            if scoped.count(joined) == 1:
                scoped = scoped.replace(joined, "")
                break
    if identity and scoped.startswith(identity):
        scoped = _scope(identity, groups) + scoped[len(identity):]
    return scoped
