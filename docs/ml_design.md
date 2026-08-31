# HINO SafePilot ML System Design

## Status and prediction task

Label V0 was human-approved and materialized as Dataset `v0_hino`. Phase 3C-1 has trained and frozen the first HINO-only Logistic Regression offline baseline; it is not a production alert model.

For an anchor time `t`, use HINO information from approximately the prior five minutes to predict whether a **new** high-risk event onset will occur in the next 60 seconds:

- input: `X[t-300s, t)`;
- target horizon: `Y(t, t+60s]`;
- grouping: all observations and labels stay within `(enabledCode, journeyCode)`;
- inference unit: one vehicle at one anchor time;
- output candidates: multi-label per event type, an any-high-risk binary probability, or both.

The left-closed/right-open input deliberately excludes the row at `t`. The workbook frequently exposes an event on the same telemetry row as its `startTime`, and also repeats active/completion observations after onset. Including those event fields would reveal the answer. The label interval is strictly open at `t` and closed at `t+60s`.

## Observed constraints

- The workbook has 493,744 telemetry rows in `output`, 20 vehicles, and 3,541 organizer-confirmed vehicle/journey pairs.
- Telemetry cadence is irregular: intervals range from a few seconds to roughly one minute, with a small number of longer gaps.
- Event slots are sparse and flattened into `event[0]`, `event[1]`, and `event[2]`; slot number is not an event class.
- Observed event IDs are 1, 2, 3, 4, 6, 7, 8, 9, 11, 12, 13, 14, 16, 17, 19, and 20. IDs 5, 10, 15, and 18 are documented but not observed in this workbook.
- PCS (19), fatigue (12), and some other safety classes are very sparse relative to normal anchors. Per-vehicle and per-time support must be audited before a final target is chosen.
- Many event records have `startTime < telemetry time`, showing active/repeated state rather than a new onset.

Aggregate counts are private-data audit results and must not be copied into public examples or fixtures.

## Candidate label designs - not final

### Candidate A: event-onset multi-label vector

For each class `c`, set `y_c(t)=1` when a deduplicated event of class `c` has an onset in `(t,t+60s]` and was not already active at `t`; otherwise set it to zero when the horizon is fully observed.

Candidate mappings:

| Product concept | Organizer event candidate | Proposed onset identity | Open issue |
|---|---|---|---|
| Hard braking | 7 `decelerate` | vehicle + journey + type + `startTime` | Whether all organizer急減速 events are sufficiently severe to call hard braking is `UNRESOLVED`. |
| PCS | 19 `pcs` | same | Extremely sparse; feasibility and alert meaning require review. |
| LDWS | 20 `ldws` | same | Repeated warning semantics and sensor availability are `UNRESOLVED`. |
| Fatigue | 12 `fatigue` | same | Event-generation policy and repeated-warning suppression are `UNRESOLVED`. |
| Distract | 16 `distract` | same | Whether phone/smoking should be separate or part of distraction is `UNRESOLVED`. |
| Sustained speeding | 8 `speeding` | threshold-crossing time, not necessarily raw start | Minimum duration, speed margin, and when a warning becomes label-positive are `UNRESOLVED`. |
| Compound risk | derived from reviewed onsets/active states | derived episode ID | Combination rule, window, severity, and minimum support are `UNRESOLVED`. |

Advantages: preserves class-specific behavior and supports class-specific thresholds. Risks: severe imbalance and some classes may not span enough vehicles/time for defensible holdouts.

### Candidate B: any-high-risk onset

Set one binary target when any reviewed target event begins in the horizon. This increases positive support and is a strong first product baseline, but it merges events with different severity, sensors, and actionability. The included event set must be human-approved.

### Candidate C: hierarchical target

Predict `any_high_risk` first, then class probabilities conditional on risk. This may stabilize rare classes but adds calibration and alert-policy complexity. It is an experiment only after Candidates A/B baselines.

### Candidate D: compound-risk episode

