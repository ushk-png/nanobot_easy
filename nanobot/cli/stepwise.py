"""Apply, remove and list stepwise reasoning ("Genie method") per agent config.

The method is turned on only by an explicit apply, which writes a named
``agents.defaults.stepwise`` block with the date into that agent's config and
keeps a backup. ``status`` lists every agent config and what is applied.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nanobot.config.schema import Config

METHOD = "genie"
LAYOUTS = ("auto", "two_call", "compact", "fine")


def apply_stepwise(config_path: Path, *, layout: str = "auto", now: datetime | None = None) -> dict[str, Any]:
    """Turn the method on for one agent config. Returns what was written and the backup."""

    if layout not in LAYOUTS:
        raise ValueError(f"layout must be one of {', '.join(LAYOUTS)}")
    stamp = (now or datetime.now(timezone.utc)).astimezone()

    def change(block: dict[str, Any]) -> None:
        block.update(
            {"enabled": True, "method": METHOD, "appliedAt": stamp.isoformat(timespec="seconds"), "layout": layout}
        )
        block.pop("removedAt", None)

    return _update(config_path, change, stamp)


def remove_stepwise(config_path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Turn the method off for one agent config; the record of when stays."""

    stamp = (now or datetime.now(timezone.utc)).astimezone()

    def change(block: dict[str, Any]) -> None:
        block.update({"enabled": False, "removedAt": stamp.isoformat(timespec="seconds")})

    return _update(config_path, change, stamp)


def stepwise_status(config_paths: list[Path]) -> list[dict[str, Any]]:
    """One row per agent config: agent name, applied or not, when, layout, model."""

    rows = []
    for path in config_paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            rows.append({"agent": agent_name(path), "config": str(path), "error": type(exc).__name__})
            continue
        defaults = (raw.get("agents") or {}).get("defaults") or {}
        block = defaults.get("stepwise") or {}
        effort = defaults.get("reasoningEffort", defaults.get("reasoning_effort"))
        rows.append(
            {
                "agent": agent_name(path),
                "config": str(path),
                "enabled": bool(block.get("enabled")),
                "method": block.get("method", ""),
                "applied_at": block.get("appliedAt", block.get("applied_at")),
                "removed_at": block.get("removedAt"),
                "layout": block.get("layout", "auto"),
                "model": defaults.get("model", ""),
                # The old implicit switch; reported so it is not mistaken for an applied method.
                "legacy_none": isinstance(effort, str) and effort.lower() == "none" and not block.get("enabled"),
            }
        )
    return rows


def find_agent_configs(directory: Path) -> list[Path]:
    """Agent configs in a directory: config.json and config.<name>.json, no backups."""

    return sorted(
        path
        for path in directory.glob("config*.json")
        if path.is_file() and (path.name == "config.json" or path.name.count(".") == 2)
    )


def agent_name(path: Path) -> str:
    parts = path.name.split(".")
    return parts[1] if len(parts) == 3 else "default"


def _update(config_path: Path, change, stamp: datetime) -> dict[str, Any]:
    path = config_path.expanduser().resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    defaults = raw.setdefault("agents", {}).setdefault("defaults", {})
    block = dict(defaults.get("stepwise") or {})
    change(block)
    defaults["stepwise"] = block
    Config.model_validate(raw)  # never write a config that would not load
    backup = path.with_name(f"{path.name}.bak-stepwise-{stamp.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"agent": agent_name(path), "config": str(path), "backup": str(backup), **block}
