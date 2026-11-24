"""Keep credential material out of agent file access and operational logs."""

from __future__ import annotations

import hmac
import os
import re
from pathlib import Path
from typing import Any

_SECRET_DIRS = {".secrets", ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".git"}
_SECRET_NAMES = {"auth.json", "oauth.json", "credentials", "credentials.json", "credentials.ini",
                 ".envrc", ".netrc", ".npmrc", ".pypirc", ".git-credentials"}
_SECRET_FIELDS = re.compile(r"token|secret|password|api.?key|^access$|^refresh$", re.I)
_VALUES: set[str] = set()
_PATTERNS = (
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\b(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{16,}\b"), "[REDACTED]"),
    (re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/-]{8,}=*"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:token|api[_-]?key|secret|access_token|refresh_token)=)[^&\s\"'<>]+"), r"\1[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED]"),
)


def constant_time_equals(supplied: str, expected: str) -> bool:
    """Compare credentials without raising on non-ASCII input.

    hmac.compare_digest rejects non-ASCII ``str`` with TypeError, so a client-supplied
    header or query value could turn an auth check into a server error.
    """
    return hmac.compare_digest(
        supplied.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def is_sensitive_path(path: str | Path) -> bool:
    """Check logical and resolved names, so an innocuous symlink cannot expose a key."""
    candidate = Path(path).expanduser()
    for item in (candidate, candidate.resolve(strict=False)):
        parts = {part.lower() for part in item.parts}
        name = item.name.lower()
        if (parts & _SECRET_DIRS or name in _SECRET_NAMES
                or name == ".env" or name.startswith(".env.") or name.endswith(".env")
                or name.startswith(("id_rsa", "id_ed25519", "id_ecdsa"))
                or item.suffix.lower() in {".key", ".p12", ".pfx"}):
            return True
    return False


def require_public_path(path: str | Path) -> None:
    if is_sensitive_path(path):
        raise PermissionError("Credential files are unavailable through agent tools and previews.")


def register_secrets(value: Any, *, field: str = "") -> None:
    """Remember actual configured values, without logging or serializing the registry."""
    if isinstance(value, dict):
        for key, item in value.items():
            register_secrets(item, field=str(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            register_secrets(item, field=field)
    elif (isinstance(value, str) and _SECRET_FIELDS.search(field)
          and len(value) >= 12 and not value.startswith(("${", "http://", "https://"))):
        _VALUES.add(value)


def redact(text: str) -> str:
    for value in sorted(_VALUES, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingStream:
    """Sanitize the final formatted record, including exception text from dependencies."""

    def __init__(self, stream):
        self.stream = stream

    def write(self, message):
        return self.stream.write(redact(str(message)))

    def __getattr__(self, name):
        return getattr(self.stream, name)


register_secrets(dict(os.environ))
