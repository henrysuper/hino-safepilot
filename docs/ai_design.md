# Phase 2.5 AI design

## Responsibility boundary

Deterministic code owns all numbers, quality decisions, event counts/rates, baseline selection, weakness ranking, and the target improvement goal. Generative code may only summarize supplied facts and turn an already-selected target into concise Traditional Chinese. It cannot create labels, risk, scores, thresholds, locations, savings, accident claims, or causal claims.

## Provider architecture

`LLMProvider` defines two typed operations: trip report and daily goal. `MockLLMProvider` is the default and has no network dependency. `GeminiLLMProvider` is optional, lazily imports the official `google-genai` SDK, reads `GEMINI_API_KEY`, `GEMINI_MODEL`, and millisecond `GEMINI_TIMEOUT_MS`, and attaches a Pydantic response schema to `generate_content`. The development model default is centralized as `gemini-3.5-flash-lite`; business logic does not contain model-specific branches.

Unit tests use only mock/failing providers and synthetic facts. An eventual live integration test must require an explicit environment flag and use a manually created synthetic payload. Phase 2.5 made no Gemini request.

## Privacy boundary

`LLMSanitizer` constructs new allowlisted models rather than serializing `TripSummary` directly. It then recursively rejects key names representing latitude, longitude, `carNum`, `enabledCode`, trip/profile/vehicle identifiers, license plates, source rows, and event candidate references. Extra fields are rejected by all Pydantic payload contracts.

Allowed location-like context is relative only: trip minute, trip progress percentage, and relative distance. Sanitized payloads omit exact trip start/end and event timestamps. Competition raw rows and identifiers are never accepted. Any violation fails closed before provider invocation.

Real HINO-derived aggregate egress is prohibited until a human approves provider terms, retention, consent, access control, logging, regional processing, and application privacy policy. Local deterministic processing remains allowed.

## Schemas

- `TripSummary`: internal application contract for basics, guarded fuel metrics, event metrics/rates, selected event contexts, and warnings. It contains local linkage keys and is not an LLM payload.
- `BehaviorMetric` / `BehaviorBaseline`: past-only vehicle-linked metrics with cutoff, exposure, contributing trips, units, and quality.
- `WeaknessScore` / `DailyImprovementGoal`: deterministic priority and evidence. No marketing text is generated here.
- `SanitizedTripReportPayload` / `SanitizedDailyGoalPayload`: identifier-free provider input.
- `AITripReport`: summary, strengths, weaknesses, provided primary risk, recommendations, and next-trip focus.
- `AIDailyGoalMessage`: unchanged target metric, title, explanation, and action.

All models reject unknown fields. `ReportService` additionally verifies that `primary_risk` is present in supplied event facts and that the daily-goal target is unchanged.

## Prompt versioning

Prompts are code-reviewed text files under `src/hino_safepilot/ai/prompts/` with explicit version headers. Each states role, allowed facts, forbidden behavior, schema contract, Traditional Chinese, and concise/actionable style. The formal output schema is supplied through the SDK rather than duplicated as a fragile JSON example.

## Failure and fallback

Provider calls receive at most two attempts. Missing key/SDK, timeout, quota, transport error, malformed JSON, schema violation, invented primary risk, or changed goal target returns `DeterministicReportRenderer`. The fallback uses only sanitized deterministic facts and still provides trip metrics, relative strength/weakness, and the deterministic goal. Invalid provider text never reaches UI.

## Future Expo integration

Expo may consume backend-validated `TripSummary`, `DailyImprovementGoal`, `TripReportResult`, and `DailyGoalMessageResult`. The UI should display generation mode and quality warnings, and must not infer that a vehicle-linked profile is a verified person. Secrets and direct Gemini calls belong on a backend, never in Expo. Before activation, define authentication, driver-to-trip mapping, consent, retention, and real-data provider policy.
