"""Fit and persist every lookup table the model and the dispatcher need.

``fit_artifacts`` takes training-period data only and returns the behaviour
prior + grid map (step 3), congestion model (step 4), driver types (step 5),
vehicle efficiency offsets (step 6). Vehicle health (step 2) is computed
separately from non-fuel telemetry and passed in.

``feature_frame`` builds model inputs in one of two modes:

* ``actual``   - the segment's observed behaviour and congestion (training,
                 post-trip scoring);
* ``expected`` - behaviour = prior x grid factor x driver-type multiplier and
                 congestion from the historical map (dispatch).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import FuelConfig
from .geo import BAND_CODE
from .maps import (
    BEHAVIOURS,
    BehaviourPrior,
    CongestionModel,
    DriverTypes,
    GridFactorMap,
    add_behaviour_totals,
    expected_totals,
    explode_grids,
    fit_congestion,
    fit_driver_types,
    observed_totals,
)
from .drivers import DRIVER_MULTS, DriverProfile, apply_profile
from .vehicles import efficiency_offsets

FEATURES_SPEC = (
    "vehicle_type",
    "vehicle_efficiency_offset",
    "health_score",
    "road_band_code",
    "elevation_gain_m",
    "avg_slope",
    "segment_km",
    "time_band_code",
    "congestion_index",
    *BEHAVIOURS,
)
FEATURES_EXTENDED = (*FEATURES_SPEC, "highway_share", "city_share")



@dataclass
class FuelArtifacts:
    prior: BehaviourPrior
    behaviour_map: GridFactorMap
    congestion: CongestionModel
    driver_types: DriverTypes
    vehicle_offsets: pd.Series  # enabled_code -> offset (full training period)
    health: pd.Series  # enabled_code -> health_score
    vehicle_types: pd.Series  # enabled_code -> type
    type_band_rates: pd.Series
    time_band_names: list[str]
    driver_trips: pd.DataFrame | None = None

    # -------------------------------------------------------------- features
    def feature_frame(self, segments: pd.DataFrame, mode: str, driver_type: str | None = None,
                      offsets: pd.Series | None = None, grid_rows: pd.DataFrame | None = None,
                      driver_profile: DriverProfile | None = None) -> pd.DataFrame:
        """Model inputs. In expected (dispatch) mode the behaviour can be personalised: by
        ``driver_profile`` (a driver's ratios per road type), else by the segments' own
        per-segment driver columns (DRIVER_MULTS, causal history), else typical behaviour.
        A ``driver_type`` uses the type multipliers instead. Actual mode keeps observed behaviour."""

        if mode not in ("actual", "expected"):
            raise ValueError("mode must be 'actual' or 'expected'")
        frame = self._base_features(segments, mode, driver_type, offsets, grid_rows)
        if mode == "actual" or driver_type is not None:
            return frame
        if driver_profile is not None:
            return apply_profile(frame, driver_profile.row_multipliers(segments["road_band"]))
        if all(c in segments for c in DRIVER_MULTS):
            return apply_profile(frame, segments[list(DRIVER_MULTS)].to_numpy(float))
        return frame

    def _base_features(self, segments: pd.DataFrame, mode: str, driver_type: str | None, offsets: pd.Series | None,
                       grid_rows: pd.DataFrame | None) -> pd.DataFrame:
        grid_rows = explode_grids(segments) if grid_rows is None else grid_rows
        frame = pd.DataFrame(index=segments.index)
        frame["vehicle_type"] = segments["vehicle_type"].astype(float)
        if offsets is not None:
            frame["vehicle_efficiency_offset"] = offsets.reindex(segments.index).astype(float)
        else:
            frame["vehicle_efficiency_offset"] = segments["enabled_code"].map(self.vehicle_offsets).fillna(1.0).astype(float)
        frame["health_score"] = segments["enabled_code"].map(self.health).astype(float)
        frame["road_band_code"] = segments["road_band"].map(BAND_CODE).astype(float)
        frame["time_band_code"] = segments["time_band"].map({n: i for i, n in enumerate(self.time_band_names)}).astype(float)
        for column in ("elevation_gain_m", "avg_slope", "segment_km", "highway_share", "city_share"):
            frame[column] = segments[column].astype(float)
        if mode == "actual":
            frame["congestion_index"] = self.congestion.actual(segments, grid_rows)
            for behaviour in BEHAVIOURS:
                frame[behaviour] = segments[behaviour].astype(float)
        else:
            frame["congestion_index"] = segments["congestion_index"] if "congestion_index" in segments else self.congestion.expected(segments, grid_rows)
            frame[list(BEHAVIOURS)] = self.expected_behaviour(segments, grid_rows, driver_type).to_numpy()
        return frame

    def expected_behaviour(self, segments: pd.DataFrame, grid_rows: pd.DataFrame, driver_type: str | None) -> pd.DataFrame:
        rates = self.prior.rates(segments)
        factors = self.behaviour_map.factors(grid_rows, segments["time_band"], segments.index)[list(BEHAVIOURS)]
        expected = rates * factors
        if driver_type is not None:
            expected = expected * self.driver_types.multiplier(driver_type, segments["road_band"]).to_numpy()
        expected["overspeed_share"] = expected["overspeed_share"].clip(0, 1)
        return expected

    # -------------------------------------------------------------- persistence
    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "behaviour_prior.json").write_text(json.dumps(self.prior.to_json(), ensure_ascii=False, default=float), encoding="utf-8")
        all_time, by_band = self.behaviour_map.to_frames()
        all_time.to_parquet(directory / "road_behaviour_map_grid.parquet", index=False)
        by_band.to_parquet(directory / "road_behaviour_map_grid_timeband.parquet", index=False)
        c_all, c_band = self.congestion.grid_map.to_frames()
        c_all.to_parquet(directory / "congestion_map_grid.parquet", index=False)
        c_band.to_parquet(directory / "congestion_map_grid_timeband.parquet", index=False)
        self.congestion.free_flow.rename("free_flow_kph").to_frame().reset_index(names="grid").to_parquet(directory / "free_flow_speed.parquet", index=False)
        congestion_meta = {
            "band_free_flow": self.congestion.band_free_flow.to_dict(),
            "prior": self.congestion.prior.reset_index().to_dict(orient="records"),
            "clip": list(self.congestion.clip),
            "k": self.congestion.grid_map.k,
        }
        (directory / "congestion_meta.json").write_text(json.dumps(congestion_meta, ensure_ascii=False), encoding="utf-8")
        (directory / "driver_types.json").write_text(json.dumps(self.driver_types.to_json(), ensure_ascii=False, default=float), encoding="utf-8")
        vehicles = pd.DataFrame({
            "vehicle_type": self.vehicle_types,
            "vehicle_efficiency_offset": self.vehicle_offsets.reindex(self.vehicle_types.index).fillna(1.0),
            "health_score": self.health.reindex(self.vehicle_types.index),
        })
        vehicles.index.name = "enabled_code"
        vehicles.reset_index().to_parquet(directory / "vehicles.parquet", index=False)
        self.type_band_rates.rename("l_per_100km").reset_index().to_parquet(directory / "type_band_rates.parquet", index=False)
        (directory / "meta.json").write_text(json.dumps({"time_band_names": self.time_band_names, "behaviour_k": self.behaviour_map.k}), encoding="utf-8")

    @classmethod
    def load(cls, directory: Path) -> "FuelArtifacts":
        meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
        prior = BehaviourPrior.from_json(json.loads((directory / "behaviour_prior.json").read_text(encoding="utf-8")))
        behaviour_map = GridFactorMap.from_frames(
            pd.read_parquet(directory / "road_behaviour_map_grid.parquet"),
            pd.read_parquet(directory / "road_behaviour_map_grid_timeband.parquet"),
            BEHAVIOURS, meta["behaviour_k"],
        )
        cmeta = json.loads((directory / "congestion_meta.json").read_text(encoding="utf-8"))
        congestion = CongestionModel(
            free_flow=pd.read_parquet(directory / "free_flow_speed.parquet").set_index("grid")["free_flow_kph"],
            band_free_flow=pd.Series(cmeta["band_free_flow"]),
            prior=pd.DataFrame(cmeta["prior"]).set_index(["road_band", "time_band"]),
            grid_map=GridFactorMap.from_frames(
                pd.read_parquet(directory / "congestion_map_grid.parquet"),
                pd.read_parquet(directory / "congestion_map_grid_timeband.parquet"),
                ("congestion",), cmeta["k"],
            ),
            clip=tuple(cmeta["clip"]),
        )
        vehicles = pd.read_parquet(directory / "vehicles.parquet").set_index("enabled_code")
        rates = pd.read_parquet(directory / "type_band_rates.parquet").set_index(["vehicle_type", "road_band"])["l_per_100km"]
        return cls(
            prior=prior,
            behaviour_map=behaviour_map,
            congestion=congestion,
            driver_types=DriverTypes.from_json(json.loads((directory / "driver_types.json").read_text(encoding="utf-8"))),
            vehicle_offsets=vehicles["vehicle_efficiency_offset"],
            health=vehicles["health_score"],
            vehicle_types=vehicles["vehicle_type"],
            type_band_rates=rates,
            time_band_names=meta["time_band_names"],
        )


def prepare_segments(segments: pd.DataFrame) -> pd.DataFrame:
    """Usable training segments: L/100km in range and a road band."""

    kept = segments[segments["l100_in_range"] & segments["road_band"].notna()].copy()
    return add_behaviour_totals(kept.reset_index(drop=True))


def fit_artifacts(train: pd.DataFrame, free_flow_table: pd.DataFrame, health: pd.Series, vehicle_types: pd.Series, config: FuelConfig) -> tuple[FuelArtifacts, pd.Series]:
    """Fit steps 3-6 on training segments. Returns artifacts and leave-one-month-out offsets."""

    k = config.maps.shrinkage_k
    grid_rows = explode_grids(train)
    prior = BehaviourPrior.fit(train)
    observed = observed_totals(train)
    expected = expected_totals(train, prior.rates(train))
    behaviour_map = GridFactorMap.fit(grid_rows, observed, expected, train["time_band"], k)
    congestion = fit_congestion(train, grid_rows, free_flow_table, k, config.maps.congestion_clip)
    factors = behaviour_map.factors(grid_rows, train["time_band"], train.index)[list(BEHAVIOURS)]
    map_expected = expected * factors.to_numpy()
    dt = config.driver_types
    driver_types, driver_trips = fit_driver_types(train, observed, map_expected, dt.names, dt.min_trip_km, dt.ratio_cap, dt.min_cell_trips)
    full_offsets, lomo_offsets, rates = efficiency_offsets(train)
    artifacts = FuelArtifacts(
        prior=prior,
        behaviour_map=behaviour_map,
        congestion=congestion,
        driver_types=driver_types,
        vehicle_offsets=full_offsets,
        health=health,
        vehicle_types=vehicle_types,
        type_band_rates=rates,
        time_band_names=config.time_band_names,
        driver_trips=driver_trips,
    )
    return artifacts, lomo_offsets
