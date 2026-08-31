from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AlertState(StrEnum):
    NORMAL = "normal"
    ACTIVE = "active"
    RESET = "reset"


class AlertReason(StrEnum):
    NONE = "none"
    NEW_RISK_EPISODE = "new_risk_episode"
    RISK_ESCALATION = "risk_escalation"
    HYSTERESIS_RESET = "hysteresis_reset"


class AlertPolicyConfig(StrictModel):
    policy_version: str = Field(min_length=1)
    threshold_enter: float = Field(ge=0.0, le=1.0)
    threshold_reset: float = Field(ge=0.0, le=1.0)
    cooldown_seconds: int = Field(ge=0)
    escalation_delta: float = Field(gt=0.0, le=1.0)
    reset_consecutive_anchors: int = Field(ge=1)
    one_escalation_per_active_episode: Literal[True] = True

    @model_validator(mode="after")
    def reset_must_be_lower(self):
        if self.threshold_reset >= self.threshold_enter:
            raise ValueError("threshold_reset must be below threshold_enter")
        return self


class AlertDecision(StrictModel):
    timestamp: datetime
    probability: float = Field(ge=0.0, le=1.0)
    should_speak: bool
    reason: AlertReason
    state_before: AlertState
    state_after: AlertState
    policy_version: str

