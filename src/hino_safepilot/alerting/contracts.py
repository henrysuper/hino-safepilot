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


class AlertLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"

    @property
    def rank(self) -> int:
        return int(self.value[1])


class AlertModality(StrEnum):
    SILENT = "silent"
    VISUAL = "visual"
    VISUAL_VOICE = "visual_voice"
    VISUAL_VOICE_HAPTIC = "visual_voice_haptic"


class SuppressionReason(StrEnum):
    NONE = "none"
    PERSISTENCE = "persistence"
    COOLDOWN = "cooldown"
    VOICE_BUDGET = "voice_budget"
    IDLING = "idling"
    DUPLICATE_EPISODE = "duplicate_episode"
    SUPPRESSED_BY_DMS = "suppressed_by_dms"
    OTHER = "other"


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


class CompositeTripKey(StrictModel):
    """Organizer-confirmed journey identity; enabled_code is not a driver ID."""

    enabled_code: str = Field(min_length=1)
    journey_code: str = Field(min_length=1)

    def state_key(self) -> tuple[str, str]:
        return self.enabled_code, self.journey_code


class DMSArbitrationInput(StrictModel):
    """Optional live DMS capability input; offline HINO data does not supply it."""

    active: bool = False
    issuing_voice: bool = False
    overlaps_predictive_reason: bool = False
    semantic_reason: str | None = None
    is_mock: bool = False


class AlertPolicyConfigV2(StrictModel):
    policy_version: str = Field(min_length=1)
    l1_enter_threshold: float = Field(ge=0.0, le=1.0)
    l1_exit_threshold: float = Field(ge=0.0, le=1.0)
    l2_enter_threshold: float = Field(ge=0.0, le=1.0)
    l2_exit_threshold: float = Field(ge=0.0, le=1.0)
    l3_enter_threshold: float = Field(ge=0.0, le=1.0)
    l3_exit_threshold: float = Field(ge=0.0, le=1.0)
    ema_half_life_seconds: float = Field(gt=0.0)
    l2_persistence_seconds: float = Field(ge=0.0)
    l2_cooldown_seconds: float = Field(ge=0.0)
    risk_slope_l3_threshold_per_second: float | None = Field(default=None, gt=0.0)
    rolling_voice_window_seconds: Literal[3600] = 3600
    rolling_l2_voice_budget: int = Field(ge=0)
    idling_gate_enabled: bool = True
    dms_capability_enabled: bool = False
    l3_bypasses_l2_voice_budget: Literal[True] = True
    same_episode_l2_voice_max: Literal[1] = 1
    same_episode_l3_voice_max: Literal[1] = 1

    @model_validator(mode="after")
    def thresholds_are_ordered(self):
        if not (
            self.l1_enter_threshold < self.l2_enter_threshold < self.l3_enter_threshold
        ):
            raise ValueError("enter thresholds must satisfy L1 < L2 < L3")
        for name, exit_value, enter_value in (
            ("L1", self.l1_exit_threshold, self.l1_enter_threshold),
            ("L2", self.l2_exit_threshold, self.l2_enter_threshold),
            ("L3", self.l3_exit_threshold, self.l3_enter_threshold),
        ):
            if exit_value >= enter_value:
                raise ValueError(f"{name} exit threshold must be below enter threshold")
        if not (
            self.l1_exit_threshold < self.l2_exit_threshold < self.l3_exit_threshold
        ):
            raise ValueError("exit thresholds must satisfy L1 < L2 < L3")
        return self


class TieredAlertDecision(StrictModel):
    enabled_code: str
    journey_code: str
    timestamp: datetime
    raw_risk: float = Field(ge=0.0, le=1.0)
    smoothed_risk: float = Field(ge=0.0, le=1.0)
    risk_slope_per_second: float
    persistence_duration_seconds: float = Field(default=0.0, ge=0.0)
    cooldown_remaining_seconds: float = Field(default=0.0, ge=0.0)
    previous_level: AlertLevel
    requested_level: AlertLevel
    emitted_level: AlertLevel
    modality: AlertModality
    episode_id: str | None
    is_idling: bool
    dms_active: bool
    direct_critical: bool
    voice_emitted: bool
    intervention_active: bool
    level_transition: bool
    voice_budget_remaining: int
    suppression_reason: SuppressionReason
    policy_version: str
    is_mock: bool = False
