"""TDX road-condition parsing and an injected-observation provider.

No network call is made here. Historical downloads require local credentials and
must be cached outside Git before this provider is populated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .joining import ContextObservation, nearest_valid_observation, normalize_timestamp
from .models import ContextAvailability, ContextQuery, TrafficContextSnapshot


def _parse_time(value: str) -> datetime:
    return normalize_timestamp(datetime.fromisoformat(value.replace("Z", "+00:00")))


def parse_tdx_vd_live(
    payload: dict[str, Any] | list[dict[str, Any]],
    *,
    vd_locations: dict[str, tuple[float, float, str | None]],
    is_mock: bool,
) -> list[ContextObservation]:
    """Parse the documented VDLives shape; static VD coordinates stay separate."""

    rows = payload.get("VDLives", []) if isinstance(payload, dict) else payload
    observations: list[ContextObservation] = []
    for row in rows:
        vd_id = str(row["VDID"])
        if vd_id not in vd_locations:
            continue
        source = _parse_time(row["DataCollectTime"])
        available = _parse_time(row.get("UpdateTime", row["DataCollectTime"]))
        speeds: list[float] = []
        volumes: list[float] = []
        for link in row.get("LinkFlows", []):
            for lane in link.get("Lanes", []):
                if lane.get("Speed") is not None:
                    speeds.append(float(lane["Speed"]))
                for vehicle in lane.get("Vehicles", []):
                    if vehicle.get("Volume") is not None:
                        volumes.append(float(vehicle["Volume"]))
        latitude, longitude, road_class = vd_locations[vd_id]
        observations.append(
            ContextObservation(
                source_timestamp=source,
                available_timestamp=available,
                latitude_deg=latitude,
                longitude_deg=longitude,
                values={
                    "road_speed_kph": sum(speeds) / len(speeds) if speeds else None,
                    "observed_volume": sum(volumes) if volumes else None,
                    "road_class": road_class,
                },
                source_reference=vd_id,
                is_mock=is_mock,
            )
        )
    return observations


class TDXContextProvider:
    provider_name = "tdx"

    def __init__(
        self,
        observations: Iterable[ContextObservation],
        *,
        maximum_age_seconds: float = 600,
        maximum_distance_m: float = 3000,
        dataset_version: str = "Traffic-v2-VDLive",
    ):
        self.observations = tuple(observations)
        self.maximum_age_seconds = maximum_age_seconds
        self.maximum_distance_m = maximum_distance_m
        self.dataset_version = dataset_version

    def traffic(self, query: ContextQuery) -> TrafficContextSnapshot:
        if query.latitude_deg is None or query.longitude_deg is None:
            return TrafficContextSnapshot(
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
                return TrafficContextSnapshot(
                    context_timestamp=query.context_timestamp,
                    source_timestamp=observation.source_timestamp,
                    available_timestamp=observation.available_timestamp,
                    freshness_seconds=stale_match.join_age_seconds,
                    join_age_seconds=stale_match.join_age_seconds,
                    join_distance_m=stale_match.join_distance_m,
                    quality_status=ContextAvailability.STALE,
                    provider_name=self.provider_name,
                    source_dataset_version=self.dataset_version,
                    error_code=None,
                    road_speed_kph=observation.values.get("road_speed_kph"),
                    road_class=observation.values.get("road_class"),
                    is_mock=observation.is_mock,
                )
            return TrafficContextSnapshot(
                context_timestamp=query.context_timestamp,
                quality_status=ContextAvailability.MISSING,
                provider_name=self.provider_name,
                source_dataset_version=self.dataset_version,
                error_code="NO_VALID_HISTORICAL_MATCH",
            )
        observation = match.observation
        return TrafficContextSnapshot(
            context_timestamp=query.context_timestamp,
            source_timestamp=observation.source_timestamp,
            available_timestamp=observation.available_timestamp,
            freshness_seconds=match.join_age_seconds,
            join_age_seconds=match.join_age_seconds,
            join_distance_m=match.join_distance_m,
            quality_status=ContextAvailability.AVAILABLE,
            provider_name=self.provider_name,
            source_dataset_version=self.dataset_version,
            road_speed_kph=observation.values.get("road_speed_kph"),
            road_class=observation.values.get("road_class"),
            is_mock=observation.is_mock,
        )

    def fetch(self, query: ContextQuery) -> TrafficContextSnapshot:
        return self.traffic(query)
