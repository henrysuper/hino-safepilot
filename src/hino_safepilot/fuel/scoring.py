"""Section 6: fuel score and explainable fuel report.

expected fuel = model prediction for the trip as actually driven (route,
time band, actual congestion) with behaviour set to the *normal* driver
type's expected values. Score = base + (1 - actual / expected) x scale,
clipped to 0-100 (base 75, scale 100 by default). Only trips >= 10 km.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .artifacts import FuelArtifacts
from .config import FuelConfig
from .maps import BEHAVIOURS, explode_grids
from .model import predict_litres

BEHAVIOUR_LABELS = {
    "idle_min_per_km": "怠速時間",
    "harsh_acc_per_km": "急加速",
    "harsh_brake_per_km": "急減速",
    "overspeed_share": "超速時間比例",
}
ROAD_BAND_LABELS = {"highway": "快速/高速道路", "normal": "一般道路", "city": "市區道路"}
TYPICAL_FLOOR = {"idle_min_per_km": 0.05, "harsh_acc_per_km": 0.02, "harsh_brake_per_km": 0.02, "overspeed_share": 0.02}
ADVICE = {
    "idle_min_per_km": "停車等候或卸貨時請熄火，避免長時間怠速。",
    "harsh_acc_per_km": "起步與加速請平順給油。",
    "harsh_brake_per_km": "拉開跟車距離、提早預判減速。",
    "overspeed_share": "維持在速限內穩定巡航。",
}


def score_from_ratio(ratio: np.ndarray, config: FuelConfig) -> np.ndarray:
    return np.clip(config.score.base + (1 - np.asarray(ratio, dtype=float)) * config.score.scale, 0, 100)


def score_trips(segments: pd.DataFrame, artifacts: FuelArtifacts, booster, columns, config: FuelConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (trip table, segment table with expected fuel and expected behaviour)."""

    segments = segments.reset_index(drop=True)
    grid_rows = explode_grids(segments)
    seg = segments.copy()
    seg["congestion_index"] = artifacts.congestion.actual(segments, grid_rows)  # actual traffic is not the driver's fault
    features = artifacts.feature_frame(seg, "expected", driver_type="normal", grid_rows=grid_rows)
    seg["expected_fuel_L"] = predict_litres(booster, features, columns, seg["segment_km"])
    for behaviour in BEHAVIOURS:
        seg[f"expected_{behaviour}"] = features[behaviour].to_numpy()
        # Fuel impact of this one behaviour: actual value instead of the normal expectation.
        swapped = features.copy()
        swapped[behaviour] = segments[behaviour].fillna(features[behaviour]).to_numpy()
        seg[f"impact_{behaviour}_L"] = predict_litres(booster, swapped, columns, seg["segment_km"]) - seg["expected_fuel_L"]
    seg["fuel_ratio"] = seg["fuel_L"] / seg["expected_fuel_L"].where(seg["expected_fuel_L"] > 0)
    grouped = seg.groupby("trip_id")
    trips = pd.DataFrame({
        "enabled_code": grouped["enabled_code"].first(),
        "vehicle_type": grouped["vehicle_type"].first(),
        "start_time": grouped["start_time"].min(),
        "km": grouped["segment_km"].sum(),
        "actual_fuel_L": grouped["fuel_L"].sum(),
        "expected_fuel_L": grouped["expected_fuel_L"].sum(),
        "segments": grouped.size(),
        "city_share": grouped.apply(lambda g: np.average(g["city_share"], weights=g["segment_km"]), include_groups=False),
        "mean_slope": grouped.apply(lambda g: np.average(g["avg_slope"].fillna(0), weights=g["segment_km"]), include_groups=False),
    })
    trips["fuel_ratio"] = trips["actual_fuel_L"] / trips["expected_fuel_L"]
    trips["scored"] = trips["km"] >= config.score.min_trip_km
    trips["fuel_score"] = np.where(trips["scored"], score_from_ratio(trips["fuel_ratio"], config), np.nan)
    return trips.reset_index(), seg


