"""Runtime evidence contracts. Document payloads never become control metadata."""
from __future__ import annotations

import hashlib
import json
import shlex
from contextvars import ContextVar
from pathlib import Path
from typing import Any

active_evidence_capture: ContextVar[bool] = ContextVar("context_evidence_capture", default=False)


def fingerprint(path: str | Path) -> dict | None:
    try:
        fp = Path(path)
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


def canonical(path: str, workspace: str | Path | None) -> str:
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
              "resource_id": None, "source_event_id": None}
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
    path = call.arguments.get("path")
    if path and not record["resource_id"]:
        record["resource_id"] = canonical(path, selected.workspace)
    if not record["read_only"] and record["resource_id"]:
        record["version"] = fingerprint(record["resource_id"])
    # Result text is turn-local evidence for semantic assessments, never telemetry.
    record["_text"] = str(result)
    return record


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
    import re
    if re.search(r"\b(?:tests?|pytest|unittest)\b|테스트", check, re.I):
        return "test_result"
    for spec in selected.design.completion_specs:
        if spec.check == check:
            return spec.result_kind
    return "semantic"


def validate_completion(check: str, records: list[dict], selected: Any) -> str | None:
    kind = completion_kind(check, selected)
    valid = [r for r in records if r.get("status") == "ok" and current(r)]
    if len(valid) != len(records) or not records:
        return "current successful execution evidence required"
    if kind == "test_result":
        if not any(r.get("result_kind") == "test_result" and r.get("exit_code") == 0 and r.get("tests_executed")
                   and r.get("checked_versions") and current(r) for r in valid):
            return "actual passing test execution against unchanged source versions required"
    elif kind != "semantic" and not any(r.get("result_kind") == kind and current(r) for r in valid):
        return "completion result kind/version does not match"
    for spec in selected.design.completion_specs:
        if spec.check == check and spec.target:
            target = canonical(spec.target, selected.workspace)
            if not any(r.get("resource_id") == target or target in r.get("checked_versions", {}) for r in valid):
                return "completion evidence targets a different resource"
            if spec.expected_values:
                try:
                    data = json.loads(Path(target).read_text())
                    if any(data.get(k) != v for k, v in spec.expected_values.items()):
                        return "completion expected values do not match current target"
                except (OSError, ValueError, AttributeError):
                    return "completion expected values unavailable"
    return None
