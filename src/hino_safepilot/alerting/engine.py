"""Deterministic state machine for revised Alert Policy V1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .contracts import AlertDecision, AlertPolicyConfig, AlertReason, AlertState


@dataclass
class _TripState:
    state: AlertState = AlertState.NORMAL
    last_alert_time: datetime | None = None
    reference_probability: float | None = None
    low_anchor_count: int = 0
    escalation_used: bool = False


class AlertPolicyEngine:
    """No model calls and no event truth: consumes only timestamped probabilities."""

    def __init__(self, config: AlertPolicyConfig):
        self.config = config
        self._states: dict[str, _TripState] = {}

    def reset(self) -> None:
        self._states.clear()

    def decide(self, trip_id: str, timestamp: datetime, probability: float) -> AlertDecision:
        value = float(probability)
        state = self._states.setdefault(str(trip_id), _TripState())
        before = state.state
        speak = False
        reason = AlertReason.NONE

        if state.state is AlertState.NORMAL:
            if value >= self.config.threshold_enter:
                speak = True
                reason = AlertReason.NEW_RISK_EPISODE
                state.state = AlertState.ACTIVE
                state.last_alert_time = timestamp
                state.reference_probability = value
                state.low_anchor_count = 0
                state.escalation_used = False
        else:
            if value < self.config.threshold_reset:
                state.low_anchor_count += 1
                if state.low_anchor_count >= self.config.reset_consecutive_anchors:
                    state.state = AlertState.NORMAL
                    state.last_alert_time = None
                    state.reference_probability = None
                    state.low_anchor_count = 0
                    state.escalation_used = False
                    reason = AlertReason.HYSTERESIS_RESET
            else:
                state.low_anchor_count = 0
                within_cooldown = bool(
                    state.last_alert_time is not None
                    and (timestamp - state.last_alert_time).total_seconds()
                    < self.config.cooldown_seconds
                )
                escalated = bool(
                    state.reference_probability is not None
                    and value >= state.reference_probability + self.config.escalation_delta
                )
                if within_cooldown and escalated and not state.escalation_used:
                    speak = True
                    reason = AlertReason.RISK_ESCALATION
                    state.last_alert_time = timestamp
                    state.reference_probability = value
                    state.escalation_used = True
                # Cooldown expiry alone intentionally never emits another alert.

        return AlertDecision(
            timestamp=timestamp,
            probability=value,
            should_speak=speak,
            reason=reason,
            state_before=before,
            state_after=state.state,
            policy_version=self.config.policy_version,
        )

