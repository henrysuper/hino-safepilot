"""Explicit source parsing specifications and Arrow output schemas."""

from __future__ import annotations

import pyarrow as pa

from .parsing import NumericSpec

BASE_NUMERIC_SPECS = (
    NumericSpec("Type", "vehicle_series_code", "int", allowed_values=frozenset({300, 500, 700})),
    NumericSpec("carStatus", "vehicle_status", "int", allowed_values=frozenset({0, 1, 2})),
    NumericSpec("gps.longitude", "longitude_deg", "float", -180.0, 180.0),
    NumericSpec("gps.latitude", "latitude_deg", "float", -90.0, 90.0),
    NumericSpec("gps.speed", "gps_speed_kph", "float", 0.0, 255.9),
    NumericSpec("gps.azimuth", "gps_azimuth_deg", "float", 0.0, 359.9),
    NumericSpec("gps.speedLimit", "gps_speed_limit", "float"),
    NumericSpec("can.canStatus", "can_status", "int", allowed_values=frozenset({0, 9})),
    NumericSpec("can.totalMileage", "total_mileage_km", "float", 0.0, 21_055_406.0),
    NumericSpec("can.canSpeed", "can_speed_kph", "float", 0.0, 255.0),
    NumericSpec("line.fuelLevel", "fuel_level_pct", "float", 0.0, 100.0),
    NumericSpec("line.battery", "battery_mv", "float", 0.0, 30_000.0),
    NumericSpec(
        "can.engine.engineCoolantTemp", "engine_coolant_temp_c", "float", -40.0, 210.0
    ),
    NumericSpec(
        "can.engine.totalFuelUsed", "total_fuel_used_l", "float", 0.0, 4_211_081.215
    ),
    NumericSpec(
        "can.engine.engineTotalTime", "engine_total_time_h", "float", 0.0, 210_554_060.75
    ),
    NumericSpec("can.engine.rpm", "engine_rpm", "float", 0.0, 8_031.875),
    NumericSpec("can.engine.engineLoad", "engine_load_pct", "float", 0.0, 250.0),
)

BASE_NUMERIC_SPEC_BY_SOURCE = {spec.source_name: spec for spec in BASE_NUMERIC_SPECS}

EVENT_TYPE_SPEC = NumericSpec(
    "event.type", "event_type", "int", allowed_values=frozenset(range(1, 21))
)

EVENT_PAYLOAD_SPECS = {
    "duration": NumericSpec("duration", "event_duration_s", "int", 0.0, None),
    "speed_difference": NumericSpec(
        "speedDifference", "event_speed_difference", "float", 0.0, 255.0
    ),
    "speed_limit": NumericSpec("speedLimit", "event_speed_limit_kph", "float"),
    "speed": NumericSpec("speed", "event_speed_kph", "float"),
    "dtc_count": NumericSpec("dtcCount", "event_dtc_count", "int", 0.0, 255.0),
    "water_temp": NumericSpec("waterTemp", "event_water_temp_c", "float", -40.0, 210.0),
    "engine_load": NumericSpec("engineLoad", "event_engine_load_pct", "float", 0.0, 100.0),
}

_INT_NAMES = {"vehicle_series_code", "vehicle_status", "can_status"}


def _base_numeric_arrow_type(name: str) -> pa.DataType:
    if name == "vehicle_series_code":
        return pa.int16()
    if name in {"vehicle_status", "can_status"}:
        return pa.int8()
    if name in {
        "total_mileage_km",
        "total_fuel_used_l",
        "engine_total_time_h",
    }:
        return pa.float64()
    return pa.float32()


def _parsed_fields(spec: NumericSpec, value_type: pa.DataType) -> list[pa.Field]:
    return [
        pa.field(f"{spec.normalized_name}_raw", pa.string()),
        pa.field(spec.normalized_name, value_type),
        pa.field(f"{spec.normalized_name}_quality", pa.string(), nullable=False),
    ]


def telemetry_schema() -> pa.Schema:
    fields = [
        pa.field("source_file_name", pa.string(), nullable=False),
        pa.field("source_file_sha256", pa.string(), nullable=False),
        pa.field("source_sheet", pa.string(), nullable=False),
        pa.field("source_row_number", pa.int64(), nullable=False),
        pa.field("source_row_hash", pa.string(), nullable=False),
    ]
    fields.extend(_parsed_fields(BASE_NUMERIC_SPECS[0], pa.int16()))
    fields.extend(
        [
            pa.field("journey_code", pa.string()),
            pa.field("enabled_code", pa.string()),
            pa.field("trip_id", pa.string()),
            pa.field("license_plate", pa.string()),
            pa.field("journey_key_quality", pa.string(), nullable=False),
            pa.field("timestamp_raw", pa.string()),
            pa.field("timestamp", pa.timestamp("s")),
            pa.field("timestamp_quality", pa.string(), nullable=False),
        ]
    )
    fields.extend(_parsed_fields(BASE_NUMERIC_SPECS[1], pa.int8()))
    for spec in BASE_NUMERIC_SPECS[2:]:
        fields.extend(_parsed_fields(spec, _base_numeric_arrow_type(spec.normalized_name)))
    fields.extend(
        [
            pa.field("fence_ids_raw", pa.string()),
            pa.field("fence_ids", pa.list_(pa.string())),
            pa.field("fence_ids_quality", pa.string(), nullable=False),
            pa.field("gps_coordinates_missing", pa.bool_(), nullable=False),
            pa.field("gps_speed_out_of_range", pa.bool_(), nullable=False),
            pa.field("azimuth_out_of_range", pa.bool_(), nullable=False),
            pa.field("row_has_malformed_value", pa.bool_(), nullable=False),
            pa.field("row_has_out_of_range_value", pa.bool_(), nullable=False),
            pa.field("is_duplicate_source_key", pa.bool_(), nullable=False),
            pa.field("duplicate_group_size", pa.int32(), nullable=False),
            pa.field("duplicate_rows_identical", pa.bool_()),
            pa.field("year_month", pa.string(), nullable=False),
        ]
    )
    schema = pa.schema(fields)
    if len(schema.names) != len(set(schema.names)):
        raise ValueError("Duplicate normalized telemetry columns detected.")
    return schema


