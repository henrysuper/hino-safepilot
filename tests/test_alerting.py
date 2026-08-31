from __future__ import annotations

from datetime import datetime, timedelta

from hino_safepilot.alerting.contracts import AlertPolicyConfig, AlertReason, AlertState
from hino_safepilot.alerting.engine import AlertPolicyEngine
from hino_safepilot.alerting.evaluation import (
    evaluate_alerts,
    exposure_hours,
    replay_reference_policy,
    replay_revised_policy,
)


def config() -> AlertPolicyConfig:
    return AlertPolicyConfig(
        policy_version="synthetic-v1",
        threshold_enter=0.6,
        threshold_reset=0.4,
        cooldown_seconds=50,
        escalation_delta=0.15,
        reset_consecutive_anchors=2,
        one_escalation_per_active_episode=True,
    )


def test_new_episode_escalation_and_no_continuous_repeat() -> None:
    engine = AlertPolicyEngine(config())
    start = datetime(2025, 1, 1)
    first = engine.decide("synthetic", start, 0.65)
    escalation = engine.decide("synthetic", start + timedelta(seconds=10), 0.82)
    repeated = engine.decide("synthetic", start + timedelta(seconds=20), 0.99)
    expired = engine.decide("synthetic", start + timedelta(seconds=90), 0.99)
    assert first.should_speak and first.reason is AlertReason.NEW_RISK_EPISODE
    assert escalation.should_speak and escalation.reason is AlertReason.RISK_ESCALATION
    assert not repeated.should_speak
    assert not expired.should_speak  # cooldown expiry alone is not another warning


def test_hysteresis_requires_two_low_anchors_then_allows_new_episode() -> None:
    engine = AlertPolicyEngine(config())
    start = datetime(2025, 1, 1)
    engine.decide("synthetic", start, 0.7)
    one_low = engine.decide("synthetic", start + timedelta(seconds=10), 0.3)
    reset = engine.decide("synthetic", start + timedelta(seconds=20), 0.2)
    next_episode = engine.decide("synthetic", start + timedelta(seconds=30), 0.7)
    assert one_low.state_after is AlertState.ACTIVE
    assert reset.state_after is AlertState.NORMAL
    assert reset.reason is AlertReason.HYSTERESIS_RESET
    assert next_episode.should_speak


def test_trip_states_are_independent() -> None:
    engine = AlertPolicyEngine(config())
    now = datetime(2025, 1, 1)
    assert engine.decide("a", now, 0.7).should_speak
    assert engine.decide("b", now, 0.7).should_speak


def test_below_threshold_and_one_percent_rise_do_not_alert() -> None:
    engine = AlertPolicyEngine(config())
    start = datetime(2025, 1, 1)
    assert not engine.decide("synthetic", start, 0.59).should_speak
    assert engine.decide("synthetic", start + timedelta(seconds=10), 0.62).should_speak
    assert not engine.decide("synthetic", start + timedelta(seconds=20), 0.63).should_speak


def test_all_escalation_delta_candidates_update_reference_once() -> None:
    start = datetime(2025, 1, 1)
    for delta in (0.10, 0.15, 0.20):
        candidate = config().model_copy(update={"escalation_delta": delta})
        engine = AlertPolicyEngine(candidate)
        engine.decide("synthetic", start, 0.60)
        assert engine.decide("synthetic", start + timedelta(seconds=10), 0.60 + delta).should_speak
        assert not engine.decide("synthetic", start + timedelta(seconds=20), 1.0).should_speak


def test_matching_is_one_alert_to_one_event_and_duplicate_is_audited() -> None:
    start = datetime(2025, 1, 1)
    alerts = [
        {"trip_id": "synthetic", "timestamp": start, "probability": 0.8, "reason": "new"},
        {"trip_id": "synthetic", "timestamp": start + timedelta(seconds=10), "probability": 0.9, "reason": "repeat"},
    ]
    events = [{"trip_id": "synthetic", "event_start_time": start + timedelta(seconds=30), "event_name": "decelerate"}]
    exposure = {"eligible_anchor_based_hours": 1.0, "telemetry_interval_weighted_hours": 1.0, "telemetry_interval_weighted_status": "EXPERIMENTAL_ESTIMATE"}
    result = evaluate_alerts(alerts, events, ["synthetic"], [start], exposure)
    assert result["matched_alert_count"] == 1
    assert result["false_alert_count"] == 1
    assert result["duplicate_alerts_for_event"] == 1
    assert result["median_lead_time_seconds"] == 30.0


def test_original_probability_rise_policy_is_noisier_than_revised() -> None:
    start = datetime(2025, 1, 1)
    trips = ["synthetic"] * 4
    timestamps = [start + timedelta(seconds=10 * value) for value in range(4)]
    probabilities = [0.62, 0.63, 0.64, 0.65]
    original = replay_reference_policy(
        trips, timestamps, probabilities, kind="original_user_policy", threshold=0.60, cooldown_seconds=50
    )
    revised = replay_revised_policy(trips, timestamps, probabilities, config())
    assert len(original) == 4
    assert len(revised) == 1


def test_replay_and_exposure_are_deterministic() -> None:
    start = datetime(2025, 1, 1)
    trips = ["synthetic"] * 4
    timestamps = [start + timedelta(seconds=10 * value) for value in range(4)]
    probabilities = [0.7, 0.75, 0.2, 0.7]
    first = replay_revised_policy(trips, timestamps, probabilities, config())
    second = replay_revised_policy(trips, timestamps, probabilities, config())
    assert first == second
    exposure = exposure_hours(trips, timestamps, [1.0, 1.0, 0.0, 0.5])
    assert exposure["eligible_anchor_based_hours"] > 0
