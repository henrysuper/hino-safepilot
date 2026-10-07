from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hino_safepilot.analytics.engine import TripAnalytics
from hino_safepilot.analytics.models import DailyImprovementGoal, QualityStatus, TripSafetyScore
from hino_safepilot.analytics.score_pipeline import (
    local_postdrive_view,
    local_predrive_view,
    resolve_score_config,
)
from hino_safepilot.analytics.scoring import (
    SafetyScorer,
    build_predrive_briefing,
    load_safety_score_config,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "safety_score_v1.json"
FORBIDDEN_VIEW_KEYS = {"trip_id", "enabled_code", "vehicle_profile_id", "latitude", "longitude"}


def telemetry_rows(
    *,
    trip_id: str = "synthetic-trip-a",
    start: datetime = datetime(2025, 3, 10, 8),
    distance_km: float = 20.0,
) -> list[dict]:
    steps = 5
    rows = []
    for index in range(steps):
        rows.append(
            {
                "source_row_number": index + 2,
                "trip_id": trip_id,
                "enabled_code": "synthetic-vehicle-a",
                "timestamp": start + timedelta(minutes=index * 5),
                "timestamp_quality": "valid",
                "vehicle_series_code": 500,
                "vehicle_series_code_quality": "valid",
                "vehicle_status": 1,
                "vehicle_status_quality": "valid",
                "gps_speed_kph": 50.0,
                "gps_speed_kph_quality": "valid",
                "gps_speed_limit": 60.0,
                "gps_speed_limit_quality": "valid",
                "can_speed_kph": 50.0,
                "can_speed_kph_quality": "valid",
                "total_mileage_km": 1000 + distance_km * index / (steps - 1),
                "total_mileage_km_quality": "valid",
                "total_fuel_used_l": 100 + index * 1.0,
                "total_fuel_used_l_quality": "valid",
                "engine_load_pct": 40.0,
                "engine_load_pct_quality": "valid",
                "gps_coordinates_missing": False,
                "row_has_malformed_value": False,
                "row_has_out_of_range_value": False,
                "is_duplicate_source_key": False,
            }
        )
    return rows


def onsets(rows: list[dict], **counts: int) -> list[dict]:
    start = rows[0]["timestamp"]
    events = []
    for name, count in counts.items():
        for index in range(count):
            events.append(
                {
                    "event_onset_candidate_id": f"synthetic-{name}-{index}",
                    "trip_id": rows[0]["trip_id"],
                    "event_name": name,
                    "event_start_time": start + timedelta(seconds=30 + index),
                    "event_start_time_quality": "valid",
                    "start_time_ambiguous": False,
                    "start_year_mismatch_observed": False,
                }
            )
    return events


def summary_for(distance_km: float = 20.0, trip_id: str = "synthetic-trip-a", start=None, **counts):
    rows = telemetry_rows(
        trip_id=trip_id,
        distance_km=distance_km,
        **({"start": start} if start else {}),
    )
    return TripAnalytics().analyze_trip(rows, onsets(rows, **counts))


@pytest.fixture(scope="module")
def config():
    return load_safety_score_config(CONFIG_PATH)


@pytest.fixture(scope="module")
def scorer(config):
    return SafetyScorer(config)


def test_clean_trip_scores_100_grade_a(scorer) -> None:
    score = scorer.score_trip(summary_for())
    assert score.score_status == "scored"
    assert score.overall_score == pytest.approx(100)
    assert score.grade == "A"
    assert score.top_deductions == []
    assert score.applied_caps == []


def test_reference_rate_maps_to_reference_score_within_category(scorer, config) -> None:
    # 12 decelerate events over 20 km = 60 per 100 km = the configured reference.
    score = scorer.score_trip(summary_for(decelerate=12))
    smooth = next(item for item in score.categories if item.category == "smooth_driving")
    expected_category = 100 * 0.7 ** config.categories["smooth_driving"]["items"]["decelerate"]["weight"]
    assert smooth.score == pytest.approx(expected_category)
    assert score.overall_score == pytest.approx(100 - 0.25 * (100 - expected_category))
    assert score.top_deductions[0].item == "decelerate"
    assert score.top_deductions[0].points_lost == pytest.approx(0.25 * (100 - expected_category))


def test_more_events_never_increase_score(scorer) -> None:
    values = [scorer.score_trip(summary_for(speeding=count)).overall_score for count in (0, 3, 8, 20)]
    assert values == sorted(values, reverse=True)
    assert values[-1] < values[0]


def test_short_trip_is_not_scored(scorer) -> None:
    score = scorer.score_trip(summary_for(distance_km=0.3, decelerate=1))
    assert score.score_status == "insufficient_exposure"
    assert score.overall_score is None
    assert score.grade is None
    assert "TRIP_DISTANCE_BELOW_SCORING_MINIMUM" in score.quality_warnings


def test_rate_distance_floor_limits_short_trip_penalty(scorer) -> None:
    score = scorer.score_trip(summary_for(distance_km=5.0, decelerate=1))
    deduction = next(item for item in score.top_deductions if item.item == "decelerate")
    assert deduction.observed_value == pytest.approx(10.0)  # 1 event / 10 km floor
    assert "RATE_DISTANCE_FLOOR_APPLIED" in score.quality_warnings


def test_critical_event_caps_overall_score(scorer) -> None:
    score = scorer.score_trip(summary_for(pcs=1))
    assert score.overall_score == pytest.approx(79)
    assert score.grade == "C"
    assert score.applied_caps == ["PCS_EVENT_CAP_79"]


def test_camera_blocking_caps_score_and_flags_dms(scorer) -> None:
    score = scorer.score_trip(summary_for(lens_ward_off=1))
    assert score.dms_reliability == "camera_blocked"
    assert "DMS_CAMERA_BLOCKED_EVENTS_MAY_BE_UNDERCOUNTED" in score.quality_warnings
    assert score.overall_score <= 89
    assert score.grade != "A"
    assert "LENS_WARD_OFF_EVENT_CAP_89" in score.applied_caps


def test_heavy_camera_blocking_lowers_safety_category(scorer) -> None:
    light = scorer.score_trip(summary_for(lens_ward_off=1))
    heavy = scorer.score_trip(summary_for(lens_ward_off=40))
    category = lambda score: next(c.score for c in score.categories if c.category == "safety_alerts")
    assert category(heavy) < category(light) < 100
    assert heavy.overall_score < light.overall_score


def test_clean_trip_keeps_dms_reliable(scorer) -> None:
    score = scorer.score_trip(summary_for())
    assert score.dms_reliability == "ok"
    assert not any("DMS" in warning for warning in score.quality_warnings)


def test_config_declares_every_event_it_reads(config) -> None:
    from hino_safepilot.analytics.engine import TRACKED_EVENT_NAMES

    assert config.required_event_names <= set(TRACKED_EVENT_NAMES)
    definitions = json.loads((ROOT / "config" / "event_definitions_v1.json").read_text(encoding="utf-8"))
    scored_items = {
        item for category in config.categories.values() for item in category["items"]
    }
    assert scored_items <= set(definitions["events"])


def test_scoring_refuses_stale_analytics_without_new_events(tmp_path) -> None:
    from hino_safepilot.analytics.score_pipeline import build_safety_scores

    summary = summary_for()
    stale = summary.model_copy(
        update={"events": [event for event in summary.events if event.event_name != "lens_ward_off"]}
    )
    analytics = tmp_path / "data" / "processed" / "analytics"
    analytics.mkdir(parents=True)
    (analytics / "trip_summaries.jsonl").write_text(stale.model_dump_json() + "\n", encoding="utf-8")
    (analytics / "improvement_goals.jsonl").write_text("", encoding="utf-8")
    config = resolve_score_config(tmp_path, score_config_path=CONFIG_PATH)
    with pytest.raises(ValueError, match="lens_ward_off"):
        build_safety_scores(config)
    assert not config.output_root.exists()


def test_postdrive_view_includes_definitions_and_cap_labels(scorer) -> None:
    from hino_safepilot.analytics.score_pipeline import load_event_definitions

    summary = summary_for(decelerate=6, pcs=1)
    score = scorer.score_trip(summary)
    definitions = load_event_definitions(ROOT / "config" / "event_definitions_v1.json")
    view = local_postdrive_view(summary, score, {"summary": "synthetic"}, "mock", definitions)
    deduction = next(d for d in view["safety_score"]["top_deductions"] if d["label"] == "急減速")
    assert "15 km/h" in deduction["definition"]
    assert view["safety_score"]["applied_caps"] == [{"label": "預警式防護（PCS）", "cap": 79}]
    assert view["safety_score"]["dms_reliability"] == "ok"


def test_config_rejects_weights_that_do_not_sum_to_one(tmp_path) -> None:
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["categories"]["speed_control"]["weight"] = 0.5
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="category weights"):
        load_safety_score_config(path)


