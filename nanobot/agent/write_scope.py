"""Resolve explicit file prohibitions; unknown scope never grants write permission."""

from __future__ import annotations

import re
from pathlib import Path

_NO_WRITE = re.compile(
    r"저장\s*(?:하지\s*(?:마|말)|금지)|수정\s*하지\s*(?:마|말)"
    r"|검토만|읽기\s*전용|do not (?:save|write|edit)|review only", re.I,
)
_REFERENCE = (
    r"(?:`[^`\n]+`|'[^'\n]+'|\"[^\"\n]+\"|[\w./~\\-]*\.[A-Za-z0-9]+"
    r"|기억\s*원문|원문|검토본)"
)
_REFERENCES = _REFERENCE + r"(?:\s*(?:와|과|및|,|and)\s*" + _REFERENCE + r")*"
_KOREAN_SUBJECT = re.compile(
    r"(?:(?:그리고|또한|단|다만)\s+)?(?P<refs>" + _REFERENCES
    + r")\s*(?:[은는을를]\s*)?(?:(?:직접|절대)\s*)?", re.I,
)
_ENGLISH_OBJECT = re.compile(
    r"\s+(?:the\s+)?(?P<refs>" + _REFERENCES + r")(?=\s*(?:[.!?;,\n]|$))", re.I,
)
_CLAUSE_BOUNDARY = re.compile(r"[.!?;]\s+|\n|,\s+|\s+(?:but|하지만|다만)\s+", re.I)
_WRITE_REQUEST = re.compile(
    r"(?:수정|변경|저장|작성|생성|삭제)\s*(?:해(?=[\s.!?]|$)|해\s*(?:줘|주세요)|하(?:세요|십시오|라))"
    r"|(?:바꿔|고쳐|만들어)\s*(?:줘|주세요)"
    r"|(?:^|[.!?;\n]\s*|\b(?:and|then|but)\s+)"
    r"(?:(?:please|(?:can|could|would)\s+you)\s+)?"
    r"(?:edit|write|save|change|delete|fix|create)(?=\s)", re.I,
)


def protected_files(instruction: str, workspace: Path) -> list[str] | None:
    """Return named protected files, [] for no ban, None for global/unknown bans.

    Every prohibition must have an explicit subject. This intentionally recognizes
    a small grammar, not arbitrary prose. In memory requests, the source and its
    prompt copy have fixed workspace paths; other unnamed objects need clarification.
    The result constrains writes, never authorizes them or trusts model-selected aliases.
    """
    protected = []
    spans = []
    memory_request = bool(re.search(r"기억|\bmemory\b", instruction, re.I))
    for match in _NO_WRITE.finditer(instruction):
        prefix = _CLAUSE_BOUNDARY.split(instruction[:match.start()])[-1]
        subject = _KOREAN_SUBJECT.fullmatch(prefix.strip())
        start, end = match.start() - len(prefix), match.end()
        if subject is None and match.group().lower().startswith("do not "):
            subject = _ENGLISH_OBJECT.match(instruction[match.end():])
            start, end = match.start(), match.end() + (subject.end() if subject else 0)
        if subject is None:
            return None
        spans.append((start, end))
        for token in re.findall(_REFERENCE, subject["refs"]):
            ref = token[1:-1] if token[:1] in {"'", '"', "`"} else token
            if ref in {"원문", "기억 원문", "기억원문", "검토본"}:
                if not memory_request:
                    return None
                filename = "prompt_snapshot.json" if ref == "검토본" else "MEMORY.md"
                ref = str(workspace / "memory" / filename)
            if "://" in ref or "\0" in ref or not ref.strip():
                return None
            # Quoting an unnamed object must not turn it into a file path.
            if not re.search(r"\.[A-Za-z0-9]+$|/$", ref):
                return None
            protected.append(ref)
    # A prohibition alone grants no permission to edit another file. Look for an
    # independent write request, not a verb embedded in the forbidden clause.
    independent = instruction
    for start, end in reversed(spans):
        independent = independent[:start] + " " * (end - start) + independent[end:]
    if protected and not _WRITE_REQUEST.search(independent):
        return None
    return protected
