"""CLI entry point for workflow stage-5 quality reports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from nanobot.workflow.quality import run_quality_report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m nanobot.workflow.quality_cli")
    parser.add_argument("--config", type=Path, help="YAML/JSON file with cases or report config")
    parser.add_argument("--output", type=Path, help="Optional report output path")
    args = parser.parse_args(argv)
    print(json.dumps(run_quality_report(args.config, output=args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
