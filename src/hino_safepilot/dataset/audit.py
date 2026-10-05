"""Post-build feature, target-leakage, schema, and privacy audits."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


FORBIDDEN_MODEL_COLUMN_TOKENS = (
    "enabled_code",
    "vehicle_id",
    "license_plate",
    "carnum",
    "trip_id",
    "journey_code",
    "source_row",
    "latitude",
    "longitude",
    "coordinate",
    "event_end",
    "final_duration",
    "final_trip",
    "post_trip",
)


def validate_model_schema(feature_columns: list[str]) -> dict[str, Any]:
    rejected = sorted(
        column
        for column in feature_columns
        if any(token in column.casefold() for token in FORBIDDEN_MODEL_COLUMN_TOKENS)
    )
    return {
        "status": "PASS" if not rejected else "FAIL",
        "checked_feature_count": len(feature_columns),
        "forbidden_columns": rejected,
        "anchor_id_is_model_feature": "anchor_id" in feature_columns,
        "coordinates_present": any(
            token in column.casefold()
            for column in feature_columns
            for token in ("latitude", "longitude", "coordinate")
        ),
    }


def _safe_number(value: Any) -> float | int | None:
    if value is None:
        return None
    result = value.as_py() if hasattr(value, "as_py") else value
    if isinstance(result, float) and not math.isfinite(result):
        return None
    return result


def _column_stats(array: pa.ChunkedArray) -> dict[str, Any]:
    total = len(array)
    valid = total - array.null_count
    if not valid:
        return {
            "row_count": total,
            "valid_count": 0,
            "missing_count": total,
            "missing_rate": 1.0 if total else None,
            "valid_rate": 0.0 if total else None,
            "mean": None,
            "std": None,
            "min": None,
            "max": None,
            "distinct_count": 0,
            "dominant_fraction": None,
        }
    values = pc.drop_null(array)
    counts = pc.value_counts(values)
    dominant = max(counts.field("counts").to_pylist()) / valid
    return {
        "row_count": total,
        "valid_count": valid,
        "missing_count": array.null_count,
        "missing_rate": array.null_count / total if total else None,
        "valid_rate": valid / total if total else None,
        "mean": _safe_number(pc.mean(values)),
        "std": _safe_number(pc.stddev(values, ddof=0)),
        "min": _safe_number(pc.min(values)),
        "max": _safe_number(pc.max(values)),
        "distinct_count": int(pc.count_distinct(values).as_py()),
        "dominant_fraction": dominant,
    }


def _point_biserial(values: list[Any], labels: list[int]) -> tuple[float | None, int, bool]:
    pairs = [(float(value), int(label)) for value, label in zip(values, labels) if value is not None]
    if len(pairs) < 2:
        return None, len(pairs), False
    n = len(pairs)
    sx = sum(value for value, _ in pairs)
    sy = sum(label for _, label in pairs)
    sxx = sum(value * value for value, _ in pairs)
    syy = sum(label * label for _, label in pairs)
    sxy = sum(value * label for value, label in pairs)
    denominator = math.sqrt(max(0.0, (n * sxx - sx * sx) * (n * syy - sy * sy)))
    correlation = (n * sxy - sx * sy) / denominator if denominator else None
    exact = all(value in {0.0, 1.0} and value == label for value, label in pairs)
    return correlation, n, exact


def audit_feature_files(
    dataset_files: dict[str, Path], feature_columns: list[str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    parquet = {split: pq.ParquetFile(path) for split, path in dataset_files.items()}
    labels = {
        split: parquet_file.read(columns=["label_any_core_risk_60s"])[
            "label_any_core_risk_60s"
        ].to_pylist()
        for split, parquet_file in parquet.items()
    }
    availability: list[dict[str, Any]] = []
    suspicious: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []

    for feature in feature_columns:
        arrays = {
            split: parquet_file.read(columns=[feature])[feature]
            for split, parquet_file in parquet.items()
        }
        split_stats = {split: _column_stats(array) for split, array in arrays.items()}
        combined = pa.chunked_array(
            [chunk for split in ("train", "validation", "test") for chunk in arrays[split].chunks]
        )
        overall = _column_stats(combined)
        train_mean, train_std = split_stats["train"]["mean"], split_stats["train"]["std"]
        drift = {}
        for split in ("validation", "test"):
            other = split_stats[split]["mean"]
            drift[split] = (
                (other - train_mean) / train_std
                if other is not None and train_mean is not None and train_std not in {None, 0}
                else None
            )

        values = []
        target = []
        for split in ("train", "validation", "test"):
            values.extend(arrays[split].to_pylist())
            target.extend(labels[split])
        correlation, compared, exact = _point_biserial(values, target)
        record = {
            "feature": feature,
            **overall,
            "by_split": split_stats,
            "standardized_mean_drift_vs_train": drift,
            "label_point_biserial_correlation": correlation,
            "label_pairs_compared": compared,
        }
        availability.append(record)

        reasons = []
        if overall["distinct_count"] <= 1:
            reasons.append("CONSTANT")
        if overall["missing_rate"] is not None and overall["missing_rate"] >= 0.95:
            reasons.append("OVERWHELMINGLY_MISSING")
        if overall["dominant_fraction"] is not None and overall["dominant_fraction"] >= 0.995:
            reasons.append("NEAR_CONSTANT")
        if reasons:
            recommendations.append({"feature": feature, "reason_codes": reasons})

        if exact or (correlation is not None and abs(correlation) >= 0.98 and compared >= 1000):
            suspicious.append(
                {
                    "feature": feature,
                    "severity": "HIGH",
                    "exact_binary_match": exact,
                    "correlation": correlation,
                    "pairs_compared": compared,
                }
            )

    return (
        {"feature_count": len(feature_columns), "features": availability},
        {
            "status": "FAIL" if suspicious else "PASS",
            "threshold": "absolute point-biserial correlation >= 0.98 with >=1000 pairs or exact binary match",
            "suspicious_features": suspicious,
        },
        {
            "policy": "recommend only; do not target-select or remove features in Phase 3B-1",
            "flagged_feature_count": len(recommendations),
            "recommendations": recommendations,
        },
    )

