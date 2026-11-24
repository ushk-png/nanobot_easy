"""Generate the D19 reviewed memory summary ONCE with the existing refresher and the real model (stage 3c).

Usage: python -m scripts.real_eval.gen_memory_snapshot --out .local/eval/stage3c/d19_snapshot.json [--budget 3000]
The cost, time, attempts and review verdict of this one-off refresh are printed and written next to the snapshot.
Nothing from the operator workspace is read; the memory text is synthetic (cases_dev.d19_memory).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
import time
from pathlib import Path

from scripts.real_eval.cases_dev import d19_memory
from scripts.real_eval.harness import (
    BASELINE_PRESET,
    DEFAULT_CONFIG,
    Case,
    Env,
    build_config,
    build_loop,
)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--budget", type=int, default=3000)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    ws = Path(tempfile.mkdtemp(prefix="eval-snapshot-")).resolve()
    from nanobot.agent.memory import MemoryStore

    store = MemoryStore(ws)
    store.memory_file.parent.mkdir(parents=True, exist_ok=True)
    text = d19_memory()
    store.write_memory(text)
    cfg = build_config(Path(args.config), ws, BASELINE_PRESET, {"memory_prompt_tokens": args.budget})
    env = Env(300_000, 0)
    case = Case(id="SNAP", split="dev", types=[], title="", materials={}, turns=[], expected="", forbidden=[],
                judge_method="", judge=lambda r: None)
    loop = build_loop(cfg, case, env, None)
    refresher = loop.context.memory_prompt.refresher
    if refresher is None:
        raise SystemExit("no refresher attached (memory_prompt_auto_refresh / budget)")
    started = time.monotonic()
    await refresher._refresh(hashlib.sha256(text.encode("utf-8")).hexdigest())
    elapsed = time.monotonic() - started
    snap = store.memory_file.with_name("prompt_snapshot.json")
    result = {"status": refresher.status, "elapsed_s": round(elapsed, 1), "tokens_total": env.total_tokens,
              "model_calls": len(env.model_calls), "budget": args.budget,
              "snapshot_written": snap.is_file()}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if snap.is_file():
        out.write_bytes(snap.read_bytes())
    out.with_suffix(".cost.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if snap.is_file() else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