def _scored(scorer, trip_id: str, start: datetime, **counts) -> TripSafetyScore:
    return scorer.score_trip(summary_for(trip_id=trip_id, start=start, **counts))


def test_briefing_uses_only_trips_before_cutoff(scorer, config) -> None:
    cutoff = datetime(2025, 3, 20, 8)
    history = [
        _scored(scorer, "synthetic-old", cutoff - timedelta(days=3), decelerate=4),
        _scored(scorer, "synthetic-last", cutoff - timedelta(days=1)),
        _scored(scorer, "synthetic-future", cutoff + timedelta(hours=1), fatigue=3),
    ]
    briefing = build_predrive_briefing(
        history,
        vehicle_profile_id=history[0].vehicle_profile_id,
        cutoff_exclusive=cutoff,
        config=config,
    )
    assert briefing.last_trip_score == pytest.approx(100)
    assert briefing.recent_scored_trip_count == 2
    assert briefing.recent_critical_event_counts["fatigue"] == 0
    assert all("疲勞" not in reminder.message for reminder in briefing.reminders)


def test_briefing_flags_decline_and_critical_events_first(scorer, config) -> None:
    cutoff = datetime(2025, 3, 20, 8)
    history = [
        _scored(scorer, "synthetic-previous", cutoff - timedelta(days=10)),
        _scored(scorer, "synthetic-recent", cutoff - timedelta(days=2), phone=2, speeding=15),
    ]
    goal = DailyImprovementGoal(
        vehicle_profile_id=history[0].vehicle_profile_id,
        effective_at=cutoff,
        target_metric="speeding",
        priority_score=0.6,
        reason_codes=["RATE_ABOVE_30D_BASELINE"],
        evidence_summary={"recent_value": 75.0, "baseline_value": 20.0, "unit": "events_per_100km", "change_pct": 275.0},
        quality_status=QualityStatus.VALID,
        scoring_version="behavior-goal-v0",
    )
    briefing = build_predrive_briefing(
        history,
        vehicle_profile_id=history[0].vehicle_profile_id,
        cutoff_exclusive=cutoff,
        config=config,
        daily_goal=goal,
    )
    assert briefing.trend == "declining"
    codes = [reminder.code for reminder in briefing.reminders]
    assert "RECENT_PHONE_EVENTS" in codes and "SCORE_DECLINING" in codes
    assert "DAILY_GOAL" not in codes
    assert briefing.daily_goal == goal
    priorities = [reminder.priority for reminder in briefing.reminders]
    assert priorities == sorted(priorities, key=lambda item: item != "high")
    assert briefing.weakest_category == "speed_control"


