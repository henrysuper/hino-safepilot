# HINO SafePilot Requirements

## Phase 3C-4 context-aware voice qualification

- The frozen XGBoost model and unseen-vehicle behavior shall not be modified.
- External context may qualify only L2 voice; rejection shall retain L1 visual intervention and L3 shall pass through.
- Historical joins shall reject future source or availability timestamps and record age/distance provenance.
- Missing/stale/error behavior shall be configurable and auditable; policy-only fallback is the default.
- Mock context shall be tagged and excluded from official optimization and metrics.
- Gate selection shall use development data only. Without admissible historical context, optimization and ablation claims shall be unavailable rather than simulated.

## Product scope

SafePilot is a driver-centered commercial-vehicle safety coach. The intended product loop is:

1. Before driving: analyze the most recent 7-30 days or N trips and select one deterministic `daily_improvement_goal` from observed weaknesses.
2. During driving: use approximately five minutes of telemetry to estimate whether a new high-risk event will begin in the next 60 seconds.
3. After driving: summarize the trip, compare it with a personal baseline, explain weaknesses, and propose the next improvement goal.

Repository foundation and audit were completed in Phase 1, and reproducible XLSX-to-Parquet ETL V0 was completed in Phase 2. Phase 2.5 implements deterministic trip analytics, past-only vehicle-linked behavior baselines, improvement goals, and an optional privacy-safe report-wording prototype. It does not generate ML windows or labels, split data, implement Expo, or train a model.

## MVP

- An immutable, reproducible HINO-only data pipeline with schema validation and journey-level data contracts.
- A reviewed candidate label specification for new-event prediction; no label becomes final without human approval.
- Leakage-safe temporal and unseen-vehicle evaluation tracks.
- Logistic Regression and gradient-boosted tree baselines before temporal deep learning.
- A hardware-agnostic offline training interface and a portable CPU-capable inference artifact.
- A deterministic alert engine that turns calibrated model risk into rate-limited warnings.
- Deterministic pre-trip weakness selection and post-trip metrics based only on verified telemetry definitions.
- Typed `TripSummary`, `BehaviorBaseline`, `DailyImprovementGoal`, and structured AI presentation contracts.
- Mock-first report wording with deterministic fallback; Gemini is optional and receives only sanitized aggregates.

## Bonus / later enhancements

- TCN and GRU experiments; a small Temporal Transformer only if simpler models justify it.
- An ensemble only when out-of-sample evidence shows a meaningful product-level gain.
- Historical TDX/CWA/NCDR features when timestamp-and-location alignment and historical availability are proven.
- Gemini-generated coaching/report wording that consumes deterministic facts but never decides safety logic.
- Expo mobile experience, CSV/PDF exports, richer fuel-cost scenarios, and external-context explanations.

## Out of scope for this phase

- Expo initialization, UI code, maps, and mobile dependencies.
- Live calls using real HINO-derived payloads; TDX, CWA, NCDR, and other external context.
- Production backend endpoints.
- Model training, deep-learning experiments, hyperparameter search, and model binary generation.
- Claims about TPMS, accident cost, avoided accidents, or savings without supporting data and reviewed assumptions.
- Git remotes, commits, pushes, or publication of competition data.

## Functional requirements

### Data and journeys

- FR-D01: Read raw Excel and reference PDFs without modifying, moving, or rewriting them.
- FR-D02: Identify one journey by the organizer-confirmed composite key `(enabledCode, journeyCode)`.
- FR-D03: Normalize source fields with explicit parsing, validation, provenance, and quarantine reasons.
- FR-D04: Preserve missingness indicators and distinguish unavailable, invalid, and structurally inapplicable fields.
- FR-D05: Produce only ignored derived data under `data/interim/` and `data/processed/`.

### Before driving

- FR-P01: Aggregate reviewed behavior metrics over a configurable 7-30 day or N-trip lookback.
- FR-P02: Compare with the driver's own prior baseline where driver identity is legitimately available; driver identity is currently `UNRESOLVED` in the supplied schema.
- FR-P03: Select one improvement goal using deterministic rules and evidence, with no LLM dependency.
- FR-P04: Label the current profile as a `vehicle-linked behavior profile`; never treat `enabledCode` as a driver identity.

### During driving

