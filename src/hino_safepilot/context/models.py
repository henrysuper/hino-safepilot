"""Typed external-context snapshots with explicit availability and freshness."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextAvailability(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    STALE = "stale"
    ERROR = "error"
    UNSUPPORTED = "unsupported"


class ContextQuery(StrictContextModel):
    context_timestamp: datetime
    latitude_deg: float | None = Field(default=None, ge=-90, le=90)
    longitude_deg: float | None = Field(default=None, ge=-180, le=180)
    heading_deg: float | None = Field(default=None, ge=0, lt=360)
    road_reference: str | None = None
    route_reference: str | None = None


class SnapshotBase(StrictContextModel):
    context_timestamp: datetime
    source_timestamp: datetime | None = None
    available_timestamp: datetime | None = None
    freshness_seconds: float | None = Field(default=None, ge=0)
    join_age_seconds: float | None = Field(default=None, ge=0)
    join_distance_m: float | None = Field(default=None, ge=0)
    quality_status: ContextAvailability
    provider_name: str
    source_dataset_version: str | None = None
    error_code: str | None = None
    is_mock: bool = False

    @field_validator("error_code")
    @classmethod
    def error_code_requires_failure(cls, value: str | None, info):
        status = info.data.get("quality_status")
        if value is not None and status not in {
            ContextAvailability.ERROR,
            ContextAvailability.MISSING,
            ContextAvailability.UNSUPPORTED,
        }:
            raise ValueError("error_code is only valid for unavailable/error states")
        return value

    @model_validator(mode="after")
    def reject_future_context(self):
        for name, value in (
            ("source_timestamp", self.source_timestamp),
            ("available_timestamp", self.available_timestamp),
        ):
            if value is not None:
                context_aware = self.context_timestamp.tzinfo is not None
                value_aware = value.tzinfo is not None
                if context_aware != value_aware:
                    raise ValueError(f"{name} timezone-awareness must match context_timestamp")
                if value > self.context_timestamp:
                    raise ValueError(f"{name} must not be after context_timestamp")
        return self


class TrafficContextSnapshot(SnapshotBase):
    road_speed_kph: float | None = Field(default=None, ge=0)
    congestion_ratio: float | None = Field(default=None, ge=0)
    road_class: str | None = None
    congestion_level: float | None = Field(default=None, ge=0, le=1)
    road_event_ahead: bool | None = None
    nearest_relevant_event_distance_m: float | None = Field(default=None, ge=0)
    relevant_incident_count: int | None = Field(default=None, ge=0)
    road_closed: bool | None = None


class WeatherContextSnapshot(SnapshotBase):
    precipitation_mm: float | None = Field(default=None, ge=0)
    rainfall_10min_mm: float | None = Field(default=None, ge=0)
    rainfall_1h_mm: float | None = Field(default=None, ge=0)
    rainfall_3h_mm: float | None = Field(default=None, ge=0)
    visibility_m: float | None = Field(default=None, ge=0)
    wind_speed_mps: float | None = Field(default=None, ge=0)
    temperature_c: float | None = None
    relative_humidity_pct: float | None = Field(default=None, ge=0, le=100)
    weather_warning: bool | None = None
    heavy_rain_warning: bool | None = None
    strong_wind_warning: bool | None = None
    fog_warning: bool | None = None


class DisasterContextSnapshot(SnapshotBase):
    heavy_rain_alert: bool | None = None
    flood_alert: bool | None = None
    landslide_alert: bool | None = None
    road_closure_alert: bool | None = None
    alert_severity: str | None = None


class ContextBundle(StrictContextModel):
    traffic: TrafficContextSnapshot
    weather: WeatherContextSnapshot
    disaster: DisasterContextSnapshot
    base_hino_model_available: bool = True