def test_briefing_without_history_asks_for_baseline(config) -> None:
    briefing = build_predrive_briefing(
        [], vehicle_profile_id="synthetic", cutoff_exclusive=datetime(2025, 3, 20), config=config
    )
    assert briefing.trend == "unavailable"
    assert briefing.last_trip_score is None
    assert [reminder.code for reminder in briefing.reminders] == ["INSUFFICIENT_RECENT_TRIPS"]


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | set().union(*(_keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(child) for child in value)) if value else set()
    return set()


def test_local_demo_views_contain_no_identifiers(scorer, config) -> None:
    summary = summary_for(decelerate=3)
    score = scorer.score_trip(summary)
    briefing = build_predrive_briefing(
        [score],
        vehicle_profile_id=score.vehicle_profile_id,
        cutoff_exclusive=score.trip_end + timedelta(hours=1),
        config=config,
    )
    categories = {name: item["label"] for name, item in config.categories.items()}
    report = {"summary": "synthetic", "next_trip_focus": "synthetic"}
    views = [
        local_predrive_view(briefing, None, categories),
        local_postdrive_view(summary, score, report, "mock"),
    ]
    for view in views:
        assert view["data_classification"] == "private_local"
        assert not (_keys(view) & FORBIDDEN_VIEW_KEYS)
        json.dumps(view, ensure_ascii=False)
    assert views[1]["safety_score"]["overall_score"] == round(score.overall_score)


