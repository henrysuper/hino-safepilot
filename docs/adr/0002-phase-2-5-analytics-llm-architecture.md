# ADR 0002: Phase 2.5 analytics and LLM architecture

- Status: Accepted for prototype
- Date: 2026-08-16

## Context

Phase 2 supplies typed telemetry and conservative onset candidates. Phase 2.5 needs post-trip facts, past-only behavior comparison, deterministic goal selection, and optional Traditional-Chinese wording without leaking competition data or making an LLM operationally necessary. The source has no trustworthy driver identity.

## Decision

1. `TripAnalytics` operates on one `trip_id`; grouping still derives from organizer-confirmed `(enabledCode, journeyCode)`, but outputs call the aggregate a `vehicle-linked behavior profile`.
2. Distance and fuel consumption use the sum of guarded positive cumulative-signal increments. Negative increments are reset/decrease boundaries. A delta above a configurable fixed floor plus time-scaled physical limit is rejected. Missing, reset, and outlier counts remain visible; no value is silently clipped.
3. Event totals use Phase 2 onset candidates within trip bounds. Normalized rates are unavailable when their denominator is absent/nonpositive. Event 7 remains `decelerate`; no sustained-speeding label is defined.
4. Event future context lives only in `analytics/post_trip_analytics.py`. It may use the next 30 seconds for explanation. The application contract retains at most 20 deterministically prioritized behavior/safety contexts per trip; event totals are never capped.
5. Baselines require contributing trip end strictly before the target cutoff. Materialized windows are previous 7 days, previous 30 days, and previous 10 trips.
6. `WeaknessScore` is config-driven. Baseline excess, absolute burden, recent trend, support, severity, and data-quality penalty combine into `[0,1]`. Prototype weights/reference rates are explicitly not HINO- or outcome-validated.
7. Outputs are five consolidated files under `data/processed/analytics/`: four JSONL contracts (`trip_summaries`, `behavior_baselines`, `weakness_scores`, `improvement_goals`) and one JSON manifest. JSONL preserves nested Pydantic contracts, supports streaming, and avoids per-trip tiny files. A normalized Parquet serving layer is deferred until backend query patterns are known.
8. `LLMSanitizer` rebuilds allowlisted payloads and recursively rejects forbidden fields. Precise coordinates, vehicle/license identifiers, raw `trip_id`, event candidate references, and source-row keys never reach a provider.
9. `LLMProvider` has mock and Gemini implementations. Mock is default. Gemini uses the optional official `google-genai` SDK, `GEMINI_MODEL`, Pydantic structured output, and versioned prompts. Provider/schema/target mismatch failures fall back to deterministic rendering.
10. No real HINO-derived payload may be sent to Gemini in this phase. Only synthetic opt-in integration tests are permitted.

## Consequences

- Core analytics, goals, and reports remain useful offline with no key, SDK, network, or quota.
- Processed artifacts contain local internal linkage keys and therefore remain private and Git-ignored.
- JSONL is easy to validate and deliver now but less efficient than columnar storage for arbitrary aggregate queries.
- Post-trip context cannot be reused for future prediction without violating the module boundary and leakage policy.
- Real driver personalization, score validation, real-data external processing, and production retention/consent policy remain `UNRESOLVED`.
