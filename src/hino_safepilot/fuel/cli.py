"""Command line: ``hino-fuel <stage>`` (see build_parser for the stage list)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .pipeline import (
    FuelPaths, attach_route_overlap, run_backtest, run_dispatch_demo, run_fence_test, run_fences, run_score, run_segments, run_train,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fuel prediction and dispatch system (docs/開發說明書.md)")
    parser.add_argument("stage", choices=("segments", "train", "drivers", "score", "dispatch-demo", "predict", "backtest", "route-overlap", "fences", "fence-test", "build-graph", "serve"))
    parser.add_argument("--trips", type=int, default=60, help="backtest: number of real trips (ORS calls)")
    parser.add_argument("--port", type=int, default=8765, help="serve: local port (binds 127.0.0.1 only)")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, default=Path("config/fuel_v1.json"))
    parser.add_argument("--review-code", action="append", default=[], help="enabledCode to inspect individually (reported by alias).")
    parser.add_argument("--origin", help="lon,lat")
    parser.add_argument("--destination", help="lon,lat")
    parser.add_argument("--depart", default=None, help="ISO time; default now with --live, else 2026-10-05T08:00:00+08:00")
    parser.add_argument("--vehicle", action="append", default=[], help="candidate enabledCode (repeatable)")
    parser.add_argument("--driver-type", action="append", default=[], help="very_eco | eco | normal | aggressive | very_aggressive")
    parser.add_argument("--live", action="store_true", help="use TDX live traffic (depart now)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    paths = FuelPaths.resolve(args.repo_root, config_path=args.config)
    if args.depart is None:
        import pandas as pd

        args.depart = pd.Timestamp.now(tz="Asia/Taipei").isoformat() if args.live else "2026-10-05T08:00:00+08:00"
    if args.stage == "segments":
        print(json.dumps(run_segments(paths, review_codes=tuple(args.review_code))["summary"], ensure_ascii=False, indent=2))
    elif args.stage == "train":
        report = run_train(paths)
        print(json.dumps({"chosen": report["selection"]["chosen"], "test": report["test"]["time_split"]["expected"]}, ensure_ascii=False, indent=2))
    elif args.stage == "score":
        summary = run_score(paths)
        print(json.dumps({k: v for k, v in summary.items() if k != "examples"}, ensure_ascii=False, indent=2, default=str))
    elif args.stage == "dispatch-demo":
        payload = run_dispatch_demo(paths, args.depart, args.live)
        for task in payload["tasks"]:
            print(task["task"], json.dumps(task["result"]["lowest_fuel_option"], ensure_ascii=False))
    elif args.stage == "backtest":
        summary = run_backtest(paths, args.trips)
        print(json.dumps({k: summary[k] for k in ("funnel", "ors_live_calls", "calibration", "terminal_overhead_by_type", "metrics", "metrics_uncalibrated", "errors")},
                         ensure_ascii=False, indent=2))
    elif args.stage == "drivers":
        from .pipeline import run_drivers

        report = run_drivers(paths)
        print(json.dumps({k: report[k] for k in ("roster", "evaluation_test_period_ge10km", "replay", "elapsed_seconds")},
                         ensure_ascii=False, indent=2, default=str))
    elif args.stage == "build-graph":
        from .pipeline import run_build_graph

        print(json.dumps(run_build_graph(paths), ensure_ascii=False, indent=2))
    elif args.stage == "route-overlap":
        print(json.dumps(attach_route_overlap(paths), ensure_ascii=False, indent=2))
    elif args.stage == "fences":
        print(json.dumps(run_fences(paths), ensure_ascii=False, indent=2))
    elif args.stage == "fence-test":
        summary = run_fence_test(paths)
        print(json.dumps({k: v for k, v in summary.items() if k not in ("pairs", "trips")}, ensure_ascii=False, indent=2))
    elif args.stage == "serve":
        from .server import serve

        serve(paths, args.port)
    elif args.stage == "predict":
        from .fuel_predictor import FuelPredictor

        if not (args.origin and args.destination and args.vehicle):
            raise SystemExit("predict needs --origin, --destination and at least one --vehicle")
        predictor = FuelPredictor.from_repo(paths.repo_root, with_live_traffic=args.live)
        result = predictor.predict_trip_fuel(
            [float(v) for v in args.origin.split(",")], [float(v) for v in args.destination.split(",")],
            args.depart, args.vehicle, args.driver_type or ["normal"], args.live,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0
