"""Step 1: cut every journey into fixed-length mileage segments.

A journey is one (enabledCode, journeyCode); the Phase 2 ETL hashes the pair
into ``trip_id``. Within a journey (time-ordered), a segment ends at the first
record whose cumulative mileage since the segment start reaches ``length_km``;
the next segment starts at that record. A journey tail of at least
``tail_min_km`` is kept. All durations use timestamp differences.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

from hino_safepilot.analytics.engine import vehicle_profile_id

from .config import FuelConfig
from .geo import (
    BAND_CODE,
    ROAD_BANDS,
    MoiDem,
    RoadIndex,
    climb_metrics,
    majority_band,
    path_profile,
    to_twd97,
    valid_lonlat,
)

EVENT_HARSH_ACC = 6
EVENT_HARSH_BRAKE = 7
SEGMENT_EVENT_TYPES = {EVENT_HARSH_ACC: "n_harsh_acc", EVENT_HARSH_BRAKE: "n_harsh_brake"}

VALUE_COLUMNS = (
    "vehicle_series_code",
    "vehicle_status",
    "gps_speed_kph",
    "gps_speed_limit",
    "total_mileage_km",
    "total_fuel_used_l",
    "fuel_level_pct",
    "latitude_deg",
    "longitude_deg",
)
TELEMETRY_COLUMNS = (
    "source_row_number",
    "trip_id",
    "enabled_code",
    "timestamp",
    *VALUE_COLUMNS,
    *(f"{c}_quality" for c in VALUE_COLUMNS),
)
ONSET_COLUMNS = (
    "trip_id",
    "event_type",
    "event_start_time",
    "event_start_time_quality",
    "start_time_ambiguous",
    "start_year_mismatch_observed",
)


def load_telemetry(input_root: Path) -> pd.DataFrame:
    dataset = ds.dataset(Path(input_root) / "telemetry_normalized", format="parquet", partitioning="hive")
    frame = dataset.to_table(columns=list(TELEMETRY_COLUMNS)).to_pandas()
    return prepare_telemetry(frame)


def prepare_telemetry(frame: pd.DataFrame) -> pd.DataFrame:
    """Null every value whose ETL quality is not ``valid``; sort; drop same-time duplicates."""

    frame = frame.copy()
    for column in VALUE_COLUMNS:
        quality = f"{column}_quality"
        if quality in frame:
            frame.loc[frame[quality] != "valid", column] = np.nan
            frame = frame.drop(columns=quality)
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    frame = frame[frame["trip_id"].notna() & frame["timestamp"].notna()]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    order = ["trip_id", "timestamp"] + (["source_row_number"] if "source_row_number" in frame else [])
    frame = frame.sort_values(order, kind="mergesort").drop_duplicates(["trip_id", "timestamp"], keep="first")
    return frame.reset_index(drop=True)


def load_onsets(input_root: Path) -> pd.DataFrame:
    dataset = ds.dataset(Path(input_root) / "events_onsets", format="parquet", partitioning="hive")
    onsets = dataset.to_table(columns=list(ONSET_COLUMNS)).to_pandas()
    usable = (
        (onsets["event_start_time_quality"] == "valid")
        & ~onsets["start_time_ambiguous"].fillna(False).astype(bool)
        & ~onsets["start_year_mismatch_observed"].fillna(False).astype(bool)
        & onsets["event_type"].isin(list(SEGMENT_EVENT_TYPES))
    )
    onsets = onsets.loc[usable, ["trip_id", "event_type", "event_start_time"]].copy()
    onsets["event_start_time"] = pd.to_datetime(onsets["event_start_time"])
    return onsets


@dataclass(frozen=True)
class GeoContext:
    roads: RoadIndex | None
    dem: MoiDem | None
    max_match_distance_m: float
    cell_m: float
    spacing_m: float = 100.0
    smooth_points: int = 5

    def profile(self, x, y) -> dict:
        return path_profile(
            x, y, roads=self.roads, dem=self.dem, spacing_m=self.spacing_m,
            max_match_distance_m=self.max_match_distance_m, smooth_points=self.smooth_points, cell_m=self.cell_m,
        )


def describe_path(profile: dict) -> dict:
    """Road band, climb and grid usage of one path profile (training and dispatch)."""

    codes = profile["road_codes"]
    gain, slope = climb_metrics(profile["elevation"], profile["length_m"])
    grids, counts = np.unique(profile["grids"], return_counts=True) if len(profile["grids"]) else (np.array([]), np.array([]))
    return {
        "road_band": majority_band(codes),
        "osm_matched_share": float((codes >= 0).mean()) if len(codes) else np.nan,
        "highway_share": float((codes == BAND_CODE["highway"]).mean()) if len(codes) else np.nan,
        "city_share": float((codes == BAND_CODE["city"]).mean()) if len(codes) else np.nan,
        "elevation_gain_m": gain,
        "avg_slope": slope,
        "path_length_km": profile["length_m"] / 1000,
        "grid_ids": grids.tolist(),
        "grid_weights": counts.astype(int).tolist(),
    }


def segment_boundaries(mileage: np.ndarray, fuel: np.ndarray, length_km: float, tail_min_km: float) -> list[tuple[int, int, bool]]:
    """Return (start_row, end_row, is_tail) index pairs into the trip arrays.

    Only rows with valid mileage and fuel can be cut points. A decrease of either
    cumulative counter discards the open segment and restarts after it.
    """

    valid = np.flatnonzero(np.isfinite(mileage) & np.isfinite(fuel))
    bounds: list[tuple[int, int, bool]] = []
    if len(valid) < 2:
        return bounds
    start = valid[0]
    previous = start
    for row in valid[1:]:
        if mileage[row] < mileage[previous] or fuel[row] < fuel[previous]:
            start = row  # counter reset/decrease: drop the open segment
        elif mileage[row] - mileage[start] >= length_km:
            bounds.append((int(start), int(row), False))
            start = row
        previous = row
    if mileage[previous] - mileage[start] >= tail_min_km:
        bounds.append((int(start), int(previous), True))
    return bounds


def _speed_limit_band(limit: np.ndarray, config: FuelConfig) -> str | None:
    limit = limit[np.isfinite(limit) & (limit > 0)]  # 0 = unavailable
    if limit.size == 0:
        return None
    bands = np.where(limit <= config.speed_limit_bands.city_max, 2, np.where(limit <= config.speed_limit_bands.normal_max, 1, 0))
    return majority_band(bands)


def trip_segments(trip: pd.DataFrame, onsets: pd.DataFrame, config: FuelConfig, geo: GeoContext, length_km: float) -> list[dict]:
    seg_cfg = config.segment
    times = trip["timestamp"].to_numpy("datetime64[ns]")
    seconds = (times - times[0]).astype("timedelta64[ns]").astype(np.int64) / 1e9
    dt = np.diff(seconds, append=seconds[-1])  # interval owned by its left record
    dt = np.where(dt > seg_cfg.exposure_max_gap_seconds, 0.0, dt)
    mileage = trip["total_mileage_km"].to_numpy(float)
    fuel = trip["total_fuel_used_l"].to_numpy(float)
    status = trip["vehicle_status"].to_numpy(float)
    speed = trip["gps_speed_kph"].to_numpy(float)
    limit = trip["gps_speed_limit"].to_numpy(float)
    lon = trip["longitude_deg"].to_numpy(float)
    lat = trip["latitude_deg"].to_numpy(float)
    gps_ok = valid_lonlat(lon, lat)
    x, y = to_twd97(np.where(gps_ok, lon, np.nan), np.where(gps_ok, lat, np.nan))
    moving = (status == 1) & np.isfinite(speed) & (speed >= seg_cfg.moving_min_speed_kph)
    event_times = {
        name: onsets.loc[onsets["event_type"] == code, "event_start_time"].to_numpy("datetime64[ns]")
        for code, name in SEGMENT_EVENT_TYPES.items()
    }
    series = trip["vehicle_series_code"].dropna()
    vehicle_type = float(series.mode().iloc[0]) if not series.empty else np.nan
    enabled_code = str(trip["enabled_code"].iloc[0])
    records = []
    bounds = segment_boundaries(mileage, fuel, length_km, seg_cfg.tail_min_km)
    for index, (s, e, is_tail) in enumerate(bounds):
        km = float(mileage[e] - mileage[s])
        litres = float(fuel[e] - fuel[s])
        inner = slice(s, e)  # intervals s..e-1 lie inside the segment
        points = slice(s, e + 1)
        idle_s = float(dt[inner][status[inner] == 2].sum())
        moving_s = float(dt[inner][moving[inner]].sum())
        # Overspeed share is measured over moving time with a known (non-zero) limit.
        limit_known = moving[inner] & np.isfinite(limit[inner]) & (limit[inner] > 0)
        known_s = float(dt[inner][limit_known].sum())
        over_s = float(dt[inner][limit_known & (speed[inner] > limit[inner])].sum())
        t_start, t_end = times[s], times[e]
        last = index == len(bounds) - 1
        counts = {
            name: int(((ts >= t_start) & ((ts <= t_end) if last else (ts < t_end))).sum())
            for name, ts in event_times.items()
        }
        path = describe_path(geo.profile(x[points], y[points]))
        start_ts = pd.Timestamp(t_start)
        duration_h = (seconds[e] - seconds[s]) / 3600
        records.append(
            {
                "segment_id": f"{trip['trip_id'].iloc[0]}_{index:03d}",
                "trip_id": trip["trip_id"].iloc[0],
                "enabled_code": enabled_code,  # private lookup key only; never a model input
                "vehicle_key": vehicle_profile_id(enabled_code),
                "vehicle_type": vehicle_type,
                "segment_index": index,
                "is_tail": is_tail,
                "start_time": start_ts,
                "end_time": pd.Timestamp(t_end),
                "duration_min": float((seconds[e] - seconds[s]) / 60),
                "segment_km": km,
                "fuel_L": litres,
                "L_per_100km": litres / km * 100 if km > 0 else np.nan,
                **path,
                "speed_limit_band": _speed_limit_band(limit[points], config),
                "time_band": config.time_band(start_ts.hour),
                "mean_speed_kph": km / duration_h if duration_h > 0 else np.nan,
                "moving_seconds": moving_s,
                "idle_min_per_km": idle_s / 60 / km if km > 0 else np.nan,
                "harsh_acc_per_km": counts["n_harsh_acc"] / km if km > 0 else np.nan,
                "harsh_brake_per_km": counts["n_harsh_brake"] / km if km > 0 else np.nan,
                "overspeed_share": over_s / known_s if known_s > 0 else np.nan,
                "n_harsh_acc": counts["n_harsh_acc"],
                "n_harsh_brake": counts["n_harsh_brake"],
                "idle_seconds": idle_s,
                "speed_limit_known_seconds": known_s,
                "point_count": int(e - s + 1),
            }
        )
    return records


def build_segments(telemetry: pd.DataFrame, onsets: pd.DataFrame, config: FuelConfig, geo: GeoContext, length_km: float | None = None) -> pd.DataFrame:
    length = length_km or config.segment.length_km
    onset_groups = {k: g for k, g in onsets.groupby("trip_id")}
    empty = onsets.iloc[0:0]
    records: list[dict] = []
    for trip_id, trip in telemetry.groupby("trip_id", sort=True):
        if trip["enabled_code"].nunique() != 1:
            continue
        records.extend(trip_segments(trip, onset_groups.get(trip_id, empty), config, geo, length))
    segments = pd.DataFrame.from_records(records)
    if segments.empty:
        return segments
    segments = segments.sort_values(["start_time", "segment_id"], kind="mergesort").reset_index(drop=True)
    c = config.segment
    segments["l100_in_range"] = segments["L_per_100km"].between(c.min_l_per_100km, c.max_l_per_100km)
    return segments


def time_split(start_times: pd.Series, train_months: int) -> pd.Timestamp:
    first = pd.Timestamp(start_times.min())
    return pd.Timestamp(year=first.year, month=first.month, day=1) + pd.DateOffset(months=train_months)


def band_label(code: int) -> str:
    return ROAD_BANDS[code]


__all__ = ["BAND_CODE", "GeoContext", "build_segments", "load_onsets", "load_telemetry", "segment_boundaries", "time_split"]
