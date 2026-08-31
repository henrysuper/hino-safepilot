"""Validated report orchestration with bounded retries and deterministic fallback."""

from __future__ import annotations

from .fallback import DeterministicReportRenderer
from .models import DailyGoalMessageResult, TripReportResult
from .providers import LLMProvider, LLMProviderError, MockLLMProvider
from .sanitizer import LLMSanitizer
from hino_safepilot.analytics.models import DailyImprovementGoal, TripSummary


class ReportService:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        sanitizer: LLMSanitizer | None = None,
        fallback: DeterministicReportRenderer | None = None,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.provider = provider
        self.sanitizer = sanitizer or LLMSanitizer()
        self.fallback = fallback or DeterministicReportRenderer()
        self.max_attempts = max_attempts

    def trip_report(self, summary: TripSummary) -> TripReportResult:
        payload = self.sanitizer.trip_report(summary)
        last_error: Exception | None = None
        for _ in range(self.max_attempts):
            try:
                report = self.provider.trip_report(payload)
                allowed_risks = {event.event_name for event in payload.event_metrics}
                allowed_risks.add("insufficient_data")
                if report.primary_risk not in allowed_risks:
                    raise LLMProviderError("Provider invented an unsupported primary risk")
                return TripReportResult(
                    report=report,
                    generation_mode=(
                        "mock" if isinstance(self.provider, MockLLMProvider) else "llm"
                    ),
                )
            except Exception as exc:
                last_error = exc
        return TripReportResult(
            report=self.fallback.trip_report(payload),
            generation_mode="deterministic_fallback",
            provider_error_code=(
                last_error.code
                if isinstance(last_error, LLMProviderError)
                else "LLM_PROVIDER_ERROR"
            ),
        )

    def daily_goal(self, goal: DailyImprovementGoal) -> DailyGoalMessageResult:
        payload = self.sanitizer.daily_goal(goal)
        last_error: Exception | None = None
        for _ in range(self.max_attempts):
            try:
                message = self.provider.daily_goal(payload)
                if message.target_metric != payload.target_metric:
                    raise LLMProviderError("Provider changed deterministic target metric")
                return DailyGoalMessageResult(
                    message=message,
                    generation_mode=(
                        "mock" if isinstance(self.provider, MockLLMProvider) else "llm"
                    ),
                )
            except Exception as exc:
                last_error = exc
        return DailyGoalMessageResult(
            message=self.fallback.daily_goal(payload),
            generation_mode="deterministic_fallback",
            provider_error_code=(
                last_error.code
                if isinstance(last_error, LLMProviderError)
                else "LLM_PROVIDER_ERROR"
            ),
        )
