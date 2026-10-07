"""Phase 2.5 analytics artifacts to trip safety scores, briefings, and local demo views."""

from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any
from uuid import uuid4

from .models import DailyImprovementGoal, PreDriveBriefing, TripSafetyScore, TripSummary
from .pipeline import _safe_resolve, _write_jsonl
from .scoring import (
    ITEM_LABELS,
    SHORT_TRIP_KM,
    SafetyScorer,
    build_predrive_briefing,
    load_safety_score_config,
)


LOCAL_MARKER = "LOCAL_PRIVATE_DERIVED"
TREND_LABELS = {
    "improving": "進步中",
    "declining": "退步中",
    "stable": "持平",
    "unavailable": "資料不足",
}
UNIT_LABELS = {"events_per_100km": "次／100 km", "idle_pct": "%"}
NEXT_BEHAVIOR_LABELS = {
    "speed_increased": "加速",
    "speed_decreased": "減速",
    "speed_stable": "速度維持穩定",
    "unavailable": "資料不足",
}


@dataclass(frozen=True)
class ScoreBuildConfig:
    repo_root: Path
    analytics_root: Path
    output_root: Path
    score_config_path: Path


def resolve_score_config(
    repo_root: Path,
    analytics_root: Path = Path("data/processed/analytics"),
    output_root: Path = Path("data/processed/safety_scores"),
    score_config_path: Path = Path("config/safety_score_v1.json"),
) -> ScoreBuildConfig:
    repo = repo_root.resolve()
    source = _safe_resolve(repo, analytics_root)
    output = _safe_resolve(repo, output_root)
    allowed_output = (repo / "data" / "processed").resolve()
    if output == allowed_output or allowed_output not in output.parents:
        raise ValueError("Safety score output must be below data/processed/")
    if source == output or source in output.parents or output in source.parents:
        raise ValueError("Safety score input and output paths must be disjoint")
    return ScoreBuildConfig(repo, source, output, _safe_resolve(repo, score_config_path))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_event_definitions(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {name: str(event["definition"]) for name, event in raw["events"].items()}


def build_safety_scores(config: ScoreBuildConfig) -> dict[str, Any]:
    """Score every trip and build past-only briefings. Performs no API calls."""

    if config.output_root.exists():
        raise FileExistsError(f"Refusing to overwrite safety score output: {config.output_root}")
    summaries_path = config.analytics_root / "trip_summaries.jsonl"
    goals_path = config.analytics_root / "improvement_goals.jsonl"
    if not summaries_path.is_file() or not goals_path.is_file():
        raise FileNotFoundError("Run `hino-analytics build` first: trip summaries and goals are required")

    started = perf_counter()
    score_config = load_safety_score_config(config.score_config_path)
    scorer = SafetyScorer(score_config)
    summaries = [TripSummary.model_validate(record) for record in _read_jsonl(summaries_path)]
    tracked = {event.event_name for summary in summaries[:1] for event in summary.events}
    missing = sorted(score_config.required_event_names - tracked)
    if summaries and missing:
        raise ValueError(
            f"Trip summaries do not track {missing}; delete data/processed/analytics "
            "and rerun `hino-analytics build` before scoring"
        )
    goals = {
        record["subject_trip_id"]: DailyImprovementGoal.model_validate(record["goal"])
        for record in _read_jsonl(goals_path)
    }
    scores = [scorer.score_trip(summary) for summary in summaries]
    by_profile: dict[str, list[TripSafetyScore]] = defaultdict(list)
    for score in scores:
        by_profile[score.vehicle_profile_id].append(score)

    briefing_records = []
    for score in sorted(scores, key=lambda item: (item.trip_start, item.trip_id)):
        briefing = build_predrive_briefing(
            by_profile[score.vehicle_profile_id],
            vehicle_profile_id=score.vehicle_profile_id,
            cutoff_exclusive=score.trip_start,
            config=score_config,
            daily_goal=goals.get(score.trip_id),
        )
        briefing_records.append(
            {"subject_trip_id": score.trip_id, "briefing": briefing.model_dump(mode="json")}
        )

    scored = [score for score in scores if score.overall_score is not None]
    staging = config.output_root.with_name(f".{config.output_root.name}.staging-{uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        score_count = _write_jsonl(
            staging / "trip_safety_scores.jsonl",
            (score.model_dump(mode="json") for score in scores),
        )
        briefing_count = _write_jsonl(staging / "predrive_briefings.jsonl", briefing_records)
        manifest = {
            "safety_score_version": score_config.version,
            "deterministic_only": True,
            "external_api_calls": 0,
            "input": {"trip_summary_count": len(summaries), "goal_count": len(goals)},
            "output": {
                "trip_score_count": score_count,
                "scored_trip_count": len(scored),
                "insufficient_exposure_count": score_count - len(scored),
                "predrive_briefing_count": briefing_count,
                "grade_distribution": dict(sorted(Counter(score.grade for score in scored).items())),
                "median_overall_score": median(score.overall_score for score in scored)
                if scored
                else None,
            },
            "elapsed_seconds": perf_counter() - started,
            "unresolved": [
                "Category weights, reference rates and critical caps are prototype values",
                "No trustworthy driver identity; scores remain vehicle-linked",
            ],
        }
        (staging / "safety_score_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.replace(config.output_root)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return manifest


def load_trip_score(path: Path, trip_id: str) -> TripSafetyScore:
    for record in _read_jsonl(path):
        if record.get("trip_id") == trip_id:
            return TripSafetyScore.model_validate(record)
    raise KeyError("Trip safety score not found")


def load_predrive_briefing(path: Path, trip_id: str) -> PreDriveBriefing:
    for record in _read_jsonl(path):
        if record.get("subject_trip_id") == trip_id:
            return PreDriveBriefing.model_validate(record["briefing"])
    raise KeyError("Pre-drive briefing not found")


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _cap_view(code: str) -> dict[str, Any]:
    event, _, cap = code.rpartition("_EVENT_CAP_")
    return {"label": ITEM_LABELS.get(event.lower(), event.lower()), "cap": int(cap)}


def score_view(
    score: TripSafetyScore, definitions: dict[str, str] | None = None
) -> dict[str, Any]:
    labels = {category.category: category.label for category in score.categories}
    definitions = definitions or {}
    return {
        "status": score.score_status,
        "overall_score": _round(score.overall_score, 0),
        "grade": score.grade,
        "categories": [
            {"category": category.category, "label": category.label, "score": _round(category.score, 0)}
            for category in score.categories
        ],
        "top_deductions": [
            {
                "label": ITEM_LABELS.get(deduction.item, deduction.item),
                "category_label": labels.get(deduction.category, deduction.category),
                "observed_value": _round(deduction.observed_value),
                "unit_label": UNIT_LABELS.get(deduction.unit, deduction.unit),
                "points_lost": _round(deduction.points_lost),
                "definition": definitions.get(deduction.item),
            }
            for deduction in score.top_deductions
        ],
        "applied_caps": [_cap_view(code) for code in score.applied_caps],
        "dms_reliability": score.dms_reliability,
    }


def briefing_view(briefing: PreDriveBriefing, categories: dict[str, str]) -> dict[str, Any]:
    return {
        "last_trip_score": _round(briefing.last_trip_score, 0),
        "last_trip_grade": briefing.last_trip_grade,
        "recent_days": briefing.recent_days,
        "recent_average_score": _round(briefing.recent_average_score, 0),
        "recent_scored_trip_count": briefing.recent_scored_trip_count,
        "previous_average_score": _round(briefing.previous_average_score, 0),
        "trend": briefing.trend,
        "trend_label": TREND_LABELS[briefing.trend],
        "weakest_category_label": categories.get(briefing.weakest_category or ""),
        "weakest_category_score": _round(briefing.weakest_category_score, 0),
        "reminders": [
            {"priority": reminder.priority, "message": reminder.message}
            for reminder in briefing.reminders
        ],
    }


def local_predrive_view(
    briefing: PreDriveBriefing,
    goal_message: dict[str, Any] | None,
    categories: dict[str, str],
) -> dict[str, Any]:
    goal = briefing.daily_goal
    goal_view = None
    display = {"target_label": "暫無改善目標", "rate_unit_label": "資料不足"}
    if goal and goal.target_metric:
        evidence = goal.evidence_summary
        goal_view = {
            "target_metric": goal.target_metric,
            "priority_score": round(goal.priority_score, 2),
            "reason_codes": goal.reason_codes,
            "evidence_summary": {
                "recent_rate": evidence.get("recent_value"),
                "baseline_rate": evidence.get("baseline_value"),
                "change_pct": evidence.get("change_pct"),
                "unit": evidence.get("unit"),
            },
            "quality_status": goal.quality_status.value,
        }
        display = {
            "target_label": f"改善{ITEM_LABELS.get(goal.target_metric, goal.target_metric)}",
            "rate_unit_label": UNIT_LABELS.get(str(evidence.get("unit")), str(evidence.get("unit")))
            + " · 本機資料",
        }
    return {
        "contract_version": "local-predrive-v1",
        "view": "predrive",
        "data_classification": "private_local",
        "prototype_marker": LOCAL_MARKER,
        "daily_improvement_goal": goal_view,
        "message": goal_message,
        "display": display,
        "briefing": briefing_view(briefing, categories),
    }


def local_postdrive_view(
    summary: TripSummary,
    score: TripSafetyScore,
    ai_report: dict[str, Any],
    generation_mode: str,
    definitions: dict[str, str] | None = None,
) -> dict[str, Any]:
    notable = summary.notable_events[0] if summary.notable_events else None
    return {
        "contract_version": "local-postdrive-v1",
        "view": "postdrive",
        "data_classification": "private_local",
        "prototype_marker": LOCAL_MARKER,
        "trip_summary": {
            "distance_km": _round(summary.basics.distance_km.value),
            "duration_minutes": _round(
                (summary.basics.duration_seconds.value or 0) / 60
                if summary.basics.duration_seconds.value is not None
                else None,
                0,
            ),
            "fuel_used_l": _round(summary.fuel.fuel_used_l.value),
            "fuel_efficiency_km_per_l": _round(summary.fuel.fuel_efficiency_km_per_l.value),
            "event_metrics": [
                {
                    "event_name": event.event_name,
                    "label": ITEM_LABELS.get(event.event_name, event.event_name),
                    "count": event.count,
                }
                for event in summary.events
                if event.count > 0
            ],
            "strongest_behavior_label": ITEM_LABELS.get(summary.strongest_behavior or "")
            if summary.strongest_behavior
            else None,
            "weakest_behavior_label": ITEM_LABELS.get(summary.weakest_behavior or "")
            if summary.weakest_behavior
            else None,
            "notable_event": {
                "event_name": notable.event_name,
                "event_label": ITEM_LABELS.get(notable.event_name, notable.event_name),
                "trip_progress_pct": round(notable.trip_progress_pct),
                "previous_speed_mean_kph": _round(notable.previous_30s_speed_mean_kph, 0),
                "next_behavior": notable.next_30s_behavior,
                "next_behavior_label": NEXT_BEHAVIOR_LABELS[notable.next_30s_behavior],
            }
            if notable
            else None,
            "quality_warnings": summary.quality_warnings,
        },
        "safety_score": score_view(score, definitions),
        "ai_report": ai_report,
        "generation_mode": generation_mode,
    }


def build_driver_safety(
    repo_root: Path,
    *,
    scores_root: Path = Path("data/processed/safety_scores"),
    fuel_root: Path = Path("data/processed/fuel"),
    output_root: Path = Path("data/processed/driver_safety"),
) -> dict[str, Any]:
    """Attribute trip scores to simulated drivers and write one history per driver."""

    from .driver_safety import build_driver_histories, load_virtual_drivers

    repo = repo_root.resolve()
    scores_path = _safe_resolve(repo, scores_root) / "trip_safety_scores.jsonl"
    output = _safe_resolve(repo, output_root)
    allowed_output = (repo / "data" / "processed").resolve()
    if output == allowed_output or allowed_output not in output.parents:
        raise ValueError("Driver safety output must be below data/processed/")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite driver safety output: {output}")
    if not scores_path.is_file():
        raise FileNotFoundError("Run `hino-analytics score` first: trip safety scores are required")

    drivers = load_virtual_drivers(_safe_resolve(repo, fuel_root))
    scores = [TripSafetyScore.model_validate(record) for record in _read_jsonl(scores_path)]
    version = scores[0].scoring_version if scores else "unknown"
    histories, unassigned = build_driver_histories(scores, drivers, scoring_version=version)

    staging = output.with_name(f".{output.name}.staging-{uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        count = _write_jsonl(
            staging / "driver_histories.jsonl",
            (history.model_dump(mode="json") for history in histories),
        )
        manifest = {
            "driver_safety_version": "driver-safety-history-v1",
            "identity_basis": "simulated driver roster (vehicle-day assignment)",
            "mock_note": drivers.mock_note,
            "deterministic_only": True,
            "external_api_calls": 0,
            "output": {
                "driver_count": count,
                "attributed_trip_count": sum(history.trip_count for history in histories),
                "unassigned_trip_count": unassigned,
                "drivers_without_trips": sum(history.trip_count == 0 for history in histories),
            },
        }
        (staging / "driver_safety_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.replace(output)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return manifest


def _latest_scored(scores: list[float | None], limit: int) -> set[int]:
    """Indices of the latest ``limit`` trips that have a score (short trips are skipped)."""

    return set([index for index, score in enumerate(scores) if score is not None][-limit:])


def local_drivers_view(
    histories: list[dict[str, Any]],
    scores_by_trip: dict[str, TripSafetyScore],
    definitions: dict[str, str] | None = None,
    *,
    detail_limit: int = 15,
    goal_checks: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Identifier-free driver history view; trips are referenced by sequence number only."""

    goal_checks = goal_checks or {}
    drivers = []
    for history in histories:
        trips = history["trips"]
        detailed = _latest_scored([trip["overall_score"] for trip in trips], detail_limit)
        drivers.append(
            {
                "driver_id": history["driver_id"],
                "driver_name": history["driver_name"],
                "primary_type": history["primary_type"],
                "new_driver": history["new_driver"],
                "trip_count": history["trip_count"],
                "scored_trip_count": history["scored_trip_count"],
                "total_distance_km": _round(history["total_distance_km"], 0),
                "average_score": _round(history["average_score"], 0),
                "grade_counts": history["grade_counts"],
                "category_averages": {
                    label: _round(value, 0) for label, value in history["category_averages"].items()
                },
                "frequent_deductions": history["frequent_deductions"],
                "recent_trips": history["recent_trips"],
                "recent_average_score": _round(history["recent_average_score"], 0),
                "previous_average_score": _round(history["previous_average_score"], 0),
                "trend": history["trend"],
                "trend_label": TREND_LABELS[history["trend"]],
                "camera_blocked_trip_count": history["camera_blocked_trip_count"],
                "series_rank": history.get("series_rank"),
                "series_ranked_size": history.get("series_ranked_size"),
                "monthly_average_scores": history["monthly_average_scores"],
                "trips": [
                    {
                        "ref": index + 1,
                        "date": trip["trip_start"][:10],
                        "plate": trip["mock_plate"],
                        "distance_km": _round(trip["distance_km"]),
                        "score": _round(trip["overall_score"], 0),
                        "grade": trip["grade"],
                        "top_deduction": trip["top_deduction"],
                        "camera_blocked": trip["dms_reliability"] == "camera_blocked",
                        "goal_status": goal_checks.get(trip["trip_id"], {}).get("status"),
                        "short_trip": trip.get("short_trip", False),
                        "detail": score_view(scores_by_trip[trip["trip_id"]], definitions)
                        if index in detailed and trip["trip_id"] in scores_by_trip
                        else None,
                    }
                    for index, trip in enumerate(trips)
                ],
            }
        )
    return {
        "contract_version": "local-drivers-v1",
        "view": "drivers",
        "data_classification": "private_local",
        "prototype_marker": LOCAL_MARKER,
        "mock_note": histories[0]["mock_note"] if histories else "",
        "drivers": drivers,
    }


SPLIT_LABELS = {
    "train": "訓練期（模型訓練時看過這趟，結果偏樂觀）",
    "validation": "驗證期",
    "test": "測試期",
}


def alert_review_view(
    reviews: dict[str, dict] | None, trip_id: str, trip_start, summary: TripSummary, limit: int = 8
) -> dict[str, Any] | None:
    """Identifier-free 60-second alert replay for one trip (None when alerts were not built)."""

    if reviews is None:
        return None
    review = reviews.get(trip_id)
    in_dataset = review is not None
    alerts = review["alerts"] if review else []
    return {
        "available": True,
        "split_label": SPLIT_LABELS.get(review["split"], review["split"]) if review else None,
        "alert_count": len(alerts),
        "event_followed_count": sum(alert["event_name"] is not None for alert in alerts),
        "in_dataset": in_dataset,
        "alerts": [
            {
                "trip_minute": round(
                    (datetime.fromisoformat(alert["timestamp"]) - trip_start).total_seconds() / 60, 1
                ),
                "risk_pct": round(alert["probability"] * 100),
                "event_label": ITEM_LABELS.get(alert["event_name"], alert["event_name"])
                if alert["event_name"]
                else None,
                "lead_seconds": alert["lead_seconds"],
                "escalation": alert["reason"] == "risk_escalation",
            }
            for alert in alerts[:limit]
        ],
    }


def road_context_view(
    context: dict | None,
    summary: TripSummary,
    scorer: SafetyScorer,
    fleet: dict[str, float],
) -> dict[str, Any] | None:
    """Same-road comparison and road-adjusted score for one trip (None without context)."""

    from .road_context import road_factors

    if context is None:
        return None
    adjusted = scorer.score_trip(summary, road_factors(context, fleet))
    # Totals per trip: counts, idle minutes, over-limit seconds (shown in minutes).
    observed, typical = context["observed"], context["typical"]
    return {
        "adjusted_score": _round(adjusted.overall_score, 0),
        "adjusted_grade": adjusted.grade,
        "city_share_pct": round(context["city_share"] * 100) if context["city_share"] is not None else None,
        "items": [
            {"label": "急加速", "you": round(observed["harsh_acc_per_km"], 1),
             "typical": round(typical["harsh_acc_per_km"], 1), "unit": "次"},
            {"label": "急減速", "you": round(observed["harsh_brake_per_km"], 1),
             "typical": round(typical["harsh_brake_per_km"], 1), "unit": "次"},
            {"label": "怠速", "you": round(observed["idle_min_per_km"], 1),
             "typical": round(typical["idle_min_per_km"], 1), "unit": "分鐘"},
            {"label": "超速時間", "you": round(observed["overspeed_share"] / 60, 1),
             "typical": round(typical["overspeed_share"] / 60, 1), "unit": "分鐘"},
        ],
    }


def safety_fuel_summary(
    scores: list[TripSafetyScore],
    fuel_trips: list[dict[str, Any]],
    groups: dict[str, list[tuple[TripSafetyScore, str]]],
) -> dict[str, Any] | None:
    """Fleet-level relation between safety score and the fuel team's road-adjusted fuel ratio.

    ``fuel_ratio`` = actual fuel / fuel expected for a *normal* driver on the same route, so
    driving style is not removed from it. Reported as found, including a null result.
    """

    from scipy.stats import spearmanr

    fuel = {
        str(row["trip_id"]): row
        for row in fuel_trips
        if row.get("scored") and row.get("fuel_ratio") is not None
    }
    pairs = [
        (score, fuel[score.trip_id])
        for score in scores
        if score.overall_score is not None and score.trip_id in fuel
    ]
    if len(pairs) < 20:
        return None
    rho, p_value = spearmanr([s.overall_score for s, _ in pairs], [f["fuel_ratio"] for _, f in pairs])
    by_grade = []
    for grade in ("A", "B", "C", "D", "E"):
        subset = [f["fuel_ratio"] for s, f in pairs if s.grade == grade]
        if subset:
            by_grade.append(
                {"grade": grade, "trips": len(subset), "mean_fuel_ratio": round(sum(subset) / len(subset), 3)}
            )
    by_series = []
    for series in sorted({int(f["vehicle_type"]) for _, f in pairs}):
        subset = [(s.overall_score, f["fuel_ratio"]) for s, f in pairs if int(f["vehicle_type"]) == series]
        if len(subset) >= 20:
            r, p = spearmanr([a for a, _ in subset], [b for _, b in subset])
            by_series.append(
                {"series": series, "trips": len(subset), "rho": round(float(r), 2), "p_value": round(float(p), 3)}
            )
    per_driver = {}
    for driver_id, trips in groups.items():
        mine = [(s, fuel[s.trip_id]) for s, _ in trips if s.overall_score is not None and s.trip_id in fuel]
        if mine:
            per_driver[driver_id] = {
                "trips": len(mine),
                "average_score": round(sum(s.overall_score for s, _ in mine) / len(mine)),
                "average_fuel_score": round(sum(f["fuel_score"] for _, f in mine) / len(mine)),
            }
    return {
        "trips": len(pairs),
        "rho": round(float(rho), 2),
        "p_value": round(float(p_value), 3),
        "significant": bool(p_value < 0.05),
        "by_grade": by_grade,
        "by_series": by_series,
        "per_driver": per_driver,
    }


FACTOR_LABELS = {
    "seconds_since_last_decelerate_onset_300s": "距離上次急減速多久",
    "decelerate_onset_count_300s": "近 5 分鐘急減速次數",
    "distract_onset_count_300s": "近 5 分鐘分心次數",
    "engine_rpm_max_300s": "近 5 分鐘最高轉速",
    "seconds_since_last_distract_onset_300s": "距離上次分心多久",
    "gps_speed_mean_60s": "近 1 分鐘平均車速",
}
FACTOR_FAMILY_LABELS = {
    "historical_events": "近期事件紀錄",
    "engine_vehicle_state": "引擎與車輛狀態",
    "speed_vehicle_dynamics": "車速變化",
}


def model_info_view(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_version": manifest["model_version"],
        "policy_version": manifest["policy_version"],
        "thresholds": manifest["risk_thresholds"],
        "top_factors": [
            {
                "label": FACTOR_LABELS.get(factor["feature"], FACTOR_FAMILY_LABELS.get(factor["feature_family"], factor["feature"])),
                "family": FACTOR_FAMILY_LABELS.get(factor["feature_family"], factor["feature_family"]),
                "importance_pct": round(float(factor["importance"]) * 100),
            }
            for factor in manifest["global_top_factors"][:3]
        ],
    }


def risk_timeline_view(review: dict | None, trip_start) -> dict[str, Any] | None:
    """The trip's 60-second risk curve on the trip-minute axis (None when not in the dataset)."""

    if review is None or "series" not in review:
        return None
    offset = (datetime.fromisoformat(review["series_start"]) - trip_start).total_seconds()
    minute = lambda seconds: round((offset + seconds) / 60, 2)
    points = [[minute(seconds), pct] for seconds, pct in review["series"]]
    peak = max(points, key=lambda point: point[1])
    return {
        "split_label": SPLIT_LABELS.get(review["split"], review["split"]),
        "points": points,
        "peak": {"minute": peak[0], "risk_pct": peak[1]},
        "alerts": [
            {
                "minute": round((datetime.fromisoformat(alert["timestamp"]) - trip_start).total_seconds() / 60, 2),
                "risk_pct": round(alert["probability"] * 100),
                "escalation": alert["reason"] == "risk_escalation",
                "event_label": ITEM_LABELS.get(alert["event_name"], alert["event_name"]) if alert["event_name"] else None,
                "lead_seconds": alert["lead_seconds"],
            }
            for alert in review["alerts"]
        ],
        "events": [
            {"minute": minute(seconds), "label": ITEM_LABELS.get(name, name)}
            for seconds, name in review.get("events", [])
        ],
    }


def build_driver_views(
    repo_root: Path,
    service: Any,
    *,
    score_config_path: Path = Path("config/safety_score_v1.json"),
    goal_config_path: Path = Path("config/behavior_goal_v0.json"),
    definitions_path: Path = Path("config/event_definitions_v1.json"),
    fuel_root: Path = Path("data/processed/fuel"),
    detail_limit: int = 15,
) -> dict[str, dict[str, Any]]:
    """Local demo files keyed by name: the driver index plus one file per simulated driver.

    Each driver file holds the briefing for the driver's *next* trip (goal and reminders
    from that driver's own past trips only) and full reports for their latest trips.
    """

    from .driver_safety import attribute_trips, goal_checks, load_virtual_drivers
    from .goals import WeaknessScorer, load_goal_config
    from .pipeline import goal_baselines

    repo = repo_root.resolve()
    processed = repo / "data" / "processed"
    histories_path = processed / "driver_safety" / "driver_histories.jsonl"
    if not histories_path.is_file():
        raise FileNotFoundError("Run `hino-analytics drivers` first: driver histories are required")
    score_config = load_safety_score_config(_safe_resolve(repo, score_config_path))
    goal_config = load_goal_config(_safe_resolve(repo, goal_config_path))
    goal_scorer = WeaknessScorer(goal_config)
    definitions = load_event_definitions(_safe_resolve(repo, definitions_path))
    categories = {name: str(item["label"]) for name, item in score_config.categories.items()}
    scores = [
        TripSafetyScore.model_validate(record)
        for record in _read_jsonl(processed / "safety_scores" / "trip_safety_scores.jsonl")
    ]
    summaries = {
        record["trip_id"]: TripSummary.model_validate(record)
        for record in _read_jsonl(processed / "analytics" / "trip_summaries.jsonl")
    }
    drivers = load_virtual_drivers(_safe_resolve(repo, fuel_root))
    groups, _ = attribute_trips(scores, drivers)
    histories = _read_jsonl(histories_path)
    road_root = processed / "road_context"
    road_contexts = (
        {record["trip_id"]: record for record in _read_jsonl(road_root / "trip_road_context.jsonl")}
        if (road_root / "trip_road_context.jsonl").is_file()
        else {}
    )
    road_fleet = (
        json.loads((road_root / "road_context_manifest.json").read_text(encoding="utf-8"))["fleet_typical_rates"]
        if road_contexts
        else {}
    )
    safety_scorer = SafetyScorer(score_config)
    alerts_path = processed / "trip_alerts" / "trip_alert_reviews.jsonl"
    alert_reviews = (
        {record["trip_id"]: record for record in _read_jsonl(alerts_path)}
        if alerts_path.is_file()
        else None
    )
    rate_floor_km = float(score_config.raw["rate_distance_floor_km"])
    checks_by_driver = {
        driver_id: goal_checks(
            trips, summaries, goal_config, profile_key=f"driver:{driver_id}", rate_floor_km=rate_floor_km
        )
        for driver_id, trips in groups.items()
    }

    fuel_path = _safe_resolve(repo, fuel_root) / "trip_scores_test.parquet"
    if fuel_path.is_file():
        import pyarrow.parquet as pq

        fuel_summary = safety_fuel_summary(scores, pq.read_table(fuel_path).to_pylist(), groups)
    else:
        fuel_summary = None
    views: dict[str, dict[str, Any]] = {
        "drivers": local_drivers_view(
            histories,
            {score.trip_id: score for score in scores},
            definitions,
            detail_limit=detail_limit,
            goal_checks={tid: check for checks in checks_by_driver.values() for tid, check in checks.items()},
        )
    }
    alert_manifest_path = processed / "trip_alerts" / "trip_alert_manifest.json"
    if alert_manifest_path.is_file():
        views["drivers"]["model_info"] = model_info_view(
            json.loads(alert_manifest_path.read_text(encoding="utf-8"))
        )
    if fuel_summary is not None:
        per_driver = fuel_summary.pop("per_driver")
        views["drivers"]["safety_fuel"] = fuel_summary
        for entry in views["drivers"]["drivers"]:
            entry["safety_fuel"] = per_driver.get(entry["driver_id"])
    for history in histories:
        driver_id = history["driver_id"]
        trips = groups.get(driver_id, [])
        if not trips:
            continue
        key = f"driver:{driver_id}"
        driver_summaries = [
            summaries[score.trip_id].model_copy(update={"vehicle_profile_id": key})
            for score, _ in trips
            if score.trip_id in summaries
        ]
        driver_scores = [score.model_copy(update={"vehicle_profile_id": key}) for score, _ in trips]
        cutoff = max(score.trip_end for score in driver_scores) + timedelta(seconds=1)
        baselines = goal_baselines(driver_summaries, key, cutoff, goal_config)
        goal = goal_scorer.daily_goal(
            vehicle_profile_id=key,
            effective_at=cutoff,
            scores=goal_scorer.score(
                baselines["recent"],
                baselines["historical"],
                baselines["trend_current"],
                baselines["trend_previous"],
            ),
        )
        briefing = build_predrive_briefing(
            driver_scores,
            vehicle_profile_id=key,
            cutoff_exclusive=cutoff,
            config=score_config,
            daily_goal=goal,
        )
        message = service.daily_goal(goal).message.model_dump(mode="json") if goal.target_metric else None

        checks = checks_by_driver.get(driver_id, {})
        reports = []
        for index in sorted(_latest_scored([score.overall_score for score, _ in trips], detail_limit)):
            score, alias = trips[index]
            summary = summaries.get(score.trip_id)
            if summary is None:
                continue
            result = service.trip_report(summary)
            reports.append(
                {
                    "ref": index + 1,
                    "date": score.trip_start.date().isoformat(),
                    "plate": drivers.plates.get(alias),
                    **local_postdrive_view(
                        summary,
                        score,
                        result.report.model_dump(mode="json"),
                        result.generation_mode,
                        definitions,
                    ),
                    "goal_check": checks.get(score.trip_id),
                    "short_trip": (score.distance_km or 0) < SHORT_TRIP_KM,
                    "alert_review": alert_review_view(
                        alert_reviews, score.trip_id, score.trip_start, summary
                    ),
                    "risk_timeline": risk_timeline_view(
                        alert_reviews.get(score.trip_id) if alert_reviews else None, score.trip_start
                    ),
                    "road_context": road_context_view(
                        road_contexts.get(score.trip_id), summary, safety_scorer, road_fleet
                    ),
                }
            )
        if road_contexts:
            from .road_context import road_factors

            adjusted = [
                safety_scorer.score_trip(
                    summaries[score.trip_id], road_factors(road_contexts[score.trip_id], road_fleet)
                ).overall_score
                for score, _ in trips
                if score.overall_score is not None
                and score.trip_id in road_contexts
                and score.trip_id in summaries
            ]
            entry = next(d for d in views["drivers"]["drivers"] if d["driver_id"] == driver_id)
            entry["road_adjusted_average"] = round(sum(adjusted) / len(adjusted)) if adjusted else None
            entry["road_adjusted_trip_count"] = len(adjusted)
        views[f"driver-{driver_id}"] = {
            "contract_version": "local-driver-v1",
            "view": "driver",
            "data_classification": "private_local",
            "prototype_marker": LOCAL_MARKER,
            "driver_id": driver_id,
            "driver_name": history["driver_name"],
            "predrive": {
                **local_predrive_view(briefing, message, categories),
                "last_goal_check": reports[-1]["goal_check"] if reports else None,
            },
            "reports": reports,
        }
    return views
