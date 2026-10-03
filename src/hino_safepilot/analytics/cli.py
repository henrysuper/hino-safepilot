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
    build_safety_scores,
    load_event_definitions,
    load_predrive_briefing,
    load_trip_score,
    local_postdrive_view,
    local_predrive_view,
    pick_demo_trip,
    resolve_score_config,
)
from .scoring import load_safety_score_config


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
    export = subcommands.add_parser(
        "export-demo", help="Write private local demo views below data/processed/demo_views"
    )
    export.add_argument("--repo-root", type=Path, default=Path.cwd())
    export.add_argument("--trip-id", default=None, help="Default: a median-score trip")
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
    analytics_root = repo / "data" / "processed" / "analytics"
    scores_root = repo / "data" / "processed" / "safety_scores"
    service = ReportService(provider_from_environment(args.provider))
    trip_id = args.trip_id
    if args.command == "export-demo" and trip_id is None:
        trip_id = pick_demo_trip(scores_root)
    briefing = load_predrive_briefing(scores_root / "predrive_briefings.jsonl", trip_id)
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
    summary = load_trip_summary(analytics_root / "trip_summaries.jsonl", trip_id)
    score = load_trip_score(scores_root / "trip_safety_scores.jsonl", trip_id)
    report = service.trip_report(summary)
    if args.command == "postdrive":
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

    score_config = load_safety_score_config(repo / args.score_config)
    categories = {name: str(item["label"]) for name, item in score_config.categories.items()}
    views = {
        "predrive": local_predrive_view(
            briefing,
            goal_message.message.model_dump(mode="json") if goal_message else None,
            categories,
        ),
        "postdrive": local_postdrive_view(
            summary,
            score,
            report.report.model_dump(mode="json"),
            report.generation_mode,
            load_event_definitions(repo / args.event_definitions),
        ),
    }
    output = repo / "data" / "processed" / "demo_views"
    output.mkdir(parents=True, exist_ok=True)
    for name, view in views.items():
        (output / f"{name}.local.json").write_text(
            json.dumps(view, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "written": sorted(
                    path.relative_to(repo).as_posix() for path in output.glob("*.local.json")
                ),
                "overall_score": views["postdrive"]["safety_score"]["overall_score"],
                "grade": views["postdrive"]["safety_score"]["grade"],
                "ai_generation_mode": report.generation_mode,
                "serve": "python scripts/serve_demo.py",
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
        if args.command in {"score", "predrive", "postdrive", "export-demo"}:
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