Create a positive episode when at least two reviewed risks overlap or begin within a reviewed interval, or when a behavior event coincides with a vehicle-state risk. This could represent high-value situations but must not be invented from arbitrary weights. Domain owners must approve combinations and severity.

### Censoring and negatives

- Phase 3A recommends at least 240 elapsed seconds, at least 10 observations, and a maximum 60-second history gap; all thresholds still require approval.
- Anchors whose journey ends before `t+60s` are censored. The candidate reference also requires at least one telemetry observation in `(t,t+60s]`; otherwise the anchor is ambiguous, not negative.
- A currently active instance of a target class is not a new-onset positive. Phase 3A recommends excluding anchors with an observed active interval; a post-onset cooldown is an alert-policy experiment, not label truth.
- Telemetry gaps that make either window incomplete require an explicit completeness rule.
- Events with unparseable time or ambiguous duplicates are quarantined, not silently reassigned.

## Phase 3A empirical label recommendation

The aggregate reference simulation uses a 10-second telemetry-aligned cadence, V0 Core Any Risk, the combined history requirement above, active-state exclusion, full future observation, and quarantine/exclusion rules documented in `docs/label_design.md`.

- 209,154 eligible anchors, 53,812 positive and 155,342 negative;
- V0 prevalence 25.73% and negative/positive ratio 2.89:1;
- 9,276 censored and 99 ambiguous anchors, neither treated as negative;
- all 20 vehicles represented among positives;
- PCS has only 119 positive anchors and 42 trusted onsets and is insufficient for a standalone claim;
- V1 keeps the same truth as five class indicators plus `any_risk`;
- V2 remains experimental because sustained-speeding threshold and observable onset are unresolved.

These are correlated anchor counts, not independent event counts. The recommendation is an approval candidate and is not a final label contract.

## Feature groups

All transformations use only data strictly available before the anchor.

### Baseline tabular aggregates

- Motion: last/mean/max/std/quantiles of GPS and CAN speed; past-only slopes; acceleration/deceleration summaries computed with actual time deltas.
- Speed context: past overspeed margin and duration only after `gps.speedLimit` unit/zero semantics are confirmed.
- CAN/engine: RPM, engine load, coolant temperature, fuel level, battery, CAN status, total mileage/fuel/time deltas with reset/invalid guards.
- Vehicle state: fractions of driving, idling, and parking; state transitions.
- GPS quality: missingness, stationary ratio, coordinate jumps, azimuth change; raw coordinates should be excluded from the first baseline or privacy-reviewed.
- History: counts/time-since prior **completed or onset** events using only starts before `t`; never current or future event details.
- Time/trip context: time of day, day of week, elapsed journey time, history coverage, sample count, gap statistics, and missingness masks.

`enabledCode`, `journeyCode`, and `carNum` are grouping/split/audit identifiers, not model features. Direct identifiers can memorize vehicle or route and undermine unseen-vehicle evaluation.

### Temporal sequences

- Resample to a reviewed grid or use variable-delta sequences.
- Carry missingness masks, time since last observation, and coverage features.
- Aggregate safely within each past bin; never forward-fill from observations at or after `t`.
- Fit normalization on training partitions only.

### External context - Model V1 only

Historical traffic, weather, and disaster features require the same historical time and location as telemetry, documented coverage, and event-time availability. Live API responses cannot be retroactively treated as training data. Missing providers must not break Model V0.

## Leakage risks and controls

