"""CWA rainfall-observation parsing and leakage-safe station joins."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .joining import ContextObservation, nearest_valid_observation, normalize_timestamp
from .models import ContextAvailability, ContextQuery, WeatherContextSnapshot


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _nested(mapping: dict, *path: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def parse_cwa_rainfall_observations(
    payload: dict[str, Any], *, is_mock: bool
) -> list[ContextObservation]:
    rows = _nested(payload, "records", "Station") or payload.get("Station", [])
    observations: list[ContextObservation] = []
    for row in rows:
        source_text = _nested(row, "ObsTime", "DateTime") or row.get("ObsTime")
        if not source_text:
            continue
        source = normalize_timestamp(datetime.fromisoformat(str(source_text).replace("Z", "+00:00")))
        available_text = row.get("AvailableTime") or source_text
        available = normalize_timestamp(datetime.fromisoformat(str(available_text).replace("Z", "+00:00")))
        geo = row.get("GeoInfo", {})
        coordinates = geo.get("Coordinates", []) if isinstance(geo, dict) else []
        coordinate = coordinates[0] if coordinates else {}
        latitude = coordinate.get("StationLatitude") or row.get("StationLatitude")
        longitude = coordinate.get("StationLongitude") or row.get("StationLongitude")
        if latitude is None or longitude is None:
            continue
        rain = row.get("RainfallElement", {})
        observations.append(
            ContextObservation(
                source_timestamp=source,
                available_timestamp=available,
                latitude_deg=float(latitude),
                longitude_deg=float(longitude),
                values={
                    "rainfall_10min_mm": _number(_nested(rain, "Past10Min", "Precipitation")),
                    "rainfall_1h_mm": _number(_nested(rain, "Past1hr", "Precipitation")),
                    "rainfall_3h_mm": _number(_nested(rain, "Past3hr", "Precipitation")),
                },
                source_reference=str(row.get("StationId") or row.get("StationName") or "station"),
                is_mock=is_mock,
            )
        )
    return observations


class CWAContextProvider:
    provider_name = "cwa"

    def __init__(
        self,
        observations: Iterable[ContextObservation],
        *,
        maximum_age_seconds: float = 1800,
        maximum_distance_m: float = 30_000,
        dataset_version: str = "O-A0002-001",
    ):
        self.observations = tuple(observations)
        self.maximum_age_seconds = maximum_age_seconds
        self.maximum_distance_m = maximum_distance_m
        self.dataset_version = dataset_version

    def weather(self, query: ContextQuery) -> WeatherContextSnapshot:
        if query.latitude_deg is None or query.longitude_deg is None:
            return WeatherContextSnapshot(
                context_timestamp=query.context_timestamp,
                quality_status=ContextAvailability.MISSING,
                provider_name=self.provider_name,
                source_dataset_version=self.dataset_version,
                error_code="GPS_REQUIRED",
            )
        match = nearest_valid_observation(
            self.observations,
            query_timestamp=query.context_timestamp,
            latitude_deg=query.latitude_deg,
            longitude_deg=query.longitude_deg,
            maximum_age_seconds=self.maximum_age_seconds,
            maximum_distance_m=self.maximum_distance_m,
        )
        if match is None:
            stale_match = nearest_valid_observation(
                self.observations,
                query_timestamp=query.context_timestamp,
                latitude_deg=query.latitude_deg,
                longitude_deg=query.longitude_deg,
                maximum_age_seconds=float("inf"),
                maximum_distance_m=self.maximum_distance_m,
            )
            if stale_match is not None:
                observation = stale_match.observation
                return WeatherContextSnapshot(
                    context_timestamp=query.context_timestamp,
                    source_timestamp=observation.source_timestamp,
                    available_timestamp=observation.available_timestamp,
                    freshness_seconds=stale_match.join_age_seconds,
                    join_age_seconds=stale_match.join_age_seconds,
                    join_distance_m=stale_match.join_distance_m,
                    quality_status=ContextAvailability.STALE,
                    provider_name=self.provider_name,
                    source_dataset_version=self.dataset_version,
                    rainfall_10min_mm=observation.values.get("rainfall_10min_mm"),
                    rainfall_1h_mm=observation.values.get("rainfall_1h_mm"),
                    rainfall_3h_mm=observation.values.get("rainfall_3h_mm"),
                    precipitation_mm=observation.values.get("rainfall_1h_mm"),
                    is_mock=observation.is_mock,
                )
            return WeatherContextSnapshot(
                context_timestamp=query.context_timestamp,
                quality_status=ContextAvailability.MISSING,
                provider_name=self.provider_name,
                source_dataset_version=self.dataset_version,
                error_code="NO_VALID_HISTORICAL_MATCH",
            )
        observation = match.observation
        return WeatherContextSnapshot(
            context_timestamp=query.context_timestamp,
            source_timestamp=observation.source_timestamp,
            available_timestamp=observation.available_timestamp,
            freshness_seconds=match.join_age_seconds,
            join_age_seconds=match.join_age_seconds,
            join_distance_m=match.join_distance_m,
            quality_status=ContextAvailability.AVAILABLE,
            provider_name=self.provider_name,
            source_dataset_version=self.dataset_version,
            rainfall_10min_mm=observation.values.get("rainfall_10min_mm"),
            rainfall_1h_mm=observation.values.get("rainfall_1h_mm"),
            rainfall_3h_mm=observation.values.get("rainfall_3h_mm"),
            precipitation_mm=observation.values.get("rainfall_1h_mm"),
            is_mock=observation.is_mock,
        )

    def fetch(self, query: ContextQuery) -> WeatherContextSnapshot:
        return self.weather(query)
