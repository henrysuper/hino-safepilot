# HINO SafePilot Dependency-Ordered Project Plan

No later stage begins until the preceding data contract or evaluation gate is approved. Phase 1 through Phase 3C-2 are complete. Logistic and XGBoost are offline comparison artifacts; temporal models and production alerts still require separate human gates.

## 1. Data audit - complete

- Verify immutable source hashes, workbook/PDF structure, field definitions, types, missingness, ranges, cadence, event representation, and journey identity.
- Deliver the data dictionary, manifest, architecture, requirements, ML design, and unresolved-decision list.
- Exit gate: human accepts the documented source semantics and unresolved items.

## 2. ETL - complete (2026-08-16)

- Implement read-only ingestion, typed normalization, quality flags/quarantine, journey assembly, event unnesting/deduplication, and partitioned derived outputs.
- Add unit tests, contract tests, fixture-only integration tests, source-hash guard, and deterministic rerun checks.
- Exit gate: typed outputs reconcile to source row counts and no raw/reference hash changes.

Implementation evidence: 493,744 normalized telemetry rows, 111,172 event observations, 86,789 conservative onset candidates, 17 preserved duplicate groups, stable source hashes, readable Parquet schemas, and passing synthetic tests. Phase 2 does not approve label semantics.

## 2.5 Trip analytics and optional report wording - complete (2026-08-16)

- Compute deterministic trip basics, guarded cumulative mileage/fuel deltas, exposure-normalized event rates, and post-trip-only event context.
- Materialize past-only 7-day, 30-day, and 10-trip vehicle-linked baselines.
- Select improvement goals with versioned rule weights; no LLM selects or changes the target.
- Enforce a sanitized aggregate boundary, mock-first provider abstraction, Gemini structured-output adapter, and deterministic fallback.
- Exit evidence: 3,541 trip summaries, 10,623 baseline records, 3,541 weakness/goal records, zero rejected trips, zero baseline cutoff violations, all 3,541 sanitized trip payloads validated, and no external API calls.

Weights, reference rates, driver identity, and real-data Gemini egress remain `UNRESOLVED`.

## 3A. Label research and local results viewer - complete (2026-08-16)

- Audited every documented event type, onset identity, speeding semantics, compound sequences, timestamp anomaly, active state, cadence, history, future completeness, quality, imbalance, vehicle concentration, time distribution, and split candidates.
- Simulated aggregate label policies without creating features, final splits, or models. Recommended V0 Core Any Risk at a 10-second telemetry-aligned cadence; recommendation is not final.
- Added a synthetic-only, framework-free localhost viewer for pre-drive, explicitly untrained driving-contract preview, and post-drive results.
- Evidence: 209,154 eligible reference anchors, 53,812 positives (25.73%), 9,276 censored, 99 ambiguous, and no identifiers/telemetry rows in aggregate outputs.

## 3B-1. Label freeze and HINO-only tabular dataset - complete (2026-08-17)

- Materialized the human-approved binary `any_core_risk_next_60s` contract and five diagnostic labels without training a model.
- Built 163 past-only tabular features for 209,149 eligible anchors, physically separating model columns from private audit metadata.
- Assigned whole-trip temporal splits, purged 5 boundary anchors, created a target-blind private vehicle-holdout manifest, and passed leakage, privacy, schema, split, and deterministic-rebuild audits.
- Added typed traffic/weather/disaster context contracts with Null and Synthetic providers only; no external API was called.
- Exit evidence: 53,812 positives, 155,337 negatives, dataset status `READY`, and zero feature-source-time violations.

## 4. Dataset generation - V0 tabular complete

- V0 tabular outputs and manifests are complete under `data/processed/ml/v0/`.
- Temporal sequence tensors remain future work and must reuse the frozen anchor/split manifests.
- Imputation remains in the future model pipeline and must be fitted on training data only.

## 5. Evaluation framework - baseline implementation complete (2026-08-17)

- Implemented ranking, threshold sweep, calibration/reliability, raw/episode alert burden, event lead time, vehicle type, temporal stability, coefficients, and private unseen-vehicle evaluation.
- Froze validation threshold selection and performed exactly one November temporal Test evaluation.
- Grouped bootstrap confidence intervals remain future work before production claims.

Exit evidence: preprocessing/privacy/reproducibility/test-lock/artifact/contract gates all pass.

## 6. Logistic Regression baseline - complete (2026-08-17)

- Selected `C=0.1`, no class weight, and raw probability using development-only periods.
- Frozen temporal Test PR-AUC 0.4911, ROC-AUC 0.7577, precision 0.4380, recall 0.6344, and median lead time 48 seconds.
- Status is `BASELINE_READY` for offline research. High alert burden and vehicle-class dependence block production use.

## 7. XGBoost baseline - complete (2026-08-17)

- Completed a bounded staged XGBoost search over three expanding pre-November folds, including class weight, FULL/REDUCED, and Type/no-Type ablations.
- Added raw/sigmoid/isotonic calibration comparison, global SHAP audit, grouped-trip bootstrap intervals, unseen-vehicle sensitivity, and CUDA/CPU portability.
- Added deterministic Alert Policy V1 replay and one-to-one event matching. No production policy winner was selected.
- Exit evidence: status `XGBOOST_EXPERIMENT_READY`; November is explicitly a known-holdout comparative benchmark, not an unbiased final test.

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

## 13. Gemini production policy and integration

- Phase 2.5 has a mock-first, structured-output prototype. Before product activation, approve real-data egress, retention, consent, timeout/quota policy, monitoring, and backend secret management.

## 14. Expo integration

- Initialize the mobile project only after backend contracts and alert behavior are stable.
- Implement pre-trip, in-trip, and post-trip flows without embedding secrets.

## 15. Bonus functions

- Add exports, advanced reports, savings scenarios with explicit assumptions, richer context, and optional small Transformer experiments only when core evidence and time permit.
