"""Internal research records. Identifier fields never enter tracked outputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


CORE_TYPES = (7, 19, 20, 12, 16)
CORE_NAMES = {
    7: "decelerate",
    19: "pcs",
    20: "ldws",
    12: "fatigue",
    16: "distract",
}
LABEL_TYPES = {
    "label_v0_core_any": CORE_TYPES,
    "label_v1_core_multilabel": CORE_TYPES,
    "label_v2_context_expanded_experimental": (*CORE_TYPES, 8),
}


@dataclass
class TripRecord:
    trip_id: str
    vehicle_key: str
    journey_code: str | None
    timestamps: list[datetime]
    raw_row_count: int
    duplicate_timestamp_row_count: int
    gps_speed_out_of_range_rows: int
    azimuth_out_of_range_rows: int
    missing_gps_rows: int
    malformed_rows: int
    distance_km: float | None = None
    distance_quality: str = "unavailable"
    driving_hours: float | None = None
    driving_hours_quality: str = "unavailable"
    duration_seconds: float | None = None
    duration_quality: str = "unavailable"

    @property
    def start(self) -> datetime:
        return self.timestamps[0]

    @property
    def end(self) -> datetime:
        return self.timestamps[-1]

    @property
    def ultra_long(self) -> bool:
        return bool(self.duration_seconds is not None and self.duration_seconds > 12 * 3600)

    @property
    def invalid_duration(self) -> bool:
        return self.duration_seconds is None or self.duration_seconds <= 0


@dataclass(frozen=True)
class OnsetRecord:
    trip_id: str
    vehicle_key: str
    event_type: int
    event_name: str
    start: datetime | None
    start_quality: str
    observation_count: int
    first_observed: datetime | None
    last_observed: datetime | None
    payload_variant_count: int
    end_variant_count: int
    payload_conflict: bool
    end_conflict: bool
    start_ambiguous: bool
    year_mismatch: bool
    start_after_telemetry: bool


@dataclass(frozen=True)
class ScenarioConfig:
    name: str
    factor: str
    anchor_cadence: str = "10s"
    label_version: str = "label_v0_core_any"
    active_policy: str = "active_state_exclusion"
    history_policy: str = "recommended_240s_10points_gap60s"
    horizon_policy: str = "full_60s_with_future_observation"
    quality_policy: str = "exclude_invalid_or_ultralong_trip"


@dataclass
class SimulationPrivateStats:
    positive_by_vehicle: dict[str, int] = field(default_factory=dict)
    eligible_by_vehicle: dict[str, int] = field(default_factory=dict)
    positive_by_month: dict[str, int] = field(default_factory=dict)
    eligible_by_month: dict[str, int] = field(default_factory=dict)
    positive_by_hour: dict[str, int] = field(default_factory=dict)
    eligible_by_hour: dict[str, int] = field(default_factory=dict)
    positive_by_weekday: dict[str, int] = field(default_factory=dict)
    eligible_by_weekday: dict[str, int] = field(default_factory=dict)
    positive_by_event_vehicle: dict[int, dict[str, int]] = field(default_factory=dict)
