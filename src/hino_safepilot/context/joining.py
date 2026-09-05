"""Leakage-safe temporal/spatial joins for offline and future online context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Iterable
from zoneinfo import ZoneInfo


def normalize_timestamp(value: datetime, *, naive_timezone: str | None = None) -> datetime:
    """Return UTC time; a naive source requires an explicit, auditable timezone."""

    if value.tzinfo is None:
        if not naive_timezone:
            raise ValueError("naive timestamp requires an explicit timezone")
        value = value.replace(tzinfo=ZoneInfo(naive_timezone))
    return value.astimezone(timezone.utc)


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius_m * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class ContextObservation:
    source_timestamp: datetime
    available_timestamp: datetime
    latitude_deg: float
    longitude_deg: float
    values: dict[str, Any]
    source_reference: str | None = None
    is_mock: bool = False


@dataclass(frozen=True)
class ContextJoinMatch:
    observation: ContextObservation
    join_distance_m: float
    join_age_seconds: float


def nearest_valid_observation(
    observations: Iterable[ContextObservation],
    *,
    query_timestamp: datetime,
    latitude_deg: float,
    longitude_deg: float,
    maximum_age_seconds: float,
    maximum_distance_m: float,
) -> ContextJoinMatch | None:
    """Choose the closest observation that was actually available by query time."""

    query = normalize_timestamp(query_timestamp, naive_timezone=None)
    candidates: list[ContextJoinMatch] = []
    for observation in observations:
        source = normalize_timestamp(observation.source_timestamp, naive_timezone=None)
        available = normalize_timestamp(observation.available_timestamp, naive_timezone=None)
        if source > query or available > query:
            continue
        age = (query - source).total_seconds()
        if age < 0 or age > maximum_age_seconds:
            continue
        distance = haversine_distance_m(
            latitude_deg,
            longitude_deg,
            observation.latitude_deg,
            observation.longitude_deg,
        )
        if distance <= maximum_distance_m:
            candidates.append(ContextJoinMatch(observation, distance, age))
    return min(
        candidates,
        key=lambda match: (
            match.join_distance_m,
            match.join_age_seconds,
            match.observation.available_timestamp,
        ),
        default=None,
    )
