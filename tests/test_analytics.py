from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hino_safepilot.analytics.baseline import build_behavior_baseline
from hino_safepilot.analytics.engine import TripAnalytics
from hino_safepilot.analytics.goals import WeaknessScorer, load_goal_config
from hino_safepilot.analytics.models import QualityStatus


def telemetry_rows(
    *,
    trip_id: str = "synthetic-trip-a",
    start: datetime = datetime(2025, 1, 2, 8),
    mileage: tuple[float, ...] = (100, 101, 102, 103),
    fuel: tuple[float, ...] = (10, 10.2, 10.4, 10.6),
) -> list[dict]:
    rows = []
    for index, (distance, fuel_value) in enumerate(zip(mileage, fuel)):
        rows.append(
            {
                "source_row_number": index + 2,
                "trip_id": trip_id,
                "enabled_code": "synthetic-vehicle-a",
                "timestamp": start + timedelta(minutes=index),
                "timestamp_quality": "valid",
                "vehicle_series_code": 500,
                "vehicle_series_code_quality": "valid",
                "vehicle_status": 1 if index < 3 else 2,
                "vehicle_status_quality": "valid",
                "gps_speed_kph": 20.0 + index * 5,
                "gps_speed_kph_quality": "valid",
                "gps_speed_limit": 50.0,
                "gps_speed_limit_quality": "valid",
                "can_speed_kph": 20.0 + index * 5,
                "can_speed_kph_quality": "valid",
                "total_mileage_km": distance,
                "total_mileage_km_quality": "valid",
                "total_fuel_used_l": fuel_value,
                "total_fuel_used_l_quality": "valid",
                "engine_load_pct": 30.0 + index,
                "engine_load_pct_quality": "valid",
                "gps_coordinates_missing": False,
                "row_has_malformed_value": False,
                "row_has_out_of_range_value": False,
                "is_duplicate_source_key": False,
            }
        )
    return rows


def onset(
    event_name: str,
    at: datetime,
    candidate_id: str = "synthetic-event-a",
    trip_id: str = "synthetic-trip-a",
) -> dict:
    return {
        "event_onset_candidate_id": candidate_id,
        "trip_id": trip_id,
        "event_name": event_name,
        "event_start_time": at,
        "event_start_time_quality": "valid",
        "start_time_ambiguous": False,
        "start_year_mismatch_observed": False,
    }


def test_trip_distance_duration_event_counts_and_rates() -> None:
    rows = telemetry_rows()
    event = onset("decelerate", rows[1]["timestamp"])
    summary = TripAnalytics().analyze_trip(rows, [event])
    assert summary.basics.distance_km.value == pytest.approx(3.0)
    assert summary.basics.duration_seconds.value == 180
    decelerate = next(item for item in summary.events if item.event_name == "decelerate")
    assert decelerate.count == 1
    assert decelerate.per_100_km.value == pytest.approx(100 / 3)
    assert decelerate.per_driving_hour.value == pytest.approx(20)


def test_cumulative_fuel_reset_sums_valid_positive_segments() -> None:
    rows = telemetry_rows(fuel=(10, 11, 0.5, 1.0))
    summary = TripAnalytics().analyze_trip(rows, [])
    assert summary.fuel.fuel_used_l.value == pytest.approx(1.5)
    assert summary.fuel.reset_or_decrease_count == 1
    assert summary.fuel.fuel_used_l.quality_status == QualityStatus.DEGRADED


def test_rate_is_unavailable_when_distance_is_zero() -> None:
    rows = telemetry_rows(mileage=(100, 100, 100, 100))
    event = onset("decelerate", rows[1]["timestamp"])
    summary = TripAnalytics().analyze_trip(rows, [event])
    metric = next(item for item in summary.events if item.event_name == "decelerate")
    assert metric.per_100_km.value is None
    assert metric.per_100_km.quality_status == QualityStatus.UNAVAILABLE


def test_event_context_includes_before_current_and_after_only_for_post_trip() -> None:
    rows = telemetry_rows()
    event = onset("decelerate", rows[2]["timestamp"])
    context = TripAnalytics().analyze_trip(rows, [event]).notable_events[0]
    assert context.previous_30s_speed_mean_kph is None
    assert context.current_speed_kph == 30
    assert context.next_30s_speed_mean_kph == 30
    assert 60 <= context.trip_progress_pct <= 70


def test_baseline_uses_only_trips_strictly_before_cutoff() -> None:
    analyzer = TripAnalytics()
    first = analyzer.analyze_trip(telemetry_rows(start=datetime(2025, 1, 1, 8)), [])
    second = analyzer.analyze_trip(
        telemetry_rows(trip_id="synthetic-trip-b", start=datetime(2025, 1, 2, 8)), []
    )
    future = analyzer.analyze_trip(
        telemetry_rows(trip_id="synthetic-trip-c", start=datetime(2025, 1, 4, 8)), []
    )
    baseline = build_behavior_baseline(
        [future, second, first],
        vehicle_profile_id=first.vehicle_profile_id,
        cutoff_exclusive=datetime(2025, 1, 3, 8),
        previous_days=7,
        minimum_trip_count=1,
    )
    assert baseline.trip_count == 2
    assert baseline.latest_trip_end < baseline.cutoff_exclusive


def test_previous_n_trip_baseline_is_past_only() -> None:
    analyzer = TripAnalytics()
    summaries = [
        analyzer.analyze_trip(
            telemetry_rows(
                trip_id=f"synthetic-trip-{index}",
                start=datetime(2025, 1, index + 1, 8),
            ),
            [],
        )
        for index in range(4)
    ]
    baseline = build_behavior_baseline(
        summaries,
        vehicle_profile_id=summaries[0].vehicle_profile_id,
        cutoff_exclusive=datetime(2025, 1, 4, 7),
        previous_n_trips=2,
        minimum_trip_count=1,
    )
    assert baseline.trip_count == 2
    assert baseline.latest_trip_end < datetime(2025, 1, 4, 7)


def test_weakness_scoring_and_daily_goal_are_config_driven() -> None:
    analyzer = TripAnalytics()
    start = datetime(2025, 1, 1, 8)
    summaries = []
    for index in range(5):
        rows = telemetry_rows(trip_id=f"synthetic-trip-{index}", start=start + timedelta(days=index))
        events = [onset("decelerate", rows[1]["timestamp"], f"event-{index}", rows[0]["trip_id"])] * (index + 1)
        summaries.append(analyzer.analyze_trip(rows, events))
    profile = summaries[0].vehicle_profile_id
    recent = build_behavior_baseline(
        summaries, vehicle_profile_id=profile, cutoff_exclusive=start + timedelta(days=6), previous_days=3, minimum_trip_count=1
    )
    historical = build_behavior_baseline(
        summaries, vehicle_profile_id=profile, cutoff_exclusive=start + timedelta(days=3), previous_days=3, minimum_trip_count=1
    )
    config = load_goal_config(Path("config/behavior_goal_v0.json"))
    scorer = WeaknessScorer(config)
    scores = scorer.score(recent, historical, recent, historical)
    goal = scorer.daily_goal(vehicle_profile_id=profile, effective_at=start + timedelta(days=6), scores=scores)
    assert scores[0].metric == "decelerate"
    assert scores[0].priority_score > 0
    assert goal.target_metric == "decelerate"
    assert goal.scoring_version == "behavior-goal-v0"
