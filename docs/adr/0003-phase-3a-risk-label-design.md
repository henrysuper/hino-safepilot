# ADR 0003: Phase 3A risk label design research

- Status: Proposed — human approval required before Phase 3B
- Date: 2026-08-16

## Context

SafePilot needs an auditable contract for predicting a new high-risk event in the next 60 seconds from approximately five minutes of prior HINO telemetry. Irregular sampling, repeated event completion observations, incomplete trip tails, a timestamp anomaly, class imbalance, and only 20 vehicles make row-level label construction unsafe without an empirical policy audit.

Phase 3A is research only. It may simulate aggregate anchor/label counts but must not create ML features, final splits, or models. Phase 2 and Phase 2.5 outputs remain immutable.

## Decision

1. Add a bounded `hino_safepilot.label_research` package that reads existing derived Parquet and writes only aggregate JSON to `data/processed/label_research/`.
2. Use unique observed telemetry timestamps as anchors; never interpolate in Phase 3A.
3. Define the research boundary as input `[t-300s,t)` and target `(t,t+60s]`.
4. Recommend, but do not finalize, V0 Core Any Risk over new onsets of `decelerate`, PCS, LDWS, fatigue, or distract.
5. Use Phase 2 conservative onset candidates as the Phase 3B candidate source, retain quality/conflict evidence, quarantine the year-mismatch onset, and exclude an anchor when the same reviewed event is observed active at `t`.
6. Recommend a 10-second anchor cadence and a history screen of at least 240 seconds, at least 10 observations, and no gap over 60 seconds.
7. Require a complete 60-second trip horizon plus at least one future telemetry observation. Incomplete horizons are censored; ambiguous horizons are not negatives.
8. Treat sustained speeding and all tested duration/excess thresholds as experimental until an observable threshold-crossing definition is approved.
9. Store no private identifiers or telemetry rows in research outputs. Vehicle concentration uses anonymous ranks and aggregate indices.
10. Add a framework-free static Demo under `demo/`, backed only by synthetic JSON. Its driving view is explicitly marked `Research / Not Trained` and `Synthetic Demo`.

## Consequences

- The reference research scenario yields 209,154 eligible anchors and 53,812 positives (25.73%), but these anchors are correlated and are not yet a dataset.
- V0 has enough aggregate support for a baseline study after approval, while PCS is too sparse for a defensible standalone test claim under the candidate temporal split.
- Full-history and future-gap choices materially affect prevalence, so these policies are versioned rather than hidden defaults.
- The static Demo can explain the product flow now and later replace its centralized `StaticJsonProvider` with an API adapter. It is not production UI and performs no inference.
- Phase 3B remains blocked on human approval of label semantics, completeness thresholds, evaluation assignments, and unresolved domain definitions.

