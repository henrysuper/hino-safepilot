from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hino_safepilot.alerting import (
    AlertLevel,
    AlertModality,
    ContextAwareVoiceGate,
    SuppressionReason,
    TieredAlertDecision,
    VoiceGateAction,
    VoiceGateConfig,
)
from hino_safepilot.alerting.evaluation import evaluate_tiered_policy
from hino_safepilot.context import (
    CWAContextProvider,
    ContextAvailability,
    ContextBundle,
    ContextCache,
    ContextObservation,
    ContextQuery,
    DisasterContextSnapshot,
    NullContextProvider,
    TDXContextProvider,
    TrafficContextSnapshot,
    WeatherContextSnapshot,
    nearest_valid_observation,
    normalize_timestamp,
    parse_cwa_rainfall_observations,
    parse_tdx_vd_live,
)


UTC = timezone.utc


def _decision(timestamp: datetime, *, trip: str = "synthetic-trip") -> TieredAlertDecision:
    return TieredAlertDecision(
        enabled_code="synthetic-vehicle",
        journey_code=trip,
        timestamp=timestamp,
        raw_risk=0.7,
        smoothed_risk=0.7,
        risk_slope_per_second=0.01,
        persistence_duration_seconds=20,
        cooldown_remaining_seconds=0,
        previous_level=AlertLevel.L1,
        requested_level=AlertLevel.L2,
        emitted_level=AlertLevel.L2,
        modality=AlertModality.VISUAL_VOICE,
        episode_id="episode-1",
        is_idling=False,
        dms_active=False,
        direct_critical=False,
        voice_emitted=True,
        intervention_active=True,
        level_transition=True,
        voice_budget_remaining=1,
        suppression_reason=SuppressionReason.NONE,
        policy_version="test-policy",
    )


def _bundle(timestamp: datetime, *, available: bool, evidence: bool = False, mock: bool = False) -> ContextBundle:
    status = ContextAvailability.AVAILABLE if available else ContextAvailability.MISSING
    error = None if available else "NO_DATA"
    common = dict(
        context_timestamp=timestamp,
        source_timestamp=timestamp if available else None,
        available_timestamp=timestamp if available else None,
        freshness_seconds=0 if available else None,
        quality_status=status,
        provider_name="synthetic",
        source_dataset_version="synthetic-v1",
        error_code=error,
        is_mock=mock,
    )
    return ContextBundle(
        traffic=TrafficContextSnapshot(**common, congestion_level=0.9 if evidence else 0.1),
        weather=WeatherContextSnapshot(**common, rainfall_1h_mm=20 if evidence else 0),
        disaster=DisasterContextSnapshot(**common, flood_alert=evidence),
    )


def test_context_timestamp_and_availability_leakage_are_rejected() -> None:
    query = datetime(2025, 1, 1, 1, tzinfo=UTC)
    future = ContextObservation(
        source_timestamp=query - timedelta(minutes=1),
        available_timestamp=query + timedelta(seconds=1),
        latitude_deg=25.0,
        longitude_deg=121.0,
        values={},
        is_mock=True,
    )
    assert nearest_valid_observation(
        [future], query_timestamp=query, latitude_deg=25.0, longitude_deg=121.0,
        maximum_age_seconds=600, maximum_distance_m=1000,
    ) is None
    with pytest.raises(ValueError, match="source_timestamp"):
        TrafficContextSnapshot(
            context_timestamp=query,
            source_timestamp=query + timedelta(seconds=1),
            quality_status=ContextAvailability.AVAILABLE,
            provider_name="synthetic",
        )


def test_timezone_conversion_requires_explicit_assumption() -> None:
    local = datetime(2025, 1, 1, 8)
    assert normalize_timestamp(local, naive_timezone="Asia/Taipei") == datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="explicit timezone"):
        normalize_timestamp(local)


def test_spatial_join_selects_nearest_valid_observation() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    observations = [
        ContextObservation(now, now, 25.02, 121.0, {"name": "far"}, is_mock=True),
        ContextObservation(now, now, 25.001, 121.0, {"name": "near"}, is_mock=True),
    ]
    match = nearest_valid_observation(
        observations, query_timestamp=now, latitude_deg=25.0, longitude_deg=121.0,
        maximum_age_seconds=60, maximum_distance_m=5000,
    )
    assert match is not None and match.observation.values["name"] == "near"


def test_stale_and_missing_context_are_explicit() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    observation = ContextObservation(now, now, 25.0, 121.0, {}, is_mock=True)
    provider = TDXContextProvider([observation], maximum_age_seconds=30)
    stale = provider.traffic(ContextQuery(context_timestamp=now + timedelta(seconds=31), latitude_deg=25, longitude_deg=121))
    missing = provider.traffic(ContextQuery(context_timestamp=now, latitude_deg=None, longitude_deg=None))
    assert stale.quality_status == ContextAvailability.STALE
    assert stale.join_age_seconds == 31
    assert missing.error_code == "GPS_REQUIRED"


def test_cache_path_is_content_addressed_and_identifier_is_not_exposed(tmp_path) -> None:
    cache = ContextCache(tmp_path)
    secret_like_identity = "synthetic-enabled/synthetic-journey"
    first = cache.put_json("tdx", "vd", secret_like_identity, {"is_mock": True})
    second = cache.path_for("tdx", "vd", "another-request")
    assert secret_like_identity not in str(first)
    assert first != second
    assert cache.get_json("tdx", "vd", secret_like_identity) == {"is_mock": True}


