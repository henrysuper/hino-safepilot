"""Driver-facing safety history on top of the simulated driver roster (此為模擬資料).

The telemetry has no driver identity. The fuel pipeline (`hino-fuel drivers`) assigns
each real vehicle-day to one simulated driver. This module reuses that assignment, so
the safety history and the dispatch console agree on who drove which trip. Scores
themselves come from real behaviour via `TripSafetyScore`.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pyarrow.parquet as pq

from .engine import vehicle_profile_id
from .models import DriverSafetyHistory, DriverTripRecord, TripSafetyScore, TripSummary
from .scoring import ITEM_LABELS, SHORT_TRIP_KM, distance_weighted_mean


DEFAULT_MOCK_NOTE = (
    "駕駛名單、車牌與行程分配為模擬資料；每趟的駕駛行為與分數來自真實資料。"
    "模擬分配會依當天的行為風格挑選駕駛，所以駕駛之間的差異（含排名）有一部分是分配規則造成的，"
    "只用來展示流程，不代表真實駕駛的好壞；接上真實排班後即可直接套用。"
)
MIN_RANKED_TRIPS = 10
TREND_DELTA_POINTS = 3.0


@dataclass(frozen=True)
class VirtualDrivers:
    roster: list[dict]
    assignment: dict[tuple[str, date], str]
    aliases: dict[str, str]
    plates: dict[str, str]
    mock_note: str

    @property
    def by_id(self) -> dict[str, dict]:
        return {driver["driver_id"]: driver for driver in self.roster}


def load_virtual_drivers(fuel_root: Path) -> VirtualDrivers:
    """Read the `hino-fuel drivers` outputs below ``fuel_root`` (data/processed/fuel)."""

    required = {
        "roster": fuel_root / "artifacts" / "virtual_drivers.json",
        "plates": fuel_root / "artifacts" / "mock_plates.json",
        "aliases": fuel_root / "vehicle_aliases.json",
        "assignment": fuel_root / "driver_assignment.parquet",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Virtual driver outputs are missing (run `hino-fuel drivers` or copy them from "
            f"the fuel handoff): {missing}"
        )
    plates_payload = json.loads(required["plates"].read_text(encoding="utf-8"))
    assignment: dict[tuple[str, date], str] = {}
    for row in pq.read_table(required["assignment"], columns=["enabled_code", "day", "driver_id"]).to_pylist():
        if row["driver_id"] is None or row["day"] is None:
            continue
        day = row["day"].date() if isinstance(row["day"], datetime) else row["day"]
        assignment[(str(row["enabled_code"]), day)] = str(row["driver_id"])
    return VirtualDrivers(
        roster=json.loads(required["roster"].read_text(encoding="utf-8")),
        assignment=assignment,
        aliases=json.loads(required["aliases"].read_text(encoding="utf-8")),
        plates=dict(plates_payload.get("plates", {})),
        mock_note=DEFAULT_MOCK_NOTE,
    )


def attribute_trips(
    scores: Iterable[TripSafetyScore], drivers: VirtualDrivers
) -> tuple[dict[str, list[tuple[TripSafetyScore, str]]], int]:
    """Group trip scores by driver using (vehicle, trip start date). Returns (groups, unassigned)."""

    code_by_profile = {vehicle_profile_id(code): code for code in drivers.aliases}
    groups: dict[str, list[tuple[TripSafetyScore, str]]] = defaultdict(list)
    unassigned = 0
    for score in scores:
        code = code_by_profile.get(score.vehicle_profile_id)
        driver_id = drivers.assignment.get((code, score.trip_start.date())) if code else None
        if driver_id is None:
            unassigned += 1
            continue
        groups[driver_id].append((score, drivers.aliases[code]))
    for trips in groups.values():
        trips.sort(key=lambda item: (item[0].trip_start, item[0].trip_id))
    return groups, unassigned


def build_driver_history(
    driver: dict,
    trips: list[tuple[TripSafetyScore, str]],
    drivers: VirtualDrivers,
    *,
    recent_trips: int = 10,
    scoring_version: str,
) -> DriverSafetyHistory:
    """Retrospective safety history of one simulated driver."""

    scored = [score for score, _ in trips if score.overall_score is not None]
    records = [
        DriverTripRecord(
            trip_id=score.trip_id,
            trip_start=score.trip_start,
            vehicle_alias=alias,
            mock_plate=drivers.plates.get(alias),
            distance_km=score.distance_km,
            score_status=score.score_status,
            overall_score=score.overall_score,
            grade=score.grade,
            top_deduction=ITEM_LABELS.get(score.top_deductions[0].item, score.top_deductions[0].item)
            if score.top_deductions
            else None,
            dms_reliability=score.dms_reliability,
            short_trip=score.distance_km is not None and score.distance_km < SHORT_TRIP_KM,
        )
        for score, alias in trips
    ]

    category_values: dict[str, list[tuple[float, float | None]]] = defaultdict(list)
    deduction_points: Counter[str] = Counter()
    deduction_trips: Counter[str] = Counter()
    for score in scored:
        for category in score.categories:
            if category.score is not None:
                category_values[category.label].append((category.score, score.distance_km))
        for deduction in score.top_deductions:
            deduction_points[deduction.item] += deduction.points_lost
            deduction_trips[deduction.item] += 1

    # Trip-based windows over full-length trips (months are not contiguous, so day windows are
    # often empty); each window is distance-weighted.
    full = [
        (score.overall_score, score.distance_km)
        for score in scored
        if (score.distance_km or 0) >= SHORT_TRIP_KM
    ]
    recent = full[-recent_trips:]
    previous = full[-2 * recent_trips : -recent_trips] if len(full) > recent_trips else []
    recent_average, previous_average = distance_weighted_mean(recent), distance_weighted_mean(previous)
    if recent_average is None or previous_average is None:
        trend = "unavailable"
    elif recent_average - previous_average >= TREND_DELTA_POINTS:
        trend = "improving"
    elif previous_average - recent_average >= TREND_DELTA_POINTS:
        trend = "declining"
    else:
        trend = "stable"

    monthly: dict[str, list[tuple[float, float | None]]] = defaultdict(list)
    for score in scored:
        monthly[score.trip_start.strftime("%Y-%m")].append((score.overall_score, score.distance_km))

    return DriverSafetyHistory(
        driver_id=str(driver["driver_id"]),
        driver_name=str(driver.get("driver_name", driver["driver_id"])),
        primary_type=driver.get("primary_type"),
        new_driver=bool(driver.get("new_driver", False)),
        trip_count=len(trips),
        scored_trip_count=len(scored),
        total_distance_km=sum(score.distance_km or 0 for score, _ in trips),
        average_score=distance_weighted_mean((score.overall_score, score.distance_km) for score in scored),
        grade_counts=dict(sorted(Counter(score.grade for score in scored).items())),
        category_averages={
            label: value
            for label, values in category_values.items()
            if (value := distance_weighted_mean(values)) is not None
        },
        frequent_deductions=[
            {
                "item": item,
                "label": ITEM_LABELS.get(item, item),
                "trips": deduction_trips[item],
                "points_lost": round(points, 2),
            }
            for item, points in deduction_points.most_common(3)
        ],
        recent_trips=recent_trips,
        recent_average_score=recent_average,
        previous_average_score=previous_average,
        trend=trend,
        camera_blocked_trip_count=sum(score.dms_reliability == "camera_blocked" for score, _ in trips),
        monthly_average_scores=[
            {"month": month, "average_score": round(distance_weighted_mean(values), 1), "trips": len(values)}
            for month, values in sorted(monthly.items())
        ],
        trips=records,
        scoring_version=scoring_version,
        mock_note=drivers.mock_note,
    )


def build_driver_histories(
    scores: Iterable[TripSafetyScore], drivers: VirtualDrivers, *, scoring_version: str
) -> tuple[list[DriverSafetyHistory], int]:
    groups, unassigned = attribute_trips(scores, drivers)
    histories = [
        build_driver_history(
            driver, groups.get(driver["driver_id"], []), drivers, scoring_version=scoring_version
        )
        for driver in drivers.roster
    ]
    return rank_within_series(histories), unassigned


def rank_within_series(
    histories: list[DriverSafetyHistory], minimum_trips: int = MIN_RANKED_TRIPS
) -> list[DriverSafetyHistory]:
    """Rank drivers by average score within their vehicle series (1 = best).

    Drivers with fewer than ``minimum_trips`` scored trips are not ranked."""

    ranks: dict[str, tuple[int, int]] = {}
    by_series: dict[int | None, list[DriverSafetyHistory]] = defaultdict(list)
    for history in histories:
        if history.average_score is not None and history.scored_trip_count >= minimum_trips:
            by_series[history.primary_type].append(history)
    for members in by_series.values():
        ordered = sorted(members, key=lambda h: (-(h.average_score or 0), h.driver_id))
        for position, history in enumerate(ordered, 1):
            ranks[history.driver_id] = (position, len(ordered))
    return [
        history.model_copy(
            update={
                "series_rank": ranks[history.driver_id][0],
                "series_ranked_size": ranks[history.driver_id][1],
            }
        )
        if history.driver_id in ranks
        else history
        for history in histories
    ]


GOAL_UNIT_LABELS = {"events_per_100km": "次／100 km", "idle_pct": "%", "km_per_l": "km/L"}


def _trip_metric(summary: TripSummary, metric: str, rate_floor_km: float) -> float | None:
    """The goal metric for one trip, on the same footing as the safety score's rates."""

    if metric == "idle":
        return summary.basics.idle_pct.value
    if metric == "fuel_efficiency":
        return summary.fuel.fuel_efficiency_km_per_l.value
    distance = summary.basics.distance_km.value
    count = next((event.count for event in summary.events if event.event_name == metric), None)
    if distance is None or count is None:
        return None
    return count / max(distance, rate_floor_km) * 100


