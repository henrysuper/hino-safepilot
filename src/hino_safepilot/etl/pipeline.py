"""Deterministic Phase 2 XLSX-to-Parquet pipeline.

No labels, ML windows, dataset splits, future aggregation, or event-semantic
reinterpretation belong in this module.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

import openpyxl
import pyarrow as pa
import pyarrow.parquet as pq

from hino_safepilot import __version__

from .constants import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_COMPRESSION,
    DEFAULT_COMPRESSION_LEVEL,
    EXPECTED_HEADERS,
    EXPECTED_MAPPING_DIMENSION,
    EXPECTED_SOURCE_COLUMNS,
    EXPECTED_SOURCE_DIMENSION,
    EXPECTED_SOURCE_ROWS,
    EVENT_FIELD_APPLICABILITY,
    EVENT_FIELDS_WITH_END,
    EVENT_NAMES,
    SOURCE_SHEET,
)
from .parsing import (
    ParsedValue,
    canonical_trip_id,
    is_missing,
    parse_fence_ids,
    parse_numeric,
    parse_timestamp,
    raw_string,
    year_month,
)
from .schema import (
    BASE_NUMERIC_SPECS,
    EVENT_PAYLOAD_SPECS,
    EVENT_TYPE_SPEC,
    event_observation_schema,
    event_onset_schema,
    schema_snapshot,
    telemetry_schema,
)
from .source import (
    SourceMetadata,
    SourceValidationError,
    expected_hash_from_manifest,
    iter_source_rows,
    sha256_file,
    validate_source,
)

LOGGER = logging.getLogger("hino_safepilot.etl")
OUTPUT_DATASETS = ("telemetry_normalized", "events_observed", "events_onsets", "audit")


class EtlError(RuntimeError):
    """Raised when an ETL safety or accounting invariant fails."""


@dataclass(frozen=True)
class EtlConfig:
    repo_root: Path
    source_path: Path
    output_root: Path
    manifest_path: Path | None = None
    expected_sha256: str | None = None
    expected_rows: int = EXPECTED_SOURCE_ROWS
    expected_columns: int = EXPECTED_SOURCE_COLUMNS
    expected_headers: tuple[str, ...] = EXPECTED_HEADERS
    expected_source_dimension: str = EXPECTED_SOURCE_DIMENSION
    expected_mapping_dimension: str = EXPECTED_MAPPING_DIMENSION
    batch_size: int = DEFAULT_BATCH_SIZE
    compression: str = DEFAULT_COMPRESSION
    compression_level: int = DEFAULT_COMPRESSION_LEVEL
    dry_run: bool = False
    enforce_output_policy: bool = True
    progress_every_rows: int = 50_000

    def __post_init__(self) -> None:
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if self.progress_every_rows <= 0:
            raise ValueError("progress_every_rows must be positive")


@dataclass(frozen=True)
class EtlResult:
    source_hash_before: str
    source_hash_after: str
    input_rows: int
    telemetry_rows: int
    event_observations: int
    event_onset_candidates: int
    malformed_cells: int
    quarantined_rows: int
    duplicate_group_count: int
    elapsed_seconds: float
    peak_rss_bytes: int | None
    output_paths: dict[str, Path] = field(default_factory=dict)


def configure_logging(level: int = logging.INFO) -> None:
    if LOGGER.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(level)
    LOGGER.propagate = False


def _log(event: str, **fields: Any) -> None:
    LOGGER.info(json.dumps({"event": event, **fields}, sort_keys=True, default=str))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _source_row_hash(row: tuple[Any, ...]) -> str:
    typed_cells = [[type(value).__name__, raw_string(value)] for value in row]
    return sha256(_canonical_json(typed_cells).encode("utf-8")).hexdigest().upper()


def _source_key_hash(row: tuple[Any, ...], indexes: dict[str, int]) -> str:
    values = [
        raw_string(row[indexes["enabledCode"]]),
        raw_string(row[indexes["journeyCode"]]),
        raw_string(row[indexes["time"]]),
    ]
    return sha256(_canonical_json(values).encode("utf-8")).hexdigest().upper()


def _identity_value(value: Any) -> str | None:
    return None if is_missing(value) else raw_string(value)


def _quality_issue(quality: str) -> bool:
    return quality in {"malformed", "out_of_range", "missing_unknown"}


class _PeakMemory:
    def __init__(self) -> None:
        self.peak: int | None = None
        try:
            import psutil  # type: ignore

            self._process = psutil.Process(os.getpid())
        except ImportError:
            self._process = None

    def sample(self) -> None:
        if self._process is None:
            return
        rss = int(self._process.memory_info().rss)
        self.peak = rss if self.peak is None else max(self.peak, rss)


class _DuplicateIndex:
    """Disk-backed source-key counts; only actual duplicate groups enter memory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute(
            """
            CREATE TABLE source_keys (
                key_hash TEXT PRIMARY KEY,
                group_size INTEGER NOT NULL,
                first_row_hash TEXT NOT NULL,
                all_identical INTEGER NOT NULL
            ) WITHOUT ROWID
            """
        )

    def add(self, key_hash: str, row_hash: str) -> None:
        self.connection.execute(
            """
            INSERT INTO source_keys(key_hash, group_size, first_row_hash, all_identical)
            VALUES (?, 1, ?, 1)
            ON CONFLICT(key_hash) DO UPDATE SET
                group_size = group_size + 1,
                all_identical = CASE
                    WHEN first_row_hash = excluded.first_row_hash AND all_identical = 1 THEN 1
                    ELSE 0
                END
            """,
            (key_hash, row_hash),
        )

    def commit(self) -> None:
        self.connection.commit()

    def duplicates(self) -> dict[str, tuple[int, bool]]:
        cursor = self.connection.execute(
            "SELECT key_hash, group_size, all_identical FROM source_keys WHERE group_size > 1"
        )
        return {key: (int(size), bool(identical)) for key, size, identical in cursor}

    def close(self) -> None:
        self.connection.close()