| Risk | Example | Required control |
|---|---|---|
| Future leakage | `event.endTime`, duration, future max speed, centered smoothing | Enforce event-time feature cutoffs and test every feature dependency. |
| Current-event leakage | event type/start appears on the row at onset | Exclude raw event slots from input; use `[t-300,t)`; derive past-event history from deduplicated starts strictly before `t`. |
| Post-trip leakage | Phase 2.5 event context inspects the next 30 seconds | Keep `post_trip_analytics` outside all feature imports; trip summaries/goals/reports are never model inputs. |
| Overlapping-window leakage | adjacent anchors share almost all rows | Split by whole journey first; purge/embargo at least input window plus horizon where boundaries can overlap. |
| Journey leakage | windows from one journey appear in multiple partitions | Persist one split assignment per `(enabledCode, journeyCode)`. |
| Vehicle leakage | same vehicle identity/behavior appears in unseen-vehicle test | Hold out complete `enabledCode` sets and remove direct identifiers. |
| Preprocessing leakage | scaler, imputer, feature selection, calibration use validation/test | Fit on train only; calibration/threshold on designated validation only. |
| Label-construction leakage | using final duration to define an onset known only later | Define operational label time explicitly; for sustained speeding use the time the persistence criterion becomes observable. |
| Route/location memorization | raw coordinates identify a vehicle/event hotspot | Compare no-coordinate baseline, privacy-review location features, and verify unseen-route behavior if used. |
| Duplicate leakage | same vehicle/journey/time appears twice | Resolve duplicates before windowing with a deterministic, audited policy. |
| Survivorship/censoring | treating unknown post-trip horizon as negative | Mark incomplete horizons censored. |
| External publication-time leakage | historical context revised after the fact | Store event-time/availability-time provenance and use only information available then. |

## Class imbalance

- Keep validation/test at natural prevalence and report event counts and vehicle coverage with uncertainty.
- Use class weights or carefully tuned focal loss only on training data; start with class-weighted Logistic Regression and boosting.
- Event-centered negative downsampling may speed training, but weights must restore the intended training distribution and no sampling crosses split boundaries.
- Do not generate nearly identical positive windows across partitions or claim more independent events than deduplicated onsets.
- If a target has too few events or vehicles for both holdouts, report `UNRESOLVED / insufficient support`; do not force a model.

## Evaluation splits

### Temporal holdout

- Assign complete journeys by journey start time: earlier journeys to train, later journeys to validation, newest journeys to test.
- Never split a journey at a calendar boundary.
- Add a 360-second minimum purge/embargo around any boundary where windows could share source observations; increase it if feature lookbacks exceed five minutes.
- Candidate boundaries are train through 2025-08-31, validation through 2025-10-31, and test from 2025-11-01, assigned by whole-trip start. Observed support is 2,389 / 663 / 489 trips. Freeze only after label approval; PCS has only two test-period onsets and cannot support a standalone test claim.

### Vehicle holdout

- Assign complete `enabledCode` sets to train, validation, and unseen-vehicle test.
- Stratify only using training-time aggregate information and keep assignments immutable.
- Confirm each evaluated label has support in validation and test; otherwise report it rather than moving events after seeing results.
- Exact vehicle allocation is `UNRESOLVED`. With only 20 vehicles, seal a small fixed unseen-vehicle test group after labels are frozen, then use grouped cross-validation inside the remaining development vehicles. Leave-one/few-vehicle-out is sensitivity analysis, not a reason to repeatedly inspect the final test.

### Recommended evaluation tracks

1. Temporal generalization on known vehicles.
2. Unseen-vehicle generalization.
3. Optional nested/hybrid analysis: temporal validation inside training vehicles and a final unseen-vehicle test.

The primary release claim must name the track; results from different tracks must not be conflated.

## Metrics and alert matching

### Model metrics

- PR-AUC: primary ranking metric under imbalance.
- ROC-AUC: secondary discrimination context, never sufficient alone.
- Precision, recall, and F1 at a validation-selected operating point.
- Calibration: reliability diagram, Brier score, expected calibration error, and calibration intercept/slope where supported.

### Product metrics

1. Event-level recall.
2. False alerts per driving hour.
3. Warning lead time from first matched alert to event onset.

An alert should match at most one event and an event at most one alert under a deterministic matching window. Cooldown-suppressed predictions are evaluated as the deployed alert stream, not as unlimited row-level classifications. The driving-hour denominator (driving state only vs. all engine-on time), matching window, minimum useful lead time, and alert budget are `UNRESOLVED`.

