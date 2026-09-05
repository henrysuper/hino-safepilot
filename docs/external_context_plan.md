# External Historical Context Plan

## Phase 3C-4 feasibility update (2026-09-05)

Official capability was rechecked, but empirical joining remains unavailable. The local workspace has no admissible historical TDX/CWA/NCDR snapshot or credentials, and HINO timestamps still have unresolved timezone semantics. GPS coverage alone is insufficient to establish safe date/time/geography overlap. No mock context was used to claim improvement.

The repository now has leakage-safe temporal/spatial joins, injected TDX/CWA parsers/providers, explicit availability/freshness states, content-addressed cache support, and an L2-only voice qualification interface. Empirical rule selection and learned-gate training remain pending. See `docs/context_design.md` and ADR 0008.

## Scope

Phase 3B-1 defines contracts only. It performs zero TDX, CWA, NCDR, Google Routes, or other external API calls. HINO-only `v0_hino` remains independently buildable and deployable.

The minimum admission rule for an external training feature is: the source must provide historical data covering the telemetry's 2025 timestamps and locations, with documented observation time, publication/availability time, spatial reference, quality, licensing, and reproducible acquisition.

## Traffic — TDX

### Official evidence

- TDX exposes a member-only historical service supporting generated downloads or historical API access. Its catalog currently includes road-condition and road-safety subjects; non-members can inspect descriptions but cannot download historical files. [TDX historical service](https://tdx.transportdata.tw/data-service/historical)
- The official historical road-condition v2 API states that data are available after July 2021, excludes the current day, and uses 10 calls or 20 MB per platform point. This makes 2025 road-condition coverage plausible. [TDX historical road-condition API](https://tdx.transportdata.tw/api-service/swagger/historical/4b4708cc-ab49-4037-a896-e24544d51609)
- The nationwide basic road-event API is authenticated for full member use; unauthenticated visitor access is limited and is a live/basic service rather than proof of a 2025 archive. [TDX road-event API](https://tdx.transportdata.tw/api-service/swagger/basic/60abfa19-ffe3-4eef-a4b1-0539435dfca9)

### Feasibility assessment

| Candidate | 2025 feasibility | Decision |
|---|---|---|
| VD/road-condition speed, flow, occupancy | `LIKELY_FEASIBLE` | Historical service begins in 2021; verify provider/road coverage against HINO coordinates before acquisition. |
| Road events/incidents | `UNRESOLVED` | Live nationwide service exists, but historical 2025 retention, exact fields, coordinates/road identifiers, and completeness require authenticated catalog inspection. |
| A1/A2/A30 road-safety accident records | `CONDITIONALLY_FEASIBLE` | Historical catalog lists these domains; access review, privacy/sensitivity, spatial precision, timing, and licensing must be approved. |

Historical data require membership/API credentials or generated file requests. Exact subscription limits and licensing must be captured at acquisition time because service tiers may change. No credential belongs in source control.

| Due-diligence item | Phase 3B-1 finding |
|---|---|
| 2025 coverage | Road-condition history is plausible because the official service states July 2021 onward; provider/road continuity during HINO dates is not yet sampled. |
| Spatial resolution | Detector, road section, and event feeds differ. Exact coordinates, road identifiers, direction, and coverage for selected datasets are `UNRESOLVED`. |
| Temporal resolution | Native cadence and archive sampling are dataset/provider-specific and `UNRESOLVED`; do not infer them from the API call quota. |
| Authentication/acquisition | TDX membership; generated historical download or authenticated historical API. |
| Rate/size limits | The cited road-condition history page states 10 calls or 20 MB per platform point; other products require separate review. |
| Licensing | Dataset/service-specific reuse and competition-demo rights are `UNRESOLVED` pending terms captured with the acquisition manifest. |
| Incident geometry | Usable coordinates, road/section IDs, heading/direction, update time, and 2025 retention are `UNRESOLVED`. |

## Weather — CWA

### Official evidence

- The CWA observation catalog explicitly marks hourly all-station observations `O-A0001-001` and rainfall observations `O-A0002-001` with `historyAPI`; the 10-minute synoptic dataset `O-A0003-001` is not marked historical. [CWA observation catalog](https://opendata.cwa.gov.tw/dataset/observation)
- CWA's field standard documents observation timestamps with `+08:00`, station coordinates, precipitation, wind, temperature, humidity, and related quality/special codes. [CWA observation standard](https://opendata.cwa.gov.tw/opendatadoc/Observation/O-A0002-001.pdf)
- CODiS supports historical station/climate browsing and daily/monthly/yearly reports containing hourly/daily/monthly observations. [CODiS user guide](https://codis.cwa.gov.tw/pdf_data/Instruction.pdf)

### Feasibility assessment

| Candidate | 2025 feasibility | Decision |
|---|---|---|
| Hourly station temperature/humidity/wind/visibility | `LIKELY_FEASIBLE` | Prefer historical `O-A0001-001`/CODiS, then validate station history and availability time. |
| Historical rainfall | `LIKELY_FEASIBLE` | Prefer `O-A0002-001 historyAPI` or CODiS; retain CWA special missing/trace codes explicitly. |
| Current 10-minute observation `O-A0003-001` | `LIVE_ONLY_FOR_CURRENT_CONTRACT` | Do not assume its current API can reconstruct 2025 history. |

The telemetry timezone remains unresolved, while CWA documents `+08:00`; no join may proceed until the HINO timestamp interpretation is approved.

| Due-diligence item | Phase 3B-1 finding |
|---|---|
| 2025 coverage | Historical hourly observation and rainfall are likely available, but selected-station continuity must be audited before joining. |
| Spatial resolution | Station point coordinates; station-to-vehicle relevance requires a reviewed nearest/terrain/coverage policy, not a silent global nearest station. |
| Temporal resolution | Historical hourly and rainfall products remain distinct; current 10-minute observations must not be backfilled as historical without a documented archive. |
| Authentication/acquisition | HistoryAPI/CODiS method, API key requirements, bulk-download workflow, and reproducibility must be frozen during acquisition. |
| Quality | Preserve CWA missing/trace/special codes and station changes; never coerce them to zero. |
| Licensing/rate limits | Exact selected-product terms and quotas are `UNRESOLVED` and must be recorded with source snapshots. |

## Disaster — NCDR CAP

### Official evidence

- The NCDR civil-alert platform provides a historical warning query and lets users download the queried CAP result. [NCDR historical warning query](https://alerts.ncdr.nat.gov.tw/web/alerts/history)
- The developer API page describes the API as an authenticated route to current warning lists/content. [NCDR warning API](https://alerts.ncdr.nat.gov.tw/web/developer/alerts-api)
- The platform FAQ says Atom/JSON feeds cover the latest seven days, while historical downloads are an authenticated member service. [NCDR platform FAQ](https://alerts.ncdr.nat.gov.tw/web/platform/faq)
- CAP documentation and administrative-area KML references are published for spatial interpretation. [NCDR CAP documents](https://alerts.ncdr.nat.gov.tw/web/developer/cap-docs)

### Feasibility assessment

NCDR is **not automatically LIVE_ONLY** because an official historical query/download surface exists. However, machine-scale 2025 archive access, category-specific retention, download limits, polygon/geocode completeness, update/cancel semantics, and licensing must be verified after member access. Until verified, historical CAP training is `CONDITIONALLY_FEASIBLE / UNRESOLVED`.

Potential categories include heavy rain, flood, landslide, and road/area closure alerts. CAP publication/effective/expiry times must remain separate; a warning revised after an anchor cannot be treated as known earlier.

| Due-diligence item | Phase 3B-1 finding |
|---|---|
| 2025 coverage | A historical query/download exists, but machine-scale completeness for 2025 and category retention are `UNRESOLVED`. |
| Spatial resolution | CAP may represent areas using administrative codes, polygons, or descriptive locations; exact geometry coverage per category is `UNRESOLVED`. |
| Temporal resolution | Event-driven CAP messages with publication/effective/expiry/update semantics; historical revision chains need reconstruction. |
| Authentication/acquisition | Current developer API is authenticated; historical query/download is a separate member workflow. |
| Licensing/rate limits | Bulk reuse, retention, request limits, and competition use are `UNRESOLVED`. |
| Historical classification | `CONDITIONALLY_FEASIBLE`, not assumed live-only and not admitted to training yet. |

## Context provider contracts

`src/hino_safepilot/context/` defines:

- `TrafficContextSnapshot`
- `WeatherContextSnapshot`
- `DisasterContextSnapshot`
- `TrafficContextProvider`, `WeatherContextProvider`, `DisasterContextProvider`
- `ContextAggregator`
- `NullContextProvider`
- `SyntheticContextProvider`

Every snapshot represents `available`, `missing`, `stale`, `error`, or `unsupported`, and carries context/source timestamps, freshness, quality, provider, and source dataset version. The aggregator contains provider failures and always preserves `base_hino_model_available=true`.

## Route relevance design

A future `ContextRelevanceEngine` must not classify an event as relevant solely because it lies within a Euclidean radius. It should combine:

1. spatial distance and coordinate quality;
2. current vehicle heading and event bearing;
3. mapped road/segment identity;
4. ahead versus behind;
5. planned or inferred route relationship;
6. estimated travel time to the context event;
7. publication, effective, update, and expiry time;
8. event severity and closure direction.

Google Routes may be an optional provider later, but cannot be a hard dependency and is not connected in Phase 3B-1.

## Versioning and ablation

- `v0_hino`: current HINO-only features
- `v1_hino_traffic`: HINO plus approved historical traffic
- `v2_hino_traffic_weather`: traffic plus approved historical weather
- `v3_full_context`: adds approved disaster/route relevance

Each version is a separately materialized dataset with its own source hashes, provider contract versions, join audit, missingness, leakage audit, and splits. Context additions are evaluated by ablation against the unchanged HINO-only baseline.

## Phase 3B-2 admission gates

- human approval of HINO timezone;
- documented 2025 source coverage and availability time;
- license/authentication/rate and retention review;
- deterministic offline acquisition manifest and immutable raw external snapshot area;
- spatial/temporal join precision and unmatched-rate audit;
- no live API dependency in training generation;
- route-relevance semantics for incidents/alerts;
- separate leakage tests proving revised or future context is excluded.
