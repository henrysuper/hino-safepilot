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

The page detects its data source automatically. When `hino-analytics export-demo` (or `scripts/build_safety.ps1`) has produced `data/processed/demo_views/`, it loads `drivers.local.json` and, for the driver chosen in the top selector, `driver-Dxx.local.json`; pre-drive, post-drive, history and the 60-second tab all follow that driver. These files are Git-ignored and served only through the `/local-data/` route of `scripts/serve_demo.py`. Without them the page shows the two bundled synthetic drivers and a red banner explaining how to build the real data. `?source=synthetic` forces the samples for public demos; `?source=local` is still accepted. Local contracts use `prototype_marker: LOCAL_PRIVATE_DERIVED` and `data_classification: private_local`, and are rejected in synthetic mode (and vice versa).

## Reminder categories

The driving tab offers five explicit Synthetic reminder scenarios: forward/PCS, lane/LDWS, attention/distraction, fatigue, and generic model risk. The hand-authored curve rises from 25% at 00:00 to 40% at 00:10 and 50% at 00:20; reminders stay silent below 50%. This gate applies only to the synthetic scenarios, not to production event detection or the frozen model policy. Select a category, load it, then play (or seek to 00:20); enable the voice checkbox to hear fixed browser speech. The scenario curve and detections are hand-authored, and are labelled separately from the selected driver's history. “Return to driver trip” restores the driver's original view.

Model decision codes 1/2 always use the generic risk message; future outcome labels never choose a category. Category-specific messages are demonstrated only by the explicitly synthetic `detected_events` contract at or after its timestamp. Braking alone does not select a category. Within a playback frame, an explicit detection supersedes generic speech and duplicate categories are combined. This is presentation arbitration for the prototype, not an approved production warning policy. Seeking is silent; pausing cancels queued speech. The risk model, labels and Alert Policy V1 are unchanged.

Playback uses elapsed time and a seconds-based slider. At 1x, the clock advances one second per real second, while risk values hold the latest available prediction until the next timestamp; irregular prediction gaps do not change playback speed or reveal future values. Pause/resume and speed changes retain the current time, and seeking is silent.

Run `node tests/demo_reminders.test.cjs` and `node tests/demo_playback.test.cjs` for routing, threshold, timing and future-information boundary checks, and `python -m pytest` for repository tests.

## Data boundary

`StaticJsonProvider` is the only loading adapter, used for both synthetic samples and local private views. A future `ApiProvider` can replace it without rewriting the view renderers. Repository samples must remain synthetic and contain no competition telemetry, precise location, source provenance, or private identifiers. Real derived demo material belongs only in the ignored processed-data area.
