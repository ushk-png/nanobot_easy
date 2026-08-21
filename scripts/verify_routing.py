#!/usr/bin/env python3
"""Run all per-skill routing_cases.json files and report 90% acceptance."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nanobot.agent.skills import SYSTEM_SKILLS_DIR  # noqa: E402
from nanobot.config.loader import load_config  # noqa: E402
from nanobot.skill_embeddings import embed_skill_query, make_skill_embedding_fn  # noqa: E402
from nanobot.skill_store import SkillStore  # noqa: E402

ACCEPTANCE_THRESHOLD = 0.90


@dataclass(frozen=True)
class _RoutingResult:
    passed: int
    total: int
    accuracy: float


def _load_cases(path: Path) -> list[dict[str, object]]:
    raw = path.read_text(encoding="utf-8")
    data: Any = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("cases", [])
    if not isinstance(data, list):
        raise ValueError(f"{path}: routing cases must be a list or {{cases: [...]}}")
    return [item for item in data if isinstance(item, dict)]


def _case_file_for_skill(row: Any) -> Path:
    return Path(str(row["path"])).parent / "routing_cases.json"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify all routing_cases.json files against nanobot skill test-routing "
            "criteria. Acceptance: per-skill and total accuracy must be >= 90%."
        )
    )
    parser.add_argument("--config", default=None, help="Config JSON path; enables skills.embedding when set")
    parser.add_argument("--workspace", default=".local/workspace", help="Workspace directory")
    parser.add_argument("--top-k", type=int, default=3, help="Candidates to inspect per case")
    args = parser.parse_args()

    config = load_config(Path(args.config).expanduser()) if args.config else None
    workspace = Path(args.workspace).expanduser()
    if config is not None and args.workspace == ".local/workspace":
        workspace = config.workspace_path
    store = SkillStore(workspace)
    if config is not None and (embedding := make_skill_embedding_fn(config)) is not None:
        embedding_fn, embedding_model, dimensions = embedding
        store.reindex(
            system_dir=SYSTEM_SKILLS_DIR,
            embedding_fn=embedding_fn,
            embedding_model=embedding_model,
            embedding_dimensions=dimensions,
        )
    else:
        store.ensure_index(system_dir=SYSTEM_SKILLS_DIR)

    rows = store.list_skills(include_deprecated=False)
    results: list[tuple[str, int, int, float, Path]] = []
    missing: list[str] = []
    total_passed = 0
    total_cases = 0

    for row in rows:
        case_file = _case_file_for_skill(row)
        if not case_file.is_file():
            missing.append(str(row["name"]))
            continue
        cases = _load_cases(case_file)
        if not cases:
            missing.append(str(row["name"]))
            continue
        if config is None or not config.skills.embedding.model:
            result = store.run_routing_test(cases, top_k=max(1, args.top_k))
        else:
            passed = 0
            for case in cases:
                query = str(case.get("query") or "").strip()
                expected = str(case.get("expected") or case.get("skill") or "").strip()
                query_vector = asyncio.run(embed_skill_query(config, query, store=store))
                matches = store.search(
                    query,
                    top_k=max(1, args.top_k),
                    query_vector=query_vector,
                )
                actual = matches[0].name if matches else ""
                passed += 1 if actual == expected else 0
            result = _RoutingResult(
                passed=passed,
                total=len(cases),
                accuracy=(passed / len(cases)) if cases else 0.0,
            )
        results.append((str(row["name"]), result.passed, result.total, result.accuracy, case_file))
        total_passed += result.passed
        total_cases += result.total

    if not results:
        print("No routing_cases.json files found.")
        return 1

    print("Skill routing verification")
    print(f"Acceptance threshold: {_fmt_pct(ACCEPTANCE_THRESHOLD)}")
    print()
    print(f"{'Skill':<32} {'Passed':>7} {'Total':>7} {'Accuracy':>9}  Status")
    print("-" * 68)
    failing = 0
    for name, passed, total, accuracy, _path in sorted(results, key=lambda item: item[0]):
        status = "OK" if accuracy >= ACCEPTANCE_THRESHOLD else "FAIL <90%"
        if accuracy < ACCEPTANCE_THRESHOLD:
            failing += 1
        print(f"{name:<32} {passed:>7} {total:>7} {_fmt_pct(accuracy):>9}  {status}")

    total_accuracy = total_passed / total_cases if total_cases else 0.0
    print("-" * 68)
    print(f"{'TOTAL':<32} {total_passed:>7} {total_cases:>7} {_fmt_pct(total_accuracy):>9}")

    if missing:
        print()
        print(f"Missing routing cases ({len(missing)} skill(s)):")
        for name in sorted(missing):
            print(f"- {name}")

    if failing or total_accuracy < ACCEPTANCE_THRESHOLD:
        print()
        print("Routing acceptance failed: one or more rates are below 90%.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
