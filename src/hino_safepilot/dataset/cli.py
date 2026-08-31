"""Command-line interface for Phase 3B-1 dataset generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .builder import DatasetBuildConfig, build_dataset


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="hino-dataset")
    result.add_argument("--repo-root", type=Path, default=Path.cwd())
    result.add_argument("--output-root", type=Path, default=Path("data/processed/ml/v0"))
    result.add_argument("--feature-config", type=Path, default=Path("config/features_v0.json"))
    result.add_argument("--batch-size", type=int, default=2_000)
    result.add_argument("--deterministic-rebuild", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = DatasetBuildConfig.from_repo(
        args.repo_root,
        output_root=args.output_root,
        feature_config=args.feature_config,
        batch_size=args.batch_size,
    )
    manifest = build_dataset(config, deterministic_rebuild=args.deterministic_rebuild)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())

