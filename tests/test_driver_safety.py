from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from hino_safepilot.analytics.driver_safety import (
    attribute_trips,
    build_driver_histories,
    build_driver_history,
    load_virtual_drivers,
)
from hino_safepilot.analytics.engine import vehicle_profile_id
from hino_safepilot.analytics.score_pipeline import build_driver_safety, local_drivers_view
from hino_safepilot.analytics.scoring import SafetyScorer, load_safety_score_config

from test_scoring import CONFIG_PATH, summary_for


VEHICLE_A, VEHICLE_B = "synthetic-vehicle-a", "synthetic-vehicle-b"
DAY_1 = datetime(2025, 3, 10, 8)
FORBIDDEN_VIEW_KEYS = {"trip_id", "enabled_code", "vehicle_profile_id", "latitude", "longitude"}


def write_fuel_outputs(root: Path, assignment: list[tuple[str, datetime, str | None]]) -> Path:
    fuel = root / "data" / "processed" / "fuel"
    (fuel / "artifacts").mkdir(parents=True)
    roster = [
        {"driver_id": "D01", "driver_name": "模擬駕駛 01", "primary_type": 500, "new_driver": False},
        {"driver_id": "D02", "driver_name": "模擬駕駛 02", "primary_type": 500, "new_driver": True},
        {"driver_id": "D03", "driver_name": "模擬駕駛 03", "primary_type": 500, "new_driver": False},
    ]
    (fuel / "artifacts" / "virtual_drivers.json").write_text(json.dumps(roster, ensure_ascii=False), encoding="utf-8")
    (fuel / "artifacts" / "mock_plates.json").write_text(
        json.dumps({"note": "模擬車牌", "plates": {"V01": "SIM-5001", "V02": "SIM-5002"}}), encoding="utf-8"
    )
    (fuel / "vehicle_aliases.json").write_text(json.dumps({VEHICLE_A: "V01", VEHICLE_B: "V02"}), encoding="utf-8")
    table = pa.table(
        {
            "enabled_code": [code for code, _, _ in assignment],
            "day": pa.array([day for _, day, _ in assignment], type=pa.timestamp("ns")),
            "driver_id": [driver for _, _, driver in assignment],
        }
    )
    pq.write_table(table, fuel / "driver_assignment.parquet")
    return fuel


def score(scorer, trip_id: str, start: datetime, vehicle: str = VEHICLE_A, **counts):
    trip = scorer.score_trip(summary_for(trip_id=trip_id, start=start, **counts))
    return trip.model_copy(update={"vehicle_profile_id": vehicle_profile_id(vehicle)})


@pytest.fixture(scope="module")
def scorer():
    return SafetyScorer(load_safety_score_config(CONFIG_PATH))


def test_trips_follow_the_vehicle_day_assignment(tmp_path, scorer) -> None:
    day_1, day_2 = DAY_1.replace(hour=0), (DAY_1 + timedelta(days=1)).replace(hour=0)
    drivers = load_virtual_drivers(
        write_fuel_outputs(
            tmp_path,
            [(VEHICLE_A, day_1, "D01"), (VEHICLE_B, day_1, "D02"), (VEHICLE_A, day_2, None)],
        )
    )
    trips = [
        score(scorer, "synthetic-a1", DAY_1),
        score(scorer, "synthetic-a2", DAY_1 + timedelta(hours=3)),
        score(scorer, "synthetic-b1", DAY_1, vehicle=VEHICLE_B),
        score(scorer, "synthetic-a3", DAY_1 + timedelta(days=1)),  # unassigned day
        score(scorer, "synthetic-a4", DAY_1 + timedelta(days=5)),  # no assignment row
    ]
    groups, unassigned = attribute_trips(trips, drivers)
    assert [s.trip_id for s, _ in groups["D01"]] == ["synthetic-a1", "synthetic-a2"]
    assert [alias for _, alias in groups["D02"]] == ["V02"]
    assert unassigned == 2


