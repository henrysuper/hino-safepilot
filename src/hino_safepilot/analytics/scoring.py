"""Config-driven trip safety score and past-only pre-drive briefing.

Scores are deterministic functions of one TripSummary. Pre-drive briefings only read
trips that ended strictly before the briefing cutoff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

from .models import (
    CategoryScore,
    DailyImprovementGoal,
    PreDriveBriefing,
    SafetyReminder,
    ScoreDeduction,
    TripSafetyScore,
    TripSummary,
)


ITEM_LABELS = {
    "accelerate": "急加速",
    "decelerate": "急減速",
    "speeding": "超速",
    "engine_overloading": "引擎過載",
    "idle": "怠速比例",
    "ldws": "車道偏移警示",
    "pcs": "預警式防護（PCS）",
    "fatigue": "疲勞",
    "distract": "分心",
    "phone": "使用手機",
    "seatbelt": "未繫安全帶",
    "smoking": "抽菸",
    "lens_ward_off": "鏡頭遮蔽",
    "fuel_efficiency": "燃油效率",
}

CRITICAL_REMINDERS = {
    "pcs": "近 {days} 天出現 {count} 次預警式防護（PCS）事件，今天請與前車保持更長的安全距離。",
    "fatigue": "近 {days} 天偵測到 {count} 次疲勞事件，出發前確認精神狀態，連續駕駛請依規定休息。",
    "lens_ward_off": "近 {days} 天有 {count} 次車內鏡頭遮蔽，出發前請確認 DMS 鏡頭沒有被擋住。",
    "phone": "近 {days} 天有 {count} 次使用手機事件，行駛中請把手機放在固定架並開啟勿擾。",
    "distract": "近 {days} 天有 {count} 次分心事件，行駛中請保持視線在前方路況。",
}

CATEGORY_TIPS = {
    "safety_alerts": "留意車距與車道，保持專注。",
    "smooth_driving": "提早鬆油門、平順煞車與起步。",
    "speed_control": "依速限行駛，下坡與彎道前提早減速。",
    "vehicle_load": "避免長時間高負荷與不必要的怠速。",
}

TREND_DELTA_POINTS = 3.0


@dataclass(frozen=True)
class SafetyScoreConfig:
    raw: dict[str, Any]

    @property
    def version(self) -> str:
        return str(self.raw["version"])

    @property
    def categories(self) -> dict[str, dict[str, Any]]:
        return self.raw["categories"]

    @property
    def predrive(self) -> dict[str, Any]:
        return self.raw["predrive"]

    @property
    def required_event_names(self) -> set[str]:
        """Event names whose counts this config reads from a TripSummary."""

        names = {
            item_name
            for category in self.categories.values()
            for item_name, item in category["items"].items()
            if item["kind"] == "events_per_100km"
        }
        names.update(self.raw.get("critical_event_caps", {}))
        names.update(self.raw.get("dms_blocking_events", []))
        names.update(self.predrive.get("critical_reminder_events", []))
        return names


def load_safety_score_config(path: Path) -> SafetyScoreConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    categories = raw.get("categories", {})
    if not categories:
        raise ValueError("Safety score config requires categories")
    if abs(sum(float(item["weight"]) for item in categories.values()) - 1) > 1e-9:
        raise ValueError("Safety score category weights must sum to 1")
    for name, category in categories.items():
        items = category.get("items", {})
        if not items or abs(sum(float(item["weight"]) for item in items.values()) - 1) > 1e-9:
            raise ValueError(f"Safety score item weights must sum to 1: {name}")
        for item in items.values():
            if float(item["reference"]) <= 0:
                raise ValueError("Safety score references must be positive")
            if item["kind"] not in {"events_per_100km", "idle_pct"}:
                raise ValueError(f"Unsupported safety score item kind: {item['kind']}")
    if not 0 < float(raw["reference_score"]) < 100:
        raise ValueError("reference_score must be between 0 and 100")
    return SafetyScoreConfig(raw)


def grade_for(score: float, thresholds: dict[str, float]) -> str:
    for grade in ("A", "B", "C", "D"):
        if score >= float(thresholds[grade]):
            return grade
    return "E"


class SafetyScorer:
    """Score = 100 * (reference_score / 100) ** weighted_ratio, per category.

    A ratio of 1 means the trip matched the configured reference rate, which maps to
    ``reference_score`` points; zero events map to 100.
    """

    def __init__(self, config: SafetyScoreConfig) -> None:
        self.config = config
        self.decay = float(config.raw["reference_score"]) / 100

    def score_trip(self, summary: TripSummary) -> TripSafetyScore:
        counts = {event.event_name: event.count for event in summary.events}
        distance = summary.basics.distance_km.value
        camera_blocked = any(
            counts.get(name, 0) > 0 for name in self.config.raw.get("dms_blocking_events", [])
        )
        dms_warnings = ["DMS_CAMERA_BLOCKED_EVENTS_MAY_BE_UNDERCOUNTED"] if camera_blocked else []
        common = {
            "trip_id": summary.trip_id,
            "vehicle_profile_id": summary.vehicle_profile_id,
            "trip_start": summary.basics.trip_start,
            "trip_end": summary.basics.trip_end,
            "distance_km": distance,
            "event_counts": counts,
            "dms_reliability": "camera_blocked" if camera_blocked else "ok",
            "scoring_version": self.config.version,
        }
        if distance is None or distance < float(self.config.raw["minimum_scored_distance_km"]):
            return TripSafetyScore(
                **common,
                score_status="insufficient_exposure",
                categories=[
                    CategoryScore(
                        category=name,
                        label=str(category["label"]),
                        weight=float(category["weight"]),
                    )
                    for name, category in self.config.categories.items()
                ],
                top_deductions=[],
                quality_warnings=["TRIP_DISTANCE_BELOW_SCORING_MINIMUM", *dms_warnings],
            )

        rate_distance = max(distance, float(self.config.raw["rate_distance_floor_km"]))
        warnings: list[str] = list(dms_warnings)
        if distance < rate_distance:
            warnings.append("RATE_DISTANCE_FLOOR_APPLIED")
        categories: list[CategoryScore] = []
        for name, category in self.config.categories.items():
            terms: list[tuple[str, float, float | None, str]] = []
            for item_name, item in category["items"].items():
                if item["kind"] == "events_per_100km":
                    observed = counts.get(item_name, 0) / rate_distance * 100
                    unit = "events_per_100km"
                else:
                    observed = summary.basics.idle_pct.value
                    unit = "idle_pct"
                if observed is None:
                    warnings.append(f"{item_name.upper()}_UNAVAILABLE_FOR_SCORE")
                    continue
                ratio = observed / float(item["reference"])
                terms.append((item_name, float(item["weight"]) * ratio, observed, unit))
            if not terms:
                categories.append(
                    CategoryScore(
                        category=name,
                        label=str(category["label"]),
                        weight=float(category["weight"]),
                    )
                )
                continue
            available_weight = sum(
                float(category["items"][item_name]["weight"]) for item_name, *_ in terms
            )
            weighted_ratio = sum(term for _, term, _, _ in terms) / available_weight
            score = 100 * self.decay**weighted_ratio
            lost = 100 - score
            total_term = sum(term for _, term, _, _ in terms)
            deductions = [
                ScoreDeduction(
                    item=item_name,
                    category=name,
                    observed_value=observed,
                    unit=unit,
                    points_lost=lost * term / total_term if total_term > 0 else 0.0,
                )
                for item_name, term, observed, unit in terms
            ]
            categories.append(
                CategoryScore(
                    category=name,
                    label=str(category["label"]),
                    weight=float(category["weight"]),
                    score=score,
                    deductions=sorted(deductions, key=lambda item: -item.points_lost),
                )
            )

        scored = [category for category in categories if category.score is not None]
        total_weight = sum(category.weight for category in scored)
        overall = sum(category.weight * (category.score or 0) for category in scored) / total_weight
        caps: list[str] = []
        for event_name, cap in self.config.raw.get("critical_event_caps", {}).items():
            if counts.get(event_name, 0) > 0 and overall > float(cap):
                overall = float(cap)
                caps.append(f"{event_name.upper()}_EVENT_CAP_{int(cap)}")

        overall_deductions = [
            deduction.model_copy(
                update={"points_lost": deduction.points_lost * category.weight / total_weight}
            )
            for category in scored
            for deduction in category.deductions
            if deduction.points_lost > 0
        ]
        top = sorted(overall_deductions, key=lambda item: -item.points_lost)[:3]
        return TripSafetyScore(
            **common,
            score_status="scored",
            overall_score=overall,
            grade=grade_for(overall, self.config.raw["grade_thresholds"]),
            categories=categories,
            top_deductions=top,
            applied_caps=caps,
            quality_warnings=warnings,
        )


def _average(scores: list[TripSafetyScore]) -> float | None:
    values = [score.overall_score for score in scores if score.overall_score is not None]
    return mean(values) if values else None


def build_predrive_briefing(
    history: Iterable[TripSafetyScore],
    *,
    vehicle_profile_id: str,
    cutoff_exclusive: datetime,
    config: SafetyScoreConfig,
    daily_goal: DailyImprovementGoal | None = None,
) -> PreDriveBriefing:
    """Summarize only trips that ended strictly before ``cutoff_exclusive``."""

    days = int(config.predrive["recent_days"])
    prior = sorted(
        (
            score
            for score in history
            if score.vehicle_profile_id == vehicle_profile_id
            and score.trip_end < cutoff_exclusive
        ),
        key=lambda score: score.trip_end,
    )
    recent_lower = cutoff_exclusive - timedelta(days=days)
    previous_lower = recent_lower - timedelta(days=days)
    recent = [score for score in prior if score.trip_start >= recent_lower]
    previous = [score for score in prior if previous_lower <= score.trip_start < recent_lower]
    recent_scored = [score for score in recent if score.score_status == "scored"]
    last = next((score for score in reversed(prior) if score.score_status == "scored"), None)

    recent_average = _average(recent_scored)
    previous_average = _average(previous)
    if recent_average is None or previous_average is None:
        trend = "unavailable"
    elif recent_average - previous_average >= TREND_DELTA_POINTS:
        trend = "improving"
    elif previous_average - recent_average >= TREND_DELTA_POINTS:
        trend = "declining"
    else:
        trend = "stable"

    category_values: dict[str, list[float]] = {}
    for score in recent_scored:
        for category in score.categories:
            if category.score is not None:
                category_values.setdefault(category.category, []).append(category.score)
    category_means = {name: mean(values) for name, values in category_values.items()}
    weakest = min(category_means, key=lambda name: (category_means[name], name)) if category_means else None

    critical_events = list(config.predrive["critical_reminder_events"])
    critical_counts = {
        event: sum(score.event_counts.get(event, 0) for score in recent) for event in critical_events
    }

    reminders: list[SafetyReminder] = []
    for event in critical_events:
        count = critical_counts[event]
        if count > 0 and event in CRITICAL_REMINDERS:
            reminders.append(
                SafetyReminder(
                    code=f"RECENT_{event.upper()}_EVENTS",
                    priority="high",
                    message=CRITICAL_REMINDERS[event].format(days=days, count=count),
                )
            )
    # The daily goal has its own card in the pre-drive view, so it is not repeated here.
    minimum_recent = int(config.predrive["minimum_recent_scored_trips"])
    if weakest and len(recent_scored) >= minimum_recent:
        label = str(config.categories[weakest]["label"])
        reminders.append(
            SafetyReminder(
                code="WEAKEST_CATEGORY",
                priority="normal",
                message=(
                    f"近 {days} 天「{label}」平均 {category_means[weakest]:.0f} 分，"
                    f"是目前最需要加強的分項：{CATEGORY_TIPS.get(weakest, '')}"
                ),
            )
        )
    if trend == "declining" and recent_average is not None and previous_average is not None:
        reminders.append(
            SafetyReminder(
                code="SCORE_DECLINING",
                priority="high",
                message=(
                    f"近 {days} 天平均分數比前一週下降 {previous_average - recent_average:.0f} 分，"
                    "今天放慢節奏、預留更多反應時間。"
                ),
            )
        )
    if not recent_scored:
        reminders.append(
            SafetyReminder(
                code="INSUFFICIENT_RECENT_TRIPS",
                priority="normal",
                message=f"近 {days} 天沒有足夠長度的行程可以評分，今天以平穩駕駛建立基準。",
            )
        )
    reminders.sort(key=lambda reminder: reminder.priority != "high")

    return PreDriveBriefing(
        vehicle_profile_id=vehicle_profile_id,
        effective_at=cutoff_exclusive,
        last_trip_score=last.overall_score if last else None,
        last_trip_grade=last.grade if last else None,
        recent_days=days,
        recent_average_score=recent_average,
        recent_scored_trip_count=len(recent_scored),
        previous_average_score=previous_average,
        trend=trend,
        weakest_category=weakest,
        weakest_category_score=category_means.get(weakest) if weakest else None,
        recent_critical_event_counts=critical_counts,
        daily_goal=daily_goal,
        reminders=reminders,
        scoring_version=config.version,
    )
