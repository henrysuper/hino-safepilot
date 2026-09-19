"""CLI for aggregate-only Phase 3A label research."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pipeline import LabelResearchConfig, run_label_research


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hino-label-research")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--interim-root", type=Path, default=Path("data/interim"))
    parser.add_argument(
        "--analytics-root", type=Path, default=Path("data/processed/analytics")
    )
    parser.add_argument(
        "--output-root", type=Path, default=Path("data/processed/label_research")
    )
    args = parser.parse_args(argv)
    try:
        config = LabelResearchConfig.from_repo(
            args.repo_root, args.interim_root, args.analytics_root, args.output_root
        )
        print(json.dumps(run_label_research(config), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
