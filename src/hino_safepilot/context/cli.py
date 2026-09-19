"""Reproducible CLI wiring download -> snapshot -> quality report.

The pipeline is split into three subcommands so each stage can run
independently and honestly under the current constraints:

- ``download``   fetches provider datasets described by a request plan, stores
                 the raw payloads into a versioned snapshot, and writes a
                 manifest. Requires credentials; fails clearly if they are absent.
                 A real network call happens only here and only when invoked.
- ``snapshot``   builds a snapshot + manifest from local fixture JSON files with
                 no network and no credentials, so the whole flow is exercisable
                 today before any API key exists.
- ``quality``    parses a snapshot's payloads and runs the integrity / temporal
                 leakage / spatial-join report. It stays provisional because the
                 HINO timezone is unresolved; decision points are optional and
                 must be timezone-aware when supplied.

Nothing here resolves the timezone or makes an empirical claim.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .cache import ContextCache
from .cwa import parse_cwa_rainfall_observations
from .download import build_downloader
from .quality import build_quality_report
from .snapshot import SnapshotBuilder
from .tdx import parse_tdx_vd_live


DEFAULT_SNAPSHOT_ROOT = Path("data/reference/context_snapshots")


def _resolve(repo_root: Path, path: Path) -> Path:
    return path if path.is_absolute() else repo_root / path


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_observations(provider: str, dataset: str, payload: Any, plan_entry: dict, *, is_mock: bool):
    """Parse a raw payload into ContextObservations using the right parser.

    TDX VD needs static VD coordinates, which the plan entry must supply as
    ``vd_locations`` mapping ``VDID -> [lat, lon, road_class]``. If a provider or
    dataset has no parser wired yet, observations are omitted (summary only).
    """

    normalized = provider.strip().lower()
    if normalized == "tdx":
        raw_locations = plan_entry.get("vd_locations") or {}
        vd_locations = {
            str(vd_id): (float(value[0]), float(value[1]), value[2] if len(value) > 2 else None)
            for vd_id, value in raw_locations.items()
        }
        return parse_tdx_vd_live(payload, vd_locations=vd_locations, is_mock=is_mock)
    if normalized == "cwa":
        return parse_cwa_rainfall_observations(payload, is_mock=is_mock)
    return None


def _build_snapshot(
    *,
    snapshot_id: str,
    snapshot_root: Path,
    entries: list[dict],
    payload_loader,
    is_mock: bool,
    extra_notes: dict | None = None,
) -> dict:
    """Shared snapshot assembly for both download and fixture paths."""

    cache = ContextCache(snapshot_root / snapshot_id / "cache")
    builder = SnapshotBuilder(snapshot_id=snapshot_id, cache=cache)
    for entry in entries:
        provider = entry["provider"]
        dataset = entry["dataset"]
        request_identity = entry["request_identity"]
        payload = payload_loader(entry)
        observations = _parse_observations(provider, dataset, payload, entry, is_mock=is_mock)
        builder.add_payload(
            provider=provider,
            dataset=dataset,
            request_identity=request_identity,
            payload=payload,
            is_mock=is_mock,
            observations=observations,
        )
    manifest_path = snapshot_root / snapshot_id / "manifest.json"
    return builder.write_manifest(manifest_path, extra_notes=extra_notes)


def _cmd_download(repo_root: Path, args: argparse.Namespace) -> dict:
    """Fetch each plan entry from its provider and snapshot the raw payloads.

    Plan schema (JSON):
        {
          "snapshot_id": "tdx-cwa-20250920",
          "entries": [
            {"provider": "tdx", "dataset": "Traffic-v2-VDLive",
             "request_identity": "2025-09-20T08:00", "url": "https://.../...",
             "vd_locations": {"VD-1": [25.0, 121.5, "urban"]}},
            {"provider": "cwa", "dataset": "O-A0002-001",
             "request_identity": "2025-09-20T08:00",
             "query": {"StationId": "466920"}}
          ]
        }
    TDX entries use ``url``; CWA entries use ``dataset`` + optional ``query``.
    """

    plan_path = _resolve(repo_root, args.plan)
    plan = _load_json(plan_path)
    snapshot_id = plan["snapshot_id"]
    snapshot_root = _resolve(repo_root, args.snapshot_root)
    downloaders: dict[str, Any] = {}

    def loader(entry: dict) -> Any:
        provider = entry["provider"].strip().lower()
        if provider not in downloaders:
            downloaders[provider] = build_downloader(provider, timeout_seconds=args.timeout_seconds)
        downloader = downloaders[provider]
        if provider == "tdx":
            return downloader.download_json(entry["url"])
        if provider == "cwa":
            return downloader.download_json(entry["dataset"], entry.get("query"))
        raise ValueError(f"no download path for provider {provider!r}")

    manifest = _build_snapshot(
        snapshot_id=snapshot_id,
        snapshot_root=snapshot_root,
        entries=plan["entries"],
        payload_loader=loader,
        is_mock=False,
        extra_notes={"source": "live_download", "plan": str(plan_path.name)},
    )
    return {
        "status": "SNAPSHOT_WRITTEN",
        "snapshot_id": manifest["snapshot_id"],
        "payload_count": manifest["payload_count"],
        "providers": manifest["providers"],
        "snapshot_content_sha256": manifest["snapshot_content_sha256"],
        "manifest_path": str(snapshot_root / snapshot_id / "manifest.json"),
        "empirical_claim_status": manifest["empirical_claim_status"],
    }


def _cmd_snapshot(repo_root: Path, args: argparse.Namespace) -> dict:
    """Build a snapshot from local fixture files (no network, no credentials).

    The plan is identical to ``download`` except each entry carries a
    ``fixture`` path (relative to the plan file or absolute) instead of network
    coordinates. Useful to validate parsing/manifest/quality end to end today.
    """

    plan_path = _resolve(repo_root, args.plan)
    plan = _load_json(plan_path)
    snapshot_id = plan["snapshot_id"]
    snapshot_root = _resolve(repo_root, args.snapshot_root)
    plan_dir = plan_path.parent

    def loader(entry: dict) -> Any:
        fixture = Path(entry["fixture"])
        fixture = fixture if fixture.is_absolute() else plan_dir / fixture
        return _load_json(fixture)

    manifest = _build_snapshot(
        snapshot_id=snapshot_id,
        snapshot_root=snapshot_root,
        entries=plan["entries"],
        payload_loader=loader,
        is_mock=bool(args.mark_mock),
        extra_notes={"source": "local_fixture", "plan": str(plan_path.name)},
    )
    return {
        "status": "SNAPSHOT_WRITTEN",
        "snapshot_id": manifest["snapshot_id"],
        "payload_count": manifest["payload_count"],
        "providers": manifest["providers"],
        "contains_mock": manifest["contains_mock"],
        "snapshot_content_sha256": manifest["snapshot_content_sha256"],
        "manifest_path": str(snapshot_root / snapshot_id / "manifest.json"),
        "empirical_claim_status": manifest["empirical_claim_status"],
    }


def _load_decision_points(path: Path | None) -> tuple[list[datetime] | None, list[tuple[datetime, float, float]] | None]:
    """Load optional timezone-aware decision points for leakage/spatial checks.

    File schema (JSON list): [{"time": "2025-09-20T00:00:00+00:00",
    "latitude": 25.0, "longitude": 121.5}, ...]. Times must be timezone-aware;
    the quality checks refuse naive times so an unresolved HINO timezone cannot
    silently corrupt a join.
    """

    if path is None:
        return None, None
    rows = _load_json(path)
    decision_times: list[datetime] = []
    spatial: list[tuple[datetime, float, float]] = []
    for row in rows:
        moment = datetime.fromisoformat(str(row["time"]))
        decision_times.append(moment)
        if row.get("latitude") is not None and row.get("longitude") is not None:
            spatial.append((moment, float(row["latitude"]), float(row["longitude"])))
    return decision_times, (spatial or None)


def _cmd_quality(repo_root: Path, args: argparse.Namespace) -> dict:
    """Parse a snapshot's cached payloads and emit the quality report."""

    snapshot_root = _resolve(repo_root, args.snapshot_root)
    snapshot_id = args.snapshot_id
    manifest_path = snapshot_root / snapshot_id / "manifest.json"
    manifest = _load_json(manifest_path)
    cache = ContextCache(snapshot_root / snapshot_id / "cache")

    observations = []
    for entry in manifest["payloads"]:
        provider = entry["provider"]
        dataset = entry["dataset"]
        payload = cache.get_json(provider, dataset, entry["request_identity"])
        if payload is None:
            continue
        parsed = _parse_observations(provider, dataset, payload, {}, is_mock=entry["is_mock"])
        if parsed:
            observations.extend(parsed)

    decision_times, spatial_queries = _load_decision_points(
        _resolve(repo_root, args.decision_points) if args.decision_points else None
    )
    report = build_quality_report(
        observations,
        decision_times=decision_times,
        spatial_queries=spatial_queries,
        maximum_age_seconds=args.maximum_age_seconds,
        maximum_distance_m=args.maximum_distance_m,
    )
    report["snapshot_id"] = snapshot_id
    report["snapshot_content_sha256"] = manifest["snapshot_content_sha256"]
    report["parsed_observation_count"] = len(observations)

    if args.output:
        output = _resolve(repo_root, args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        report["report_path"] = str(output)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="hino-context")
    result.add_argument("--repo-root", type=Path, default=Path.cwd())
    result.add_argument(
        "--snapshot-root",
        type=Path,
        default=DEFAULT_SNAPSHOT_ROOT,
        help="gitignored root for versioned snapshots (default data/reference/context_snapshots)",
    )
    commands = result.add_subparsers(dest="command", required=True)

    download = commands.add_parser("download", help="fetch a request plan and write a snapshot (requires credentials)")
    download.add_argument("--plan", type=Path, required=True, help="JSON request plan")
    download.add_argument("--timeout-seconds", type=float, default=30.0)

    snapshot = commands.add_parser("snapshot", help="build a snapshot from local fixtures (no network)")
    snapshot.add_argument("--plan", type=Path, required=True, help="JSON fixture plan")
    snapshot.add_argument("--mark-mock", action="store_true", help="flag payloads as mock (kept out of empirical metrics)")

    quality = commands.add_parser("quality", help="run integrity/leakage/spatial-join checks over a snapshot")
    quality.add_argument("--snapshot-id", required=True)
    quality.add_argument("--decision-points", type=Path, help="optional JSON of timezone-aware decision points")
    quality.add_argument("--maximum-age-seconds", type=float, default=1800.0)
    quality.add_argument("--maximum-distance-m", type=float, default=30000.0)
    quality.add_argument("--output", type=Path, help="optional path to write the JSON report")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.command == "download":
        payload = _cmd_download(repo_root, args)
    elif args.command == "snapshot":
        payload = _cmd_snapshot(repo_root, args)
    else:
        payload = _cmd_quality(repo_root, args)
    print(json.dumps({"status": payload.get("status", "OK"), **{k: payload[k] for k in ("snapshot_id",) if k in payload}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
