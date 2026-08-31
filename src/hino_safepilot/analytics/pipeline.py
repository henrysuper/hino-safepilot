"""Phase 2 Parquet to deterministic Phase 2.5 analytics artifacts."""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable
from uuid import uuid4

import pyarrow.dataset as ds

from .baseline import build_behavior_baseline
from .engine import TripAnalytics
from .goals import WeaknessScorer, load_goal_config
from .models import BehaviorBaseline, DailyImprovementGoal, TripSummary, WeaknessScore


TELEMETRY_COLUMNS = (
    "source_row_number",
    "trip_id",
    "enabled_code",
    "timestamp",
    "timestamp_quality",
    "vehicle_series_code",
    "vehicle_series_code_quality",
    "vehicle_status",
    "vehicle_status_quality",
    "gps_speed_kph",
    "gps_speed_kph_quality",
    "gps_speed_limit",
    "gps_speed_limit_quality",
    "can_speed_kph",
    "can_speed_kph_quality",
    "total_mileage_km",
    "total_mileage_km_quality",
    "total_fuel_used_l",
    "total_fuel_used_l_quality",
    "engine_load_pct",
    "engine_load_pct_quality",
    "gps_coordinates_missing",
    "row_has_malformed_value",
    "row_has_out_of_range_value",
    "is_duplicate_source_key",
)

ONSET_COLUMNS = (
    "event_onset_candidate_id",
    "trip_id",
    "event_name",
    "event_start_time",
    "event_start_time_quality",
    "start_time_ambiguous",
    "start_year_mismatch_observed",
)


@dataclass(frozen=True)
class AnalyticsBuildConfig:
    repo_root: Path
    input_root: Path
    output_root: Path
    goal_config_path: Path


