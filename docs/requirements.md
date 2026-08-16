# HINO SafePilot Requirements

## Product scope

SafePilot is a driver-centered commercial-vehicle safety coach. The intended product loop is:

1. Before driving: analyze the most recent 7-30 days or N trips and select one deterministic `daily_improvement_goal` from observed weaknesses.
2. During driving: use approximately five minutes of telemetry to estimate whether a new high-risk event will begin in the next 60 seconds.
3. After driving: summarize the trip, compare it with a personal baseline, explain weaknesses, and propose the next improvement goal.

This repository phase covers only repository foundation, private-data audit, requirements, architecture, and ML system design. It does not implement the application or train a model.

## MVP

- An immutable, reproducible HINO-only data pipeline with schema validation and journey-level data contracts.
- A reviewed candidate label specification for new-event prediction; no label becomes final without human approval.
- Leakage-safe temporal and unseen-vehicle evaluation tracks.
- Logistic Regression and gradient-boosted tree baselines before temporal deep learning.
- A hardware-agnostic offline training interface and a portable CPU-capable inference artifact.
- A deterministic alert engine that turns calibrated model risk into rate-limited warnings.
- Deterministic pre-trip weakness selection and post-trip metrics based only on verified telemetry definitions.

## Bonus / later enhancements

- TCN and GRU experiments; a small Temporal Transformer only if simpler models justify it.
- An ensemble only when out-of-sample evidence shows a meaningful product-level gain.
- Historical TDX/CWA/NCDR features when timestamp-and-location alignment and historical availability are proven.
- Gemini-generated coaching/report wording that consumes deterministic facts but never decides safety logic.
- Expo mobile experience, CSV/PDF exports, richer fuel-cost scenarios, and external-context explanations.

## Out of scope for this phase

- Expo initialization, UI code, maps, and mobile dependencies.
- Gemini, TDX, CWA, NCDR, or any live external API integration.
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

### During driving

- FR-R01: Build features from a past-only window, nominally `[t-300s, t)`.
- FR-R02: Predict one or more candidate new-event onsets in `(t, t+60s]`.
- FR-R03: Support calibrated risk output, configurable thresholds, cooldown, deduplication, hysteresis, and a maximum alert rate.
- FR-R04: Continue HINO-only inference if any optional external provider is unavailable.
- FR-R05: Never require a training workstation GPU for demo or production inference.

### After driving

- FR-T01: Produce deterministic trip duration, distance, event, speed, engine, idle, and fuel summaries only where definitions and data quality permit.
- FR-T02: Compare a trip with an appropriate personal or vehicle baseline without cross-split or future leakage.
- FR-T03: Keep score and weakness calculations versioned and auditable.
- FR-T04: Treat AI narrative, PDF, and CSV export as later integrations over a deterministic report payload.

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

