# Preprocessing V0 — Logistic Baseline

## Contract

`preprocessing-v0-1.0` is fitted only on the 145,177-row temporal Train partition. Validation and Test contribute zero rows to imputation, scaling, encoding, or exact-constant detection. The fitted transformer is serialized with the model; production inference must use the same 163-column order from `features-v0-hino-1.0`.

## Missing-value semantics

### Structural no-event recency

The nine `seconds_since_last_<event>_onset_300s` columns are null exactly when no matching onset was available in `[t-300s,t)`. This is not a sensor failure. Each null is encoded as:

- recency value `301.0` seconds, deliberately beyond the 300-second window;
- an additional `no_prior_event__<feature>` indicator equal to 1.

Observed recencies remain unchanged and receive indicator 0. These columns are then standardized using Train-only statistics.

### Sensor / insufficient-observation missingness

The remaining nullable speed/trend statistics arise from no valid observation or too few distinct timestamps in a short history window. They use Train-only median imputation plus scikit-learn's missing indicator, followed by Train-only scaling. The generated dataset itself remains unimputed.

No Dataset V0 feature has unresolved missing semantics requiring exclusion. GPS speed-limit features remain absent because their source semantics are unresolved.

## Vehicle Type

`vehicle_series_code` is a vehicle-class feature, not identity. Values 300/500/700 are one-hot encoded with `handle_unknown="ignore"`; no ordinal magnitude is assumed. Direct vehicle, trip, anchor, license, source-row, or coordinate identifiers fail the preprocessing privacy guard.

## Constant and near-constant policy

Train-fitted `VarianceThreshold(0)` removed six exact constants:

- four `gps_speed_out_of_range_count_{30,60,120,300}s` columns;
- `vehicle_status_parked_fraction_60s`;
- `vehicle_status_parked_fraction_300s`.

The following rare/near-constant domain features remain: fatigue 30-second onset count, four PCS onset-count windows, PCS/fatigue recency encodings, and CAN normal/abnormal fractions. No target correlation was used for feature removal.

## Fit scopes and leakage controls

| Component | Fit scope |
|---|---|
| Candidate preprocessing/models | January–May Train only |
| Candidate evaluation | July Train-development only |
| Final preprocessing/model | Full Train only |
| Calibration | Early-September whole trips |
| Calibration comparison / threshold | Late-September whole trips |
| Temporal Test | November, opened once after freeze |

October has zero source support. The planned September/October subdivision was therefore replaced by a versioned, whole-trip, time-ordered September split. The machine audit is stored in the ignored Phase 3C-1 report directory.

