"""Stage/check/install core memory explicitly; never change MEMORY.md or runtime config.

Phrase checks are lexical only. Installation requires the SHA256 of the candidate
that an operator has separately reviewed for meaning, scope, corrections and authority.
Use private directories outside workspaces and Git checkouts. No command is automatic.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from nanobot.agent.memory_prompt import (
    _MAX_SNAPSHOT_BYTES,
    SNAPSHOT_NAME,
    BoundedMemoryPrompt,
    check_candidate,
    legacy_memory_tokens,
    projection_tokens,
    snapshot_summary,
)
from nanobot.agent.memory_prompt_refresh import MemoryPromptRefresher

_MAX_FILE_BYTES = 512_000


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("input must be a regular file, not a symlink")
    with path.open("rb") as stream:
        raw = stream.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError("input file is too large")
    return raw


def _source_text(path: Path) -> str:
    # Match MemoryStore.read_memory()/Path.read_text() on CRLF and bare-CR sources.
    return _read(path).decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _paths(workspace: str) -> tuple[Path, Path, Path]:
    root = Path(workspace).expanduser().resolve()
    memory = root / "memory"
    for path in (memory, memory / "MEMORY.md", memory / SNAPSHOT_NAME,
                 memory / ".prompt_snapshot.lock"):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("memory paths must stay inside the workspace without symlinks")
    return root, memory / "MEMORY.md", memory / SNAPSHOT_NAME


def _private_location(path: str, root: Path) -> Path:
    candidate = Path(path).expanduser().absolute()
    if candidate.is_symlink():
        raise ValueError("private directory must not be a symlink")
    candidate = candidate.resolve()
    if candidate.is_relative_to(root) or root.is_relative_to(candidate):
        raise ValueError("private directory must not overlap the workspace")
    if any((parent / ".git").exists() for parent in (candidate, *candidate.parents)):
        raise ValueError("private directory must be outside Git checkouts")
    return candidate


def _fsync_directory(path: Path) -> None:
    if os.name != "nt":
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _write_new(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def inspect(workspace: str, config: str | None = None) -> dict:
    root, source, snapshot = _paths(workspace)
    text = _source_text(source)
    row: dict = {"workspace": str(root), "source_sha256": _sha(text),
                 "raw_tokens": legacy_memory_tokens(text)}
    if config:
        defaults = json.loads(_read(Path(config)).decode("utf-8")).get("agents", {}).get("defaults", {})
        row["memoryPromptTokens"] = defaults.get("memoryPromptTokens", defaults.get("memory_prompt_tokens", 0))
        row["memoryPromptAutoRefresh"] = defaults.get("memoryPromptAutoRefresh", defaults.get("memory_prompt_auto_refresh", False))
    if snapshot.exists():
        try:
            raw = _read(snapshot)
            data = json.loads(raw)
            row["snapshot_tokens"] = projection_tokens(snapshot_summary(data), source)
            row["snapshot_matches_source"] = data.get("source_sha256") == _sha(text)
            row["snapshot_sha256"] = hashlib.sha256(raw).hexdigest()
        except (ValueError, TypeError, RecursionError):
            row["snapshot_unreadable"] = True
    if "memoryPromptTokens" in row:
        loader = BoundedMemoryPrompt(source, int(row["memoryPromptTokens"] or 0))
        row["prompt_uses"] = "snapshot" if loader.build(text) else (
            "raw (budget off)" if loader.budget <= 0 else f"raw fallback: {loader.status.get('reason')}")
    return row


class _StagedRefresher(MemoryPromptRefresher):
    """Keep bounded diagnostic responses only in the explicitly private operator stage."""

    async def _ask(self, provider, model, template, payload, max_tokens, purpose=None):
        response = await super()._ask(provider, model, template, payload, max_tokens, purpose)
        count = len(list(self.workspace.glob("attempt-*.json"))) + 1
        record = {"purpose": purpose, "response": response}
        raw = json.dumps(record, ensure_ascii=False).encode("utf-8")
        if len(raw) > _MAX_FILE_BYTES:
            record = {"purpose": purpose, "response_sha256": _sha(response),
                      "omitted": "response exceeds private diagnostic size limit"}
            raw = json.dumps(record).encode("utf-8")
        _write_new(self.workspace / f"attempt-{count}.json", raw)
        return response


async def generate(workspace: str, staging: str, budget: int, provider, model: str, *,
                   reuse_snapshot: bool = False) -> dict:
    """Generate/review on a new private copy; never reuse or unlink a previous stage."""
    if type(budget) is not int or not 1 <= budget <= 20_000:
        raise ValueError("budget must be between 1 and 20000")
    root, source, snapshot = _paths(workspace)
    stage = _private_location(staging, root)
    text = _source_text(source)
    previous = _read(snapshot) if reuse_snapshot and snapshot.exists() else None
    if previous is not None:
        snapshot_summary(json.loads(previous))  # Reject malformed input before creating the stage.
    stage.mkdir(parents=True, exist_ok=False, mode=0o700)
    (stage / "memory").mkdir(mode=0o700)
    staged = stage / "memory/MEMORY.md"
    _write_new(staged, text.encode("utf-8"))
    if previous is not None:
        _write_new(stage / "memory" / SNAPSHOT_NAME, previous)
    refresher = _StagedRefresher(staged, budget, lambda: (provider, model))
    await refresher._refresh(_sha(text))
    produced = stage / "memory" / SNAPSHOT_NAME
    current = (produced.is_file() and refresher.status.get("status") in {"published", "current"}
               and json.loads(_read(produced)).get("source_sha256") == _sha(text))
    manifest = {"workspace": str(root), "source_sha256": _sha(text), "budget": budget,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "candidate_sha256": hashlib.sha256(_read(produced)).hexdigest() if current else None,
                "refresh_status": refresher.status}
    _write_new(stage / "manifest.json", json.dumps(manifest).encode("utf-8"))
    return {"candidate": bool(current), "staging": str(stage),
            "candidate_sha256": manifest["candidate_sha256"], "semantic_review_required": True,
            "refresh_status": refresher.status}


def _checked_candidate(root: Path, text: str, source: Path, stage: Path,
                       budget: int, conditions: str) -> tuple[bytes, list[str]]:
    """Read once and validate these exact bytes; installation must not reread the stage."""
    candidate = stage / "memory" / SNAPSHOT_NAME
    for path in (stage / "memory", candidate, stage / "manifest.json"):
        if path.is_symlink() or not path.resolve().is_relative_to(stage):
            return b"", ["candidate paths must stay inside the staging directory"]
    manifest = json.loads(_read(stage / "manifest.json"))
    cond = json.loads(_read(Path(conditions).expanduser()))
    if not isinstance(manifest, dict) or not isinstance(cond, dict):
        return b"", ["manifest and conditions must be JSON objects"]
    problems = []
    for record, label in ((manifest, "candidate"), (cond, "conditions")):
        workspace = record.get("workspace")
        if not isinstance(workspace, str) or Path(workspace).expanduser().resolve() != root:
            problems.append(f"{label} is for another workspace")
        if record.get("source_sha256") != _sha(text):
            problems.append(f"{label} is for another source version; review again")
    if not candidate.is_file():
        return b"", problems + ["no candidate (generation/review failed or was rejected)"]
    raw = _read(candidate)
    if len(raw) > _MAX_SNAPSHOT_BYTES:
        problems.append("candidate exceeds the runtime snapshot file limit")
    if manifest.get("candidate_sha256") != hashlib.sha256(raw).hexdigest():
        problems.append("candidate changed since generation; review again")
    problems += check_candidate(text, raw, source, budget,
                                required=cond.get("required"), revoked=cond.get("revoked"))
    return raw, problems


def check(workspace: str, staging: str, budget: int, conditions: str) -> list[str]:
    try:
        root, source, _ = _paths(workspace)
        stage = _private_location(staging, root)
        _, problems = _checked_candidate(root, _source_text(source), source,
                                         stage, budget, conditions)
        return problems
    except (OSError, ValueError, TypeError, RecursionError):
        return ["unreadable or invalid transition inputs"]


def _backup(snapshot: Path, keep: Path) -> None:
    if keep.is_symlink():
        raise ValueError("backup directory must not be a symlink")
    keep.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(keep, 0o700)
    fd, name = tempfile.mkstemp(prefix="prompt_snapshot.", suffix=".json", dir=keep)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_read(snapshot))
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(keep)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def install(workspace: str, staging: str, budget_now: int, conditions: str, backup: str, *,
            reviewed_sha256: str | None = None) -> dict:
    """Publish validated bytes under the existing lock after operator semantic review."""
    try:
        root, source, snapshot = _paths(workspace)
        stage = _private_location(staging, root)
        keep = _private_location(backup, root)
        if keep.is_relative_to(stage) or stage.is_relative_to(keep):
            return {"installed": False, "problems": ["backup must not overlap staging"]}
        with FileLock(source.with_name(".prompt_snapshot.lock"), timeout=0, mode=0o600, thread_local=False):
            text = _source_text(source)
            raw, problems = _checked_candidate(root, text, source, stage, budget_now, conditions)
            if (not isinstance(reviewed_sha256, str) or not reviewed_sha256
                    or hashlib.sha256(raw).hexdigest() != reviewed_sha256):
                problems.append("semantic review of these exact candidate bytes is required")
            if problems:
                return {"installed": False, "problems": problems}
            before = _read(snapshot) if snapshot.exists() else None
            if before is not None:
                _backup(snapshot, keep)
            fd, name = tempfile.mkstemp(prefix=".memory-prompt-", dir=snapshot.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Uncooperative writers do not hold this lock; recheck both source and destination.
                _paths(workspace)
                current = _read(snapshot) if snapshot.exists() else None
                if _source_text(source) != text or current != before:
                    return {"installed": False, "problems": ["source or snapshot changed during install"]}
                os.replace(temporary, snapshot)
                _fsync_directory(snapshot.parent)
            finally:
                temporary.unlink(missing_ok=True)
    except Timeout:
        return {"installed": False, "problems": ["a refresh holds the snapshot lock; retry later"]}
    except (OSError, ValueError, TypeError, RecursionError):
        return {"installed": False, "problems": ["unreadable or invalid transition inputs"]}
    loader = BoundedMemoryPrompt(source, budget_now)
    active = loader.build(source.read_text(encoding="utf-8")) is not None
    return {"installed": True, "active_under_current_budget": active, "status": loader.status}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect", "generate", "check", "install"])
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--config")
    parser.add_argument("--staging")
    parser.add_argument("--budget", type=int)
    parser.add_argument("--budget-now", type=int)
    parser.add_argument("--conditions")
    parser.add_argument("--backup")
    parser.add_argument("--reviewed-sha256", help="SHA256 of the candidate separately reviewed for meaning")
    parser.add_argument("--reuse-snapshot", action="store_true",
                        help="Copy the existing snapshot into the private stage for a full-source-audited update")
    args = parser.parse_args(argv)
    required = {"inspect": [], "generate": ["staging", "budget", "config"],
                "check": ["staging", "budget", "conditions"],
                "install": ["staging", "budget_now", "conditions", "backup", "reviewed_sha256"]}
    for name in required[args.command]:
        if getattr(args, name) is None:
            parser.error("missing --" + name.replace("_", "-"))
    try:
        success = True
        if args.command == "inspect":
            result: object = inspect(args.workspace, args.config)
        elif args.command == "generate":
            from nanobot.config.loader import load_config
            from nanobot.providers.factory import make_provider

            config = load_config(Path(args.config))
            provider = make_provider(config)
            result = asyncio.run(generate(args.workspace, args.staging, args.budget, provider,
                                          provider.get_default_model(), reuse_snapshot=args.reuse_snapshot))
            success = result["candidate"]
        elif args.command == "check":
            problems = check(args.workspace, args.staging, args.budget, args.conditions)
            result = {"problems": problems, "semantic_review_required": True,
                      "notice": "Phrase checks do not establish meaning or authorize installation."}
            success = not problems
        else:
            result = install(args.workspace, args.staging, args.budget_now, args.conditions, args.backup,
                             reviewed_sha256=args.reviewed_sha256)
            success = result["installed"]
    except (OSError, ValueError, TypeError, RecursionError):
        result, success = {"error": "invalid inputs or unavailable private files"}, False
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0 if success else 2


if __name__ == "__main__":
    sys.exit(main())
