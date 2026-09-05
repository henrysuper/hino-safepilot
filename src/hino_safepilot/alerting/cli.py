"""Reproducible CLI for Phase 3C-3 policy evaluation and search."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .study import evaluate_specified_policy, generate_policy_report, run_policy_search
from .phase3c4 import generate_phase3c4_report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="hino-alert-policy")
    result.add_argument("--repo-root", type=Path, default=Path.cwd())
    result.add_argument(
        "--config", type=Path, default=Path("config/alert_policy_v2.json")
    )
    commands = result.add_subparsers(dest="command", required=True)
    evaluate = commands.add_parser("evaluate", help="evaluate one specified v2 policy")
    evaluate.add_argument("--policy-config", type=Path, required=True)
    search = commands.add_parser("search", help="search development data only")
    search.add_argument("--output", type=Path)
    report = commands.add_parser("report", help="search and generate JSON/Markdown reports")
    report.add_argument("--report-root", type=Path)
    phase3c4 = commands.add_parser("phase3c4-report", help="audit metrics, context feasibility, and voice forensics")
    phase3c4.add_argument("--context-config", type=Path, default=Path("config/context_voice_gate_v1.json"))
    phase3c4.add_argument("--report-root", type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    config_path = args.config if args.config.is_absolute() else repo_root / args.config
    if args.command == "evaluate":
        policy_path = (
            args.policy_config
            if args.policy_config.is_absolute()
            else repo_root / args.policy_config
        )
        payload = evaluate_specified_policy(repo_root, config_path, policy_path)
    elif args.command == "search":
        payload = run_policy_search(repo_root, config_path)
        if args.output:
            output = args.output if args.output.is_absolute() else repo_root / args.output
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
    elif args.command == "report":
        report_root = args.report_root or repo_root / "reports" / "generated" / "phase3c3"
        if not report_root.is_absolute():
            report_root = repo_root / report_root
        payload = generate_policy_report(repo_root, config_path, report_root)
    else:
        context_path = args.context_config if args.context_config.is_absolute() else repo_root / args.context_config
        report_root = args.report_root or repo_root / "reports" / "generated" / "phase3c4"
        if not report_root.is_absolute():
            report_root = repo_root / report_root
        payload = generate_phase3c4_report(repo_root, config_path, context_path, report_root)
    print(
        json.dumps(
            {
                "status": payload.get("status")
                or payload.get("selection_summary", {}).get("winner_status")
                or "EVALUATED",
                "winner_status": payload.get("selection", {}).get("winner_status")
                or payload.get("selection_summary", {}).get("winner_status"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
