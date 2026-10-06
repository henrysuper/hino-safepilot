"""Back-test the full dispatch pipeline on real test-period trips.

For each sampled trip the real origin and destination are sent to ORS (user
approved), the returned routes go through ``FuelPredictor`` exactly as in
dispatch (historical congestion at the real departure time, behaviour from the
road behaviour map x driver type), and the prediction is compared with the
trip's real fuel. ORS responses are cached locally (git-ignored) so re-runs do
not spend the daily quota. Reports contain vehicle aliases only, no coordinates.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .geo import haversine_km, valid_lonlat
from .routing import CandidateRoute, RoutingError

DRIVER_ORDER = ("very_eco", "eco", "normal", "aggressive", "very_aggressive")


def trip_endpoints(telemetry: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp | None = None) -> pd.DataFrame:
    """Whole-trip distance, fuel, start time and first/last valid GPS point per trip in [start, end)."""

    first = telemetry.groupby("trip_id")["timestamp"].transform("min")
    keep = first >= start
    if end is not None:
        keep &= first < end
    tel = telemetry[keep]
    tel = tel[valid_lonlat(tel["longitude_deg"], tel["latitude_deg"])]
    grouped = tel.sort_values("timestamp").groupby("trip_id")
    trips = pd.DataFrame({
        "enabled_code": grouped["enabled_code"].first(),
        "vehicle_type": grouped["vehicle_series_code"].agg(lambda s: s.mode().iloc[0] if s.notna().any() else np.nan),
        "start_time": grouped["timestamp"].first(),
        "end_time": grouped["timestamp"].last(),
        "km": grouped["total_mileage_km"].max() - grouped["total_mileage_km"].min(),
        "fuel_L": grouped["total_fuel_used_l"].max() - grouped["total_fuel_used_l"].min(),
        "o_lon": grouped["longitude_deg"].first(), "o_lat": grouped["latitude_deg"].first(),
        "d_lon": grouped["longitude_deg"].last(), "d_lat": grouped["latitude_deg"].last(),
    })
    trips["straight_km"] = haversine_km(trips["o_lat"], trips["o_lon"], trips["d_lat"], trips["d_lon"])
    trips["l_per_100km"] = trips["fuel_L"] / trips["km"] * 100
    return trips.reset_index()


def select_trips(trips: pd.DataFrame, known_vehicles, n: int, seed: int, min_km: float = 10.0, min_directness: float = 0.3) -> tuple[pd.DataFrame, dict]:
    """One-way-like trips >= ``min_km`` (straight O-D >= 30 % of driven km), stratified by type."""

    base = trips[(trips["km"] >= min_km) & trips["l_per_100km"].between(2, 120) & trips["enabled_code"].isin(known_vehicles)]
    one_way = base[(base["straight_km"] >= 2) & (base["straight_km"] / base["km"] >= min_directness)]
    rng = np.random.default_rng(seed)
    per_type = max(1, n // max(1, one_way["vehicle_type"].nunique()))
    picks = []
    for _, group in one_way.groupby("vehicle_type"):
        picks.append(group.iloc[rng.permutation(len(group))[:per_type]])
    chosen = pd.concat(picks) if picks else one_way.iloc[0:0]
    funnel = {"test_trips": int(len(trips)), "ge_min_km": int(len(base)), "one_way_like": int(len(one_way)),
              "one_way_by_type": {str(int(k)): int(v) for k, v in one_way["vehicle_type"].value_counts().sort_index().items()},
              "sampled": int(len(chosen))}
    return chosen.sort_values("start_time").reset_index(drop=True), funnel


def _route_to_json(route: CandidateRoute) -> dict:
    return {"route_id": route.route_id, "lonlat": route.lonlat.round(6).tolist(),
            "elevation": None if route.elevation is None else route.elevation.round(1).tolist(),
            "distance_km": route.distance_km, "ors_minutes": route.ors_minutes}


def _route_from_json(payload: dict) -> CandidateRoute:
    return CandidateRoute(payload["route_id"], np.asarray(payload["lonlat"], dtype=float),
                          None if payload["elevation"] is None else np.asarray(payload["elevation"], dtype=float),
                          float(payload["distance_km"]), float(payload["ors_minutes"]))


class CachedOrs:
    """ORS wrapper with an on-disk cache and a pause between live calls (40/min limit)."""

    def __init__(self, ors, cache_path: Path, pause_s: float = 1.6):
        self.ors, self.cache_path, self.pause_s = ors, Path(cache_path), pause_s
        self.cache = json.loads(self.cache_path.read_text(encoding="utf-8")) if self.cache_path.is_file() else {}
        self.live_calls = 0

    def routes(self, key: str, origin: list[float], destination: list[float], avoid_tollways: bool = False) -> list[CandidateRoute]:
        if avoid_tollways:
            key = f"notoll:{key}"
        if key not in self.cache:
            if self.live_calls:
                time.sleep(self.pause_s)
            self.live_calls += 1
            self.cache[key] = [_route_to_json(r) for r in self.ors.routes(origin, destination, avoid_tollways=avoid_tollways)]
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache), encoding="utf-8")
        return [_route_from_json(r) for r in self.cache[key]]


def backtest(predictor, trips: pd.DataFrame, ors: CachedOrs, aliases: dict[str, str]) -> tuple[pd.DataFrame, list[dict]]:
    rows, details = [], []
    for trip in trips.itertuples():
        try:
            routes = ors.routes(trip.trip_id, [trip.o_lon, trip.o_lat], [trip.d_lon, trip.d_lat])
        except RoutingError as error:
            rows.append({"trip_id": trip.trip_id, "error": str(error)[:120]})
            continue
        result = predictor.predict_trip_fuel([trip.o_lon, trip.o_lat], [trip.d_lon, trip.d_lat], pd.Timestamp(trip.start_time).isoformat(),
                                             [trip.enabled_code], list(DRIVER_ORDER), False, routes=routes)
        by_route = {}
        for route in result["routes"]:
            by_route[route["route_id"]] = {o["driver_type"]: o for o in route["options"]}
        closest = min(result["routes"], key=lambda r: abs(r["distance_km"] - trip.km))
        for label, route in (("ors_first", result["routes"][0]), ("closest_distance", closest)):
            options = by_route[route["route_id"]]
            normal = options["normal"]
            rows.append({
                "trip_id": trip.trip_id, "route_choice": label, "route_id": route["route_id"],
                "vehicle": aliases[trip.enabled_code], "vehicle_type": int(trip.vehicle_type), "date": pd.Timestamp(trip.start_time).strftime("%Y-%m-%d"),
                "actual_km": float(trip.km), "route_km": float(route["distance_km"]), "actual_fuel_L": float(trip.fuel_L),
                "pred_normal_L": normal["predicted_fuel_L"], "interval_low_L": normal["prediction_interval_L"][0], "interval_high_L": normal["prediction_interval_L"][1],
                "pred_very_eco_L": options["very_eco"]["predicted_fuel_L"], "pred_very_aggressive_L": options["very_aggressive"]["predicted_fuel_L"],
                "pred_normal_scaled_to_actual_km_L": normal["predicted_fuel_L"] / route["distance_km"] * trip.km if route["distance_km"] > 0 else np.nan,
                "actual_minutes": (pd.Timestamp(trip.end_time) - pd.Timestamp(trip.start_time)).total_seconds() / 60,
                "pred_minutes": normal["predicted_minutes"], "routes_offered": len(result["routes"]),
                "tdx_or_hist": "historical_map",
            })
        details.append({
            "trip_id": trip.trip_id, "vehicle": aliases[trip.enabled_code], "vehicle_type": int(trip.vehicle_type),
            "date": pd.Timestamp(trip.start_time).strftime("%Y-%m-%d %H:%M"), "actual_km": round(float(trip.km), 1),
            "actual_fuel_L": round(float(trip.fuel_L), 2),
            "routes": [{**r, "options": [{k: v for k, v in o.items() if k not in ("vehicle_id",)} for o in r["options"]],
                        "geometry": [[round(float(a), 5), round(float(b), 5)] for a, b in routes[i].lonlat[:: max(1, len(routes[i].lonlat) // 400)]]}
                       for i, r in enumerate(result["routes"])],
        })
    return pd.DataFrame(rows), details


def summarize(table: pd.DataFrame) -> dict:
    ok = table.dropna(subset=["pred_normal_L"]) if "pred_normal_L" in table else table.iloc[0:0]
    out = {}
    for label, part in ok.groupby("route_choice"):
        ape = (part["pred_normal_L"] - part["actual_fuel_L"]).abs() / part["actual_fuel_L"]
        ape_scaled = (part["pred_normal_scaled_to_actual_km_L"] - part["actual_fuel_L"]).abs() / part["actual_fuel_L"]
        km_err = (part["route_km"] - part["actual_km"]).abs() / part["actual_km"]
        inside = (part["actual_fuel_L"] >= part["interval_low_L"]) & (part["actual_fuel_L"] <= part["interval_high_L"])
        in_range = (part["actual_fuel_L"] >= part["pred_very_eco_L"]) & (part["actual_fuel_L"] <= part["pred_very_aggressive_L"])
        close = part[km_err <= 0.15]
        ape_close = (close["pred_normal_L"] - close["actual_fuel_L"]).abs() / close["actual_fuel_L"]
        out[label] = {
            "trips": int(len(part)),
            "fuel_mape_pct": round(float(ape.mean() * 100), 1),
            "fuel_median_ape_pct": round(float(ape.median() * 100), 1),
            "fuel_bias_pct": round(float((part["pred_normal_L"].sum() / part["actual_fuel_L"].sum() - 1) * 100), 1),
            "fuel_mape_if_route_length_matched_pct": round(float(ape_scaled.mean() * 100), 1),
            "route_km_abs_error_median_pct": round(float(km_err.median() * 100), 1),
            "trips_with_route_km_within_15pct": int(len(close)),
            "fuel_mape_on_those_trips_pct": round(float(ape_close.mean() * 100), 1) if len(close) else None,
            "interval_coverage": round(float(inside.mean()), 3),
            "actual_within_driver_type_range": round(float(in_range.mean()), 3),
            "minutes_mape_pct": round(float(((part["pred_minutes"] - part["actual_minutes"]).abs() / part["actual_minutes"]).mean() * 100), 1),
            "by_type_fuel_mape_pct": {str(t): round(float(((g["pred_normal_L"] - g["actual_fuel_L"]).abs() / g["actual_fuel_L"]).mean() * 100), 1)
                                      for t, g in part.groupby("vehicle_type")},
        }
    return out


# ------------------------------------------------------------------ dispatch calibration (training period only)
def terminal_overhead(trips: pd.DataFrame, segments: pd.DataFrame, min_km: float = 10.0) -> pd.DataFrame:
    """Fuel / time spent outside driving segments (depot and stop idling), per vehicle type.

    For each trip: overhead = whole-trip fuel - segment fuel - uncovered km x the
    trip's own segment L/100km. Reported as totals / trips (no medians).
    """

    seg = segments.groupby("trip_id").agg(seg_km=("segment_km", "sum"), seg_fuel=("fuel_L", "sum"), seg_min=("duration_min", "sum"))
    frame = trips[trips["km"] >= min_km].join(seg, on="trip_id", how="inner")
    frame = frame[frame["seg_km"] > 0]
    rate = frame["seg_fuel"] / frame["seg_km"]
    uncovered = (frame["km"] - frame["seg_km"]).clip(lower=0)
    frame["overhead_L"] = (frame["fuel_L"] - frame["seg_fuel"] - uncovered * rate).clip(lower=0)
    whole_min = (frame["end_time"] - frame["start_time"]).dt.total_seconds() / 60
    frame["overhead_min"] = (whole_min - frame["seg_min"] - uncovered / (frame["seg_km"] / frame["seg_min"].clip(lower=1e-6))).clip(lower=0)
    grouped = frame.groupby("vehicle_type")
    return pd.DataFrame({"trips": grouped.size(), "overhead_L_per_trip": grouped["overhead_L"].sum() / grouped.size(),
                         "overhead_min_per_trip": grouped["overhead_min"].sum() / grouped.size(),
                         "overhead_share_of_fuel": grouped["overhead_L"].sum() / grouped["fuel_L"].sum()})


def _variant_predictions(part: pd.DataFrame, fit: pd.DataFrame, overhead: pd.DataFrame, variant: str, target: str, pred: str, over_col: str) -> np.ndarray:
    """Calibrated predictions for ``part`` with parameters estimated on ``fit``."""

    over_part = part["vehicle_type"].map(overhead[over_col]).fillna(0).to_numpy() if "overhead" in variant else np.zeros(len(part))
    over_fit = fit["vehicle_type"].map(overhead[over_col]).fillna(0).to_numpy() if "overhead" in variant else np.zeros(len(fit))
    base = part[pred].to_numpy(float)
    if variant in ("none", "overhead"):
        return base + over_part
    ratio = (fit[target].to_numpy(float) - over_fit) / fit[pred].to_numpy(float)
    if variant.endswith("median_factor"):
        return base * float(np.median(ratio)) + over_part
    raise ValueError(variant)


CALIBRATION_VARIANTS = ("none", "overhead", "median_factor", "overhead+median_factor")


def select_calibration(table: pd.DataFrame, overhead: pd.DataFrame, target: str, pred: str, over_col: str) -> dict:
    """Pick the calibration form with the lowest leave-one-out MAPE on calibration trips."""

    part = table[table["route_choice"] == "ors_first"].dropna(subset=[pred]).reset_index(drop=True)
    scores = {}
    for variant in CALIBRATION_VARIANTS:
        errors = []
        for i in range(len(part)):
            row, rest = part.iloc[[i]], part.drop(index=i)
            value = _variant_predictions(row, rest, overhead, variant, target, pred, over_col)[0]
            errors.append(abs(value - row[target].iloc[0]) / row[target].iloc[0])
        scores[variant] = float(np.mean(errors) * 100)
    best = min(scores, key=scores.get)
    ratio = (part[target] - part["vehicle_type"].map(overhead[over_col]).fillna(0) * ("overhead" in best)) / part[pred]
    factor = float(np.median(ratio)) if "factor" in best else 1.0
    return {"variant": best, "factor": factor, "use_overhead": "overhead" in best, "loo_mape_pct_by_variant": {k: round(v, 1) for k, v in scores.items()}}


def fit_route_factors(table: pd.DataFrame, overhead: pd.DataFrame) -> dict:
    """Choose and fit the calibration (fuel and time separately) by leave-one-out MAPE.

    Candidate forms: identity, + terminal overhead, x median factor, both. A median
    (not a ratio of totals) is used so that a few long trips do not dominate.
    """

    part = table[table["route_choice"] == "ors_first"].dropna(subset=["pred_normal_L"])
    fuel = select_calibration(part, overhead, "actual_fuel_L", "pred_normal_L", "overhead_L_per_trip")
    time_choice = select_calibration(part, overhead, "actual_minutes", "pred_minutes", "overhead_min_per_trip")
    # Dispatch interval: 10th-90th percentile of actual / calibrated prediction on the calibration trips.
    calibrated = _variant_predictions(part, part, overhead, fuel["variant"], "actual_fuel_L", "pred_normal_L", "overhead_L_per_trip")
    ratio = part["actual_fuel_L"].to_numpy(float) / np.clip(calibrated, 1e-6, None)
    return {
        "interval_ratio": [float(np.quantile(ratio, 0.1)), float(np.quantile(ratio, 0.9))],
        "route_fuel_factor": fuel["factor"], "use_fuel_overhead": fuel["use_overhead"], "fuel_variant": fuel["variant"],
        "fuel_loo_mape_pct": fuel["loo_mape_pct_by_variant"],
        "route_time_factor": time_choice["factor"], "use_time_overhead": time_choice["use_overhead"], "time_variant": time_choice["variant"],
        "time_loo_mape_pct": time_choice["loo_mape_pct_by_variant"],
        "fit_trips": int(len(part)),
    }


# ------------------------------------------------------------------ did the driver follow the planned route?
def route_overlap(track_lonlat: np.ndarray, route_lonlat: np.ndarray, tolerance_m: float = 200.0) -> dict:
    """Geometric agreement between the driven GPS track and an ORS route.

    * ``route_followed_share``: share of the ORS route (every 50 m) within
      ``tolerance_m`` of the driven track - how much of the plan was actually driven.
    * ``track_on_route_share``: share of the driven track within ``tolerance_m`` of the
      ORS route - how much of the drive stayed on the plan (detours lower this).
    """

    from scipy.spatial import cKDTree

    from .geo import densify_path, to_twd97

    tx, ty = to_twd97(track_lonlat[:, 0], track_lonlat[:, 1])
    rx, ry = to_twd97(route_lonlat[:, 0], route_lonlat[:, 1])
    tx, ty = densify_path(tx, ty, 50.0)
    rx, ry = densify_path(rx, ry, 50.0)
    track_tree, route_tree = cKDTree(np.column_stack([tx, ty])), cKDTree(np.column_stack([rx, ry]))
    followed = float((track_tree.query(np.column_stack([rx, ry]))[0] <= tolerance_m).mean())
    on_route = float((route_tree.query(np.column_stack([tx, ty]))[0] <= tolerance_m).mean())
    return {"route_followed_share": followed, "track_on_route_share": on_route}


def overlap_table(telemetry: pd.DataFrame, trips: pd.DataFrame, cache: dict) -> pd.DataFrame:
    rows = []
    for trip in trips.itertuples():
        if trip.trip_id not in cache:
            continue
        track = telemetry[telemetry["trip_id"] == trip.trip_id].sort_values("timestamp")
        track = track[valid_lonlat(track["longitude_deg"], track["latitude_deg"])]
        if len(track) < 2:
            continue
        lonlat = track[["longitude_deg", "latitude_deg"]].to_numpy(float)
        route = np.asarray(cache[trip.trip_id][0]["lonlat"], dtype=float)
        rows.append({"trip_id": trip.trip_id, **route_overlap(lonlat, route)})
    return pd.DataFrame(rows)


def error_by_overlap(table: pd.DataFrame, overlap: pd.DataFrame) -> list[dict]:
    """Fuel error of the ORS-route prediction grouped by how closely the driver followed it."""

    part = table[table["route_choice"] == "ors_first"].merge(overlap, on="trip_id")
    part["both"] = np.minimum(part["route_followed_share"], part["track_on_route_share"])
    bins = [(0.8, 1.01, "照規劃路線開（重疊 ≥80%）"), (0.5, 0.8, "部分照開（50–80%）"), (0.0, 0.5, "走了別條路（<50%）")]
    rows = []
    for low, high, label in bins:
        group = part[(part["both"] >= low) & (part["both"] < high)]
        if group.empty:
            rows.append({"group": label, "trips": 0})
            continue
        ape = (group["pred_normal_L"] - group["actual_fuel_L"]).abs() / group["actual_fuel_L"]
        rows.append({
            "group": label, "trips": int(len(group)),
            "fuel_mape_pct": round(float(ape.mean() * 100), 1), "fuel_median_ape_pct": round(float(ape.median() * 100), 1),
            "fuel_bias_pct": round(float((group["pred_normal_L"].sum() / group["actual_fuel_L"].sum() - 1) * 100), 1),
            "km_ratio_actual_over_route": round(float(group["actual_km"].sum() / group["route_km"].sum()), 2),
        })
    return rows


def detect_stops(track: pd.DataFrame, min_minutes: float = 5.0, still_kph: float = 3.0, merge_m: float = 300.0,
                 max_stops: int = 8) -> list[tuple[float, float, float]]:
    """Intermediate stops (lon, lat, minutes) where the truck stood still >= ``min_minutes``.

    Stops within ``merge_m`` of the trip's start or end, or of the previous stop, are merged away.
    """

    track = track.sort_values("timestamp")
    track = track[(track["longitude_deg"] > 100) & (track["latitude_deg"] > 20)]
    if len(track) < 3:
        return []
    dt = track["timestamp"].diff().dt.total_seconds().shift(-1).fillna(0).clip(upper=600).to_numpy()
    still = (track["gps_speed_kph"].fillna(0) < still_kph).to_numpy()
    lon, lat = track["longitude_deg"].to_numpy(), track["latitude_deg"].to_numpy()
    runs, start, acc = [], None, 0.0
    for i in range(len(track)):
        if still[i]:
            start = i if start is None else start
            acc += dt[i]
        elif start is not None:
            runs.append((start, i, acc))
            start, acc = None, 0.0
    first, last = (lon[0], lat[0]), (lon[-1], lat[-1])
    stops: list[tuple[float, float, float]] = []
    for s, e, seconds in runs:
        if seconds < min_minutes * 60:
            continue
        p = (float(np.median(lon[s:e])), float(np.median(lat[s:e])))
        near = lambda q: haversine_km(p[1], p[0], q[1], q[0]) * 1000 < merge_m  # noqa: E731
        if near(first) or near(last) or (stops and near(stops[-1][:2])):
            continue
        stops.append((p[0], p[1], seconds / 60))
    return stops[:max_stops]


def stop_idle_model(telemetry: pd.DataFrame, min_stop_min: float = 5.0) -> dict:
    """Idling fuel during stops, per vehicle type, from (training) telemetry.

    * rate_l_per_h: litres per engine-on idle hour (carStatus 2 -> 2 intervals, cumulative fuel).
    * engine_on_share: share of stop time (stationary >= ``min_stop_min``) spent with the engine idling.
    Both are ratios of totals.
    """

    tel = telemetry.sort_values(["trip_id", "timestamp"])
    nxt = tel.groupby("trip_id")[["timestamp", "vehicle_status", "total_fuel_used_l"]].shift(-1)
    dt = (nxt["timestamp"] - tel["timestamp"]).dt.total_seconds()
    dfuel = nxt["total_fuel_used_l"] - tel["total_fuel_used_l"]
    vtype = tel["vehicle_series_code"]
    idle = (tel["vehicle_status"] == 2) & (nxt["vehicle_status"] == 2) & dt.between(1, 300) & dfuel.between(0, 5)
    rate = dfuel[idle].groupby(vtype[idle]).sum() / (dt[idle].groupby(vtype[idle]).sum() / 3600)
    still = tel["gps_speed_kph"].fillna(0) < 3
    run = (still != still.groupby(tel["trip_id"]).shift()).cumsum()
    seconds = dt.clip(lower=0, upper=600).fillna(0)
    blocks = pd.DataFrame({"run": run, "still": still, "s": seconds, "on": seconds * (tel["vehicle_status"] == 2), "type": vtype})
    blocks = blocks[blocks["still"]].groupby("run").agg(s=("s", "sum"), on=("on", "sum"), type=("type", "first"))
    long = blocks[blocks["s"] >= min_stop_min * 60]
    share = long.groupby("type")["on"].sum() / long.groupby("type")["s"].sum()
    return {"rate_l_per_h": {str(int(k)): round(float(v), 3) for k, v in rate.items()},
            "engine_on_share": {str(int(k)): round(float(v), 3) for k, v in share.items()},
            "method": "training-period telemetry; ratios of totals"}
