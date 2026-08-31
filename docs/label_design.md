# Phase 3A Risk Label Design and Empirical Audit

## Status and scope

This document records aggregate Phase 3A research. It recommends a candidate label contract for human review; it does **not** authorize dataset generation, feature construction, model training, or production alerts.

The intended task is pre-event prediction:

- input observations: `[t-300s,t)`;
- label horizon: `(t,t+60s]`;
- target: one or more **new** reviewed event onsets;
- not a target: a current event, persistence of an event already active at `t`, or an event reconstructed with future information.

Phase 2 Parquet and Phase 2.5 analytics were read-only inputs. Phase 3A writes aggregate-only JSON under `data/processed/label_research/`. No training feature vector, raw telemetry row, precise location, source row, trip identifier, or vehicle identifier is written there.

## Research method

The research CLI is `python -m hino_safepilot.label_research`. It reads the Phase 2 normalized telemetry, event observations, conservative onset candidates, and Phase 2.5 trip summaries. It selects anchors only from unique existing telemetry timestamps and does not interpolate.

The reference simulation varies one policy at a time around:

| Concern | Reference research policy |
|---|---|
| Anchor | At most one existing timestamp every 10 seconds per trip |
| Input | `[t-300s,t)` |
| History | At least 240 seconds, at least 10 points, maximum gap at most 60 seconds |
| Label | V0 Core Any Risk |
| Horizon | Full `(t,t+60s]` plus at least one observed telemetry timestamp in that interval |
| Active state | Exclude an anchor if the same reviewed event is observed active at `t` |
| Trip quality | Exclude invalid/zero-duration and longer-than-12-hour trips pending review |
| Timestamp anomaly | Quarantine the year-mismatch onset |

The 12-hour rule is a research sensitivity choice, not an organizer definition.

## Event inventory

The inventory explicitly includes documented types with zero support. Counts below are organizer observations, Phase 2 onset candidates, and trusted within-trip candidates; they are aggregate private-data audit results.

| Type | Source name | Observations | Onset candidates | Trusted onsets | Trips | Vehicles |
|---:|---|---:|---:|---:|---:|---:|
| 1 | driving | 4,823 | 4,823 | 4,823 | 3,530 | 20 |
| 2 | idle | 3,215 | 1,620 | 1,613 | 699 | 20 |
| 3 | parked | 3,511 | 3,511 | 3,511 | 3,510 | 20 |
| 4 | engine_on | 3,517 | 3,517 | 3,517 | 3,517 | 20 |
| 5 | fence | 0 | 0 | 0 | 0 | 0 |
| 6 | accelerate | 3,001 | 3,001 | 3,001 | 1,007 | 20 |
| 7 | decelerate | 18,568 | 18,568 | 18,567 | 1,849 | 19 |
| 8 | speeding | 26,461 | 13,239 | 13,237 | 1,837 | 20 |
| 9 | dtc | 103 | 103 | 95 | 92 | 13 |
| 10 | engine_overheat | 0 | 0 | 0 | 0 | 0 |
| 11 | engine_overloading | 20,520 | 10,954 | 10,953 | 1,634 | 12 |
| 12 | fatigue | 686 | 686 | 686 | 198 | 10 |
| 13 | seatbelt | 13,726 | 13,726 | 13,726 | 672 | 10 |
| 14 | phone | 2,478 | 2,478 | 2,478 | 300 | 10 |
| 15 | smoking | 0 | 0 | 0 | 0 | 0 |
| 16 | distract | 5,012 | 5,012 | 5,012 | 486 | 10 |
| 17 | lens_ward_off | 1,480 | 1,480 | 1,480 | 32 | 6 |
| 18 | driver_lost | 0 | 0 | 0 | 0 | 0 |
| 19 | pcs | 42 | 42 | 42 | 40 | 13 |
| 20 | ldws | 4,029 | 4,029 | 4,029 | 543 | 18 |

Detailed first/last dates, repeated observations, variants, duration/end-time availability, interval distributions, per-trip rates, per-100-km rates, and per-driving-hour rates are in `event_inventory.json`. Event 7 remains `decelerate`; equivalence to hard braking is `UNRESOLVED`.

## Core event support

