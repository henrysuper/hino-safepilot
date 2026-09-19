"""Sanitized input and structured-output contracts for optional LLM wording."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SanitizedMetric(StrictModel):
    name: str
    value: float | None
    unit: str
    quality_status: str


class SanitizedEventMetric(StrictModel):
    event_name: str
    count: int = Field(ge=0)
    per_100_km: float | None
    per_driving_hour: float | None
    quality_status: str


class SanitizedNotableEvent(StrictModel):
    event_name: str
    trip_minute: float = Field(ge=0)
    trip_progress_pct: float = Field(ge=0, le=100)
    relative_distance_km: float | None = Field(default=None, ge=0)
    previous_30s_speed_mean_kph: float | None = None
    current_speed_kph: float | None = None
    next_30s_speed_mean_kph: float | None = None
    next_30s_behavior: str


class SanitizedTripReportPayload(StrictModel):
    payload_version: Literal["sanitized-trip-report-v1"] = "sanitized-trip-report-v1"
    duration_minutes: SanitizedMetric
    distance_km: SanitizedMetric
    driving_hours: SanitizedMetric
    idle_pct: SanitizedMetric
    fuel_used_l: SanitizedMetric
    fuel_efficiency_km_per_l: SanitizedMetric
    fuel_consumption_l_per_100km: SanitizedMetric
    event_metrics: list[SanitizedEventMetric]
    notable_events: list[SanitizedNotableEvent]
    strongest_behavior: str | None
    weakest_behavior: str | None
    quality_warnings: list[str]


class SanitizedDailyGoalPayload(StrictModel):
    payload_version: Literal["sanitized-daily-goal-v1"] = "sanitized-daily-goal-v1"
    target_metric: str
    priority_score: float = Field(ge=0, le=1)
    reason_codes: list[str]
    recent_value: float | None = None
    baseline_value: float | None = None
    unit: str | None = None
    change_pct: float | None = None
    quality_status: str


class AITripReport(StrictModel):
    schema_version: Literal["ai-trip-report-v1"] = "ai-trip-report-v1"
    summary: str = Field(min_length=1, max_length=500)
    strengths: list[str] = Field(max_length=5)
    weaknesses: list[str] = Field(max_length=5)
    primary_risk: str = Field(min_length=1, max_length=80)
    recommendations: list[str] = Field(min_length=1, max_length=5)
    next_trip_focus: str = Field(min_length=1, max_length=200)

    @field_validator("strengths", "weaknesses", "recommendations")
    @classmethod
    def reject_blank_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("list items must not be blank")
        return values


class AIDailyGoalMessage(StrictModel):
    schema_version: Literal["ai-daily-goal-v1"] = "ai-daily-goal-v1"
    target_metric: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=80)
    explanation: str = Field(min_length=1, max_length=300)
    action: str = Field(min_length=1, max_length=200)


class TripReportResult(StrictModel):
    report: AITripReport
    generation_mode: Literal["llm", "mock", "deterministic_fallback"]
    provider_error_code: str | None = None


class DailyGoalMessageResult(StrictModel):
    message: AIDailyGoalMessage
    generation_mode: Literal["llm", "mock", "deterministic_fallback"]
    provider_error_code: str | None = None
