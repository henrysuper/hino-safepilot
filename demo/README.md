# SafePilot Local Demo

This directory is a framework-free localhost results viewer built with HTML, CSS, and vanilla JavaScript. Phase 3C-2 has produced an offline XGBoost experiment and Alert Policy V1 simulation, so the driving screen may show aggregate known-holdout metrics. All bundled per-trip contracts, risk values, and timeline points remain manually created synthetic examples; the page performs no inference and is not connected to live telemetry.

## Start on Windows

From the repository root:

```powershell
python scripts/serve_demo.py
```

Open `http://127.0.0.1:8000` for the synthetic samples.

Opening `index.html` directly is not supported because browsers normally block `fetch()` for local JSON files.

## Local private mode

After `python -m hino_safepilot.analytics score`, `python -m hino_safepilot.analytics drivers` and `python -m hino_safepilot.analytics export-demo`, open `http://127.0.0.1:8000/?source=local`. The page loads `drivers.local.json` (the simulated-driver index) and, for the driver chosen in the top selector, `driver-Dxx.local.json` (next-trip briefing plus full reports for the latest scored trips). Pre-drive, post-drive and history all follow that driver. Files live in `data/processed/demo_views/` (Git-ignored) and are served through the `/local-data/` route of `scripts/serve_demo.py`; the driving view stays synthetic. If the local driver files are missing, the whole page falls back to the synthetic driver samples and says so. A banner marks the page as private local data. Local contracts use `prototype_marker: LOCAL_PRIVATE_DERIVED` and `data_classification: private_local`, and are rejected in synthetic mode (and vice versa).

## Data boundary

`StaticJsonProvider` is the only loading adapter, used for both synthetic samples and local private views. A future `ApiProvider` can replace it without rewriting the view renderers. Repository samples must remain synthetic and contain no competition telemetry, precise location, source provenance, or private identifiers. Real derived demo material belongs only in the ignored processed-data area.
