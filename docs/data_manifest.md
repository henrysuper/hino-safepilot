# HINO SafePilot Private Data Manifest

This manifest contains file-level metadata only. It intentionally contains no telemetry row, vehicle identifier, license plate, coordinate, event instance, or PDF screenshot.

Audit date: 2026-08-16 (Asia/Taipei).

| Filename | Purpose | Confidentiality | Git policy | File size | Structure / safe row count | SHA-256 |
|---|---|---|---|---:|---|---|
| `data/raw/hino_telemetry_20260511.xlsx` | Primary HINO telemetry and organizer field mapping | Competition private data | Must remain under ignored `data/raw/`; never track, move, rewrite, commit, or publish | 115,582,132 bytes | Sheets: `output` (`A1:BC493745`; 493,744 data rows + header; 55 columns), `欄位對照表` (`B1:H82`) | `A58E9F0A372D8F0372B3B2E8D634AB411E0F28C527251679A0770B1271D6794B` |
| `data/reference/hino_field_faq.pdf` | Organizer FAQ for field semantics and journey/vehicle identity | Competition private data | Must remain under ignored `data/reference/`; never track, move, rewrite, commit, or publish | 2,146,021 bytes | 7 pages | `52BD5A4E0E4837B586AE27F12C7AFAABF88693A8A9E2BE83E9E1552982C21A29` |
| `data/reference/itraq_hackathon_manual.pdf` | Organizer iTRAQ system reference manual | Competition private data | Must remain under ignored `data/reference/`; never track, move, rewrite, commit, or publish | 1,063,102 bytes | 16 image-based pages | `0710C211E9BC0C6A07C1819048CE0AD3B09EA24B32C115E191287A2B0E6A7942` |

## Integrity policy

- Recompute all hashes before and after any audit or ETL execution and fail if a source hash changes.
- Do not use Excel save/export operations on source files, including seemingly harmless recalculation or format conversion.
- Keep derived audit outputs, Parquet files, split manifests containing private statistics, and processed datasets only under `data/interim/` or `data/processed/`; both are ignored.
- Tracked documentation may contain schema and reviewed aggregate audit findings, but never actual private rows or identifiers.