def goal_checks(
    trips: list[tuple[TripSafetyScore, str]],
    summaries: dict[str, TripSummary],
    goal_config,
    *,
    profile_key: str,
    rate_floor_km: float,
) -> dict[str, dict]:
    """Per full-length scored trip: the goal shown before it (earlier trips only) and whether it was met.

    A higher-is-worse goal is met when the trip is at or below the driver's recent average
    that the goal was based on; a lower-is-worse goal when it is at or above it. ``streak``
    counts consecutive met goals up to and including the trip.
    """

    from .goals import WeaknessScorer
    from .pipeline import goal_baselines

    scorer = WeaknessScorer(goal_config)
    history = [
        summaries[score.trip_id].model_copy(update={"vehicle_profile_id": profile_key})
        for score, _ in trips
        if score.trip_id in summaries
    ]
    checks: dict[str, dict] = {}
    streak = 0
    for score, _ in trips:
        summary = summaries.get(score.trip_id)
        if score.overall_score is None or summary is None:
            continue
        if (score.distance_km or 0) < SHORT_TRIP_KM:
            continue  # a sub-2 km trip says little about the goal; it neither counts nor breaks a streak
        baselines = goal_baselines(history, profile_key, score.trip_start, goal_config)
        goal = scorer.daily_goal(
            vehicle_profile_id=profile_key,
            effective_at=score.trip_start,
            scores=scorer.score(
                baselines["recent"],
                baselines["historical"],
                baselines["trend_current"],
                baselines["trend_previous"],
            ),
        )
        if not goal.target_metric:
            checks[score.trip_id] = {"status": "no_goal", "streak": streak}
            continue
        metric = goal.target_metric
        goal_value = goal.evidence_summary.get("recent_value")
        trip_value = _trip_metric(summary, metric, rate_floor_km)
        unit = str(goal.evidence_summary.get("unit") or "")
        if goal_value is None or trip_value is None:
            status = "unavailable"
        else:
            higher_is_worse = goal_config.metrics[metric]["direction"] == "higher_is_worse"
            met = trip_value <= goal_value if higher_is_worse else trip_value >= goal_value
            status = "achieved" if met else "missed"
            streak = streak + 1 if met else 0
        checks[score.trip_id] = {
            "target_metric": metric,
            "target_label": ITEM_LABELS.get(metric, metric),
            "goal_value": round(float(goal_value), 1) if goal_value is not None else None,
            "trip_value": round(trip_value, 1) if trip_value is not None else None,
            "unit_label": GOAL_UNIT_LABELS.get(unit, unit),
            "direction": goal_config.metrics[metric]["direction"],
            "status": status,
            "streak": streak,
        }
    return checks
