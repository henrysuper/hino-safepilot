"""Explainable L2-only context qualification after Alert Policy v2."""

from __future__ import annotations

from enum import StrEnum
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hino_safepilot.context import ContextAvailability, ContextBundle

from .contracts import AlertLevel, AlertModality, SuppressionReason, TieredAlertDecision


class VoiceGateAction(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    VOICE_ALLOWED = "voice_allowed"
    VOICE_DOWNGRADED = "voice_downgraded"


class VoiceGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context_version: str = Field(min_length=1)
    mode: Literal["policy_only", "simple_context_rule"] = "policy_only"
    missing_context_behavior: Literal["policy_only", "downgrade_to_l1"] = "policy_only"
    minimum_congestion_level: float = Field(default=0.7, ge=0, le=1)
    minimum_rainfall_1h_mm: float = Field(default=10.0, ge=0)
    minimum_risk_without_context: float = Field(default=0.75, ge=0, le=1)


class VoiceGateAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled_code: str
    journey_code: str
    timestamp: datetime
    candidate_alert_level: AlertLevel
    action: VoiceGateAction
    context_evidence: tuple[str, ...] = ()
    context_availability: dict[str, str]
    downgrade_reason: str | None = None
    policy_version: str
    context_version: str
    is_mock: bool = False


class ContextAwareVoiceGate:
    """Qualify only emitted L2 voice; L3 and non-voice state pass through."""

    def __init__(self, config: VoiceGateConfig):
        self.config = config

    @staticmethod
    def _availability(bundle: ContextBundle) -> dict[str, str]:
        return {
            "traffic": bundle.traffic.quality_status.value,
            "weather": bundle.weather.quality_status.value,
            "disaster": bundle.disaster.quality_status.value,
        }

    def decide(
        self, decision: TieredAlertDecision, context: ContextBundle
    ) -> tuple[TieredAlertDecision, VoiceGateAudit]:
        availability = self._availability(context)
        mock = bool(
            decision.is_mock
            or context.traffic.is_mock
            or context.weather.is_mock
            or context.disaster.is_mock
        )
        applicable = decision.voice_emitted and decision.emitted_level is AlertLevel.L2
        if not applicable:
            return decision.model_copy(update={"is_mock": mock}), VoiceGateAudit(
                enabled_code=decision.enabled_code,
                journey_code=decision.journey_code,
                timestamp=decision.timestamp,
                candidate_alert_level=decision.emitted_level,
                action=VoiceGateAction.NOT_APPLICABLE,
                context_availability=availability,
                policy_version=decision.policy_version,
                context_version=self.config.context_version,
                is_mock=mock,
            )

        available = any(
            status == ContextAvailability.AVAILABLE.value for status in availability.values()
        )
        evidence: list[str] = []
        traffic = context.traffic
        weather = context.weather
        disaster = context.disaster
        if traffic.quality_status is ContextAvailability.AVAILABLE:
            if (
                traffic.congestion_level is not None
                and traffic.congestion_level >= self.config.minimum_congestion_level
            ):
                evidence.append("traffic_congestion")
            if traffic.road_event_ahead or traffic.road_closed:
                evidence.append("road_hazard")
        if weather.quality_status is ContextAvailability.AVAILABLE:
            if (
                weather.rainfall_1h_mm is not None
                and weather.rainfall_1h_mm >= self.config.minimum_rainfall_1h_mm
            ):
                evidence.append("recent_rainfall")
            if weather.weather_warning or weather.heavy_rain_warning or weather.strong_wind_warning or weather.fog_warning:
                evidence.append("weather_warning")
        if disaster.quality_status is ContextAvailability.AVAILABLE and any(
            (
                disaster.heavy_rain_alert,
                disaster.flood_alert,
                disaster.landslide_alert,
                disaster.road_closure_alert,
            )
        ):
            evidence.append("disaster_warning")

        allow = self.config.mode == "policy_only"
        reason: str | None = None
        if self.config.mode == "simple_context_rule":
            if available:
                allow = bool(evidence) or decision.smoothed_risk >= self.config.minimum_risk_without_context
                if not allow:
                    reason = "no_supporting_context_evidence"
            else:
                allow = self.config.missing_context_behavior == "policy_only"
                if not allow:
                    reason = "context_unavailable_configured_downgrade"

        action = VoiceGateAction.VOICE_ALLOWED if allow else VoiceGateAction.VOICE_DOWNGRADED
        transformed = decision.model_copy(
            update={
                "emitted_level": decision.emitted_level if allow else AlertLevel.L1,
                "modality": decision.modality if allow else AlertModality.VISUAL,
                "voice_emitted": bool(allow),
                "intervention_active": True,
                "suppression_reason": decision.suppression_reason if allow else SuppressionReason.OTHER,
                "is_mock": mock,
            }
        )
        return transformed, VoiceGateAudit(
            enabled_code=decision.enabled_code,
            journey_code=decision.journey_code,
            timestamp=decision.timestamp,
            candidate_alert_level=AlertLevel.L2,
            action=action,
            context_evidence=tuple(evidence),
            context_availability=availability,
            downgrade_reason=reason,
            policy_version=decision.policy_version,
            context_version=self.config.context_version,
            is_mock=mock,
        )
