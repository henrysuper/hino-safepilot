"""Context risk adjustment layer (logit-additive, data-driven where possible).

This layer adjusts the base model probability using external context (road
condition, road class, weather) WITHOUT retraining the base model. It works on
the logit scale, which is mathematically consistent with logistic regression,
never exceeds [0, 1], composes multiple factors additively, and degrades to the
exact base probability when no context is present.

    adjusted = sigmoid( logit(base_probability) + sum(context_logit_deltas) )

Coefficients:
- Road-condition and road-class deltas are ESTIMATED FROM THE HINO DATA ITSELF
  (see estimate_context_coefficients): the log-ratio of the observed core-event
  rate in a condition bucket versus the overall rate. These are auditable and
  data-grounded, not guessed.
- Weather deltas are ASSUMPTION VALUES (no 2025 historical weather is available
  to validate them). They are clearly flagged as unvalidated and default to a
  conservative, small effect that can be disabled.

Every applied delta is reported with its source ("data_estimated" or
"assumption_unvalidated") so the UI/API can be fully transparent.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("config/context_risk_v0.json")

# Bounds so a single factor cannot dominate; keeps the adjustment sane.
MAX_ABS_DELTA_PER_FACTOR = 1.5
MAX_ABS_TOTAL_DELTA = 2.5


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1 / (1 + z)
    z = math.exp(x)
    return z / (1 + z)


@dataclass
class AppliedFactor:
    name: str
    logit_delta: float
    source: str  # "data_estimated" | "assumption_unvalidated"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor": self.name,
            "logit_delta": round(self.logit_delta, 4),
            "source": self.source,
            "detail": self.detail,
        }


@dataclass
class ContextAdjustment:
    base_probability: float
    adjusted_probability: float
    total_logit_delta: float
    factors: list[AppliedFactor] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_probability": round(self.base_probability, 6),
            "adjusted_probability": round(self.adjusted_probability, 6),
            "total_logit_delta": round(self.total_logit_delta, 4),
            "factors": [f.to_dict() for f in self.factors],
        }


def _congestion_bucket(road_speed_kph: float | None, speed_limit_kph: float | None) -> str | None:
    """Classify congestion from live road speed relative to the limit.

    Falls back to absolute-speed buckets when no limit is available.
    """
    if road_speed_kph is None:
        return None
    if speed_limit_kph and speed_limit_kph > 0:
        ratio = road_speed_kph / speed_limit_kph
        if ratio < 0.3:
            return "heavy_congestion"
        if ratio < 0.6:
            return "moderate_congestion"
        return "free_flow"
    # No limit: use absolute speed thresholds (urban assumption).
    if road_speed_kph < 15:
        return "heavy_congestion"
    if road_speed_kph < 35:
        return "moderate_congestion"
    return "free_flow"


class ContextRiskAdjuster:
    """Applies logit-additive context deltas from a coefficient config."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.enabled = bool(config.get("enabled", True))
        self.congestion = config.get("congestion_logit_delta", {})
        self.road_class = config.get("road_class_logit_delta", {})
        self.weather = config.get("weather", {})

    @classmethod
    def from_file(cls, path: Path) -> "ContextRiskAdjuster":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def disabled(cls) -> "ContextRiskAdjuster":
        return cls({"enabled": False})

    def adjust(
        self,
        base_probability: float,
        *,
        road_speed_kph: float | None = None,
        speed_limit_kph: float | None = None,
        road_class: str | None = None,
        rainfall_1h_mm: float | None = None,
        visibility_m: float | None = None,
    ) -> ContextAdjustment:
        factors: list[AppliedFactor] = []
        if not self.enabled:
            return ContextAdjustment(base_probability, base_probability, 0.0, factors)

        # 1) Congestion (data-estimated)
        bucket = _congestion_bucket(road_speed_kph, speed_limit_kph)
        if bucket is not None and bucket in self.congestion:
            delta = float(self.congestion[bucket])
            factors.append(
                AppliedFactor(
                    f"congestion:{bucket}",
                    _clamp(delta, MAX_ABS_DELTA_PER_FACTOR),
                    "data_estimated",
                    f"road_speed={road_speed_kph} limit={speed_limit_kph}",
                )
            )

        # 2) Road class (data-estimated)
        if road_class is not None and str(road_class) in self.road_class:
            delta = float(self.road_class[str(road_class)])
            factors.append(
                AppliedFactor(
                    f"road_class:{road_class}",
                    _clamp(delta, MAX_ABS_DELTA_PER_FACTOR),
                    "data_estimated",
                    f"road_class_code={road_class}",
                )
            )

        # 3) Weather (assumption, unvalidated)
        if self.weather.get("enabled", True):
            rain_delta = self._weather_rain_delta(rainfall_1h_mm)
            if rain_delta is not None:
                factors.append(
                    AppliedFactor(
                        "weather:rain",
                        _clamp(rain_delta, MAX_ABS_DELTA_PER_FACTOR),
                        "assumption_unvalidated",
                        f"rainfall_1h_mm={rainfall_1h_mm}",
                    )
                )
            vis_delta = self._weather_visibility_delta(visibility_m)
            if vis_delta is not None:
                factors.append(
                    AppliedFactor(
                        "weather:low_visibility",
                        _clamp(vis_delta, MAX_ABS_DELTA_PER_FACTOR),
                        "assumption_unvalidated",
                        f"visibility_m={visibility_m}",
                    )
                )

        total = _clamp(sum(f.logit_delta for f in factors), MAX_ABS_TOTAL_DELTA)
        adjusted = _sigmoid(_logit(base_probability) + total)
        return ContextAdjustment(base_probability, adjusted, total, factors)

    def _weather_rain_delta(self, rainfall_1h_mm: float | None) -> float | None:
        if rainfall_1h_mm is None:
            return None
        thresholds = self.weather.get("rain_thresholds_mm", {"light": 2.0, "moderate": 8.0, "heavy": 15.0})
        deltas = self.weather.get("rain_logit_delta", {"light": 0.1, "moderate": 0.3, "heavy": 0.6})
        if rainfall_1h_mm >= thresholds["heavy"]:
            return float(deltas["heavy"])
        if rainfall_1h_mm >= thresholds["moderate"]:
            return float(deltas["moderate"])
        if rainfall_1h_mm >= thresholds["light"]:
            return float(deltas["light"])
        return 0.0

    def _weather_visibility_delta(self, visibility_m: float | None) -> float | None:
        if visibility_m is None:
            return None
        threshold = self.weather.get("low_visibility_threshold_m", 500.0)
        delta = self.weather.get("low_visibility_logit_delta", 0.4)
        return float(delta) if visibility_m < threshold else 0.0


def _clamp(value: float, bound: float) -> float:
    return max(-bound, min(bound, value))
