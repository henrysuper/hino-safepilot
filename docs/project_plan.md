# HINO SafePilot Dependency-Ordered Project Plan

No later stage begins until the preceding data contract or evaluation gate is approved. Current work stops after Stage 1.

## 1. Data audit - current phase

- Verify immutable source hashes, workbook/PDF structure, field definitions, types, missingness, ranges, cadence, event representation, and journey identity.
- Deliver the data dictionary, manifest, architecture, requirements, ML design, and unresolved-decision list.
- Exit gate: human accepts the documented source semantics and unresolved items.

## 2. ETL

- Implement read-only ingestion, typed normalization, quality flags/quarantine, journey assembly, event unnesting/deduplication, and partitioned derived outputs.
- Add unit tests, contract tests, fixture-only integration tests, source-hash guard, and deterministic rerun checks.
- Exit gate: typed outputs reconcile to source row counts and no raw/reference hash changes.

## 3. Label definition

- Measure per-type event-onset support across vehicles/time; review sustained-speeding and compound-risk semantics with domain owners.
- Freeze label version, target event set, onset identity, horizon boundaries, exclusion/censor rules, and alert matching rule.
- Exit gate: signed-off label specification; no candidate silently promoted to final.

## 4. Dataset generation

- Create past-only tabular and temporal windows, missingness masks, quality filters, split manifests, and dataset cards.
- Purge overlapping windows and guarantee one journey belongs to one partition.
- Exit gate: automated cutoff/leakage tests pass.

## 5. Evaluation framework

- Implement event-level matching, PR-AUC, ROC-AUC, precision, recall, F1, false alerts/driving hour, lead time, calibration, subgroup and bootstrap uncertainty.
- Freeze validation threshold-selection and one-time test evaluation protocol.

## 6. Logistic Regression baseline

- Fit train-only preprocessing and a regularized, class-aware baseline on aggregated five-minute features.
- Use it to expose leakage, calibration, and feature-contract problems before complex models.

## 7. XGBoost / LightGBM baseline

- Compare one available gradient-boosted implementation under the same manifests, labels, metrics, and alert policy.
- Avoid broad hyperparameter search; prioritize reproducibility and calibrated product metrics.

## 8. TCN / GRU experiments

- Add configurable temporal loaders, masks/time deltas, device selection, CPU fallback, optional CUDA, configurable batches, and future AMP support.
- Compare TCN and GRU only after baselines and data sufficiency justify them.

## 9. Model comparison

- Compare frozen candidates by recall, false alerts/hour, lead time, calibration, inference cost, stability across vehicles/time, and operational complexity.
- Accuracy is reported only as a secondary diagnostic, never the selection criterion.

## 10. Ensemble only if justified

- Consider an ensemble only if it yields a robust, statistically and operationally meaningful gain over the best single model.
- Include added latency, calibration, packaging, and failure-mode costs.

## 11. External historical context

- Audit historical TDX/CWA/NCDR availability and exact spatiotemporal joins.
- Materialize provenance-aware historical features offline; retain an HINO-only fallback and comparison arm.

## 12. Backend inference

- Implement the portable model service, rolling buffer, feature parity, alert engine, observability, version negotiation, and degraded states.
- Do not depend on the offline-training GPU.

## 13. Gemini

- Add optional wording/report generation over deterministic facts with schemas, safety boundaries, redaction, timeout, and template fallback.

## 14. Expo integration

- Initialize the mobile project only after backend contracts and alert behavior are stable.
- Implement pre-trip, in-trip, and post-trip flows without embedding secrets.

## 15. Bonus functions

- Add exports, advanced reports, savings scenarios with explicit assumptions, richer context, and optional small Transformer experiments only when core evidence and time permit.

