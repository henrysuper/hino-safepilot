# HINO SafePilot ML System Design

## Status and prediction task

This document proposes experiments; it does not finalize a label.

For an anchor time `t`, use HINO information from approximately the prior five minutes to predict whether a **new** high-risk event onset will occur in the next 60 seconds:

- input: `X[t-300s, t)`;
- target horizon: `Y(t, t+60s]`;
- grouping: all observations and labels stay within `(enabledCode, journeyCode)`;
- inference unit: one vehicle at one anchor time;
- output candidates: multi-label per event type, an any-high-risk binary probability, or both.

The left-closed/right-open input deliberately excludes the row at `t` until event timestamp semantics are settled. The workbook frequently exposes an event on the same telemetry row as its `startTime`, and also repeats an active event after its onset. Including those event fields would reveal the answer.

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

- Anchors with less than 300 seconds of usable history are excluded or marked insufficient-history according to a reviewed rule.
- Anchors within 60 seconds of journey end are censored unless full future observation is available.
- A currently active instance of the target class is not a new-onset positive; whether to exclude such anchors or retain them as negatives is `UNRESOLVED`.
- Telemetry gaps that make either window incomplete require an explicit completeness rule.
- Events with unparseable time or ambiguous duplicates are quarantined, not silently reassigned.

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
- Freeze the newest test period before modeling. Exact dates and proportions are `UNRESOLVED` pending class-support analysis.

### Vehicle holdout

- Assign complete `enabledCode` sets to train, validation, and unseen-vehicle test.
- Stratify only using training-time aggregate information and keep assignments immutable.
- Confirm each evaluated label has support in validation and test; otherwise report it rather than moving events after seeing results.
- Exact vehicle allocation is `UNRESOLVED`; only 20 vehicles are present, so uncertainty and repeated group folds on the development set may be needed without repeatedly inspecting the final test.

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

## Human decisions required before label implementation

- Final target event set and whether hard braking equals organizer event 7.
- Multi-label versus any-risk primary product output.
- Sustained-speeding persistence, margin, and operational onset timestamp.
- Compound-risk definition and whether active states count.
- Treatment of active events at anchor time, duplicate timestamps, telemetry gaps, and incomplete horizons.
- Exact temporal dates, vehicle assignments, purge, driving-hour denominator, alert matching, alert budget, and minimum useful lead time.
- Whether raw location, vehicle series, and past event history may be used as features.

