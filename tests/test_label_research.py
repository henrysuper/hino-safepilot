from __future__ import annotations

from datetime import datetime, timedelta

from hino_safepilot.label_research.models import OnsetRecord, ScenarioConfig, TripRecord
from hino_safepilot.label_research.simulation import (
    future_horizon_status,
    history_diagnostics,
    history_is_valid,
    simulate_scenario,
    telemetry_aligned_anchors,
)


def synthetic_trip() -> TripRecord:
    start = datetime(2025, 1, 1, 8)
    timestamps = [start + timedelta(seconds=10 * index) for index in range(71)]
    return TripRecord(
        trip_id="synthetic-trip",
        vehicle_key="synthetic-vehicle",
        journey_code="25010100synthetic",
        timestamps=timestamps,
        raw_row_count=len(timestamps) + 1,
        duplicate_timestamp_row_count=2,
        gps_speed_out_of_range_rows=0,
        azimuth_out_of_range_rows=0,
        missing_gps_rows=0,
        malformed_rows=0,
        distance_km=10,
        distance_quality="valid",
        driving_hours=0.2,
        driving_hours_quality="valid",
        duration_seconds=700,
        duration_quality="valid",
    )


def synthetic_onset(at: datetime, *, mismatch: bool = False) -> OnsetRecord:
    return OnsetRecord(
        trip_id="synthetic-trip",
        vehicle_key="synthetic-vehicle",
        event_type=7,
        event_name="decelerate",
        start=at,
        start_quality="valid",
        observation_count=1,
        first_observed=at,
        last_observed=at,
        payload_variant_count=1,
        end_variant_count=0,
        payload_conflict=False,
        end_conflict=False,
        start_ambiguous=False,
        year_mismatch=mismatch,
        start_after_telemetry=False,
    )


def test_telemetry_aligned_anchor_cadence_deduplicates_timestamp() -> None:
    start = datetime(2025, 1, 1)
    timestamps = [start, start, start + timedelta(seconds=4), start + timedelta(seconds=10)]
    assert telemetry_aligned_anchors(timestamps, "every_telemetry_point") == [
        start,
        start + timedelta(seconds=4),
        start + timedelta(seconds=10),
    ]
    assert telemetry_aligned_anchors(timestamps, "10s") == [
        start,
        start + timedelta(seconds=10),
    ]


def test_history_window_excludes_anchor_and_supports_recommended_policy() -> None:
    trip = synthetic_trip()
    anchor = trip.start + timedelta(seconds=300)
    diagnostics = history_diagnostics(trip.timestamps, anchor)
    assert diagnostics["telemetry_point_count"] == 30
    assert diagnostics["elapsed_history_seconds"] == 300
    assert history_is_valid(diagnostics, "recommended_240s_10points_gap60s")


def test_horizon_requires_future_observation_and_censors_trip_end() -> None:
    start = datetime(2025, 1, 1)
    timestamps = [start, start + timedelta(seconds=61), start + timedelta(seconds=120)]
    status, _ = future_horizon_status(
        timestamps, start, "full_60s_with_future_observation"
    )
    assert status == "ambiguous"
    status, _ = future_horizon_status(
        timestamps, start + timedelta(seconds=61), "full_60s_with_future_observation"
    )
    assert status == "censored"


def test_label_interval_is_open_at_anchor_and_closed_at_sixty_seconds() -> None:
    trip = synthetic_trip()
    anchor = trip.start + timedelta(seconds=300)
    scenario = ScenarioConfig(
        name="synthetic",
        factor="test",
        anchor_cadence="10s",
        history_policy="strict_300s",
        horizon_policy="full_60s_with_future_observation",
        quality_policy="keep_all_timestamp_valid",
    )
    current_only, _ = simulate_scenario(
        scenario, {trip.trip_id: trip}, [synthetic_onset(anchor)]
    )
    assert current_only["positive_anchors"] == 0
    boundary, _ = simulate_scenario(
        scenario,
        {trip.trip_id: trip},
        [synthetic_onset(anchor + timedelta(seconds=60))],
    )
    assert boundary["positive_anchors"] > 0
    assert boundary["positives_by_event_type"]["7"] > 0


def test_year_mismatch_onset_is_quarantined_from_simulation() -> None:
    trip = synthetic_trip()
    onset = synthetic_onset(trip.start + timedelta(seconds=360), mismatch=True)
    result, _ = simulate_scenario(
        ScenarioConfig(name="synthetic", factor="test", quality_policy="keep_all_timestamp_valid"),
        {trip.trip_id: trip},
        [onset],
    )
    assert result["positive_anchors"] == 0
