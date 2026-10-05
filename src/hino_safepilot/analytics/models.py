"""Versioned application contracts for deterministic analytics."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QualityStatus(StrEnum):
    VALID = "valid"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class NumericMetric(ContractModel):
    value: float | None = None
    unit: str
    quality_status: QualityStatus
    warnings: list[str] = Field(default_factory=list)


class TelemetryQualitySummary(ContractModel):
    total_point_count: int = Field(ge=0)
    valid_timestamp_point_count: int = Field(ge=0)
    malformed_row_count: int = Field(ge=0)
    out_of_range_row_count: int = Field(ge=0)
    duplicate_key_row_count: int = Field(ge=0)
    missing_gps_point_count: int = Field(ge=0)


class TripBasics(ContractModel):
    trip_start: datetime
    trip_end: datetime
    duration_seconds: NumericMetric
    distance_km: NumericMetric
    driving_hours: NumericMetric
    idle_minutes: NumericMetric
    idle_pct: NumericMetric
    vehicle_series_code: int | None
    valid_telemetry_point_count: int = Field(ge=0)
    telemetry_quality: TelemetryQualitySummary


class FuelMetrics(ContractModel):
    fuel_used_l: NumericMetric
    fuel_efficiency_km_per_l: NumericMetric
    fuel_consumption_l_per_100km: NumericMetric
    valid_segment_count: int = Field(ge=0)
    reset_or_decrease_count: int = Field(ge=0)
    rejected_outlier_count: int = Field(ge=0)
    missing_point_count: int = Field(ge=0)


class EventMetric(ContractModel):
    event_name: str
    count: int = Field(ge=0)
    per_100_km: NumericMetric
    per_driving_hour: NumericMetric


class EventContext(ContractModel):
    event_candidate_reference: str
    event_name: str
    event_time: datetime
    trip_minute: float = Field(ge=0)
    trip_progress_pct: float = Field(ge=0, le=100)
    relative_distance_km: float | None = Field(default=None, ge=0)
    previous_30s_speed_mean_kph: float | None = None
    previous_30s_speed_max_kph: float | None = None
    previous_30s_speed_trend_kph_per_min: float | None = None
    previous_30s_engine_load_mean: float | None = None
    recent_event_count: int = Field(ge=0)
    current_telemetry_timestamp: datetime | None = None
    current_speed_kph: float | None = None
    current_speed_limit_kph: float | None = None
    current_engine_load_pct: float | None = None
    next_30s_speed_mean_kph: float | None = None
    next_30s_behavior: Literal[
        "speed_increased", "speed_decreased", "speed_stable", "unavailable"
    ]
    quality_warnings: list[str] = Field(default_factory=list)


class TripSummary(ContractModel):
    contract_version: Literal["trip-summary-v1"] = "trip-summary-v1"
    trip_id: str
    behavior_profile_type: Literal["vehicle-linked behavior profile"] = (
        "vehicle-linked behavior profile"
    )
    vehicle_profile_id: str
    basics: TripBasics
    fuel: FuelMetrics
    events: list[EventMetric]
    notable_events: list[EventContext]
    strongest_behavior: str | None = None
    weakest_behavior: str | None = None
    quality_warnings: list[str] = Field(default_factory=list)


class BehaviorMetric(ContractModel):
    name: str
    value: float | None
    unit: str
    quality_status: QualityStatus
    contributing_trip_count: int = Field(ge=0)
    exposure_value: float | None = None
    warnings: list[str] = Field(default_factory=list)


class BehaviorBaseline(ContractModel):
    contract_version: Literal["behavior-baseline-v1"] = "behavior-baseline-v1"
    behavior_profile_type: Literal["vehicle-linked behavior profile"] = (
        "vehicle-linked behavior profile"
    )
    vehicle_profile_id: str
    cutoff_exclusive: datetime
    window_kind: Literal["previous_days", "previous_n_trips"]
    window_value: int = Field(gt=0)
    trip_count: int = Field(ge=0)
    earliest_trip_start: datetime | None = None
    latest_trip_end: datetime | None = None
    metrics: list[BehaviorMetric]
    quality_warnings: list[str] = Field(default_factory=list)


class WeaknessScore(ContractModel):
    metric: str
    priority_score: float = Field(ge=0, le=1)
    reason_codes: list[str]
    evidence: dict[str, float | int | str | None]
    quality_status: QualityStatus
    scoring_version: str


class DailyImprovementGoal(ContractModel):
    contract_version: Literal["daily-improvement-goal-v1"] = (
        "daily-improvement-goal-v1"
    )
    behavior_profile_type: Literal["vehicle-linked behavior profile"] = (
        "vehicle-linked behavior profile"
    )
    vehicle_profile_id: str
    effective_at: datetime
    target_metric: str | None
    priority_score: float = Field(ge=0, le=1)
    reason_codes: list[str]
    evidence_summary: dict[str, float | int | str | None]
    quality_status: QualityStatus
    scoring_version: str


def json_safe(model: BaseModel) -> dict[str, Any]:
    """Return a JSON-compatible mapping without custom encoders at call sites."""

    return model.model_dump(mode="json")
