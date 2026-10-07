"""Terminal prototype for deterministic analytics and optional AI wording."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hino_safepilot.ai.providers import provider_from_environment
from hino_safepilot.ai.service import ReportService

from .pipeline import build_analytics, load_daily_goal, load_trip_summary, resolve_config
from .score_pipeline import (
    build_driver_safety,
    build_driver_views,
    build_safety_scores,
    load_predrive_briefing,
    load_trip_score,
    resolve_score_config,
)


def _score_parsers(subcommands: argparse._SubParsersAction) -> None:
    score = subcommands.add_parser("score", help="Build trip safety scores and pre-drive briefings")
    score.add_argument("--repo-root", type=Path, default=Path.cwd())
    score.add_argument("--analytics-root", type=Path, default=Path("data/processed/analytics"))
    score.add_argument("--output-root", type=Path, default=Path("data/processed/safety_scores"))
    score.add_argument("--score-config", type=Path, default=Path("config/safety_score_v1.json"))
    for name in ("predrive", "postdrive"):
        command = subcommands.add_parser(name)
        command.add_argument("--repo-root", type=Path, default=Path.cwd())
        command.add_argument("--trip-id", required=True)
        command.add_argument("--provider", choices=("mock", "gemini"), default=None)
    road = subcommands.add_parser(
        "road", help="Same-road typical behaviour per trip from the fuel behaviour map"
    )
    road.add_argument("--repo-root", type=Path, default=Path.cwd())
    alerts = subcommands.add_parser(
        "alerts", help="Replay the frozen 60-second model and Alert Policy V1 for every trip"
    )
    alerts.add_argument("--repo-root", type=Path, default=Path.cwd())
    drivers = subcommands.add_parser(
        "drivers", help="Attribute trip scores to the simulated driver roster (hino-fuel drivers)"
    )
    drivers.add_argument("--repo-root", type=Path, default=Path.cwd())
    drivers.add_argument("--fuel-root", type=Path, default=Path("data/processed/fuel"))
    drivers.add_argument("--output-root", type=Path, default=Path("data/processed/driver_safety"))
    export = subcommands.add_parser(
        "export-demo", help="Write private local demo views below data/processed/demo_views"
    )
    export.add_argument("--repo-root", type=Path, default=Path.cwd())
    export.add_argument("--provider", choices=("mock", "gemini"), default=None)
    export.add_argument("--score-config", type=Path, default=Path("config/safety_score_v1.json"))
    export.add_argument(
        "--event-definitions", type=Path, default=Path("config/event_definitions_v1.json")
    )


def _run_score_command(args: argparse.Namespace) -> int:
    repo = args.repo_root.resolve()
    if args.command == "score":
        manifest = build_safety_scores(
            resolve_score_config(repo, args.analytics_root, args.output_root, args.score_config)
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    if args.command == "road":
        from .road_context import build_road_context

        print(json.dumps(build_road_context(repo), ensure_ascii=False, indent=2))
        return 0
    if args.command == "alerts":
        from .trip_alerts import build_trip_alert_reviews

        print(json.dumps(build_trip_alert_reviews(repo), ensure_ascii=False, indent=2))
        return 0
    if args.command == "drivers":
        manifest = build_driver_safety(
            repo, fuel_root=args.fuel_root, output_root=args.output_root
        )
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    analytics_root = repo / "data" / "processed" / "analytics"
    scores_root = repo / "data" / "processed" / "safety_scores"
    service = ReportService(provider_from_environment(args.provider))
    if args.command == "export-demo":
        views = build_driver_views(
            repo,
            service,
            score_config_path=args.score_config,
            definitions_path=args.event_definitions,
        )
        output = repo / "data" / "processed" / "demo_views"
        output.mkdir(parents=True, exist_ok=True)
        for stale in output.glob("*.local.json"):
            stale.unlink()
        for name, view in views.items():
            (output / f"{name}.local.json").write_text(
                json.dumps(view, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(
            json.dumps(
                {
                    "written_files": len(views),
                    "driver_files": sum(name.startswith("driver-") for name in views),
                    "output": output.relative_to(repo).as_posix(),
                    "serve": "python scripts/serve_demo.py",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    briefing = load_predrive_briefing(scores_root / "predrive_briefings.jsonl", args.trip_id)
    goal_message = (
        service.daily_goal(briefing.daily_goal)
        if briefing.daily_goal and briefing.daily_goal.target_metric
        else None
    )
    if args.command == "predrive":
        print(
            json.dumps(
                {
                    "briefing": briefing.model_dump(mode="json"),
                    "goal_message": goal_message.model_dump(mode="json") if goal_message else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    summary = load_trip_summary(analytics_root / "trip_summaries.jsonl", args.trip_id)
    score = load_trip_score(scores_root / "trip_safety_scores.jsonl", args.trip_id)
    report = service.trip_report(summary)
    print(
        json.dumps(
            {
                "safety_score": score.model_dump(mode="json"),
                "ai_report": report.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


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
    _score_parsers(subcommands)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in {"score", "road", "alerts", "drivers", "predrive", "postdrive", "export-demo"}:
            return _run_score_command(args)
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
