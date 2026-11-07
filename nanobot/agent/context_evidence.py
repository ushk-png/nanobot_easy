"""Runtime evidence contracts. Document payloads never become control metadata."""
from __future__ import annotations

import hashlib
import re
import json
import shlex
from contextvars import ContextVar
from pathlib import Path
from typing import Any

active_evidence_capture: ContextVar[bool] = ContextVar("context_evidence_capture", default=False)
# Canonical paths that this write must create, never overwrite (t36: exclusive create).
exclusive_create_paths: ContextVar[frozenset] = ContextVar("exclusive_create_paths", default=frozenset())


def lookup_clock() -> str:
    """Execution time of a tool call (UTC, ISO). A record replay substitutes the original time."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def fingerprint(path: str | Path) -> dict | None:
    try:
        fp = Path(path)
        if fp.is_dir():
            # A directory's identity (t35): "the folder exists and is this folder". Its entries are
            # not part of it, so creating the requested file inside does not invalidate the check.
            st = fp.stat()
            return {"directory": True, "dev": st.st_dev, "ino": st.st_ino}
        before = fp.stat()
        digest = hashlib.sha256()
        with fp.open("rb") as stream:
            for chunk in iter(lambda: stream.read(131072), b""):
                digest.update(chunk)
        after = fp.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            return None
        return {"sha256": digest.hexdigest(), "mtime_ns": after.st_mtime_ns, "size": after.st_size}
    except OSError:
        return None


def listing_version(path: str | Path) -> str | None:
    """Content version of a directory: digest of its entry names and types (not identity)."""
    try:
        entries = sorted((e.name, e.is_dir()) for e in Path(path).iterdir())
    except OSError:
        return None
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def canonical(path: str, workspace: str | Path | None) -> str:
    """Normalized target identifier (t36 policy): relative paths are relative to the workspace root
    (cwd only when none is known), ``~`` is expanded, and symlinks/aliases are resolved so one file
    has one identifier. Type (file vs directory) is checked by the consumer; the identifier says
    nothing about content or version."""
    fp = Path(path).expanduser()
    return str((fp if fp.is_absolute() else Path(workspace or Path.cwd()) / fp).resolve())


def current(record: dict) -> bool:
    if record.get("historical"):
        return False
    resource, version = record.get("resource_id"), record.get("version")
    if version and resource:
        return fingerprint(resource) == version
    for path, version in record.get("checked_versions", {}).items():
        if not version or fingerprint(path) != version:
            return False
    return record.get("status") == "ok"


def test_command(command: str) -> bool:
    """Recognize an actual single test-runner invocation, never an echo/proposed command."""
    if any(c in command for c in (";", "|", "&", "`", "$", "\n", ">", "<")):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    if any(arg.split("=", 1)[0] in {"-h", "--help", "--version", "-V", "--collect-only", "--co", "--collectonly"} for arg in argv[1:]):
        return False
    executable = Path(argv[0]).name
    if executable in {"pytest", "pytest.exe"}:
        return True
    if executable.startswith("python"):
        return len(argv) > 2 and argv[1] == "-m" and argv[2] in {"pytest", "unittest"}
    return executable in {"bun", "npm", "cargo", "go"} and len(argv) > 1 and argv[1] == "test"


def observation(selected: Any, call: Any, result: Any, status: str, tool: Any) -> dict:
    """Capture trusted producer attributes, not JSON fields in returned source text."""
    record = {"tool_call_id": call.id, "task_id": (selected.state or {}).get("task_id"),
              "request_id": selected.trace.request_id, "name": call.name, "status": status,
              "read_only": bool(tool and tool.read_only), "result_kind": "read_result" if
              bool(tool and tool.read_only) else "mutation_result",
              "instruction_version": selected.instruction_version,
              "output_hash": hashlib.sha256(str(result).encode()).hexdigest(),
              "resource_id": None, "source_event_id": None, "executed_at": lookup_clock()}
    if not record["read_only"] and status == "ok":
        # t40: object IDs a state-changing tool printed (e.g. "[main 1a2b3c4] msg"), linking the call to
        # the commit it created; a branch switch prints none of the new commit's ID.
        import re as _re
        record["object_refs"] = sorted(set(_re.findall(r"\b[0-9a-f]{7,40}\b", str(result))))[:20]
    coverage = getattr(result, "read_evidence", None)
    if isinstance(coverage, dict):
        record.update(result_kind="file_content", resource_id=coverage.get("target"),
                      version=coverage.get("version"), range={k: coverage.get(k) for k in
                      ("unit", "start", "end", "total")}, extraction_ok=coverage.get("extraction_ok", False))
    memory = getattr(result, "memory_evidence", None)
    if isinstance(memory, dict):
        record.update(memory)
    execution = getattr(result, "execution_evidence", None)
    if isinstance(execution, dict):
        record.update({key: value for key, value in execution.items() if key in {
            "exit_code", "result_kind", "tests_executed", "test_counts", "checked_versions", "source_versions_stable"}})
    if call.arguments.get("url"):
        record["url"] = str(call.arguments["url"])   # identity for repeated-fetch detection
        record["fetch_mode"] = [str(call.arguments.get("extractMode") or "markdown"),
                                call.arguments.get("maxChars")]
    path = call.arguments.get("path")
    if path and not record["resource_id"]:
        record["resource_id"] = canonical(path, selected.workspace)
    if (call.name == "list_dir" and status == "ok" and record["resource_id"]
            and Path(record["resource_id"]).is_dir()):
        # version = the folder's identity (which folder); content_version = its entries at
        # listing time. Equal identity never implies equal content (t36).
        record.update(result_kind="directory_listing", version=fingerprint(record["resource_id"]),
                      content_version=listing_version(record["resource_id"]), extraction_ok=True)
    if not record["read_only"] and record["resource_id"]:
        record["version"] = fingerprint(record["resource_id"])
    failure = failure_class(call.name, str(result), status)
    if failure:
        record["failure_class"] = failure
    # Result text is turn-local evidence for semantic assessments, never telemetry.
    record["_text"] = str(result)
    return record


_TRANSPORT_ERROR = re.compile(r"tls|ssl|handshake|connection (?:reset|refused|error)|timed? ?out|"
                              r"error sending request|name resolution|network is unreachable", re.I)


def failure_class(name: str, text: str, status: str) -> str | None:
    """Type of a failed lookup, from the producer's own error shape (t35), so a transport failure,
    a site that returned nothing, and an empty search are not reported as one "missing evidence".

    transport: TLS/connection/timeout before any response. fetch_rejected: web_fetch answered with
    an error object and no page. no_results: the search ran and found nothing. tool_error: other."""
    stripped = text.strip()
    if name == "web_fetch" and stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict) and "error" in data and not data.get("status") and not data.get("text"):
            return "transport" if _TRANSPORT_ERROR.search(str(data.get("error") or "")) else "fetch_rejected"
    if status == "ok" and not stripped.startswith("Error"):
        return None
    if _TRANSPORT_ERROR.search(stripped):
        return "transport"
    if re.search(r"no results found", stripped, re.I):
        return "no_results"
    return "tool_error"


def need_target(need: Any, workspace: str | Path | None) -> str | None:
    if need.target:
        return canonical(need.target, workspace) if need.source_kind == "file" else need.target
    if need.source_kind == "file":
        import re
        paths = re.findall(r"[\w./~-]+\.(?:py|json|md|txt|ya?ml|toml|csv|pdf|ini|cfg)\b", need.query)
        if len(set(paths)) == 1:
            return canonical(paths[0], workspace)
    return None


def match_need(need: Any, records: list[dict], workspace: Any) -> tuple[bool, str]:
    valid = [r for r in records if r.get("status") == "ok" and current(r)]
    if not valid:
        return False, "successful current evidence required"
    target = need_target(need, workspace)
    if need.source_kind == "file":
        if target is None:
            return False, "required file target must be identified before assessment"
        if Path(target).is_dir():
            # A folder need is about which folder (identity), met by a listing of that same canonical
            # folder whose identity is unchanged. Its entries (content_version) are a separate fact.
            if any(r.get("result_kind") == "directory_listing" and r.get("resource_id") == target
                   and r.get("version") and current(r) for r in valid):
                return True, "directory identity and listing checked"
            return False, "required directory listing of the target does not match"
        valid = [r for r in valid if r.get("result_kind") == "file_content"
                 and r.get("resource_id") and r.get("version") and r.get("extraction_ok") and current(r)
                 and (target is None or r["resource_id"] == target)]
        if not valid:
            return False, "required file identity/version/extraction does not match"
        versions = {(r["resource_id"], json.dumps(r["version"], sort_keys=True)) for r in valid}
        for resource, version in versions:
            group = [r for r in valid if r["resource_id"] == resource and json.dumps(r["version"], sort_keys=True) == version]
            unit = need.required_range.unit if need.required_range else "all"
            start = need.required_range.start if need.required_range else 1
            end = need.required_range.end if need.required_range else None
            for range_unit in ("lines", "pages"):
                ranges = [r["range"] for r in group if r.get("range", {}).get("unit") == range_unit]
                if not ranges or unit not in {"all", range_unit}:
                    continue
                limit = end if unit != "all" else ranges[0].get("total")
                cursor = start
                for span in sorted(ranges, key=lambda r: r["start"]):
                    if span["start"] > cursor:
                        break
                    cursor = max(cursor, span["end"] + 1)
                if limit is not None and cursor > limit:
                    if need.expected_values:
                        try:
                            data = json.loads(Path(resource).read_text())
                            if any(data.get(k) != v for k, v in need.expected_values.items()):
                                return False, "structured expected values do not match"
                        except (ValueError, OSError, AttributeError):
                            return False, "structured expected values unavailable"
                    return True, "resource, version, coverage and structured expectations checked"
        return False, "required range is not covered by one current file version"
    if need.source_kind == "memory":
        valid = [r for r in valid if r.get("result_kind") == "memory_original" and r.get("memory_complete")]
        if not valid:
            return False, "complete memory originals required, not search candidates"
        if need.source_ids and not all(any(sid in r.get("memory_source_ids", []) for r in valid) for sid in need.source_ids):
            return False, "required memory source identities not present"
    if need.source_kind == "tool" and (not need.target or not need.result_kind):
        return False, "required tool evidence needs a target and result-kind contract"
    if need.source_kind == "tool" and not any(need.target in {r.get("resource_id"), r.get("name")} for r in valid):
        return False, "required tool target does not match"
    if need.result_kind and not any(r.get("result_kind") == need.result_kind for r in valid):
        return False, "result kind does not match"
    return True, "source contract checked; meaning remains an attributed executor assessment"


def completion_kind(check: str, selected: Any) -> str:
    """The designer's typed spec decides; then typed obligations; legacy word rule last.

    The word rule only remains for untyped designs without a spec (stored proposals, old models).
    """
    import re
    from nanobot.agent.context_selection import trusted_obligations
    event = getattr(selected, "current_event", None)
    ob = trusted_obligations(selected.design, (event.content or "") if event else "")
    spec_for = getattr(selected, "spec_for", None)
    spec = (spec_for(check) if callable(spec_for)
            else next((s for s in selected.design.completion_specs if s.check == check), None))
    if spec is not None and (spec.result_kind != "semantic" or ob is not None):
        return spec.result_kind
    if ob is not None:
        if ob.external_change == "required":
            return "mutation_result"
        return "sourced" if ob.lookup == "required" else "answer_content"
    if re.search(r"\b(?:tests?|pytest|unittest)\b|테스트", check, re.I):
        return "test_result"
    return "semantic"


def validate_completion(check: str, records: list[dict], selected: Any) -> str | None:
    kind = completion_kind(check, selected)
    valid = [r for r in records if r.get("status") == "ok" and current(r)]
    if len(valid) != len(records) or not records:
        return "current successful execution evidence required"
    if getattr(selected, "compliance_check", None) and selected.compliance_check(check):
        # A prohibition is verified from this turn's execution records, not from a cited write.
        return ("a state-changing execution in this turn violates the prohibition"
                if selected.compliance_failures() else None)
    if kind == "test_result":
        if not any(r.get("result_kind") == "test_result" and r.get("exit_code") == 0 and r.get("tests_executed")
                   and r.get("checked_versions") and current(r) for r in valid):
            return "actual passing test execution against unchanged source versions required"
    elif kind == "sourced":
        if not any(r.get("read_only", True) and current(r) for r in valid):
            return "a successful read/lookup result is required for a sourced check"
    elif kind != "semantic" and not any(r.get("result_kind") == kind and current(r) for r in valid):
        return "completion result kind/version does not match"
    for spec in selected.design.completion_specs:
        if spec.check == check and spec.target:
            target = canonical(spec.target, selected.workspace)
            if not any(r.get("resource_id") == target or target in r.get("checked_versions", {}) for r in valid):
                return "completion evidence targets a different resource"
            if spec.expected_values:
                if Path(target).suffix.lower() != ".json":
                    return text_expected_mismatch(target, spec.expected_values, selected.workspace)
                try:
                    data = json.loads(Path(target).read_text())
                    if any(data.get(k) != v for k, v in spec.expected_values.items()):
                        return "completion expected values do not match current target"
                except (OSError, ValueError, AttributeError):
                    return "completion expected values unavailable"
    return None


_TEXT_EXPECTED_KEYS = {"content", "contains", "path"}


def text_expected_mismatch(target: str, expected: dict, workspace: Any) -> str | None:
    """Expected values of a non-JSON file, checked against the current file (t35).

    content: the whole text equals it (one trailing newline ignored); contains: the text includes
    it; path: names this same file. Any other key is not recognized and fails as such."""
    unknown = sorted(set(expected) - _TEXT_EXPECTED_KEYS)
    if unknown:
        return ("completion expected values not recognized for a text file: " + ", ".join(unknown)
                + " (accepted keys: content, contains, path)")
    if "path" in expected and canonical(str(expected["path"]), workspace) != target:
        return "completion expected path is a different file"
    try:
        text = Path(target).read_text()
    except (OSError, UnicodeDecodeError):
        return "completion expected values unavailable"
    if "content" in expected and text.rstrip("\n") != str(expected["content"]).rstrip("\n"):
        return "completion expected content does not match current target"
    if "contains" in expected and str(expected["contains"]) not in text:
        return "completion expected text is not in current target"
    return None