Report bootstrap confidence intervals by journey or vehicle, not by row. Include per-class, per-vehicle, time-period, data-quality, and vehicle-series slices where sample support permits.

## Models

### Baselines

1. Rule/prevalence sanity baselines to validate metrics and alert matching.
2. Regularized Logistic Regression on past-window aggregates with train-only scaling/imputation and class weights.
3. XGBoost or LightGBM on the same split/label manifests. The specific library is selected based on reproducible availability and licensing; it is not a reason to change the dataset.

### Temporal models

- TCN: preferred first sequence model for parallel training and bounded receptive field.
- GRU: compare on identical sequences, masks, and evaluation.
- Small Temporal Transformer: optional only if TCN/GRU and data volume justify the complexity.

### Future ensemble

Consider only after calibrated out-of-sample comparison. Candidate averaging or stacking must be trained without test leakage and must materially improve recall/false-alert/lead-time tradeoffs after operational cost.

## Calibration and threshold selection

- Compare Platt/sigmoid and isotonic calibration where validation support permits; calibrate per class only with sufficient positives.
- Fit calibration on a designated validation/calibration partition or out-of-fold development predictions, never the final test.
- Select thresholds to satisfy a reviewed false-alert/hour budget while maximizing recall and preserving useful lead time.
- Package calibration and thresholds with the model; monitor drift and reliability without auto-changing them during the test.

## Training versus inference architecture

| Concern | Offline training | Demo / production inference |
|---|---|---|
| Hardware | Configurable CPU or CUDA; RTX 3080/5080 are possible resources, not code assumptions | CPU-capable package required; optional accelerator must not be required |
| Batch | Configurable, tuned to available memory | Usually batch 1/per-vehicle micro-batch |
| Precision | FP32 baseline; future mixed precision behind config and correctness tests | Portable supported precision with numerical parity tests |
| Data | Historical partitioned dataset and split manifest | Per-vehicle rolling event-time buffer |
| Features | Shared versioned specification, fitted train-only state | Same frozen specification and preprocessing state |
| Outputs | Probabilities, metrics, calibration, model package | Calibrated risk, quality state, alert reason, version |
| Failures | Abort/quarantine with audit report | Gracefully suppress/degrade and remain observable |

Suggested future configuration fields include `device=auto|cpu|cuda`, `batch_size`, `num_workers`, `precision=fp32|amp`, `seed`, feature/label/split versions, and model export format. No GPU model name belongs in training logic.

## Phase 3B-1 tabular dataset contract

Dataset `v0_hino` is materialized under `data/processed/ml/v0/` and is `READY`; no model was trained. Each row is one eligible existing telemetry anchor. Features use `[t-300s,t)`, labels use `(t,t+60s]`, and source-availability provenance proves every contributing telemetry/event observation was available before the anchor.

The physical dataset tables contain a deterministic `anchor_id` linkage key, 163 explicitly enumerated features, and six labels. Loaders must select only `feature_columns` for `X`; `anchor_id` is never a feature. Private `trip_id`, vehicle identity, timestamps, eligibility evidence, and source-time evidence reside only in separate Git-ignored metadata/audit tables. No direct identifier, source row, latitude, longitude, or Phase 2.5 post-trip output is a model feature.

The feature families are speed/dynamics (56), historical events (45), speeding observations (4), engine/vehicle state (46), exposure/history quality (11), and vehicle class (1). Nulls and quality indicators are retained; no global imputation was fitted. The full machine-readable specification is `config/features_v0.json`, with the resolved 163-column contract copied into the private output metadata.

The primary whole-trip temporal assignment produced 145,177 train, 36,789 validation, and 27,183 test rows. Five anchors were purged in `[boundary-60s,boundary+300s]`. A separate target-blind SHA-256 vehicle holdout proposal selects four of twenty vehicles while covering every observed vehicle class; identities stay in a private Parquet manifest.

