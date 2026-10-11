"""Run the stepwise comparison and append one JSON line per run.

Configurations
- current_ops: layout compact, no first-step prefill, all approved procedures,
  no run budget (selection 3 calls, evaluation 5 calls as operated before).
- two_call:    layout two_call with routing prefill, conditional review,
  default run budget (120 s, 8 model calls).
- plain:       one model call with the same reasoning effort, no procedure.
- fine:        layout fine (7 steps) with routing, template procedures only
  (the S1-S7 setup before code selection existed), no run budget.

Usage: python scripts/stepwise_compare/run.py --config .local/config.genie.json \
    --configs current_ops,two_call,plain,fine --repeats 3 --out results.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from grade import grade  # noqa: E402

PLAIN_SYSTEM = "다음 질문에 한국어로 답한다. 결론과 그 이유를 간결하게 쓴다."


def _configs(template_ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    return {
        "current_ops": {"layout": "compact", "prefill": False, "procedures": None,
                        "time_budget_s": None, "call_budget": None},
        "two_call": {"layout": "two_call", "prefill": None, "procedures": None},
        "fine": {"layout": "fine", "prefill": False, "procedures": template_ids,
                 "time_budget_s": None, "call_budget": None},
    }


async def _run_plain(provider, model: str, case: dict[str, Any]) -> dict[str, Any]:
    from nanobot.providers.base import hosted_tools_disabled
    from nanobot.reasoning.bridge import STAGE_REASONING_EFFORT

    messages = [{"role": "system", "content": PLAIN_SYSTEM}, *case.get("conversation", []),
                {"role": "user", "content": case["question"]}]
    started = time.monotonic()
    with hosted_tools_disabled():
        response = await provider.chat_with_retry(
            messages=messages, tools=None, model=model, reasoning_effort=STAGE_REASONING_EFFORT
        )
    wall = time.monotonic() - started
    usage = response.usage if isinstance(response.usage, dict) else {}
    text = response.content or "" if response.finish_reason != "error" else ""
    return {
        "status": "completed" if text.strip() else "failed",
        "path": "plain",
        "procedure": None,
        "calls": 1,
        "wall_s": round(wall, 2),
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "text": text,
        "structured": None,
        "events": [],
    }


async def _run_pipeline(provider, model: str, case: dict[str, Any], options: dict[str, Any],
                        workspace: Path) -> dict[str, Any]:
    from nanobot.reasoning.bridge import run_reasoning_procedure, user_reply

    started = time.monotonic()
    result = await run_reasoning_procedure(
        provider,
        model=model,
        request=case["question"],
        workspace=workspace,
        conversation=case.get("conversation", []),
        **options,
    )
    wall = time.monotonic() - started
    selection = next(
        (o["selection"] for o in result.step_outputs.values() if isinstance(o.get("selection"), dict)), None
    )
    structured: dict[str, Any] = {}
    if selection is not None:
        structured["selected"] = selection.get("selected")
    if result.conclusion is not None:
        structured["conclusion_status"] = result.conclusion.status
    records = result.call_records
    text = user_reply(result) if result.status != "direct" else ""
    return {
        "status": result.status,
        "path": result.path,
        "procedure": result.procedure_id,
        "calls": len(records),
        "wall_s": round(wall, 2),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in records),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in records),
        "reasoning_tokens": sum(int(r.get("reasoning_tokens") or 0) for r in records),
        "text": text,
        "structured": structured or None,
        "events": [
            {k: v for k, v in item.items() if k in {"event", "stage", "reason", "reasons", "run", "kind"}}
            for item in result.history
            if item.get("event") in {"extra_call", "retry", "return", "review_decision", "template_fix",
                                     "template_answer", "prefilled", "open_issues", "verdict_unconfirmed",
                                     "budget_exceeded"}
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--configs", default="current_ops,two_call,plain,fine")
    parser.add_argument("--cases", default="")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    logger.remove()
    from nanobot.config.loader import load_config, set_config_path
    from nanobot.providers.factory import make_provider
    from nanobot.reasoning.registry import template_procedures

    config_path = Path(args.config).resolve()
    set_config_path(config_path)
    config = load_config(config_path)
    provider = make_provider(config)
    model = config.agents.defaults.model
    cases = json.loads((HERE / "cases.json").read_text())["cases"]
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [item for item in cases if item["id"] in wanted]
    names = args.configs.split(",")
    options = _configs(template_procedures())
    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    out = Path(args.out)

    for repeat in range(args.repeats):
        for case in cases:
            for name in names:
                try:
                    if name == "plain":
                        run = asyncio.run(_run_plain(provider, model, case))
                    else:
                        run = asyncio.run(_run_pipeline(provider, model, case, options[name], workspace))
                except Exception as exc:  # recorded as a failed run, never dropped
                    run = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300], "text": "",
                           "calls": None, "wall_s": None, "structured": None, "events": []}
                graded = grade(case, run.get("text", ""), run.get("structured"))
                line = {"config": name, "case": case["id"], "repeat": repeat, **run, **graded,
                        "text": (run.get("text") or "")[:600]}
                with out.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(line, ensure_ascii=False) + "\n")
                print(f"{name:12s} {case['id']:20s} r{repeat} {run.get('status'):12s} "
                      f"{graded['label']:10s} ok={graded['correct']} calls={run.get('calls')} "
                      f"t={run.get('wall_s')}", flush=True)


if __name__ == "__main__":
    main()
