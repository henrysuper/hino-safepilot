# HINO SafePilot XGBoost Risk Experiment V1

## Model details

- Version: `v1_xgboost_1.0`
- Status: `XGBOOST_EXPERIMENT_READY`; not production ready
- Target: any new core-risk onset in the next 60 seconds
- Dataset: HINO-only `v0_hino`
- Algorithm: XGBoost binary logistic, depth 5, eta 0.05, 168 trees
- Features: FULL target-blind feature set with vehicle Type class feature
- Calibration: raw probability
- Research policy: Alert Policy V1; balanced development entry threshold 0.50, 90-second cooldown, 0.10 escalation delta, 0.10 reset margin

## Evaluation

Three expanding pre-November folds produced mean PR-AUC 0.5400 (fold standard deviation 0.0136). FULL/REDUCED and Type/no-Type were within a 0.002 PR-AUC review band; late-September F1, alert burden, and Type stability completed selection. Type remains review-required.

The single November run is a `KNOWN_HOLDOUT COMPARATIVE BENCHMARK`, because November was previously exposed in Phase 3C-1. XGBoost PR-AUC is 0.5255, ROC-AUC 0.7877, Brier 0.1462, and log loss 0.4477. At the frozen 0.50 policy threshold, precision is 0.6042 and recall is 0.3125. Logistic V0 has PR-AUC 0.4911, precision 0.4380, and recall 0.6344 at its own frozen threshold.

Grouped-trip bootstrap estimates the PR-AUC difference as +0.0345 with 95% interval [0.0231, 0.0462]. This does not restore an unbiased final test.

The frozen V1 stream produced 3.12 false alerts per eligible-anchor driving hour, 10.89% event coverage, and 26-second median matched lead time. Logistic's older simple-cooldown stream had much higher event coverage and much higher false-alert burden; neither policy is deployable.

Secondary unseen-vehicle PR-AUC is 0.4317 with Type and 0.4310 without Type, versus Logistic 0.4322. Vehicle Type therefore remains a sensitivity concern.

## Intended use and limitations

This package supports offline model comparison, calibration research, aggregate demo disclosure, and synthetic contract integration. It must not drive live warnings, autonomous control, disciplinary decisions, driver identification, event-specific PCS claims, or accident-reduction claims.

SHAP values are associations among correlated features, not causes. November is known, policy thresholds are unapproved, exposure semantics are experimental, timezone is unresolved, and the alert policy trades substantial event coverage for lower speech burden. Binaries, detailed reports, private split metadata, and identifiers remain Git-ignored.

