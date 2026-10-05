"""Fail-closed privacy boundary between private analytics and any LLM provider."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .models import (
    SanitizedDailyGoalPayload,
    SanitizedEventMetric,
    SanitizedMetric,
    SanitizedNotableEvent,
    SanitizedTripReportPayload,
)
from hino_safepilot.analytics.models import DailyImprovementGoal, NumericMetric, TripSummary


FORBIDDEN_KEY_TOKENS = frozenset(
    {
        "latitude",
        "longitude",
        "carnum",
        "enabledcode",
        "tripid",
        "sourcerow",
        "sourceidentifier",
        "licenseplate",
        "vehicleid",
        "vehicleprofileid",
        "eventcandidateid",
        "eventcandidatereference",
    }
)


class PrivacyViolation(ValueError):
    pass


def _canonical_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def validate_no_forbidden_fields(payload: Any, path: str = "$") -> None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            canonical = _canonical_key(key)
            if any(token in canonical for token in FORBIDDEN_KEY_TOKENS):
                raise PrivacyViolation(f"Forbidden field at {path}.{key}")
            validate_no_forbidden_fields(value, f"{path}.{key}")
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for index, value in enumerate(payload):
            validate_no_forbidden_fields(value, f"{path}[{index}]")


def _metric(
    name: str,
    metric: NumericMetric,
    multiplier: float = 1.0,
    unit: str | None = None,
) -> SanitizedMetric:
    return SanitizedMetric(
        name=name,
        value=metric.value * multiplier if metric.value is not None else None,
        unit=unit or metric.unit,
        quality_status=metric.quality_status.value,
    )


class LLMSanitizer:
    def trip_report(self, summary: TripSummary) -> SanitizedTripReportPayload:
        payload = SanitizedTripReportPayload(
            duration_minutes=_metric(
                "duration", summary.basics.duration_seconds, 1 / 60, "minutes"
            ),
            distance_km=_metric("distance", summary.basics.distance_km),
            driving_hours=_metric("driving_time", summary.basics.driving_hours),
            idle_pct=_metric("idle_ratio", summary.basics.idle_pct),
            fuel_used_l=_metric("fuel_used", summary.fuel.fuel_used_l),
            fuel_efficiency_km_per_l=_metric(
                "fuel_efficiency", summary.fuel.fuel_efficiency_km_per_l
            ),
            fuel_consumption_l_per_100km=_metric(
                "fuel_consumption", summary.fuel.fuel_consumption_l_per_100km
            ),
            event_metrics=[
                SanitizedEventMetric(
                    event_name=event.event_name,
                    count=event.count,
                    per_100_km=event.per_100_km.value,
                    per_driving_hour=event.per_driving_hour.value,
                    quality_status=event.per_100_km.quality_status.value,
                )
                for event in summary.events
            ],
            notable_events=[
                SanitizedNotableEvent(
                    event_name=event.event_name,
                    trip_minute=event.trip_minute,
                    trip_progress_pct=event.trip_progress_pct,
                    relative_distance_km=event.relative_distance_km,
                    previous_30s_speed_mean_kph=event.previous_30s_speed_mean_kph,
                    current_speed_kph=event.current_speed_kph,
                    next_30s_speed_mean_kph=event.next_30s_speed_mean_kph,
                    next_30s_behavior=event.next_30s_behavior,
                )
                for event in summary.notable_events
            ],
            strongest_behavior=summary.strongest_behavior,
            weakest_behavior=summary.weakest_behavior,
            quality_warnings=summary.quality_warnings,
        )
        validate_no_forbidden_fields(payload.model_dump(mode="json"))
        return payload

    def daily_goal(self, goal: DailyImprovementGoal) -> SanitizedDailyGoalPayload:
        if not goal.target_metric:
            raise PrivacyViolation("Daily goal has no deterministic target metric")
        evidence = goal.evidence_summary
        payload = SanitizedDailyGoalPayload(
            target_metric=goal.target_metric,
            priority_score=goal.priority_score,
            reason_codes=goal.reason_codes,
            recent_value=_optional_float(evidence.get("recent_value")),
            baseline_value=_optional_float(evidence.get("baseline_value")),
            unit=str(evidence["unit"]) if evidence.get("unit") is not None else None,
            change_pct=_optional_float(evidence.get("change_pct")),
            quality_status=goal.quality_status.value,
        )
        validate_no_forbidden_fields(payload.model_dump(mode="json"))
        return payload


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
