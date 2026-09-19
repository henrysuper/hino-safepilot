# XGBoost Model V1 Design and Results

## Scope and target

XGBoost V1 predicts the unchanged binary `label_any_core_risk_60s` from Dataset `v0_hino`. It does not train event-specific models, alter Label V0, use external context, or make production alert decisions.

## Development protocol

`xgb_development_protocol_v0` contains three expanding temporal folds: January→March, January–March→May, and January–May→July. Whole-trip dataset boundaries and the existing purge/embargo contract remain authoritative. Early September fits calibration; late September selects calibration and Alert Policy research candidates. November is opened once after a configuration digest and is always labeled `KNOWN_HOLDOUT COMPARATIVE BENCHMARK`.

The bounded search first compares six capacity candidates, then eight regularization/class-weight candidates around the best capacity. Four ablations compare FULL/REDUCED and with/without vehicle Type. Primary selection is mean fold PR-AUC with a small fold-variability penalty; ROC-AUC is secondary. Search histories, best iterations, and evaluation curves are stored in the ignored experiment report.

## Preprocessing

- Sensor/telemetry null remains NaN for XGBoost missing branches.
- Structural no-event recency null becomes 301 seconds plus a no-prior-event indicator.
- No StandardScaler or sensor imputer is used.
- REDUCED removes only exact constants discovered without target labels on training data.
- Type 300/500/700 is one-hot vehicle-class context, not identity; a no-Type model is evaluated.
- Direct identity, trip, source-row, coordinate, and post-trip features are rejected.

## Selected experiment

The selected FULL+Type model uses depth 5, eta 0.05, min-child-weight 1, subsample 0.8, column sample 0.8, alpha 0.1, lambda 5, gamma 0.1, class weight 1, and 168 trees. Mean fold PR-AUC is 0.5400. All four ablations were within 0.002 PR-AUC and were therefore reviewed by late-September F1, fixed-policy burden, and Type stability. Raw probability has the best late-September Brier/log-loss/ECE profile and is retained.

November known-holdout PR-AUC/ROC-AUC is 0.5255/0.7877. At the separate frozen 0.50 research threshold, precision/recall/F1 is 0.6042/0.3125/0.4120. Logistic V0 has PR-AUC 0.4911 and a different frozen operating point with precision/recall/F1 0.4380/0.6344/0.5182. Recall-matched and grouped-trip bootstrap comparisons are in the private generated report.

## Explainability and portability

Global mean-absolute SHAP and feature-family totals are produced from a deterministic late-September sample. Values are association-only and never causal. Special audits report vehicle Type, missing indicators, and quality/density features.

Training probes modern XGBoost CUDA (`device=cuda`, `tree_method=hist`) and falls back to CPU on failure. The serialized Booster is explicitly switched to CPU, and batch-1 reload parity is checked with synthetic input. Production inference does not require the training GPU.

## Limitations

November is not an unbiased final test. Fold and subgroup variation remain material; unseen-vehicle PR-AUC does not improve over Logistic. Alert thresholds are not validated, PCS is too sparse for a standalone claim, timezone is unresolved, and no observed model gain establishes accident reduction or causality.

Phase 3C-3 freezes this model and its calibration. Tiered alert-policy development changes only downstream intervention/voice decisions; it does not retrain, retune, or materially modify XGBoost. Existing unseen-vehicle diagnostics remain visible but are not optimization targets, and November cannot be relabeled as a fresh holdout.
