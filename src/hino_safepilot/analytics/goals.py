"""Config-driven weakness scoring and deterministic daily goal selection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    BehaviorBaseline,
    BehaviorMetric,
    DailyImprovementGoal,
    QualityStatus,
    WeaknessScore,
)


@dataclass(frozen=True)
class GoalConfig:
    raw: dict[str, Any]

    @property
    def version(self) -> str:
        return str(self.raw["version"])

    @property
    def weights(self) -> dict[str, float]:
        return {key: float(value) for key, value in self.raw["score_weights"].items()}

    @property
    def minimum_trip_count(self) -> int:
        return int(self.raw["minimum_trip_count"])

    @property
    def metrics(self) -> dict[str, dict[str, Any]]:
        return self.raw["metrics"]


def load_goal_config(path: Path) -> GoalConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    required_weights = {"baseline_excess", "absolute_burden", "recent_trend", "support"}
    if set(raw.get("score_weights", {})) != required_weights:
        raise ValueError("Goal config score_weights has an invalid contract")
    if abs(sum(float(value) for value in raw["score_weights"].values()) - 1) > 1e-9:
        raise ValueError("Goal config score weights must sum to 1")
    return GoalConfig(raw)


def _by_name(baseline: BehaviorBaseline) -> dict[str, BehaviorMetric]:
    return {metric.name: metric for metric in baseline.metrics}


def _relative_worsening(current: float, reference: float, direction: str) -> float:
    if reference == 0:
        return 1.0 if (current > 0 and direction == "higher_is_worse") else 0.0
    change = (
        (current - reference) / abs(reference)
        if direction == "higher_is_worse"
        else (reference - current) / abs(reference)
    )
    return max(0.0, change)


class WeaknessScorer:
    def __init__(self, config: GoalConfig) -> None:
        self.config = config

    def score(
        self,
        recent: BehaviorBaseline,
        historical: BehaviorBaseline,
        trend_current: BehaviorBaseline | None = None,
        trend_previous: BehaviorBaseline | None = None,
    ) -> list[WeaknessScore]:
        recent_by_name, baseline_by_name = _by_name(recent), _by_name(historical)
        current_by_name = _by_name(trend_current) if trend_current else {}
        previous_by_name = _by_name(trend_previous) if trend_previous else {}
        results: list[WeaknessScore] = []
        for name, metric_config in self.config.metrics.items():
            current = recent_by_name.get(name)
            reference = baseline_by_name.get(name)
            if not current or current.value is None:
                continue
            direction = str(metric_config["direction"])
            baseline_excess = (
                _relative_worsening(current.value, reference.value, direction)
                if reference and reference.value is not None
                else 0.0
            )
            reference_rate = float(metric_config["reference_rate"])
            if direction == "higher_is_worse":
                burden = max(0.0, current.value / reference_rate)
            else:
                burden = max(0.0, (reference_rate - current.value) / reference_rate)
            trend = 0.0
            trend_current_metric = current_by_name.get(name)
            trend_previous_metric = previous_by_name.get(name)
            if (
                trend_current_metric
                and trend_previous_metric
                and trend_current_metric.value is not None
                and trend_previous_metric.value is not None
            ):
                trend = _relative_worsening(
                    trend_current_metric.value, trend_previous_metric.value, direction
                )
            evidence_exists = baseline_excess > 0 or burden > 0 or trend > 0
            support = min(
                1.0, current.contributing_trip_count / self.config.minimum_trip_count
            ) if evidence_exists else 0.0
            quality_factor = {
                QualityStatus.VALID: 1.0,
                QualityStatus.DEGRADED: 0.5,
                QualityStatus.UNAVAILABLE: 0.0,
                QualityStatus.INVALID: 0.0,
            }[current.quality_status]
            weighted = (
                self.config.weights["baseline_excess"] * min(baseline_excess / 2, 1)
                + self.config.weights["absolute_burden"] * min(burden / 2, 1)
                + self.config.weights["recent_trend"] * min(trend / 2, 1)
                + self.config.weights["support"] * support
            )
            score = min(1.0, weighted * float(metric_config["severity"]) * quality_factor)
            reasons: list[str] = []
            if baseline_excess > 0:
                reasons.append("RATE_ABOVE_30D_BASELINE" if direction == "higher_is_worse" else "EFFICIENCY_BELOW_30D_BASELINE")
            if burden >= 1:
                reasons.append("ABSOLUTE_BURDEN_ABOVE_PROTOTYPE_REFERENCE")
            if trend > 0:
                reasons.append("RECENT_UPWARD_TREND" if direction == "higher_is_worse" else "RECENT_DOWNWARD_TREND")
            if current.quality_status == QualityStatus.DEGRADED:
                reasons.append("DATA_QUALITY_PENALTY_APPLIED")
            change_pct = (
                ((current.value - reference.value) / abs(reference.value) * 100)
                if reference and reference.value not in {None, 0}
                else None
            )
            results.append(
                WeaknessScore(
                    metric=name,
                    priority_score=score,
                    reason_codes=reasons,
                    evidence={
                        "recent_value": current.value,
                        "baseline_value": reference.value if reference else None,
                        "unit": current.unit,
                        "change_pct": change_pct,
                        "contributing_trip_count": current.contributing_trip_count,
                    },
                    quality_status=current.quality_status,
                    scoring_version=self.config.version,
                )
            )
        return sorted(results, key=lambda item: (-item.priority_score, item.metric))

    def daily_goal(
        self,
        *,
        vehicle_profile_id: str,
        effective_at: datetime,
        scores: list[WeaknessScore],
    ) -> DailyImprovementGoal:
        selected = next((score for score in scores if score.priority_score > 0), None)
        if selected is None:
            return DailyImprovementGoal(
                vehicle_profile_id=vehicle_profile_id,
                effective_at=effective_at,
                target_metric=None,
                priority_score=0,
                reason_codes=["INSUFFICIENT_WEAKNESS_EVIDENCE"],
                evidence_summary={},
                quality_status=QualityStatus.UNAVAILABLE,
                scoring_version=self.config.version,
            )
        return DailyImprovementGoal(
            vehicle_profile_id=vehicle_profile_id,
            effective_at=effective_at,
            target_metric=selected.metric,
            priority_score=selected.priority_score,
            reason_codes=selected.reason_codes,
            evidence_summary=selected.evidence,
            quality_status=selected.quality_status,
            scoring_version=self.config.version,
        )
