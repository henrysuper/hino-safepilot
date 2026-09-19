"""Realtime 60-second risk engine: telemetry + live context -> risk score.

Flow for a single prediction:
  1. caller feeds recent telemetry points into a rolling buffer;
  2. at an anchor (now) with a GPS location, the engine queries context:
       - TDX road condition via real OAuth2 credentials (if configured),
       - weather via a synthetic provider (until a CWA key exists);
  3. windowed features are assembled from the buffer (+ optional context
     overrides) into the frozen model's 163-feature vector;
  4. the trained RiskModelBundle produces a calibrated 60s risk probability
     and prototype risk level, with a data-quality status reflecting how much
     history and context were actually available.

The base HINO model never depends on context: if a provider fails or is
unconfigured, its snapshot is marked missing and the prediction still returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib

from ..context.models import ContextAvailability, ContextQuery
from ..context.providers import ContextAggregator, NullContextProvider
from ..modeling.contracts import PredictionDataQuality, RiskPredictionResult
from .context_risk import ContextRiskAdjuster
from .features import RollingTelemetryBuffer, TelemetryPoint, build_feature_row


DEFAULT_BUNDLE = Path("models/risk/v0_logistic_run/risk_model_bundle.joblib")
DEFAULT_FEATURE_SPEC = Path("data/processed/ml/v0/metadata/resolved_feature_spec.json")
DEFAULT_CONTEXT_RISK = Path("config/context_risk_v0.json")

# How external context maps onto model-visible features. The v0_hino model has
# NO traffic/weather columns, so context does not feed the model directly; it is
# surfaced in the prediction envelope for the UI and future v1 retraining. This
# mapping is intentionally empty for v0 and documented rather than faked.
CONTEXT_FEATURE_MAP: dict[str, str] = {}


@dataclass
class RealtimeInputs:
    """One prediction request."""

    anchor_timestamp: datetime
    latitude_deg: float | None = None
    longitude_deg: float | None = None
    heading_deg: float | None = None
    vehicle_series_code: float | None = None


@dataclass
class RealtimePrediction:
    """Prediction plus the context envelope and honesty metadata."""

    risk: RiskPredictionResult
    traffic: dict[str, Any]
    weather: dict[str, Any]
    history_coverage_seconds: float
    history_point_count: int
    context_used_by_model: bool
    context_adjustment: dict[str, Any] | None = None
    adjusted_probability: float | None = None
    adjusted_risk_level: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_probability": self.risk.risk_probability,
            "risk_level": self.risk.risk_level.value,
            "adjusted_risk_probability": self.adjusted_probability,
            "adjusted_risk_level": self.adjusted_risk_level,
            "prediction_horizon_sec": self.risk.prediction_horizon_sec,
            "data_quality_status": self.risk.data_quality_status.value,
            "model_version": self.risk.model_version,
            "threshold_version": self.risk.threshold_version,
            "history_coverage_seconds": self.history_coverage_seconds,
            "history_point_count": self.history_point_count,
            "context_used_by_model": self.context_used_by_model,
            "context_adjustment": self.context_adjustment,
            "traffic_context": self.traffic,
            "weather_context": self.weather,
            "notes": self.notes,
        }


def _load_feature_names(spec_path: Path) -> list[str]:
    import json

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    return list(spec["feature_columns"])


class RealtimeRiskEngine:
    def __init__(
        self,
        *,
        repo_root: Path,
        bundle_path: Path | None = None,
        feature_spec_path: Path | None = None,
        traffic_provider: Any | None = None,
        weather_provider: Any | None = None,
        history_horizon_seconds: float = 300.0,
        min_coverage_valid_seconds: float = 240.0,
        min_coverage_degraded_seconds: float = 60.0,
        context_risk_path: Path | None = None,
    ):
        repo_root = repo_root.resolve()
        bundle_path = (bundle_path or repo_root / DEFAULT_BUNDLE).resolve()
        feature_spec_path = (feature_spec_path or repo_root / DEFAULT_FEATURE_SPEC).resolve()
        if not bundle_path.exists():
            raise FileNotFoundError(f"Trained model bundle not found: {bundle_path}")
        if not feature_spec_path.exists():
            raise FileNotFoundError(f"Feature spec not found: {feature_spec_path}")

        self.bundle = joblib.load(bundle_path)
        self.feature_names = _load_feature_names(feature_spec_path)
        self.buffer = RollingTelemetryBuffer(history_horizon_seconds)
        self.min_coverage_valid_seconds = min_coverage_valid_seconds
        self.min_coverage_degraded_seconds = min_coverage_degraded_seconds

        # Context risk adjuster (logit-additive; degrades to base when disabled).
        context_risk_path = (context_risk_path or repo_root / DEFAULT_CONTEXT_RISK).resolve()
        if context_risk_path.exists():
            self.context_adjuster = ContextRiskAdjuster.from_file(context_risk_path)
        else:
            self.context_adjuster = ContextRiskAdjuster.disabled()

        # Providers are optional. Missing providers -> Null (marked unavailable).
        self.traffic_provider = traffic_provider or NullContextProvider(ContextAvailability.MISSING)
        self.weather_provider = weather_provider or NullContextProvider(ContextAvailability.MISSING)
        self.aggregator = ContextAggregator(
            traffic_provider=self.traffic_provider,
            weather_provider=self.weather_provider,
            disaster_provider=NullContextProvider(ContextAvailability.UNSUPPORTED),
        )

    def observe(self, point: TelemetryPoint) -> None:
        """Feed one telemetry sample into the rolling buffer."""
        self.buffer.add(point)

    def _quality(self, coverage: float) -> PredictionDataQuality:
        if coverage >= self.min_coverage_valid_seconds:
            return PredictionDataQuality.VALID
        if coverage >= self.min_coverage_degraded_seconds:
            return PredictionDataQuality.DEGRADED
        return PredictionDataQuality.INSUFFICIENT

    def predict(self, inputs: RealtimeInputs) -> RealtimePrediction:
        notes: list[str] = []
        anchor = inputs.anchor_timestamp

        query = ContextQuery(
            context_timestamp=anchor,
            latitude_deg=inputs.latitude_deg,
            longitude_deg=inputs.longitude_deg,
            heading_deg=inputs.heading_deg,
        )
        bundle_ctx = self.aggregator.collect(query)
        traffic = bundle_ctx.traffic.model_dump(mode="json")
        weather = bundle_ctx.weather.model_dump(mode="json")

        if bundle_ctx.traffic.quality_status != ContextAvailability.AVAILABLE:
            notes.append(f"traffic_context={bundle_ctx.traffic.quality_status.value}")
        if bundle_ctx.weather.quality_status != ContextAvailability.AVAILABLE:
            notes.append(f"weather_context={bundle_ctx.weather.quality_status.value}")
        if bundle_ctx.weather.is_mock:
            notes.append("weather_is_synthetic_mock")

        # v0_hino model has no context columns, so context is not fed to it.
        context_overrides: dict[str, float] = {}
        context_used_by_model = bool(CONTEXT_FEATURE_MAP)
        if not context_used_by_model:
            notes.append("v0_model_has_no_context_features; context is advisory only")

        coverage = self.buffer.coverage_seconds(anchor)
        row, _named = build_feature_row(
            self.buffer,
            anchor,
            self.feature_names,
            vehicle_series_code=inputs.vehicle_series_code,
            context_overrides=context_overrides,
        )
        quality = self._quality(coverage)
        risk = self.bundle.predict_result(row, data_quality_status=quality)

        # Apply the logit-additive context risk adjustment (transparent layer).
        traffic_snapshot = bundle_ctx.traffic
        weather_snapshot = bundle_ctx.weather
        road_speed = traffic_snapshot.road_speed_kph if traffic_snapshot.quality_status in (
            ContextAvailability.AVAILABLE,
            ContextAvailability.STALE,
        ) else None
        adjustment = self.context_adjuster.adjust(
            risk.risk_probability,
            road_speed_kph=road_speed,
            speed_limit_kph=None,
            road_class=traffic_snapshot.road_class,
            rainfall_1h_mm=weather_snapshot.rainfall_1h_mm,
            visibility_m=weather_snapshot.visibility_m,
        )
        adjusted_p = adjustment.adjusted_probability
        adjusted_level = self._level_for(adjusted_p)
        if adjustment.factors:
            notes.append("context_risk_adjustment_applied (logit-additive)")

        return RealtimePrediction(
            risk=risk,
            traffic=traffic,
            weather=weather,
            history_coverage_seconds=coverage,
            history_point_count=self.buffer.point_count,
            context_used_by_model=context_used_by_model,
            context_adjustment=adjustment.to_dict(),
            adjusted_probability=round(adjusted_p, 6),
            adjusted_risk_level=adjusted_level,
            notes=notes,
        )

    def _level_for(self, probability: float) -> str:
        """Same thresholding the bundle uses, applied to the adjusted probability."""
        lower = min(self.bundle.high_recall_threshold, self.bundle.selected_threshold)
        if probability < lower:
            return "low"
        if probability < self.bundle.selected_threshold:
            return "elevated"
        return "high"