def test_history_summarizes_scores_trend_and_deductions(tmp_path, scorer) -> None:
    days = [DAY_1 + timedelta(days=offset) for offset in (0, 10, 40, 45, 50)]
    drivers = load_virtual_drivers(
        write_fuel_outputs(tmp_path, [(VEHICLE_A, day.replace(hour=0), "D01") for day in days])
    )
    trips = [
        score(scorer, "synthetic-1", days[0]),
        score(scorer, "synthetic-2", days[1]),
        score(scorer, "synthetic-3", days[2], speeding=15),
        score(scorer, "synthetic-4", days[3], speeding=15, decelerate=12),
        score(scorer, "synthetic-5", days[4], lens_ward_off=1),
    ]
    histories, unassigned = build_driver_histories(trips, drivers, scoring_version="test")
    history = next(h for h in histories if h.driver_id == "D01")
    assert unassigned == 0
    assert history.trip_count == history.scored_trip_count == 5
    assert history.total_distance_km == pytest.approx(100)
    assert history.trend == "unavailable"  # 5 trips < 10 recent + previous trips
    groups, _ = attribute_trips(trips, drivers)
    windowed = build_driver_history(
        drivers.by_id["D01"], groups["D01"], drivers, recent_trips=2, scoring_version="test"
    )
    assert windowed.trend == "declining"  # last 2 trips vs the 2 before
    assert history.frequent_deductions[0]["item"] == "speeding"
    assert history.camera_blocked_trip_count == 1
    assert [m["month"] for m in history.monthly_average_scores] == ["2025-03", "2025-04"]
    assert history.trips[0].mock_plate == "SIM-5001"
    assert {h.driver_id for h in histories} == {"D01", "D02", "D03"}
    empty = next(h for h in histories if h.driver_id == "D03")
    assert empty.trip_count == 0 and empty.average_score is None and empty.trend == "unavailable"


