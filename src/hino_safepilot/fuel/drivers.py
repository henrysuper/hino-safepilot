"""Virtual drivers (此為模擬資料): roster, trip assignment, personal driving averages.

The data set has no driver identity. The organiser allows a simulated roster,
so this module builds one and attaches it to the real history:

1. **Roster** - drivers per series (vehicle type), each with a usual truck of
   that series, a simulated style target (a quantile of the fleet's
   behaviour) and, for a few, a later start date ("new drivers": they only
   start working in the test period).
2. **Assignment** - each real *vehicle-day* (one vehicle, one local date) is
   given to one driver of the same series. The day's composite behaviour ratio
   (observed / expected for those roads and times, mean over idle, harsh
   acceleration, harsh braking, overspeed) is ranked within its series; a
   driver is drawn with weight exp(-(rank - style target)^2 / 2 sigma^2),
   times a preference for the truck's usual drivers, so each driver gets days
   of a consistent style. One driver drives at most one vehicle a day.
   This is a designed scenario: the consistency of a driver is *simulated*.
3. **Personal averages** - for each road type (highway / normal / city) a
   driver's idle, harsh acceleration, harsh braking and overspeed, as a ratio
   to what is typical on the same roads at the same time (expected = vehicle
   type x road type x time band prior x 500 m grid factor). Only the driver's
   earlier days are used (no current-day or future leakage). Few trips are
   shrunk toward the most similar established drivers, few kilometres on one
   road type toward the driver's own all-road ratio.

The fuel model itself is trained on real segment behaviour and never sees a
driver identity, so adding drivers needs no retraining: a driver changes the
prediction only through their expected behaviour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .maps import BEHAVIOURS, expected_totals, observed_totals

BANDS = ("highway", "normal", "city")
DRIVER_MULTS = ("driver_idle_mult", "driver_acc_mult", "driver_brake_mult", "driver_overspeed_mult")  # per-segment columns
MULT_OF = dict(zip(BEHAVIOURS, DRIVER_MULTS))
STYLE_LABELS = ("很省油", "省油", "一般", "偏耗油", "很耗油")  # simulated persona description only
MOCK_NOTE = "駕駛名單、車牌與行程分配為模擬資料；駕駛行為與油耗數值來自真實資料。"
ONES = (1.0, 1.0, 1.0, 1.0)


# ------------------------------------------------------------------ profiles
@dataclass(frozen=True)
class DriverProfile:
    """Behaviour ratios to the typical value on the same roads (1.0 = typical), overall and per road type."""

    multipliers: tuple[float, float, float, float] = ONES  # all roads, order of BEHAVIOURS
    by_road: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)
    history_trips: int = 0
    road_segments: dict[str, int] = field(default_factory=dict)
    driver_id: str | None = None
    neighbours: tuple[str, ...] = ()
    own_weight: float = 0.0

    @classmethod
    def neutral(cls) -> "DriverProfile":
        return cls()

    def row_multipliers(self, road_band: pd.Series) -> np.ndarray:
        """(n, 4) multipliers for segments on the given road types."""

        table = {band: self.by_road.get(band, self.multipliers) for band in BANDS}
        return np.array([table.get(b, self.multipliers) for b in road_band.to_numpy()], dtype=float).reshape(-1, len(BEHAVIOURS))

    def to_json(self) -> dict:
        def named(values):
            return {b: round(float(v), 4) for b, v in zip(BEHAVIOURS, values)}

        return {"driver_id": self.driver_id, "multipliers": named(self.multipliers),
                "by_road": {band: named(values) for band, values in self.by_road.items()},
                "history_trips": int(self.history_trips), "road_segments": {k: int(v) for k, v in self.road_segments.items()},
                "neighbours": list(self.neighbours), "own_weight": round(float(self.own_weight), 3)}

    @classmethod
    def from_json(cls, payload: dict) -> "DriverProfile":
        def values(d):
            return tuple(float(d[b]) for b in BEHAVIOURS)

        return cls(values(payload["multipliers"]), {band: values(v) for band, v in payload.get("by_road", {}).items()},
                   int(payload.get("history_trips", 0)), dict(payload.get("road_segments", {})), payload.get("driver_id"),
                   tuple(payload.get("neighbours", ())), float(payload.get("own_weight", 0.0)))


def apply_profile(features: pd.DataFrame, multipliers) -> pd.DataFrame:
    """Expected behaviour x driver multipliers ((n, 4) array or a 4-tuple, order of BEHAVIOURS)."""

    out = features.copy()
    mult = np.broadcast_to(np.asarray(multipliers, dtype=float), (len(out), len(BEHAVIOURS)))
    for i, behaviour in enumerate(BEHAVIOURS):
        out[behaviour] = out[behaviour].to_numpy(float) * mult[:, i]
    out["overspeed_share"] = out["overspeed_share"].clip(0, 1)
    return out


def shrink_profile(raw: np.ndarray, n: int, pool_raw: np.ndarray, pool_n: np.ndarray, pool_ids: list[str], k_trips: float,
                   neighbours: int, min_neighbour_trips: int, clip: tuple[float, float]) -> tuple[np.ndarray, tuple[str, ...], float]:
    """Own all-road ratios shrunk toward the mean of the most similar established drivers.

    weight on own history w = n / (n + k_trips). With no history (n = 0) the
    driver cannot be compared yet and gets the typical value (1.0).
    """

    raw = np.asarray(raw, dtype=float)
    pool_raw = np.asarray(pool_raw, dtype=float).reshape(-1, len(BEHAVIOURS))
    pool_n = np.asarray(pool_n, dtype=float)
    target = np.ones(len(BEHAVIOURS))
    chosen: tuple[str, ...] = ()
    if n > 0:
        eligible = np.flatnonzero(pool_n >= min_neighbour_trips)
        if len(eligible):
            w_pool = pool_n[eligible] / (pool_n[eligible] + k_trips)
            stable = w_pool[:, None] * np.nan_to_num(pool_raw[eligible], nan=1.0) + (1 - w_pool[:, None])
            own = np.where(np.isfinite(raw), raw, 1.0)
            distance = np.abs(np.log(np.clip(stable, *clip)) - np.log(np.clip(own, *clip))).sum(axis=1)  # L1 in log space
            nearest = np.argsort(distance, kind="stable")[:neighbours]
            target = stable[nearest].mean(axis=0)
            chosen = tuple(pool_ids[eligible[i]] for i in nearest)
    w = n / (n + k_trips) if n > 0 else 0.0
    own = np.where(np.isfinite(raw), raw, target)
    return np.clip(w * own + (1 - w) * target, *clip), chosen, w


def _raw(obs: np.ndarray, exp: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(exp > 0, obs / np.where(exp > 0, exp, 1.0), np.nan)


def _profiles(ids: list[str], obs: np.ndarray, exp: np.ndarray, n_trips: np.ndarray, n_seg: np.ndarray, cfg,
              excluded_from_pool: set[str] | None = None) -> dict[str, DriverProfile]:
    """obs / exp: (drivers, bands, 4) totals; n_seg: (drivers, bands)."""

    excluded_from_pool = excluded_from_pool or set()
    overall_raw = _raw(obs.sum(axis=1), exp.sum(axis=1))
    clip = tuple(cfg.multiplier_clip)
    out = {}
    for i, driver in enumerate(ids):
        pool = [j for j in range(len(ids)) if j != i and ids[j] not in excluded_from_pool]
        overall, neighbours, w = shrink_profile(overall_raw[i], int(n_trips[i]), overall_raw[pool], n_trips[pool], [ids[j] for j in pool],
                                                cfg.profile_k_trips, cfg.neighbours, cfg.min_neighbour_trips, clip)
        by_road = {}
        for b, band in enumerate(BANDS):
            raw = _raw(obs[i, b], exp[i, b])
            w_band = n_seg[i, b] / (n_seg[i, b] + cfg.profile_k_segments)
            # Few kilometres on this road type: lean on the driver's own all-road ratio.
            by_road[band] = tuple(float(v) for v in np.clip(np.where(np.isfinite(raw), w_band * raw + (1 - w_band) * overall, overall), *clip))
        out[driver] = DriverProfile(tuple(float(v) for v in overall), by_road, int(n_trips[i]),
                                    {band: int(n_seg[i, b]) for b, band in enumerate(BANDS)}, driver, neighbours, w)
    return out


# ------------------------------------------------------------------ roster
def mock_plates(vehicles_by_type: dict[int, list[str]]) -> dict[str, str]:
    """Simulated plate per vehicle alias, e.g. 300 series -> SIM-3001 (此為模擬資料; not the real plate)."""

    return {alias: f"SIM-{int(t) // 100}{n:03d}" for t, aliases in vehicles_by_type.items() for n, alias in enumerate(sorted(aliases), 1)}


def build_roster(cfg, vehicles_by_type: dict[int, list[str]], mock_label: str) -> list[dict]:
    """Simulated roster: each driver belongs to one series and usually drives one truck of it.

    Drivers of the same truck get style targets far apart (driver i and i + vehicles share a truck)."""

    types = sorted(int(t) for t in vehicles_by_type)
    roster, number = [], 0
    for t_index, vehicle_type in enumerate(types):
        vehicles = sorted(vehicles_by_type[vehicle_type])
        count = int(cfg.per_type.get(str(vehicle_type), 0))
        new_q = cfg.new_driver_style_quantiles[t_index % len(cfg.new_driver_style_quantiles)] if cfg.new_driver_style_quantiles else None
        new_index = int(round(new_q * (count - 1))) if (new_q is not None and count > 1) else None
        for i in range(count):
            number += 1
            q = (i + 0.5) / count
            roster.append({
                "driver_id": f"D{number:02d}",
                "driver_name": f"模擬駕駛 {number:02d}",
                "primary_type": vehicle_type,
                "home_vehicle": vehicles[i % len(vehicles)] if vehicles else None,
                "style_target": round(q, 3),
                "style_label": STYLE_LABELS[min(len(STYLE_LABELS) - 1, int(q * len(STYLE_LABELS)))],
                "new_driver": i == new_index,
                "mock_label": mock_label,
            })
    return roster


# ------------------------------------------------------------------ assignment
def vehicle_days(segments: pd.DataFrame, rates: pd.DataFrame, ratio_cap: float) -> pd.DataFrame:
    """One row per (vehicle, local date): type, trips, km and the composite behaviour ratio.

    ``rates``: expected behaviour per segment (same index as ``segments``)."""

    obs = observed_totals(segments)
    exp = expected_totals(segments, rates)
    key = [segments["enabled_code"], segments["start_time"].dt.normalize().rename("day")]
    o, e = obs.groupby(key).sum(), exp.groupby(key).sum()
    ratio = (o / e.where(e > 0)).clip(upper=ratio_cap)
    grouped = segments.groupby(key)
    days = pd.DataFrame({
        "vehicle_type": grouped["vehicle_type"].first().astype(int),
        "trips": grouped["trip_id"].nunique(),
        "km": grouped["segment_km"].sum(),
        "composite_ratio": ratio.mean(axis=1, skipna=True),
    })
    days["style_rank"] = days.groupby("vehicle_type")["composite_ratio"].rank(pct=True).fillna(0.5)
    return days


def assign_drivers(days: pd.DataFrame, roster: list[dict], test_start: pd.Timestamp, cfg, aliases: dict[str, str] | None = None) -> pd.Series:
    """(enabled_code, day) -> driver_id of the same series. Deterministic for a fixed seed.

    ``aliases`` maps enabled_code -> vehicle alias (the roster's home_vehicle); the truck's usual
    drivers are preferred (other drivers of the series weigh ``other_vehicle_weight``)."""

    rng = np.random.default_rng(cfg.seed)
    aliases = aliases or {}
    ids = [d["driver_id"] for d in roster]
    target = np.array([d["style_target"] for d in roster])
    primary = np.array([d["primary_type"] for d in roster])
    home = np.array([d.get("home_vehicle") for d in roster], dtype=object)
    new = np.array([d["new_driver"] for d in roster])
    out = {}
    ordered = days.reset_index().sort_values(["day", "vehicle_type", "enabled_code"])
    for day, rows in ordered.groupby("day", sort=True):
        busy = np.zeros(len(roster), dtype=bool)
        started = (~new) | (day >= test_start)
        for row in rows.itertuples():
            allowed = (primary == row.vehicle_type) & ~busy & started
            if not allowed.any():
                out[(row.enabled_code, day)] = None
                continue
            weight = np.exp(-((row.style_rank - target) ** 2) / (2 * cfg.assignment_sigma ** 2))
            weight = weight * np.where(home == aliases.get(row.enabled_code, row.enabled_code), 1.0, cfg.other_vehicle_weight) * allowed
            if weight.sum() <= 0:
                weight = allowed.astype(float)
            pick = int(rng.choice(len(roster), p=weight / weight.sum()))
            busy[pick] = True
            out[(row.enabled_code, day)] = ids[pick]
    series = pd.Series(out, name="driver_id")
    series.index = series.index.set_names(["enabled_code", "day"])
    return series


def attach_drivers(segments: pd.DataFrame, assignment: pd.Series) -> pd.Series:
    key = pd.MultiIndex.from_arrays([segments["enabled_code"], segments["start_time"].dt.normalize()])
    return pd.Series(assignment.reindex(key).to_numpy(), index=segments.index, name="driver_id")


# ------------------------------------------------------------------ history -> profiles
def trip_totals(segments: pd.DataFrame, rates: pd.DataFrame) -> pd.DataFrame:
    """Per (trip, road type): driver, day, segments, observed and expected behaviour totals."""

    obs = observed_totals(segments).add_prefix("obs_")
    exp = expected_totals(segments, rates).add_prefix("exp_")
    frame = pd.concat([segments[["trip_id", "road_band", "segment_km"]].assign(segments=1), obs, exp], axis=1)
    rows = frame.groupby(["trip_id", "road_band"]).sum().reset_index()
    first = segments.groupby("trip_id").agg(driver_id=("driver_id", "first"), start_time=("start_time", "min"),
                                            enabled_code=("enabled_code", "first"), vehicle_type=("vehicle_type", "first"),
                                            trip_km=("segment_km", "sum"))
    rows = rows.join(first, on="trip_id")
    rows["day"] = rows["start_time"].dt.normalize()
    return rows


def _accumulate(rows: pd.DataFrame, index: dict[str, int], obs: np.ndarray, exp: np.ndarray, n_trips: np.ndarray, n_seg: np.ndarray) -> None:
    band_index = {b: i for i, b in enumerate(BANDS)}
    for row in rows.itertuples():
        i, b = index.get(row.driver_id), band_index.get(row.road_band)
        if i is None or b is None:
            continue
        obs[i, b] += np.nan_to_num(np.array([getattr(row, f"obs_{x}") for x in BEHAVIOURS], dtype=float))
        exp[i, b] += np.nan_to_num(np.array([getattr(row, f"exp_{x}") for x in BEHAVIOURS], dtype=float))
        n_seg[i, b] += row.segments
    for driver, trips in rows.groupby("driver_id")["trip_id"].nunique().items():
        if driver in index:
            n_trips[index[driver]] += trips


def _state(n: int):
    return np.zeros((n, len(BANDS), len(BEHAVIOURS))), np.zeros((n, len(BANDS), len(BEHAVIOURS))), np.zeros(n), np.zeros((n, len(BANDS)))


def daily_profiles(rows: pd.DataFrame, roster: list[dict], cfg) -> pd.DataFrame:
    """Every driver's profile at the start of every day that has trips (history strictly before that day).

    Columns: day, driver_id, driver_history_trips, neighbours, own_weight, the all-road ratios
    (DRIVER_MULTS) and per road type ``<band>:<behaviour>``."""

    ids = [d["driver_id"] for d in roster]
    index = {d: i for i, d in enumerate(ids)}
    obs, exp, n_trips, n_seg = _state(len(ids))
    out = []
    for day, today in rows[rows["driver_id"].notna()].groupby("day", sort=True):
        for driver, p in _profiles(ids, obs, exp, n_trips, n_seg, cfg).items():
            row = {"day": day, "driver_id": driver, "driver_history_trips": p.history_trips, "neighbours": list(p.neighbours),
                   "own_weight": p.own_weight, **{MULT_OF[b]: v for b, v in zip(BEHAVIOURS, p.multipliers)}}
            for band, values in p.by_road.items():
                row.update({f"{band}:{b}": v for b, v in zip(BEHAVIOURS, values)})
            out.append(row)
        _accumulate(today, index, obs, exp, n_trips, n_seg)
    return pd.DataFrame(out)


def daily_profile(daily_row: pd.Series) -> DriverProfile:
    """DriverProfile from one row of ``daily_profiles``."""

    return DriverProfile(tuple(float(daily_row[MULT_OF[b]]) for b in BEHAVIOURS),
                         {band: tuple(float(daily_row[f"{band}:{b}"]) for b in BEHAVIOURS) for band in BANDS},
                         int(daily_row["driver_history_trips"]), driver_id=daily_row.get("driver_id"))


def segment_multipliers(segments: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """Per segment: its driver's ratios (as of the trip's day) for the segment's road type, plus history trips."""

    key = segments[["driver_id"]].assign(day=segments["start_time"].dt.normalize())
    joined = key.merge(daily.drop(columns=["neighbours"]), on=["day", "driver_id"], how="left").set_axis(segments.index)
    out = pd.DataFrame(index=segments.index)
    for behaviour in BEHAVIOURS:
        values = joined[MULT_OF[behaviour]].copy()
        for band in BANDS:
            on_band = (segments["road_band"] == band).to_numpy()
            values[on_band] = joined.loc[on_band, f"{band}:{behaviour}"]
        out[MULT_OF[behaviour]] = values.fillna(1.0).to_numpy(float)
    out["driver_history_trips"] = joined["driver_history_trips"].fillna(0).to_numpy()
    return out


