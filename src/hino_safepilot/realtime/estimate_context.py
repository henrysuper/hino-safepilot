"""Estimate data-driven context-risk coefficients from the HINO interim data.

Idea: for each telemetry row we know (a) its congestion bucket, derived from
gps_speed vs gps_speed_limit, and (b) whether a core-risk event onset occurs
within the next 60 seconds on the same trip. The ratio of the core-event rate
inside a bucket to the overall rate, on the log scale, is the logit delta the
context layer adds. This grounds the adjustment in the actual data rather than
guessed multipliers.

This reads only derived interim Parquet (never raw rows), aggregates counts, and
writes config/context_risk_v0.json. It performs a bounded single pass and does
not train any model.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

import pyarrow.dataset as ds


CORE_EVENT_TYPES = {7, 12, 16, 19, 20}  # decelerate, fatigue, distract, pcs, ldws
HORIZON_SECONDS = 60


def _congestion_bucket(speed: float | None, limit: float | None) -> str | None:
    if speed is None:
        return None
    if limit and limit > 0:
        r = speed / limit
        if r < 0.3:
            return "heavy_congestion"
        if r < 0.6:
            return "moderate_congestion"
        return "free_flow"
    if speed < 15:
        return "heavy_congestion"
    if speed < 35:
        return "moderate_congestion"
    return "free_flow"


def estimate_context_coefficients(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    interim = repo_root / "data" / "interim"

    # 1) Core event onset times per trip (from events_observed).
    events = ds.dataset(interim / "events_observed", format="parquet", partitioning="hive")
    onset_times: dict[str, list] = defaultdict(list)
    scanner = events.scanner(
        columns=["trip_id", "telemetry_timestamp", "telemetry_timestamp_quality", "event_type"],
        batch_size=50_000,
    )
    for batch in scanner.to_batches():
        for row in batch.to_pylist():
            if (
                row["trip_id"]
                and row["event_type"] in CORE_EVENT_TYPES
                and row["telemetry_timestamp"] is not None
                and row["telemetry_timestamp_quality"] == "valid"
            ):
                onset_times[row["trip_id"]].append(row["telemetry_timestamp"])
    for t in onset_times.values():
        t.sort()

    # 2) Walk telemetry rows; per congestion bucket count rows and positives
    #    (a core event onset within the next 60s on the same trip).
    telemetry = ds.dataset(interim / "telemetry_normalized", format="parquet", partitioning="hive")
    bucket_total: dict[str, int] = defaultdict(int)
    bucket_pos: dict[str, int] = defaultdict(int)
    roadless_total = 0  # rows with no usable speed
    scanner = telemetry.scanner(
        columns=[
            "trip_id",
            "timestamp",
            "timestamp_quality",
            "gps_speed_kph",
            "gps_speed_kph_quality",
            "gps_speed_limit",
            "gps_speed_limit_quality",
        ],
        batch_size=50_000,
    )
    for batch in scanner.to_batches():
        for row in batch.to_pylist():
            if not row["trip_id"] or row["timestamp"] is None or row["timestamp_quality"] != "valid":
                continue
            speed = row["gps_speed_kph"] if row["gps_speed_kph_quality"] == "valid" else None
            limit = row["gps_speed_limit"] if row["gps_speed_limit_quality"] == "valid" else None
            bucket = _congestion_bucket(speed, limit)
            if bucket is None:
                roadless_total += 1
                continue
            bucket_total[bucket] += 1
            times = onset_times.get(row["trip_id"])
            if times:
                anchor = row["timestamp"]
                horizon = anchor + timedelta(seconds=HORIZON_SECONDS)
                # positive if any onset in (anchor, anchor+60s]
                if bisect_right(times, horizon) > bisect_right(times, anchor):
                    bucket_pos[bucket] += 1

    overall_total = sum(bucket_total.values())
    overall_pos = sum(bucket_pos.values())
    overall_rate = overall_pos / overall_total if overall_total else 0.0

    congestion_delta: dict[str, float] = {}
    rates: dict[str, dict[str, Any]] = {}
    for bucket in ("heavy_congestion", "moderate_congestion", "free_flow"):
        total = bucket_total.get(bucket, 0)
        pos = bucket_pos.get(bucket, 0)
        rate = pos / total if total else 0.0
        # log-ratio vs overall; guard tiny/zero rates.
        if rate > 0 and overall_rate > 0:
            delta = math.log(rate / overall_rate)
        else:
            delta = 0.0
        congestion_delta[bucket] = round(delta, 4)
        rates[bucket] = {"rows": total, "positives": pos, "event_rate": round(rate, 6)}

    config = {
        "version": "context-risk-v0-data-estimated",
        "enabled": True,
        "estimated_from": "data/interim (events_observed + telemetry_normalized)",
        "horizon_seconds": HORIZON_SECONDS,
        "core_event_types": sorted(CORE_EVENT_TYPES),
        "overall_event_rate": round(overall_rate, 6),
        "overall_rows": overall_total,
        "congestion_logit_delta": congestion_delta,
        "congestion_rates_audit": rates,
        "rows_without_usable_speed": roadless_total,
        # Road-class deltas: not estimable from telemetry alone (no per-row road
        # class in interim). Left neutral pending a TDX-joined estimate; kept for
        # forward compatibility so the adjuster can use them when available.
        "road_class_logit_delta": {},
        # Weather deltas are assumptions (no 2025 historical weather available).
        "weather": {
            "enabled": True,
            "note": "ASSUMPTION values, not validated against HINO data",
            "rain_thresholds_mm": {"light": 2.0, "moderate": 8.0, "heavy": 15.0},
            "rain_logit_delta": {"light": 0.10, "moderate": 0.30, "heavy": 0.60},
            "low_visibility_threshold_m": 500.0,
            "low_visibility_logit_delta": 0.40,
        },
    }
    return config


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="hino-estimate-context")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("config/context_risk_v0.json"))
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    config = estimate_context_coefficients(repo_root)
    output = args.output if args.output.is_absolute() else repo_root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "WRITTEN", "output": str(output), "overall_event_rate": config["overall_event_rate"], "congestion_logit_delta": config["congestion_logit_delta"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
