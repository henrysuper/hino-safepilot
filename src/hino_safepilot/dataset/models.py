"""Internal dataset records. Private identity remains metadata-only."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


CORE_EVENT_TYPES = {
    "decelerate": 7,
    "pcs": 19,
    "ldws": 20,
    "fatigue": 12,
    "distract": 16,
}
HISTORICAL_EVENT_TYPES = {
    "accelerate": 6,
    "decelerate": 7,
    "speeding": 8,
    "engine_overloading": 11,
    "fatigue": 12,
    "phone": 14,
    "distract": 16,
    "pcs": 19,
    "ldws": 20,
}


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    group: str
    source: str
    history_window_seconds: int | None
    aggregation: str
    dtype: str
    leakage_status: str
    quality_handling: str
    model_inclusion: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "group": self.group,
            "source": self.source,
            "history_window_seconds": self.history_window_seconds,
            "aggregation": self.aggregation,
            "dtype": self.dtype,
            "leakage_status": self.leakage_status,
            "quality_handling": self.quality_handling,
            "model_inclusion": self.model_inclusion,
        }


@dataclass
class TripTelemetry:
    trip_id: str
    vehicle_id: str
    vehicle_series_code: int | None
    timestamps: list[datetime]
    values: dict[str, list[float | int | None]]
    qualities: dict[str, list[str]]
    duplicate_extra_rows: int = 0

    @property
    def start(self) -> datetime:
        return self.timestamps[0]

    @property
    def end(self) -> datetime:
        return self.timestamps[-1]

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class EventOnset:
    event_type: int
    start: datetime
    first_observed: datetime | None
    active_end: datetime | None


@dataclass
class LeakageEvidence:
    feature_source_timestamp_violations: int = 0
    label_boundary_violations: int = 0
    current_event_feature_violations: int = 0
    future_telemetry_feature_violations: int = 0
    split_boundary_violations: int = 0
    checked_feature_rows: int = 0
    checked_positive_labels: int = 0
    maximum_feature_timestamp: datetime | None = None
    minimum_anchor_minus_feature_seconds: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(
            (
                self.feature_source_timestamp_violations,
                self.label_boundary_violations,
                self.current_event_feature_violations,
                self.future_telemetry_feature_violations,
                self.split_boundary_violations,
            )
        )
