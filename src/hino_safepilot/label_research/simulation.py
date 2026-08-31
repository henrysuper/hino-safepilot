"""Aggregate anchor/label simulation; deliberately creates no ML feature vectors."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Iterable

from .models import (
    LABEL_TYPES,
    OnsetRecord,
    ScenarioConfig,
    SimulationPrivateStats,
    TripRecord,
)


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def telemetry_aligned_anchors(timestamps: list[datetime], cadence: str) -> list[datetime]:
    """Select unique existing telemetry timestamps; never interpolate anchors."""

    unique = sorted(set(timestamps))
    if cadence == "every_telemetry_point":
        return unique
    seconds = int(cadence.removesuffix("s"))
    selected: list[datetime] = []
    for timestamp in unique:
        if not selected or (timestamp - selected[-1]).total_seconds() >= seconds:
            selected.append(timestamp)
    return selected


def history_diagnostics(timestamps: list[datetime], anchor: datetime) -> dict[str, float | int]:
    left_boundary = anchor - timedelta(seconds=300)
    left = bisect_left(timestamps, left_boundary)
    right = bisect_left(timestamps, anchor)
    history = timestamps[left:right]
    coverage = (anchor - history[0]).total_seconds() if history else 0.0
    points = [left_boundary, *history, anchor]
    max_gap = max(
        ((right_time - left_time).total_seconds() for left_time, right_time in zip(points, points[1:])),
        default=300.0,
    )
    return {
        "elapsed_history_seconds": coverage,
        "telemetry_point_count": len(history),
        "max_gap_seconds": max_gap,
        "sampling_density_points_per_minute": len(history) / 5.0,
    }


def history_is_valid(diagnostics: dict[str, float | int], policy: str) -> bool:
    coverage = float(diagnostics["elapsed_history_seconds"])
    count = int(diagnostics["telemetry_point_count"])
    max_gap = float(diagnostics["max_gap_seconds"])
    if policy == "strict_300s":
        return coverage >= 300
    if policy == "minimum_240s":
        return coverage >= 240
    if policy == "minimum_10_points":
        return count >= 10
    if policy == "recommended_240s_10points_gap60s":
        return coverage >= 240 and count >= 10 and max_gap <= 60
    raise ValueError(f"Unknown history policy: {policy}")


def future_horizon_status(
    timestamps: list[datetime], anchor: datetime, policy: str
) -> tuple[str, float]:
    horizon = anchor + timedelta(seconds=60)
    if timestamps[-1] < horizon:
        return "censored", 60.0
    right = bisect_right(timestamps, horizon)
    left = bisect_right(timestamps, anchor)
    points = [anchor, *timestamps[left:right], horizon]
    max_gap = max(
        ((right_time - left_time).total_seconds() for left_time, right_time in zip(points, points[1:])),
        default=60.0,
    )
    future_observations = timestamps[left:right]
    if policy == "trip_end_only_60s":
        return "valid", max_gap
    if policy == "full_60s_with_future_observation":
        return ("valid" if future_observations else "ambiguous"), max_gap
    if policy == "full_60s_max_gap30s":
        return ("valid" if max_gap <= 30 else "ambiguous"), max_gap
    raise ValueError(f"Unknown horizon policy: {policy}")


def _increment(mapping: dict[str, int], key: str) -> None:
    mapping[key] = mapping.get(key, 0) + 1


def _target_index(
    onset_rows: Iterable[OnsetRecord], trips: dict[str, TripRecord]
) -> tuple[
    dict[str, dict[int, list[datetime]]],
    dict[str, dict[int, list[tuple[datetime, datetime]]]],
]:
    times: dict[str, dict[int, list[datetime]]] = defaultdict(lambda: defaultdict(list))
    intervals: dict[str, dict[int, list[tuple[datetime, datetime]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for onset in onset_rows:
        trip = trips.get(onset.trip_id)
        if (
            not trip
            or onset.start is None
            or onset.start_quality != "valid"
            or onset.start_ambiguous
            or onset.year_mismatch
            or not (trip.start <= onset.start <= trip.end)
        ):
            continue
        times[onset.trip_id][onset.event_type].append(onset.start)
        active_end = onset.last_observed
        if active_end is not None and active_end > onset.start:
            intervals[onset.trip_id][onset.event_type].append((onset.start, active_end))
    for trip_types in times.values():
        for values in trip_types.values():
            values.sort()
    for trip_types in intervals.values():
        for values in trip_types.values():
            values.sort()
    return times, intervals


def simulate_scenario(
    scenario: ScenarioConfig,
    trips: dict[str, TripRecord],
    onset_rows: list[OnsetRecord],
) -> tuple[dict, SimulationPrivateStats]:
    target_types = LABEL_TYPES[scenario.label_version]
    onset_times, active_intervals = _target_index(onset_rows, trips)
    counts = Counter()
    positives_by_event = Counter()
    eligible_trips: set[str] = set()
    positive_trips: set[str] = set()
    eligible_vehicles: set[str] = set()
    positive_vehicles: set[str] = set()
    private = SimulationPrivateStats()

    for trip in trips.values():
        anchors = telemetry_aligned_anchors(trip.timestamps, scenario.anchor_cadence)
        counts["candidate_anchors"] += len(anchors)
        counts["duplicate_timestamp_rows_suppressed"] += (
            trip.raw_row_count - len(set(trip.timestamps))
        )
        for anchor in anchors:
            if scenario.quality_policy == "exclude_invalid_or_ultralong_trip" and (
                trip.invalid_duration or trip.ultra_long
            ):
                counts["excluded_quality"] += 1
                continue
            diagnostics = history_diagnostics(trip.timestamps, anchor)
            if not history_is_valid(diagnostics, scenario.history_policy):
                counts["excluded_history"] += 1
                continue
            horizon_status, _ = future_horizon_status(
                trip.timestamps, anchor, scenario.horizon_policy
            )
            if horizon_status == "censored":
                counts["censored"] += 1
                continue
            if horizon_status == "ambiguous":
                counts["ambiguous"] += 1
                continue

            if scenario.active_policy == "active_state_exclusion":
                active = any(
                    start < anchor <= end
                    for event_type in target_types
                    for start, end in active_intervals[trip.trip_id].get(event_type, [])
                )
                if active:
                    counts["excluded_active"] += 1
                    continue
            elif scenario.active_policy == "cooldown_60s":
                cooling = False
                lower = anchor - timedelta(seconds=60)
                for event_type in target_types:
                    values = onset_times[trip.trip_id].get(event_type, [])
                    if bisect_right(values, anchor) > bisect_right(values, lower):
                        cooling = True
                        break
                if cooling:
                    counts["excluded_cooldown"] += 1
                    continue
            elif scenario.active_policy != "strict_onset_only":
                raise ValueError(f"Unknown active policy: {scenario.active_policy}")

            counts["eligible_anchors"] += 1
            eligible_trips.add(trip.trip_id)
            eligible_vehicles.add(trip.vehicle_key)
            _increment(private.eligible_by_vehicle, trip.vehicle_key)
            month = anchor.strftime("%Y-%m")
            hour = f"{anchor.hour:02d}"
            weekday = WEEKDAYS[anchor.weekday()]
            _increment(private.eligible_by_month, month)
            _increment(private.eligible_by_hour, hour)
            _increment(private.eligible_by_weekday, weekday)

            future_types: list[int] = []
            horizon = anchor + timedelta(seconds=60)
            for event_type in target_types:
                values = onset_times[trip.trip_id].get(event_type, [])
                if bisect_right(values, horizon) > bisect_right(values, anchor):
                    future_types.append(event_type)
                    positives_by_event[str(event_type)] += 1
                    event_vehicle = private.positive_by_event_vehicle.setdefault(
                        event_type, {}
                    )
                    event_vehicle[trip.vehicle_key] = (
                        event_vehicle.get(trip.vehicle_key, 0) + 1
                    )
            if future_types:
                counts["positive_anchors"] += 1
                positive_trips.add(trip.trip_id)
                positive_vehicles.add(trip.vehicle_key)
                _increment(private.positive_by_vehicle, trip.vehicle_key)
                _increment(private.positive_by_month, month)
                _increment(private.positive_by_hour, hour)
                _increment(private.positive_by_weekday, weekday)
            else:
                counts["negative_anchors"] += 1

    eligible = counts["eligible_anchors"]
    result = {
        "scenario_name": scenario.name,
        "varied_factor": scenario.factor,
        "configuration": {
            "anchor_cadence": scenario.anchor_cadence,
            "label_version": scenario.label_version,
            "active_policy": scenario.active_policy,
            "history_policy": scenario.history_policy,
            "horizon_policy": scenario.horizon_policy,
            "quality_policy": scenario.quality_policy,
            "input_interval": "[t-300s,t)",
            "label_interval": "(t,t+60s]",
        },
        "candidate_anchors": counts["candidate_anchors"],
        "eligible_anchors": eligible,
        "positive_anchors": counts["positive_anchors"],
        "negative_anchors": counts["negative_anchors"],
        "positive_rate": counts["positive_anchors"] / eligible if eligible else None,
        "censored": counts["censored"],
        "ambiguous": counts["ambiguous"],
        "excluded": {
            "quality": counts["excluded_quality"],
            "history": counts["excluded_history"],
            "active": counts["excluded_active"],
            "cooldown": counts["excluded_cooldown"],
        },
        "duplicate_timestamp_rows_suppressed": counts[
            "duplicate_timestamp_rows_suppressed"
        ],
        "positives_by_event_type": dict(sorted(positives_by_event.items())),
        "eligible_trips": len(eligible_trips),
        "positive_trips": len(positive_trips),
        "eligible_vehicles": len(eligible_vehicles),
        "positive_vehicles": len(positive_vehicles),
    }
    return result, private


def scenario_matrix() -> list[ScenarioConfig]:
    reference = ScenarioConfig(name="reference", factor="reference")
    scenarios = [reference]
    scenarios.extend(
        ScenarioConfig(
            name=f"anchor_{cadence}", factor="anchor_cadence", anchor_cadence=cadence
        )
        for cadence in ("every_telemetry_point", "5s", "15s", "30s")
    )
    scenarios.extend(
        ScenarioConfig(name=f"label_{version}", factor="label_version", label_version=version)
        for version in (
            "label_v1_core_multilabel",
            "label_v2_context_expanded_experimental",
        )
    )
    scenarios.extend(
        ScenarioConfig(name=f"active_{policy}", factor="active_policy", active_policy=policy)
        for policy in ("strict_onset_only", "cooldown_60s")
    )
    scenarios.extend(
        ScenarioConfig(name=f"history_{policy}", factor="history_policy", history_policy=policy)
        for policy in ("strict_300s", "minimum_240s", "minimum_10_points")
    )
    scenarios.append(
        ScenarioConfig(
            name="horizon_trip_end_only",
            factor="horizon_policy",
            horizon_policy="trip_end_only_60s",
        )
    )
    scenarios.append(
        ScenarioConfig(
            name="horizon_max_gap30s",
            factor="horizon_policy",
            horizon_policy="full_60s_max_gap30s",
        )
    )
    scenarios.append(
        ScenarioConfig(
            name="quality_keep_all_timestamp_valid",
            factor="quality_policy",
            quality_policy="keep_all_timestamp_valid",
        )
    )
    return scenarios
