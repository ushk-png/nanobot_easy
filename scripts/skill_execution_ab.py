#!/usr/bin/env python3
"""Run Execution A/B checks for skill Method value.

For each routing case, generate:
  A. an answer using the target skill Method
  B. an answer using ordinary reasoning without the skill
Then ask the configured LLM judge to score accuracy, structure, and omissions.

This is an operational validation script, not a registration gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nanobot.agent.skills import _STRIP_SKILL_FRONTMATTER  # noqa: E402
from nanobot.config.loader import load_config  # noqa: E402
from nanobot.providers.factory import make_provider  # noqa: E402
from nanobot.skill_store import SkillStore  # noqa: E402


def _load_cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cases", [])
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list or {{cases: [...]}}")
    return [item for item in data if isinstance(item, dict)]


def _case_files(workspace: Path) -> list[Path]:
    roots = [workspace / "skills", ROOT / "nanobot" / "skills"]
    seen: set[Path] = set()
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*/routing_cases.json")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)
    return files


def _method_from_skill(path: Path) -> str:
    skill_file = path if path.name == "SKILL.md" else path / "SKILL.md"
    markdown = skill_file.read_text(encoding="utf-8")
    body = _STRIP_SKILL_FRONTMATTER.sub("", markdown, count=1).strip()
    match = re.search(r"(?ims)^##\s+Method\s*$([\s\S]*?)(?=^##\s+|\Z)", body)
    return (match.group(1).strip() if match else body).strip()


def _json_from_text(text: str | None) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:-1]).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": text, "error": "judge did not return JSON"}
    return data if isinstance(data, dict) else {"raw": data, "error": "judge JSON was not an object"}


async def _ask(provider, model: str, prompt: str) -> str:
    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": prompt}],
        model=model,
        max_tokens=2000,
        temperature=0.1,
    )
    return response.content or ""


async def _run_case(provider, model: str, skill_name: str, method: str, query: str) -> dict[str, Any]:
    skill_prompt = (
        "Answer the user request by following this skill Method exactly where applicable.\n\n"
        f"# Skill: {skill_name}\n\n## Method\n{method}\n\n"
        f"# User request\n{query}"
    )
    baseline_prompt = (
        "Answer the user request using ordinary reasoning. Do not use any hidden skill Method "
        "or named skill procedure.\n\n"
        f"# User request\n{query}"
    )
    skill_answer, baseline_answer = await asyncio.gather(
        _ask(provider, model, skill_prompt),
        _ask(provider, model, baseline_prompt),
    )
    judge_prompt = f"""
You are judging whether a named skill materially improves an answer.
Score both answers from 1 to 5 on:
- accuracy
- structure
- omissions (5 means few/no important omissions)

Return JSON only:
{{
  "skill_scores": {{"accuracy": 1, "structure": 1, "omissions": 1}},
  "baseline_scores": {{"accuracy": 1, "structure": 1, "omissions": 1}},
  "winner": "skill|baseline|tie",
  "reason": "short reason"
}}

# User request
{query}

# Answer A: skill Method
{skill_answer}

# Answer B: ordinary reasoning
{baseline_answer}
""".strip()
    judge = _json_from_text(await _ask(provider, model, judge_prompt))
    return {
        "skill": skill_name,
        "query": query,
        "skill_answer": skill_answer,
        "baseline_answer": baseline_answer,
        "judge": judge,
    }


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Run skill Execution A/B validation.")
    parser.add_argument("--config", default=None, help="Config JSON path")
    parser.add_argument("--workspace", default=None, help="Workspace directory override")
    parser.add_argument("--skill", default=None, help="Only run one skill name")
    parser.add_argument("--limit", type=int, default=0, help="Maximum cases to run; 0 means all")
    parser.add_argument("--output", default=None, help="Optional JSON report path")
    args = parser.parse_args()

    config = load_config(Path(args.config).expanduser() if args.config else None)
    if args.workspace:
        config.agents.defaults.workspace = str(Path(args.workspace).expanduser())
    provider = make_provider(config)
    model = config.resolve_preset().model
    workspace = config.workspace_path
    store = SkillStore(workspace)
    rows = {str(row["name"]): row for row in store.list_skills(include_deprecated=False)}

    jobs: list[tuple[str, str, str]] = []
    for path in _case_files(workspace):
        skill_name = path.parent.name
        if args.skill and skill_name != args.skill:
            continue
        row = rows.get(skill_name)
        if row is None:
            continue
        method = _method_from_skill(Path(str(row["path"])))
        for case in _load_cases(path):
            query = str(case.get("query") or "").strip()
            expected = str(case.get("expected") or case.get("skill") or "").strip()
            if query and (not expected or expected == skill_name):
                jobs.append((skill_name, method, query))
    if args.limit > 0:
        jobs = jobs[: args.limit]
    if not jobs:
        print("No matching routing cases found.")
        return 1

    results: list[dict[str, Any]] = []
    for idx, (skill_name, method, query) in enumerate(jobs, start=1):
        print(f"[{idx}/{len(jobs)}] {skill_name}: {query[:80]}")
        results.append(await _run_case(provider, model, skill_name, method, query))

    summary: dict[str, dict[str, int]] = {}
    for item in results:
        skill = item["skill"]
        winner = str(item.get("judge", {}).get("winner") or "unknown")
        summary.setdefault(skill, {"skill": 0, "baseline": 0, "tie": 0, "unknown": 0})
        summary[skill][winner if winner in summary[skill] else "unknown"] += 1

    report = {"summary": summary, "results": results}
    print(json.dumps({"summary": summary}, ensure_ascii=False, indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
