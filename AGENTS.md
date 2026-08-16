# HINO SafePilot Repository Rules

- Treat all competition telemetry and organizer reference material as confidential private data.
- Raw and reference data are immutable: never edit, move, delete, publish, or commit them.
- Keep derived datasets only under `data/interim/` or `data/processed/`; these directories remain excluded from Git.
- Never copy actual telemetry rows or identifying values into tracked documentation, tests, logs, fixtures, or examples.
- Prefer deterministic, auditable analytics before probabilistic or generative behavior.
- Safety-critical decisions, labels, thresholds, scoring, and alerts must not depend on an LLM.
- Prevent future, current-event, overlapping-window, journey, vehicle, and preprocessing leakage in all ML work.
- Keep training hardware agnostic: configurable device and batch size, CPU fallback, optional CUDA, and no hard-coded GPU model.
- Never place secrets, API keys, credentials, or private endpoints in source control.
- External providers must be optional abstractions and must not be hard dependencies of the core ETL or safety path.
- Add and run tests appropriate to the change before declaring implementation complete.
- Keep documentation synchronized with actual code, data contracts, and verified organizer definitions.
- Mark unsupported or conflicting facts as `UNRESOLVED`; do not silently infer them.
- Do not create remotes, commit, or push unless a human explicitly authorizes that separate action.
