# Scripts

The Phase 2 ETL is exposed as an installed module/console entry point rather than a path-specific script:

```powershell
python -m hino_safepilot.etl --repo-root . --dry-run
python -m hino_safepilot.etl --repo-root . --batch-size 10000
```

Use `--source`, `--output-root`, or `--manifest` to override repository-relative defaults. The CLI never saves the workbook, refuses unsafe output roots, and refuses to overwrite existing ETL dataset directories.

Phase 2.5 analytics commands:

```powershell
python -m hino_safepilot.analytics build --repo-root .
python -m hino_safepilot.analytics trip-report --trip-id <local-trip-id>
python -m hino_safepilot.analytics daily-goal --trip-id <local-trip-id>
```

`build` refuses to overwrite `data/processed/analytics/`. Report commands default to the mock provider. `--provider gemini` is opt-in, requires the optional SDK and environment variables, and must use synthetic payloads until real-data egress receives human approval.

Phase 3B-1 HINO-only dataset generation:

```powershell
python -m hino_safepilot.dataset --repo-root . --deterministic-rebuild
```

The command writes only under `data/processed/ml/v0/`, refuses an existing output directory, materializes separate dataset/metadata/audit/split layers, and exits non-zero unless privacy and leakage audits pass. It does not train a model or call an external context provider.

Phase 3C-1 offline Logistic baseline:

```powershell
python -m hino_safepilot.modeling --repo-root .
```

The trainer requires Dataset V0 `READY` plus leakage/privacy `PASS`, uses only Dummy and Logistic Regression, and refuses to overwrite an existing artifact/report root. It writes ignored artifacts under `models/risk/v0_logistic/` and detailed reports under `reports/generated/phase3c1/`; only the sanitized model card is tracked. The command performs a one-way November Test evaluation after a frozen configuration manifest exists.

Install runtime dependencies with `pip install -e .`, development/test dependencies with `pip install -e ".[dev]"`, and the optional official Gemini SDK with `pip install -e ".[ai]"`.
