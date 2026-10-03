"""Phase 2.5 analytics artifacts to trip safety scores, briefings, and local demo views."""

from __future__ import annotations

import json
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any
from uuid import uuid4

from .models import DailyImprovementGoal, PreDriveBriefing, TripSafetyScore, TripSummary
from .pipeline import _safe_resolve, _write_jsonl
from .scoring import (
    ITEM_LABELS,
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


def pick_demo_trip(scores_root: Path) -> str:
    """Pick the scored trip nearest the median score that also has a usable briefing."""

    scores = {
        record["trip_id"]: TripSafetyScore.model_validate(record)
        for record in _read_jsonl(scores_root / "trip_safety_scores.jsonl")
    }
    candidates: list[TripSafetyScore] = []
    baseline_available: dict[str, bool] = {}
    for record in _read_jsonl(scores_root / "predrive_briefings.jsonl"):
        briefing = PreDriveBriefing.model_validate(record["briefing"])
        score = scores.get(record["subject_trip_id"])
        if (
            score
            and score.overall_score is not None
            and briefing.recent_scored_trip_count >= 1
            and briefing.daily_goal is not None
            and briefing.daily_goal.target_metric
        ):
            candidates.append(score)
            baseline_available[score.trip_id] = (
                briefing.daily_goal.evidence_summary.get("baseline_value") is not None
            )
    if not candidates:
        raise LookupError("No trip has both a score and a usable pre-drive briefing")
    with_baseline = {trip_id for trip_id, has_baseline in baseline_available.items() if has_baseline}
    if with_baseline:
        candidates = [score for score in candidates if score.trip_id in with_baseline]
    middle = median(score.overall_score or 0 for score in candidates)
    return min(
        candidates, key=lambda score: (abs((score.overall_score or 0) - middle), score.trip_start)
    ).trip_id


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def score_view(score: TripSafetyScore) -> dict[str, Any]:
    labels = {category.category: category.label for category in score.categories}
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
            }
            for deduction in score.top_deductions
        ],
        "applied_caps": score.applied_caps,
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
        "safety_score": score_view(score),
        "ai_report": ai_report,
        "generation_mode": generation_mode,
    }
