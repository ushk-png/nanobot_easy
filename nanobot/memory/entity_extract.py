"""Deterministic entity extraction for Phase 1 conversation memory."""

from __future__ import annotations

import re
from collections.abc import Iterable

_PATH_RE = re.compile(
    r"(?P<path>(?:~|/|\.?\.?/)?[\w@.+-]+(?:/[\w@.+-]+)+(?:\.(?:py|json|ya?ml|md|toml|txt|sh|sql|js|ts|tsx|jsx|html|css))?)"
)
_FILE_RE = re.compile(r"(?<![\w./-])(?P<file>[\w@.+-]+\.(?:py|json|ya?ml|md|toml|txt|sh|sql|js|ts|tsx|jsx|html|css))(?![\w/-])")
_SNAKE_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]*\(\)?")
_CAMEL_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]+(?:[A-Z][a-zA-Z0-9]*)+\b")
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
_TOOL_HINT_RE = re.compile(r"\b(?:tool|Tool|TOOL)[:= ]+([a-zA-Z_][a-zA-Z0-9_\-.]*)")
_AGENT_NAMES = ("nanobot", "Genie", "Heart", "Amira", "Testy", "AGENT_A", "AGENT_B", "지니", "테스티", "아미라")


def extract_entities(content: str | None, metadata_json: str | None = None) -> set[tuple[str, str]]:
    """Return ``(kind, value)`` pairs from raw text without LLM assistance."""
    text = "\n".join(part for part in (content or "", metadata_json or "") if part)
    if not text:
        return set()
    found: set[tuple[str, str]] = set()
    for match in _URL_RE.finditer(text):
        found.add(("url", match.group(0).rstrip(".,")))
    for regex, group in ((_PATH_RE, "path"), (_FILE_RE, "file")):
        for match in regex.finditer(text):
            value = match.group(group).strip("`'\".,;:)")
            if value and "/" in value or "." in value:
                found.add(("path", value))
    for match in _SNAKE_RE.finditer(text):
        value = match.group(0)
        if len(value) >= 3 and "__" not in value:
            found.add(("symbol", value))
    for match in _CAMEL_RE.finditer(text):
        found.add(("symbol", match.group(0)))
    for match in _TOOL_HINT_RE.finditer(text):
        found.add(("tool", match.group(1)))
    lowered = text.lower()
    for name in _AGENT_NAMES:
        if name.lower() in lowered:
            found.add(("agent", name))
    return found


def exact_terms_from_query(query: str, exact: Iterable[str] | None = None) -> set[str]:
    terms = {str(x).strip() for x in (exact or []) if str(x).strip()}
    for kind, value in extract_entities(query):
        if kind in {"path", "symbol", "tool", "agent", "url"}:
            terms.add(value)
    return terms
