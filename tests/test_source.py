from __future__ import annotations

from pathlib import Path

import pytest

from hino_safepilot.etl.source import SourceValidationError, sha256_file, validate_source

from conftest import write_synthetic_workbook


def test_hash_guard_and_schema_validation(synthetic_workbook: tuple[Path, int]) -> None:
    path, row_count = synthetic_workbook
    digest = sha256_file(path)
    metadata = validate_source(
        path,
        expected_sha256=digest,
        expected_rows=row_count,
        expected_source_dimension=f"A1:BC{row_count + 1}",
    )
    assert metadata.sha256 == digest
    with pytest.raises(SourceValidationError, match="SHA-256 mismatch"):
        validate_source(
            path,
            expected_sha256="0" * 64,
            expected_rows=row_count,
            expected_source_dimension=f"A1:BC{row_count + 1}",
        )


def test_schema_drift_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "drift.xlsx"
    write_synthetic_workbook(path)
    from openpyxl import load_workbook

    workbook = load_workbook(path)
    workbook["output"]["A1"] = "UnexpectedType"
    workbook.save(path)
    workbook.close()
    with pytest.raises(SourceValidationError, match="schema drift"):
        validate_source(
            path,
            expected_sha256=sha256_file(path),
            expected_rows=12,
            expected_source_dimension="A1:BC13",
        )

