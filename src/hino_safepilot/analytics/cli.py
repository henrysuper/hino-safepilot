"""Terminal prototype for deterministic analytics and optional AI wording."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hino_safepilot.ai.providers import provider_from_environment
from hino_safepilot.ai.service import ReportService

from .pipeline import build_analytics, load_daily_goal, load_trip_summary, resolve_config


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hino-analytics")
    subcommands = parser.add_subparsers(dest="command", required=True)
    build = subcommands.add_parser("build", help="Build deterministic Phase 2.5 outputs")
    build.add_argument("--repo-root", type=Path, default=Path.cwd())
    build.add_argument("--input-root", type=Path, default=Path("data/interim"))
    build.add_argument(
        "--output-root", type=Path, default=Path("data/processed/analytics")
    )
    build.add_argument(
        "--goal-config", type=Path, default=Path("config/behavior_goal_v0.json")
    )
    for name in ("trip-report", "daily-goal"):
        command = subcommands.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        command.add_argument("--trip-id", required=True)
        command.add_argument("--provider", choices=("mock", "gemini"), default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            manifest = build_analytics(
                resolve_config(
                    args.repo_root,
                    args.input_root,
                    args.output_root,
                    args.goal_config,
                )
            )
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return 0
        analytics_root = args.repo_root.resolve() / "data" / "processed" / "analytics"
        service = ReportService(provider_from_environment(args.provider))
        if args.command == "trip-report":
            summary = load_trip_summary(analytics_root / "trip_summaries.jsonl", args.trip_id)
            result = service.trip_report(summary)
            print(result.model_dump_json(indent=2))
            return 0
        goal = load_daily_goal(analytics_root / "improvement_goals.jsonl", args.trip_id)
        if not goal.target_metric:
            print(
                json.dumps(
                    {
                        "generation_mode": "deterministic_only",
                        "reason": "INSUFFICIENT_WEAKNESS_EVIDENCE",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(service.daily_goal(goal).model_dump_json(indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