def _safe_resolve(repo_root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def resolve_config(
    repo_root: Path,
    input_root: Path = Path("data/interim"),
    output_root: Path = Path("data/processed/analytics"),
    goal_config_path: Path = Path("config/behavior_goal_v0.json"),
) -> AnalyticsBuildConfig:
    repo = repo_root.resolve()
    source = _safe_resolve(repo, input_root)
    output = _safe_resolve(repo, output_root)
    goal = _safe_resolve(repo, goal_config_path)
    allowed_output = (repo / "data" / "processed").resolve()
    if output == allowed_output or allowed_output not in output.parents:
        raise ValueError("Analytics output must be below data/processed/")
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("Analytics input and output paths must be disjoint")
    return AnalyticsBuildConfig(repo, source, output, goal)


def _read_grouped(dataset: ds.Dataset, columns: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for batch in dataset.to_batches(columns=list(columns), batch_size=20_000):
        for row in batch.to_pylist():
            trip_id = row.get("trip_id")
            if trip_id:
                grouped[trip_id].append(row)
    return grouped


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            count += 1
    return count


def build_analytics(config: AnalyticsBuildConfig) -> dict[str, Any]:
    """Build local deterministic analytics. This function performs no API calls."""

    if config.output_root.exists():
        raise FileExistsError(f"Refusing to overwrite analytics output: {config.output_root}")
    telemetry_path = config.input_root / "telemetry_normalized"
    onset_path = config.input_root / "events_onsets"
    if not telemetry_path.is_dir() or not onset_path.is_dir():
        raise FileNotFoundError("Phase 2 telemetry_normalized/events_onsets are required")
    telemetry_dataset = ds.dataset(telemetry_path, format="parquet", partitioning="hive")
    onset_dataset = ds.dataset(onset_path, format="parquet", partitioning="hive")
    missing_telemetry = set(TELEMETRY_COLUMNS) - set(telemetry_dataset.schema.names)
    missing_onsets = set(ONSET_COLUMNS) - set(onset_dataset.schema.names)
    if missing_telemetry or missing_onsets:
        raise ValueError(
            f"Phase 2 schema mismatch: telemetry={sorted(missing_telemetry)}, "
            f"onsets={sorted(missing_onsets)}"
        )

    started = perf_counter()
    telemetry = _read_grouped(telemetry_dataset, TELEMETRY_COLUMNS)
    onsets = _read_grouped(onset_dataset, ONSET_COLUMNS)
    analyzer = TripAnalytics()
    summaries: list[TripSummary] = []
    rejected_trips = 0
    for trip_id in sorted(telemetry):
        try:
            summaries.append(analyzer.analyze_trip(telemetry[trip_id], onsets.get(trip_id, [])))
        except ValueError:
            rejected_trips += 1

    by_profile: dict[str, list[TripSummary]] = defaultdict(list)
    for summary in summaries:
        by_profile[summary.vehicle_profile_id].append(summary)
    for profile_summaries in by_profile.values():
        profile_summaries.sort(key=lambda summary: summary.basics.trip_start)

    goal_config = load_goal_config(config.goal_config_path)
    scorer = WeaknessScorer(goal_config)
    baseline_records: list[dict[str, Any]] = []
    score_records: list[dict[str, Any]] = []
    goal_records: list[dict[str, Any]] = []
    for summary in sorted(summaries, key=lambda item: (item.basics.trip_start, item.trip_id)):
        history = by_profile[summary.vehicle_profile_id]
        cutoff = summary.basics.trip_start
        recent = build_behavior_baseline(
            history,
            vehicle_profile_id=summary.vehicle_profile_id,
            cutoff_exclusive=cutoff,
            previous_days=int(goal_config.raw["recent_days"]),
            minimum_trip_count=goal_config.minimum_trip_count,
        )
        baseline_30d = build_behavior_baseline(
            history,
            vehicle_profile_id=summary.vehicle_profile_id,
            cutoff_exclusive=cutoff,
            previous_days=int(goal_config.raw["baseline_days"]),
            minimum_trip_count=goal_config.minimum_trip_count,
        )
        previous_n = build_behavior_baseline(
            history,
            vehicle_profile_id=summary.vehicle_profile_id,
            cutoff_exclusive=cutoff,
            previous_n_trips=10,
            minimum_trip_count=goal_config.minimum_trip_count,
        )
        historical = build_behavior_baseline(
            history,
            vehicle_profile_id=summary.vehicle_profile_id,
            cutoff_exclusive=cutoff - timedelta(days=int(goal_config.raw["recent_days"])),
            previous_days=int(goal_config.raw["baseline_days"]),
            minimum_trip_count=goal_config.minimum_trip_count,
        )
        trend_current = build_behavior_baseline(
            history,
            vehicle_profile_id=summary.vehicle_profile_id,
            cutoff_exclusive=cutoff,
            previous_days=4,
            minimum_trip_count=1,
        )
        trend_previous = build_behavior_baseline(
            history,
            vehicle_profile_id=summary.vehicle_profile_id,
            cutoff_exclusive=cutoff - timedelta(days=4),
            previous_days=3,
            minimum_trip_count=1,
        )
        scores = scorer.score(recent, historical, trend_current, trend_previous)
        goal = scorer.daily_goal(
            vehicle_profile_id=summary.vehicle_profile_id,
            effective_at=cutoff,
            scores=scores,
        )
        for label, baseline in (
            ("previous_7_days", recent),
            ("previous_30_days", baseline_30d),
            ("previous_10_trips", previous_n),
        ):
            baseline_records.append(
                {
                    "subject_trip_id": summary.trip_id,
                    "baseline_name": label,
                    "baseline": baseline.model_dump(mode="json"),
                }
            )
        score_records.append(
            {
                "subject_trip_id": summary.trip_id,
                "scores": [score.model_dump(mode="json") for score in scores],
            }
        )
        goal_records.append(
            {"subject_trip_id": summary.trip_id, "goal": goal.model_dump(mode="json")}
        )

    staging = config.output_root.with_name(
        f".{config.output_root.name}.staging-{uuid4().hex}"
    )
    staging.mkdir(parents=True, exist_ok=False)
    try:
        summary_count = _write_jsonl(
            staging / "trip_summaries.jsonl",
            (summary.model_dump(mode="json") for summary in summaries),
        )
        baseline_count = _write_jsonl(staging / "behavior_baselines.jsonl", baseline_records)
        score_count = _write_jsonl(staging / "weakness_scores.jsonl", score_records)
        goal_count = _write_jsonl(staging / "improvement_goals.jsonl", goal_records)
        manifest = {
            "analytics_version": "phase-2.5-v1",
            "deterministic_only": True,
            "external_api_calls": 0,
            "behavior_profile_type": "vehicle-linked behavior profile",
            "input": {
                "telemetry_rows": telemetry_dataset.count_rows(),
                "onset_candidate_rows": onset_dataset.count_rows(),
                "telemetry_schema_columns": len(telemetry_dataset.schema.names),
                "onset_schema_columns": len(onset_dataset.schema.names),
            },
            "output": {
                "trip_summary_count": summary_count,
                "baseline_record_count": baseline_count,
                "weakness_score_record_count": score_count,
                "improvement_goal_count": goal_count,
                "rejected_trip_count": rejected_trips,
            },
            "goal_config_version": goal_config.version,
            "elapsed_seconds": perf_counter() - started,
            "unresolved": [
                "Fuel cumulative reset/rollover/device replacement semantics",
                "Prototype weakness weights and reference rates are not domain validated",
                "No trustworthy driver identity; profile remains vehicle-linked",
                "Speed-limit zero semantics",
            ],
        }
        (staging / "analytics_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.replace(config.output_root)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return manifest


def load_trip_summary(path: Path, trip_id: str) -> TripSummary:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("trip_id") == trip_id:
                return TripSummary.model_validate(record)
    raise KeyError("Trip summary not found")


def load_daily_goal(path: Path, trip_id: str) -> DailyImprovementGoal:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("subject_trip_id") == trip_id:
                return DailyImprovementGoal.model_validate(record["goal"])
    raise KeyError("Daily goal not found")
