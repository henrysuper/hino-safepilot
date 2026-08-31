from __future__ import annotations

from datetime import datetime

import pytest

from hino_safepilot.ai.fallback import DeterministicReportRenderer
from hino_safepilot.ai.models import AITripReport
from hino_safepilot.ai.providers import (
    GeminiLLMProvider,
    LLMInvalidResponseError,
    LLMProvider,
    LLMUnavailableError,
    MockLLMProvider,
    parse_structured_output,
)
from hino_safepilot.ai.sanitizer import (
    LLMSanitizer,
    PrivacyViolation,
    validate_no_forbidden_fields,
)
from hino_safepilot.ai.service import ReportService
from hino_safepilot.analytics.engine import TripAnalytics
from hino_safepilot.analytics.models import DailyImprovementGoal, QualityStatus
from test_analytics import onset, telemetry_rows


def synthetic_summary():
    rows = telemetry_rows()
    return TripAnalytics().analyze_trip(
        rows, [onset("decelerate", rows[1]["timestamp"])]
    )


def synthetic_goal() -> DailyImprovementGoal:
    return DailyImprovementGoal(
        vehicle_profile_id="synthetic-profile",
        effective_at=datetime(2025, 1, 3),
        target_metric="decelerate",
        priority_score=0.8,
        reason_codes=["RATE_ABOVE_30D_BASELINE"],
        evidence_summary={
            "recent_value": 4.1,
            "baseline_value": 3.0,
            "unit": "events_per_100km",
            "change_pct": 36.7,
        },
        quality_status=QualityStatus.VALID,
        scoring_version="synthetic-v1",
    )


def test_sanitizer_removes_identifiers_and_precise_location() -> None:
    payload = LLMSanitizer().trip_report(synthetic_summary()).model_dump(mode="json")
    serialized_keys = str(payload).lower()
    for forbidden in ("trip_id", "enabled_code", "latitude", "longitude", "vehicle_profile_id"):
        assert forbidden not in serialized_keys


@pytest.mark.parametrize(
    "payload",
    [
        {"latitude": 25.0},
        {"nested": {"enabledCode": "synthetic"}},
        {"items": [{"source_row_number": 12}]},
        {"trip_id": "synthetic-trip"},
    ],
)
def test_forbidden_field_rejection(payload: dict) -> None:
    with pytest.raises(PrivacyViolation):
        validate_no_forbidden_fields(payload)


def test_mock_provider_returns_valid_structured_output() -> None:
    result = ReportService(MockLLMProvider()).trip_report(synthetic_summary())
    assert result.generation_mode == "mock"
    assert isinstance(result.report, AITripReport)


def test_gemini_structured_output_parser_accepts_valid_json() -> None:
    raw = AITripReport(
        summary="合成摘要",
        strengths=["平順"],
        weaknesses=["急減速"],
        primary_risk="decelerate",
        recommendations=["提早減速"],
        next_trip_focus="增加跟車距離",
    ).model_dump_json()
    assert parse_structured_output(raw, AITripReport).primary_risk == "decelerate"


def test_malformed_structured_output_is_rejected() -> None:
    with pytest.raises(LLMInvalidResponseError):
        parse_structured_output('{"summary": 1}', AITripReport)


class UnavailableProvider(LLMProvider):
    provider_name = "unavailable"

    def trip_report(self, payload):
        raise LLMUnavailableError("synthetic timeout")

    def daily_goal(self, payload):
        raise LLMUnavailableError("synthetic timeout")


class WrongTargetProvider(MockLLMProvider):
    provider_name = "wrong-target"

    def daily_goal(self, payload):
        result = super().daily_goal(payload)
        return result.model_copy(update={"target_metric": "speeding"})


class MalformedProvider(LLMProvider):
    provider_name = "malformed"

    def trip_report(self, payload):
        return parse_structured_output('{"summary": 1}', AITripReport)

    def daily_goal(self, payload):
        raise LLMInvalidResponseError("synthetic malformed response")


def test_api_unavailable_falls_back_and_deterministic_report_still_works() -> None:
    result = ReportService(UnavailableProvider(), max_attempts=1).trip_report(synthetic_summary())
    assert result.generation_mode == "deterministic_fallback"
    assert result.report.summary


def test_malformed_provider_output_falls_back() -> None:
    result = ReportService(MalformedProvider(), max_attempts=1).trip_report(synthetic_summary())
    assert result.generation_mode == "deterministic_fallback"
    assert result.provider_error_code == "LLM_INVALID_RESPONSE"


def test_provider_cannot_change_daily_goal_target() -> None:
    result = ReportService(WrongTargetProvider(), max_attempts=1).daily_goal(synthetic_goal())
    assert result.generation_mode == "deterministic_fallback"
    assert result.message.target_metric == "decelerate"


def test_deterministic_renderer_works_without_llm() -> None:
    payload = LLMSanitizer().trip_report(synthetic_summary())
    report = DeterministicReportRenderer().trip_report(payload)
    assert report.recommendations


def test_gemini_provider_missing_key_fails_before_network(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiLLMProvider(api_key=None)
    with pytest.raises(LLMUnavailableError):
        provider.trip_report(LLMSanitizer().trip_report(synthetic_summary()))
