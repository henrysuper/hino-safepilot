from __future__ import annotations

from datetime import datetime, timedelta

from hino_safepilot.context import (
    ContextAggregator,
    ContextAvailability,
    ContextQuery,
    DisasterContextSnapshot,
    NullContextProvider,
    SyntheticContextProvider,
    TrafficContextSnapshot,
    WeatherContextSnapshot,
)


def snapshots(source_time: datetime):
    common = {
        "context_timestamp": source_time,
        "source_timestamp": source_time,
        "freshness_seconds": 0,
        "quality_status": ContextAvailability.AVAILABLE,
        "provider_name": "synthetic",
        "source_dataset_version": "synthetic-v1",
    }
    return (
        TrafficContextSnapshot(**common, congestion_level=0.4, road_event_ahead=False),
        WeatherContextSnapshot(**common, precipitation_mm=1.2, visibility_m=6000),
        DisasterContextSnapshot(**common, heavy_rain_alert=False, flood_alert=False),
    )


def test_null_provider_represents_missing_without_blocking_hino_model() -> None:
    provider = NullContextProvider(ContextAvailability.MISSING)
    query = ContextQuery(context_timestamp=datetime(2025, 1, 1))
    bundle = ContextAggregator(provider, provider, provider).collect(query)
    assert bundle.traffic.quality_status == ContextAvailability.MISSING
    assert bundle.weather.quality_status == ContextAvailability.MISSING
    assert bundle.disaster.quality_status == ContextAvailability.MISSING
    assert bundle.base_hino_model_available


def test_synthetic_provider_available_and_stale_states() -> None:
    source = datetime(2025, 1, 1)
    traffic, weather, disaster = snapshots(source)
    provider = SyntheticContextProvider(traffic, weather, disaster, maximum_freshness_seconds=60)
    fresh = provider.traffic(ContextQuery(context_timestamp=source + timedelta(seconds=30)))
    stale = provider.weather(ContextQuery(context_timestamp=source + timedelta(seconds=61)))
    assert fresh.quality_status == ContextAvailability.AVAILABLE
    assert fresh.freshness_seconds == 30
    assert stale.quality_status == ContextAvailability.STALE


def test_context_aggregator_contains_provider_errors() -> None:
    class Broken:
        def traffic(self, _query):
            raise RuntimeError("synthetic failure")

        def weather(self, _query):
            raise RuntimeError("synthetic failure")

        def disaster(self, _query):
            raise RuntimeError("synthetic failure")

    query = ContextQuery(context_timestamp=datetime(2025, 1, 1))
    bundle = ContextAggregator(Broken(), Broken(), Broken()).collect(query)
    assert bundle.traffic.quality_status == ContextAvailability.ERROR
    assert bundle.weather.quality_status == ContextAvailability.ERROR
    assert bundle.disaster.quality_status == ContextAvailability.ERROR
    assert bundle.base_hino_model_available

