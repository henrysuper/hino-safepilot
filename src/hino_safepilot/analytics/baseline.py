"""Past-only vehicle-linked behavior baselines."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from .models import BehaviorBaseline, BehaviorMetric, QualityStatus, TripSummary


BASELINE_EVENT_METRICS = (
    "decelerate",
    "accelerate",
    "speeding",
    "engine_overloading",
    "ldws",
    "pcs",
    "fatigue",
    "distract",
    "phone",
)


def _event_count(summary: TripSummary, event_name: str) -> int:
    return next((event.count for event in summary.events if event.event_name == event_name), 0)


def _aggregate_metric(
    name: str, trips: list[TripSummary], minimum_trip_count: int
) -> BehaviorMetric:
    warnings: list[str] = []
    if name in BASELINE_EVENT_METRICS:
        usable = [
            trip
            for trip in trips
            if trip.basics.distance_km.value is not None
            and trip.basics.distance_km.value > 0
            and trip.basics.distance_km.quality_status
            not in {QualityStatus.INVALID, QualityStatus.UNAVAILABLE}
        ]
        distance = sum(trip.basics.distance_km.value or 0 for trip in usable)
        count = sum(_event_count(trip, name) for trip in usable)
        value = count / distance * 100 if distance > 0 else None
        unit = "events_per_100km"
        exposure = distance
    elif name == "idle":
        usable = [
            trip
            for trip in trips
            if trip.basics.driving_hours.value is not None
            and trip.basics.idle_minutes.value is not None
        ]
        idle_minutes = sum(trip.basics.idle_minutes.value or 0 for trip in usable)
        driving_minutes = sum((trip.basics.driving_hours.value or 0) * 60 for trip in usable)
        exposure = idle_minutes + driving_minutes
        value = idle_minutes / exposure * 100 if exposure > 0 else None
        unit = "idle_pct"
    elif name == "fuel_efficiency":
        usable = [
            trip
            for trip in trips
            if trip.basics.distance_km.value is not None
            and trip.fuel.fuel_used_l.value is not None
            and trip.fuel.fuel_used_l.value > 0
        ]
        distance = sum(trip.basics.distance_km.value or 0 for trip in usable)
        fuel = sum(trip.fuel.fuel_used_l.value or 0 for trip in usable)
        exposure = fuel
        value = distance / fuel if distance > 0 and fuel > 0 else None
        unit = "km_per_l"
    else:
        raise ValueError(f"Unsupported behavior metric: {name}")

    if value is None:
        status = QualityStatus.UNAVAILABLE
        warnings.append("NO_VALID_EXPOSURE")
    elif len(usable) < minimum_trip_count:
        status = QualityStatus.DEGRADED
        warnings.append("INSUFFICIENT_TRIP_SUPPORT")
    elif any(trip.quality_warnings for trip in usable):
        status = QualityStatus.DEGRADED
        warnings.append("CONTRIBUTING_TRIP_QUALITY_WARNING")
    else:
        status = QualityStatus.VALID
    return BehaviorMetric(
        name=name,
        value=value,
        unit=unit,
        quality_status=status,
        contributing_trip_count=len(usable),
        exposure_value=exposure,
        warnings=warnings,
    )


def build_behavior_baseline(
    summaries: Iterable[TripSummary],
    *,
    vehicle_profile_id: str,
    cutoff_exclusive: datetime,
    previous_days: int | None = None,
    previous_n_trips: int | None = None,
    minimum_trip_count: int = 2,
) -> BehaviorBaseline:
    """Aggregate only trips ending strictly before the requested cutoff."""

    if (previous_days is None) == (previous_n_trips is None):
        raise ValueError("Specify exactly one of previous_days or previous_n_trips")
    eligible = sorted(
        (
            summary
            for summary in summaries
            if summary.vehicle_profile_id == vehicle_profile_id
            and summary.basics.trip_end < cutoff_exclusive
        ),
        key=lambda summary: summary.basics.trip_start,
    )
    if previous_days is not None:
        lower = cutoff_exclusive - timedelta(days=previous_days)
        eligible = [summary for summary in eligible if summary.basics.trip_start >= lower]
        kind, value = "previous_days", previous_days
    else:
        eligible = eligible[-int(previous_n_trips or 0) :]
        kind, value = "previous_n_trips", int(previous_n_trips or 0)
    metric_names = (*BASELINE_EVENT_METRICS, "idle", "fuel_efficiency")
    metrics = [
        _aggregate_metric(name, eligible, minimum_trip_count) for name in metric_names
    ]
    warnings = []
    if len(eligible) < minimum_trip_count:
        warnings.append("INSUFFICIENT_TRIP_SUPPORT")
    return BehaviorBaseline(
        vehicle_profile_id=vehicle_profile_id,
        cutoff_exclusive=cutoff_exclusive,
        window_kind=kind,
        window_value=value,
        trip_count=len(eligible),
        earliest_trip_start=eligible[0].basics.trip_start if eligible else None,
        latest_trip_end=max((trip.basics.trip_end for trip in eligible), default=None),
        metrics=metrics,
        quality_warnings=warnings,
    )
