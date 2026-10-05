"""Empirical Phase 3A label audit over private, local derived data."""

from __future__ import annotations

import json
import math
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable
from uuid import uuid4

import pyarrow.dataset as ds

from hino_safepilot.etl.constants import EVENT_NAMES

from .models import CORE_TYPES, OnsetRecord, SimulationPrivateStats, TripRecord
from .simulation import (
    history_diagnostics,
    history_is_valid,
    scenario_matrix,
    simulate_scenario,
    telemetry_aligned_anchors,
)


@dataclass(frozen=True)
class LabelResearchConfig:
    repo_root: Path
    interim_root: Path
    analytics_root: Path
    output_root: Path

    @classmethod
    def from_repo(
        cls,
        repo_root: Path,
        interim_root: Path = Path("data/interim"),
        analytics_root: Path = Path("data/processed/analytics"),
        output_root: Path = Path("data/processed/label_research"),
    ) -> "LabelResearchConfig":
        repo = repo_root.resolve()
        resolve = lambda path: path.resolve() if path.is_absolute() else (repo / path).resolve()
        interim, analytics, output = resolve(interim_root), resolve(analytics_root), resolve(output_root)
        allowed = (repo / "data" / "processed").resolve()
        if output == allowed or allowed not in output.parents:
            raise ValueError("Label research output must be below data/processed/")
        if output.exists():
            raise FileExistsError(f"Refusing to overwrite label research output: {output}")
        return cls(repo, interim, analytics, output)


