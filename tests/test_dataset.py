from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from hino_safepilot.dataset.audit import validate_model_schema
from hino_safepilot.dataset.builder import (
    LeakageEvidence,
    _active_core_at_anchor,
    _available_past_onsets,
    _crosses_split_boundary,
    _complete_feature_source_max,
    _features,
    _future_status,
    _history_diagnostics,
    _in_embargo,
    _labels,
    _trip_quality_reason,
    _trusted_onset_row,
)
from hino_safepilot.dataset.models import TripTelemetry
from hino_safepilot.dataset.models import EventOnset


def synthetic_trip(*, start: datetime | None = None, duration_seconds: int = 700) -> TripTelemetry:
    origin = start or datetime(2025, 7, 1, 8)
    timestamps = [origin + timedelta(seconds=10 * index) for index in range(duration_seconds // 10 + 1)]
    count = len(timestamps)
    values = {
        "can_speed": [30.0] * count,
        "gps_speed": [31.0] * count,
        "engine_rpm": [1200.0] * count,
        "engine_load": [40.0] * count,
        "coolant_temp": [80.0] * count,
        "battery_mv": [24000.0] * count,
        "fuel_level": [70.0] * count,
        "can_status": [0] * count,
        "vehicle_status": [1] * count,
    }
    qualities = {name: ["valid"] * count for name in values}
    return TripTelemetry(
        trip_id="synthetic-trip",
        vehicle_id="synthetic-vehicle",
        vehicle_series_code=500,
        timestamps=timestamps,
        values=values,
        qualities=qualities,
    )


def test_positive_future_event_and_negative_horizon() -> None:
    anchor = datetime(2025, 7, 1, 8, 5)
    evidence = LeakageEvidence()
    positive = _labels(anchor, {7: [anchor + timedelta(seconds=60)]}, evidence)
    negative = _labels(anchor, {7: [anchor + timedelta(seconds=61)]}, evidence)
    assert positive["label_any_core_risk_60s"] == 1
    assert positive["label_decelerate_60s"] == 1
    assert negative["label_any_core_risk_60s"] == 0


def test_current_event_is_neither_feature_nor_future_label() -> None:
    trip = synthetic_trip()
    anchor = trip.start + timedelta(seconds=300)
    history = _history_diagnostics(trip.timestamps, anchor)
    features, _ = _features(trip, anchor, {7: [anchor]}, [], history)
    labels = _labels(anchor, {7: [anchor]}, LeakageEvidence())
    assert features["decelerate_onset_count_300s"] == 0
    assert labels["label_decelerate_60s"] == 0


def test_feature_before_anchor_included_exact_anchor_and_future_excluded() -> None:
    trip = synthetic_trip()
    anchor = trip.start + timedelta(seconds=300)
    index = trip.timestamps.index(anchor)
    trip.values["can_speed"][index - 1] = 45.0
    trip.values["can_speed"][index] = 90.0
    trip.values["can_speed"][index + 1] = 120.0
    features, maximum_source = _features(
        trip, anchor, defaultdict(list), [], _history_diagnostics(trip.timestamps, anchor)
    )
    assert features["can_speed_max_30s"] == 45.0
    assert maximum_source == anchor - timedelta(seconds=10)


def test_backfilled_onset_not_available_until_first_observation() -> None:
    anchor = datetime(2025, 7, 1, 8, 5)
    event = EventOnset(
        event_type=7,
        start=anchor - timedelta(seconds=20),
        first_observed=anchor + timedelta(seconds=10),
        active_end=None,
    )
    assert _available_past_onsets([event], anchor) == {}


def test_feature_provenance_includes_event_availability_time() -> None:
    anchor = datetime(2025, 7, 1, 8, 5)
    event = EventOnset(
        event_type=7,
        start=anchor - timedelta(seconds=20),
        first_observed=anchor - timedelta(seconds=5),
        active_end=None,
    )
    maximum = _complete_feature_source_max(
        anchor,
        anchor - timedelta(seconds=10),
        [event],
        [anchor - timedelta(seconds=8)],
    )
    assert maximum == anchor - timedelta(seconds=5)


def test_censored_horizon() -> None:
    trip = synthetic_trip(duration_seconds=300)
    assert _future_status(trip.timestamps, trip.end - timedelta(seconds=50)) == "censored"


def test_split_boundary_and_embargo() -> None:
    boundary = datetime(2025, 9, 1)
    assert _in_embargo(boundary - timedelta(seconds=60))
    assert _in_embargo(boundary + timedelta(seconds=300))
    assert not _in_embargo(boundary - timedelta(seconds=61))
    assert _crosses_split_boundary(boundary - timedelta(seconds=1), boundary)


def test_identity_coordinates_and_post_trip_features_are_rejected() -> None:
    audit = validate_model_schema(
        ["can_speed_mean_300s", "vehicle_id", "latitude_deg", "post_trip_score"]
    )
    assert audit["status"] == "FAIL"
    assert set(audit["forbidden_columns"]) == {
        "latitude_deg",
        "post_trip_score",
        "vehicle_id",
    }


def test_speeding_is_predictor_but_not_v0_label() -> None:
    trip = synthetic_trip()
    anchor = trip.start + timedelta(seconds=300)
    past_speeding = anchor - timedelta(seconds=10)
    features, _ = _features(
        trip,
        anchor,
        {8: [past_speeding]},
        [past_speeding],
        _history_diagnostics(trip.timestamps, anchor),
    )
    labels = _labels(anchor, {8: [anchor + timedelta(seconds=10)]}, LeakageEvidence())
    assert features["speeding_onset_count_30s"] == 1
    assert features["speeding_observation_count_30s"] == 1
    assert labels["label_any_core_risk_60s"] == 0


def test_active_event_exclusion() -> None:
    anchor = datetime(2025, 7, 1, 8, 5)
    assert _active_core_at_anchor(
        {7: [(anchor - timedelta(seconds=10), anchor + timedelta(seconds=10))]}, anchor
    )
    assert not _active_core_at_anchor({7: [(anchor, anchor + timedelta(seconds=10))]}, anchor)


def test_1970_year_mismatch_is_not_trusted() -> None:
    row = {
        "trip_id": "synthetic-trip",
        "event_type": 7,
        "event_start_time": datetime(1970, 1, 1),
        "event_start_time_quality": "valid",
        "start_time_ambiguous": False,
        "start_year_mismatch_observed": True,
    }
    assert not _trusted_onset_row(row)


def test_ultra_long_trip_is_excluded() -> None:
    trip = synthetic_trip(duration_seconds=12 * 3600 + 10)
    assert _trip_quality_reason(trip) == "ULTRA_LONG_TRIP_OVER_12H"
