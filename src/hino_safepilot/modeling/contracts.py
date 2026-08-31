"""Typed contracts for the Phase 3C-1 offline risk baseline."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskLevel(StrEnum):
    """Prototype-only UI level; not a validated safety standard."""

    LOW = "low"
    ELEVATED = "elevated"
    HIGH = "high"


class PredictionDataQuality(StrEnum):
    VALID = "valid"
    DEGRADED = "degraded"
    INSUFFICIENT = "insufficient"


class RiskFactor(StrictModel):
    """Association-only explanation term; never a causal statement."""

    feature: str = Field(min_length=1)
    feature_family: str = Field(min_length=1)
    direction: Literal["increases_risk", "decreases_risk", "mixed_or_unknown"]
    importance: float = Field(ge=0.0)


class RiskPredictionResult(StrictModel):
    risk_probability: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel
    prediction_horizon_sec: Literal[60] = 60
    threshold_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    data_quality_status: PredictionDataQuality
    model_family: Literal["logistic_regression", "xgboost"] = "logistic_regression"
    calibration_version: str = "raw-v0"
    top_factors: list[RiskFactor] = Field(default_factory=list)
    predicted_event_type: str | None = None
    risk_level_policy_status: Literal["EXPERIMENTAL_UI_PROTOTYPE"] = (
        "EXPERIMENTAL_UI_PROTOTYPE"
    )
