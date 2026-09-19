"""Deterministic trip metrics; never used for future-event prediction features."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Iterable

from .models import (
    EventMetric,
    FuelMetrics,
    NumericMetric,
    QualityStatus,
    TelemetryQualitySummary,
    TripBasics,
    TripSummary,
)
from .post_trip_analytics import build_event_contexts


TRACKED_EVENT_NAMES = (
    "accelerate",
    "decelerate",
    "speeding",
    "engine_overloading",
    "fatigue",
    "seatbelt",
    "phone",
    "distract",
    "pcs",
    "ldws",
)

NOTABLE_EVENT_PRIORITY = {
    "pcs": 0,
    "fatigue": 1,
    "distract": 2,
    "phone": 3,
    "ldws": 4,
    "speeding": 5,
    "decelerate": 6,
    "accelerate": 7,
    "engine_overloading": 8,
    "seatbelt": 9,
}


@dataclass(frozen=True)
class TripAnalyticsConfig:
    maximum_exposure_gap_seconds: float = 300.0
    distance_step_floor_km: float = 5.0
    maximum_plausible_speed_kph: float = 200.0
    fuel_step_floor_l: float = 5.0
    maximum_plausible_fuel_rate_lph: float = 200.0
    minimum_rate_distance_km: float = 1.0
    minimum_rate_driving_hours: float = 0.05
    minimum_fuel_used_l: float = 0.01
    notable_event_limit: int = 20


@dataclass(frozen=True)
class _DeltaResult:
    total: float | None
    valid_segments: int
    decreases: int
    rejected: int
    missing: int
    quality: QualityStatus
    warnings: tuple[str, ...]


def vehicle_profile_id(enabled_code: str) -> str:
    """Create a stable pseudonymous vehicle-linked profile key."""

    return sha256(f"vehicle-profile-v1\x1f{enabled_code}".encode()).hexdigest()


def _valid(row: dict[str, Any], name: str) -> bool:
    return row.get(name) is not None and row.get(f"{name}_quality") == "valid"


def _sorted_valid_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (row for row in rows if row.get("timestamp") is not None),
        key=lambda row: (row["timestamp"], row.get("source_row_number", 0)),
    )


def _cumulative_delta(
    rows: list[dict[str, Any]],
    field: str,
    *,
    step_floor: float,
    rate_limit_per_hour: float,
) -> _DeltaResult:
    previous_value: float | None = None
    previous_time: datetime | None = None
    total = 0.0
    segments = decreases = rejected = missing = 0
    for row in rows:
        timestamp = row.get("timestamp")
        if timestamp is None or not _valid(row, field):
            missing += 1
            continue
        value = float(row[field])
        if previous_value is not None and previous_time is not None:
            elapsed_hours = (timestamp - previous_time).total_seconds() / 3600.0
            if elapsed_hours > 0:
                delta = value - previous_value
                if delta < 0:
                    decreases += 1
                else:
                    maximum = step_floor + rate_limit_per_hour * elapsed_hours
                    if delta <= maximum:
                        total += delta
                        segments += 1
                    else:
                        rejected += 1
        previous_value = value
        previous_time = timestamp

    warnings: list[str] = []
    if decreases:
        warnings.append(f"{field.upper()}_RESET_OR_DECREASE")
    if rejected:
        warnings.append(f"{field.upper()}_OUTLIER_INCREMENT_REJECTED")
    if missing:
        warnings.append(f"{field.upper()}_MISSING_OR_INVALID_POINTS")
    if segments == 0:
        return _DeltaResult(
            None,
            segments,
            decreases,
            rejected,
            missing,
            QualityStatus.UNAVAILABLE,
            tuple(warnings + [f"{field.upper()}_NO_VALID_SEGMENTS"]),
        )
    quality = QualityStatus.DEGRADED if warnings else QualityStatus.VALID
    return _DeltaResult(
        total, segments, decreases, rejected, missing, quality, tuple(warnings)
    )


def _rate(value: int, denominator: NumericMetric, scale: float, unit: str) -> NumericMetric:
    if (
        denominator.value is None
        or denominator.value <= 0
        or denominator.quality_status in {QualityStatus.INVALID, QualityStatus.UNAVAILABLE}
    ):
        return NumericMetric(
            value=None,
            unit=unit,
            quality_status=QualityStatus.UNAVAILABLE,
            warnings=["RATE_DENOMINATOR_UNAVAILABLE"],
        )
    return NumericMetric(
        value=value / denominator.value * scale,
        unit=unit,
        quality_status=denominator.quality_status,
        warnings=list(denominator.warnings),
    )


class TripAnalytics:
    """Compute one auditable summary from one trip's typed telemetry and onsets."""

    def __init__(self, config: TripAnalyticsConfig | None = None) -> None:
        self.config = config or TripAnalyticsConfig()

    def analyze_trip(
        self,
        telemetry_rows: Iterable[dict[str, Any]],
        onset_rows: Iterable[dict[str, Any]],
    ) -> TripSummary:
        rows = _sorted_valid_rows(telemetry_rows)
        if not rows:
            raise ValueError("TripAnalytics requires at least one valid timestamp")
        trip_ids = {row.get("trip_id") for row in rows if row.get("trip_id")}
        enabled_codes = {
            row.get("enabled_code") for row in rows if row.get("enabled_code")
        }
        if len(trip_ids) != 1 or len(enabled_codes) != 1:
            raise ValueError("TripAnalytics input must contain exactly one trip and vehicle")
        trip_id = next(iter(trip_ids))
        enabled_code = next(iter(enabled_codes))
        start, end = rows[0]["timestamp"], rows[-1]["timestamp"]
        duration_seconds = (end - start).total_seconds()
        duration_status = (
            QualityStatus.VALID if duration_seconds > 0 else QualityStatus.INVALID
        )
        duration = NumericMetric(
            value=duration_seconds if duration_seconds >= 0 else None,
            unit="seconds",
            quality_status=duration_status,
            warnings=[] if duration_seconds > 0 else ["NON_POSITIVE_TRIP_DURATION"],
        )

        distance_delta = _cumulative_delta(
            rows,
            "total_mileage_km",
            step_floor=self.config.distance_step_floor_km,
            rate_limit_per_hour=self.config.maximum_plausible_speed_kph,
        )
        distance = NumericMetric(
            value=distance_delta.total,
            unit="km",
            quality_status=distance_delta.quality,
            warnings=list(distance_delta.warnings),
        )

        driving_seconds = idle_seconds = 0.0
        exposure_gaps = 0
        for left, right in zip(rows, rows[1:]):
            elapsed = (right["timestamp"] - left["timestamp"]).total_seconds()
            if elapsed <= 0:
                continue
            if elapsed > self.config.maximum_exposure_gap_seconds:
                exposure_gaps += 1
                continue
            if left.get("vehicle_status_quality") != "valid":
                continue
            if left.get("vehicle_status") == 1:
                driving_seconds += elapsed
            elif left.get("vehicle_status") == 2:
                idle_seconds += elapsed
        exposure_warnings = ["EXPOSURE_LONG_GAPS_EXCLUDED"] if exposure_gaps else []
        exposure_quality = (
            QualityStatus.DEGRADED if exposure_warnings else QualityStatus.VALID
        )
        driving_hours = NumericMetric(
            value=driving_seconds / 3600,
            unit="hours",
            quality_status=(
                exposure_quality if driving_seconds > 0 else QualityStatus.UNAVAILABLE
            ),
            warnings=exposure_warnings
            + ([] if driving_seconds > 0 else ["NO_VALID_DRIVING_EXPOSURE"]),
        )
        idle_minutes = NumericMetric(
            value=idle_seconds / 60,
            unit="minutes",
            quality_status=exposure_quality,
            warnings=exposure_warnings,
        )
        motion_seconds = driving_seconds + idle_seconds
        idle_pct = NumericMetric(
            value=(idle_seconds / motion_seconds * 100) if motion_seconds > 0 else None,
            unit="percent",
            quality_status=(
                exposure_quality if motion_seconds > 0 else QualityStatus.UNAVAILABLE
            ),
            warnings=exposure_warnings
            + ([] if motion_seconds > 0 else ["NO_VALID_MOTION_EXPOSURE"]),
        )

        fuel_delta = _cumulative_delta(
            rows,
            "total_fuel_used_l",
            step_floor=self.config.fuel_step_floor_l,
            rate_limit_per_hour=self.config.maximum_plausible_fuel_rate_lph,
        )
        fuel_used = NumericMetric(
            value=fuel_delta.total,
            unit="liters",
            quality_status=fuel_delta.quality,
            warnings=list(fuel_delta.warnings),
        )
        if (
            fuel_delta.total is None
            or fuel_delta.total < self.config.minimum_fuel_used_l
            or distance.value is None
            or distance.value < self.config.minimum_rate_distance_km
        ):
            fuel_efficiency = NumericMetric(
                value=None,
                unit="km_per_liter",
                quality_status=QualityStatus.UNAVAILABLE,
                warnings=["FUEL_OR_DISTANCE_EXPOSURE_INSUFFICIENT"],
            )
            fuel_per_100 = NumericMetric(
                value=None,
                unit="liters_per_100km",
                quality_status=QualityStatus.UNAVAILABLE,
                warnings=["FUEL_OR_DISTANCE_EXPOSURE_INSUFFICIENT"],
            )
        else:
            combined_quality = (
                QualityStatus.DEGRADED
                if QualityStatus.DEGRADED
                in {fuel_used.quality_status, distance.quality_status}
                else QualityStatus.VALID
            )
            combined_warnings = fuel_used.warnings + distance.warnings
            fuel_efficiency = NumericMetric(
                value=distance.value / fuel_delta.total,
                unit="km_per_liter",
                quality_status=combined_quality,
                warnings=combined_warnings,
            )
            fuel_per_100 = NumericMetric(
                value=fuel_delta.total / distance.value * 100,
                unit="liters_per_100km",
                quality_status=combined_quality,
                warnings=combined_warnings,
            )

        valid_onsets: list[dict[str, Any]] = []
        excluded_onsets = 0
        for event in onset_rows:
            event_time = event.get("event_start_time")
            if (
                event.get("trip_id") != trip_id
                or event.get("start_time_ambiguous")
                or event.get("start_year_mismatch_observed")
                or event.get("event_start_time_quality") != "valid"
                or event_time is None
                or not (start <= event_time <= end)
            ):
                excluded_onsets += 1
                continue
            valid_onsets.append(event)
        event_counts = Counter(event.get("event_name") for event in valid_onsets)
        event_metrics = [
            EventMetric(
                event_name=name,
                count=event_counts[name],
                per_100_km=_rate(event_counts[name], distance, 100.0, "events_per_100km"),
                per_driving_hour=_rate(
                    event_counts[name], driving_hours, 1.0, "events_per_driving_hour"
                ),
            )
            for name in TRACKED_EVENT_NAMES
        ]

        quality = TelemetryQualitySummary(
            total_point_count=len(rows),
            valid_timestamp_point_count=len(rows),
            malformed_row_count=sum(bool(row.get("row_has_malformed_value")) for row in rows),
            out_of_range_row_count=sum(
                bool(row.get("row_has_out_of_range_value")) for row in rows
            ),
            duplicate_key_row_count=sum(
                bool(row.get("is_duplicate_source_key")) for row in rows
            ),
            missing_gps_point_count=sum(
                bool(row.get("gps_coordinates_missing")) for row in rows
            ),
        )
        series = [
            row.get("vehicle_series_code")
            for row in rows
            if row.get("vehicle_series_code_quality") == "valid"
        ]
        warnings = list(dict.fromkeys(distance.warnings + fuel_used.warnings + exposure_warnings))
        if excluded_onsets:
            warnings.append("EVENT_ONSETS_EXCLUDED_BY_QUALITY_OR_TRIP_BOUNDS")

        rates = {
            metric.event_name: metric.per_100_km.value
            for metric in event_metrics
            if metric.per_100_km.value is not None
        }
        has_meaningful_contrast = rates and max(rates.values()) > min(rates.values())
        weakest = max(rates, key=rates.get) if has_meaningful_contrast else None
        strongest = min(rates, key=rates.get) if has_meaningful_contrast else None
        context_candidates = sorted(
            (
                event
                for event in valid_onsets
                if event.get("event_name") in TRACKED_EVENT_NAMES
            ),
            key=lambda event: (
                NOTABLE_EVENT_PRIORITY[event["event_name"]],
                event["event_start_time"],
            ),
        )[: self.config.notable_event_limit]
        tracked_context_events = [
            event
            for event in valid_onsets
            if event.get("event_name") in TRACKED_EVENT_NAMES
        ]
        contexts = build_event_contexts(
            rows,
            tracked_context_events,
            maximum=self.config.notable_event_limit,
            selected_events=context_candidates,
        )
        return TripSummary(
            trip_id=trip_id,
            vehicle_profile_id=vehicle_profile_id(enabled_code),
            basics=TripBasics(
                trip_start=start,
                trip_end=end,
                duration_seconds=duration,
                distance_km=distance,
                driving_hours=driving_hours,
                idle_minutes=idle_minutes,
                idle_pct=idle_pct,
                vehicle_series_code=series[0] if series else None,
                valid_telemetry_point_count=len(rows),
                telemetry_quality=quality,
            ),
            fuel=FuelMetrics(
                fuel_used_l=fuel_used,
                fuel_efficiency_km_per_l=fuel_efficiency,
                fuel_consumption_l_per_100km=fuel_per_100,
                valid_segment_count=fuel_delta.valid_segments,
                reset_or_decrease_count=fuel_delta.decreases,
                rejected_outlier_count=fuel_delta.rejected,
                missing_point_count=fuel_delta.missing,
            ),
            events=event_metrics,
            notable_events=contexts,
            strongest_behavior=strongest,
            weakest_behavior=weakest,
            quality_warnings=warnings,
        )
