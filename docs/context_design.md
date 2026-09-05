# External Context and L2 Voice Qualification Design

## Boundary

External context does not replace or retrain the frozen HINO XGBoost model:

`HINO telemetry -> frozen risk score -> Alert Policy v2 -> emitted L2 candidate -> ContextAwareVoiceGate -> voice or L1 visual fallback`

Only L2 voice is qualifiable. A rejected candidate retains L1 visual intervention. L3 passes through, including direct critical semantics. Context is additional evidence, not causal proof.

## Contracts and leakage controls

`ContextQuery` carries decision time and optional position/road references. Traffic, weather, and disaster snapshots have `available`, `stale`, `missing`, `error`, or `unsupported` status. Every accepted join records source time, availability time, age, distance, provider, dataset version, and mock status.

An observation is eligible only when source and availability timestamps are no later than decision time, temporal/spatial tolerances pass, and naive times have an explicit timezone before UTC conversion. Tolerances live in `config/context_voice_gate_v1.json`; they are integration bounds, not optimized policy parameters. HINO timezone is unresolved, so empirical joins are currently prohibited.

## Providers

- TDX parsing covers the documented `VDLives` structure and separates live observations from static VD locations. Historical Traffic v2 requires member credentials and provides data after July 2021 excluding the current day.
- CWA parsing covers O-A0002-001 station rainfall, including 10-minute, one-hour, and three-hour accumulation. O-A0001-001 and O-A0002-001 advertise historyAPI; O-A0003-001 is current-only.
- NCDR is capability-only until reproducible historical access and adequate overlap are demonstrated.

Providers make no mandatory network call. Future download adapters must read credentials only from environment/local secrets and cache snapshots outside Git. `ContextCache` uses content-addressed filenames so HINO identifiers are not exposed in paths.

Official references:

- TDX historical Traffic v2: <https://tdx.transportdata.tw/api-service/swagger/historical/4b4708cc-ab49-4037-a896-e24544d51609>
- CWA observation catalog: <https://opendata.cwa.gov.tw/dataset/observation>
- CWA history API: <https://opendata.cwa.gov.tw/dist/opendata-history-swagger.html>
- NCDR developer API: <https://alerts.ncdr.nat.gov.tw/web/developer/alerts-api>
- NCDR history: <https://alerts.ncdr.nat.gov.tw/web/alerts/history>

## Gate and current status

The gate records candidate level, action, evidence, source availability, downgrade reason, policy/context versions, and mock status. Missing context defaults to policy-only fallback. The simple gate is implemented only for integration tests/demo behavior; it is not empirically selected. A learned gate is intentionally not trained because there are no admissible joined development rows.

The private audit found usable GPS coverage, but no cached historical context and an unresolved HINO timezone. Therefore TDX, CWA, combined, and NCDR ablations are `UNAVAILABLE`; Policy v2 is the only empirical row. No mock improvement is reported.
