"""Checkpoint-1 diagnostics: segment counts, OSM vs speed-limit agreement, vehicle review.

Implements the spec's three credibility checks: different statistics on the same
data, per-vehicle comparison within type, and fuelLevel vs totalFuelUsed.
Outputs aggregates only (no telemetry rows).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .geo import ROAD_BANDS


def pooled_l100(frame: pd.DataFrame) -> float:
    km = frame["segment_km"].sum()
    return float(frame["fuel_L"].sum() / km * 100) if km > 0 else float("nan")


def _round(value, digits=1):
    return None if value is None or not np.isfinite(value) else round(float(value), digits)


def segment_summary(segments: pd.DataFrame, test_start: pd.Timestamp) -> dict:
    kept = segments[segments["l100_in_range"]]
    period = np.where(kept["start_time"] < test_start, "train", "test")
    return {
        "segments_before_l100_filter": int(len(segments)),
        "segments_after_l100_filter": int(len(kept)),
        "removed_by_l100_filter": int((~segments["l100_in_range"]).sum()),
        "tail_segments_kept": int(kept["is_tail"].sum()),
        "trips_with_segments": int(kept["trip_id"].nunique()),
        "by_period": pd.Series(period).value_counts().to_dict(),
        "by_type": {str(int(k)): int(v) for k, v in kept["vehicle_type"].value_counts().sort_index().items()},
        "by_road_band": kept["road_band"].fillna("unmatched").value_counts().to_dict(),
        "by_time_band": kept["time_band"].value_counts().to_dict(),
        "segment_km_quantiles": {str(q): _round(kept["segment_km"].quantile(q), 2) for q in (0.05, 0.5, 0.95)},
        "fuel_L_value_counts_top": {str(k): int(v) for k, v in kept["fuel_L"].round(1).value_counts().head(6).items()},
        "missing_share": {
            c: _round(kept[c].isna().mean(), 4)
            for c in ("road_band", "elevation_gain_m", "avg_slope", "overspeed_share", "idle_min_per_km")
        },
        "osm_matched_point_share_median": _round(kept["osm_matched_share"].median(), 3),
        "test_start": test_start.isoformat(),
    }


def osm_vs_speed_limit(segments: pd.DataFrame) -> dict:
    kept = segments[segments["l100_in_range"] & segments["road_band"].notna() & segments["speed_limit_band"].notna()]
    table = pd.crosstab(kept["road_band"], kept["speed_limit_band"]).reindex(index=list(ROAD_BANDS), columns=list(ROAD_BANDS), fill_value=0)
    km_table = kept.pivot_table(index="road_band", columns="speed_limit_band", values="segment_km", aggfunc="sum", fill_value=0).reindex(
        index=list(ROAD_BANDS), columns=list(ROAD_BANDS), fill_value=0
    )
    total = int(table.to_numpy().sum())
    agree = int(np.trace(table.to_numpy()))
    highway_row = table.loc["highway"]
    return {
        "segments_compared": total,
        "overall_agreement": _round(agree / total, 3) if total else None,
        "osm_highway_with_speed_limit_over_80_share": _round(highway_row["highway"] / highway_row.sum(), 3) if highway_row.sum() else None,
        "speed_limit_over_80_labelled_osm_highway_share": _round(table.loc["highway", "highway"] / table["highway"].sum(), 3) if table["highway"].sum() else None,
        "per_osm_band_agreement": {b: _round(table.loc[b, b] / table.loc[b].sum(), 3) if table.loc[b].sum() else None for b in ROAD_BANDS},
        "crosstab_rows_osm_cols_speed_limit": table.to_dict(orient="index"),
        "crosstab_km": {k: {c: _round(v, 0) for c, v in row.items()} for k, row in km_table.to_dict(orient="index").items()},
    }


def fuel_level_table(segments: pd.DataFrame, band_column: str) -> dict:
    kept = segments[segments["l100_in_range"] & segments[band_column].notna()]
    result = {}
    for vehicle_type, group in kept.groupby("vehicle_type"):
        result[str(int(vehicle_type))] = {
            band: {"pooled_l100": _round(pooled_l100(g)), "median_l100": _round(g["L_per_100km"].median()), "km": _round(g["segment_km"].sum(), 0)}
            for band, g in group.groupby(band_column)
        }
    return result


def per_vehicle_table(segments: pd.DataFrame, alias: dict[str, str]) -> list[dict]:
    kept = segments[segments["l100_in_range"]]
    type_band = {
        (t, b): pooled_l100(g) for (t, b), g in kept.dropna(subset=["road_band"]).groupby(["vehicle_type", "road_band"])
    }
    rows = []
    for code, group in kept.groupby("enabled_code"):
        vehicle_type = group["vehicle_type"].iloc[0]
        row = {
            "vehicle": alias[code],
            "vehicle_type": int(vehicle_type),
            "segments": int(len(group)),
            "km": _round(group["segment_km"].sum(), 0),
            "pooled_l100": _round(pooled_l100(group)),
            "mean_l100": _round(group["L_per_100km"].mean()),
            "median_l100": _round(group["L_per_100km"].median()),
        }
        for band in ROAD_BANDS:
            g = group[group["road_band"] == band]
            ref = type_band.get((vehicle_type, band))
            row[f"{band}_km"] = _round(g["segment_km"].sum(), 0)
            row[f"{band}_pooled_l100"] = _round(pooled_l100(g)) if len(g) else None
            row[f"{band}_vs_type"] = _round(pooled_l100(g) / ref, 2) if len(g) and ref else None
        rows.append(row)
    return sorted(rows, key=lambda r: (r["vehicle_type"], r["vehicle"]))


def fuel_level_cross_check(telemetry: pd.DataFrame, alias: dict[str, str], vehicle_type: dict[str, float],
                           smooth_points: int = 31, refuel_rise_pct: float = 3.0, min_daily_drop_pct: float = 2.0) -> list[dict]:
    """Compare daily fuelLevel drop (%) with daily totalFuelUsed (L).

    fuelLevel is reported in coarse steps and is noisy, so it is smoothed with a
    rolling median per vehicle. Days with a refuel (smoothed level rises more
    than ``refuel_rise_pct``) are skipped. Same-type vehicles should show a
    positive correlation and similar litres per 1 % level drop.
    """

    rows = []
    for code, vehicle in telemetry.sort_values("timestamp").groupby("enabled_code"):
        vehicle = vehicle[np.isfinite(vehicle["fuel_level_pct"]) & np.isfinite(vehicle["total_fuel_used_l"]) & (vehicle["fuel_level_pct"] > 0)]
        raw = vehicle["fuel_level_pct"].to_numpy(float)
        step = np.abs(np.diff(raw))
        smooth = pd.Series(raw).rolling(smooth_points, center=True, min_periods=5).median().to_numpy()
        frame = vehicle.assign(day=vehicle["timestamp"].dt.date, level=smooth)
        daily = frame.groupby("day").agg(
            l0=("level", "first"), l1=("level", "last"), lmax=("level", "max"),
            f0=("total_fuel_used_l", "first"), f1=("total_fuel_used_l", "last"),
        )
        daily = daily[(daily.lmax - daily.l0 < refuel_rise_pct) & (daily.l0 - daily.l1 > min_daily_drop_pct) & (daily.f1 > daily.f0)]
        used, drop = daily.f1 - daily.f0, daily.l0 - daily.l1
        rows.append({
            "vehicle": alias[code],
            "vehicle_type": int(vehicle_type.get(code, 0)),
            "level_step_median_pct": _round(float(np.median(step)), 1) if step.size else None,
            "level_step_p95_pct": _round(float(np.quantile(step, 0.95)), 1) if step.size else None,
            "comparable_days": int(len(daily)),
            "daily_correlation": _round(float(np.corrcoef(used, drop)[0, 1]), 2) if len(daily) > 5 else None,
            "litres_per_level_pct": _round(float(used.sum() / drop.sum()), 2) if len(daily) else None,
        })
    return sorted(rows, key=lambda r: (r["vehicle_type"], r["vehicle"]))


def behaviour_by_vehicle_band(segments: pd.DataFrame, alias: dict[str, str], band: str = "city") -> list[dict]:
    """Mean driving behaviour per vehicle on one road band, vs. its type (time-weighted by km)."""

    kept = segments[segments["l100_in_range"] & (segments["road_band"] == band)]
    rows = []
    for code, group in kept.groupby("enabled_code"):
        km = group["segment_km"].sum()
        rows.append({
            "vehicle": alias[code],
            "vehicle_type": int(group["vehicle_type"].iloc[0]),
            "km": _round(km, 0),
            "idle_min_per_km": _round(group["idle_seconds"].sum() / 60 / km, 2),
            "harsh_acc_per_100km": _round(group["n_harsh_acc"].sum() / km * 100, 1),
            "harsh_brake_per_100km": _round(group["n_harsh_brake"].sum() / km * 100, 1),
            "mean_speed_kph": _round(km / (group["duration_min"].sum() / 60), 1),
            "pooled_l100": _round(pooled_l100(group)),
        })
    return sorted(rows, key=lambda r: (r["vehicle_type"], r["vehicle"]))
