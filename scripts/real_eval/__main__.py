"""CLI: python -m scripts.real_eval <manifest|export|run|compare|seal|verify-seal> ... (see README.md)."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

from scripts.real_eval.harness import (
    BASELINE_PRESET,
    DEFAULT_CONFIG,
    collect_manifest,
    render_markdown,
    run_case,
    summarize,
)

HERE = Path(__file__).parent
SEAL = HERE / "FINAL_SEAL.sha256"


def load_cases(split: str):
    if split == "dev":
        from scripts.real_eval.cases_dev import CASES
    else:
        from scripts.real_eval.cases_final import CASES
    return CASES


def final_digest() -> str:
    return hashlib.sha256((HERE / "cases_final.py").read_bytes()).hexdigest()


def stage_spent(parent: Path, exclude: Path | None = None) -> tuple[int, dict[str, list[int]]]:
    """Tokens already spent by every run directory next to ``exclude`` (invalid and superseded ones too), per case."""
    total = 0
    per_case: dict[str, list[int]] = {}
    for runs in sorted(parent.glob("*/runs.jsonl")):
        if exclude is not None and runs.parent.resolve() == exclude.resolve():
            continue
        for line in runs.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            total += int(row.get("tokens_total") or 0)
            per_case.setdefault(row["case"], []).append(int(row.get("tokens_total") or 0))
    return total, per_case


def run_estimate(case_id: str, per_case: dict[str, list[int]], fallback: int) -> int:
    """Expected cost of one more run: the larger of the median and the max of earlier runs of the case, else the cap."""
    seen = per_case.get(case_id)
    if not seen:
        return fallback
    ordered = sorted(seen)
    return max(ordered[len(ordered) // 2], ordered[-1])


def parse_sets(items: list[str] | None) -> dict:
    """``--set stepwise.lightweight=true`` -> {"stepwise.lightweight": True} (in-memory only; no file is written)."""
    out: dict = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        out[key.strip()] = json.loads(raw) if raw.strip() in ("true", "false", "null") or raw.strip().isdigit() else raw
    return out


def case_dict(case, *, redact: bool = False) -> dict:
    if redact:
        return {"id": case.id, "split": case.split, "types": case.types, "reps": case.reps,
                "note": "sealed: contents are not exported"}
    return {"id": case.id, "split": case.split, "types": case.types, "title": case.title, "reps": case.reps,
            "materials": case.materials, "turns": case.turns, "seed_messages": len(case.seed_messages),
            "faults": [f.__dict__ for f in case.faults], "extra_tools": list(case.extra_tools),
            "context_window_tokens": case.context_window_tokens, "restart_after_turn": case.restart_after_turn,
            "consolidation_failures": case.consolidation_failures, "expected": case.expected,
            "forbidden": case.forbidden, "judge_method": case.judge_method}


def cmd_manifest(args) -> int:
    manifest = collect_manifest(Path(args.config), args.preset)
    text = json.dumps(manifest, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def cmd_export(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dev = [case_dict(c) for c in load_cases("dev")]
    final = [case_dict(c, redact=not args.include_final) for c in load_cases("final")]
    (out / "cases_dev.json").write_text(json.dumps(dev, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "cases_final_index.json").write_text(
        json.dumps({"seal_sha256": final_digest(), "cases": final}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(dev)} dev cases and {len(final)} final entries to {out}")
    return 0


def cmd_seal(args) -> int:
    SEAL.write_text(final_digest() + "\n", encoding="utf-8")
    print("sealed:", final_digest())
    return 0


def verify_seal() -> bool:
    return SEAL.is_file() and SEAL.read_text().strip() == final_digest()


async def _run(args) -> int:
    if args.split == "final":
        if not args.confirm_final:
            print("final cases need --confirm-final (they must not be used while developing)", file=sys.stderr)
            return 2
        if not verify_seal():
            print("FINAL_SEAL.sha256 does not match cases_final.py: the sealed cases changed", file=sys.stderr)
            return 2
    cases = load_cases(args.split)
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c.id in wanted]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    variant = parse_sets(args.set)
    runs_path = out / "runs.jsonl"
    done = set()
    runs: list[dict] = []
    if args.resume and runs_path.exists():
        for line in runs_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            runs.append(row)
            done.add((row["case"], row["rep"]))
    plan = [(c, r) for c in cases for r in range(1, (args.reps or c.reps) + 1) if (c.id, r) not in done]
    manifest = collect_manifest(Path(args.config), args.preset, {
        "run": {"split": args.split, "planned_runs": len(plan) + len(done), "resumed_runs": len(done),
                "token_cap_per_run": args.token_cap, "token_cap_total": args.total_cap,
                "timeout_per_turn_s": args.timeout, "reps_override": args.reps, "config_overrides": variant,
                "command": " ".join(sys.argv)}})
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    spent = sum(r["tokens_total"] for r in runs)
    aborted = None
    stage_total, per_case = (0, {})
    if args.stage_cap:
        stage_total, per_case = stage_spent(out.parent, out)
        fallback = args.estimate_per_run or args.token_cap
        reserved = sum(run_estimate(c.id, per_case, fallback) for c, _r in plan)
        print(f"stage spend so far {stage_total:,} + reserved for this run {reserved:,} (cap {args.stage_cap:,})",
              file=sys.stderr)
        if stage_total + spent + reserved > args.stage_cap:
            print("REFUSED: this run could exceed the stage token cap; lower the plan (cases/reps) or raise the cap "
                  "explicitly after deciding", file=sys.stderr)
            return 3
    with runs_path.open("a", encoding="utf-8") as sink:
        for case, rep in plan:
            if spent >= args.total_cap:
                aborted = f"total token cap {args.total_cap:,} reached before {case.id} rep {rep}"
                break
            if args.stage_cap and stage_total + spent + run_estimate(case.id, per_case, args.estimate_per_run or args.token_cap) > args.stage_cap:
                aborted = f"stage token cap {args.stage_cap:,} would be exceeded by {case.id} rep {rep}"
                break
            row = await run_case(case, rep, preset=args.preset, config_path=Path(args.config),
                                 timeout_s=args.timeout, token_cap=args.token_cap, keep=args.keep,
                                 variant_overrides=variant)
            runs.append(row)
            spent += row["tokens_total"]
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            sink.flush()
            print(f"{case.id} rep{rep}: {row['outcome']:13} tokens={row['tokens_total']:>7,} "
                  f"time={row['time_s']:>6.1f}s calls={row['model_calls']} tool_err={row['tool_errors']} "
                  f"critical={row['verdict']['critical']}", flush=True)
    summary = summarize(runs)
    summary["aborted"] = aborted
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "summary.md").write_text(render_markdown(summary, f"{args.split} 결과 ({out.name})"), encoding="utf-8")
    print(render_markdown(summary, f"{args.split} 결과"))
    if aborted:
        print("ABORTED:", aborted)
    return 0


def cmd_rescore(args) -> int:
    from scripts.real_eval.harness import rescore

    src = Path(args.dir)
    rows = [json.loads(line) for line in (src / "runs.jsonl").read_text(encoding="utf-8").splitlines()]
    out, notes = rescore(rows, load_cases(args.split))
    (src / "runs.rescored.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out) + "\n",
                                            encoding="utf-8")
    (src / "rescore_notes.json").write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = summarize(out)
    (src / "summary.rescored.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (src / "summary.rescored.md").write_text(render_markdown(summary, f"{args.split} 결과 - 판정 정정본"), encoding="utf-8")
    print(f"re-scored {sum(1 for r in out if 'rescored_from' in r)} runs; outcome changes: {notes}")
    return 0


def cmd_compare(args) -> int:
    def load(path: str) -> dict:
        return json.loads((Path(path) / "summary.json").read_text(encoding="utf-8"))

    a, b = load(args.before), load(args.after)
    lines = [f"# 비교: {args.before} → {args.after}", "",
             "| 사례 | 전 PASS/FAIL/NOT_EX | 후 PASS/FAIL/NOT_EX | 중앙 토큰 전→후 | 중앙 시간 전→후 | 치명 위반 전→후 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for cid in sorted(set(a["cases"]) | set(b["cases"])):
        x, y = a["cases"].get(cid), b["cases"].get(cid)
        fmt = lambda c: "-" if c is None else f"{c['PASS']}/{c['FAIL']}/{c['NOT_EXERCISED']}"  # noqa: E731
        tok = lambda c: "-" if c is None else f"{c['median_tokens']:,.0f}"  # noqa: E731
        sec = lambda c: "-" if c is None else f"{c['median_time_s']:.1f}"  # noqa: E731
        crit = lambda c: "-" if c is None else (sum(c["critical"].values()))  # noqa: E731
        lines.append(f"| {cid} | {fmt(x)} | {fmt(y)} | {tok(x)} → {tok(y)} | {sec(x)} → {sec(y)} | {crit(x)} → {crit(y)} |")
    ta, tb = a["totals"], b["totals"]
    lines += ["", f"전체 PASS {ta['PASS']} → {tb['PASS']} · FAIL {ta['FAIL']} → {tb['FAIL']} · "
              f"토큰 {ta['tokens']:,} → {tb['tokens']:,} · 시간 {ta['time_s']}s → {tb['time_s']}s", "",
              "치명 위반: " + ", ".join(f"{k} {a['critical_violations'][k]}→{b['critical_violations'][k]}"
                                    for k in a["critical_violations"])]
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.real_eval")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("manifest")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--preset", default=BASELINE_PRESET)
    p.add_argument("--out")
    p = sub.add_parser("export")
    p.add_argument("--out", required=True)
    p.add_argument("--include-final", action="store_true")
    sub.add_parser("seal")
    sub.add_parser("verify-seal")
    p = sub.add_parser("run")
    p.add_argument("--split", choices=("dev", "final"), default="dev")
    p.add_argument("--cases", help="comma-separated case ids")
    p.add_argument("--reps", type=int, help="override repetitions (smoke tests only)")
    p.add_argument("--out", required=True)
    p.add_argument("--preset", default=BASELINE_PRESET)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--token-cap", type=int, default=150_000, dest="token_cap")
    p.add_argument("--total-cap", type=int, default=1_500_000, dest="total_cap")
    p.add_argument("--estimate-per-run", type=int, default=None, dest="estimate_per_run",
                   help="planning estimate for a case with no earlier run (default: the per-run token cap)")
    p.add_argument("--stage-cap", type=int, default=None, dest="stage_cap",
                   help="cumulative cap over every run directory next to --out; the run is refused up front if its "
                        "estimated cost would pass it")
    p.add_argument("--timeout", type=float, default=240.0)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--keep", action="store_true")
    p.add_argument("--set", action="append", help="in-memory config override, e.g. stepwise.lightweight=true")
    p.add_argument("--confirm-final", action="store_true", dest="confirm_final")
    p = sub.add_parser("rescore")
    p.add_argument("dir")
    p.add_argument("--split", choices=("dev", "final"), default="dev")
    p = sub.add_parser("compare")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--out")
    args = parser.parse_args()
    if args.command == "manifest":
        return cmd_manifest(args)
    if args.command == "export":
        return cmd_export(args)
    if args.command == "seal":
        return cmd_seal(args)
    if args.command == "verify-seal":
        ok = verify_seal()
        print("seal ok" if ok else "SEAL MISMATCH")
        return 0 if ok else 1
    if args.command == "compare":
        return cmd_compare(args)
    if args.command == "rescore":
        return cmd_rescore(args)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