| Core event | Trusted onsets | Trips | Vehicles | Events/100 km | Events/driving hour |
|---|---:|---:|---:|---:|---:|
| decelerate | 18,567 | 1,849 | 19 | 35.054 | 11.926 |
| PCS | 42 | 40 | 13 | 0.079 | 0.027 |
| LDWS | 4,029 | 543 | 18 | 7.607 | 2.588 |
| fatigue | 686 | 198 | 10 | 1.295 | 0.441 |
| distract | 5,012 | 486 | 10 | 9.463 | 3.219 |

The exposure rates describe observed organizer events; they do not prove severity, cause, or independent samples.

## Speeding research

Speeding is not in candidate V0/V1. Of 13,239 onset candidates, 13,237 are trusted within-trip episodes. Duration is available for 13,222 (99.89%): median 37 seconds, p90 108 seconds, maximum 2,214 seconds. An excess-speed value is available for all trusted episodes: median 7 km/h, p90 21 km/h, maximum 76 km/h.

Observed research thresholds are sensitivity probes only:

- duration at least 10/30/60 seconds: 12,450 / 7,914 / 3,747 episodes;
- excess at least 5/10/20 km/h: 9,083 / 5,449 / 1,610 episodes;
- combined threshold counts are retained in `speeding_research.json`.

The repeated records overwhelmingly look like onset/completion records with evolving speed payload. The raw onset is identifiable, but a duration-qualified label cannot be known at raw onset without future information. Phase 3B would need an operational threshold-crossing timestamp. Sustained speeding and every numerical threshold remain `EXPERIMENTAL / UNRESOLVED`; none is claimed as a HINO or legal standard.

## Compound sequence associations

Each rate is the share of source onsets followed by at least one target onset within the stated horizon in the same trip. These are temporal associations only.

| Sequence | 30 s | 60 s | 120 s | 300 s |
|---|---:|---:|---:|---:|
| speeding → decelerate | 15.84% | 25.46% | 31.89% | 39.97% |
| speeding → LDWS | 4.37% | 7.15% | 10.92% | 19.58% |
| LDWS → decelerate | 8.02% | 13.90% | 19.71% | 28.49% |
| fatigue → LDWS | 7.00% | 12.24% | 22.01% | 39.36% |
| distract → LDWS | 4.43% | 7.24% | 11.03% | 17.38% |
| accelerate → decelerate | 27.16% | 47.42% | 62.21% | 69.44% |

No causal claim or compound-risk label is justified by these statistics. Base-rate adjustment, repeated-measures uncertainty, operational actionability, and domain severity review remain future work.

## Timestamp anomaly

One `decelerate` candidate has an event-start year inconsistent with its associated trip telemetry year. Phase 2 correctly preserves it. Phase 3A recommends **quarantine** from all label simulations because inclusion could assign a false event far outside the trip, while rewriting or guessing the time would destroy provenance. Organizer clarification is `UNRESOLVED`.

## Event identity

Phase 2 reconstructed 86,789 identity groups. Aggregate classifications are:

- 62,406 singleton groups;
- 1,595 enrichment/completion groups;
- 22,788 completion groups with evolving payload;
- zero core candidates classified as conflicting or start-ambiguous after the timestamp quarantine.

Phase 2 `events_onsets` is sufficient as the candidate source for Phase 3B, provided Phase 3B retains its quality/conflict columns and the Phase 3A quarantine. Payload changes must not create additional onset labels. Speeding and engine-overloading completion payloads must not be misread as distinct episodes.

## Active-event policy

Relative to strict onset-only, observed active-state exclusion removed 6 reference anchors and changed positive anchors from 53,817 to 53,812. A 60-second post-onset cooldown removed 55,930 anchors and reduced positive prevalence to 19.23%.

Recommendation: use onset plus active-state exclusion for the candidate label contract. It implements the semantic rule that an event already active before `t` is not a future new event. Cooldown is an alert-policy experiment, not part of label truth, unless domain owners explicitly define refractory episodes.

The very small active-state effect for core events reflects limited end/active observations, not proof that persistence is absent.

## Anchor cadence and duplicates

| Cadence | Eligible anchors | Positive rate |
|---|---:|---:|
| Every unique telemetry timestamp | 368,319 | 26.89% |
| 5 seconds | 325,258 | 25.69% |
| 10 seconds | 209,154 | 25.73% |
| 15 seconds | 164,330 | 25.24% |
| 30 seconds | 104,500 | 24.55% |

Ten seconds is the recommended research cadence: it limits nearly identical windows while preserving substantially more anchors and timing resolution than 30 seconds. It is not evidence that adjacent anchors are independent; grouped splits and alert-level evaluation remain mandatory.

