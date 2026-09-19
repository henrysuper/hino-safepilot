# ADR 0005: Phase 3C-1 Logistic baseline

- Status: Accepted as Offline Baseline
- Date: 2026-08-17

## Context

Dataset `v0_hino` is leakage/privacy audited and human-authorized for a first 60-second risk baseline. Test labels must not influence preprocessing, candidate selection, calibration, or threshold choice. Validation contains September only, so the proposed September/October subdivision is impossible.

## Decision

1. Compare only Dummy and six regularized Logistic candidates. Select candidates on a Train-internal time split: January–May fit, July evaluation.
2. Fit the final preprocessing/model on full Train. Fit raw/sigmoid/isotonic calibration candidates on early-September whole trips and compare/select them on late-September whole trips.
3. Encode structural no-event recency as 301 seconds plus an indicator. Median-impute other missing values on Train only, add missing indicators, remove Train-exact constants, standardize numeric columns, and one-hot vehicle Type.
4. Select three validation operating points. Freeze balanced max-F1 threshold 0.27 solely as an offline prototype because no alert budget is approved.
5. Write a configuration digest before opening November Test. Evaluate Test once; later changes require a new experiment version.
6. Report raw false-positive anchors and a separately marked experimental 60-second cooldown episode metric. Cooldown does not alter labels.
7. Package pipeline, calibrator, feature schema, threshold configuration, and experiment manifest together. CPU inference and validated `RiskPredictionResult` are required.
8. Keep model binaries/reports ignored; track only documentation/model card. No external context or advanced model is used.

## Consequences

- `BASELINE_READY` means a reproducible offline research baseline, not production readiness.
- Test PR-AUC is 0.4911 versus a late-validation prior baseline of 0.2679; test recall is 0.6344 at the prototype threshold.
- Test false-alert burden remains high even after experimental cooldown, so the model must not drive real alerts.
- Raw probability calibration outperformed the fitted alternatives on late validation and remains frozen.
- Vehicle-class dependence and uneven class performance require explicit ablation before a stronger product claim.
- The fixed November evaluation cannot be reused to tune this experiment. XGBoost, if separately authorized, must use a new version while preserving the same lock policy.

