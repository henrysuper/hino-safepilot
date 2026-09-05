from __future__ import annotations

from datetime import datetime, timedelta

from hino_safepilot.alerting.contracts import (
    AlertLevel,
    AlertPolicyConfig,
    AlertPolicyConfigV2,
    AlertReason,
    AlertState,
    DMSArbitrationInput,
    SuppressionReason,
)
from hino_safepilot.alerting.engine import AlertPolicyEngine, TieredAlertPolicyEngine
from hino_safepilot.alerting.evaluation import (
    build_target_risk_episodes,
    evaluate_alerts,
    evaluate_tiered_policy,
    exposure_hours,
    replay_reference_policy,
    replay_revised_policy,
    select_policy_candidate,
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


def config_v2(**updates) -> AlertPolicyConfigV2:
    values = {
        "policy_version": "synthetic-v2",
        "l1_enter_threshold": 0.2,
        "l1_exit_threshold": 0.1,
        "l2_enter_threshold": 0.5,
        "l2_exit_threshold": 0.4,
        "l3_enter_threshold": 0.9,
        "l3_exit_threshold": 0.8,
        "ema_half_life_seconds": 0.01,
        "l2_persistence_seconds": 10.0,
        "l2_cooldown_seconds": 0.0,
        "risk_slope_l3_threshold_per_second": None,
        "rolling_voice_window_seconds": 3600,
        "rolling_l2_voice_budget": 2,
        "idling_gate_enabled": True,
        "dms_capability_enabled": False,
        "l3_bypasses_l2_voice_budget": True,
        "same_episode_l2_voice_max": 1,
        "same_episode_l3_voice_max": 1,
    }
    values.update(updates)
    return AlertPolicyConfigV2(**values)


def _persist_to_l2(engine, enabled="vehicle-a", journey="journey-a", *, idling=False):
    start = datetime(2025, 1, 1)
    engine.decide(enabled, journey, start, 0.6, is_idling=idling)
    return engine.decide(
        enabled, journey, start + timedelta(seconds=10), 0.6, is_idling=idling
    )


def test_v2_journey_code_collision_across_vehicles_does_not_share_state() -> None:
    engine = TieredAlertPolicyEngine(config_v2())
    first = _persist_to_l2(engine, "vehicle-a", "colliding-journey")
    second = _persist_to_l2(engine, "vehicle-b", "colliding-journey")
    assert first.voice_emitted
    assert second.voice_emitted
    assert first.episode_id == second.episode_id == "episode-1"


def test_v2_state_does_not_leak_between_journeys() -> None:
    engine = TieredAlertPolicyEngine(config_v2())
    assert _persist_to_l2(engine, "vehicle-a", "journey-a").voice_emitted
    assert _persist_to_l2(engine, "vehicle-a", "journey-b").voice_emitted


def test_v2_idling_suppresses_normal_predictive_voice_but_keeps_visual() -> None:
    decision = _persist_to_l2(TieredAlertPolicyEngine(config_v2()), idling=True)
    assert decision.requested_level is AlertLevel.L2
    assert decision.emitted_level is AlertLevel.L2
    assert not decision.voice_emitted
    assert decision.suppression_reason is SuppressionReason.IDLING
    assert decision.intervention_active


def test_v2_direct_critical_is_not_silenced_by_idling() -> None:
    engine = TieredAlertPolicyEngine(config_v2())
    decision = engine.decide(
        "vehicle-a", "journey-a", datetime(2025, 1, 1), 0.1,
        is_idling=True, direct_critical=True,
    )
    assert decision.emitted_level is AlertLevel.L3
    assert decision.voice_emitted


def test_v2_persistence_blocks_one_sample_l2_spike() -> None:
    engine = TieredAlertPolicyEngine(config_v2())
    start = datetime(2025, 1, 1)
    spike = engine.decide("v", "j", start, 0.6)
    cleared = engine.decide("v", "j", start + timedelta(seconds=1), 0.0)
    assert spike.emitted_level is AlertLevel.L1
    assert spike.suppression_reason is SuppressionReason.PERSISTENCE
    assert not spike.voice_emitted
    assert not cleared.voice_emitted


def test_v2_hysteresis_prevents_l2_flapping() -> None:
    engine = TieredAlertPolicyEngine(config_v2(l2_persistence_seconds=0.0))
    start = datetime(2025, 1, 1)
    entered = engine.decide("v", "j", start, 0.6)
    between = engine.decide("v", "j", start + timedelta(seconds=1), 0.45)
    assert entered.emitted_level is AlertLevel.L2
    assert between.emitted_level is AlertLevel.L2
    assert not between.level_transition


def test_v2_continuing_episode_emits_only_one_l2_voice() -> None:
    engine = TieredAlertPolicyEngine(config_v2(l2_persistence_seconds=0.0))
    start = datetime(2025, 1, 1)
    decisions = [
        engine.decide("v", "j", start + timedelta(seconds=index), 0.6)
        for index in range(4)
    ]
    assert sum(decision.voice_emitted for decision in decisions) == 1


def _new_l2_episode(engine, start, offset):
    high = engine.decide("v", "j", start + timedelta(seconds=offset), 0.6)
    engine.decide("v", "j", start + timedelta(seconds=offset + 1), 0.0)
    return high


def test_v2_rolling_l2_voice_budget_never_exceeds_two_per_hour() -> None:
    engine = TieredAlertPolicyEngine(config_v2(l2_persistence_seconds=0.0))
    start = datetime(2025, 1, 1)
    alerts = [_new_l2_episode(engine, start, offset) for offset in (0, 10, 20)]
    assert sum(value.voice_emitted for value in alerts) == 2
    assert alerts[-1].suppression_reason is SuppressionReason.VOICE_BUDGET


def test_v2_l3_can_bypass_advisory_budget_once() -> None:
    engine = TieredAlertPolicyEngine(config_v2(l2_persistence_seconds=0.0))
    start = datetime(2025, 1, 1)
    _new_l2_episode(engine, start, 0)
    _new_l2_episode(engine, start, 10)
    critical = engine.decide("v", "j", start + timedelta(seconds=20), 0.95)
    assert critical.emitted_level is AlertLevel.L3
    assert critical.voice_emitted


def test_v2_repeated_l3_is_deduplicated_in_same_episode() -> None:
    engine = TieredAlertPolicyEngine(config_v2())
    start = datetime(2025, 1, 1)
    first = engine.decide("v", "j", start, 0.95)
    repeated = engine.decide("v", "j", start + timedelta(seconds=1), 0.96)
    assert first.voice_emitted
    assert not repeated.voice_emitted
    assert repeated.suppression_reason is SuppressionReason.DUPLICATE_EPISODE


def test_v2_does_not_speak_l2_after_l3_in_same_episode() -> None:
    engine = TieredAlertPolicyEngine(config_v2(l2_persistence_seconds=0.0))
    start = datetime(2025, 1, 1)
    critical = engine.decide("v", "j", start, 0.95)
    downgraded = engine.decide("v", "j", start + timedelta(seconds=1), 0.6)
    assert critical.voice_emitted
    assert downgraded.emitted_level is AlertLevel.L2
    assert not downgraded.voice_emitted
    assert downgraded.suppression_reason is SuppressionReason.DUPLICATE_EPISODE


def test_v2_dms_arbitration_prevents_duplicate_voice() -> None:
    engine = TieredAlertPolicyEngine(
        config_v2(l2_persistence_seconds=0.0, dms_capability_enabled=True)
    )
    decision = engine.decide(
        "v", "j", datetime(2025, 1, 1), 0.6,
        dms=DMSArbitrationInput(
            active=True,
            issuing_voice=True,
            overlaps_predictive_reason=True,
            semantic_reason="synthetic-fatigue",
            is_mock=True,
        ),
        is_mock=True,
    )
    assert not decision.voice_emitted
    assert decision.suppression_reason is SuppressionReason.SUPPRESSED_BY_DMS
    assert decision.intervention_active
    assert decision.is_mock


def test_v2_mock_rows_are_excluded_from_official_metrics() -> None:
    engine = TieredAlertPolicyEngine(config_v2(l2_persistence_seconds=0.0))
    start = datetime(2025, 1, 1)
    real = engine.decide("v", "real", start, 0.6)
    mock = engine.decide("v", "mock", start, 0.6, is_mock=True)
    episodes = [{
        "enabled_code": "v",
        "journey_code": "real",
        "episode_start_time": start,
        "episode_end_time": start,
        "target_event_start_time": start + timedelta(seconds=30),
    }]
    metrics = evaluate_tiered_policy([real, mock], episodes, active_driving_hours=1.0)
    assert metrics["mock_rows_excluded"] == 1
    assert metrics["voice_alert_episode_count"] == 1
    assert metrics["voice_precision"] == 1.0


def _criteria():
    return {
        "any_intervention_recall_floor": 0.85,
        "any_intervention_recall_target": 0.90,
        "timely_coverage_3s_floor": 0.8,
        "mean_advisory_voice_per_driving_hour_max": 1.0,
        "advisory_voice_per_rolling_60min_hard_max": 2,
        "p95_rolling_advisory_voice_count_max": 2,
        "offline_false_voice_per_driving_hour_max": 0.25,
        "voice_precision_min": 0.8,
        "median_lead_time_target_seconds": 5.0,
        "same_episode_l2_voice_max": 1,
    }


def test_v2_selector_refuses_winner_when_constraints_fail() -> None:
    metrics = {
        "any_intervention_recall": 0.1,
        "timely_coverage_3s": 0.9,
        "mean_advisory_voice_per_driving_hour": 0.0,
        "p95_rolling_advisory_voice_count_60min": 0.0,
        "max_rolling_advisory_voice_count_60min": 0,
        "offline_false_voice_per_driving_hour": 0.0,
        "voice_precision": 1.0,
        "median_lead_time_seconds": 10.0,
        "same_episode_l2_voice_max_observed": 0,
        "advisory_voice_alerts_per_driving_hour": 0.0,
        "level_transitions_per_driving_hour": 1.0,
    }
    result = select_policy_candidate(
        [{"candidate_id": "bad", "policy": {}, "metrics": metrics}], _criteria()
    )
    assert result["winner"] is None
    assert result["winner_status"] == "NO_FEASIBLE_POLICY"
    assert result["constraint_violation_table"][0]["constraint_violations"] == [
        "any_intervention_recall"
    ]


def test_v2_continuous_positive_rows_form_one_target_episode() -> None:
    start = datetime(2025, 1, 1)
    timestamps = [start + timedelta(seconds=10 * index) for index in range(4)]
    episodes = build_target_risk_episodes(
        ["v"] * 4,
        ["j"] * 4,
        timestamps,
        [1, 1, 1, 0],
        events=[{
            "enabled_code": "v",
            "journey_code": "j",
            "event_start_time": start + timedelta(seconds=40),
            "event_name": "synthetic_decelerate",
            "is_mock": True,
        }],
    )
    assert len(episodes) == 1
    assert episodes[0]["positive_anchor_count"] == 3


def test_phase3c2_legacy_metric_names_remain_available() -> None:
    start = datetime(2025, 1, 1)
    result = evaluate_alerts(
        [],
        [],
        ["synthetic"],
        [start],
        {
            "eligible_anchor_based_hours": 1.0,
            "telemetry_interval_weighted_hours": 1.0,
            "telemetry_interval_weighted_status": "EXPERIMENTAL_ESTIMATE",
        },
    )
    assert "event_coverage" in result
    assert "false_alerts_per_hour_anchor_based" in result
    assert "max_alerts_in_rolling_10_minutes" in result