Seventeen extra rows share trip/timestamp keys. Canonical anchor policy is one anchor per unique timestamp, while source rows and duplicate flags remain intact in Phase 2. Event ordering at equal time uses the label interval boundary and cannot be invented from source-row order. No duplicate is silently deleted.

## History coverage

Across 493,727 unique timestamp anchors:

- elapsed history median 290 seconds; p25 269 seconds;
- telemetry-point count median 22; p25 14;
- maximum gap median 60 seconds; p90 62 seconds; p95 153 seconds;
- sampling density median 4.4 points/minute.

Pass counts are 49,103 for strict 300 seconds, 449,801 for minimum 240 seconds, 421,081 for minimum 10 points, and 399,013 for the combined 240-second/10-point/60-second-gap rule.

The combined rule is recommended for the label research contract because strict 300-second elapsed coverage rejects most irregularly sampled anchors. No interpolation is authorized. Its thresholds require human approval before dataset generation.

## Horizon completeness

The reference scenario requires the trip to extend through `t+60s` and at least one telemetry observation in `(t,t+60s]`. It produced:

- 9,276 censored anchors because the trip ended before the full horizon;
- 99 ambiguous anchors because no future telemetry was observed inside the otherwise bounded horizon;
- neither group was labeled negative.

A stricter maximum-future-gap rule of 30 seconds marks 69,468 anchors ambiguous and changes prevalence to 30.84%, showing meaningful sensitivity to observation completeness. The reference one-observation rule is a pragmatic candidate; the final maximum-gap policy is `UNRESOLVED`.

## Quality policy

| Observed issue | Aggregate count | Candidate treatment |
|---|---:|---|
| GPS speed out of range | 2 rows | Keep trip; flag the cell and exclude dependent feature value |
| Azimuth out of range | 61 rows | Keep + flag; do not silently wrap |
| Missing GPS | 37 rows | Keep + missingness flag; permit reduced feature path |
| Duplicate timestamp extras | 17 rows | One anchor per timestamp; preserve/audit rows |
| Invalid event duration | 0 observations | If seen, exclude duration-based candidate only |
| Invalid/zero-duration trips | 2 trips | Exclude trip from anchors |
| Distance unavailable | 2 trips | Keep label anchors; normalized distance rate unavailable |
| Longer than 12 hours | 5 trips | Exclude pending journey-boundary review |
| Event start year mismatch | 1 candidate | Quarantine from labels |

Distance is not needed to define a new onset and therefore must not determine label truth. GPS/CAN feature policies belong to Phase 3B and must respect past-only availability.

## Candidate label comparison

### V0 — Core Any Risk (human-approved for Phase 3B-1)

`label_any_core_risk_60s(t)=1` when at least one trusted new onset of `decelerate`, `PCS`, `LDWS`, `fatigue`, or `distract` occurs in `(t,t+60s]`. The user explicitly approved this contract on 2026-08-17 for Phase 3B-1 dataset generation. This approval does not validate event semantics clinically, approve alert cooldown, or authorize a standalone PCS claim.

### V1 — Core Multi-label

Five class indicators use the same horizon and also derive `any_risk`. V1 preserves actionability but exposes severe class imbalance. It should be retained for reporting/experiments, while unsupported class-specific claims are prohibited.

### V2 — Context-expanded (experimental)

Adds speeding only after an operational sustained-speeding threshold and threshold-crossing time are approved. Current sensitivity simulation uses any speeding onset solely to show impact: 174,498 eligible anchors, 62,861 positives, and 36.02% prevalence. It is not an approved label.

## Reference prevalence and class imbalance

V0 reference simulation has 209,154 eligible anchors: 53,812 positive, 155,342 negative, 25.73% prevalence, and a 2.89:1 negative/positive ratio.

| Event | Positive anchors | Rate | Negative/positive | Standalone assessment |
|---|---:|---:|---:|---|
| decelerate | 36,025 | 17.22% | 4.81 | Candidate |
| PCS | 119 | 0.057% | 1,756.60 | Insufficient under research heuristic |
| LDWS | 8,802 | 4.21% | 22.76 | Candidate |
| fatigue | 1,454 | 0.70% | 142.85 | Candidate with high uncertainty |
| distract | 10,054 | 4.81% | 19.80 | Candidate |

Positive anchors overlap when several event types occur in one horizon and must not be presented as independent onset counts. PCS cannot support a standalone performance claim with the proposed temporal test period.

## Vehicle concentration

