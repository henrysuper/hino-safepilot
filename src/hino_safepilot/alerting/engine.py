"""Deterministic state machines for Alert Policy V1 and tiered V2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections import deque
import math

from .contracts import (
    AlertDecision,
    AlertLevel,
    AlertModality,
    AlertPolicyConfig,
    AlertPolicyConfigV2,
    AlertReason,
    AlertState,
    CompositeTripKey,
    DMSArbitrationInput,
    SuppressionReason,
    TieredAlertDecision,
)


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


@dataclass
class _TieredTripState:
    level: AlertLevel = AlertLevel.L0
    last_timestamp: datetime | None = None
    smoothed_risk: float | None = None
    l2_candidate_since: datetime | None = None
    episode_sequence: int = 0
    episode_id: str | None = None
    l2_voice_emitted: bool = False
    l3_voice_emitted: bool = False
    last_l2_voice_time: datetime | None = None
    l2_voice_times: deque[datetime] | None = None

    def __post_init__(self) -> None:
        if self.l2_voice_times is None:
            self.l2_voice_times = deque()


class TieredAlertPolicyEngine:
    """Stateful Alert Policy V2, isolated by ``(enabledCode, journeyCode)``.

    It consumes risk and capability signals only. Event truth is deliberately absent
    and is used only by the offline evaluator.
    """

    def __init__(self, config: AlertPolicyConfigV2):
        self.config = config
        self._states: dict[tuple[str, str], _TieredTripState] = {}

    def reset(self) -> None:
        self._states.clear()

    def _smooth(
        self, state: _TieredTripState, timestamp: datetime, raw_risk: float
    ) -> tuple[float, float]:
        if state.last_timestamp is None or state.smoothed_risk is None:
            return raw_risk, 0.0
        elapsed = (timestamp - state.last_timestamp).total_seconds()
        if elapsed < 0:
            raise ValueError("timestamps must be non-decreasing within a composite trip")
        if elapsed == 0:
            return state.smoothed_risk, 0.0
        alpha = 1.0 - math.exp(-math.log(2.0) * elapsed / self.config.ema_half_life_seconds)
        smoothed = state.smoothed_risk + alpha * (raw_risk - state.smoothed_risk)
        return smoothed, (smoothed - state.smoothed_risk) / elapsed

    def _requested_level(
        self,
        state: _TieredTripState,
        timestamp: datetime,
        smoothed: float,
        slope: float,
        direct_critical: bool,
    ) -> tuple[AlertLevel, SuppressionReason]:
        slope_critical = bool(
            self.config.risk_slope_l3_threshold_per_second is not None
            and slope >= self.config.risk_slope_l3_threshold_per_second
            and smoothed >= self.config.l2_enter_threshold
        )
        if direct_critical or smoothed >= self.config.l3_enter_threshold or slope_critical:
            return AlertLevel.L3, SuppressionReason.NONE
        if state.level is AlertLevel.L3 and smoothed >= self.config.l3_exit_threshold:
            return AlertLevel.L3, SuppressionReason.NONE

        if smoothed >= self.config.l2_enter_threshold:
            if state.l2_candidate_since is None:
                state.l2_candidate_since = timestamp
        elif smoothed < self.config.l2_exit_threshold:
            state.l2_candidate_since = None

        persistent = bool(
            state.l2_candidate_since is not None
            and (timestamp - state.l2_candidate_since).total_seconds()
            >= self.config.l2_persistence_seconds
        )
        stays_l2 = state.level in {AlertLevel.L2, AlertLevel.L3} and smoothed >= self.config.l2_exit_threshold
        if persistent and (smoothed >= self.config.l2_enter_threshold or stays_l2):
            return AlertLevel.L2, SuppressionReason.NONE

        l1_active = smoothed >= self.config.l1_enter_threshold or (
            state.level is not AlertLevel.L0 and smoothed >= self.config.l1_exit_threshold
        )
        if l1_active:
            reason = (
                SuppressionReason.PERSISTENCE
                if smoothed >= self.config.l2_enter_threshold and not persistent
                else SuppressionReason.NONE
            )
            return AlertLevel.L1, reason
        return AlertLevel.L0, SuppressionReason.NONE

    def _prune_budget(self, state: _TieredTripState, timestamp: datetime) -> None:
        assert state.l2_voice_times is not None
        while state.l2_voice_times and (
            timestamp - state.l2_voice_times[0]
        ).total_seconds() >= self.config.rolling_voice_window_seconds:
            state.l2_voice_times.popleft()

    def decide(
        self,
        enabled_code: str,
        journey_code: str,
        timestamp: datetime,
        raw_risk: float,
        *,
        is_idling: bool = False,
        dms: DMSArbitrationInput | None = None,
        direct_critical: bool = False,
        is_mock: bool = False,
    ) -> TieredAlertDecision:
        value = float(raw_risk)
        if not 0.0 <= value <= 1.0:
            raise ValueError("raw_risk must be in [0, 1]")
        trip = CompositeTripKey(enabled_code=enabled_code, journey_code=journey_code)
        state = self._states.setdefault(trip.state_key(), _TieredTripState())
        before = state.level
        smoothed, slope = self._smooth(state, timestamp, value)
        requested, base_reason = self._requested_level(
            state, timestamp, smoothed, slope, direct_critical
        )
        persistence_duration = (
            max(0.0, (timestamp - state.l2_candidate_since).total_seconds())
            if state.l2_candidate_since is not None
            else 0.0
        )
        cooldown_remaining = (
            max(
                0.0,
                self.config.l2_cooldown_seconds
                - (timestamp - state.last_l2_voice_time).total_seconds(),
            )
            if state.last_l2_voice_time is not None
            else 0.0
        )

        if requested is not AlertLevel.L0 and state.episode_id is None:
            state.episode_sequence += 1
            state.episode_id = f"episode-{state.episode_sequence}"
            state.l2_voice_emitted = False
            state.l3_voice_emitted = False
        episode_id = state.episode_id

        self._prune_budget(state, timestamp)
        dms_value = dms or DMSArbitrationInput()
        suppression = base_reason
        voice = False
        voice_attempt = requested in {AlertLevel.L2, AlertLevel.L3} and (
            requested is not before
            or (requested is AlertLevel.L3 and state.l3_voice_emitted)
        )
        if voice_attempt:
            already_emitted = (
                requested is AlertLevel.L2
                and (state.l2_voice_emitted or state.l3_voice_emitted)
            ) or (requested is AlertLevel.L3 and state.l3_voice_emitted)
            if already_emitted:
                suppression = SuppressionReason.DUPLICATE_EPISODE
            elif (
                self.config.dms_capability_enabled
                and dms_value.active
                and dms_value.issuing_voice
                and dms_value.overlaps_predictive_reason
            ):
                suppression = SuppressionReason.SUPPRESSED_BY_DMS
            elif self.config.idling_gate_enabled and is_idling and not direct_critical:
                suppression = SuppressionReason.IDLING
            elif requested is AlertLevel.L2:
                within_cooldown = bool(
                    state.last_l2_voice_time is not None
                    and (timestamp - state.last_l2_voice_time).total_seconds()
                    < self.config.l2_cooldown_seconds
                )
                if within_cooldown:
                    suppression = SuppressionReason.COOLDOWN
                elif len(state.l2_voice_times or ()) >= self.config.rolling_l2_voice_budget:
                    suppression = SuppressionReason.VOICE_BUDGET
                else:
                    voice = True
            else:
                # Genuine L3 escalation bypasses the advisory quota, not de-duplication.
                voice = True

        if voice and requested is AlertLevel.L2:
            state.l2_voice_emitted = True
            state.last_l2_voice_time = timestamp
            assert state.l2_voice_times is not None
            state.l2_voice_times.append(timestamp)
        elif voice and requested is AlertLevel.L3:
            state.l3_voice_emitted = True

        if requested is AlertLevel.L0:
            state.episode_id = None
            state.l2_voice_emitted = False
            state.l3_voice_emitted = False
            state.l2_candidate_since = None

        state.level = requested
        state.last_timestamp = timestamp
        state.smoothed_risk = smoothed
        remaining = max(
            0, self.config.rolling_l2_voice_budget - len(state.l2_voice_times or ())
        )
        modality = (
            AlertModality.SILENT
            if requested is AlertLevel.L0
            else AlertModality.VISUAL
            if not voice
            else AlertModality.VISUAL_VOICE_HAPTIC
            if requested is AlertLevel.L3
            else AlertModality.VISUAL_VOICE
        )
        return TieredAlertDecision(
            enabled_code=enabled_code,
            journey_code=journey_code,
            timestamp=timestamp,
            raw_risk=value,
            smoothed_risk=smoothed,
            risk_slope_per_second=slope,
            persistence_duration_seconds=persistence_duration,
            cooldown_remaining_seconds=cooldown_remaining,
            previous_level=before,
            requested_level=requested,
            emitted_level=requested,
            modality=modality,
            episode_id=episode_id,
            is_idling=is_idling,
            dms_active=dms_value.active,
            direct_critical=direct_critical,
            voice_emitted=voice,
            intervention_active=requested is not AlertLevel.L0,
            level_transition=requested is not before,
            voice_budget_remaining=remaining,
            suppression_reason=suppression,
            policy_version=self.config.policy_version,
            is_mock=bool(is_mock or dms_value.is_mock),
        )
