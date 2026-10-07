"""Road-and-time context for each trip, from the fuel team's behaviour map (no retraining).

For every trip the fuel segments give the *observed* harsh acceleration, harsh braking,
idle minutes and over-limit seconds, and the fuel artifacts give what is *typical* on the
same road type, time band and 500 m grid. The ratio of a trip's typical rate to the fleet's
typical rate says how demanding its roads were; the road-adjusted safety score divides the
matching events by that factor, so a city-heavy trip is not penalised for its roads.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from .pipeline import _write_jsonl


# Fuel behaviour -> safety score item it adjusts.
BEHAVIOUR_TO_ITEM = {
    "harsh_acc_per_km": "accelerate",
    "harsh_brake_per_km": "decelerate",
    "overspeed_share": "speeding",
    "idle_min_per_km": "idle",
}
FACTOR_CLIP = (0.5, 2.0)


def road_factors(context: dict[str, Any], fleet: dict[str, float]) -> dict[str, float]:
    """Per score item: this trip's typical rate / the fleet's typical rate, clipped."""

    factors: dict[str, float] = {}
    for behaviour, item in BEHAVIOUR_TO_ITEM.items():
        typical = context["typical_rates"].get(behaviour)
        baseline = fleet.get(behaviour)
        if typical is None or not baseline:
            continue
        factors[item] = min(FACTOR_CLIP[1], max(FACTOR_CLIP[0], typical / baseline))
    return factors


def build_road_context(
    repo_root: Path,
    *,
    fuel_root: Path = Path("data/processed/fuel"),
    output_root: Path = Path("data/processed/road_context"),
) -> dict[str, Any]:
    import pandas as pd

    from hino_safepilot.fuel.artifacts import FuelArtifacts
    from hino_safepilot.fuel.config import load_config
    from hino_safepilot.fuel.maps import (
        BEHAVIOUR_TOTALS,
        add_behaviour_totals,
        expected_totals,
        explode_grids,
        observed_totals,
    )

    repo = repo_root.resolve()
    fuel = (repo / fuel_root).resolve()
    output = (repo / output_root).resolve()
    processed = (repo / "data" / "processed").resolve()
    if output == processed or processed not in output.parents:
        raise ValueError("Road context output must be below data/processed/")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite road context output: {output}")
    config = load_config(repo / "config" / "fuel_v1.json")
    segments_path = fuel / f"segments_{int(config.segment.length_km)}km.parquet"
    if not segments_path.is_file():
        raise FileNotFoundError("Run `hino-fuel segments` first: road segments are required")

    segments = pd.read_parquet(segments_path)
    segments = add_behaviour_totals(segments[segments["road_band"].notna()].reset_index(drop=True))
    artifacts = FuelArtifacts.load(fuel / "artifacts")
    rates = artifacts.expected_behaviour(segments, explode_grids(segments), None)
    expected = expected_totals(segments, rates).add_prefix("typical_")
    observed = observed_totals(segments).add_prefix("observed_")
    frame = pd.concat(
        [
            segments[["trip_id", "segment_km", "speed_limit_known_seconds", "road_band"]],
            observed,
            expected,
        ],
        axis=1,
    )
    frame["city_km"] = frame["segment_km"].where(frame["road_band"] == "city", 0.0)
    totals = frame.drop(columns=["road_band"]).groupby("trip_id").sum()

    def per_unit(row, behaviour: str, prefix: str) -> float | None:
        denominator = row[BEHAVIOUR_TOTALS[behaviour][1]]
        return float(row[f"{prefix}{behaviour}"] / denominator) if denominator > 0 else None

    records = []
    for trip_id, row in totals.iterrows():
        records.append(
            {
                "trip_id": str(trip_id),
                "segment_km": round(float(row["segment_km"]), 3),
                "city_share": round(float(row["city_km"] / row["segment_km"]), 3) if row["segment_km"] > 0 else None,
                "observed": {b: round(float(row[f"observed_{b}"]), 3) for b in BEHAVIOUR_TOTALS},
                "typical": {b: round(float(row[f"typical_{b}"]), 3) for b in BEHAVIOUR_TOTALS},
                "typical_rates": {b: per_unit(row, b, "typical_") for b in BEHAVIOUR_TOTALS},
            }
        )
    fleet_totals = totals.sum()
    fleet = {
        behaviour: float(fleet_totals[f"typical_{behaviour}"] / fleet_totals[denominator])
        for behaviour, (_, denominator) in BEHAVIOUR_TOTALS.items()
        if fleet_totals[denominator] > 0
    }

    staging = output.with_name(f".{output.name}.staging-{uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        count = _write_jsonl(staging / "trip_road_context.jsonl", records)
        manifest = {
            "road_context_version": "road-context-v1",
            "source": "fuel segments + fuel behaviour map (prior x 500 m grid x time band); no retraining",
            "segment_km": config.segment.length_km,
            "trip_count": count,
            "fleet_typical_rates": fleet,
            "factor_clip": list(FACTOR_CLIP),
            "behaviour_to_item": BEHAVIOUR_TO_ITEM,
        }
        (staging / "road_context_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.replace(output)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return manifest