def test_tdx_provider_parses_recorded_synthetic_fixture() -> None:
    payload = {"VDLives": [{
        "VDID": "mock-vd", "DataCollectTime": "2025-01-01T00:00:00+00:00",
        "UpdateTime": "2025-01-01T00:00:05+00:00",
        "LinkFlows": [{"Lanes": [{"Speed": 42, "Vehicles": [{"Volume": 3}]}]}],
    }]}
    rows = parse_tdx_vd_live(payload, vd_locations={"mock-vd": (25.0, 121.0, "mock-class")}, is_mock=True)
    snapshot = TDXContextProvider(rows).traffic(ContextQuery(
        context_timestamp=datetime(2025, 1, 1, 0, 1, tzinfo=UTC), latitude_deg=25, longitude_deg=121
    ))
    assert snapshot.road_speed_kph == 42
    assert snapshot.is_mock
    assert snapshot.available_timestamp <= snapshot.context_timestamp


def test_cwa_provider_parses_recorded_synthetic_fixture() -> None:
    payload = {"records": {"Station": [{
        "StationId": "mock-station",
        "ObsTime": {"DateTime": "2025-01-01T08:00:00+08:00"},
        "AvailableTime": "2025-01-01T08:00:10+08:00",
        "GeoInfo": {"Coordinates": [{"StationLatitude": 25.0, "StationLongitude": 121.0}]},
        "RainfallElement": {
            "Past10Min": {"Precipitation": 1}, "Past1hr": {"Precipitation": 5},
            "Past3hr": {"Precipitation": 8},
        },
    }]}}
    rows = parse_cwa_rainfall_observations(payload, is_mock=True)
    snapshot = CWAContextProvider(rows).weather(ContextQuery(
        context_timestamp=datetime(2025, 1, 1, 0, 2, tzinfo=UTC), latitude_deg=25, longitude_deg=121
    ))
    assert snapshot.rainfall_1h_mm == 5
    assert snapshot.join_age_seconds == 120
    assert snapshot.is_mock


def test_gate_downgrades_l2_to_l1_but_never_silences_intervention() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    gate = ContextAwareVoiceGate(VoiceGateConfig(
        context_version="test", mode="simple_context_rule", minimum_risk_without_context=0.9
    ))
    transformed, audit = gate.decide(_decision(now), _bundle(now, available=True))
    assert audit.action is VoiceGateAction.VOICE_DOWNGRADED
    assert transformed.emitted_level is AlertLevel.L1
    assert transformed.intervention_active and not transformed.voice_emitted


def test_gate_does_not_downgrade_l3() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    critical = _decision(now).model_copy(update={
        "requested_level": AlertLevel.L3,
        "emitted_level": AlertLevel.L3,
        "modality": AlertModality.VISUAL_VOICE_HAPTIC,
        "direct_critical": True,
    })
    gate = ContextAwareVoiceGate(VoiceGateConfig(
        context_version="test", mode="simple_context_rule", minimum_risk_without_context=0.9
    ))
    transformed, audit = gate.decide(critical, _bundle(now, available=True))
    assert transformed.emitted_level is AlertLevel.L3
    assert transformed.voice_emitted
    assert audit.action is VoiceGateAction.NOT_APPLICABLE


def test_missing_context_policy_only_fallback_is_auditable() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    gate = ContextAwareVoiceGate(VoiceGateConfig(
        context_version="test", mode="simple_context_rule", missing_context_behavior="policy_only"
    ))
    transformed, audit = gate.decide(_decision(now), _bundle(now, available=False))
    assert transformed.voice_emitted
    assert audit.action is VoiceGateAction.VOICE_ALLOWED
    assert set(audit.context_availability.values()) == {"missing"}


def test_mock_context_is_excluded_and_intervention_recall_is_preserved() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    decision = _decision(now)
    gate = ContextAwareVoiceGate(VoiceGateConfig(
        context_version="test", mode="simple_context_rule", minimum_risk_without_context=0.9
    ))
    transformed, _ = gate.decide(decision, _bundle(now, available=True, mock=False))
    episode = [{
        "enabled_code": "synthetic-vehicle", "journey_code": "synthetic-trip",
        "episode_start_time": now, "episode_end_time": now + timedelta(seconds=10),
        "target_event_start_time": now + timedelta(seconds=20),
    }]
    baseline = evaluate_tiered_policy([decision], episode, active_driving_hours=1)
    gated = evaluate_tiered_policy([transformed], episode, active_driving_hours=1)
    assert baseline["any_intervention_recall"] == gated["any_intervention_recall"] == 1
    assert baseline["offline_false_voice_count"] == 0
    assert gated["voice_alert_episode_count"] == 0

    mock_decision, _ = gate.decide(decision, _bundle(now, available=True, evidence=True, mock=True))
    mock_metrics = evaluate_tiered_policy([mock_decision], episode, active_driving_hours=1)
    assert mock_metrics["mock_rows_excluded"] == 1
    assert mock_metrics["voice_alert_episode_count"] == 0


def test_null_provider_remains_policy_only_compatible() -> None:
    provider = NullContextProvider(ContextAvailability.MISSING)
    snapshot = provider.traffic(ContextQuery(context_timestamp=datetime(2025, 1, 1)))
    assert snapshot.quality_status is ContextAvailability.MISSING


def test_offline_false_voice_metric_counts_an_unmatched_voice_episode() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    metrics = evaluate_tiered_policy([_decision(now)], [], active_driving_hours=2)
    assert metrics["offline_false_voice_count"] == 1
    assert metrics["offline_false_voice_per_driving_hour"] == 0.5
    assert metrics["voice_precision"] == 0
