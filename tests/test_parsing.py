from __future__ import annotations

from hino_safepilot.etl.parsing import (
    NumericSpec,
    canonical_trip_id,
    parse_numeric,
    parse_timestamp,
)


def test_numeric_parser_distinguishes_missing_malformed_and_range() -> None:
    spec = NumericSpec("speed", "speed", "float", 0, 100)
    assert parse_numeric(None, spec).quality == "missing_unknown"
    assert parse_numeric("", spec, missing_kind="structural").quality == "missing_structural"
    assert parse_numeric("not-a-number", spec).quality == "malformed"
    parsed = parse_numeric("477", spec)
    assert parsed.quality == "out_of_range"
    assert parsed.value == 477.0
    assert parsed.raw == "477"


def test_timestamp_is_naive_and_malformed_is_preserved() -> None:
    valid = parse_timestamp("2025-01-01 01:02:03")
    assert valid.quality == "valid"
    assert valid.value is not None and valid.value.tzinfo is None
    malformed = parse_timestamp("UTC? not parseable")
    assert malformed.quality == "malformed"
    assert malformed.value is None
    assert malformed.raw == "UTC? not parseable"


def test_trip_id_uses_vehicle_and_journey() -> None:
    first = canonical_trip_id("SYNTH00001", "250101000001")
    second = canonical_trip_id("SYNTH00002", "250101000001")
    assert first and first.startswith("trip_")
    assert first != second
    assert first == canonical_trip_id("SYNTH00001", "250101000001")

