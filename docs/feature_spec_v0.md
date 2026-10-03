# Feature Specification V0 — HINO-only Tabular Dataset

## Status and contract

Phase 3B-1 implements the human-approved HINO-only Dataset V0. No model is trained in this phase.

- Dataset version: `v0_hino`
- Feature specification: `features-v0-hino-1.0`
- Label specification: `label-v0-human-approved-2026-08-17`
- Input: `[t-300s,t)`
- Label: `(t,t+60s]`
- Anchor: unique existing telemetry timestamp, selected at a 10-second minimum cadence
- Final feature count: 163

The tracked source of truth is `config/features_v0.json`. The build expands it into an explicit per-column contract at `data/processed/ml/v0/metadata/resolved_feature_spec.json`, including name, source, window, aggregation, dtype, leakage status, quality handling, and model-inclusion flag for every feature.

## Physical and privacy separation

| Layer | Contents | Model input? |
|---|---|---|
| `dataset/split=*/part-00000.parquet` | hashed `anchor_id`, 163 features, six label columns | Only the 163 manifest-listed `feature_columns` |
| `metadata/split=*/part-00000.parquet` | private trip/vehicle grouping, timestamp, split, source-cutoff evidence, quality | No |
| `audit/anchor_eligibility.parquet` | every candidate anchor, eligibility, exclusion reason, horizon status | No |
| `splits/vehicle_holdout_manifest.parquet` | private vehicle-to-group assignment | No |

`anchor_id` is a deterministic SHA-256 digest of dataset version, internal trip identity, and anchor timestamp. It is a linkage key, not a model feature. Direct vehicle/trip identifiers, license plates, source rows, and coordinates are absent from the model dataset.

## Feature groups

| Group | Count | Windows | Main features |
|---|---:|---|---|
| Speed / vehicle dynamics | 56 | 30/60/120/300 s | CAN speed mean/std/min/max/median/trend/range/count; GPS speed mean/std/min/max/count/out-of-range count |
| Historical event onsets | 45 | 30/60/120/300 s plus 300 s recency | Counts and seconds-since-last for accelerate, decelerate, speeding, engine overloading, fatigue, phone, distract, PCS, LDWS |
| Speeding observation state | 4 | 30/60/120/300 s | Prior speeding observation count; final duration is never used |
| Engine / vehicle state | 46 | 60/300 s | RPM, engine load, coolant, battery, fuel level, CAN status, driving/idling/parked fractions |
| Exposure / history quality | 11 | 30/60/120/300 s and 300 s eligibility | Point count, telemetry density, elapsed trip time, history coverage, maximum gap |
| Vehicle class | 1 | Static | HINO series 300/500/700; not vehicle identity |

## Source-time rules

All telemetry statistics use only observations with `timestamp < anchor`. The anchor row itself is excluded. No interpolation, future fill, centered smoothing, post-trip value, or final-trip statistic is permitted.

Historical event features require both:

1. `event_start_time < anchor`; and
2. `first_observed_telemetry_timestamp < anchor`.

This prevents a later event record with a backfilled start time from leaking into an earlier feature window. Event end time, final duration, completion payload, and Phase 2.5 post-trip context are forbidden.

## Numeric aggregation and quality

- Mean/std/min/max/median/range operate on Phase 2 values whose quality is `valid`.
- Standard deviation is population standard deviation; a single valid value yields zero.
- Trend is ordinary least-squares slope per elapsed second using actual irregular timestamps and requires at least two distinct times.
- Missing or invalid input remains null; Dataset V0 performs no imputation.
- GPS out-of-range speed values are excluded from statistics and counted explicitly.
- Counts and valid-count features remain numeric and deterministic.
- CAN status uses organizer-documented 0 normal / 9 abnormal categories.
- Vehicle status uses organizer-documented 0 parked / 1 driving / 2 idling categories.

## Speed-limit decision

`gps.speedLimit` unit and zero semantics remain `UNRESOLVED`. The organizer event table (0929) defines speeding as speed ≥ limit +10 km/h **or** ≥ 107 km/h for 5 s; the absolute 107 km/h fallback is consistent with treating a zero limit as unavailable, but this is not explicitly confirmed. Therefore V0 emits no speed-to-limit margin, overspeed fraction, or inferred speed-limit availability feature. Past speeding onset/observation counts are allowed predictors, but speeding is not a V0 positive label.

## Labels

Primary:

- `label_any_core_risk_60s`

Diagnostics:

- `label_decelerate_60s`
- `label_pcs_60s`
- `label_ldws_60s`
- `label_fatigue_60s`
- `label_distract_60s`

Diagnostic labels preserve analysis capability; they do not imply standalone model feasibility. Event 7 remains `decelerate`.

## Eligibility and split rules

- History: at least 240 seconds, at least 10 unique telemetry points, maximum gap at most 60 seconds.
- Future: trip must extend through `t+60s` and include at least one telemetry observation in `(t,t+60s]`.
- Exclude active core-event state, invalid/zero-duration trips, trips longer than 12 hours, and the quarantined year-mismatch onset.
- One anchor per internal trip/timestamp; duplicate rows remain represented in metadata/audit evidence.
- Temporal split uses whole-trip start: train through 2025-08-31, validation through 2025-10-31, test from 2025-11-01.
- Purge anchors in `[boundary-60s, boundary+300s]`; a trip crossing a boundary is excluded rather than divided.

## Missingness, drift, and screening

`feature_availability_audit.json` reports every feature's valid/missing counts and rates, mean/std/min/max, exact distinct count, dominant fraction, per-split statistics, and validation/test standardized mean drift from train. `constant_feature_report.json` flags constant, ≥99.5%-dominant, and ≥95%-missing features without removing them.

`target_leakage_screen.json` flags an exact binary label match or absolute point-biserial correlation ≥0.98 with at least 1,000 comparable rows. This is a high-severity screen, not a feature-selection method.

## Explicitly forbidden model inputs

- direct vehicle, license, journey, trip, or source-row identity;
- latitude, longitude, raw route identity, or precise coordinates;
- anchor-row or future telemetry;
- future onset, event end, final duration, or completion payload;
- final trip duration, mileage, fuel, summary, event count, or progress percentage;
- Phase 2.5 EventContext, TripSummary, behavior goal, or any GenAI output;
- timezone-dependent local hour, weekday, or rush-hour feature while timezone remains unresolved.

