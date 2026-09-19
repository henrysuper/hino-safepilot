# ADR 0001: ETL V0 Parquet Layout and Compression

- Status: Accepted
- Date: 2026-08-16
- Scope: Phase 2 ETL V0 only

## Context

The private source contains 493,744 telemetry rows and 55 source columns. ETL must be streaming, reproducible, easy to inspect, and must not create one file per vehicle or trip. Source timestamps have unresolved timezone semantics, and event start times may precede their observation substantially.

## Decision

1. Write three long-lived interim datasets: `telemetry_normalized`, `events_observed`, and conservative `events_onsets`.
2. Partition each dataset by `year_month` and write at most one `part-00000.parquet` per observed month per dataset in a run.
3. Use parsed telemetry time for telemetry/event-observation partitions. Use parsed event start time for onset partitions, falling back to telemetry observation time only when event start is unparseable.
4. Store timezone-naive Arrow `timestamp[s]` plus the exact raw string; perform no timezone conversion.
5. Use ZSTD level 3, dictionary encoding, and statistics as the default Parquet settings.
6. Define determinism semantically: identical code/config/source must yield the same logical schemas, counts, aggregate quality results, event counts, candidate identities, and conflict flags. Byte-for-byte Parquet identity is not promised.

## Rationale

- Month partitioning gives useful pruning without producing thousands of files for 3,541 trips or 20 vehicles.
- A single unpartitioned file would be simpler but less convenient for incremental inspection and future time-bounded processing.
- Per-trip/per-vehicle partitioning was rejected because it creates tiny files and couples physical layout to private identifiers.
- ZSTD was chosen over Snappy because this is an offline/interim pipeline where moderate compression CPU cost is acceptable and smaller artifacts are valuable.
- Partitioning on preserved parsed event start time exposes rather than hides timestamp anomalies. One candidate therefore appears under `year_month=1970-01`; changing it to 2025 would be an unsupported semantic correction.

## Consequences

- Actual output contains 8 telemetry files, 8 event-observation files, and 10 onset files.
- Months with very few source rows still produce a small file; this remains bounded to one file per observed month and is preferable to silently coalescing timestamps across calendar partitions.
- A future incremental pipeline will need a reviewed append/versioning strategy. ETL V0 instead refuses to overwrite an existing dataset directory.
- Phase 3 must decide how to handle implausible-but-parseable event times and payload variants; ETL V0 only preserves and flags them.
