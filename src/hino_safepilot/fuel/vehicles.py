"""Step 2 (vehicle health) and step 6 (vehicle efficiency offset).

Health uses only non-fuel signals: coolant temperature at comparable engine
load, DTC / overheat / overload event rates, engine hours and battery voltage.
Every component is compared with the same-type median, so the score says
"worse than similar trucks", not "a big truck".

The efficiency offset is total fuel / expected fuel, where expected fuel uses
same-type, same-road-band pooled L/100km (total fuel / total km). Training rows
get a leave-one-month-out offset so the model never sees a feature computed
from the segment's own fuel.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

HEALTH_COLUMNS = (
    "trip_id",
    "enabled_code",
    "timestamp",
    "vehicle_series_code",
    "vehicle_status",
    "total_mileage_km",
    "engine_coolant_temp_c",
    "engine_load_pct",
    "engine_total_time_h",
    "battery_mv",
)
EVENT_DTC, EVENT_OVERHEAT, EVENT_OVERLOAD = 9, 10, 11

HEALTH_COMPONENTS = {
    "coolant_excess_c": "相近引擎負載下冷卻水溫高於同型車（°C）",
    "dtc_overheat_per_1000km": "DTC 與過熱事件（每千公里）",
    "overload_per_100km": "引擎過載事件（每百公里）",
    "engine_hours_excess": "引擎累積運轉時數高於同型車（小時）",
    "battery_deficit_mv": "電瓶電壓低於同型車（mV）",
}


def load_health_telemetry(input_root: Path, before: pd.Timestamp) -> pd.DataFrame:
    dataset = ds.dataset(Path(input_root) / "telemetry_normalized", format="parquet", partitioning="hive")
    value_columns = [c for c in HEALTH_COLUMNS if c not in ("trip_id", "enabled_code", "timestamp")]
    frame = dataset.to_table(columns=[*HEALTH_COLUMNS, *(f"{c}_quality" for c in value_columns)]).to_pandas()
    for column in value_columns:
        frame.loc[frame[f"{column}_quality"] != "valid", column] = np.nan
        frame = frame.drop(columns=f"{column}_quality")
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame[frame["timestamp"] < before].reset_index(drop=True)


def load_health_events(input_root: Path, before: pd.Timestamp) -> pd.DataFrame:
    dataset = ds.dataset(Path(input_root) / "events_onsets", format="parquet", partitioning="hive")
    onsets = dataset.to_table(
        columns=["enabled_code", "event_type", "event_start_time", "event_start_time_quality", "start_time_ambiguous", "start_year_mismatch_observed"]
    ).to_pandas()
    keep = (
        (onsets["event_start_time_quality"] == "valid")
        & ~onsets["start_time_ambiguous"].fillna(False).astype(bool)
        & ~onsets["start_year_mismatch_observed"].fillna(False).astype(bool)
        & onsets["event_type"].isin([EVENT_DTC, EVENT_OVERHEAT, EVENT_OVERLOAD])
        & (pd.to_datetime(onsets["event_start_time"]) < before)
    )
    return onsets.loc[keep, ["enabled_code", "event_type"]]


def _trip_km(telemetry: pd.DataFrame) -> pd.Series:
    trips = telemetry.groupby(["enabled_code", "trip_id"])["total_mileage_km"].agg(lambda s: s.max() - s.min())
    return trips.clip(lower=0).groupby(level=0).sum()


def health_indicators(telemetry: pd.DataFrame, events: pd.DataFrame, load_bin_pct: float = 10.0, warm_coolant_c: float = 60.0) -> pd.DataFrame:
    """Raw per-vehicle indicators (higher = worse except battery, handled later)."""

    vehicle_type = telemetry.groupby("enabled_code")["vehicle_series_code"].agg(lambda s: s.mode().iloc[0])
    km = _trip_km(telemetry)
    warm = telemetry[(telemetry["vehicle_status"] == 1) & (telemetry["engine_coolant_temp_c"] >= warm_coolant_c) & telemetry["engine_load_pct"].notna()].copy()
    warm["load_bin"] = np.floor(warm["engine_load_pct"].clip(0, 99.9) / load_bin_pct)
    warm["vehicle_type"] = warm["enabled_code"].map(vehicle_type)
    type_bin = warm.groupby(["vehicle_type", "load_bin"])["engine_coolant_temp_c"].median().rename("type_bin_median")
    vehicle_bin = warm.groupby(["enabled_code", "vehicle_type", "load_bin"])["engine_coolant_temp_c"].agg(["median", "size"]).reset_index()
    vehicle_bin = vehicle_bin.join(type_bin, on=["vehicle_type", "load_bin"])
    vehicle_bin["excess"] = vehicle_bin["median"] - vehicle_bin["type_bin_median"]
    coolant = vehicle_bin.groupby("enabled_code").apply(lambda g: np.average(g["excess"], weights=g["size"]), include_groups=False)
    counts = events.groupby(["enabled_code", "event_type"]).size().unstack(fill_value=0)
    counts = counts.reindex(columns=[EVENT_DTC, EVENT_OVERHEAT, EVENT_OVERLOAD], fill_value=0).reindex(vehicle_type.index, fill_value=0)
    running = telemetry[telemetry["vehicle_status"].isin([1, 2]) & telemetry["battery_mv"].gt(0)]
    table = pd.DataFrame({
        "vehicle_type": vehicle_type,
        "train_km": km.reindex(vehicle_type.index),
        "coolant_excess_c": coolant.reindex(vehicle_type.index),
        "dtc_count": counts[EVENT_DTC],
        "overheat_count": counts[EVENT_OVERHEAT],
        "overload_count": counts[EVENT_OVERLOAD],
        "engine_hours": telemetry.groupby("enabled_code")["engine_total_time_h"].max().reindex(vehicle_type.index),
        "battery_median_mv": running.groupby("enabled_code")["battery_mv"].median().reindex(vehicle_type.index),
    })
    per_km = table["train_km"].where(table["train_km"] > 0)
    table["dtc_overheat_per_1000km"] = (table["dtc_count"] + table["overheat_count"]) / per_km * 1000
    table["overload_per_100km"] = table["overload_count"] / per_km * 100
    type_median = table.groupby("vehicle_type")
    table["engine_hours_excess"] = table["engine_hours"] - type_median["engine_hours"].transform("median")
    table["battery_deficit_mv"] = type_median["battery_median_mv"].transform("median") - table["battery_median_mv"]
    return table


def health_scores(indicators: pd.DataFrame, penalty_per_scale: float = 25.0) -> pd.DataFrame:
    """0-100 sub-scores (equal weight). Only the worse-than-type direction is penalised.

    Deviation = value - same-type median, divided by a fleet-wide robust scale
    (1.4826 x MAD of the deviations, falling back to the standard deviation).
    Each scale unit above the type median costs ``penalty_per_scale`` points.
    """

    table = indicators.copy()
    sub = []
    for component in HEALTH_COMPONENTS:
        values = table[component]
        if component in ("engine_hours_excess", "battery_deficit_mv"):
            deviation = values  # already relative to the type median
        else:
            deviation = values - table.groupby("vehicle_type")[component].transform("median")
        mad = float(1.4826 * np.nanmedian(np.abs(deviation - np.nanmedian(deviation))))
        scale = mad if mad > 0 else float(np.nanstd(deviation))
        z = (deviation / scale).clip(lower=0, upper=4) if scale > 0 else deviation * 0
        score = (100 - penalty_per_scale * z).fillna(100.0)
        table[f"{component}_deviation"] = deviation
        table[f"{component}_score"] = score
        sub.append(f"{component}_score")
    table["health_score"] = table[sub].mean(axis=1)
    return table


def type_band_rates(segments: pd.DataFrame) -> pd.Series:
    """Same-type, same-road-band pooled L/100km (total fuel / total km)."""

    grouped = segments.groupby(["vehicle_type", "road_band"])
    return grouped["fuel_L"].sum() / grouped["segment_km"].sum() * 100


def _offset(fuel: float, expected: float, prior_litres: float) -> float:
    return float((fuel + prior_litres) / (expected + prior_litres)) if expected + prior_litres > 0 else 1.0


def efficiency_offsets(train_segments: pd.DataFrame, prior_litres: float = 10.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(full-train offset per vehicle, leave-one-month-out offset per segment, rates).

    ``prior_litres`` shrinks vehicles with little data toward 1.0.
    """

    rates = type_band_rates(train_segments)
    frame = train_segments.copy()
    frame["expected_L"] = frame.set_index(["vehicle_type", "road_band"]).index.map(rates).to_numpy() * frame["segment_km"] / 100
    frame["month"] = frame["start_time"].dt.to_period("M")
    totals = frame.groupby("enabled_code")[["fuel_L", "expected_L"]].sum()
    full = totals.apply(lambda r: _offset(r["fuel_L"], r["expected_L"], prior_litres), axis=1)
    by_month = frame.groupby(["enabled_code", "month"])[["fuel_L", "expected_L"]].sum()
    lomo = {}
    for (code, month), row in by_month.iterrows():
        rest = totals.loc[code] - row
        lomo[(code, month)] = _offset(rest["fuel_L"], rest["expected_L"], prior_litres)
    per_segment = pd.Series([lomo[(c, m)] for c, m in zip(frame["enabled_code"], frame["month"])], index=frame.index)
    return full, per_segment, rates


def vehicle_table(indicators_scored: pd.DataFrame, offsets: pd.Series, aliases: dict[str, str]) -> pd.DataFrame:
    table = indicators_scored.copy()
    table["vehicle_efficiency_offset"] = offsets.reindex(table.index).fillna(1.0)
    table.insert(0, "vehicle_alias", table.index.map(aliases))
    table.index.name = "enabled_code"
    return table.reset_index()
