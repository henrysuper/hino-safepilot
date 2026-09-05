# ADR 0008: Phase 3C-4 Context-Aware Voice Qualification

- Status: Accepted as Architecture; Empirical Context Optimization Unavailable
- Date: 2026-09-05

## Context

The closest Phase 3C-3 diagnostic policy preserves high visual intervention coverage but fails voice-burden and precision constraints. Phase 3C-4 asks whether historical traffic/weather evidence can qualify L2 voice without changing the frozen XGBoost model, risk detection, L1 intervention, L3 semantics, or unseen-vehicle behavior.

The metric audit found no label input to the online policy engine. It clarified that the reported 41-second median is the first L1/L2/L3 intervention lead, dominated by L1. Onset-aware emitted-voice lead is 16.5 seconds median (p25/p75 11/29 seconds). Label V0 intentionally expands prediction anchors up to 60 seconds before an event, so intervention lead must not be described as voice lead.

The audit also found a counting defect: the Phase 3C-3 evaluator collapsed an L2 voice and a later genuine L3 escalation in the same policy episode. The corrected definition counts one voice transition per `(composite trip, policy episode, level)`. The old collapsed count remains explicitly available for backward comparison. The diagnostic policy now has 63 voices (59 L2, 4 L3), 1.590 total voices/driving hour, 0.101 critical voices/hour, 0.479 label-unmatched voices/hour, and 0.698 precision. Advisory burden remains 1.489/hour.

TDX, CWA, and NCDR have relevant official capabilities, but this workspace has no admissible historical snapshots or credentials, and HINO timestamp timezone semantics remain `UNRESOLVED`. Exact date/time/geography overlap therefore cannot be established without risking temporal leakage.

## Decision

1. Keep the XGBoost bundle frozen and place an optional deterministic `ContextAwareVoiceGate` after Alert Policy v2.
2. The gate may qualify only an emitted L2 candidate. Rejection becomes L1 visual, never silent. L3 and direct critical semantics pass through unchanged.
3. Preserve `source_timestamp`, `available_timestamp`, `join_age_seconds`, and `join_distance_m`. Both source and availability must be no later than the HINO decision time.
4. Require explicit timezone handling for naive timestamps. No empirical join is permitted while HINO timezone is unresolved.
5. Provide TDX VD and CWA rainfall parsers, injected-observation providers, configurable spatial/time tolerances, explicit AVAILABLE/STALE/MISSING/ERROR states, and a content-addressed cache whose paths do not contain HINO identifiers.
6. Missing context defaults to recorded policy-only fallback. Failures are contained at the provider boundary.
7. Synthetic fixtures are always `is_mock=true`; downstream empirical evaluation excludes them.
8. Do not train a learned gate or select a simple rule without admissible development context. Do not report mock ablation improvements.
9. NCDR remains capability-only until reproducible historical access and adequate overlap are demonstrated.

## Consequences

The runtime architecture and replay contracts are ready for future historical context, but Phase 3C-4 has no empirical context winner. Required ablations are unavailable rather than filled with synthetic estimates. The acceptance rerun remains `NO FEASIBLE POLICY`; advisory voice/hour, label-unmatched voice/hour, and voice precision are the blockers.

Results remain provisional development evidence: there is no fresh holdout, only about 20 vehicles, no verified driver identity, no simultaneous DMS state, and no causal interpretation of context evidence.
