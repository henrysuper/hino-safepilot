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


class ScoreDeduction(ContractModel):
    item: str
    category: str
    observed_value: float | None
    unit: str
    points_lost: float = Field(ge=0)


class CategoryScore(ContractModel):
    category: str
    label: str
    weight: float = Field(ge=0, le=1)
    score: float | None = Field(default=None, ge=0, le=100)
    deductions: list[ScoreDeduction] = Field(default_factory=list)


class TripSafetyScore(ContractModel):
    contract_version: Literal["trip-safety-score-v1"] = "trip-safety-score-v1"
    trip_id: str
    behavior_profile_type: Literal["vehicle-linked behavior profile"] = (
        "vehicle-linked behavior profile"
    )
    vehicle_profile_id: str
    trip_start: datetime
    trip_end: datetime
    distance_km: float | None
    score_status: Literal["scored", "insufficient_exposure"]
    overall_score: float | None = Field(default=None, ge=0, le=100)
    grade: Literal["A", "B", "C", "D", "E"] | None = None
    categories: list[CategoryScore]
    top_deductions: list[ScoreDeduction]
    applied_caps: list[str] = Field(default_factory=list)
    event_counts: dict[str, int]
    dms_reliability: Literal["ok", "camera_blocked"] = "ok"
    scoring_version: str
    quality_warnings: list[str] = Field(default_factory=list)


class SafetyReminder(ContractModel):
    code: str
    priority: Literal["high", "normal"]
    message: str


class PreDriveBriefing(ContractModel):
    contract_version: Literal["predrive-briefing-v1"] = "predrive-briefing-v1"
    behavior_profile_type: Literal["vehicle-linked behavior profile"] = (
        "vehicle-linked behavior profile"
    )
    vehicle_profile_id: str
    effective_at: datetime
    last_trip_score: float | None = None
    last_trip_grade: Literal["A", "B", "C", "D", "E"] | None = None
    recent_days: int = Field(gt=0)
    recent_average_score: float | None = None
    recent_scored_trip_count: int = Field(ge=0)
    previous_average_score: float | None = None
    trend: Literal["improving", "declining", "stable", "unavailable"]
    weakest_category: str | None = None
    weakest_category_score: float | None = None
    recent_critical_event_counts: dict[str, int]
    daily_goal: DailyImprovementGoal | None = None
    reminders: list[SafetyReminder]
    scoring_version: str


class DriverTripRecord(ContractModel):
    """One scored trip attributed to a simulated driver through the vehicle-day assignment."""

    trip_id: str
    trip_start: datetime
    vehicle_alias: str
    mock_plate: str | None = None
    distance_km: float | None = None
    score_status: Literal["scored", "insufficient_exposure"]
    overall_score: float | None = Field(default=None, ge=0, le=100)
    grade: Literal["A", "B", "C", "D", "E"] | None = None
    top_deduction: str | None = None
    dms_reliability: Literal["ok", "camera_blocked"] = "ok"


class DriverSafetyHistory(ContractModel):
    contract_version: Literal["driver-safety-history-v1"] = "driver-safety-history-v1"
    identity_basis: Literal["simulated driver roster (vehicle-day assignment)"] = (
        "simulated driver roster (vehicle-day assignment)"
    )
    driver_id: str
    driver_name: str
    primary_type: int | None = None
    new_driver: bool = False
    trip_count: int = Field(ge=0)
    scored_trip_count: int = Field(ge=0)
    total_distance_km: float = Field(ge=0)
    average_score: float | None = None
    grade_counts: dict[str, int]
    category_averages: dict[str, float]
    frequent_deductions: list[dict[str, float | int | str]]
    recent_trips: int = Field(gt=0)
    recent_average_score: float | None = None
    previous_average_score: float | None = None
    trend: Literal["improving", "declining", "stable", "unavailable"]
    camera_blocked_trip_count: int = Field(ge=0)
    series_rank: int | None = Field(default=None, ge=1)
    series_ranked_size: int | None = Field(default=None, ge=1)
    monthly_average_scores: list[dict[str, float | int | str]]
    trips: list[DriverTripRecord]
    scoring_version: str
    mock_note: str


def json_safe(model: BaseModel) -> dict[str, Any]:
    """Return a JSON-compatible mapping without custom encoders at call sites."""

    return model.model_dump(mode="json")
