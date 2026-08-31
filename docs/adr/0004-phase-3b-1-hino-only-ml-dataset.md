# ADR 0004: Phase 3B-1 HINO-only ML dataset

- Status: Accepted for Dataset V0
- Date: 2026-08-17

## Context

Phase 3A recommended a binary core-risk label but created no features or split assignments. Phase 3B-1 must materialize a model-ready tabular dataset without leaking the anchor row, future telemetry, event completion, final journey outcomes, post-trip analytics, location, or direct identity. It must also preserve private metadata for grouped evaluation without allowing those fields into model features.

## Decision

1. Freeze Label V0 as `label-v0-human-approved-2026-08-17`: any new onset of `decelerate`, PCS, LDWS, fatigue, or distract in `(t,t+60s]`.
2. Use unique existing telemetry timestamps at a minimum 10-second cadence. Require 240-second coverage, 10 points, maximum 60-second gap, complete 60-second future, and active-state exclusion.
3. Reconcile the pre-split eligible count exactly to Phase 3A's 209,154 before publishing output.
4. Materialize 163 HINO-only, past-available features from `[t-300s,t)`. Historical onset features additionally require `first_observed_telemetry_timestamp < t`.
5. Exclude `gps.speedLimit` relationship features because unit and zero semantics are unresolved. Speeding history is a predictor but not a label.
6. Physically separate model dataset and private metadata. The model dataset contains only hashed `anchor_id`, manifest-listed features, and labels. `anchor_id` is not a model feature.
7. Partition both dataset and metadata into exactly one ZSTD Parquet file per temporal split. Keep one consolidated eligibility audit and one private vehicle-holdout manifest; reject per-trip/per-vehicle files.
8. Assign whole trips by start time: train through 2025-08-31, validation through 2025-10-31, test from 2025-11-01. Purge `[boundary-60s,boundary+300s]`; never random-split rows.
9. Create a target-blind unseen-vehicle proposal using a versioned SHA-256 rank, guaranteeing one selected vehicle from each observed vehicle type before filling to four of twenty vehicles. Private mapping remains Git-ignored.
10. Fail closed: any schema/privacy/time-boundary/target-leakage/reconciliation failure sets dataset status `INVALID`.
11. Define context schemas and Null/Synthetic providers only. Perform zero external API calls and keep HINO-only inference independent.
12. Verify semantic determinism by rebuilding into a separate staging output and comparing Arrow content digests; build timestamps and physical Parquet bytes are not compared.

## Consequences

- Final output has 209,149 anchors after five boundary-embargo removals, 53,812 positives, 155,337 negatives, and 163 features.
- The physical layer uses three model Parquet files, three metadata files, one eligibility file, and one private vehicle-holdout file. This bounds file count while preserving split pruning.
- Missing values and quality indicators are retained. Imputation and scaling remain train-only responsibilities of the future model pipeline.
- Vehicle holdout prevalence differs materially from development prevalence, so vehicle-generalization uncertainty must be reported rather than hidden.
- Constant/near-constant and drift reports are recommendations only; Phase 3B-1 does not target-select features.
- `v0_hino` is suitable for a HINO-only baseline after human review of this completion report. External context remains a separate versioned ablation path.

