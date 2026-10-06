# Fuel console

Local inspection UI for the fuel prediction and dispatch system. Served by `hino-fuel serve` (http://127.0.0.1:8765). Handoff guide (tabs, API, extension points): `docs/fuel_console_guide.md`; model inputs/outputs/training explained: `docs/fuel_model_explained.md`; algorithms and experiments: `docs/fuel_system.md`.

Plain HTML/CSS/JS, no build step. Leaflet 1.9.4 is loaded from unpkg with SRI. The server binds to 127.0.0.1 only and has no authentication, so do not expose it on a network.

Dashboard data is bundled in dashboard_data.json with explicit user authorization for this merge. Regeneration is UNRESOLVED: scripts/export_dashboard.py was not supplied.