def profiles_from_trips(rows: pd.DataFrame, roster: list[dict], cfg, first_k: dict[str, int] | None = None) -> dict[str, DriverProfile]:
    """Profiles from all known trips (dispatch "now"); ``first_k`` limits chosen drivers to their first k trips
    (those drivers are also kept out of everybody's similar-driver pool)."""

    ids = [d["driver_id"] for d in roster]
    if first_k:
        keep = []
        for driver, part in rows.groupby("driver_id"):
            if driver in first_k:
                trips = part.drop_duplicates("trip_id").sort_values("start_time")["trip_id"].head(first_k[driver])
                part = part[part["trip_id"].isin(set(trips))]
            keep.append(part)
        rows = pd.concat(keep) if keep else rows.iloc[:0]
    obs, exp, n_trips, n_seg = _state(len(ids))
    _accumulate(rows, {d: i for i, d in enumerate(ids)}, obs, exp, n_trips, n_seg)
    return _profiles(ids, obs, exp, n_trips, n_seg, cfg, excluded_from_pool=set(first_k or ()))


def match_unique(options: list[dict], sort_key: str = "predicted_fuel_L") -> list[dict]:
    """Sorted options -> at most one row per driver and per vehicle (a truck has one driver at a time).

    Greedy on ``sort_key`` (default predicted fuel): the cheapest pair first, then the cheapest pair
    among the remaining drivers and trucks, and so on. When a dispatch health gate has run,
    pass ``sort_key='dispatch_fuel_L'`` so vehicle condition influences the greedy pairing;
    excluded options (health_excluded) are placed last."""

    def key(o: dict):
        return (bool(o.get("health_excluded", False)), bool(o.get("safety_excluded", False)),
                o.get(sort_key, o["predicted_fuel_L"]))

    used_drivers, used_vehicles, out = set(), set(), []
    for option in sorted(options, key=key):
        if option["driver_id"] in used_drivers or option["vehicle_id"] in used_vehicles:
            continue
        used_drivers.add(option["driver_id"])
        used_vehicles.add(option["vehicle_id"])
        out.append(option)
    return out


def composite(multipliers) -> float:
    values = [float(v) for v in multipliers if v is not None and math.isfinite(float(v))]
    return float(np.mean(values)) if values else 1.0


__all__ = [
    "BANDS", "DRIVER_MULTS", "DriverProfile", "MOCK_NOTE", "STYLE_LABELS", "apply_profile", "assign_drivers", "attach_drivers",
    "build_roster", "composite", "daily_profile", "daily_profiles", "match_unique", "mock_plates", "profiles_from_trips",
    "segment_multipliers", "shrink_profile", "trip_totals", "vehicle_days",
]
