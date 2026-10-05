"""Organizer-confirmed source schema and ETL V0 constants."""

from __future__ import annotations

SOURCE_SHEET = "output"
MAPPING_SHEET = "欄位對照表"
EXPECTED_SOURCE_ROWS = 493_744
EXPECTED_SOURCE_COLUMNS = 55
EXPECTED_SOURCE_DIMENSION = "A1:BC493745"
EXPECTED_MAPPING_DIMENSION = "B1:H82"
DEFAULT_SOURCE_RELATIVE_PATH = "data/raw/hino_telemetry_20260511.xlsx"
DEFAULT_MANIFEST_RELATIVE_PATH = "docs/data_manifest.md"
DEFAULT_OUTPUT_RELATIVE_PATH = "data/interim"
DEFAULT_BATCH_SIZE = 10_000
DEFAULT_COMPRESSION = "zstd"
DEFAULT_COMPRESSION_LEVEL = 3

EXPECTED_HEADERS = (
    "Type",
    "journeyCode",
    "time",
    "carStatus",
    "enabledCode",
    "carNum",
    "gps.longitude",
    "gps.latitude",
    "gps.speed",
    "gps.azimuth",
    "gps.speedLimit",
    "can.canStatus",
    "can.totalMileage",
    "can.canSpeed",
    "line.fuelLevel",
    "line.battery",
    "can.engine.engineCoolantTemp",
    "can.engine.totalFuelUsed",
    "can.engine.engineTotalTime",
    "can.engine.rpm",
    "can.engine.engineLoad",
    "fenceId",
    "event[0].type",
    "event[0].startTime",
    "event[0].endTime",
    "event[0].info.duration",
    "event[0].info.speedDifference",
    "event[0].info.speedLimit",
    "event[0].info.speed",
    "event[0].info.dtcCount",
    "event[0].info.waterTemp",
    "event[0].info.engineLoad",
    "event[1].type",
    "event[1].startTime",
    "event[1].endTime",
    "event[1].info.duration",
    "event[1].info.speedDifference",
    "event[1].info.speedLimit",
    "event[1].info.speed",
    "event[1].info.dtcCount",
    "event[1].info.waterTemp",
    "event[1].info.engineLoad",
    "event[2].type",
    "event[2].startTime",
    "event[2].endTime",
    "event[2].info.duration",
    "event[2].info.speedDifference",
    "event[2].info.speedLimit",
    "event[2].info.speed",
    "event[2].info.dtcCount",
    "event[2].info.waterTemp",
    "event[2].info.engineLoad",
    "event[0].info.dtcCodes[0]",
    "event[1].info.dtcCodes[0]",
    "event[2].info.dtcCodes[0]",
)

EVENT_NAMES = {
    1: "driving",
    2: "idle",
    3: "parked",
    4: "engine_on",
    5: "fence",
    6: "accelerate",
    7: "decelerate",
    8: "speeding",
    9: "dtc",
    10: "engine_overheat",
    11: "engine_overloading",
    12: "fatigue",
    13: "seatbelt",
    14: "phone",
    15: "smoking",
    16: "distract",
    17: "lens_ward_off",
    18: "driver_lost",
    19: "pcs",
    20: "ldws",
}

EVENT_FIELDS_WITH_END = frozenset({2, 5, 8, 10, 11})
EVENT_FIELD_APPLICABILITY = {
    "duration": frozenset({2, 5, 8, 10, 11}),
    "speed_difference": frozenset({6, 7}),
    "speed_limit": frozenset({8}),
    "speed": frozenset({8}),
    "dtc_count": frozenset({9}),
    "water_temp": frozenset({10}),
    "engine_load": frozenset({11}),
    "dtc_code_0": frozenset({9}),
}

