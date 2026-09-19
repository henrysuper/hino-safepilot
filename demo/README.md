# SafePilot Local Demo

This directory is a framework-free localhost results viewer built with HTML, CSS, and vanilla JavaScript. Phase 3C-2 has produced an offline XGBoost experiment and Alert Policy V1 simulation, so the driving screen may show aggregate known-holdout metrics. All bundled per-trip contracts, risk values, and timeline points remain manually created synthetic examples; the page performs no inference and is not connected to live telemetry.

## Start on Windows

From the repository root:

```powershell
python -m http.server 8000 --bind 127.0.0.1 --directory demo
```

Open `http://127.0.0.1:8000`.

Opening `index.html` directly is not supported because browsers normally block `fetch()` for local JSON files.

## Data boundary

`StaticJsonProvider` is the only current loading adapter. A future `ApiProvider` can replace it without rewriting the view renderers. Repository samples must remain synthetic and contain no competition telemetry, precise location, source provenance, or private identifiers. Real derived demo material, if separately approved later, belongs only in the ignored processed-data area.
