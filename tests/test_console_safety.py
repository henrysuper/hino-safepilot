from __future__ import annotations

import json
from pathlib import Path

import pytest

from hino_safepilot.analytics.console_safety import attention_reasons, safety_driver, safety_overview


def driver(driver_id: str, **overrides) -> dict:
    base = {
        "driver_id": driver_id,
        "driver_name": f"模擬駕駛 {driver_id[1:]}",
        "primary_type": 500,
        "new_driver": False,
        "trip_count": 12,
        "scored_trip_count": 12,
        "total_distance_km": 200.0,
        "average_score": 85.0,
        "recent_trips": 10,
        "recent_average_score": 85.0,
        "previous_average_score": 85.0,
        "trend": "stable",
        "trend_label": "持平",
        "series_rank": 1,
        "series_ranked_size": 2,
        "camera_blocked_trip_count": 0,
        "frequent_deductions": [{"item": "speeding", "label": "超速", "trips": 3, "points_lost": 4.0}],
        "category_averages": {"安全警示": 90.0},
        "trips": [],
    }
    base.update(overrides)
    return base


def write_outputs(root: Path, drivers: list[dict]) -> None:
    views = root / "data" / "processed" / "demo_views"
    views.mkdir(parents=True)
    (views / "drivers.local.json").write_text(
        json.dumps({"mock_note": "模擬", "drivers": drivers}, ensure_ascii=False), encoding="utf-8"
    )
    for d in drivers:
        (views / f"driver-{d['driver_id']}.local.json").write_text(
            json.dumps({"predrive": {"briefing": {"reminders": []}}, "reports": [
                {"ref": 1, "date": "2025-03-10", "safety_score": {"overall_score": 80},
                 "risk_timeline": {"peak": {"risk_pct": 70}, "alerts": [{"event_label": "急減速"}, {"event_label": None}]}}
            ]}), encoding="utf-8",
        )
    scores = root / "data" / "processed" / "safety_scores"
    scores.mkdir(parents=True)
    rows = [
        {"overall_score": 90, "distance_km": 30, "grade": "A", "trip_start": "2025-03-10T08:00:00"},
        {"overall_score": 60, "distance_km": 10, "grade": "D", "trip_start": "2025-03-11T08:00:00"},
        {"overall_score": 100, "distance_km": 1.0, "grade": "A", "trip_start": "2025-03-11T09:00:00"},  # short: excluded
    ]
    (scores / "trip_safety_scores.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_attention_rules_flag_only_clear_cases() -> None:
    goals = [{"goal_status": "missed"}] * 4 + [{"goal_status": "achieved"}] * 2
    reasons = attention_reasons(driver(
        "D01", recent_average_score=70.0, previous_average_score=80.0, camera_blocked_trip_count=2, trips=goals,
    ))
    assert len(reasons) == 4  # decline >= 7, recent < 75, camera >= 2, goals 33% <= 40%
    assert attention_reasons(driver("D02", recent_average_score=80.0, previous_average_score=85.0,
                                    camera_blocked_trip_count=1)) == []  # -5 points, one blocked trip


def test_overview_weights_by_distance_and_skips_short_trips(tmp_path) -> None:
    write_outputs(tmp_path, [driver("D01", average_score=90.0, total_distance_km=300.0),
                             driver("D02", average_score=70.0, total_distance_km=100.0, recent_average_score=70.0)])
    overview = safety_overview(tmp_path)
    assert overview["available"] is True
    assert overview["fleet"]["average_score"] == pytest.approx(85.0)
    assert overview["fleet"]["scored_full_trips"] == 2
    assert overview["fleet"]["grade_counts"] == {"A": 1, "D": 1}
    assert overview["monthly"][0]["average_score"] == pytest.approx((90 * 30 + 60 * 10) / 40)
    assert [w["driver_id"] for w in overview["watch_list"]] == ["D02"]


def test_driver_report_summarises_risk_and_validates_id(tmp_path) -> None:
    write_outputs(tmp_path, [driver("D01")])
    report = safety_driver(tmp_path, "D01")
    assert report["summary"]["driver_id"] == "D01"
    assert report["reports"][0]["risk_summary"] == {"peak_risk_pct": 70, "alerts": 2, "alerts_followed_by_event": 1}
    assert "risk_timeline" not in report["reports"][0]
    for bad in ("../x", "D1", "D01/..", ""):
        with pytest.raises(ValueError):
            safety_driver(tmp_path, bad)


def test_missing_outputs_return_a_message(tmp_path) -> None:
    assert safety_overview(tmp_path)["available"] is False
    assert "hino-analytics" in safety_overview(tmp_path)["message"]


def test_console_serves_javascript_with_executable_type() -> None:
    import mimetypes

    import hino_safepilot.fuel.server  # noqa: F401  (registers the types on import)

    assert mimetypes.guess_type("app.js")[0] == "text/javascript"
    assert mimetypes.guess_type("styles.css")[0] == "text/css"
