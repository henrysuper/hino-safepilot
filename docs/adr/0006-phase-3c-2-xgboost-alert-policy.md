# ADR 0006: Phase 3C-2 XGBoost and Alert Policy V1

- Status: Accepted as Offline Experiment
- Date: 2026-08-17

## Context

Dataset `v0_hino` and Logistic V0 passed their completion gates. November had already been exposed by Phase 3C-1, so it cannot become a fresh unbiased test for XGBoost. The next useful question is whether a nonlinear HINO-only model improves ranking and whether a stateful speech policy can reduce warning burden without changing label truth.

## Decision

1. Use protocol `xgb_development_protocol_v0`: January→March, January–March→May, and January–May→July expanding folds. Capacity, regularization, class weight, FULL/REDUCED features, Type inclusion, calibration, and alert policy are all chosen before November.
2. Use XGBoost `binary:logistic`, `hist`, `aucpr`, early stopping, deterministic seed, CUDA when available, and CPU fallback. Sensor NaN remains NaN; event-recency null becomes 301 seconds plus a no-prior-event flag. No scaling or target-fitted feature deletion is used.
3. The REDUCED set removes only Train-exact constants. Vehicle Type is a class feature, never identity, and is explicitly ablated.
4. Fit calibration on early September and evaluate/select it on late September. Raw probability is retained. Freeze the complete model/calibration/policy digest before a single November run.
5. Label November results `KNOWN_HOLDOUT COMPARATIVE BENCHMARK`; they are comparative evidence, not an unbiased final estimate.
6. Keep `AlertPolicyEngine` separate from the risk model. V1 emits a new-episode warning, permits at most one within-cooldown escalation, updates its escalation reference, never warns merely because cooldown expired, and resets only after two anchors below a lower hysteresis threshold.
7. Match alerts and onsets chronologically one-to-one in `(alert, alert+60s]`. Report event coverage, alert precision, voice/false alerts per hour, lead time, duplicates, and rolling 10-minute burden. No policy candidate is approved for production.
8. SHAP is global association-only analysis. It must not be presented as causal or as a per-driver diagnosis.

## Consequences

- Selected model: depth 5, eta 0.05, 168 rounds, modest regularization, class weight 1.0, FULL features with Type. Selection used a pre-November PR tolerance plus F1, alert burden, and Type-stability review; Type remains review-required.
- November PR-AUC is 0.5255 versus Logistic 0.4911; grouped-trip bootstrap difference is +0.0345 with 95% interval [0.0231, 0.0462]. November is known holdout evidence only.
- At its frozen 0.50 balanced research policy, XGBoost recall is lower than Logistic's operating point. Alert Policy V1 materially reduces speech burden, but event coverage is also much lower. This is a trade-off, not a production win.
- Unseen-vehicle PR-AUC is 0.4317 with Type and 0.4310 without Type; Logistic is 0.4322. Nonlinear gains therefore do not establish better unseen-vehicle generalization.
- Status `XGBOOST_EXPERIMENT_READY` means reproducible offline research. It does not authorize live warnings, deployment, or a safety claim.
