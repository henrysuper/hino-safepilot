"""Section 5: dispatch-time fuel prediction for route x vehicle x driver type.

Flow: ORS candidate routes -> cut into segments -> describe each segment with
the same path profile as training (OSM road band, MOI DEM climb, 500 m grids)
-> time band from the running clock -> congestion (TDX live when requested and
available, else the historical map) -> expected behaviour (road behaviour map x
driver-type multiplier) -> XGBoost per segment -> sum per route.

Usage::

    predictor = FuelPredictor.from_repo(Path("."))
    result = predictor.predict_trip_fuel([121.30, 24.99], [120.67, 24.15],
                                         "2026-10-02T08:00:00+08:00", ["<enabledCode>"], ["eco", "normal"], False)
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from .artifacts import FuelArtifacts
from .config import FuelConfig, load_config
from .drivers import DriverProfile, apply_profile
from .maps import BEHAVIOURS, explode_grids
from .model import predict_litres
from .routing import CandidateRoute, OrsClient, RoutingError, TdxClient, load_env_file
from .segments import GeoContext, describe_path
from .geo import to_twd97

LOCAL_TZ = "Asia/Taipei"


@dataclass
class RouteSegment:
    x: np.ndarray
    y: np.ndarray
    km: float


def cut_polyline(x: np.ndarray, y: np.ndarray, length_km: float, tail_min_km: float) -> list[RouteSegment]:
    """Cut a TWD97 polyline every ``length_km``; a short tail merges into the previous piece."""

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    cumulative = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))]) / 1000
    total = float(cumulative[-1])
    if total <= 0:
        return []
    cuts = list(np.arange(0.0, total, length_km)) + [total]
    if len(cuts) > 2 and cuts[-1] - cuts[-2] < tail_min_km:
        cuts.pop(-2)
    pieces = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        inside = (cumulative > a) & (cumulative < b)
        stations = np.concatenate([[a], cumulative[inside], [b]])
        pieces.append(RouteSegment(np.interp(stations, cumulative, x), np.interp(stations, cumulative, y), float(b - a)))
    return pieces


def _local_time(value: str | pd.Timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(LOCAL_TZ).tz_localize(None)
    return stamp


class FuelPredictor:
    def __init__(self, config: FuelConfig, booster: xgb.Booster, meta: dict, artifacts: FuelArtifacts, geo: GeoContext,
                 ors: OrsClient | None = None, tdx: TdxClient | None = None):
        self.config = config
        self.booster = booster
        self.meta = meta
        self.columns = tuple(meta["features"])
        self.segment_km = float(meta["segment_km"])
        self.interval = tuple(meta["prediction_interval_ratio"])
        self.artifacts = artifacts
        self.geo = geo
        self.ors = ors
        self.tdx = tdx
        # Dispatch calibration (fitted on training-period real trips); identity when absent.
        self.calibration = {"route_fuel_factor": 1.0, "route_time_factor": 1.0, "overhead_L_by_type": {}, "overhead_min_by_type": {}}

    def set_calibration(self, calibration: dict | None) -> None:
        if calibration:
            self.calibration.update(calibration)
            if calibration.get("interval_ratio"):
                # Real-trip interval includes routing uncertainty (ORS path vs the path actually driven).
                self.interval = tuple(calibration["interval_ratio"])

    @classmethod
    def from_repo(cls, repo_root: Path, *, ors: OrsClient | None = None, tdx: TdxClient | None = None, with_live_traffic: bool = True,
                  calibrated: bool = True, model_version: str | None = None) -> "FuelPredictor":
        """model_version None = v1 (the only fuel model; drivers act through expected behaviour)."""

        from .pipeline import FuelPaths, geo_context, model_dir_for

        paths = FuelPaths.resolve(repo_root)
        load_env_file(paths.repo_root / ".env")
        config = paths.config()
        model_dir = model_dir_for(paths, model_version)
        booster = xgb.Booster()
        booster.load_model(model_dir / "fuel_model.json")
        booster.set_param({"device": "cpu"})
        meta = json.loads((model_dir / "model_meta.json").read_text(encoding="utf-8"))
        artifacts = FuelArtifacts.load(paths.output_root / "artifacts")
        tdx_client = tdx if tdx is not None else (TdxClient(config.tdx, paths.under_repo("data/interim/external/tdx")) if with_live_traffic else None)
        predictor = cls(config, booster, meta, artifacts, geo_context(paths, config), ors or OrsClient(config.ors), tdx_client)
        calibration_path = paths.output_root / "artifacts" / "dispatch_calibration.json"
        if calibrated and calibration_path.is_file():
            predictor.set_calibration(json.loads(calibration_path.read_text(encoding="utf-8")))
        return predictor

    # -------------------------------------------------------------- segments
    def describe_route(self, route: CandidateRoute, depart: pd.Timestamp, use_live_traffic: bool) -> tuple[pd.DataFrame, list[str]]:
        notes: list[str] = []
        x, y = to_twd97(route.lonlat[:, 0], route.lonlat[:, 1])
        pieces = cut_polyline(x, y, self.segment_km, self.config.segment.tail_min_km)
        live_speeds = None
        if use_live_traffic:
            if self.tdx is None:
                notes.append("未設定 TDX，改用歷史塞車地圖。")
            else:
                try:
                    live_speeds = self.tdx.live_speeds()
                except RoutingError as error:
                    notes.append(f"TDX 即時路況取得失敗（{error}），改用歷史塞車地圖。")
        rows = []
        clock = depart
        for i, piece in enumerate(pieces):
            profile = self.geo.profile(piece.x, piece.y)
            info = describe_path(profile)
            band = info["road_band"]
            if band is None:
                band = "normal"
                notes.append(f"第 {i + 1} 段無法對應 OSM 道路，暫以一般道路計算。")
            row = {
                **info,
                "road_band": band,
                "segment_index": i,
                "segment_km": piece.km,
                "time_band": self.config.time_band(clock.hour),
                "start_clock": clock,
            }
            frame = pd.DataFrame([row])
            grid_rows = explode_grids(frame)
            free_flow = float(self.artifacts.congestion.segment_free_flow(frame, grid_rows).iloc[0])
            congestion = float(self.artifacts.congestion.expected(frame, grid_rows).iloc[0])
            source = "historical_map"
            if live_speeds is not None:
                matched = self.tdx.match(profile["x"], profile["y"], live_speeds)
                if np.isfinite(matched).mean() >= 0.5:
                    live = float(1 / np.nanmean(1 / matched[np.isfinite(matched)]))
                    # TDX speeds are all-vehicle speeds; above truck free-flow just means "no congestion".
                    congestion = float(np.clip(live / free_flow, self.config.maps.congestion_clip[0], self.config.tdx.max_congestion))
                    source = "tdx_live"
            minutes = piece.km / (free_flow * congestion) * 60
            row.update(congestion_index=congestion, congestion_source=source, free_flow_kph=free_flow, predicted_minutes=minutes)
            rows.append(row)
            clock = clock + pd.Timedelta(minutes=minutes)
        return pd.DataFrame(rows), notes

    # -------------------------------------------------------------- prediction
    def predict_segments(self, segments: pd.DataFrame, enabled_code: str, driver: str | DriverProfile) -> np.ndarray:
        """Litres per segment. ``driver`` is a driver type name or a personal DriverProfile."""

        return self.predict_pairs(segments, [(enabled_code, driver)])[0]

    def predict_pairs(self, segments: pd.DataFrame, pairs: list[tuple[str, str | DriverProfile]]) -> np.ndarray:
        """(len(pairs), len(segments)) litres for many vehicle x driver pairs in one model call."""

        if not pairs:
            return np.zeros((0, len(segments)))
        frames, neutral = [], {}
        for enabled_code, driver in pairs:
            if enabled_code not in self.artifacts.vehicle_types.index:
                raise KeyError(f"Unknown vehicle id: {enabled_code}")
            frame = segments.assign(enabled_code=enabled_code, vehicle_type=float(self.artifacts.vehicle_types[enabled_code]))
            if isinstance(driver, str):
                frames.append(self.artifacts.feature_frame(frame, "expected", driver_type=driver))
                continue
            if enabled_code not in neutral:  # behaviour prior x grid map, before the driver's multipliers
                neutral[enabled_code] = self.artifacts.feature_frame(frame, "expected")
            frames.append(apply_profile(neutral[enabled_code], driver.row_multipliers(segments["road_band"])))
        stacked = pd.concat(frames, ignore_index=True)
        km = pd.Series(np.tile(segments["segment_km"].to_numpy(float), len(pairs)))
        return predict_litres(self.booster, stacked, self.columns, km).reshape(len(pairs), len(segments))

    def predict_trip_fuel(self, origin: list[float], destination: list[float], depart_time: str, vehicle_ids: list[str],
                          driver_types: list[str], use_live_traffic: bool, routes: list[CandidateRoute] | None = None) -> dict:
        """origin / destination are [lon, lat]. Returns every route x vehicle x driver-type option."""

        if not vehicle_ids or not driver_types:
            raise ValueError("vehicle_ids and driver_types must not be empty")
        unknown = [d for d in driver_types if d not in self.artifacts.driver_types.names]
        if unknown:
            raise ValueError(f"Unknown driver types {unknown}; expected {self.artifacts.driver_types.names}")
        depart = _local_time(depart_time)
        candidates = routes if routes is not None else self.ors.routes(origin, destination)
        out_routes, notes = [], []
        if len(candidates) == 1 and candidates[0].distance_km > self.config.ors.max_alternative_km:
            notes.append("任務超過 100 公里，ORS 只提供一條路線。")
        for route in candidates:
            segments, route_notes = self.describe_route(route, depart, use_live_traffic)
            notes.extend(n for n in route_notes if n not in notes)
            options = []
            cal = self.calibration
            for vehicle_id in vehicle_ids:
                vehicle_type = int(self.artifacts.vehicle_types[vehicle_id])
                overhead_l = float(cal["overhead_L_by_type"].get(str(vehicle_type), 0.0))
                overhead_min = float(cal["overhead_min_by_type"].get(str(vehicle_type), 0.0))
                driving_min = float(segments["predicted_minutes"].sum()) * float(cal["route_time_factor"])
                for driver_type in driver_types:
                    litres = self.predict_segments(segments, vehicle_id, driver_type) * float(cal["route_fuel_factor"])
                    driving = float(litres.sum())
                    total = driving + overhead_l
                    options.append({
                        "vehicle_id": vehicle_id,
                        "vehicle_type": vehicle_type,
                        "driver_type": driver_type,
                        "predicted_fuel_L": round(total, 2),
                        "prediction_interval_L": [round(total * self.interval[0], 2), round(total * self.interval[1], 2)],
                        "predicted_minutes": round(driving_min + overhead_min),
                        "driving_fuel_L": round(driving, 2),
                        "terminal_overhead_L": round(overhead_l, 2),
                        "segment_fuel_L": [round(float(v), 3) for v in litres],
                    })
            options.sort(key=lambda o: o["predicted_fuel_L"])
            out_routes.append({
                "route_id": route.route_id,
                "distance_km": round(route.distance_km, 1),
                "ors_minutes_free_flow": round(route.ors_minutes),
                "options": options,
                "segments": [
                    {
                        "km": round(float(s.segment_km), 2),
                        "road_band": s.road_band,
                        "avg_slope": None if not math.isfinite(s.avg_slope) else round(float(s.avg_slope), 2),
                        "elevation_gain_m": None if not math.isfinite(s.elevation_gain_m) else round(float(s.elevation_gain_m), 1),
                        "time_band": s.time_band,
                        "congestion_index": round(float(s.congestion_index), 3),
                        "congestion_source": s.congestion_source,
                        "predicted_minutes": round(float(s.predicted_minutes), 1),
                    }
                    for s in segments.itertuples()
                ],
            })
        best = min(((r["route_id"], o) for r in out_routes for o in r["options"]), key=lambda item: item[1]["predicted_fuel_L"])
        return {
            "model_version": self.meta["model_version"],
            "depart_time_local": depart.isoformat(),
            "routes": out_routes,
            "lowest_fuel_option": {"route_id": best[0], **{k: best[1][k] for k in ("vehicle_id", "driver_type", "predicted_fuel_L", "predicted_minutes")}},
            "notes": notes,
            "calibration": {k: self.calibration[k] for k in ("route_fuel_factor", "route_time_factor")},
            "driver_type_note": "駕駛類型由真實行為資料分群；駕駛名單與分配為模擬資料。",
        }


def predict_trip_fuel(origin: list[float], destination: list[float], depart_time: str, vehicle_ids: list[str],
                      driver_types: list[str], use_live_traffic: bool, repo_root: Path | str = ".") -> dict:
    """Convenience wrapper matching the spec interface."""

    predictor = FuelPredictor.from_repo(Path(repo_root), with_live_traffic=use_live_traffic)
    return predictor.predict_trip_fuel(origin, destination, depart_time, vehicle_ids, driver_types, use_live_traffic)


__all__ = ["BEHAVIOURS", "FuelPredictor", "cut_polyline", "predict_trip_fuel"]