All leakage/privacy audits and the semantic two-build comparison pass. The test split contains only three PCS-positive anchors, so PCS standalone performance remains unsupported. Constant/near-constant/overwhelmingly-missing features are reported for train-time review and were not target-selected or silently deleted.

## Human decisions required before label implementation

- Approve or reject candidate V0 core event set; event 7 remains `decelerate`, and equivalence to hard braking is unresolved.
- Approve V0 any-risk as the primary baseline truth and V1 as class diagnostics, or choose another version with rationale.
- Sustained-speeding persistence, margin, and operational onset timestamp.
- Compound-risk definition and whether active states count.
- Approve the 10-second anchor cadence, combined history policy, active-state exclusion, 60-second future-observation rule, future maximum-gap policy, timestamp quarantine, and ultra-long-trip treatment.
- Freeze temporal dates, vehicle assignments, purge, driving-hour denominator, alert matching, alert budget, and minimum useful lead time.
- Confirm official timezone semantics.
- Whether raw location, vehicle series, and past event history may be used as features.

## Phase 3C-1 Logistic baseline result

The first evaluation package uses `preprocessing-v0-1.0`, Logistic `C=0.1`, no class weighting, raw probability, and prototype threshold 0.27. Candidate selection used January–May Train for fitting and July Train for development evaluation. The final model/preprocessor fitted full Train; calibration fitted early-September whole trips; calibration/threshold selection used later-September whole trips. November Test was opened once after the configuration digest was frozen.

Frozen Test metrics are PR-AUC 0.4911, ROC-AUC 0.7577, precision 0.4380, recall 0.6344, and F1 0.5182. Raw Logistic calibration was retained because sigmoid/isotonic did not improve late-validation Brier/log loss/ECE. The 60-second cooldown alert metric is experimental; 24.63 false alert episodes/driving hour is not deployable.

Vehicle-class performance and coefficients differ materially. Type 300/500/700 Test PR-AUC is 0.5626/0.3955/0.4670. This is not identity leakage, but it requires feature ablation and subgroup review before product claims. All coefficient interpretations remain associative, not causal.

`BASELINE_READY` denotes reproducible offline research, successful artifact reload, passed preprocessing/privacy/test-lock audits, and a validated prediction contract. It does not authorize production warnings or establish an approved threshold.

## Phase 3C-2 XGBoost result

Protocol `xgb_development_protocol_v0` uses January→March, January–March→May, and January–May→July expanding folds with the existing whole-trip temporal separation. The selected target-blind FULL+Type configuration uses depth 5, eta 0.05, min-child-weight 1, subsample/column sample 0.8, alpha 0.1, lambda 5, gamma 0.1, positive-class weight 1, and 168 fixed rounds inferred from fold early stopping. Missing sensor values remain NaN; recency null is 301 seconds plus an indicator; no scaling is applied.

Mean fold PR-AUC is 0.5400. FULL/REDUCED and Type/no-Type fold means lie between 0.5396 and 0.5413, so the tiny differences were treated as a 0.002 review band; late-September F1, fixed-policy burden, and Type stability then selected FULL+Type. Type PR-AUC range remains above 0.10, so review is required. Raw calibration won on late-September Brier/log-loss/ECE.

The one-time November `KNOWN_HOLDOUT COMPARATIVE BENCHMARK` has XGBoost PR-AUC 0.5255 versus Logistic 0.4911. Grouped-trip bootstrap estimates a +0.0345 difference with 95% interval [0.0231, 0.0462]. At the frozen policy threshold, XGBoost precision/recall is 0.6042/0.3125, whereas Logistic is 0.4380/0.6344 at its separate frozen operating point; ranking improvement is not the same as a product operating-point win.

Global SHAP is restricted to association-only reporting. The strongest families are dominated by prior-event history plus engine/dynamics signals. No SHAP result is causal, and no private identifier is explained.

Alert Policy V1 is evaluated separately. It materially reduces voice/false-alert burden but also lowers event coverage. Its Pareto candidates remain experimental; there is no approved production threshold or policy winner.