- FR-R01: Build features from a past-only window, nominally `[t-300s, t)`.
- FR-R02: Predict one or more candidate new-event onsets in `(t, t+60s]`.
- FR-R03: Support calibrated risk output, configurable thresholds, cooldown, deduplication, hysteresis, and a maximum alert rate.
- FR-R04: Continue HINO-only inference if any optional external provider is unavailable.
- FR-R05: Never require a training workstation GPU for demo or production inference.
- FR-R06: Separate target episodes receiving any L1/L2/L3 intervention from those receiving L2/L3 voice; high voice coverage is not an objective.
- FR-R07: Isolate smoothing, persistence, hysteresis, cooldown, episode, and rolling-budget state by the organizer-confirmed `(enabledCode, journeyCode)` key.
- FR-R08: Gate normal predictive voice during organizer-defined idling without blindly silencing explicit direct-critical or DMS semantics.
- FR-R09: Accept optional DMS arbitration state and suppress semantically overlapping SafePilot voice while retaining visual state and an auditable `suppressed_by_dms` reason.
- FR-R10: Apply one global Alert Policy v2 configuration to overall, vehicle, existing seen/unseen-assignment, and HINO Type 300/500/700 slices.

### After driving

- FR-T01: Produce deterministic trip duration, distance, event, speed, engine, idle, and fuel summaries only where definitions and data quality permit.
- FR-T02: Compare a trip with an appropriate personal or vehicle baseline without cross-split or future leakage.
- FR-T03: Keep score and weakness calculations versioned and auditable.
- FR-T04: Treat AI narrative, PDF, and CSV export as later integrations over a deterministic report payload.
- FR-T05: Compute cumulative mileage/fuel through guarded positive segments; flag resets, decreases, missingness, and rejected jumps.
- FR-T06: Build event context only in the post-trip analytics boundary, where future 30-second observations are explicitly permitted.
- FR-T07: Reject LLM payloads containing raw identifiers, source-row fields, or precise coordinates before any provider call.
- FR-T08: Validate AI wording against versioned Pydantic schemas and fall back to deterministic rendering after bounded provider failures.

## Non-functional requirements

- NFR-01 Confidentiality: no telemetry rows, identifiers, raw/reference files, secrets, or model/data artifacts enter Git.
- NFR-02 Immutability: source files must retain their byte hashes before and after every audit/ETL run.
- NFR-03 Reproducibility: configs, schemas, split manifests, feature/label versions, seeds, and dependency versions must be recorded.
- NFR-04 Leakage safety: splits are journey/vehicle/time aware; preprocessing is fitted only on training data; overlapping windows are purged.
- NFR-05 Safety: LLM output is never part of labels, scores, thresholds, alert decisions, or failsafe behavior.
- NFR-06 Portability: device, batch size, workers, precision, and artifact format are configurable; CPU is the required fallback and CUDA is optional.
- NFR-07 Observability: pipeline rejection counts, feature freshness, missingness, inference latency, risk, alert decisions, and model version are measurable without logging private raw rows.
- NFR-08 Graceful degradation: missing CAN, GPS, context, model, or network signals result in explicit reduced-capability states rather than fabricated values.
- NFR-09 Testability: schema, journey assembly, cutoff semantics, window purging, metrics, calibration, and alert state transitions require automated tests before implementation is complete.
- NFR-10 Documentation fidelity: documents must distinguish organizer-confirmed facts, observed workbook behavior, design proposals, and `UNRESOLVED` decisions.
- NFR-11 Performance targets: online latency, memory, alert budget, and availability SLOs are `UNRESOLVED` until demo hardware and product constraints are confirmed.
- NFR-12 Real-data egress: no real competition-derived payload may be sent to Gemini until a human approves a separate API policy; synthetic opt-in testing only.
- NFR-13 Holdout honesty: November results after Phase 3C-1 must be labeled known-holdout comparative evidence; no later model may present them as an unbiased final test.
- NFR-14 Alert separation: prediction probability and speech decisions use separate typed contracts; cooldown, escalation, and hysteresis cannot modify labels or model probabilities.
- NFR-15 Mock isolation: every synthetic/mock policy record is tagged `is_mock=true` and excluded from official empirical optimization and HINO performance metrics.
- NFR-16 Policy selection integrity: search uses development data only, applies available feasibility constraints before lexicographic ranking, and returns no winner when none is feasible.
- NFR-17 Identity fidelity: `enabledCode` is a vehicle identifier, not a driver identity; true driver personalization must not be claimed while driver rotation is unavailable.
- NFR-18 Voice auditability: retain raw and smoothed risk, slope, requested/emitted level, modality, episode, idling/DMS state, budget, suppression reason, and policy version for every policy decision.
