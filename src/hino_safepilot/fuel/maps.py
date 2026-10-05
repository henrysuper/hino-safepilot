"""Steps 3-5: road behaviour map, historical congestion map and driver types.

All statistics are fitted on training-period segments only.

Behaviour (idle, harsh accel, harsh brake, overspeed) is modelled
multiplicatively:

    expected = prior(vehicle type, road band, time band)
               x grid factor (500 m grid, time band)
               x driver-type multiplier (driver type, road band)

The grid factor is observed / prior-expected for the vehicles that passed the
grid, shrunk toward 1 with w = n / (n + k) (spec step 3; k = 5): first the
all-time grid factor toward 1, then the time-band factor toward the all-time
factor. A never-driven grid has factor 1, i.e. the road-type average. The
congestion map uses the same shrinkage around a (road band, time band) prior.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .geo import grid_cell_ids, to_twd97, valid_lonlat

BEHAVIOURS = ("idle_min_per_km", "harsh_acc_per_km", "harsh_brake_per_km", "overspeed_share")
# (numerator column, denominator column) used for pooled rates
BEHAVIOUR_TOTALS = {
    "idle_min_per_km": ("idle_minutes", "segment_km"),
    "harsh_acc_per_km": ("n_harsh_acc", "segment_km"),
    "harsh_brake_per_km": ("n_harsh_brake", "segment_km"),
    "overspeed_share": ("overspeed_seconds", "speed_limit_known_seconds"),
}


def add_behaviour_totals(segments: pd.DataFrame) -> pd.DataFrame:
    frame = segments.copy()
    frame["idle_minutes"] = frame["idle_seconds"] / 60
    frame["overspeed_seconds"] = frame["overspeed_share"].fillna(0) * frame["speed_limit_known_seconds"]
    return frame


def explode_grids(segments: pd.DataFrame) -> pd.DataFrame:
    """(segment index, grid id, fraction of the segment's path in that grid)."""

    lengths = segments["grid_ids"].map(len)
    rows = np.repeat(segments.index.to_numpy(), lengths.to_numpy())
    grids = np.concatenate([np.asarray(g, dtype=object) for g in segments["grid_ids"] if len(g)]) if lengths.sum() else np.array([], dtype=object)
    weights = np.concatenate([np.asarray(w, dtype=float) / max(1, sum(w)) for w in segments["grid_weights"] if len(w)]) if lengths.sum() else np.array([])
    return pd.DataFrame({"seg": rows, "grid": grids, "frac": weights})


def _shrink(raw: pd.Series, n: pd.Series, k: float, toward) -> pd.Series:
    w = n / (n + k)
    return w * raw + (1 - w) * toward


# ------------------------------------------------------------------ behaviour priors
@dataclass
class BehaviourPrior:
    """Pooled rates by (type, band, time band) with fallbacks (type, band) -> band -> fleet."""

    levels: dict[str, pd.DataFrame] = field(default_factory=dict)
    min_km: float = 50.0

    @classmethod
    def fit(cls, segments: pd.DataFrame, min_km: float = 50.0) -> "BehaviourPrior":
        prior = cls(min_km=min_km)
        for name, keys in (("tbt", ["vehicle_type", "road_band", "time_band"]), ("tb", ["vehicle_type", "road_band"]), ("b", ["road_band"])):
            grouped = segments.groupby(keys)
            table = pd.DataFrame({"km": grouped["segment_km"].sum()})
            for behaviour, (num, den) in BEHAVIOUR_TOTALS.items():
                table[behaviour] = grouped[num].sum() / grouped[den].sum().replace(0, np.nan)
            prior.levels[name] = table
        fleet = {b: segments[n].sum() / segments[d].sum() for b, (n, d) in BEHAVIOUR_TOTALS.items()}
        prior.levels["fleet"] = pd.DataFrame([fleet])
        return prior

    def rates(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Prior rate for each row of ``frame`` (needs vehicle_type, road_band, time_band)."""

        out = pd.DataFrame(index=frame.index, columns=list(BEHAVIOURS), dtype=float)
        for name, keys in (("tbt", ["vehicle_type", "road_band", "time_band"]), ("tb", ["vehicle_type", "road_band"]), ("b", ["road_band"])):
            table = self.levels[name]
            table = table[table["km"] >= self.min_km]
            joined = frame[keys].join(table, on=keys)
            for behaviour in BEHAVIOURS:
                out[behaviour] = out[behaviour].fillna(joined[behaviour])
        fleet = self.levels["fleet"].iloc[0]
        return out.fillna({b: fleet[b] for b in BEHAVIOURS})

    def to_json(self) -> dict:
        return {name: table.reset_index().to_dict(orient="records") for name, table in self.levels.items()} | {"min_km": self.min_km}

    @classmethod
    def from_json(cls, payload: dict) -> "BehaviourPrior":
        keys = {"tbt": ["vehicle_type", "road_band", "time_band"], "tb": ["vehicle_type", "road_band"], "b": ["road_band"]}
        levels = {name: pd.DataFrame(payload[name]).set_index(k) for name, k in keys.items()}
        levels["fleet"] = pd.DataFrame(payload["fleet"])
        return cls(levels=levels, min_km=payload["min_km"])


def expected_totals(frame: pd.DataFrame, rates: pd.DataFrame) -> pd.DataFrame:
    """Turn per-row rates into expected totals with the same units as the observations."""

    out = pd.DataFrame(index=frame.index)
    for behaviour, (_, den) in BEHAVIOUR_TOTALS.items():
        out[behaviour] = rates[behaviour] * frame[den]
    return out


def observed_totals(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({b: frame[n] for b, (n, _) in BEHAVIOUR_TOTALS.items()}, index=frame.index)


# ------------------------------------------------------------------ grid factor maps
@dataclass
class GridFactorMap:
    """Shrunk observed/expected ratios per grid and per (grid, time band)."""

    columns: tuple[str, ...]
    all_time: pd.DataFrame  # index grid -> factor columns + n
    by_band: pd.DataFrame  # index (grid, time_band) -> factor columns + n
    k: float

    @classmethod
    def fit(cls, grid_rows: pd.DataFrame, observed: pd.DataFrame, expected: pd.DataFrame, time_band: pd.Series, k: float) -> "GridFactorMap":
        columns = tuple(observed.columns)
        frac = grid_rows["frac"].to_numpy()[:, None]
        obs = pd.DataFrame(observed.loc[grid_rows["seg"]].to_numpy() * frac, columns=columns)
        exp = pd.DataFrame(expected.loc[grid_rows["seg"]].to_numpy() * frac, columns=columns)
        keys = pd.DataFrame({"grid": grid_rows["grid"].to_numpy(), "time_band": time_band.loc[grid_rows["seg"]].to_numpy()})
        def aggregate(group_keys):
            o = obs.groupby([keys[c] for c in group_keys]).sum()
            e = exp.groupby([keys[c] for c in group_keys]).sum()
            n = keys.groupby(group_keys).size()
            return o, e, n
        o_all, e_all, n_all = aggregate(["grid"])
        raw_all = (o_all / e_all.replace(0, np.nan)).fillna(1.0)
        all_time = raw_all.apply(lambda col: _shrink(col, n_all, k, 1.0))
        all_time["n"] = n_all
        o_b, e_b, n_b = aggregate(["grid", "time_band"])
        raw_b = (o_b / e_b.replace(0, np.nan))
        parent = all_time.reindex(raw_b.index.get_level_values("grid"))[list(columns)].set_axis(raw_b.index)
        raw_b = raw_b.fillna(parent)
        by_band = pd.DataFrame({c: _shrink(raw_b[c], n_b, k, parent[c]) for c in columns}, index=raw_b.index)
        by_band["n"] = n_b
        return cls(columns, all_time, by_band, k)

    def factors(self, grid_rows: pd.DataFrame, time_band: pd.Series, index: pd.Index) -> pd.DataFrame:
        """Path-weighted factor for each segment (1.0 where a grid was never driven)."""

        tb = time_band.loc[grid_rows["seg"]].to_numpy()
        key = pd.MultiIndex.from_arrays([grid_rows["grid"].to_numpy(), tb], names=["grid", "time_band"])
        band = self.by_band.reindex(key)[list(self.columns)].reset_index(drop=True)
        fallback = self.all_time.reindex(grid_rows["grid"].to_numpy())[list(self.columns)].reset_index(drop=True)
        values = band.fillna(fallback).fillna(1.0)
        weighted = values.mul(grid_rows["frac"].to_numpy(), axis=0)
        weighted["seg"] = grid_rows["seg"].to_numpy()
        sums = weighted.groupby("seg").sum()
        weight = pd.Series(grid_rows["frac"].to_numpy(), index=grid_rows["seg"].to_numpy()).groupby(level=0).sum()
        out = sums.div(weight, axis=0).reindex(index)
        return out.fillna(1.0)

    def coverage(self, grid_rows: pd.DataFrame) -> pd.Series:
        """Share of each segment's path in grids with any training history."""

        seen = grid_rows["grid"].isin(self.all_time.index).to_numpy() * grid_rows["frac"].to_numpy()
        return pd.Series(seen, index=grid_rows["seg"].to_numpy()).groupby(level=0).sum()

    def to_frames(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        return self.all_time.reset_index(), self.by_band.reset_index()

    @classmethod
    def from_frames(cls, all_time: pd.DataFrame, by_band: pd.DataFrame, columns, k: float) -> "GridFactorMap":
        return cls(tuple(columns), all_time.set_index("grid"), by_band.set_index(["grid", "time_band"]), k)


# ------------------------------------------------------------------ congestion
def free_flow_speeds(telemetry: pd.DataFrame, cell_m: float, quantile: float, min_records: int, moving_min_speed: float) -> pd.DataFrame:
    """Per grid: free-flow speed = top-15 % (85th percentile) moving speed; fallback speed limit."""

    moving = telemetry[(telemetry["vehicle_status"] == 1) & (telemetry["gps_speed_kph"] >= moving_min_speed)]
    moving = moving[valid_lonlat(moving["longitude_deg"], moving["latitude_deg"])]
    x, y = to_twd97(moving["longitude_deg"], moving["latitude_deg"])
    grid = grid_cell_ids(x, y, cell_m)
    frame = pd.DataFrame({"grid": grid, "speed": moving["gps_speed_kph"].to_numpy(), "limit": moving["gps_speed_limit"].to_numpy()})
    grouped = frame.groupby("grid")
    table = pd.DataFrame({
        "records": grouped.size(),
        "speed_q": grouped["speed"].quantile(quantile),
        "limit_median": frame[frame["limit"] > 0].groupby("grid")["limit"].median(),
    })
    table["free_flow_kph"] = np.where(table["records"] >= min_records, table["speed_q"], table["limit_median"])
    table["free_flow_source"] = np.where(table["records"] >= min_records, "speed_quantile", np.where(table["limit_median"].notna(), "speed_limit", "none"))
    return table


def segment_free_flow(grid_rows: pd.DataFrame, free_flow: pd.Series, band_default: pd.Series, segments: pd.DataFrame) -> pd.Series:
    """Harmonic (time-based) free-flow speed along each segment's path."""

    ff = free_flow.reindex(grid_rows["grid"].to_numpy()).to_numpy()
    default = band_default.reindex(segments.loc[grid_rows["seg"], "road_band"].to_numpy()).to_numpy()
    ff = np.where(np.isfinite(ff) & (ff > 0), ff, default)
    time_per_km = pd.Series(grid_rows["frac"].to_numpy() / ff, index=grid_rows["seg"].to_numpy()).groupby(level=0).sum()
    frac = pd.Series(grid_rows["frac"].to_numpy(), index=grid_rows["seg"].to_numpy()).groupby(level=0).sum()
    speed = (frac / time_per_km).reindex(segments.index)
    return speed.fillna(band_default.reindex(segments["road_band"]).set_axis(segments.index))


@dataclass
class CongestionModel:
    free_flow: pd.Series  # grid -> km/h
    band_free_flow: pd.Series  # road_band -> km/h
    prior: pd.DataFrame  # (road_band, time_band) -> congestion
    grid_map: GridFactorMap
    clip: tuple[float, float]

    def segment_free_flow(self, segments: pd.DataFrame, grid_rows: pd.DataFrame) -> pd.Series:
        return segment_free_flow(grid_rows, self.free_flow, self.band_free_flow, segments)

    def actual(self, segments: pd.DataFrame, grid_rows: pd.DataFrame) -> pd.Series:
        ff = self.segment_free_flow(segments, grid_rows)
        return (segments["mean_speed_kph"] / ff).clip(*self.clip)

    def expected(self, segments: pd.DataFrame, grid_rows: pd.DataFrame) -> pd.Series:
        base = segments[["road_band", "time_band"]].join(self.prior, on=["road_band", "time_band"])["congestion"]
        base = base.fillna(segments["road_band"].map(self.prior.groupby(level=0)["congestion"].mean())).fillna(self.prior["congestion"].mean())
        factor = self.grid_map.factors(grid_rows, segments["time_band"], segments.index)["congestion"]
        return (base * factor).clip(*self.clip)


def fit_congestion(segments: pd.DataFrame, grid_rows: pd.DataFrame, free_flow_table: pd.DataFrame, k: float, clip) -> CongestionModel:
    ff = free_flow_table["free_flow_kph"].dropna()
    # Default per band: km-weighted median grid free-flow speed of that band's segments.
    rows = grid_rows.assign(band=segments.loc[grid_rows["seg"], "road_band"].to_numpy(), ff=ff.reindex(grid_rows["grid"].to_numpy()).to_numpy())
    band_ff = rows.dropna(subset=["ff"]).groupby("band")["ff"].median()
    model = CongestionModel(ff, band_ff, pd.DataFrame(), None, tuple(clip))  # type: ignore[arg-type]
    actual = model.actual(segments, grid_rows)
    weights = segments["segment_km"]
    frame = segments[["road_band", "time_band"]].assign(c=actual * weights, w=weights)
    prior = frame.groupby(["road_band", "time_band"]).sum()
    model.prior = pd.DataFrame({"congestion": prior["c"] / prior["w"]})
    base = segments[["road_band", "time_band"]].join(model.prior, on=["road_band", "time_band"])["congestion"]
    observed = pd.DataFrame({"congestion": actual * weights})
    expected = pd.DataFrame({"congestion": base * weights})
    model.grid_map = GridFactorMap.fit(grid_rows, observed, expected, segments["time_band"], k)
    return model


# ------------------------------------------------------------------ driver types
@dataclass
class DriverTypes:
    names: list[str]
    edges: list[float]  # composite-ratio quintile edges (4 values)
    multipliers: pd.DataFrame  # index (driver_type, road_band) -> behaviour multipliers + trips

    def to_json(self) -> dict:
        return {"names": self.names, "edges": self.edges, "multipliers": self.multipliers.reset_index().to_dict(orient="records")}

    @classmethod
    def from_json(cls, payload: dict) -> "DriverTypes":
        return cls(payload["names"], payload["edges"], pd.DataFrame(payload["multipliers"]).set_index(["driver_type", "road_band"]))

    def multiplier(self, driver_type: str, road_band: pd.Series) -> pd.DataFrame:
        if driver_type not in self.names:
            raise ValueError(f"Unknown driver type {driver_type!r}; expected one of {self.names}")
        table = self.multipliers.xs(driver_type, level="driver_type")[list(BEHAVIOURS)]
        out = table.reindex(road_band.to_numpy()).set_axis(road_band.index)
        return out.fillna(table.mean())


def fit_driver_types(segments: pd.DataFrame, observed: pd.DataFrame, expected: pd.DataFrame, names: list[str],
                     min_trip_km: float, ratio_cap: float, min_cell_trips: int) -> tuple[DriverTypes, pd.DataFrame]:
    """Quintiles of the trip-level composite behaviour ratio -> five driver types."""

    trip_obs = observed.groupby(segments["trip_id"]).sum()
    trip_exp = expected.groupby(segments["trip_id"]).sum()
    trip_km = segments.groupby("trip_id")["segment_km"].sum()
    ratios = (trip_obs / trip_exp.where(trip_exp > 0)).clip(upper=ratio_cap)
    composite = ratios.mean(axis=1, skipna=True)
    eligible = composite[(trip_km >= min_trip_km) & composite.notna()]
    edges = [float(eligible.quantile(q)) for q in (0.2, 0.4, 0.6, 0.8)]
    labels = pd.Series(np.searchsorted(edges, eligible.to_numpy(), side="right"), index=eligible.index).map(dict(enumerate(names)))
    trips = pd.DataFrame({"composite_ratio": eligible, "driver_type": labels, "trip_km": trip_km.reindex(eligible.index)})
    seg = segments[segments["trip_id"].isin(labels.index)]
    seg_type = seg["trip_id"].map(labels)
    obs = observed.loc[seg.index]
    exp = expected.loc[seg.index]
    type_obs, type_exp = obs.groupby(seg_type).sum(), exp.groupby(seg_type).sum()
    type_ratio = (type_obs / type_exp.where(type_exp > 0)).fillna(1.0)
    rows = []
    for band in sorted(seg["road_band"].dropna().unique()):
        in_band = seg["road_band"] == band
        cell_obs = obs[in_band].groupby(seg_type[in_band]).sum()
        cell_exp = exp[in_band].groupby(seg_type[in_band]).sum()
        cell_trips = seg[in_band].groupby(seg_type[in_band])["trip_id"].nunique()
        for behaviour in BEHAVIOURS:
            values, weights = [], []
            for driver_type in names:
                o = float(cell_obs[behaviour].get(driver_type, 0.0))
                e = float(cell_exp[behaviour].get(driver_type, 0.0))
                parent = float(type_ratio[behaviour].get(driver_type, 1.0))
                n = int(cell_trips.get(driver_type, 0))
                # Band cell shrunk toward the type's all-road multiplier (w = n / (n + min_cell_trips)).
                w = n / (n + min_cell_trips)
                raw = o / e if e > 0 else parent
                values.append(w * raw + (1 - w) * parent)
                weights.append(max(e, 1e-9))
            ordered = _isotonic_increasing(np.asarray(values), np.asarray(weights))
            for driver_type, value in zip(names, ordered):
                rows.append({"driver_type": driver_type, "road_band": band, "behaviour": behaviour, "value": float(value),
                             "trips": int(cell_trips.get(driver_type, 0))})
    long = pd.DataFrame(rows)
    table = long.pivot_table(index=["driver_type", "road_band"], columns="behaviour", values="value")
    table["trips"] = long.groupby(["driver_type", "road_band"])["trips"].first()
    table["low_sample"] = table["trips"] < min_cell_trips
    return DriverTypes(list(names), edges, table[[*BEHAVIOURS, "trips", "low_sample"]]), trips


def _isotonic_increasing(values: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Weighted pool-adjacent-violators: very_eco <= eco <= ... <= very_aggressive."""

    blocks = [[float(v), float(w), 1] for v, w in zip(values, weights)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0]:
            v1, w1, c1 = blocks[i]
            v2, w2, c2 = blocks[i + 1]
            blocks[i] = [(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, c1 + c2]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    return np.concatenate([[v] * c for v, _, c in blocks])