def event_observation_schema() -> pa.Schema:
    fields = [
        pa.field("source_file_name", pa.string(), nullable=False),
        pa.field("source_file_sha256", pa.string(), nullable=False),
        pa.field("source_sheet", pa.string(), nullable=False),
        pa.field("telemetry_source_row_number", pa.int64(), nullable=False),
        pa.field("telemetry_source_row_hash", pa.string(), nullable=False),
        pa.field("enabled_code", pa.string()),
        pa.field("journey_code", pa.string()),
        pa.field("trip_id", pa.string()),
        pa.field("telemetry_timestamp_raw", pa.string()),
        pa.field("telemetry_timestamp", pa.timestamp("s")),
        pa.field("telemetry_timestamp_quality", pa.string(), nullable=False),
        pa.field("event_slot", pa.int8(), nullable=False),
        pa.field("event_type_raw", pa.string()),
        pa.field("event_type", pa.int8()),
        pa.field("event_type_quality", pa.string(), nullable=False),
        pa.field("event_name", pa.string()),
        pa.field("event_start_time_raw", pa.string()),
        pa.field("event_start_time", pa.timestamp("s")),
        pa.field("event_start_time_quality", pa.string(), nullable=False),
        pa.field("event_start_to_telemetry_seconds", pa.float64()),
        pa.field("event_start_year_differs_from_telemetry", pa.bool_(), nullable=False),
        pa.field("event_start_after_telemetry", pa.bool_(), nullable=False),
        pa.field("event_end_time_raw", pa.string()),
        pa.field("event_end_time", pa.timestamp("s")),
        pa.field("event_end_time_quality", pa.string(), nullable=False),
    ]
    for key, spec in EVENT_PAYLOAD_SPECS.items():
        value_type: pa.DataType = pa.int32() if spec.target == "int" else pa.float32()
        fields.extend(_parsed_fields(spec, value_type))
    fields.extend(
        [
            pa.field("event_dtc_code_0_raw", pa.string()),
            pa.field("event_dtc_code_0_quality", pa.string(), nullable=False),
            pa.field("is_duplicate_telemetry_key", pa.bool_(), nullable=False),
            pa.field("duplicate_telemetry_group_size", pa.int32(), nullable=False),
            pa.field("event_observation_has_quality_issue", pa.bool_(), nullable=False),
            pa.field("year_month", pa.string(), nullable=False),
        ]
    )
    schema = pa.schema(fields)
    if len(schema.names) != len(set(schema.names)):
        raise ValueError("Duplicate normalized event columns detected.")
    return schema


def event_onset_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field("source_file_name", pa.string(), nullable=False),
            pa.field("source_file_sha256", pa.string(), nullable=False),
            pa.field("source_sheet", pa.string(), nullable=False),
            pa.field("event_onset_candidate_id", pa.string(), nullable=False),
            pa.field("enabled_code", pa.string()),
            pa.field("journey_code", pa.string()),
            pa.field("trip_id", pa.string()),
            pa.field("event_type", pa.int8()),
            pa.field("event_name", pa.string()),
            pa.field("event_start_time_raw", pa.string()),
            pa.field("event_start_time", pa.timestamp("s")),
            pa.field("event_start_time_quality", pa.string(), nullable=False),
            pa.field("first_observed_source_row", pa.int64(), nullable=False),
            pa.field("last_observed_source_row", pa.int64(), nullable=False),
            pa.field("observation_count", pa.int32(), nullable=False),
            pa.field("first_observed_telemetry_timestamp", pa.timestamp("s")),
            pa.field("last_observed_telemetry_timestamp", pa.timestamp("s")),
            pa.field("slots_observed", pa.list_(pa.int8()), nullable=False),
            pa.field("start_raw_variant_count", pa.int32(), nullable=False),
            pa.field("end_time_variant_count", pa.int32(), nullable=False),
            pa.field("payload_variant_count", pa.int32(), nullable=False),
            pa.field("end_time_conflict", pa.bool_(), nullable=False),
            pa.field("payload_conflict", pa.bool_(), nullable=False),
            pa.field("start_time_ambiguous", pa.bool_(), nullable=False),
            pa.field("start_year_mismatch_observed", pa.bool_(), nullable=False),
            pa.field("start_after_telemetry_observed", pa.bool_(), nullable=False),
            pa.field("has_observation_quality_issue", pa.bool_(), nullable=False),
            pa.field("year_month", pa.string(), nullable=False),
        ]
    )


def schema_snapshot(schema: pa.Schema) -> list[dict[str, object]]:
    return [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in schema
    ]
