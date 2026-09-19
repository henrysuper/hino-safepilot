"""Command-line entry point for Phase 2 ETL V0."""

from __future__ import annotations

import argparse
from pathlib import Path

from .constants import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_MANIFEST_RELATIVE_PATH,
    DEFAULT_OUTPUT_RELATIVE_PATH,
    DEFAULT_SOURCE_RELATIVE_PATH,
)
from .pipeline import EtlConfig, configure_logging, run_etl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate immutable HINO XLSX and create normalized Phase 2 Parquet datasets."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--source", type=Path, default=Path(DEFAULT_SOURCE_RELATIVE_PATH))
    parser.add_argument("--output-root", type=Path, default=Path(DEFAULT_OUTPUT_RELATIVE_PATH))
    parser.add_argument(
        "--manifest", type=Path, default=Path(DEFAULT_MANIFEST_RELATIVE_PATH)
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate hash, sheets, dimensions, and ordered columns without writing outputs.",
    )
    return parser


def _under_repo(repo_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else repo_root / path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    repo_root = args.repo_root.resolve()
    config = EtlConfig(
        repo_root=repo_root,
        source_path=_under_repo(repo_root, args.source),
        output_root=_under_repo(repo_root, args.output_root),
        manifest_path=_under_repo(repo_root, args.manifest),
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    run_etl(config)
    return 0

