from __future__ import annotations

import json
from pathlib import Path

import pyarrow.dataset as ds

from hino_safepilot.etl.pipeline import EtlConfig, run_etl
from hino_safepilot.etl.source import sha256_file


def _config(source: Path, output: Path, row_count: int) -> EtlConfig:
    return EtlConfig(
        repo_root=source.parent,
        source_path=source,
        output_root=output,
        expected_sha256=sha256_file(source),
        expected_rows=row_count,
        expected_source_dimension=f"A1:BC{row_count + 1}",
        batch_size=3,
        progress_every_rows=100,
        enforce_output_policy=False,
    )


def _table(root: Path):
    return ds.dataset(root, format="parquet").to_table()


def test_pipeline_preserves_rows_unnests_events_and_flags_quality(
    synthetic_workbook: tuple[Path, int], tmp_path: Path
) -> None:
    source, row_count = synthetic_workbook
    source_hash = sha256_file(source)
    result = run_etl(_config(source, tmp_path / "run", row_count))
    assert result.input_rows == result.telemetry_rows == row_count
    assert result.event_observations == 6
    assert result.event_onset_candidates == 4
    assert result.duplicate_group_count == 2
    assert result.source_hash_before == result.source_hash_after == source_hash
    assert sha256_file(source) == source_hash

    telemetry = _table(result.output_paths["telemetry_normalized"]).to_pylist()
    assert sum(row["gps_coordinates_missing"] for row in telemetry) == 1
    assert sum(row["gps_speed_out_of_range"] for row in telemetry) == 1
    assert sum(row["azimuth_out_of_range"] for row in telemetry) == 1
    assert sum(row["row_has_malformed_value"] for row in telemetry) == 2
    duplicate_rows = [row for row in telemetry if row["is_duplicate_source_key"]]
    assert len(duplicate_rows) == 4
    assert {row["duplicate_rows_identical"] for row in duplicate_rows} == {True, False}

    same_journey = [row for row in telemetry if row["journey_code"] == "250101000001"]
    trip_ids_by_vehicle = {
        row["enabled_code"]: row["trip_id"] for row in same_journey
    }
    assert trip_ids_by_vehicle["SYNTH00001"] != trip_ids_by_vehicle["SYNTH00002"]

    events = _table(result.output_paths["events_observed"]).to_pylist()
    assert {event["event_slot"] for event in events} == {0, 1, 2}
    dtc_events = [event for event in events if event["event_type"] == 9]
    assert len(dtc_events) == 1
    assert dtc_events[0]["event_dtc_code_0_raw"] == "PTEST0"
    assert all(event["event_name"] != "hard_braking" for event in events)
    assert any(event["event_name"] == "decelerate" for event in events)
    assert not any(event["event_start_year_differs_from_telemetry"] for event in events)
    assert not any(event["event_start_after_telemetry"] for event in events)

    onsets = _table(result.output_paths["events_onsets"]).to_pylist()
    speeding = [row for row in onsets if row["event_type"] == 8]
    assert len(speeding) == 1
    assert speeding[0]["observation_count"] == 3
    assert speeding[0]["payload_conflict"] is True
    assert speeding[0]["end_time_conflict"] is True

    quality = json.loads(
        (result.output_paths["audit"] / "quality_summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert quality["quarantined_row_count"] == 0
    assert quality["identical_duplicate_group_count"] == 1


def test_semantic_rerun_is_deterministic(
    synthetic_workbook: tuple[Path, int], tmp_path: Path
) -> None:
    source, row_count = synthetic_workbook
    first = run_etl(_config(source, tmp_path / "run_a", row_count))
    second = run_etl(_config(source, tmp_path / "run_b", row_count))
    assert (
        first.telemetry_rows,
        first.event_observations,
        first.event_onset_candidates,
        first.malformed_cells,
        first.duplicate_group_count,
    ) == (
        second.telemetry_rows,
        second.event_observations,
        second.event_onset_candidates,
        second.malformed_cells,
        second.duplicate_group_count,
    )
    for name in ("quality_summary.json", "schema_snapshot.json"):
        left = json.loads((first.output_paths["audit"] / name).read_text(encoding="utf-8"))
        right = json.loads((second.output_paths["audit"] / name).read_text(encoding="utf-8"))
        assert left == right
    left_onsets = _table(first.output_paths["events_onsets"])
    right_onsets = _table(second.output_paths["events_onsets"])
    assert left_onsets.schema == right_onsets.schema
    assert sorted(left_onsets.column("event_onset_candidate_id").to_pylist()) == sorted(
        right_onsets.column("event_onset_candidate_id").to_pylist()
    )