def test_build_writes_once_and_requires_fuel_outputs(tmp_path, scorer) -> None:
    scores_dir = tmp_path / "data" / "processed" / "safety_scores"
    scores_dir.mkdir(parents=True)
    trip = score(scorer, "synthetic-a1", DAY_1)
    (scores_dir / "trip_safety_scores.jsonl").write_text(trip.model_dump_json() + "\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="hino-fuel drivers"):
        build_driver_safety(tmp_path)
    write_fuel_outputs(tmp_path, [(VEHICLE_A, DAY_1.replace(hour=0), "D01")])
    manifest = build_driver_safety(tmp_path)
    assert manifest["output"]["attributed_trip_count"] == 1
    assert (tmp_path / "data" / "processed" / "driver_safety" / "driver_histories.jsonl").is_file()
    with pytest.raises(FileExistsError):
        build_driver_safety(tmp_path)


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | set().union(*(_keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(child) for child in value)) if value else set()
    return set()


def test_drivers_view_is_identifier_free_and_limits_details(tmp_path, scorer) -> None:
    days = [DAY_1 + timedelta(days=offset) for offset in range(4)]
    drivers = load_virtual_drivers(
        write_fuel_outputs(tmp_path, [(VEHICLE_A, day.replace(hour=0), "D01") for day in days])
    )
    trips = [score(scorer, f"synthetic-{i}", day, decelerate=i) for i, day in enumerate(days)]
    trips[3] = scorer.score_trip(summary_for(trip_id="synthetic-3", start=days[3], distance_km=1.0)).model_copy(
        update={"vehicle_profile_id": vehicle_profile_id(VEHICLE_A)}
    )  # latest trip too short to score
    histories, _ = build_driver_histories(trips, drivers, scoring_version="test")
    view = local_drivers_view(
        [h.model_dump(mode="json") for h in histories],
        {trip.trip_id: trip for trip in trips},
        detail_limit=2,
    )
    assert view["data_classification"] == "private_local"
    assert not (_keys(view) & FORBIDDEN_VIEW_KEYS)
    d01 = next(d for d in view["drivers"] if d["driver_id"] == "D01")
    assert [t["ref"] for t in d01["trips"]] == [1, 2, 3, 4]
    # details cover the latest 2 *scored* trips; the unscored latest trip is skipped
    assert [t["detail"] is not None for t in d01["trips"]] == [False, True, True, False]
    assert d01["trips"][0]["date"] == "2025-03-10"


def test_ranking_is_within_series_and_needs_enough_trips(tmp_path, scorer) -> None:
    from hino_safepilot.analytics.driver_safety import rank_within_series

    days = [DAY_1 + timedelta(days=offset) for offset in range(12)]
    drivers = load_virtual_drivers(
        write_fuel_outputs(
            tmp_path,
            [(VEHICLE_A, day.replace(hour=0), "D01") for day in days[:6]]
            + [(VEHICLE_B, day.replace(hour=0), "D02") for day in days[:6]]
            + [(VEHICLE_A, day.replace(hour=0), "D03") for day in days[6:]],
        )
    )
    trips = (
        [score(scorer, f"a-{i}", day) for i, day in enumerate(days[:6])]
        + [score(scorer, f"b-{i}", day, vehicle=VEHICLE_B, speeding=10) for i, day in enumerate(days[:6])]
        + [score(scorer, f"c-{i}", day, decelerate=30) for i, day in enumerate(days[6:])]
    )
    histories, _ = build_driver_histories(trips, drivers, scoring_version="test")
    ranked = {h.driver_id: h for h in rank_within_series(histories, minimum_trips=5)}
    assert (ranked["D01"].series_rank, ranked["D01"].series_ranked_size) == (1, 3)
    assert ranked["D02"].series_rank == 2 and ranked["D03"].series_rank == 3
    # default minimum (10 scored trips) leaves these 6-trip drivers unranked
    assert all(h.series_rank is None for h in histories)


def test_goal_checks_use_only_earlier_trips_and_track_streaks(tmp_path, scorer) -> None:
    from hino_safepilot.analytics.driver_safety import goal_checks
    from hino_safepilot.analytics.goals import load_goal_config

    root = Path(__file__).resolve().parents[1]
    goal_config = load_goal_config(root / "config" / "behavior_goal_v0.json")
    days = [DAY_1 + timedelta(days=offset) for offset in range(6)]
    counts = [8, 8, 8, 2, 1, 9]  # heavy braking, then improvement, then a relapse
    summaries = {
        f"t{i}": summary_for(trip_id=f"t{i}", start=day, decelerate=count)
        for i, (day, count) in enumerate(zip(days, counts))
    }
    trips = [
        (scorer.score_trip(summaries[f"t{i}"]), "V01") for i in range(len(days))
    ]
    checks = goal_checks(trips, summaries, goal_config, profile_key="driver:D01", rate_floor_km=10.0)
    assert checks["t0"]["status"] == "no_goal"  # nothing earlier to learn from
    later = [checks[f"t{i}"] for i in range(2, 6)]
    assert {c["target_metric"] for c in later if c["status"] != "no_goal"} == {"decelerate"}
    assert checks["t3"]["status"] == "achieved" and checks["t4"]["status"] == "achieved"
    assert checks["t4"]["streak"] == checks["t3"]["streak"] + 1
    assert checks["t5"]["status"] == "missed" and checks["t5"]["streak"] == 0
    assert checks["t3"]["trip_value"] == pytest.approx(2 / 20 * 100)  # 20 km trip, floor 10 km


def test_alert_replay_links_next_core_event_within_horizon() -> None:
    from hino_safepilot.analytics.trip_alerts import review_alerts

    t0 = datetime(2025, 9, 20, 8)
    alerts = [
        {"trip_id": "trip-a", "timestamp": t0, "probability": 0.62, "reason": "new_risk_episode"},
        {"trip_id": "trip-a", "timestamp": t0 + timedelta(minutes=10), "probability": 0.55, "reason": "new_risk_episode"},
        {"trip_id": "trip-b", "timestamp": t0, "probability": 0.71, "reason": "risk_escalation"},
    ]
    onsets = {
        "trip-a": [
            (t0 - timedelta(seconds=5), "decelerate"),  # before the alert: ignored
            (t0 + timedelta(seconds=26), "decelerate"),  # within 60 s: linked
            (t0 + timedelta(minutes=12), "ldws"),  # 120 s after the 2nd alert: too late
        ]
    }
    reviews = review_alerts(alerts, onsets, "test")
    a = reviews["trip-a"]
    assert a["alert_count"] == 2 and a["event_followed_count"] == 1
    assert a["alerts"][0]["event_name"] == "decelerate" and a["alerts"][0]["lead_seconds"] == 26
    assert a["alerts"][1]["event_name"] is None
    assert reviews["trip-b"]["event_followed_count"] == 0 and reviews["trip-b"]["split"] == "test"