def test_demo_server_only_maps_local_view_files() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("serve_demo", ROOT / "scripts" / "serve_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = object.__new__(module.DemoHandler)
    handler.directory = str(module.DEMO)
    allowed = Path(handler.translate_path("/local-data/postdrive.local.json?x=1"))
    assert allowed == module.LOCAL_VIEWS / "postdrive.local.json"
    for attempt in (
        "/local-data/../analytics/trip_summaries.jsonl",
        "/local-data/..%2F..%2Fraw%2Fsecret.local.json",
        "/local-data/trip_summaries.jsonl",
    ):
        resolved = Path(handler.translate_path(attempt)).resolve()
        assert resolved.parent == module.LOCAL_VIEWS.resolve()
        assert not resolved.exists()


def test_score_output_must_stay_below_processed(tmp_path) -> None:
    with pytest.raises(ValueError, match="data/processed"):
        resolve_score_config(tmp_path, output_root=Path("reports/scores"))


def test_road_factors_are_relative_to_fleet_and_clipped() -> None:
    from hino_safepilot.analytics.road_context import road_factors

    context = {
        "typical_rates": {
            "harsh_brake_per_km": 0.6,  # 2x the fleet: city-heavy trip
            "harsh_acc_per_km": 0.01,  # far below the fleet: clipped to 0.5
            "idle_min_per_km": None,  # unknown: not adjusted
            "overspeed_share": 0.2,
        }
    }
    fleet = {"harsh_brake_per_km": 0.3, "harsh_acc_per_km": 0.05, "idle_min_per_km": 0.3, "overspeed_share": 0.2}
    factors = road_factors(context, fleet)
    assert factors["decelerate"] == pytest.approx(2.0)
    assert factors["accelerate"] == pytest.approx(0.5)
    assert factors["speeding"] == pytest.approx(1.0)
    assert "idle" not in factors


def test_road_adjustment_only_rescales_the_adjusted_items(scorer) -> None:
    summary = summary_for(decelerate=12, phone=1)
    plain = scorer.score_trip(summary)
    adjusted = scorer.score_trip(summary, {"decelerate": 2.0})
    smooth = lambda s: next(c.score for c in s.categories if c.category == "smooth_driving")
    alerts = lambda s: next(c.score for c in s.categories if c.category == "safety_alerts")
    assert smooth(adjusted) > smooth(plain)  # half the braking rate after a 2x road factor
    assert alerts(adjusted) == pytest.approx(alerts(plain))  # DMS items untouched
    assert scorer.score_trip(summary, {}).overall_score == pytest.approx(plain.overall_score)


def test_trips_between_half_and_two_km_are_scored(scorer) -> None:
    score = scorer.score_trip(summary_for(distance_km=1.0, decelerate=1))
    assert score.score_status == "scored"
    assert score.overall_score is not None


def test_distance_weighted_mean_ignores_missing_and_weights_by_km() -> None:
    from hino_safepilot.analytics.scoring import distance_weighted_mean

    assert distance_weighted_mean([(100, 1.0), (50, 9.0)]) == pytest.approx(55.0)
    assert distance_weighted_mean([(100, 1.0), (None, 9.0), (70, 0)]) == pytest.approx(100.0)
    assert distance_weighted_mean([]) is None
