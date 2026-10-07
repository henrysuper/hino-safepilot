"""Back-office (fleet console) payloads for the safety score: fleet overview and driver reports.

Reads the local, identifier-free outputs of `hino-analytics score` / `drivers` / `export-demo`;
it computes nothing new about individual trips. Driver identities are the simulated roster.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .scoring import SHORT_TRIP_KM, distance_weighted_mean


DRIVER_ID = re.compile(r"^D\d{2,3}$")
LOW_RECENT_SCORE = 75
DECLINE_POINTS = 7
CAMERA_BLOCKED_TRIPS = 2
LOW_GOAL_RATE = 0.4
GOAL_WINDOW = 10
MISSING_MESSAGE = (
    "尚未產生安全分數資料：請先執行 hino-analytics score、drivers、export-demo"
    "（見 docs/team_setup_guide.md 第 6 節）。"
)


def _views_root(repo_root: Path) -> Path:
    return repo_root / "data" / "processed" / "demo_views"


def _read_json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _goal_rate(trips: list[dict[str, Any]]) -> tuple[float | None, int]:
    checked = [t["goal_status"] for t in trips if t.get("goal_status") in ("achieved", "missed")][-GOAL_WINDOW:]
    if not checked:
        return None, 0
    return sum(status == "achieved" for status in checked) / len(checked), len(checked)


def attention_reasons(driver: dict[str, Any]) -> list[str]:
    """Deterministic watch-list rules for the fleet manager."""

    reasons: list[str] = []
    recent, previous = driver.get("recent_average_score"), driver.get("previous_average_score")
    if recent is not None and previous is not None and previous - recent >= DECLINE_POINTS:
        reasons.append(f"分數退步：最近 {driver['recent_trips']} 趟平均比前 {driver['recent_trips']} 趟低 {previous - recent:.0f} 分")
    if recent is not None and recent < LOW_RECENT_SCORE:
        reasons.append(f"最近 {driver['recent_trips']} 趟平均 {recent:.0f} 分，低於 {LOW_RECENT_SCORE} 分")
    if (driver.get("camera_blocked_trip_count") or 0) >= CAMERA_BLOCKED_TRIPS:
        reasons.append(f"{driver['camera_blocked_trip_count']} 趟車內鏡頭遮蔽（疲勞、手機、分心可能偵測不到）")
    rate, checked = _goal_rate(driver.get("trips", []))
    if rate is not None and checked >= 5 and rate <= LOW_GOAL_RATE:
        reasons.append(f"最近 {checked} 次改善目標只達成 {rate:.0%}")
    return reasons


def _driver_row(driver: dict[str, Any]) -> dict[str, Any]:
    rate, checked = _goal_rate(driver.get("trips", []))
    top = driver.get("frequent_deductions") or []
    return {
        "driver_id": driver["driver_id"],
        "driver_name": driver["driver_name"],
        "series": driver.get("primary_type"),
        "new_driver": driver.get("new_driver", False),
        "average_score": driver.get("average_score"),
        "recent_average_score": driver.get("recent_average_score"),
        "previous_average_score": driver.get("previous_average_score"),
        "trend": driver.get("trend"),
        "trend_label": driver.get("trend_label"),
        "series_rank": driver.get("series_rank"),
        "series_ranked_size": driver.get("series_ranked_size"),
        "trip_count": driver.get("trip_count"),
        "scored_trip_count": driver.get("scored_trip_count"),
        "total_distance_km": driver.get("total_distance_km"),
        "camera_blocked_trip_count": driver.get("camera_blocked_trip_count", 0),
        "road_adjusted_average": driver.get("road_adjusted_average"),
        "goal_rate": round(rate * 100) if rate is not None else None,
        "goal_checked": checked,
        "top_deduction": top[0]["label"] if top else None,
        "attention": attention_reasons(driver),
    }


def safety_overview(repo_root: Path) -> dict[str, Any]:
    index = _read_json(_views_root(repo_root) / "drivers.local.json")
    scores_path = repo_root / "data" / "processed" / "safety_scores" / "trip_safety_scores.jsonl"
    if index is None or not scores_path.is_file():
        return {"available": False, "message": MISSING_MESSAGE}

    full_trips: list[tuple[float, float, str, str]] = []
    with scores_path.open(encoding="utf-8") as handle:
        for line in handle:
            trip = json.loads(line)
            if trip["overall_score"] is not None and (trip["distance_km"] or 0) >= SHORT_TRIP_KM:
                full_trips.append(
                    (trip["overall_score"], trip["distance_km"], trip["grade"], trip["trip_start"][:7])
                )
    grades = defaultdict(int)
    monthly: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for score, km, grade, month in full_trips:
        grades[grade] += 1
        monthly[month].append((score, km))

    drivers = [_driver_row(driver) for driver in index["drivers"] if driver.get("trip_count")]
    by_series: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in drivers:
        by_series[row["series"]].append(row)
    series = [
        {
            "series": key,
            "drivers": len(rows),
            "average_score": distance_weighted_mean((r["average_score"], r["total_distance_km"]) for r in rows),
            "total_km": sum(r["total_distance_km"] or 0 for r in rows),
            "camera_blocked_trips": sum(r["camera_blocked_trip_count"] for r in rows),
        }
        for key, rows in sorted(by_series.items(), key=lambda item: (item[0] is None, item[0]))
    ]
    fleet_average = distance_weighted_mean((r["average_score"], r["total_distance_km"]) for r in drivers)
    watch = sorted(
        (row for row in drivers if row["attention"]),
        key=lambda row: (-len(row["attention"]), row["recent_average_score"] or 0),
    )
    return {
        "available": True,
        "generated_at": datetime.fromtimestamp(scores_path.stat().st_mtime).isoformat(timespec="minutes"),
        "mock_note": index.get("mock_note"),
        "fleet": {
            "average_score": fleet_average,
            "drivers": len(drivers),
            "scored_full_trips": len(full_trips),
            "total_km": sum(r["total_distance_km"] or 0 for r in drivers),
            "grade_counts": dict(sorted(grades.items())),
            "watch_count": len(watch),
        },
        "monthly": [
            {"month": month, "trips": len(rows), "average_score": distance_weighted_mean(rows)}
            for month, rows in sorted(monthly.items())
        ],
        "series": series,
        "watch_list": watch,
        "drivers": drivers,
        "rules": {
            "low_recent_score": LOW_RECENT_SCORE,
            "decline_points": DECLINE_POINTS,
            "camera_blocked_trips": CAMERA_BLOCKED_TRIPS,
            "low_goal_rate_pct": round(LOW_GOAL_RATE * 100),
            "goal_window": GOAL_WINDOW,
            "short_trip_km": SHORT_TRIP_KM,
        },
    }


def safety_driver(repo_root: Path, driver_id: str) -> dict[str, Any]:
    if not DRIVER_ID.match(driver_id or ""):
        raise ValueError("Unknown driver id")
    root = _views_root(repo_root)
    index = _read_json(root / "drivers.local.json")
    detail = _read_json(root / f"driver-{driver_id}.local.json")
    if index is None or detail is None:
        return {"available": False, "message": MISSING_MESSAGE}
    driver = next((d for d in index["drivers"] if d["driver_id"] == driver_id), None)
    if driver is None:
        raise ValueError("Unknown driver id")
    reports = []
    for report in detail["reports"]:
        timeline = report.get("risk_timeline")
        reports.append(
            {
                **{key: value for key, value in report.items() if key != "risk_timeline"},
                "risk_summary": {
                    "peak_risk_pct": timeline["peak"]["risk_pct"],
                    "alerts": len(timeline["alerts"]),
                    "alerts_followed_by_event": sum(a["event_label"] is not None for a in timeline["alerts"]),
                }
                if timeline
                else None,
            }
        )
    return {
        "available": True,
        "mock_note": index.get("mock_note"),
        "summary": _driver_row(driver),
        "history": driver,
        "next_trip": detail["predrive"],
        "reports": reports,
    }
