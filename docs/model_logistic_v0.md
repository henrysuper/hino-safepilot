# Logistic Risk Baseline V0

## Status

- Experiment: `phase3c1-logistic-v0-20260817`
- Model: `v0_logistic_1.0`
- Dataset: `v0_hino`
- Status: `BASELINE_READY`
- Scope: offline HINO-only research baseline; not a production alert system

The model predicts `label_any_core_risk_60s`. It does not claim to predict a specific event type, and no PCS standalone model or claim exists.

## Selection and calibration

Six Logistic candidates compared `C={0.1,1,10}` and `class_weight={None,balanced}` using January–May for fit and July for development evaluation. `C=0.1`, no class weighting, `lbfgs`, L2 regularization, `max_iter=1000`, and seed `20260817` won on PR-AUC (0.5165) and converged.

Early-September whole trips fitted calibration; late-September whole trips evaluated calibration and thresholds. Raw Logistic probabilities had PR-AUC 0.5339, ROC-AUC 0.7527, Brier 0.1641, log loss 0.4991, and ECE 0.0163. Sigmoid and isotonic did not improve Brier/log loss/ECE, so the frozen method is `raw`.

## Prototype operating points

| Candidate | Threshold | Precision | Recall | F1 | False alert episodes/hour |
|---|---:|---:|---:|---:|---:|
| High recall | 0.13 | 0.350 | 0.901 | 0.504 | 57.88 |
| Balanced | 0.27 | 0.455 | 0.660 | 0.539 | 27.53 |
| Low alert burden | 0.46 | 0.578 | 0.373 | 0.453 | 10.30 |

Threshold 0.27 is frozen only as the balanced offline prototype. The 60-second cooldown and driving-hour denominator are experimental and do not change label truth. No threshold is approved for production.

The experimental driving exposure counts each eligible anchor with past-60-second driving fraction at least 0.5 as 10 seconds. This is an evaluation convention, not an organizer-approved engine-on/driving-hour definition. Prototype risk levels are `low` below 0.13, `elevated` from 0.13 to below 0.27, and `high` at or above 0.27; these labels and any future colors are UI experiments only.

## Frozen temporal test

November was opened exactly once after preprocessing, model configuration, calibration method, and threshold were written to the freeze manifest.

| Metric | Result |
|---|---:|
| PR-AUC | 0.4911 |
| ROC-AUC | 0.7577 |
| Precision | 0.4380 |
| Recall | 0.6344 |
| F1 | 0.5182 |
| Brier | 0.1532 |
| Log loss | 0.4710 |
| ECE (10 bins) | 0.0184 |
| Confusion matrix | TN 15,459 / FP 5,261 / FN 2,363 / TP 4,100 |
| Raw FP anchors/driving hour | 75.69 |
| Experimental false alert episodes/driving hour | 24.63 |
| Median lead time | 48 seconds |

Event-level detection coverage was 70.55%; coverage at least 10/30 seconds early was 67.47%/57.76%. PCS had one eligible onset and cannot support a standalone interpretation.

## Stability and limitations

Expanding-month forward PR-AUC was 0.4987 in March, 0.5220 in May, and 0.5165 in July. Test PR-AUC by vehicle class was 0.5626/0.3955/0.4670 for Type 300/500/700. Vehicle class also appears among the largest standardized coefficients. This is allowed class context, not identity leakage, but it is a material dependence requiring ablation and fairness/generalization review.

The secondary unseen-vehicle protocol trained only on Train-period development vehicles, calibrated on early-validation development vehicles, and evaluated Test-period holdout vehicles. It produced PR-AUC 0.4322, ROC-AUC 0.7704, precision 0.4134, and recall 0.6199. It is reported separately from the primary temporal test.

Top positive associations include 300-second GPS/CAN speed means, past decelerate count, and short-window maximum speed. Top negative associations include vehicle-class indicators, 300-second RPM mean, and past decelerate recency. Coefficients are associations after correlated-feature scaling, never causal effects.

The current alert burden is too high for deployment. Required future work includes operating-budget approval, grouped uncertainty, vehicle-class ablation, coefficient/collinearity stability, production driving exposure semantics, and alert-episode matching review.
