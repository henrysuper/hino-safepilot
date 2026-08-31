"""Replaceable LLM providers. Gemini is lazy and never required by analytics."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .fallback import DeterministicReportRenderer
from .models import (
    AIDailyGoalMessage,
    AITripReport,
    SanitizedDailyGoalPayload,
    SanitizedTripReportPayload,
)


class LLMProviderError(RuntimeError):
    code = "LLM_PROVIDER_ERROR"


class LLMUnavailableError(LLMProviderError):
    code = "LLM_UNAVAILABLE"


class LLMInvalidResponseError(LLMProviderError):
    code = "LLM_INVALID_RESPONSE"


T = TypeVar("T", bound=BaseModel)


def parse_structured_output(text: str, schema: type[T]) -> T:
    try:
        return schema.model_validate_json(text)
    except (ValidationError, ValueError) as exc:
        raise LLMInvalidResponseError("Provider response failed schema validation") from exc


class LLMProvider(ABC):
    provider_name: str

    @abstractmethod
    def trip_report(self, payload: SanitizedTripReportPayload) -> AITripReport: ...

    @abstractmethod
    def daily_goal(self, payload: SanitizedDailyGoalPayload) -> AIDailyGoalMessage: ...


class MockLLMProvider(LLMProvider):
    provider_name = "mock"

    def __init__(self) -> None:
        self.renderer = DeterministicReportRenderer()

    def trip_report(self, payload: SanitizedTripReportPayload) -> AITripReport:
        return self.renderer.trip_report(payload)

    def daily_goal(self, payload: SanitizedDailyGoalPayload) -> AIDailyGoalMessage:
        return self.renderer.daily_goal(payload)


class GeminiLLMProvider(LLMProvider):
    provider_name = "gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_ms: int | None = None,
        prompt_root: Path | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        self.timeout_ms = timeout_ms or int(os.getenv("GEMINI_TIMEOUT_MS", "10000"))
        self.prompt_root = prompt_root or Path(__file__).with_name("prompts")

    def _generate(self, prompt_name: str, payload: BaseModel, schema: type[T]) -> T:
        if not self.api_key:
            raise LLMUnavailableError("GEMINI_API_KEY is not configured")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise LLMUnavailableError(
                "Install the optional 'ai' dependency to use Gemini"
            ) from exc
        prompt = (self.prompt_root / f"{prompt_name}.v1.txt").read_text(encoding="utf-8")
        try:
            client = genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(timeout=self.timeout_ms),
            )
            response = client.models.generate_content(
                model=self.model,
                contents=f"{prompt}\n\nSANITIZED_FACTS_JSON:\n{payload.model_dump_json()}",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.2,
                ),
            )
        except Exception as exc:
            raise LLMUnavailableError("Gemini request failed") from exc
        return parse_structured_output(response.text or "", schema)

    def trip_report(self, payload: SanitizedTripReportPayload) -> AITripReport:
        return self._generate("trip_report", payload, AITripReport)

    def daily_goal(self, payload: SanitizedDailyGoalPayload) -> AIDailyGoalMessage:
        return self._generate("daily_goal", payload, AIDailyGoalMessage)


def provider_from_environment(name: str | None = None) -> LLMProvider:
    selected = (name or os.getenv("LLM_PROVIDER", "mock")).strip().lower()
    if selected == "mock":
        return MockLLMProvider()
    if selected == "gemini":
        return GeminiLLMProvider()
    raise ValueError(f"Unsupported LLM provider: {selected}")
