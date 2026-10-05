"""Build the reviewed Phase 3B-1 tabular V0 dataset without training a model."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import statistics
import subprocess
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Iterator
from uuid import uuid4

import psutil
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from .audit import audit_feature_files, validate_model_schema
from .models import CORE_EVENT_TYPES, EventOnset, LeakageEvidence, TripTelemetry
from .spec import load_feature_config, resolved_feature_spec


DATASET_VERSION = "v0_hino"
LABEL_SPEC_VERSION = "label-v0-human-approved-2026-08-17"
SPLITS = ("train", "validation", "test")
TRAIN_END = datetime(2025, 8, 31, 23, 59, 59)
VALIDATION_END = datetime(2025, 10, 31, 23, 59, 59)
BOUNDARIES = (datetime(2025, 9, 1), datetime(2025, 11, 1))

TELEMETRY_VALUE_FIELDS = {
    "can_speed": "can_speed_kph",
    "gps_speed": "gps_speed_kph",
    "engine_rpm": "engine_rpm",
    "engine_load": "engine_load_pct",
    "coolant_temp": "engine_coolant_temp_c",
    "battery_mv": "battery_mv",
    "fuel_level": "fuel_level_pct",
    "can_status": "can_status",
    "vehicle_status": "vehicle_status",
}
TELEMETRY_QUALITY_FIELDS = {
    name: f"{column}_quality" for name, column in TELEMETRY_VALUE_FIELDS.items()
}


@dataclass(frozen=True)
class DatasetBuildConfig:
    repo_root: Path
    output_root: Path
    feature_config: Path
    batch_size: int = 2_000

    @classmethod
    def from_repo(
        cls,
        repo_root: Path,
        output_root: Path = Path("data/processed/ml/v0"),
        feature_config: Path = Path("config/features_v0.json"),
        batch_size: int = 2_000,
    ) -> "DatasetBuildConfig":
        root = repo_root.resolve()
        output = output_root.resolve() if output_root.is_absolute() else (root / output_root).resolve()
        spec = feature_config.resolve() if feature_config.is_absolute() else (root / feature_config).resolve()
        allowed = (root / "data" / "processed" / "ml").resolve()
        if output == allowed or allowed not in output.parents:
            raise ValueError("Dataset output must be below data/processed/ml/")
        if output.exists():
            raise FileExistsError(f"Refusing to overwrite dataset output: {output}")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        return cls(root, output, spec, batch_size)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _protected_hashes(repo_root: Path) -> dict[str, str]:
    roots = [
        repo_root / "data" / "interim" / "telemetry_normalized",
        repo_root / "data" / "interim" / "events_observed",
        repo_root / "data" / "interim" / "events_onsets",
        repo_root / "data" / "processed" / "label_research",
    ]
    return {
        path.relative_to(repo_root).as_posix(): _file_sha256(path)
        for root in roots
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _git_info(repo_root: Path) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "-c", f"safe.directory={repo_root.as_posix()}", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {"git_commit": commit or "UNAVAILABLE", "working_tree_may_be_dirty": True}


def _anchor_id(trip_id: str, timestamp: datetime) -> str:
    material = f"{DATASET_VERSION}|{trip_id}|{timestamp.isoformat()}".encode()
    return hashlib.sha256(material).hexdigest()


def _temporal_split(trip_start: datetime) -> str:
    if trip_start <= TRAIN_END:
        return "train"
    if trip_start <= VALIDATION_END:
        return "validation"
    return "test"


def _crosses_split_boundary(start: datetime, end: datetime) -> bool:
    return any(start < boundary <= end for boundary in BOUNDARIES)


def _in_embargo(anchor: datetime) -> bool:
    return any(
        boundary - timedelta(seconds=60) <= anchor <= boundary + timedelta(seconds=300)
        for boundary in BOUNDARIES
    )


def _trip_quality_reason(trip: TripTelemetry) -> str | None:
    if trip.duration_seconds <= 0:
        return "INVALID_OR_ZERO_DURATION_TRIP"
    if trip.duration_seconds > 12 * 3600:
        return "ULTRA_LONG_TRIP_OVER_12H"
    if _crosses_split_boundary(trip.start, trip.end):
        return "TRIP_CROSSES_TEMPORAL_SPLIT_BOUNDARY"
    return None


def _active_core_at_anchor(
    active_intervals: dict[int, list[tuple[datetime, datetime]]], anchor: datetime
) -> bool:
    return any(
        start < anchor <= end
        for event_type in CORE_EVENT_TYPES.values()
        for start, end in active_intervals.get(event_type, [])
    )


def _available_past_onsets(
    events: list[EventOnset], anchor: datetime
) -> dict[int, list[datetime]]:
    result: dict[int, list[datetime]] = defaultdict(list)
    for event in events:
        if (
            event.start < anchor
            and event.first_observed is not None
            and event.first_observed < anchor
        ):
            result[event.event_type].append(event.start)
    return result


def _complete_feature_source_max(
    anchor: datetime,
    telemetry_max: datetime | None,
    events: list[EventOnset],
    speeding_observations: list[datetime],
) -> datetime | None:
    candidates = [telemetry_max] if telemetry_max is not None else []
    history_start = anchor - timedelta(seconds=300)
    for event in events:
        if (
            history_start <= event.start < anchor
            and event.first_observed is not None
            and event.first_observed < anchor
        ):
            candidates.extend((event.start, event.first_observed))
    index = bisect_left(speeding_observations, anchor) - 1
    if index >= 0 and speeding_observations[index] >= history_start:
        candidates.append(speeding_observations[index])
    return max(candidates) if candidates else None


def _telemetry_table(repo_root: Path) -> pa.Table:
    columns = [
        "source_row_number",
        "trip_id",
        "enabled_code",
        "timestamp",
        "timestamp_quality",
        "vehicle_series_code",
        "vehicle_series_code_quality",
        *TELEMETRY_VALUE_FIELDS.values(),
        *TELEMETRY_QUALITY_FIELDS.values(),
        "is_duplicate_source_key",
    ]
    dataset = ds.dataset(
        repo_root / "data" / "interim" / "telemetry_normalized",
        format="parquet",
        partitioning="hive",
    )
    table = dataset.to_table(columns=columns)
    valid = pc.and_(
        pc.is_valid(table["trip_id"]),
        pc.and_(pc.is_valid(table["timestamp"]), pc.equal(table["timestamp_quality"], "valid")),
    )
    table = table.filter(valid)
    order = pc.sort_indices(
        table,
        sort_keys=[("trip_id", "ascending"), ("timestamp", "ascending"), ("source_row_number", "ascending")],
    )
    return table.take(order)


def _trip_slices(table: pa.Table) -> Iterator[TripTelemetry]:
    trip_ids = table["trip_id"].to_pylist()
    start = 0
    while start < len(trip_ids):
        end = start + 1
        while end < len(trip_ids) and trip_ids[end] == trip_ids[start]:
            end += 1
        section = table.slice(start, end - start)
        raw = section.to_pydict()
        timestamps: list[datetime] = []
        values = {name: [] for name in TELEMETRY_VALUE_FIELDS}
        qualities = {name: [] for name in TELEMETRY_VALUE_FIELDS}
        duplicate_extra = 0
        last_timestamp: datetime | None = None
        for index, timestamp in enumerate(raw["timestamp"]):
            if timestamp == last_timestamp:
                duplicate_extra += 1
                continue
            last_timestamp = timestamp
            timestamps.append(timestamp)
            for name, column in TELEMETRY_VALUE_FIELDS.items():
                values[name].append(raw[column][index])
                qualities[name].append(raw[TELEMETRY_QUALITY_FIELDS[name]][index])
        if timestamps:
            yield TripTelemetry(
                trip_id=trip_ids[start],
                vehicle_id=raw["enabled_code"][0] or "MISSING_VEHICLE",
                vehicle_series_code=raw["vehicle_series_code"][0],
                timestamps=timestamps,
                values=values,
                qualities=qualities,
                duplicate_extra_rows=duplicate_extra,
            )
        start = end


def _load_onsets(repo_root: Path) -> dict[str, list[EventOnset]]:
    dataset = ds.dataset(
        repo_root / "data" / "interim" / "events_onsets",
        format="parquet",
        partitioning="hive",
    )
    columns = [
        "trip_id",
        "event_type",
        "event_start_time",
        "event_start_time_quality",
        "first_observed_telemetry_timestamp",
        "last_observed_telemetry_timestamp",
        "start_time_ambiguous",
        "start_year_mismatch_observed",
    ]
    grouped: dict[str, list[EventOnset]] = defaultdict(list)
    for batch in dataset.to_batches(columns=columns, batch_size=20_000):
        for row in batch.to_pylist():
            start = row["event_start_time"]
            if not _trusted_onset_row(row):
                continue
            active_end = row["last_observed_telemetry_timestamp"]
            grouped[row["trip_id"]].append(
                EventOnset(
                    int(row["event_type"]),
                    start,
                    row["first_observed_telemetry_timestamp"],
                    active_end if active_end is not None and active_end > start else None,
                )
            )
    for rows in grouped.values():
        rows.sort(key=lambda event: (event.start, event.event_type))
    return grouped


def _trusted_onset_row(row: dict[str, Any]) -> bool:
    return bool(
        row.get("trip_id")
        and row.get("event_type") is not None
        and row.get("event_start_time") is not None
        and row.get("event_start_time_quality") == "valid"
        and not row.get("start_time_ambiguous")
        and not row.get("start_year_mismatch_observed")
    )


def _load_speeding_observations(repo_root: Path) -> dict[str, list[datetime]]:
    dataset = ds.dataset(
        repo_root / "data" / "interim" / "events_observed",
        format="parquet",
        partitioning="hive",
    )
    grouped: dict[str, list[datetime]] = defaultdict(list)
    scanner = dataset.scanner(
        columns=["trip_id", "telemetry_timestamp", "telemetry_timestamp_quality", "event_type"],
        filter=ds.field("event_type") == 8,
        batch_size=20_000,
    )
    for batch in scanner.to_batches():
        for row in batch.to_pylist():
            if row["trip_id"] and row["telemetry_timestamp"] and row["telemetry_timestamp_quality"] == "valid":
                grouped[row["trip_id"]].append(row["telemetry_timestamp"])
    for values in grouped.values():
        values.sort()
    return grouped


def _vehicle_holdout_map(trips: Iterable[TripTelemetry]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    vehicle_types: dict[str, int | None] = {}
    for trip in trips:
        vehicle_types.setdefault(trip.vehicle_id, trip.vehicle_series_code)
    ranked = sorted(
        vehicle_types,
        key=lambda vehicle: hashlib.sha256(f"v0-vehicle-holdout|{vehicle}".encode()).hexdigest(),
    )
    selected: set[str] = set()
    for series in sorted({value for value in vehicle_types.values() if value is not None}):
        selected.add(next(vehicle for vehicle in ranked if vehicle_types[vehicle] == series))
    for vehicle in ranked:
        if len(selected) >= max(4, math.ceil(len(ranked) * 0.2)):
            break
        selected.add(vehicle)
    mapping = {vehicle: ("holdout" if vehicle in selected else "development") for vehicle in ranked}
    private_rows = [
        {
            "vehicle_id": vehicle,
            "vehicle_series_code": vehicle_types[vehicle],
            "assignment": mapping[vehicle],
            "selection_rank": ranked.index(vehicle) + 1,
            "selection_method": "sha256_rank_type_coverage_v1",
        }
        for vehicle in ranked
    ]
    return mapping, private_rows


def _anchors(timestamps: list[datetime]) -> list[datetime]:
    selected: list[datetime] = []
    for timestamp in timestamps:
        if not selected or (timestamp - selected[-1]).total_seconds() >= 10:
            selected.append(timestamp)
    return selected


def _history_diagnostics(timestamps: list[datetime], anchor: datetime) -> dict[str, float | int]:
    boundary = anchor - timedelta(seconds=300)
    left = bisect_left(timestamps, boundary)
    right = bisect_left(timestamps, anchor)
    history = timestamps[left:right]
    points = [boundary, *history, anchor]
    return {
        "left": left,
        "right": right,
        "coverage": (anchor - history[0]).total_seconds() if history else 0.0,
        "point_count": len(history),
        "max_gap": max(
            ((later - earlier).total_seconds() for earlier, later in zip(points, points[1:])),
            default=300.0,
        ),
    }


def _future_status(timestamps: list[datetime], anchor: datetime) -> str:
    horizon = anchor + timedelta(seconds=60)
    if timestamps[-1] < horizon:
        return "censored"
    left = bisect_right(timestamps, anchor)
    right = bisect_right(timestamps, horizon)
    return "valid" if right > left else "ambiguous"


def _valid_values(
    trip: TripTelemetry, name: str, left: int, right: int
) -> tuple[list[float], list[datetime], int]:
    values: list[float] = []
    times: list[datetime] = []
    out_of_range = 0
    for index in range(left, right):
        quality = trip.qualities[name][index]
        value = trip.values[name][index]
        if quality == "out_of_range":
            out_of_range += 1
        if quality == "valid" and value is not None:
            values.append(float(value))
            times.append(trip.timestamps[index])
    return values, times, out_of_range


def _trend(values: list[float], times: list[datetime]) -> float | None:
    if len(values) < 2:
        return None
    origin = times[0]
    xs = [(timestamp - origin).total_seconds() for timestamp in times]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(values)
    denominator = sum((value - mean_x) ** 2 for value in xs)
    return (
        sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / denominator
        if denominator
        else None
    )


def _numeric_aggregations(
    values: list[float], times: list[datetime], out_of_range: int
) -> dict[str, float | None]:
    if not values:
        return {
            "mean": None,
            "std": None,
            "min": None,
            "max": None,
            "median": None,
            "trend_per_second": None,
            "range": None,
            "valid_count": 0.0,
            "out_of_range_count": float(out_of_range),
        }
    minimum, maximum = min(values), max(values)
    return {
        "mean": statistics.fmean(values),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "min": minimum,
        "max": maximum,
        "median": statistics.median(values),
        "trend_per_second": _trend(values, times),
        "range": maximum - minimum,
        "valid_count": float(len(values)),
        "out_of_range_count": float(out_of_range),
    }


def _count_between(values: list[datetime], start: datetime, end: datetime) -> int:
    return bisect_left(values, end) - bisect_left(values, start)


def _features(
    trip: TripTelemetry,
    anchor: datetime,
    onsets_by_type: dict[int, list[datetime]],
    speeding_observations: list[datetime],
    history: dict[str, float | int],
) -> tuple[dict[str, float | int | None], datetime | None]:
    result: dict[str, float | int | None] = {}
    right = int(history["right"])
    for window in (30, 60, 120, 300):
        left_time = anchor - timedelta(seconds=window)
        left = bisect_left(trip.timestamps, left_time, 0, right)
        point_count = right - left
        result[f"telemetry_point_count_{window}s"] = float(point_count)
        result[f"telemetry_density_per_min_{window}s"] = point_count / (window / 60)
        for name in ("can_speed", "gps_speed"):
            values, times, out_of_range = _valid_values(trip, name, left, right)
            stats = _numeric_aggregations(values, times, out_of_range)
            aggregations = (
                ("mean", "std", "min", "max", "median", "trend_per_second", "range", "valid_count")
                if name == "can_speed"
                else ("mean", "std", "min", "max", "valid_count", "out_of_range_count")
            )
            for aggregation in aggregations:
                result[f"{name}_{aggregation}_{window}s"] = stats[aggregation]
        for event_name, event_type in {
            "accelerate": 6,
            "decelerate": 7,
            "speeding": 8,
            "engine_overloading": 11,
            "fatigue": 12,
            "phone": 14,
            "distract": 16,
            "pcs": 19,
            "ldws": 20,
        }.items():
            result[f"{event_name}_onset_count_{window}s"] = float(
                _count_between(onsets_by_type.get(event_type, []), left_time, anchor)
            )
        result[f"speeding_observation_count_{window}s"] = float(
            _count_between(speeding_observations, left_time, anchor)
        )

    for event_name, event_type in {
        "accelerate": 6,
        "decelerate": 7,
        "speeding": 8,
        "engine_overloading": 11,
        "fatigue": 12,
        "phone": 14,
        "distract": 16,
        "pcs": 19,
        "ldws": 20,
    }.items():
        values = onsets_by_type.get(event_type, [])
        index = bisect_left(values, anchor) - 1
        last = values[index] if index >= 0 and values[index] >= anchor - timedelta(seconds=300) else None
        result[f"seconds_since_last_{event_name}_onset_300s"] = (
            (anchor - last).total_seconds() if last is not None else None
        )

    for window in (60, 300):
        left = bisect_left(trip.timestamps, anchor - timedelta(seconds=window), 0, right)
        for name, aggregations in {
            "engine_rpm": ("mean", "std", "max", "valid_count"),
            "engine_load": ("mean", "std", "max", "valid_count"),
            "coolant_temp": ("mean", "max", "valid_count"),
            "battery_mv": ("mean", "min", "valid_count"),
            "fuel_level": ("mean", "std", "valid_count"),
        }.items():
            values, times, out_of_range = _valid_values(trip, name, left, right)
            stats = _numeric_aggregations(values, times, out_of_range)
            for aggregation in aggregations:
                result[f"{name}_{aggregation}_{window}s"] = stats[aggregation]

        can_values, _, _ = _valid_values(trip, "can_status", left, right)
        result[f"can_status_normal_fraction_{window}s"] = (
            sum(value == 0 for value in can_values) / len(can_values) if can_values else None
        )
        result[f"can_status_abnormal_fraction_{window}s"] = (
            sum(value == 9 for value in can_values) / len(can_values) if can_values else None
        )
        result[f"can_status_valid_count_{window}s"] = float(len(can_values))

        status_values, _, _ = _valid_values(trip, "vehicle_status", left, right)
        for code, label in ((0, "parked"), (1, "driving"), (2, "idling")):
            result[f"vehicle_status_{label}_fraction_{window}s"] = (
                sum(value == code for value in status_values) / len(status_values)
                if status_values
                else None
            )

    result["elapsed_trip_seconds"] = (anchor - trip.start).total_seconds()
    result["history_coverage_seconds"] = float(history["coverage"])
    result["history_max_gap_seconds"] = float(history["max_gap"])
    result["vehicle_series_code"] = trip.vehicle_series_code
    return result, trip.timestamps[right - 1] if right > int(history["left"]) else None


def _schema(feature_names: list[str]) -> pa.Schema:
    fields = [pa.field("anchor_id", pa.string(), nullable=False)]
    for name in feature_names:
        fields.append(pa.field(name, pa.int16() if name == "vehicle_series_code" else pa.float64()))
    fields.extend(
        pa.field(name, pa.int8(), nullable=False)
        for name in (
            "label_any_core_risk_60s",
            "label_decelerate_60s",
            "label_pcs_60s",
            "label_ldws_60s",
            "label_fatigue_60s",
            "label_distract_60s",
        )
    )
    return pa.schema(fields)


METADATA_SCHEMA = pa.schema(
    [
        pa.field("anchor_id", pa.string(), nullable=False),
        pa.field("trip_id", pa.string(), nullable=False),
        pa.field("vehicle_id", pa.string(), nullable=False),
        pa.field("anchor_timestamp", pa.timestamp("s"), nullable=False),
        pa.field("temporal_split", pa.string(), nullable=False),
        pa.field("vehicle_holdout_assignment", pa.string(), nullable=False),
        pa.field("vehicle_series_code", pa.int16()),
        pa.field("history_coverage_seconds", pa.float64(), nullable=False),
        pa.field("history_point_count", pa.int32(), nullable=False),
        pa.field("history_max_gap_seconds", pa.float64(), nullable=False),
        pa.field("feature_source_max_timestamp", pa.timestamp("s")),
        pa.field("duplicate_extra_rows_in_trip", pa.int32(), nullable=False),
        pa.field("future_horizon_status", pa.string(), nullable=False),
        pa.field("timezone_semantics", pa.string(), nullable=False),
    ]
)

ELIGIBILITY_SCHEMA = pa.schema(
    [
        pa.field("anchor_id", pa.string(), nullable=False),
        pa.field("trip_id", pa.string(), nullable=False),
        pa.field("anchor_timestamp", pa.timestamp("s"), nullable=False),
        pa.field("temporal_split", pa.string(), nullable=False),
        pa.field("eligible", pa.bool_(), nullable=False),
        pa.field("exclusion_reason", pa.string()),
        pa.field("future_horizon_status", pa.string(), nullable=False),
        pa.field("embargo_purged", pa.bool_(), nullable=False),
    ]
)


class _Writers:
    def __init__(self, root: Path, schema: pa.Schema, batch_size: int):
        self.root, self.schema, self.batch_size = root, schema, batch_size
        self.dataset_writers: dict[str, pq.ParquetWriter] = {}
        self.metadata_writers: dict[str, pq.ParquetWriter] = {}
        self.dataset_buffers = {split: {name: [] for name in schema.names} for split in SPLITS}
        self.metadata_buffers = {split: {name: [] for name in METADATA_SCHEMA.names} for split in SPLITS}
        self.eligibility_buffer = {name: [] for name in ELIGIBILITY_SCHEMA.names}
        self.eligibility_writer: pq.ParquetWriter | None = None
        self.digests = {f"dataset_{split}": hashlib.sha256() for split in SPLITS}
        self.digests.update({f"metadata_{split}": hashlib.sha256() for split in SPLITS})
        self.digests["eligibility"] = hashlib.sha256()
        for split in SPLITS:
            dataset_path = root / "dataset" / f"split={split}" / "part-00000.parquet"
            metadata_path = root / "metadata" / f"split={split}" / "part-00000.parquet"
            dataset_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            self.dataset_writers[split] = pq.ParquetWriter(
                dataset_path, schema, compression="zstd", compression_level=3, use_dictionary=True
            )
            self.metadata_writers[split] = pq.ParquetWriter(
                metadata_path, METADATA_SCHEMA, compression="zstd", compression_level=3, use_dictionary=True
            )
        eligibility_path = root / "audit" / "anchor_eligibility.parquet"
        eligibility_path.parent.mkdir(parents=True, exist_ok=True)
        self.eligibility_writer = pq.ParquetWriter(
            eligibility_path, ELIGIBILITY_SCHEMA, compression="zstd", compression_level=3, use_dictionary=True
        )

    @staticmethod
    def _digest_table(table: pa.Table, digest: Any) -> None:
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            writer.write_table(table)
        digest.update(sink.getvalue().to_pybytes())

    def _flush(self, split: str) -> None:
        if not self.dataset_buffers[split]["anchor_id"]:
            return
        table = pa.Table.from_pydict(self.dataset_buffers[split], schema=self.schema)
        metadata = pa.Table.from_pydict(self.metadata_buffers[split], schema=METADATA_SCHEMA)
        self.dataset_writers[split].write_table(table)
        self.metadata_writers[split].write_table(metadata)
        self._digest_table(table, self.digests[f"dataset_{split}"])
        self._digest_table(metadata, self.digests[f"metadata_{split}"])
        self.dataset_buffers[split] = {name: [] for name in self.schema.names}
        self.metadata_buffers[split] = {name: [] for name in METADATA_SCHEMA.names}

    def add(self, split: str, dataset_row: dict[str, Any], metadata_row: dict[str, Any]) -> None:
        for name in self.schema.names:
            self.dataset_buffers[split][name].append(dataset_row.get(name))
        for name in METADATA_SCHEMA.names:
            self.metadata_buffers[split][name].append(metadata_row.get(name))
        if len(self.dataset_buffers[split]["anchor_id"]) >= self.batch_size:
            self._flush(split)

    def add_eligibility(self, row: dict[str, Any]) -> None:
        for name in ELIGIBILITY_SCHEMA.names:
            self.eligibility_buffer[name].append(row.get(name))
        if len(self.eligibility_buffer["anchor_id"]) >= self.batch_size:
            self._flush_eligibility()

    def _flush_eligibility(self) -> None:
        if not self.eligibility_buffer["anchor_id"]:
            return
        table = pa.Table.from_pydict(self.eligibility_buffer, schema=ELIGIBILITY_SCHEMA)
        assert self.eligibility_writer is not None
        self.eligibility_writer.write_table(table)
        self._digest_table(table, self.digests["eligibility"])
        self.eligibility_buffer = {name: [] for name in ELIGIBILITY_SCHEMA.names}

    def close(self) -> dict[str, str]:
        for split in SPLITS:
            self._flush(split)
            self.dataset_writers[split].close()
            self.metadata_writers[split].close()
        self._flush_eligibility()
        assert self.eligibility_writer is not None
        self.eligibility_writer.close()
        return {name: digest.hexdigest() for name, digest in self.digests.items()}


def _labels(
    anchor: datetime, onsets_by_type: dict[int, list[datetime]], leakage: LeakageEvidence
) -> dict[str, int]:
    horizon = anchor + timedelta(seconds=60)
    result: dict[str, int] = {}
    for name, event_type in CORE_EVENT_TYPES.items():
        values = onsets_by_type.get(event_type, [])
        positive = bisect_right(values, horizon) > bisect_right(values, anchor)
        result[f"label_{name}_60s"] = int(positive)
        if positive:
            leakage.checked_positive_labels += 1
            first = values[bisect_right(values, anchor)]
            if not (anchor < first <= horizon):
                leakage.label_boundary_violations += 1
    result["label_any_core_risk_60s"] = int(any(result.values()))
    return result


def _split_stat_template() -> dict[str, Any]:
    return {
        "row_count": 0,
        "positive_count": 0,
        "negative_count": 0,
        "event_positive_counts": Counter(),
        "trips": set(),
        "vehicles": set(),
        "vehicle_type_distribution": Counter(),
        "min_timestamp": None,
        "max_timestamp": None,
    }


def _public_split_stats(stats: dict[str, Any]) -> dict[str, Any]:
    rows = stats["row_count"]
    return {
        "row_count": rows,
        "positive_count": stats["positive_count"],
        "negative_count": stats["negative_count"],
        "positive_rate": stats["positive_count"] / rows if rows else None,
        "event_positive_counts": dict(sorted(stats["event_positive_counts"].items())),
        "unique_trip_count": len(stats["trips"]),
        "unique_vehicle_count": len(stats["vehicles"]),
        "vehicle_type_distribution": dict(sorted(stats["vehicle_type_distribution"].items())),
        "date_range": {
            "start": stats["min_timestamp"].isoformat() if stats["min_timestamp"] else None,
            "end": stats["max_timestamp"].isoformat() if stats["max_timestamp"] else None,
        },
    }


def _build_once(config: DatasetBuildConfig) -> dict[str, Any]:
    started = perf_counter()
    process = psutil.Process()
    input_hashes_before = _protected_hashes(config.repo_root)
    feature_config = load_feature_config(config.feature_config)
    feature_definitions = resolved_feature_spec(feature_config)
    feature_names = [feature.name for feature in feature_definitions if feature.model_inclusion]
    schema_audit = validate_model_schema(feature_names)
    if schema_audit["status"] != "PASS":
        raise RuntimeError(f"Forbidden model feature columns: {schema_audit['forbidden_columns']}")

    telemetry = _telemetry_table(config.repo_root)
    trips = list(_trip_slices(telemetry))
    onsets = _load_onsets(config.repo_root)
    speeding_observations = _load_speeding_observations(config.repo_root)
    holdout_map, holdout_private_rows = _vehicle_holdout_map(trips)

    staging = config.output_root.with_name(f".{config.output_root.name}-staging-{uuid4().hex}")
    staging.mkdir(parents=True)
    writers = _Writers(staging, _schema(feature_names), config.batch_size)
    exclusions = Counter()
    horizon_counts = Counter()
    split_stats = {split: _split_stat_template() for split in SPLITS}
    holdout_stats = {name: _split_stat_template() for name in ("development", "holdout")}
    leakage = LeakageEvidence(notes=["No Phase 2.5 analytics module or output is imported/read by this builder."])
    candidate_anchors = 0
    phase3a_eligible_before_split = 0
    final_eligible = 0

    for trip in trips:
        anchors = _anchors(trip.timestamps)
        candidate_anchors += len(anchors)
        split = _temporal_split(trip.start)
        trip_onsets = [event for event in onsets.get(trip.trip_id, []) if trip.start <= event.start <= trip.end]
        onsets_by_type: dict[int, list[datetime]] = defaultdict(list)
        active_intervals: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
        for event in trip_onsets:
            onsets_by_type[event.event_type].append(event.start)
            if event.active_end is not None:
                active_intervals[event.event_type].append((event.start, event.active_end))
        for values in onsets_by_type.values():
            values.sort()
        for values in active_intervals.values():
            values.sort()

        trip_reason = _trip_quality_reason(trip)

        for anchor in anchors:
            anchor_key = _anchor_id(trip.trip_id, anchor)
            history = _history_diagnostics(trip.timestamps, anchor)
            future_status = _future_status(trip.timestamps, anchor)
            horizon_counts[future_status] += 1
            reason = trip_reason
            if reason is None and float(history["coverage"]) < 240:
                reason = "HISTORY_COVERAGE_LT_240S"
            elif reason is None and int(history["point_count"]) < 10:
                reason = "HISTORY_POINTS_LT_10"
            elif reason is None and float(history["max_gap"]) > 60:
                reason = "HISTORY_MAX_GAP_GT_60S"
            elif reason is None and future_status == "censored":
                reason = "FUTURE_HORIZON_CENSORED"
            elif reason is None and future_status == "ambiguous":
                reason = "FUTURE_HORIZON_AMBIGUOUS"
            if reason is None:
                active = _active_core_at_anchor(active_intervals, anchor)
                if active:
                    reason = "CORE_EVENT_ACTIVE_AT_ANCHOR"
            if reason is None:
                phase3a_eligible_before_split += 1
                if _in_embargo(anchor):
                    reason = "TEMPORAL_SPLIT_EMBARGO"

            eligible = reason is None
            if not eligible:
                exclusions[reason] += 1
            writers.add_eligibility(
                {
                    "anchor_id": anchor_key,
                    "trip_id": trip.trip_id,
                    "anchor_timestamp": anchor,
                    "temporal_split": split,
                    "eligible": eligible,
                    "exclusion_reason": reason,
                    "future_horizon_status": future_status,
                    "embargo_purged": reason == "TEMPORAL_SPLIT_EMBARGO",
                }
            )
            if not eligible:
                continue

            labels = _labels(anchor, onsets_by_type, leakage)
            available_past_onsets = _available_past_onsets(trip_onsets, anchor)
            features, telemetry_source_max = _features(
                trip,
                anchor,
                available_past_onsets,
                speeding_observations.get(trip.trip_id, []),
                history,
            )
            max_source_timestamp = _complete_feature_source_max(
                anchor,
                telemetry_source_max,
                trip_onsets,
                speeding_observations.get(trip.trip_id, []),
            )
            if set(features) != set(feature_names):
                missing = sorted(set(feature_names) - set(features))
                extra = sorted(set(features) - set(feature_names))
                raise RuntimeError(f"Feature/spec mismatch; missing={missing}, extra={extra}")
            leakage.checked_feature_rows += 1
            if max_source_timestamp is not None:
                if max_source_timestamp >= anchor:
                    leakage.feature_source_timestamp_violations += 1
                    leakage.future_telemetry_feature_violations += 1
                delta = (anchor - max_source_timestamp).total_seconds()
                leakage.minimum_anchor_minus_feature_seconds = (
                    delta
                    if leakage.minimum_anchor_minus_feature_seconds is None
                    else min(leakage.minimum_anchor_minus_feature_seconds, delta)
                )
                leakage.maximum_feature_timestamp = max(
                    max_source_timestamp,
                    leakage.maximum_feature_timestamp or max_source_timestamp,
                )
            if any(
                event_time >= anchor
                for values in available_past_onsets.values()
                for event_time in values
            ):
                leakage.current_event_feature_violations += 1
            if _in_embargo(anchor) or _crosses_split_boundary(trip.start, trip.end):
                leakage.split_boundary_violations += 1
            dataset_row = {"anchor_id": anchor_key, **features, **labels}
            metadata_row = {
                "anchor_id": anchor_key,
                "trip_id": trip.trip_id,
                "vehicle_id": trip.vehicle_id,
                "anchor_timestamp": anchor,
                "temporal_split": split,
                "vehicle_holdout_assignment": holdout_map[trip.vehicle_id],
                "vehicle_series_code": trip.vehicle_series_code,
                "history_coverage_seconds": float(history["coverage"]),
                "history_point_count": int(history["point_count"]),
                "history_max_gap_seconds": float(history["max_gap"]),
                "feature_source_max_timestamp": max_source_timestamp,
                "duplicate_extra_rows_in_trip": trip.duplicate_extra_rows,
                "future_horizon_status": future_status,
                "timezone_semantics": "UNRESOLVED_NO_LOCAL_TIME_FEATURES",
            }
            writers.add(split, dataset_row, metadata_row)
            final_eligible += 1

            for stats in (split_stats[split], holdout_stats[holdout_map[trip.vehicle_id]]):
                stats["row_count"] += 1
                stats["positive_count"] += labels["label_any_core_risk_60s"]
                stats["negative_count"] += 1 - labels["label_any_core_risk_60s"]
                for event_name in CORE_EVENT_TYPES:
                    stats["event_positive_counts"][event_name] += labels[f"label_{event_name}_60s"]
                stats["trips"].add(trip.trip_id)
                stats["vehicles"].add(trip.vehicle_id)
                stats["vehicle_type_distribution"][str(trip.vehicle_series_code)] += 1
                stats["min_timestamp"] = min(anchor, stats["min_timestamp"] or anchor)
                stats["max_timestamp"] = max(anchor, stats["max_timestamp"] or anchor)

    semantic_digests = writers.close()
    if phase3a_eligible_before_split != 209_154:
        raise RuntimeError(
            f"Phase 3A reconciliation failed: {phase3a_eligible_before_split} != 209154"
        )

    split_public = {split: _public_split_stats(stats) for split, stats in split_stats.items()}
    holdout_public = {
        assignment: _public_split_stats(stats) for assignment, stats in holdout_stats.items()
    }
    _write_json(
        staging / "splits" / "temporal_split_manifest.json",
        {
            "strategy": "whole_trip_start_calendar_split_v1",
            "train_end_inclusive": TRAIN_END.isoformat(),
            "validation_end_inclusive": VALIDATION_END.isoformat(),
            "embargo_policy": "purge anchors in [boundary-60s,boundary+300s]",
            "purged_anchor_count": exclusions["TEMPORAL_SPLIT_EMBARGO"],
            "splits": split_public,
        },
    )
    holdout_table = pa.Table.from_pylist(
        holdout_private_rows,
        schema=pa.schema(
            [
                pa.field("vehicle_id", pa.string(), nullable=False),
                pa.field("vehicle_series_code", pa.int16()),
                pa.field("assignment", pa.string(), nullable=False),
                pa.field("selection_rank", pa.int16(), nullable=False),
                pa.field("selection_method", pa.string(), nullable=False),
            ]
        ),
    )
    (staging / "splits").mkdir(parents=True, exist_ok=True)
    pq.write_table(
        holdout_table,
        staging / "splits" / "vehicle_holdout_manifest.parquet",
        compression="zstd",
        compression_level=3,
    )
    _write_json(
        staging / "splits" / "vehicle_holdout_audit.json",
        {
            "strategy": "target-blind SHA-256 rank with one vehicle per observed vehicle type, then fill to 20%",
            "vehicle_count": len(holdout_private_rows),
            "holdout_vehicle_count": sum(row["assignment"] == "holdout" for row in holdout_private_rows),
            "development_vehicle_count": sum(row["assignment"] == "development" for row in holdout_private_rows),
            "aggregate_support": holdout_public,
            "private_identifiers_in_this_json": False,
            "private_mapping_location": "vehicle_holdout_manifest.parquet (Git-ignored)",
        },
    )
    _write_json(
        staging / "metadata" / "resolved_feature_spec.json",
        {
            "feature_spec_version": feature_config["feature_spec_version"],
            "feature_count": len(feature_names),
            "metadata_columns": METADATA_SCHEMA.names,
            "linkage_columns_not_model_features": ["anchor_id"],
            "feature_columns": feature_names,
            "label_columns": [name for name in _schema(feature_names).names if name.startswith("label_")],
            "features": [definition.as_dict() for definition in feature_definitions],
            "excluded_candidate": feature_config["speed_limit_candidates"],
        },
    )
    dataset_files = {
        split: staging / "dataset" / f"split={split}" / "part-00000.parquet"
        for split in SPLITS
    }
    availability, target_screen, constant_report = audit_feature_files(dataset_files, feature_names)
    _write_json(staging / "audit" / "feature_availability_audit.json", availability)
    _write_json(staging / "audit" / "target_leakage_screen.json", target_screen)
    _write_json(staging / "audit" / "constant_feature_report.json", constant_report)
    leakage_report = {
        "status": "PASS" if leakage.passed and target_screen["status"] == "PASS" else "FAIL",
        "contract": {"input": "[t-300s,t)", "label": "(t,t+60s]"},
        "feature_source_timestamp_violations": leakage.feature_source_timestamp_violations,
        "label_boundary_violations": leakage.label_boundary_violations,
        "current_event_feature_violations": leakage.current_event_feature_violations,
        "future_telemetry_feature_violations": leakage.future_telemetry_feature_violations,
        "split_boundary_violations": leakage.split_boundary_violations,
        "checked_feature_rows": leakage.checked_feature_rows,
        "checked_positive_event_labels": leakage.checked_positive_labels,
        "minimum_anchor_minus_feature_seconds": leakage.minimum_anchor_minus_feature_seconds,
        "post_trip_dependency_present": False,
        "target_leakage_screen_status": target_screen["status"],
        "notes": leakage.notes,
    }
    privacy_report = {
        **schema_audit,
        "status": "PASS" if schema_audit["status"] == "PASS" else "FAIL",
        "model_dataset_contains_private_identifier": False,
        "model_dataset_contains_coordinates": False,
        "private_identifiers_restricted_to_gitignored_metadata": True,
        "raw_source_rows_written": False,
    }
    _write_json(staging / "audit" / "dataset_leakage_audit.json", leakage_report)
    _write_json(staging / "audit" / "privacy_audit.json", privacy_report)
    _write_json(
        staging / "audit" / "eligibility_summary.json",
        {
            "candidate_anchors": candidate_anchors,
            "phase3a_eligible_before_split": phase3a_eligible_before_split,
            "final_eligible_anchors": final_eligible,
            "excluded_anchor_count": candidate_anchors - final_eligible,
            "exclusion_reasons": dict(sorted(exclusions.items())),
            "future_horizon_status_counts_before_other_exclusions": dict(sorted(horizon_counts.items())),
        },
    )

    input_hashes_after = _protected_hashes(config.repo_root)
    protected_unchanged = input_hashes_before == input_hashes_after
    output_files = [path for path in staging.rglob("*") if path.is_file()]
    output_size = sum(path.stat().st_size for path in output_files)
    elapsed = perf_counter() - started
    status = (
        "READY"
        if leakage_report["status"] == "PASS"
        and privacy_report["status"] == "PASS"
        and protected_unchanged
        and phase3a_eligible_before_split == 209_154
        else "INVALID"
    )
    positives = sum(stats["positive_count"] for stats in split_stats.values())
    manifest = {
        "dataset_version": DATASET_VERSION,
        "status": status,
        "model_training_performed": False,
        "external_api_calls": 0,
        "source_versions": {
            "phase2_etl": _json(config.repo_root / "data/interim/audit/etl_run_manifest.json")["etl_version"],
            "phase3a_label_research": _json(config.repo_root / "data/processed/label_research/label_research_manifest.json")["research_version"],
        },
        "feature_spec_version": feature_config["feature_spec_version"],
        "label_spec_version": LABEL_SPEC_VERSION,
        "dataset_contract": {"input": "[t-300s,t)", "label": "(t,t+60s]"},
        "anchor_count": final_eligible,
        "candidate_anchor_count": candidate_anchors,
        "excluded_count": candidate_anchors - final_eligible,
        "positive_count": positives,
        "negative_count": final_eligible - positives,
        "positive_rate": positives / final_eligible if final_eligible else None,
        "feature_count": len(feature_names),
        "train_validation_test_counts": {
            split: split_stats[split]["row_count"] for split in SPLITS
        },
        "build_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "build_runtime_seconds": elapsed,
        "peak_rss_bytes_approximate": process.memory_info().rss,
        "code_version": _git_info(config.repo_root),
        "privacy_status": privacy_report["status"],
        "leakage_audit_status": leakage_report["status"],
        "protected_inputs_unchanged": protected_unchanged,
        "semantic_content_digests": semantic_digests,
        "parquet_layout": {
            "dataset": "dataset/split={train|validation|test}/part-00000.parquet",
            "metadata": "metadata/split={train|validation|test}/part-00000.parquet",
            "compression": "zstd level 3",
            "parquet_file_count": sum(path.suffix == ".parquet" for path in output_files),
            "total_output_bytes_before_manifest": output_size,
        },
        "dataset_versions_reserved": [
            "v0_hino",
            "v1_hino_traffic",
            "v2_hino_traffic_weather",
            "v3_full_context",
        ],
        "unresolved": [
            "Official telemetry timezone",
            "GPS speed-limit unit and zero semantics",
            "Ultra-long journey root cause and permanent policy",
            "Final unseen-vehicle evaluation protocol after baseline feasibility review",
            "Historical external-context availability and event-time join semantics",
        ],
    }
    _write_json(staging / "dataset_manifest.json", manifest)
    staging.replace(config.output_root)
    return manifest


def build_dataset(config: DatasetBuildConfig, deterministic_rebuild: bool = False) -> dict[str, Any]:
    overall_started = perf_counter()
    first = _build_once(config)
    if not deterministic_rebuild:
        return first
    check_root = config.output_root.with_name(f".{config.output_root.name}-determinism-{uuid4().hex}")
    check_config = DatasetBuildConfig(
        repo_root=config.repo_root,
        output_root=check_root,
        feature_config=config.feature_config,
        batch_size=config.batch_size,
    )
    second = _build_once(check_config)
    matched = first["semantic_content_digests"] == second["semantic_content_digests"]
    deterministic_report = {
        "status": "PASS" if matched else "FAIL",
        "semantic_content_digests_match": matched,
        "first": first["semantic_content_digests"],
        "second": second["semantic_content_digests"],
        "first_build_runtime_seconds": first["build_runtime_seconds"],
        "second_build_runtime_seconds": second["build_runtime_seconds"],
        "total_rebuild_check_runtime_seconds": perf_counter() - overall_started,
        "note": "Build timestamps/runtime and physical Parquet bytes are excluded from semantic determinism.",
    }
    _write_json(config.output_root / "audit" / "deterministic_rebuild.json", deterministic_report)
    resolved = check_root.resolve()
    allowed = (config.repo_root / "data" / "processed" / "ml").resolve()
    if allowed not in resolved.parents or not resolved.name.startswith(f".{config.output_root.name}-determinism-"):
        raise RuntimeError(f"Refusing unsafe deterministic-check cleanup: {resolved}")
    shutil.rmtree(resolved)
    if not matched:
        manifest_path = config.output_root / "dataset_manifest.json"
        manifest = _json(manifest_path)
        manifest["status"] = "INVALID"
        manifest["deterministic_rebuild_status"] = "FAIL"
        _write_json(manifest_path, manifest)
        return manifest
    manifest_path = config.output_root / "dataset_manifest.json"
    manifest = _json(manifest_path)
    manifest["deterministic_rebuild_status"] = "PASS"
    _write_json(manifest_path, manifest)
    return manifest