All 20 vehicles contribute V0 positives. Anonymous concentration is top-1 12.35%, top-3 30.81%, top-5 47.18%, and HHI 0.0749. Per-event concentration is materially higher for fatigue (top-1 53.23%, HHI 0.3312) and distract (top-3 84.87%, HHI 0.2726). This motivates grouped evaluation and uncertainty by vehicle. Outputs contain anonymous ranks only.

## Temporal and timezone findings

The reference positive rate is 26.30% for January–June and 25.19% for July–November. Data are observed in six alternating months, so calendar gaps and fleet composition can confound a simple time trend.

All 3,541 journey codes are parseable as timestamps. The empirical floor-hour difference between telemetry and journey code is +8 hours for 3,514 trips (99.24%). This strongly suggests a UTC/+08 relationship but is not official confirmation. Hour-of-day and weekday aggregates are therefore marked experimental; no timezone conversion is applied.

## Evaluation split research

Recommended temporal candidate, using whole trips:

- train: trip start through 2025-08-31 (effective observed months January–July), 2,389 trips;
- validation: 2025-09-01 through 2025-10-31 (effective September), 663 trips;
- test: from 2025-11-01, 489 trips.

The temporal test period has only two PCS onsets, so PCS must not receive a standalone test claim. Exact assignments are not created in Phase 3A.

With only 20 vehicles, use a small, sealed fixed unseen-vehicle test group for the final external-style claim and grouped cross-validation within development vehicles. Leave-one/few-vehicle-out analysis is a sensitivity check. Vehicle IDs must be assigned once after labels are frozen and never adjusted after test outcomes are inspected.

## Leakage threat model

The following are forbidden in a real-time feature pipeline:

- the event onset at `t` or any raw event slot on the anchor row;
- event end time, final duration, completion payload, or any observation after `t`;
- future speed, fuel, state, location, or trip outcome;
- Phase 2.5 trip summary, weakness/goal output, or `post_trip_analytics` event context;
- preprocessing, calibration, threshold, or sampling state fitted outside the training partition;
- direct private identifiers used as model inputs.

Windows from one trip never cross partitions. Vehicle evaluation groups whole vehicles. Overlapping anchors remain correlated even inside a partition; split first by whole trip/vehicle, apply purge where a boundary could share observations, and estimate uncertainty at trip or vehicle level.

## Phase 3B-1 realization

The approved contract was materialized without model training:

- 209,149 final anchors after 5 split-boundary embargo removals;
- 53,812 positive and 155,337 negative anchors (25.7290% positive);
- 163 past-only model features, with private metadata physically separated;
- train/validation/test rows: 145,177 / 36,789 / 27,183;
- leakage, privacy, target-leakage screening, and semantic deterministic rebuild: `PASS`.

The 5-anchor difference from the Phase 3A reference count is entirely the approved split-boundary purge. Diagnostic labels are retained but do not imply sufficient standalone support. In particular, the test split has only 3 PCS-positive anchors.

## Approved contract and remaining gates

Label V0 is frozen for `v0_hino` with:

1. `[t-300s,t)` input and `(t,t+60s]` label boundaries.
2. Core types 7/19/20/12/16, with type 7 still named `decelerate`.
3. Unique existing telemetry anchors at 10-second cadence.
4. Combined 240-second, 10-point, 60-second maximum-history-gap rule.
5. Full horizon plus at least one future observation; censor/mark ambiguous rather than negative.
6. Phase 2 onset candidates plus anomaly quarantine and active-state exclusion.
7. V1 retained for class diagnostics; V2 excluded until speeding semantics are approved.

Remaining `UNRESOLVED` decisions:

- whether event 7 may ever be equated to or presented as hard braking; it remains `decelerate`;
- official timezone;
- history/future maximum-gap thresholds and longer-than-12-hour trip treatment;
- active/cooldown episode semantics and final alert cooldown;
- sustained-speeding duration, excess margin, threshold-crossing time, and legal/domain provenance;
- final unseen-vehicle reporting protocol and minimum per-class test support; the current private holdout is a deterministic proposal;
- label/alert matching, driving-hour denominator, alert budget, and minimum useful lead time;
- compound-risk definition and whether phone/other classes join a future target.

Phase 3C-2 does not change Label V0. Cooldown, escalation, hysteresis, alert matching, and speech suppression are downstream Alert Policy V1 behavior only. They never rewrite onset truth or turn persistent/cooldown observations into new positives.
