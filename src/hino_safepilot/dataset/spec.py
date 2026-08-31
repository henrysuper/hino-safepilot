"""Expand the reviewed compact feature configuration into explicit columns."""

from __future__ import annotations

import json
from pathlib import Path

from .models import FeatureDefinition


def load_feature_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("dataset_version") != "v0_hino":
        raise ValueError("Unsupported dataset version")
    if config.get("input_interval") != "[t-300s,t)":
        raise ValueError("V0 input boundary must remain [t-300s,t)")
    if config.get("label_interval") != "(t,t+60s]":
        raise ValueError("V0 label boundary must remain (t,t+60s]")
    return config


def _windows(config: dict, selector: str) -> list[int]:
    key = "windows_seconds" if selector == "all" else "engine_windows_seconds"
    return [int(value) for value in config[key]]


def resolved_feature_spec(config: dict) -> list[FeatureDefinition]:
    result: list[FeatureDefinition] = []
    for window in config["windows_seconds"]:
        result.extend(
            [
                FeatureDefinition(
                    f"telemetry_point_count_{window}s",
                    "exposure",
                    "telemetry.timestamp",
                    window,
                    "count",
                    "float64",
                    "past_only",
                    "unique timestamps in [t-window,t); no interpolation",
                ),
                FeatureDefinition(
                    f"telemetry_density_per_min_{window}s",
                    "exposure",
                    "telemetry.timestamp",
                    window,
                    "count/window_minutes",
                    "float64",
                    "past_only",
                    "unique timestamps in [t-window,t); no interpolation",
                ),
            ]
        )

    for family in config["numeric_families"]:
        for window in _windows(config, family["windows"]):
            for aggregation in family["aggregations"]:
                result.append(
                    FeatureDefinition(
                        f"{family['name']}_{aggregation}_{window}s",
                        "speed_vehicle_dynamics"
                        if family["name"] in {"can_speed", "gps_speed"}
                        else "engine_vehicle_state",
                        family["source"],
                        window,
                        aggregation,
                        family["dtype"],
                        family["leakage_status"],
                        family["quality_handling"],
                        family["model_inclusion"],
                    )
                )

    for event_name in config["historical_event_types"]:
        for window in config["windows_seconds"]:
            result.append(
                FeatureDefinition(
                    f"{event_name}_onset_count_{window}s",
                    "historical_events",
                    f"events_onsets.{event_name}.event_start_time",
                    window,
                    "strict_past_count",
                    "float64",
                    "past_only",
                    "trusted onset with start < anchor and first-observed telemetry < anchor; no end/final-duration fields",
                )
            )
        result.append(
            FeatureDefinition(
                f"seconds_since_last_{event_name}_onset_300s",
                "historical_events",
                f"events_onsets.{event_name}.event_start_time",
                300,
                "seconds_since_last_strict_past_onset",
                "float64",
                "past_only",
                "null when no trusted onset exists in [t-300s,t)",
            )
        )

    for window in config["windows_seconds"]:
        result.append(
            FeatureDefinition(
                f"speeding_observation_count_{window}s",
                "speeding_predictor",
                "events_observed.speeding.telemetry_timestamp",
                window,
                "strict_past_count",
                "float64",
                "past_only",
                "counts prior observations only; final duration is never used",
            )
        )

    for family in config["state_families"]:
        for window in _windows(config, family["windows"]):
            for aggregation in family["aggregations"]:
                result.append(
                    FeatureDefinition(
                        f"{family['name']}_{aggregation}_{window}s",
                        "engine_vehicle_state",
                        family["source"],
                        window,
                        aggregation,
                        family["dtype"],
                        family["leakage_status"],
                        family["quality_handling"],
                        family["model_inclusion"],
                    )
                )

    result.extend(
        [
            FeatureDefinition(
                "elapsed_trip_seconds",
                "exposure",
                "telemetry.timestamp and trip start",
                None,
                "anchor_minus_trip_start",
                "float64",
                "past_only",
                "trip start is known at inference; final trip duration is forbidden",
            ),
            FeatureDefinition(
                "history_coverage_seconds",
                "exposure",
                "telemetry.timestamp",
                300,
                "anchor_minus_first_history_timestamp",
                "float64",
                "past_only",
                "computed only from timestamps before anchor",
            ),
            FeatureDefinition(
                "history_max_gap_seconds",
                "exposure",
                "telemetry.timestamp",
                300,
                "maximum_gap",
                "float64",
                "past_only",
                "includes left boundary and anchor; no interpolation",
            ),
            FeatureDefinition(
                "vehicle_series_code",
                "vehicle_class",
                "telemetry.vehicle_series_code",
                None,
                "categorical_identity_safe",
                "int16",
                "static_available",
                "300/500/700 vehicle class; not a direct vehicle identifier",
            ),
        ]
    )

    names = [feature.name for feature in result]
    if len(names) != len(set(names)):
        raise ValueError("Feature specification contains duplicate names")
    if any(feature.leakage_status not in {"past_only", "static_available"} for feature in result):
        raise ValueError("Every V0 feature must be past-only or static")
    return result
