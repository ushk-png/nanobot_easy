"""Summarize comparison results (one or more JSONL files) as Markdown tables."""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ORDER = ["current_ops", "two_call", "plain", "fine"]


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]  # nearest rank


def main(paths: list[str]) -> None:
    rows = [json.loads(line) for path in paths for line in Path(path).read_text().splitlines() if line.strip()]
    by_config: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_config[row["config"]].append(row)

    print("| 구성 | 실행 | 정답 | 필수 조건 위반 | 미확인→확정 표현 | 완료 | 결과·답변 일치 | 이유 완전(선정) | 시간 중앙값 | 시간 p95(참고) | 호출 평균 | 총 토큰 평균 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name in [item for item in ORDER if item in by_config]:
        items = by_config[name]
        walls = [row["wall_s"] for row in items if row.get("wall_s") is not None]
        tokens = [row["input_tokens"] + row["output_tokens"] for row in items if row.get("input_tokens") is not None]
        calls = [row["calls"] for row in items if row.get("calls") is not None]
        consistent = [row["consistent"] for row in items if row.get("consistent") is not None]
        reasons = [row["reasons_complete"] for row in items if row.get("reasons_complete") is not None]
        print(
            f"| {name} | {len(items)} | {sum(r['correct'] for r in items)}/{len(items)} "
            f"| {sum(bool(r['required_violation']) for r in items)} "
            f"| {sum(bool(r['unknown_as_confirmed']) for r in items)} "
            f"| {sum(r['status'] == 'completed' for r in items)}/{len(items)} "
            f"| {f'{sum(consistent)}/{len(consistent)}' if consistent else '-'} "
            f"| {f'{sum(reasons)}/{len(reasons)}' if reasons else '-'} "
            f"| {statistics.median(walls):.1f}초 | {_p95(walls):.1f}초 "
            f"| {statistics.mean(calls):.1f} | {statistics.mean(tokens):,.0f} |"
        )

    print("\n| 질문 | " + " | ".join(item for item in ORDER if item in by_config) + " |")
    print("|---|" + "---|" * len([item for item in ORDER if item in by_config]))
    cases = sorted({row["case"] for row in rows}, key=lambda c: [r["case"] for r in rows].index(c))
    for case in cases:
        cells = []
        for name in [item for item in ORDER if item in by_config]:
            items = [row for row in by_config[name] if row["case"] == case]
            walls = [row["wall_s"] for row in items if row.get("wall_s") is not None]
            labels = ",".join(row["label"] for row in items)
            cells.append(
                f"{sum(r['correct'] for r in items)}/{len(items)} ({labels}) "
                f"{statistics.median(walls):.0f}초" if walls else "-"
            )
        print(f"| {case} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main(sys.argv[1:])