def period_scores(trips: pd.DataFrame, key: str = "enabled_code") -> pd.DataFrame:
    """Km-weighted mean of trip scores per key (vehicle-linked: one driver per vehicle trip)."""

    scored = trips[trips["scored"]]
    grouped = scored.groupby(key)
    return pd.DataFrame({
        "trips": grouped.size(),
        "km": grouped["km"].sum(),
        "period_score": grouped.apply(lambda g: np.average(g["fuel_score"], weights=g["km"]), include_groups=False),
        "fuel_ratio": grouped["actual_fuel_L"].sum() / grouped["expected_fuel_L"].sum(),
    })


def _r(value, digits=2):
    return None if value is None or not math.isfinite(float(value)) else round(float(value), digits)


def trip_report_facts(trip: pd.Series, segments: pd.DataFrame, health: float | None, config: FuelConfig, top_segments: int = 2,
                      low_health: float = 75.0) -> dict:
    """Structured facts for one trip: worst segments and the behaviour that exceeded most."""

    seg = segments.sort_values("segment_index").reset_index(drop=True)
    seg["excess_L"] = seg["fuel_L"] - seg["expected_fuel_L"]
    worst = seg[seg["excess_L"] > 0].nlargest(top_segments, "excess_L")
    findings = []
    for _, row in worst.iterrows():
        items = []
        for behaviour in BEHAVIOURS:
            expected = float(row[f"expected_{behaviour}"])
            actual = float(row[behaviour]) if pd.notna(row[behaviour]) else 0.0
            # "x times typical" is only meaningful when the typical value is not ~0.
            ratio = actual / expected if expected >= TYPICAL_FLOOR[behaviour] else None
            items.append({"behaviour": behaviour, "label": BEHAVIOUR_LABELS[behaviour], "actual": _r(actual, 3), "typical": _r(expected, 3),
                          "times_typical": _r(ratio, 1) if ratio is not None else None,
                          "fuel_impact_L": _r(row.get(f"impact_{behaviour}_L", np.nan), 2), "exceeds": actual > expected})
        # Main behaviour = the one whose deviation costs the most fuel according to the model.
        costly = [i for i in items if i["exceeds"] and (i["fuel_impact_L"] or 0) > 0.02]
        top = max(costly, key=lambda i: i["fuel_impact_L"]) if costly else None
        findings.append({
            "segment_no": int(row["segment_index"]) + 1,
            "road_band": row["road_band"],
            "road_band_label": ROAD_BAND_LABELS.get(row["road_band"], row["road_band"]),
            "km": _r(row["segment_km"], 1),
            "actual_fuel_L": _r(row["fuel_L"], 2),
            "expected_fuel_L": _r(row["expected_fuel_L"], 2),
            "excess_pct": _r((row["fuel_L"] / row["expected_fuel_L"] - 1) * 100, 0) if row["expected_fuel_L"] > 0 else None,
            "behaviours": items,
            "main_behaviour": top,
            "advice": ADVICE[top["behaviour"]] if top else None,
        })
    ratio = float(trip["fuel_ratio"])
    if ratio <= 1:
        findings = []  # nothing over-used: no "worst segment" story to tell
    return {
        "report_type": "trip_fuel_report",
        "trip": {"date": pd.Timestamp(trip["start_time"]).strftime("%Y-%m-%d"), "km": _r(trip["km"], 1), "segments": int(trip["segments"]),
                 "vehicle_type": int(trip["vehicle_type"])},
        "score": {"fuel_score": _r(trip["fuel_score"], 0), "actual_fuel_L": _r(trip["actual_fuel_L"], 2),
                  "expected_fuel_L": _r(trip["expected_fuel_L"], 2), "vs_expected_pct": _r((ratio - 1) * 100, 0),
                  "baseline": "同車、同路線、同時段與實際路況下，normal 駕駛類型的模型預測油耗"},
        "worst_segments": findings,
        "vehicle_health": {"health_score": _r(health, 0), "low": bool(health is not None and health < low_health),
                           "note": "健康度偏低，超用可能部分與車況有關。" if health is not None and health < low_health else None},
        "money": {"excess_twd": _r(max(0.0, float(trip["actual_fuel_L"] - trip["expected_fuel_L"])) * config.mock.fuel_price_twd_per_l, 0),
                  "fuel_price_twd_per_l": config.mock.fuel_price_twd_per_l, "mock_label": config.mock.label},
    }
