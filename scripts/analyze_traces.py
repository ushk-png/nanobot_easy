#!/usr/bin/env python3
"""Analyze skill trace acceptance signals from skillstore.db."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nanobot.skill_store import skillstore_path  # noqa: E402


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _hot_path_skills(config: dict[str, Any]) -> set[str]:
    defaults = config.get("agents", {}).get("defaults", {})
    hot = set(str(item) for item in defaults.get("skills", []) if item)
    profiles = defaults.get("subagentProfiles") or defaults.get("subagent_profiles") or {}
    if isinstance(profiles, dict):
        for profile in profiles.values():
            if isinstance(profile, dict):
                hot.update(str(item) for item in profile.get("skills", []) if item)
    return hot


def _print_rows(title: str, rows: list[sqlite3.Row], columns: list[str]) -> None:
    print(title)
    if not rows:
        print("  none")
        print()
        return
    widths = {
        column: max(len(column), *(len(str(row[column] if row[column] is not None else "")) for row in rows))
        for column in columns
    }
    print("  " + "  ".join(column.ljust(widths[column]) for column in columns))
    print("  " + "  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  " + "  ".join(str(row[column] if row[column] is not None else "").ljust(widths[column]) for column in columns))
    print()


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze skillstore traces for 9장/10장 acceptance signals: "
            "unused verified skills, Hot Path skills selected via cold search, "
            "and requires_exec skills executed by main."
        )
    )
    parser.add_argument("--workspace", default=".local/workspace", help="Workspace directory")
    parser.add_argument("--db", help="Explicit skillstore.db path")
    parser.add_argument("--config", default=".local/config.json", help="Config JSON path for profile skills")
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser()
    db_path = Path(args.db).expanduser() if args.db else skillstore_path(workspace)
    if not db_path.is_file():
        print(f"skillstore db not found: {db_path}", file=sys.stderr)
        return 2

    config = _load_json(Path(args.config).expanduser() if args.config else None)
    hot_skills = _hot_path_skills(config)

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        unused_verified = conn.execute(
            """
            SELECT s.name, s.category, s.source
            FROM skills s
            LEFT JOIN traces t ON t.selected_skill = s.name
            WHERE s.status = 'verified'
            GROUP BY s.name
            HAVING COUNT(t.trace_id) = 0
            ORDER BY s.name
            """
        ).fetchall()

        if hot_skills:
            placeholders = ",".join("?" for _ in hot_skills)
            hot_cold = conn.execute(
                f"""
                SELECT selected_skill AS name, COUNT(*) AS cold_count
                FROM traces
                WHERE selected_skill IN ({placeholders})
                  AND selection_reason = 'cold'
                GROUP BY selected_skill
                ORDER BY cold_count DESC, selected_skill
                """,
                tuple(sorted(hot_skills)),
            ).fetchall()
        else:
            hot_cold = []

        exec_main = conn.execute(
            """
            SELECT t.selected_skill AS name, COUNT(*) AS main_count
            FROM traces t
            JOIN skills s ON s.name = t.selected_skill
            WHERE s.requires_exec = 1
              AND t.executed_by = 'main'
            GROUP BY t.selected_skill
            ORDER BY main_count DESC, t.selected_skill
            """
        ).fetchall()

        duration_rows: list[sqlite3.Row] = []
        if _has_column(conn, "traces", "duration_ms"):
            duration_rows = conn.execute(
                """
                SELECT
                    selection_reason AS phase,
                    COUNT(*) AS count,
                    ROUND(AVG(duration_ms), 1) AS avg_ms,
                    MAX(duration_ms) AS max_ms
                FROM traces
                WHERE duration_ms IS NOT NULL
                GROUP BY selection_reason
                ORDER BY avg_ms DESC, phase
                """
            ).fetchall()

    print(f"Trace analysis: {db_path}")
    print(f"Hot Path skills from config: {', '.join(sorted(hot_skills)) if hot_skills else '(none configured)'}")
    print()
    _print_rows("(a) verified but selected 0 times", list(unused_verified), ["name", "category", "source"])
    _print_rows("(b) Hot Path skill selected via cold search", list(hot_cold), ["name", "cold_count"])
    _print_rows("(c) requires_exec=true skill executed_by main", list(exec_main), ["name", "main_count"])
    _print_rows("(d) trace duration by phase", list(duration_rows), ["phase", "count", "avg_ms", "max_ms"])
    return 1 if hot_cold or exec_main else 0


if __name__ == "__main__":
    raise SystemExit(main())