class _PartitionedWriter:
    """One Parquet file per year_month partition, appended in deterministic row order."""

    def __init__(
        self,
        root: Path,
        schema: pa.Schema,
        *,
        compression: str,
        compression_level: int,
    ) -> None:
        self.root = root
        self.schema = schema
        self.compression = compression
        self.compression_level = compression_level
        self.writers: dict[str, pq.ParquetWriter] = {}

    def _writer(self, partition: str) -> pq.ParquetWriter:
        if not re.fullmatch(r"(?:\d{4}-\d{2}|UNPARSED)", partition):
            raise EtlError(f"Unsafe year_month partition value: {partition!r}")
        writer = self.writers.get(partition)
        if writer is not None:
            return writer
        directory = self.root / f"year_month={partition}"
        directory.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "compression": self.compression,
            "use_dictionary": True,
            "write_statistics": True,
        }
        if self.compression == "zstd":
            kwargs["compression_level"] = self.compression_level
        writer = pq.ParquetWriter(directory / "part-00000.parquet", self.schema, **kwargs)
        self.writers[partition] = writer
        return writer

    def write(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        by_partition: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            by_partition[record["year_month"]].append(record)
        for partition in sorted(by_partition):
            table = pa.Table.from_pylist(by_partition[partition], schema=self.schema)
            self._writer(partition).write_table(table)

    def close(self) -> None:
        for partition in sorted(self.writers):
            self.writers[partition].close()
        self.writers.clear()


class _OnsetAccumulator:
    def __init__(self) -> None:
        self._groups: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _candidate_identity(event: dict[str, Any]) -> tuple[list[Any], bool]:
        valid = all(
            [
                event.get("enabled_code"),
                event.get("journey_code"),
                event.get("event_type") is not None,
                event.get("event_start_time_quality") == "valid",
                event.get("event_start_time") is not None,
            ]
        )
        if valid:
            return (
                [
                    event["enabled_code"],
                    event["journey_code"],
                    event["event_type"],
                    event["event_start_time"].isoformat(sep=" "),
                ],
                False,
            )
        return (
            [
                "AMBIGUOUS",
                event["telemetry_source_row_number"],
                event["event_slot"],
                event.get("event_type_raw"),
                event.get("event_start_time_raw"),
            ],
            True,
        )

    @staticmethod
    def _payload_signature(event: dict[str, Any]) -> str | None:
        payload_names = [
            "event_duration_s_raw",
            "event_speed_difference_raw",
            "event_speed_limit_kph_raw",
            "event_speed_kph_raw",
            "event_dtc_count_raw",
            "event_water_temp_c_raw",
            "event_engine_load_pct_raw",
            "event_dtc_code_0_raw",
        ]
        payload = {name: event.get(name) for name in payload_names}
        if all(value is None or value == "" for value in payload.values()):
            return None
        return sha256(_canonical_json(payload).encode("utf-8")).hexdigest().upper()

    def add(self, event: dict[str, Any]) -> None:
        identity, ambiguous = self._candidate_identity(event)
        candidate_id = "onset_" + sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
        group = self._groups.get(candidate_id)
        if group is None:
            group = {
                "source_file_name": event["source_file_name"],
                "source_file_sha256": event["source_file_sha256"],
                "source_sheet": event["source_sheet"],
                "event_onset_candidate_id": candidate_id,
                "enabled_code": event.get("enabled_code"),
                "journey_code": event.get("journey_code"),
                "trip_id": event.get("trip_id"),
                "event_type": event.get("event_type"),
                "event_name": event.get("event_name"),
                "event_start_time_raw": event.get("event_start_time_raw"),
                "event_start_time": event.get("event_start_time"),
                "event_start_time_quality": event.get("event_start_time_quality"),
                "first_observed_source_row": event["telemetry_source_row_number"],
                "last_observed_source_row": event["telemetry_source_row_number"],
                "observation_count": 0,
                "first_observed_telemetry_timestamp": event.get("telemetry_timestamp"),
                "last_observed_telemetry_timestamp": event.get("telemetry_timestamp"),
                "slots": set(),
                "start_raw_variants": set(),
                "end_time_variants": set(),
                "payload_variants": set(),
                "start_time_ambiguous": ambiguous,
                "start_year_mismatch_observed": False,
                "start_after_telemetry_observed": False,
                "has_observation_quality_issue": False,
            }
            self._groups[candidate_id] = group
        group["observation_count"] += 1
        group["last_observed_source_row"] = event["telemetry_source_row_number"]
        group["last_observed_telemetry_timestamp"] = event.get("telemetry_timestamp")
        group["slots"].add(event["event_slot"])
        if event.get("event_start_time_raw") not in (None, ""):
            group["start_raw_variants"].add(event["event_start_time_raw"])
        if event.get("event_end_time_raw") not in (None, ""):
            group["end_time_variants"].add(event["event_end_time_raw"])
        payload_signature = self._payload_signature(event)
        if payload_signature is not None:
            group["payload_variants"].add(payload_signature)
        group["has_observation_quality_issue"] = bool(
            group["has_observation_quality_issue"]
            or event["event_observation_has_quality_issue"]
        )
        group["start_year_mismatch_observed"] = bool(
            group["start_year_mismatch_observed"]
            or event["event_start_year_differs_from_telemetry"]
        )
        group["start_after_telemetry_observed"] = bool(
            group["start_after_telemetry_observed"]
            or event["event_start_after_telemetry"]
        )

    def records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for candidate_id in sorted(self._groups):
            group = self._groups[candidate_id]
            start_variants = len(group["start_raw_variants"])
            end_variants = len(group["end_time_variants"])
            payload_variants = len(group["payload_variants"])
            partition_time = group["event_start_time"] or group[
                "first_observed_telemetry_timestamp"
            ]
            records.append(
                {
                    "source_file_name": group["source_file_name"],
                    "source_file_sha256": group["source_file_sha256"],
                    "source_sheet": group["source_sheet"],
                    "event_onset_candidate_id": candidate_id,
                    "enabled_code": group["enabled_code"],
                    "journey_code": group["journey_code"],
                    "trip_id": group["trip_id"],
                    "event_type": group["event_type"],
                    "event_name": group["event_name"],
                    "event_start_time_raw": group["event_start_time_raw"],
                    "event_start_time": group["event_start_time"],
                    "event_start_time_quality": group["event_start_time_quality"],
                    "first_observed_source_row": group["first_observed_source_row"],
                    "last_observed_source_row": group["last_observed_source_row"],
                    "observation_count": group["observation_count"],
                    "first_observed_telemetry_timestamp": group[
                        "first_observed_telemetry_timestamp"
                    ],
                    "last_observed_telemetry_timestamp": group[
                        "last_observed_telemetry_timestamp"
                    ],
                    "slots_observed": sorted(group["slots"]),
                    "start_raw_variant_count": start_variants,
                    "end_time_variant_count": end_variants,
                    "payload_variant_count": payload_variants,
                    "end_time_conflict": end_variants > 1,
                    "payload_conflict": payload_variants > 1,
                    "start_time_ambiguous": bool(
                        group["start_time_ambiguous"] or start_variants > 1
                    ),
                    "start_year_mismatch_observed": group[
                        "start_year_mismatch_observed"
                    ],
                    "start_after_telemetry_observed": group[
                        "start_after_telemetry_observed"
                    ],
                    "has_observation_quality_issue": group[
                        "has_observation_quality_issue"
                    ],
                    "year_month": year_month(partition_time),
                }
            )
        return records


def _validate_output_root(config: EtlConfig) -> Path:
    output = config.output_root.resolve()
    if not config.enforce_output_policy:
        output.mkdir(parents=True, exist_ok=True)
        return output
    repo = config.repo_root.resolve()
    allowed = [(repo / "data" / "interim").resolve(), (repo / "data" / "processed").resolve()]
    if not any(output == root or output.is_relative_to(root) for root in allowed):
        raise EtlError(
            f"Unsafe output root {output}; outputs must remain under data/interim or data/processed."
        )
    source = config.source_path.resolve()
    if output == source or output in source.parents or source.is_relative_to(output):
        raise EtlError("Output root overlaps the immutable source path.")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _validate_new_targets(output_root: Path) -> None:
    existing = [name for name in OUTPUT_DATASETS if (output_root / name).exists()]
    if existing:
        raise EtlError(
            f"Refusing to overwrite existing ETL outputs: {existing}. Use a fresh output root."
        )


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        stream.write("\n")


def _add_quality(counter: Counter[str], name: str, parsed: ParsedValue) -> None:
    counter[f"{name}.{parsed.quality}"] += 1


def _normalize_telemetry(
    *,
    row: tuple[Any, ...],
    source_row_number: int,
    row_hash: str,
    metadata: SourceMetadata,
    indexes: dict[str, int],
    duplicate_info: tuple[int, bool] | None,
    quality_counts: Counter[str],
) -> dict[str, Any]:
    enabled_code = _identity_value(row[indexes["enabledCode"]])
    journey_code = _identity_value(row[indexes["journeyCode"]])
    license_plate = _identity_value(row[indexes["carNum"]])
    key_valid = bool(
        enabled_code
        and journey_code
        and len(enabled_code) == 10
        and re.fullmatch(r"\d{12}", journey_code)
    )
    if not enabled_code or not journey_code:
        journey_key_quality = "missing_unknown"
    elif not key_valid:
        journey_key_quality = "malformed"
    else:
        journey_key_quality = "valid"
    quality_counts[f"journey_key.{journey_key_quality}"] += 1

    timestamp = parse_timestamp(row[indexes["time"]])
    _add_quality(quality_counts, "timestamp", timestamp)
    record: dict[str, Any] = {
        "source_file_name": metadata.file_name,
        "source_file_sha256": metadata.sha256,
        "source_sheet": metadata.sheet,
        "source_row_number": source_row_number,
        "source_row_hash": row_hash,
        "journey_code": journey_code,
        "enabled_code": enabled_code,
        "trip_id": canonical_trip_id(enabled_code, journey_code),
        "license_plate": license_plate,
        "journey_key_quality": journey_key_quality,
        "timestamp_raw": timestamp.raw,
        "timestamp": timestamp.value,
        "timestamp_quality": timestamp.quality,
    }
    parsed_by_name: dict[str, ParsedValue] = {}
    for spec in BASE_NUMERIC_SPECS:
        parsed = parse_numeric(row[indexes[spec.source_name]], spec)
        parsed_by_name[spec.normalized_name] = parsed
        record[f"{spec.normalized_name}_raw"] = parsed.raw
        record[spec.normalized_name] = parsed.value
        record[f"{spec.normalized_name}_quality"] = parsed.quality
        _add_quality(quality_counts, spec.normalized_name, parsed)

    fence_raw, fence_ids, fence_quality = parse_fence_ids(row[indexes["fenceId"]])
    record["fence_ids_raw"] = fence_raw
    record["fence_ids"] = fence_ids
    record["fence_ids_quality"] = fence_quality
    quality_counts[f"fence_ids.{fence_quality}"] += 1

    all_qualities = [timestamp.quality, journey_key_quality, fence_quality]
    all_qualities.extend(parsed.quality for parsed in parsed_by_name.values())
    duplicate_size, duplicate_identical = duplicate_info or (1, False)
    record.update(
        {
            "gps_coordinates_missing": bool(
                parsed_by_name["longitude_deg"].value is None
                or parsed_by_name["latitude_deg"].value is None
            ),
            "gps_speed_out_of_range": parsed_by_name["gps_speed_kph"].quality
            == "out_of_range",
            "azimuth_out_of_range": parsed_by_name["gps_azimuth_deg"].quality
            == "out_of_range",
            "row_has_malformed_value": "malformed" in all_qualities,
            "row_has_out_of_range_value": "out_of_range" in all_qualities,
            "is_duplicate_source_key": duplicate_size > 1,
            "duplicate_group_size": duplicate_size,
            "duplicate_rows_identical": duplicate_identical if duplicate_size > 1 else None,
            "year_month": year_month(timestamp.value if isinstance(timestamp.value, datetime) else None),
        }
    )
    return record


def _event_raw_values(
    row: tuple[Any, ...], indexes: dict[str, int], slot: int
) -> dict[str, Any]:
    prefix = f"event[{slot}]"
    return {
        "type": row[indexes[f"{prefix}.type"]],
        "start_time": row[indexes[f"{prefix}.startTime"]],
        "end_time": row[indexes[f"{prefix}.endTime"]],
        "duration": row[indexes[f"{prefix}.info.duration"]],
        "speed_difference": row[indexes[f"{prefix}.info.speedDifference"]],
        "speed_limit": row[indexes[f"{prefix}.info.speedLimit"]],
        "speed": row[indexes[f"{prefix}.info.speed"]],
        "dtc_count": row[indexes[f"{prefix}.info.dtcCount"]],
        "water_temp": row[indexes[f"{prefix}.info.waterTemp"]],
        "engine_load": row[indexes[f"{prefix}.info.engineLoad"]],
        "dtc_code_0": row[indexes[f"{prefix}.info.dtcCodes[0]"]],
    }


def _normalize_event(
    *,
    row: tuple[Any, ...],
    slot: int,
    telemetry: dict[str, Any],
    metadata: SourceMetadata,
    indexes: dict[str, int],
    quality_counts: Counter[str],
) -> dict[str, Any] | None:
    raw = _event_raw_values(row, indexes, slot)
    if all(is_missing(value) for value in raw.values()):
        return None

    event_type = parse_numeric(raw["type"], EVENT_TYPE_SPEC)
    parsed_type = int(event_type.value) if event_type.value is not None else None
    start_time = parse_timestamp(raw["start_time"])
    telemetry_timestamp = telemetry["timestamp"]
    parsed_event_start = start_time.value
    if isinstance(telemetry_timestamp, datetime) and isinstance(parsed_event_start, datetime):
        start_to_telemetry_seconds = (
            telemetry_timestamp - parsed_event_start
        ).total_seconds()
        start_year_differs = telemetry_timestamp.year != parsed_event_start.year
        start_after_telemetry = start_to_telemetry_seconds < 0
    else:
        start_to_telemetry_seconds = None
        start_year_differs = False
        start_after_telemetry = False
    if start_year_differs:
        quality_counts["event_start_time.year_differs_from_telemetry"] += 1
    if start_after_telemetry:
        quality_counts["event_start_time.after_telemetry"] += 1
    end_missing_kind = (
        "unknown" if parsed_type in EVENT_FIELDS_WITH_END or parsed_type is None else "structural"
    )
    end_time = parse_timestamp(raw["end_time"], missing_kind=end_missing_kind)
    for name, parsed in (
        ("event_type", event_type),
        ("event_start_time", start_time),
        ("event_end_time", end_time),
    ):
        _add_quality(quality_counts, name, parsed)

    event: dict[str, Any] = {
        "source_file_name": metadata.file_name,
        "source_file_sha256": metadata.sha256,
        "source_sheet": metadata.sheet,
        "telemetry_source_row_number": telemetry["source_row_number"],
        "telemetry_source_row_hash": telemetry["source_row_hash"],
        "enabled_code": telemetry["enabled_code"],
        "journey_code": telemetry["journey_code"],
        "trip_id": telemetry["trip_id"],
        "telemetry_timestamp_raw": telemetry["timestamp_raw"],
        "telemetry_timestamp": telemetry["timestamp"],
        "telemetry_timestamp_quality": telemetry["timestamp_quality"],
        "event_slot": slot,
        "event_type_raw": event_type.raw,
        "event_type": parsed_type,
        "event_type_quality": event_type.quality,
        "event_name": EVENT_NAMES.get(parsed_type),
        "event_start_time_raw": start_time.raw,
        "event_start_time": start_time.value,
        "event_start_time_quality": start_time.quality,
        "event_start_to_telemetry_seconds": start_to_telemetry_seconds,
        "event_start_year_differs_from_telemetry": start_year_differs,
        "event_start_after_telemetry": start_after_telemetry,
        "event_end_time_raw": end_time.raw,
        "event_end_time": end_time.value,
        "event_end_time_quality": end_time.quality,
    }
    issue_qualities = [event_type.quality, start_time.quality, end_time.quality]
    for key, spec in EVENT_PAYLOAD_SPECS.items():
        applicable = parsed_type in EVENT_FIELD_APPLICABILITY[key]
        missing_kind = "unknown" if applicable or parsed_type is None else "structural"
        parsed = parse_numeric(raw[key], spec, missing_kind=missing_kind)
        event[f"{spec.normalized_name}_raw"] = parsed.raw
        event[spec.normalized_name] = parsed.value
        event[f"{spec.normalized_name}_quality"] = parsed.quality
        _add_quality(quality_counts, spec.normalized_name, parsed)
        issue_qualities.append(parsed.quality)

    dtc_raw = raw_string(raw["dtc_code_0"])
    if is_missing(raw["dtc_code_0"]):
        dtc_quality = (
            "missing_unknown"
            if parsed_type in EVENT_FIELD_APPLICABILITY["dtc_code_0"] or parsed_type is None
            else "missing_structural"
        )
    else:
        dtc_quality = "valid"
    event["event_dtc_code_0_raw"] = dtc_raw
    event["event_dtc_code_0_quality"] = dtc_quality
    quality_counts[f"event_dtc_code_0.{dtc_quality}"] += 1
    issue_qualities.append(dtc_quality)
    event.update(
        {
            "is_duplicate_telemetry_key": telemetry["is_duplicate_source_key"],
            "duplicate_telemetry_group_size": telemetry["duplicate_group_size"],
            "event_observation_has_quality_issue": any(
                _quality_issue(quality) for quality in issue_qualities
            )
            or start_year_differs
            or start_after_telemetry,
            "year_month": telemetry["year_month"],
        }
    )
    return event


def _build_duplicate_index(
    config: EtlConfig,
    staging_root: Path,
    indexes: dict[str, int],
    memory: _PeakMemory,
) -> tuple[int, dict[str, tuple[int, bool]]]:
    database_path = staging_root / "duplicate_index.sqlite"
    duplicate_index = _DuplicateIndex(database_path)
    rows = 0
    try:
        for source_row_number, row in iter_source_rows(config.source_path):
            row_hash = _source_row_hash(row)
            duplicate_index.add(_source_key_hash(row, indexes), row_hash)
            rows += 1
            if rows % config.batch_size == 0:
                duplicate_index.commit()
            if rows % config.progress_every_rows == 0:
                memory.sample()
                _log("duplicate_index_progress", rows_processed=rows)
        duplicate_index.commit()
        duplicates = duplicate_index.duplicates()
    finally:
        duplicate_index.close()
    database_path.unlink(missing_ok=True)
    return rows, duplicates


def _software_versions() -> dict[str, str]:
    result = {
        "hino_safepilot": __version__,
        "python": platform.python_version(),
        "openpyxl": openpyxl.__version__,
        "pyarrow": pa.__version__,
    }
    try:
        import psutil  # type: ignore

        result["psutil"] = psutil.__version__
    except ImportError:
        result["psutil"] = "not-installed"
    return result


def _promote(staging_root: Path, output_root: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for name in OUTPUT_DATASETS:
        source = staging_root / name
        destination = output_root / name
        if not source.exists():
            source.mkdir(parents=True, exist_ok=True)
        source.replace(destination)
        paths[name] = destination
    staging_root.rmdir()
    return paths


def run_etl(config: EtlConfig) -> EtlResult:
    """Validate, normalize, and write Phase 2 datasets without ML semantics."""

    configure_logging()
    started_at = _utc_now()
    start_clock = time.perf_counter()
    memory = _PeakMemory()
    memory.sample()
    repo_root = config.repo_root.resolve()
    source_path = config.source_path.resolve()
    if config.expected_sha256:
        expected_sha256 = config.expected_sha256.upper()
    else:
        if config.manifest_path is None:
            raise EtlError("manifest_path or expected_sha256 is required")
        try:
            source_relative = source_path.relative_to(repo_root).as_posix()
        except ValueError as exc:
            raise EtlError("Source outside repo requires explicit expected_sha256") from exc
        expected_sha256 = expected_hash_from_manifest(
            config.manifest_path.resolve(), source_relative
        )

    metadata = validate_source(
        source_path,
        expected_sha256=expected_sha256,
        expected_rows=config.expected_rows,
        expected_columns=config.expected_columns,
        expected_headers=config.expected_headers,
        expected_source_dimension=config.expected_source_dimension,
        expected_mapping_dimension=config.expected_mapping_dimension,
    )
    _log(
        "source_validated",
        sha256=metadata.sha256,
        size_bytes=metadata.size_bytes,
        sheet=metadata.sheet,
        source_rows=metadata.source_rows,
        source_columns=metadata.source_columns,
    )
    if config.dry_run:
        elapsed = time.perf_counter() - start_clock
        _log("dry_run_complete", elapsed_seconds=round(elapsed, 3))
        return EtlResult(
            source_hash_before=metadata.sha256,
            source_hash_after=metadata.sha256,
            input_rows=metadata.source_rows,
            telemetry_rows=0,
            event_observations=0,
            event_onset_candidates=0,
            malformed_cells=0,
            quarantined_rows=0,
            duplicate_group_count=0,
            elapsed_seconds=elapsed,
            peak_rss_bytes=memory.peak,
        )

    output_root = _validate_output_root(config)
    _validate_new_targets(output_root)
    staging_root = output_root / f".etl_staging_{os.getpid()}"
    if staging_root.exists():
        raise EtlError(f"Staging path already exists: {staging_root}")
    staging_root.mkdir(parents=True)
    indexes = {name: index for index, name in enumerate(EXPECTED_HEADERS)}

    telemetry_writer: _PartitionedWriter | None = None
    event_writer: _PartitionedWriter | None = None
    onset_writer: _PartitionedWriter | None = None
    try:
        _log("phase_started", phase="duplicate_index")
        indexed_rows, duplicate_groups = _build_duplicate_index(
            config, staging_root, indexes, memory
        )
        if indexed_rows != metadata.source_rows:
            raise EtlError(
                f"Duplicate pass row accounting failed: {indexed_rows} != {metadata.source_rows}."
            )
        duplicate_group_count = len(duplicate_groups)
        duplicate_rows = sum(size for size, _ in duplicate_groups.values())
        duplicate_excess_rows = sum(size - 1 for size, _ in duplicate_groups.values())
        identical_duplicate_groups = sum(
            1 for _, identical in duplicate_groups.values() if identical
        )
        _log(
            "duplicate_index_complete",
            duplicate_group_count=duplicate_group_count,
            duplicate_rows=duplicate_rows,
            duplicate_excess_rows=duplicate_excess_rows,
            identical_duplicate_groups=identical_duplicate_groups,
        )

        telemetry_arrow_schema = telemetry_schema()
        event_arrow_schema = event_observation_schema()
        onset_arrow_schema = event_onset_schema()
        telemetry_writer = _PartitionedWriter(
            staging_root / "telemetry_normalized",
            telemetry_arrow_schema,
            compression=config.compression,
            compression_level=config.compression_level,
        )
        event_writer = _PartitionedWriter(
            staging_root / "events_observed",
            event_arrow_schema,
            compression=config.compression,
            compression_level=config.compression_level,
        )
        onset_writer = _PartitionedWriter(
            staging_root / "events_onsets",
            onset_arrow_schema,
            compression=config.compression,
            compression_level=config.compression_level,
        )
        telemetry_quality: Counter[str] = Counter()
        event_quality: Counter[str] = Counter()
        quality_flags: Counter[str] = Counter()
        event_type_counts: Counter[str] = Counter()
        onset_accumulator = _OnsetAccumulator()
        telemetry_buffer: list[dict[str, Any]] = []
        event_buffer: list[dict[str, Any]] = []
        telemetry_rows = 0
        event_observations = 0
        malformed_telemetry_rows = 0
        out_of_range_telemetry_rows = 0

        _log("phase_started", phase="normalize_and_write")
        for source_row_number, row in iter_source_rows(source_path):
            row_hash = _source_row_hash(row)
            duplicate_info = duplicate_groups.get(_source_key_hash(row, indexes))
            telemetry = _normalize_telemetry(
                row=row,
                source_row_number=source_row_number,
                row_hash=row_hash,
                metadata=metadata,
                indexes=indexes,
                duplicate_info=duplicate_info,
                quality_counts=telemetry_quality,
            )
            telemetry_buffer.append(telemetry)
            telemetry_rows += 1
            for flag in (
                "gps_coordinates_missing",
                "gps_speed_out_of_range",
                "azimuth_out_of_range",
                "row_has_malformed_value",
                "row_has_out_of_range_value",
                "is_duplicate_source_key",
            ):
                if telemetry[flag]:
                    quality_flags[flag] += 1
            if telemetry["row_has_malformed_value"]:
                malformed_telemetry_rows += 1
            if telemetry["row_has_out_of_range_value"]:
                out_of_range_telemetry_rows += 1

            for slot in range(3):
                event = _normalize_event(
                    row=row,
                    slot=slot,
                    telemetry=telemetry,
                    metadata=metadata,
                    indexes=indexes,
                    quality_counts=event_quality,
                )
                if event is None:
                    continue
                event_buffer.append(event)
                event_observations += 1
                event_type_counts[str(event["event_type"])] += 1
                onset_accumulator.add(event)

            if len(telemetry_buffer) >= config.batch_size:
                telemetry_writer.write(telemetry_buffer)
                telemetry_buffer.clear()
            if len(event_buffer) >= config.batch_size:
                event_writer.write(event_buffer)
                event_buffer.clear()
            if telemetry_rows % config.progress_every_rows == 0:
                memory.sample()
                _log(
                    "normalization_progress",
                    telemetry_rows=telemetry_rows,
                    event_observations=event_observations,
                )

        telemetry_writer.write(telemetry_buffer)
        event_writer.write(event_buffer)
        telemetry_writer.close()
        event_writer.close()
        telemetry_writer = None
        event_writer = None
        if telemetry_rows != metadata.source_rows:
            raise EtlError(
                f"Telemetry row accounting failed: {telemetry_rows} != {metadata.source_rows}."
            )
        if telemetry_quality["timestamp.valid"] == 0:
            raise EtlError("Catastrophic timestamp failure: no source timestamp parsed successfully.")

        onset_records = onset_accumulator.records()
        for start in range(0, len(onset_records), config.batch_size):
            onset_writer.write(onset_records[start : start + config.batch_size])
        onset_writer.close()
        onset_writer = None
        onset_count = len(onset_records)
        onset_conflicts = {
            "payload_conflict": sum(1 for record in onset_records if record["payload_conflict"]),
            "end_time_conflict": sum(1 for record in onset_records if record["end_time_conflict"]),
            "start_time_ambiguous": sum(
                1 for record in onset_records if record["start_time_ambiguous"]
            ),
        }

        source_hash_after = sha256_file(source_path)
        source_stat_after = source_path.stat()
        if source_hash_after != metadata.sha256:
            raise SourceValidationError(
                f"Source hash changed during ETL: before {metadata.sha256}, after {source_hash_after}."
            )
        if (
            source_stat_after.st_mtime_ns != metadata.mtime_ns
            or source_stat_after.st_size != metadata.size_bytes
        ):
            raise SourceValidationError("Source file metadata changed during ETL.")

        malformed_cells = sum(
            count
            for key, count in (*telemetry_quality.items(), *event_quality.items())
            if key.endswith(".malformed")
        )
        ended_at = _utc_now()
        elapsed = time.perf_counter() - start_clock
        memory.sample()
        quality_summary = {
            "telemetry_quality_counts": dict(sorted(telemetry_quality.items())),
            "event_quality_counts": dict(sorted(event_quality.items())),
            "quality_flag_counts": dict(sorted(quality_flags.items())),
            "event_type_observation_counts": dict(sorted(event_type_counts.items())),
            "malformed_cell_count": malformed_cells,
            "malformed_telemetry_row_count": malformed_telemetry_rows,
            "out_of_range_telemetry_row_count": out_of_range_telemetry_rows,
            "quarantined_row_count": 0,
            "duplicate_group_count": duplicate_group_count,
            "duplicate_rows_in_groups": duplicate_rows,
            "duplicate_excess_row_count": duplicate_excess_rows,
            "identical_duplicate_group_count": identical_duplicate_groups,
            "event_onset_conflict_counts": onset_conflicts,
            "missing_policy": {
                "structural": "Organizer-confirmed or event-type-inapplicable field; preserved as null.",
                "unknown": "Missing without a confirmed structural explanation; preserved as null.",
                "malformed": "Non-missing value failed explicit parsing; raw value preserved.",
                "out_of_range": "Parsed value violates documented range/domain; value and raw preserved.",
            },
        }
        run_manifest = {
            "etl_version": __version__,
            "status": "complete",
            "started_at_utc": started_at.isoformat(),
            "ended_at_utc": ended_at.isoformat(),
            "elapsed_seconds": round(elapsed, 6),
            "peak_rss_bytes_approximate": memory.peak,
            "software_versions": _software_versions(),
            "source": {
                "file_name": metadata.file_name,
                "sha256_before": metadata.sha256,
                "sha256_after": source_hash_after,
                "size_bytes": metadata.size_bytes,
                "sheet": metadata.sheet,
                "rows": metadata.source_rows,
                "columns": metadata.source_columns,
                "dimension": metadata.source_dimension,
            },
            "configuration": {
                "batch_size": config.batch_size,
                "compression": config.compression,
                "compression_level": config.compression_level,
                "partition_key": "year_month",
                "timestamp_timezone_semantics": "UNRESOLVED; no timezone conversion performed",
            },
            "accounting": {
                "input_rows": metadata.source_rows,
                "output_telemetry_rows": telemetry_rows,
                "event_observations": event_observations,
                "event_onset_candidates": onset_count,
                "malformed_cells": malformed_cells,
                "quarantined_rows": 0,
                "duplicate_groups": duplicate_group_count,
            },
            "semantic_determinism": {
                "guaranteed": [
                    "row counts",
                    "logical schemas",
                    "aggregate quality counts",
                    "event observation counts",
                    "event onset candidate identities and conflict flags",
                ],
                "not_claimed": "Byte-identical Parquet or run manifest timestamps.",
            },
        }
        schemas = {
            "source": {
                "sheet": metadata.sheet,
                "headers": list(metadata.headers),
                "rows": metadata.source_rows,
                "columns": metadata.source_columns,
            },
            "telemetry_normalized": {
                "partition_key": "year_month",
                "fields": schema_snapshot(telemetry_arrow_schema),
            },
            "events_observed": {
                "partition_key": "year_month",
                "fields": schema_snapshot(event_arrow_schema),
            },
            "events_onsets": {
                "partition_key": "year_month",
                "fields": schema_snapshot(onset_arrow_schema),
            },
        }
        _write_json(staging_root / "audit" / "etl_run_manifest.json", run_manifest)
        _write_json(staging_root / "audit" / "quality_summary.json", quality_summary)
        _write_json(staging_root / "audit" / "schema_snapshot.json", schemas)
        output_paths = _promote(staging_root, output_root)
        _log(
            "etl_complete",
            telemetry_rows=telemetry_rows,
            event_observations=event_observations,
            event_onset_candidates=onset_count,
            malformed_cells=malformed_cells,
            output_root=str(output_root),
            elapsed_seconds=round(elapsed, 3),
            peak_rss_bytes=memory.peak,
        )
        return EtlResult(
            source_hash_before=metadata.sha256,
            source_hash_after=source_hash_after,
            input_rows=metadata.source_rows,
            telemetry_rows=telemetry_rows,
            event_observations=event_observations,
            event_onset_candidates=onset_count,
            malformed_cells=malformed_cells,
            quarantined_rows=0,
            duplicate_group_count=duplicate_group_count,
            elapsed_seconds=elapsed,
            peak_rss_bytes=memory.peak,
            output_paths=output_paths,
        )
    except Exception:
        _log("etl_failed", phase="execution", staging_root=str(staging_root))
        raise
    finally:
        for writer in (telemetry_writer, event_writer, onset_writer):
            if writer is not None:
                writer.close()
        if staging_root.exists():
            shutil.rmtree(staging_root)
