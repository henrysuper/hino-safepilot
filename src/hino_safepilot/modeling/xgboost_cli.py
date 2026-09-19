"""CLI for Phase 3C-2 XGBoost and alert-policy experimentation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .xgboost_training import train_phase3c2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="hino-train-xgboost")
    result.add_argument("--repo-root", type=Path, default=Path.cwd())
    result.add_argument("--config", type=Path)
    result.add_argument("--artifact-root", type=Path)
    result.add_argument("--report-root", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = train_phase3c2(
        args.repo_root,
        config_path=args.config,
        artifact_root=args.artifact_root,
        report_root=args.report_root,
    )
    print(json.dumps({
        "status": report["status"],
        "known_holdout_status": report["known_holdout_status"],
        "runtime_seconds": report["runtime_seconds"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "XGBOOST_EXPERIMENT_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())

