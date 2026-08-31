"""Null and synthetic context providers; deliberately no HTTP/network code."""

from __future__ import annotations

from typing import Protocol

from .models import (
    ContextAvailability,
    ContextBundle,
    ContextQuery,
    DisasterContextSnapshot,
    TrafficContextSnapshot,
    WeatherContextSnapshot,
)


class TrafficContextProvider(Protocol):
    def traffic(self, query: ContextQuery) -> TrafficContextSnapshot: ...


class WeatherContextProvider(Protocol):
    def weather(self, query: ContextQuery) -> WeatherContextSnapshot: ...


class DisasterContextProvider(Protocol):
    def disaster(self, query: ContextQuery) -> DisasterContextSnapshot: ...


class NullContextProvider:
    """Explicitly unavailable provider used by HINO-only V0."""

    def __init__(self, status: ContextAvailability = ContextAvailability.UNSUPPORTED):
        if status not in {ContextAvailability.MISSING, ContextAvailability.UNSUPPORTED}:
            raise ValueError("Null provider status must be missing or unsupported")
        self.status = status

    def _base(self, query: ContextQuery) -> dict:
        return {
            "context_timestamp": query.context_timestamp,
            "source_timestamp": None,
            "freshness_seconds": None,
            "quality_status": self.status,
            "provider_name": "null",
            "source_dataset_version": None,
            "error_code": self.status.value.upper(),
        }

    def traffic(self, query: ContextQuery) -> TrafficContextSnapshot:
        return TrafficContextSnapshot(**self._base(query))

    def weather(self, query: ContextQuery) -> WeatherContextSnapshot:
        return WeatherContextSnapshot(**self._base(query))

    def disaster(self, query: ContextQuery) -> DisasterContextSnapshot:
        return DisasterContextSnapshot(**self._base(query))


class SyntheticContextProvider:
    """Returns manually supplied fake snapshots and marks over-age data stale."""

    def __init__(
        self,
        traffic: TrafficContextSnapshot,
        weather: WeatherContextSnapshot,
        disaster: DisasterContextSnapshot,
        maximum_freshness_seconds: float = 900,
    ):
        self.traffic_snapshot = traffic
        self.weather_snapshot = weather
        self.disaster_snapshot = disaster
        self.maximum_freshness_seconds = maximum_freshness_seconds

    def _at_query(self, snapshot, query: ContextQuery):
        source = snapshot.source_timestamp
        freshness = (
            max(0.0, (query.context_timestamp - source).total_seconds()) if source else None
        )
        status = snapshot.quality_status
        if (
            status == ContextAvailability.AVAILABLE
            and freshness is not None
            and freshness > self.maximum_freshness_seconds
        ):
            status = ContextAvailability.STALE
        return snapshot.model_copy(
            update={
                "context_timestamp": query.context_timestamp,
                "freshness_seconds": freshness,
                "quality_status": status,
            }
        )

    def traffic(self, query: ContextQuery) -> TrafficContextSnapshot:
        return self._at_query(self.traffic_snapshot, query)

    def weather(self, query: ContextQuery) -> WeatherContextSnapshot:
        return self._at_query(self.weather_snapshot, query)

    def disaster(self, query: ContextQuery) -> DisasterContextSnapshot:
        return self._at_query(self.disaster_snapshot, query)


class ContextAggregator:
    """Collects optional providers without making them a HINO-model dependency."""

    def __init__(
        self,
        traffic_provider: TrafficContextProvider,
        weather_provider: WeatherContextProvider,
        disaster_provider: DisasterContextProvider,
    ):
        self.traffic_provider = traffic_provider
        self.weather_provider = weather_provider
        self.disaster_provider = disaster_provider

    @staticmethod
    def _error(snapshot_type, query: ContextQuery, code: str):
        return snapshot_type(
            context_timestamp=query.context_timestamp,
            quality_status=ContextAvailability.ERROR,
            provider_name="provider_error_boundary",
            error_code=code,
        )

    def collect(self, query: ContextQuery) -> ContextBundle:
        try:
            traffic = self.traffic_provider.traffic(query)
        except Exception:
            traffic = self._error(TrafficContextSnapshot, query, "TRAFFIC_PROVIDER_ERROR")
        try:
            weather = self.weather_provider.weather(query)
        except Exception:
            weather = self._error(WeatherContextSnapshot, query, "WEATHER_PROVIDER_ERROR")
        try:
            disaster = self.disaster_provider.disaster(query)
        except Exception:
            disaster = self._error(DisasterContextSnapshot, query, "DISASTER_PROVIDER_ERROR")
        return ContextBundle(
            traffic=traffic,
            weather=weather,
            disaster=disaster,
            base_hino_model_available=True,
        )

