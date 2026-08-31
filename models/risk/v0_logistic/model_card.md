# HINO SafePilot Logistic Risk Baseline V0

## Model details

- Version: `v0_logistic_1.0`
- Status: `BASELINE_READY` for offline research
- Algorithm: L2 Logistic Regression, `C=0.1`, `lbfgs`
- Target: any new core-risk onset in the next 60 seconds
- Inputs: 163 HINO-only past-window features; 6 Train-exact constants removed by preprocessing
- Calibration: raw Logistic probability
- Prototype threshold: 0.27 (`EXPERIMENTAL`)

Prototype UI levels use 0.13 and 0.27 boundaries and are explicitly unvalidated. The driving-hour metric treats each qualifying driving anchor as 10 seconds of exposure; this denominator is not yet domain-approved.

## Intended use

This artifact establishes a reproducible HINO-only ranking baseline and evaluates feature, calibration, threshold, lead-time, and alert-burden contracts. It may support offline architecture research and synthetic UI integration.

It must not be used for real-time safety alerts, autonomous control, disciplinary decisions, driver identity inference, accident-reduction claims, or standalone PCS/fatigue/LDWS/decelerate/distract prediction.

## Evaluation

Frozen November temporal Test: PR-AUC 0.4911, ROC-AUC 0.7577, precision 0.4380, recall 0.6344, F1 0.5182, Brier 0.1532, log loss 0.4710. Median event warning lead time was 48 seconds. Experimental false alert episodes were 24.63 per driving hour under an unapproved 60-second cooldown.

Secondary unseen-vehicle evaluation is reported separately: PR-AUC 0.4322, ROC-AUC 0.7704, precision 0.4134, recall 0.6199.

## Limitations

- Alert burden is too high for deployment.
- Adjacent anchors are correlated; row counts are not independent event counts.
- Vehicle Type has large coefficients and materially different subgroup performance.
- Event types are merged into one target; PCS support is insufficient for any standalone claim.
- Timezone, production driving-hour denominator, alert budget/cooldown, and UI risk levels remain unresolved.
- Coefficients are non-causal associations among correlated standardized features.

The serialized binaries, private manifests, detailed reports, and any source-derived identifiers remain Git-ignored.