def _quantiles(values: Iterable[float]) -> dict[str, float | None]:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return {key: None for key in ("min", "p25", "median", "p75", "p90", "p95", "p99", "max")}

    def value_at(probability: float) -> float:
        position = (len(ordered) - 1) * probability
        lower, upper = math.floor(position), math.ceil(position)
        if lower == upper:
            return ordered[lower]
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return {
        "min": ordered[0],
        "p25": value_at(0.25),
        "median": value_at(0.5),
        "p75": value_at(0.75),
        "p90": value_at(0.9),
        "p95": value_at(0.95),
        "p99": value_at(0.99),
        "max": ordered[-1],
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_telemetry(config: LabelResearchConfig) -> tuple[dict[str, TripRecord], int]:
    dataset = ds.dataset(
        config.interim_root / "telemetry_normalized", format="parquet", partitioning="hive"
    )
    columns = [
        "trip_id",
        "enabled_code",
        "journey_code",
        "timestamp",
        "timestamp_quality",
        "gps_speed_out_of_range",
        "azimuth_out_of_range",
        "gps_coordinates_missing",
        "row_has_malformed_value",
        "is_duplicate_source_key",
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for batch in dataset.to_batches(columns=columns, batch_size=20_000):
        for row in batch.to_pylist():
            trip_id = row.get("trip_id")
            timestamp = row.get("timestamp")
            if not trip_id or timestamp is None or row.get("timestamp_quality") != "valid":
                continue
            group = grouped.setdefault(
                trip_id,
                {
                    "vehicle": row.get("enabled_code") or "MISSING_VEHICLE",
                    "journey": row.get("journey_code"),
                    "timestamps": [],
                    "raw": 0,
                    "duplicate": 0,
                    "gps_speed": 0,
                    "azimuth": 0,
                    "missing_gps": 0,
                    "malformed": 0,
                },
            )
            group["timestamps"].append(timestamp)
            group["raw"] += 1
            group["duplicate"] += bool(row.get("is_duplicate_source_key"))
            group["gps_speed"] += bool(row.get("gps_speed_out_of_range"))
            group["azimuth"] += bool(row.get("azimuth_out_of_range"))
            group["missing_gps"] += bool(row.get("gps_coordinates_missing"))
            group["malformed"] += bool(row.get("row_has_malformed_value"))
    trips = {
        trip_id: TripRecord(
            trip_id=trip_id,
            vehicle_key=group["vehicle"],
            journey_code=group["journey"],
            timestamps=sorted(set(group["timestamps"])),
            raw_row_count=group["raw"],
            duplicate_timestamp_row_count=group["duplicate"],
            gps_speed_out_of_range_rows=group["gps_speed"],
            azimuth_out_of_range_rows=group["azimuth"],
            missing_gps_rows=group["missing_gps"],
            malformed_rows=group["malformed"],
        )
        for trip_id, group in grouped.items()
    }
    return trips, dataset.count_rows()


def _attach_trip_analytics(config: LabelResearchConfig, trips: dict[str, TripRecord]) -> None:
    path = config.analytics_root / "trip_summaries.jsonl"
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            summary = json.loads(line)
            trip = trips.get(summary.get("trip_id"))
            if not trip:
                continue
            basics, fuel = summary["basics"], summary["fuel"]
            trip.distance_km = basics["distance_km"]["value"]
            trip.distance_quality = basics["distance_km"]["quality_status"]
            trip.driving_hours = basics["driving_hours"]["value"]
            trip.driving_hours_quality = basics["driving_hours"]["quality_status"]
            trip.duration_seconds = basics["duration_seconds"]["value"]
            trip.duration_quality = basics["duration_seconds"]["quality_status"]


def _load_onsets(config: LabelResearchConfig) -> tuple[list[OnsetRecord], int]:
    dataset = ds.dataset(
        config.interim_root / "events_onsets", format="parquet", partitioning="hive"
    )
    columns = [
        "trip_id",
        "enabled_code",
        "event_type",
        "event_name",
        "event_start_time",
        "event_start_time_quality",
        "observation_count",
        "first_observed_telemetry_timestamp",
        "last_observed_telemetry_timestamp",
        "payload_variant_count",
        "end_time_variant_count",
        "payload_conflict",
        "end_time_conflict",
        "start_time_ambiguous",
        "start_year_mismatch_observed",
        "start_after_telemetry_observed",
    ]
    onsets = []
    for batch in dataset.to_batches(columns=columns, batch_size=20_000):
        for row in batch.to_pylist():
            if row.get("trip_id") is None or row.get("event_type") is None:
                continue
            onsets.append(
                OnsetRecord(
                    trip_id=row["trip_id"],
                    vehicle_key=row.get("enabled_code") or "MISSING_VEHICLE",
                    event_type=int(row["event_type"]),
                    event_name=row.get("event_name") or "unknown",
                    start=row.get("event_start_time"),
                    start_quality=row["event_start_time_quality"],
                    observation_count=int(row["observation_count"]),
                    first_observed=row.get("first_observed_telemetry_timestamp"),
                    last_observed=row.get("last_observed_telemetry_timestamp"),
                    payload_variant_count=int(row["payload_variant_count"]),
                    end_variant_count=int(row["end_time_variant_count"]),
                    payload_conflict=bool(row["payload_conflict"]),
                    end_conflict=bool(row["end_time_conflict"]),
                    start_ambiguous=bool(row["start_time_ambiguous"]),
                    year_mismatch=bool(row["start_year_mismatch_observed"]),
                    start_after_telemetry=bool(row["start_after_telemetry_observed"]),
                )
            )
    return onsets, dataset.count_rows()


OBSERVATION_COLUMNS = [
    "trip_id",
    "enabled_code",
    "telemetry_timestamp",
    "event_type",
    "event_name",
    "event_start_time",
    "event_start_time_quality",
    "event_end_time",
    "event_end_time_quality",
    "event_duration_s",
    "event_duration_s_quality",
    "event_speed_difference",
    "event_speed_difference_quality",
    "event_speed_limit_kph",
    "event_speed_limit_kph_quality",
    "event_speed_kph",
    "event_speed_kph_quality",
    "event_dtc_count",
    "event_dtc_count_quality",
    "event_water_temp_c",
    "event_water_temp_c_quality",
    "event_engine_load_pct",
    "event_engine_load_pct_quality",
    "event_dtc_code_0_raw",
    "event_dtc_code_0_quality",
]


def _load_observations(config: LabelResearchConfig) -> tuple[list[dict[str, Any]], int]:
    dataset = ds.dataset(
        config.interim_root / "events_observed", format="parquet", partitioning="hive"
    )
    rows: list[dict[str, Any]] = []
    for batch in dataset.to_batches(columns=OBSERVATION_COLUMNS, batch_size=20_000):
        rows.extend(batch.to_pylist())
    return rows, dataset.count_rows()


def _trusted_onset(onset: OnsetRecord, trips: dict[str, TripRecord]) -> bool:
    trip = trips.get(onset.trip_id)
    return bool(
        trip
        and onset.start is not None
        and onset.start_quality == "valid"
        and not onset.start_ambiguous
        and not onset.year_mismatch
        and trip.start <= onset.start <= trip.end
    )


def _event_inventory(
    observations: list[dict[str, Any]],
    onsets: list[OnsetRecord],
    trips: dict[str, TripRecord],
) -> dict[str, Any]:
    observation_stats: dict[int, Counter] = defaultdict(Counter)
    for row in observations:
        event_type = int(row["event_type"])
        stats = observation_stats[event_type]
        stats["observation_count"] += 1
        stats["duration_available"] += row["event_duration_s_quality"] == "valid"
        stats["end_time_available"] += row["event_end_time_quality"] == "valid"

    onset_stats: dict[int, dict[str, Any]] = defaultdict(
        lambda: {
            "candidate_count": 0,
            "trusted_count": 0,
            "trips": set(),
            "vehicles": set(),
            "dates": [],
            "repeated_candidate_count": 0,
            "repeated_observation_count": 0,
            "payload_variant_candidate_count": 0,
            "payload_conflict_candidate_count": 0,
            "end_conflict_candidate_count": 0,
            "intervals": [],
            "by_trip": defaultdict(list),
        }
    )
    for onset in onsets:
        stats = onset_stats[onset.event_type]
        stats["candidate_count"] += 1
        stats["repeated_candidate_count"] += onset.observation_count > 1
        stats["repeated_observation_count"] += max(onset.observation_count - 1, 0)
        stats["payload_variant_candidate_count"] += onset.payload_variant_count > 1
        stats["payload_conflict_candidate_count"] += onset.payload_conflict
        stats["end_conflict_candidate_count"] += onset.end_conflict
        if _trusted_onset(onset, trips):
            stats["trusted_count"] += 1
            stats["trips"].add(onset.trip_id)
            stats["vehicles"].add(onset.vehicle_key)
            stats["dates"].append(onset.start)
            stats["by_trip"][onset.trip_id].append(onset.start)

    total_distance = sum(
        trip.distance_km or 0
        for trip in trips.values()
        if trip.distance_km is not None and trip.distance_quality in {"valid", "degraded"}
    )
    total_driving_hours = sum(
        trip.driving_hours or 0
        for trip in trips.values()
        if trip.driving_hours is not None
        and trip.driving_hours_quality in {"valid", "degraded"}
    )
    results = []
    for event_type in range(1, 21):
        obs, stats = observation_stats[event_type], onset_stats[event_type]
        intervals = []
        for values in stats["by_trip"].values():
            ordered = sorted(set(values))
            intervals.extend(
                (right - left).total_seconds() for left, right in zip(ordered, ordered[1:])
            )
        trusted = stats["trusted_count"]
        observation_count = obs["observation_count"]
        results.append(
            {
                "event_type": event_type,
                "event_name": EVENT_NAMES.get(event_type, "unknown"),
                "observation_count": observation_count,
                "onset_candidate_count": stats["candidate_count"],
                "trusted_onset_count": trusted,
                "unique_trips": len(stats["trips"]),
                "unique_vehicles": len(stats["vehicles"]),
                "first_date": min(stats["dates"]).date().isoformat() if stats["dates"] else None,
                "last_date": max(stats["dates"]).date().isoformat() if stats["dates"] else None,
                "repeated_candidate_count": stats["repeated_candidate_count"],
                "repeated_observation_count": stats["repeated_observation_count"],
                "payload_variant_candidate_count": stats[
                    "payload_variant_candidate_count"
                ],
                "payload_conflict_candidate_count": stats[
                    "payload_conflict_candidate_count"
                ],
                "end_conflict_candidate_count": stats["end_conflict_candidate_count"],
                "duration_available_observations": obs["duration_available"],
                "duration_availability_rate": (
                    obs["duration_available"] / observation_count if observation_count else None
                ),
                "end_time_available_observations": obs["end_time_available"],
                "end_time_availability_rate": (
                    obs["end_time_available"] / observation_count if observation_count else None
                ),
                "within_trip_interval_seconds": _quantiles(intervals),
                "events_per_all_trips": trusted / len(trips) if trips else None,
                "events_per_affected_trip": (
                    trusted / len(stats["trips"]) if stats["trips"] else None
                ),
                "events_per_100km": trusted / total_distance * 100 if total_distance > 0 else None,
                "events_per_driving_hour": (
                    trusted / total_driving_hours if total_driving_hours > 0 else None
                ),
            }
        )
    return {
        "inventory_version": "event-inventory-v1",
        "rate_denominators": {
            "valid_or_degraded_distance_km": total_distance,
            "valid_or_degraded_driving_hours": total_driving_hours,
            "trip_count": len(trips),
        },
        "events": results,
    }


PAYLOAD_FIELDS = (
    "event_end_time",
    "event_duration_s",
    "event_speed_difference",
    "event_speed_limit_kph",
    "event_speed_kph",
    "event_dtc_count",
    "event_water_temp_c",
    "event_engine_load_pct",
    "event_dtc_code_0_raw",
)


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = {}
    for field in PAYLOAD_FIELDS:
        quality = row.get(f"{field}_quality")
        value = row.get(field)
        if value is not None and (quality == "valid" or field == "event_dtc_code_0_raw"):
            payload[field] = value
    return payload


def _classify_identity_group(rows: list[dict[str, Any]]) -> str:
    if len(rows) == 1:
        return "singleton"
    rows = sorted(rows, key=lambda row: row["telemetry_timestamp"])
    payloads = [_payload(row) for row in rows]
    signatures = {tuple(sorted(payload.items())) for payload in payloads}
    if len(signatures) == 1:
        return "exact_repetition"
    common_fields = set.intersection(*(set(payload) for payload in payloads)) if payloads else set()
    completed_later = (
        payloads[0].get("event_end_time") is None
        and payloads[-1].get("event_end_time") is not None
        and payloads[0].get("event_duration_s") is None
        and payloads[-1].get("event_duration_s") is not None
    )
    if completed_later:
        changing_non_temporal = any(
            len({payload[field] for payload in payloads if field in payload}) > 1
            for field in set().union(*(set(payload) for payload in payloads))
            - {"event_end_time", "event_duration_s"}
        )
        return (
            "completion_with_evolving_payload"
            if changing_non_temporal
            else "enrichment_or_completion"
        )
    conflicting_common = any(
        len({payload[field] for payload in payloads}) > 1
        for field in common_fields - {"event_end_time", "event_duration_s"}
    )
    if conflicting_common:
        return "payload_conflict"
    presence = [set(payload) for payload in payloads]
    if all(left <= right for left, right in zip(presence, presence[1:])):
        return "enrichment_or_completion"
    durations = [payload.get("event_duration_s") for payload in payloads]
    durations = [value for value in durations if value is not None]
    if durations == sorted(durations):
        return "evolving_duration_or_end"
    return "payload_conflict"


def _identity_audit(observations: list[dict[str, Any]], onsets: list[OnsetRecord]) -> dict:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    ungroupable = 0
    for row in observations:
        if row.get("event_start_time") is None or row.get("event_start_time_quality") != "valid":
            ungroupable += 1
            continue
        groups[(row["trip_id"], row["event_type"], row["event_start_time"])].append(row)
    classifications = Counter()
    by_event: dict[int, Counter] = defaultdict(Counter)
    for (_, event_type, _), rows in groups.items():
        classification = _classify_identity_group(rows)
        classifications[classification] += 1
        by_event[int(event_type)][classification] += 1
    core = [onset for onset in onsets if onset.event_type in CORE_TYPES]
    core_conflicts = sum(
        onset.payload_conflict or onset.end_conflict or onset.start_ambiguous for onset in core
    )
    return {
        "identity_rule_audited": "trip + event_type + valid event_start_time",
        "reconstructed_identity_groups": len(groups),
        "phase2_onset_candidates": len(onsets),
        "ungroupable_observations": ungroupable,
        "classification_counts": dict(sorted(classifications.items())),
        "classification_by_event": [
            {
                "event_type": event_type,
                "event_name": EVENT_NAMES.get(event_type, "unknown"),
                "classifications": dict(sorted(counts.items())),
            }
            for event_type, counts in sorted(by_event.items())
        ],
        "core_candidate_conflict_or_ambiguity_count": core_conflicts,
        "recommendation": (
            "Phase 2 events_onsets is sufficient as the Phase 3B source for the five core "
            "onset labels after quarantining year/start anomalies. Duration-dependent labels "
            "must rejoin observation payload and define when the duration threshold becomes observable."
        ),
    }


def _speeding_research(
    observations: list[dict[str, Any]], onsets: list[OnsetRecord], trips: dict[str, TripRecord]
) -> dict:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in observations:
        if (
            row.get("event_type") == 8
            and row.get("event_start_time") is not None
            and row.get("event_start_time_quality") == "valid"
        ):
            groups[(row["trip_id"], 8, row["event_start_time"])].append(row)
    trusted_keys = {
        (onset.trip_id, 8, onset.start)
        for onset in onsets
        if onset.event_type == 8 and _trusted_onset(onset, trips)
    }
    durations: list[float] = []
    excesses: list[float] = []
    episodes: list[tuple[float | None, float | None]] = []
    repeated = payload_variant = 0
    for key in trusted_keys:
        rows = groups.get(key, [])
        valid_durations = [
            float(row["event_duration_s"])
            for row in rows
            if row.get("event_duration_s_quality") == "valid"
            and row.get("event_duration_s") is not None
        ]
        valid_excess = [
            float(row["event_speed_kph"] - row["event_speed_limit_kph"])
            for row in rows
            if row.get("event_speed_kph_quality") == "valid"
            and row.get("event_speed_limit_kph_quality") == "valid"
            and row.get("event_speed_limit_kph") is not None
            and row["event_speed_limit_kph"] > 0
        ]
        duration = max(valid_durations) if valid_durations else None
        excess = max(valid_excess) if valid_excess else None
        if duration is not None:
            durations.append(duration)
        if excess is not None:
            excesses.append(excess)
        episodes.append((duration, excess))
        repeated += len(rows) > 1
        payload_variant += len({tuple(sorted(_payload(row).items())) for row in rows}) > 1

    duration_thresholds = (10, 30, 60)
    excess_thresholds = (5, 10, 20)
    speeding_onsets = [onset for onset in onsets if onset.event_type == 8]
    return {
        "event_name": "speeding",
        "trusted_episode_count": len(trusted_keys),
        "duration_available_episodes": len(durations),
        "duration_availability_rate": len(durations) / len(trusted_keys) if trusted_keys else None,
        "duration_seconds": _quantiles(durations),
        "excess_speed_available_episodes": len(excesses),
        "excess_speed_availability_rate": len(excesses) / len(trusted_keys) if trusted_keys else None,
        "excess_speed_kph": _quantiles(excesses),
        "repeated_episode_count": repeated,
        "payload_variant_episode_count": payload_variant,
        "onset_reliability": {
            "phase2_candidate_count": len(speeding_onsets),
            "trusted_within_trip_count": len(trusted_keys),
            "outside_trip_or_other_excluded_count": len(speeding_onsets) - len(trusted_keys),
            "start_ambiguous_count": sum(onset.start_ambiguous for onset in speeding_onsets),
            "year_mismatch_count": sum(onset.year_mismatch for onset in speeding_onsets),
            "repeated_candidate_count": sum(
                onset.observation_count > 1 for onset in speeding_onsets
            ),
            "phase2_payload_conflict_count": sum(
                onset.payload_conflict for onset in speeding_onsets
            ),
            "interpretation": (
                "Repeated speeding observations are overwhelmingly onset/completion records "
                "with evolving speed payload, not evidence that the onset identity itself is duplicated."
            ),
        },
        "candidate_a_any_onset": {"episode_count": len(trusted_keys), "status": "available"},
        "candidate_b_duration": {
            "status": "experimental_not_official_thresholds",
            "threshold_counts": {
                f"duration_gte_{threshold}s": sum(
                    duration is not None and duration >= threshold for duration, _ in episodes
                )
                for threshold in duration_thresholds
            },
        },
        "candidate_c_excess_speed": {
            "status": "experimental_not_official_thresholds",
            "threshold_counts": {
                f"excess_gte_{threshold}kph": sum(
                    excess is not None and excess >= threshold for _, excess in episodes
                )
                for threshold in excess_thresholds
            },
        },
        "candidate_d_duration_and_excess": {
            "status": "experimental_not_official_thresholds",
            "threshold_counts": {
                f"duration_gte_{duration_threshold}s_and_excess_gte_{excess_threshold}kph": sum(
                    duration is not None
                    and excess is not None
                    and duration >= duration_threshold
                    and excess >= excess_threshold
                    for duration, excess in episodes
                )
                for duration_threshold in duration_thresholds
                for excess_threshold in excess_thresholds
            },
        },
        "recommendation": (
            "Organizer speeding onset is empirically identifiable, but a sustained-speeding "
            "label remains experimental. Final duration is future information at raw onset; "
            "Phase 3B must define threshold-crossing time before V2 can become operational."
        ),
    }


SEQUENCE_PAIRS = (
    (8, 7),
    (8, 20),
    (20, 7),
    (12, 20),
    (16, 20),
    (6, 7),
)


def _sequence_statistics(onsets: list[OnsetRecord], trips: dict[str, TripRecord]) -> dict:
    by_trip_type: dict[str, dict[int, list[datetime]]] = defaultdict(lambda: defaultdict(list))
    for onset in onsets:
        if _trusted_onset(onset, trips):
            by_trip_type[onset.trip_id][onset.event_type].append(onset.start)
    for types in by_trip_type.values():
        for values in types.values():
            values.sort()
    results = []
    for source_type, target_type in SEQUENCE_PAIRS:
        source_total = sum(len(types.get(source_type, [])) for types in by_trip_type.values())
        for horizon in (30, 60, 120, 300):
            matched_sources = pair_occurrences = 0
            matched_trips: set[str] = set()
            for trip_id, types in by_trip_type.items():
                targets = types.get(target_type, [])
                for source_time in types.get(source_type, []):
                    upper = source_time.timestamp() + horizon
                    matches = [
                        target
                        for target in targets
                        if source_time < target and target.timestamp() <= upper
                    ]
                    if matches:
                        matched_sources += 1
                        pair_occurrences += len(matches)
                        matched_trips.add(trip_id)
            results.append(
                {
                    "source_event": EVENT_NAMES[source_type],
                    "target_event": EVENT_NAMES[target_type],
                    "horizon_seconds": horizon,
                    "source_onset_count": source_total,
                    "source_onsets_with_target": matched_sources,
                    "association_rate": matched_sources / source_total if source_total else None,
                    "pair_occurrence_count": pair_occurrences,
                    "trips_with_association": len(matched_trips),
                }
            )
    return {
        "interpretation": "Temporal association only; no causal claim.",
        "sequences": results,
    }


def _history_audit(trips: dict[str, TripRecord]) -> dict:
    diagnostics = []
    duplicate_rows = 0
    for trip in trips.values():
        duplicate_rows += trip.raw_row_count - len(trip.timestamps)
        for anchor in telemetry_aligned_anchors(trip.timestamps, "every_telemetry_point"):
            diagnostics.append(history_diagnostics(trip.timestamps, anchor))
    return {
        "anchor_basis": "unique existing telemetry timestamps; no interpolation",
        "anchor_count": len(diagnostics),
        "duplicate_timestamp_rows_suppressed": duplicate_rows,
        "elapsed_history_seconds": _quantiles(
            item["elapsed_history_seconds"] for item in diagnostics
        ),
        "telemetry_point_count": _quantiles(
            item["telemetry_point_count"] for item in diagnostics
        ),
        "max_gap_seconds": _quantiles(item["max_gap_seconds"] for item in diagnostics),
        "sampling_density_points_per_minute": _quantiles(
            item["sampling_density_points_per_minute"] for item in diagnostics
        ),
        "policy_pass_counts": {
            policy: sum(history_is_valid(item, policy) for item in diagnostics)
            for policy in (
                "strict_300s",
                "minimum_240s",
                "minimum_10_points",
                "recommended_240s_10points_gap60s",
            )
        },
    }


def _timezone_audit(trips: dict[str, TripRecord]) -> dict:
    offsets = Counter()
    parseable = 0
    for trip in trips.values():
        try:
            encoded = datetime.strptime(str(trip.journey_code)[:8], "%y%m%d%H")
        except (TypeError, ValueError):
            continue
        parseable += 1
        offsets[str(math.floor((trip.start - encoded).total_seconds() / 3600))] += 1
    mode, mode_count = offsets.most_common(1)[0] if offsets else (None, 0)
    support = mode_count / parseable if parseable else None
    return {
        "journey_code_parseable_trips": parseable,
        "floor_hour_offset_distribution": dict(
            sorted(offsets.items(), key=lambda item: int(item[0]))
        ),
        "modal_floor_hour_offset": int(mode) if mode is not None else None,
        "modal_support_rate": support,
        "assessment": (
            "Strong empirical evidence that telemetry timestamps are approximately UTC+8 "
            "relative to the organizer-documented UTC journey-code hour. This is not an "
            "organizer-confirmed timezone definition; hour/weekday results remain experimental."
            if support is not None and support >= 0.95 and mode == "8"
            else "UNRESOLVED: offset evidence is not sufficiently concentrated."
        ),
    }


def _quality_impact(
    observations: list[dict[str, Any]],
    onsets: list[OnsetRecord],
    trips: dict[str, TripRecord],
) -> dict:
    duration_invalid = sum(
        row["event_duration_s_quality"] in {"malformed", "out_of_range"}
        for row in observations
    )
    return {
        "observed_counts": {
            "gps_speed_out_of_range_rows": sum(
                trip.gps_speed_out_of_range_rows for trip in trips.values()
            ),
            "azimuth_out_of_range_rows": sum(
                trip.azimuth_out_of_range_rows for trip in trips.values()
            ),
            "missing_gps_rows": sum(trip.missing_gps_rows for trip in trips.values()),
            "duplicate_timestamp_rows": sum(
                trip.raw_row_count - len(trip.timestamps) for trip in trips.values()
            ),
            "malformed_telemetry_rows": sum(trip.malformed_rows for trip in trips.values()),
            "invalid_event_duration_observations": duration_invalid,
            "invalid_or_zero_duration_trips": sum(trip.invalid_duration for trip in trips.values()),
            "distance_unavailable_trips": sum(
                trip.distance_quality in {"invalid", "unavailable"} for trip in trips.values()
            ),
            "ultra_long_over_12h_trips": sum(trip.ultra_long for trip in trips.values()),
            "event_start_year_mismatch_candidates": sum(onset.year_mismatch for onset in onsets),
        },
        "recommended_policy": [
            {"issue": "GPS speed out of range", "action": "keep trip; flag/exclude affected feature cell or anchor feature dependency"},
            {"issue": "azimuth out of range", "action": "keep trip + flag; do not silently wrap 360 to 0"},
            {"issue": "missing GPS", "action": "keep trip + missingness flag; use reduced feature set"},
            {"issue": "duplicate timestamp", "action": "one canonical anchor per trip/timestamp; retain rows and duplicate audit flags"},
            {"issue": "invalid event duration", "action": "exclude duration-dependent candidate only; onset may remain if time identity is valid"},
            {"issue": "distance unavailable", "action": "keep label anchors; normalized rate unavailable"},
            {"issue": "zero-duration trip", "action": "exclude trip from anchor simulation"},
            {"issue": "ultra-long trip", "action": "exclude pending journey-boundary review"},
            {"issue": "1970 event timestamp", "action": "quarantine from all label simulations"},
        ],
    }


def _concentration(private: SimulationPrivateStats) -> dict:
    ordered = sorted(private.positive_by_vehicle.values(), reverse=True)
    total = sum(ordered)
    ranked = [
        {
            "anonymous_vehicle_rank": rank,
            "positive_anchors": count,
            "share": count / total if total else None,
            "cumulative_share": sum(ordered[:rank]) / total if total else None,
        }
        for rank, count in enumerate(ordered, 1)
    ]
    shares = [count / total for count in ordered] if total else []
    per_event = []
    for event_type, vehicle_counts in sorted(private.positive_by_event_vehicle.items()):
        event_ordered = sorted(vehicle_counts.values(), reverse=True)
        event_total = sum(event_ordered)
        event_shares = [count / event_total for count in event_ordered] if event_total else []
        per_event.append(
            {
                "event_type": event_type,
                "event_name": EVENT_NAMES[event_type],
                "positive_vehicle_count": len(event_ordered),
                "top_1_share": sum(event_ordered[:1]) / event_total if event_total else None,
                "top_3_share": sum(event_ordered[:3]) / event_total if event_total else None,
                "herfindahl_hirschman_index": sum(share * share for share in event_shares),
                "anonymous_ranked_positive_anchors": [
                    {"anonymous_vehicle_rank": rank, "positive_anchors": count}
                    for rank, count in enumerate(event_ordered, 1)
                ],
            }
        )
    return {
        "positive_vehicle_count": len(ordered),
        "top_1_share": sum(ordered[:1]) / total if total else None,
        "top_3_share": sum(ordered[:3]) / total if total else None,
        "top_5_share": sum(ordered[:5]) / total if total else None,
        "herfindahl_hirschman_index": sum(share * share for share in shares),
        "anonymous_ranked_distribution": ranked,
        "per_event_concentration": per_event,
        "note": "Ranks are recomputed from aggregate positive counts and contain no vehicle identifier or stable pseudonym.",
    }


def _rate_distribution(positive: dict[str, int], eligible: dict[str, int]) -> list[dict]:
    return [
        {
            "period": key,
            "eligible_anchors": eligible.get(key, 0),
            "positive_anchors": positive.get(key, 0),
            "positive_rate": positive.get(key, 0) / eligible[key] if eligible.get(key) else None,
        }
        for key in sorted(set(positive) | set(eligible))
    ]


def _split_support(
    trips: dict[str, TripRecord],
    onsets: list[OnsetRecord],
    train_end: datetime,
    validation_end: datetime,
) -> dict:
    trip_partition = {}
    trip_counts = Counter()
    vehicle_sets: dict[str, set[str]] = defaultdict(set)
    for trip in trips.values():
        partition = (
            "train"
            if trip.start <= train_end
            else "validation"
            if trip.start <= validation_end
            else "test"
        )
        trip_partition[trip.trip_id] = partition
        trip_counts[partition] += 1
        vehicle_sets[partition].add(trip.vehicle_key)
    event_counts: dict[str, Counter] = defaultdict(Counter)
    for onset in onsets:
        if onset.event_type in CORE_TYPES and _trusted_onset(onset, trips):
            event_counts[trip_partition[onset.trip_id]][str(onset.event_type)] += 1
    return {
        "train_end_inclusive": train_end.isoformat(sep=" "),
        "validation_end_inclusive": validation_end.isoformat(sep=" "),
        "partitions": {
            name: {
                "trip_count": trip_counts[name],
                "vehicle_count": len(vehicle_sets[name]),
                "core_onsets_by_event_type": dict(sorted(event_counts[name].items())),
            }
            for name in ("train", "validation", "test")
        },
    }


def _temporal_distribution(
    private: SimulationPrivateStats,
    trips: dict[str, TripRecord],
    onsets: list[OnsetRecord],
) -> dict:
    starts = sorted(trip.start for trip in trips.values())
    train_quantile = starts[int((len(starts) - 1) * 0.70)]
    validation_quantile = starts[int((len(starts) - 1) * 0.85)]
    calendar = _split_support(
        trips,
        onsets,
        datetime(2025, 8, 31, 23, 59, 59),
        datetime(2025, 10, 31, 23, 59, 59),
    )
    quantile = _split_support(trips, onsets, train_quantile, validation_quantile)
    early_months = {key for key in private.eligible_by_month if key <= "2025-06"}
    late_months = set(private.eligible_by_month) - early_months
    early_eligible = sum(private.eligible_by_month[key] for key in early_months)
    late_eligible = sum(private.eligible_by_month[key] for key in late_months)
    early_positive = sum(private.positive_by_month.get(key, 0) for key in early_months)
    late_positive = sum(private.positive_by_month.get(key, 0) for key in late_months)
    return {
        "reference_positive_by_month": _rate_distribution(
            private.positive_by_month, private.eligible_by_month
        ),
        "experimental_local_hour": _rate_distribution(
            private.positive_by_hour, private.eligible_by_hour
        ),
        "experimental_local_weekday": _rate_distribution(
            private.positive_by_weekday, private.eligible_by_weekday
        ),
        "hour_weekday_timezone_caveat": "Based on strong inferred UTC+8 evidence, not an organizer-confirmed timezone.",
        "early_vs_late": [
            {
                "period": "early_2025-01_to_2025-06",
                "eligible_anchors": early_eligible,
                "positive_anchors": early_positive,
                "positive_rate": early_positive / early_eligible if early_eligible else None,
            },
            {
                "period": "late_2025-07_to_2025-11",
                "eligible_anchors": late_eligible,
                "positive_anchors": late_positive,
                "positive_rate": late_positive / late_eligible if late_eligible else None,
            },
        ],
        "temporal_split_candidates": {
            "calendar_interpretable": calendar,
            "trip_start_70_15_15_quantiles": quantile,
        },
    }


def _class_balance(reference: dict, inventory: dict) -> dict:
    eligible = reference["eligible_anchors"]
    inventory_by_type = {item["event_type"]: item for item in inventory["events"]}
    per_event = []
    for event_type in CORE_TYPES:
        positives = int(reference["positives_by_event_type"].get(str(event_type), 0))
        negatives = eligible - positives
        support = inventory_by_type[event_type]
        insufficient = support["trusted_onset_count"] < 100 or support["unique_vehicles"] < 5
        per_event.append(
            {
                "event_type": event_type,
                "event_name": EVENT_NAMES[event_type],
                "positive_anchors": positives,
                "negative_anchors": negatives,
                "positive_rate": positives / eligible if eligible else None,
                "negative_to_positive_ratio": negatives / positives if positives else None,
                "trusted_onsets": support["trusted_onset_count"],
                "vehicles_with_onset": support["unique_vehicles"],
                "standalone_training_support": (
                    "insufficient_under_research_heuristic" if insufficient else "candidate"
                ),
            }
        )
    positives = reference["positive_anchors"]
    return {
        "reference_scenario": reference["scenario_name"],
        "any_risk": {
            "positive_anchors": positives,
            "negative_anchors": reference["negative_anchors"],
            "positive_rate": reference["positive_rate"],
            "negative_to_positive_ratio": (
                reference["negative_anchors"] / positives if positives else None
            ),
        },
        "per_event": per_event,
        "heuristic_note": "The >=100 onsets and >=5 vehicles screen is a research sufficiency flag, not a final modeling rule.",
    }


def _timestamp_anomaly(onsets: list[OnsetRecord], trips: dict[str, TripRecord]) -> dict:
    anomalies = [onset for onset in onsets if onset.year_mismatch]
    event_counts = Counter(onset.event_name for onset in anomalies)
    telemetry_years = Counter()
    observation_counts = Counter()
    for onset in anomalies:
        trip = trips.get(onset.trip_id)
        if trip:
            telemetry_years[str(trip.start.year)] += 1
        observation_counts[str(onset.observation_count)] += 1
    return {
        "year_mismatch_candidate_count": len(anomalies),
        "event_name_counts": dict(sorted(event_counts.items())),
        "source_event_year_counts": dict(
            sorted(Counter(str(onset.start.year) for onset in anomalies if onset.start).items())
        ),
        "associated_trip_telemetry_year_counts": dict(sorted(telemetry_years.items())),
        "observation_count_distribution": dict(sorted(observation_counts.items())),
        "recommendation": "quarantine",
        "rationale": (
            "The source onset parses syntactically but is outside its trip bounds and its year "
            "conflicts with telemetry. Preserve it in Phase 2, exclude it from all Phase 3 labels, "
            "and retain the aggregate audit flag."
        ),
    }


def run_label_research(config: LabelResearchConfig) -> dict[str, Any]:
    started = perf_counter()
    trips, telemetry_rows = _load_telemetry(config)
    _attach_trip_analytics(config, trips)
    onsets, onset_rows = _load_onsets(config)
    observations, observation_rows = _load_observations(config)

    inventory = _event_inventory(observations, onsets, trips)
    identity = _identity_audit(observations, onsets)
    speeding = _speeding_research(observations, onsets, trips)
    sequences = _sequence_statistics(onsets, trips)
    history = _history_audit(trips)
    timezone = _timezone_audit(trips)
    quality = _quality_impact(observations, onsets, trips)
    anomaly = _timestamp_anomaly(onsets, trips)

    simulations = []
    reference_private: SimulationPrivateStats | None = None
    for scenario in scenario_matrix():
        result, private = simulate_scenario(scenario, trips, onsets)
        simulations.append(result)
        if scenario.name == "reference":
            reference_private = private
    if reference_private is None:
        raise RuntimeError("Reference label simulation was not produced")
    reference = next(result for result in simulations if result["scenario_name"] == "reference")
    balance = _class_balance(reference, inventory)
    concentration = _concentration(reference_private)
    temporal = _temporal_distribution(reference_private, trips, onsets)
    evaluation = {
        "temporal_recommendation": {
            "preferred_candidate": "calendar_interpretable",
            "boundaries": temporal["temporal_split_candidates"]["calendar_interpretable"],
            "reason": (
                "Whole-trip January-July effective train, September validation, and November "
                "test are operationally readable. PCS has only two test-period onsets, so PCS "
                "must not receive a standalone test-performance claim."
            ),
        },
        "vehicle_generalization_options": [
            {
                "method": "fixed_grouped_holdout",
                "assessment": "Necessary for one sealed unseen-vehicle claim, but high variance with only 20 vehicles.",
            },
            {
                "method": "grouped_cross_validation",
                "assessment": "Recommended for development estimates; use approximately five vehicle-group folds without row leakage.",
            },
            {
                "method": "leave_one_or_few_vehicle_out",
                "assessment": "Use as sensitivity analysis; computationally feasible but folds have uneven event support.",
            },
        ],
        "recommended_vehicle_strategy": (
            "After labels are frozen, seal a small fixed unseen-vehicle test group and use grouped "
            "cross-validation only inside the remaining development vehicles. Do not select IDs "
            "after inspecting test outcomes."
        ),
        "final_assignments_created": False,
    }

    recommendation = {
        "recommended_label": "label_v0_core_any",
        "status": "recommended_for_human_approval_before_phase_3b",
        "definition": {
            "input": "[t-300s,t)",
            "label": "(t,t+60s]",
            "positive_events": [EVENT_NAMES[event_type] for event_type in CORE_TYPES],
            "event_7_name": "decelerate",
        },
        "anchor_cadence": "10s telemetry-aligned existing timestamps",
        "history_requirement": "at least 240s elapsed, at least 10 points, maximum 60s gap; no interpolation",
        "horizon_policy": (
            "full 60s must remain within trip and contain a future telemetry observation; "
            "trip-end failure is censored and missing future observation is ambiguous"
        ),
        "active_policy": "exclude empirically observed active interval; keep strict new-onset interval",
        "duplicate_policy": "one canonical anchor per trip/timestamp, retaining duplicate flags and rows for audit",
        "timestamp_anomaly_policy": "quarantine year-mismatch candidates",
        "speeding_v2_status": "experimental; not part of recommended V0",
        "phase2_onset_source": "sufficient for core labels after anomaly quarantine",
        "human_approval_required": True,
    }

    staging = config.output_root.with_name(f".{config.output_root.name}.staging-{uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    outputs = {
        "event_inventory.json": inventory,
        "speeding_research.json": speeding,
        "event_sequence_statistics.json": sequences,
        "event_identity_audit.json": identity,
        "timestamp_1970_audit.json": anomaly,
        "anchor_history_audit.json": history,
        "label_simulation.json": {
            "simulation_version": "label-simulation-v1",
            "aggregate_only": True,
            "training_features_created": False,
            "scenarios": simulations,
        },
        "class_balance.json": balance,
        "vehicle_concentration.json": concentration,
        "temporal_distribution.json": temporal,
        "evaluation_split_research.json": evaluation,
        "quality_impact.json": quality,
        "timezone_audit.json": timezone,
        "label_recommendation.json": recommendation,
    }
    try:
        for filename, payload in outputs.items():
            _write_json(staging / filename, payload)
        manifest = {
            "research_version": "phase-3a-label-research-v1",
            "status": "complete",
            "aggregate_only": True,
            "training_features_created": False,
            "model_training_performed": False,
            "input_counts": {
                "telemetry_rows": telemetry_rows,
                "event_observation_rows": observation_rows,
                "event_onset_candidate_rows": onset_rows,
                "trip_count": len(trips),
                "vehicle_count": len({trip.vehicle_key for trip in trips.values()}),
            },
            "output_files": sorted([*outputs, "label_research_manifest.json"]),
            "reference_simulation": {
                key: reference[key]
                for key in (
                    "eligible_anchors",
                    "positive_anchors",
                    "negative_anchors",
                    "positive_rate",
                    "censored",
                    "ambiguous",
                    "eligible_trips",
                    "positive_trips",
                    "eligible_vehicles",
                    "positive_vehicles",
                )
            },
            "elapsed_seconds": perf_counter() - started,
            "privacy": {
                "vehicle_identifiers_written": False,
                "telemetry_rows_written": False,
                "precise_locations_written": False,
            },
            "unresolved": [
                "Human approval of Label V0",
                "Organizer/domain validation of active and cooldown semantics",
                "Operational threshold-crossing definition for sustained speeding",
                "Official telemetry timezone",
                "Final temporal and vehicle evaluation assignments",
                "Ultra-long journey treatment",
            ],
        }
        _write_json(staging / "label_research_manifest.json", manifest)
        staging.replace(config.output_root)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return manifest
