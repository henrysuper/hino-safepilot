"""Multi-stop dispatch tasks between fences, with dwell times, from a departure time.

A task is an ordered list of fence stops, a departure time, the trucks of one
series and that series' drivers. Every driver x truck pair is predicted with the
driver's personal averages per road type; the ranking then keeps each truck and
each driver at most once (one driver per truck at a time), cheapest first. Each leg
(stop i -> stop i+1) gets candidate routes (ORS 1-3, fleet-familiar local
route, habitual route between data fences, optionally ORS avoiding tolls);
the leg route is the candidate with the lowest mean predicted fuel over all
requested vehicle x driver-type options, so every option shares one schedule.
Each leg starts at the previous arrival + dwell time, so time bands (and
therefore congestion) follow the running clock.

Fuel during dwell = dwell time x same-type engine-on share at stops x idle
fuel rate (both fitted on training telemetry); zero when no model is given.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .drivers import DriverProfile, match_unique
from .fences import Fence, habitual_candidate
from .routing import CandidateRoute, RoutingError

DEFAULT_DWELL_MIN = 20.0


@dataclass
class Stop:
    fence: Fence
    dwell_min: float = DEFAULT_DWELL_MIN


@dataclass
class DriverChoice:
    """A candidate driver: a virtual driver with a personal profile, or a bare driver type name.

    ``vehicle`` = the driver's own truck (enabled_code); None pairs the driver with every candidate vehicle."""

    driver_id: str
    profile: DriverProfile | str
    name: str | None = None
    vehicle: str | None = None

    @classmethod
    def of(cls, value) -> "DriverChoice":
        return value if isinstance(value, DriverChoice) else cls(str(value), str(value))


class TaskPlanner:
    def __init__(self, predictor, route_source, habitual_routes: dict | None = None, avoid_toll_candidates: bool = True,
                 familiar_router=None, stop_idle: dict | None = None,
                 safety_by_driver: dict | None = None, safety_policy=None):
        """route_source: object with ``routes(key, origin, destination, avoid_tollways=False)`` (e.g. CachedOrs).

        familiar_router: LocalRouter with fleet-familiar grids set (adds a "車隊熟悉路線" candidate).
        stop_idle: {"rate_l_per_h": {type: L/h}, "engine_on_share": {type: share}} for idling fuel during dwell.
        safety_by_driver: driver_id -> safety summary (average/recent/trend) for the driver-safety gate;
            empty/None makes the safety layer a no-op.
        safety_policy: SafetyPolicy thresholds; None uses defaults.
        """

        self.predictor = predictor
        self.routes = route_source
        self.habitual = habitual_routes or {}
        self.avoid_toll_candidates = avoid_toll_candidates
        self.familiar_router = familiar_router
        self.stop_idle = stop_idle or {}
        self.safety_by_driver = safety_by_driver or {}
        self.safety_policy = safety_policy

    # ------------------------------------------------------------------ candidates
    def leg_candidates(self, a: Fence, b: Fence) -> tuple[list[CandidateRoute], list[str]]:
        notes = []
        key = f"leg:{a.fence_id}->{b.fence_id}"
        candidates = list(self.routes.routes(key, [a.lon, a.lat], [b.lon, b.lat]))
        for route in candidates:
            if not route.route_id.startswith("本機"):
                route.route_id = f"ORS-{route.route_id}"
        if self.avoid_toll_candidates:
            try:
                extra = self.routes.routes(key, [a.lon, a.lat], [b.lon, b.lat], avoid_tollways=True)[:1]
                for route in extra:
                    route.route_id = "本機路網-免收費" if route.route_id.startswith("本機") else "ORS-免收費"
                if extra and abs(extra[0].distance_km - candidates[0].distance_km) > 0.5:
                    candidates += extra  # identical to the default route: skip
            except RoutingError:
                notes.append(f"{a.name}→{b.name}：找不到避開收費道路的路線。")
        if self.familiar_router is not None:
            try:
                familiar = self.familiar_router.route((a.lon, a.lat), (b.lon, b.lat), route_id="車隊熟悉路線", habit=True)
                if all(abs(familiar.distance_km - c.distance_km) > 0.3 for c in candidates):
                    candidates.append(familiar)
            except RoutingError:
                pass
        habitual = habitual_candidate(self.habitual, a.fence_id, b.fence_id)
        if habitual is not None:
            habitual.route_id = "慣用路線"
            candidates.append(habitual)
        return candidates, notes

    # ------------------------------------------------------------------ evaluation
    def _evaluate_route(self, route, clock, live, pairs):
        """Describe + predict one candidate; memoised per (route, 15-minute clock bucket, live)."""

        key = (id(route), pd.Timestamp(clock).floor("15min"), live)
        cache = self.__dict__.setdefault("_memo", {})
        if key not in cache:
            cal = self.predictor.calibration
            segments, route_notes = self.predictor.describe_route(route, clock, live)
            litres = self.predictor.predict_pairs(segments, [(vehicle, driver.profile) for vehicle, driver in pairs]) * float(cal["route_fuel_factor"])
            fuel = {(vehicle, driver.driver_id): litres[i] for i, (vehicle, driver) in enumerate(pairs)}
            minutes = float(segments["predicted_minutes"].sum()) * float(cal["route_time_factor"])
            cache[key] = ({"route": route, "segments": segments, "fuel": fuel, "minutes": minutes,
                           "mean_fuel": float(np.mean([v.sum() for v in fuel.values()]))}, route_notes)
        return cache[key]

    def _evaluate_leg(self, candidates, clock, live, pairs):
        evaluated, notes = [], []
        for route in candidates:
            result, route_notes = self._evaluate_route(route, clock, live, pairs)
            evaluated.append(result)
            notes += route_notes
        best = min(range(len(evaluated)), key=lambda i: evaluated[i]["mean_fuel"])
        return evaluated, best, notes

    def _simulate(self, stops, depart, legs, live, pairs):
        clock = depart
        schedule = [{"stop": 0, "arrive": None, "depart": depart}]
        results, notes = [], []
        for i, candidates in enumerate(legs):
            evaluated, best, leg_notes = self._evaluate_leg(candidates, clock, live and i == 0, pairs)
            notes += leg_notes
            arrive = clock + pd.Timedelta(minutes=evaluated[best]["minutes"])
            dwell = stops[i + 1].dwell_min if i + 1 < len(stops) - 1 else 0.0
            schedule.append({"stop": i + 1, "arrive": arrive, "depart": arrive + pd.Timedelta(minutes=dwell)})
            results.append((evaluated, best))
            clock = arrive + pd.Timedelta(minutes=dwell)
        return schedule, results, notes

    def plan(self, stops: list[Stop], vehicles: list[str], drivers: list, depart: pd.Timestamp | None = None, live: bool = False,
             now: pd.Timestamp | None = None) -> dict:
        """drivers: DriverChoice items (virtual drivers) or driver type names, paired with every vehicle in
        ``vehicles`` (or only their own ``vehicle`` when set). Options are sorted by predicted fuel; with
        virtual drivers each truck and each driver appears at most once."""

        if len(stops) < 2:
            raise ValueError("a task needs at least an origin and a destination fence")
        if any(a.fence.fence_id == b.fence.fence_id for a, b in zip(stops, stops[1:])):
            raise ValueError("consecutive stops must be different fences")
        choices = [DriverChoice.of(d) for d in drivers]
        pairs = [(d.vehicle, d) for d in choices if d.vehicle] + [(v, d) for d in choices if not d.vehicle for v in vehicles]
        if not pairs:
            raise ValueError("no driver / vehicle to compare")
        now = now or pd.Timestamp.now(tz="Asia/Taipei").tz_localize(None)
        self._memo = {}
        notes: list[str] = []
        legs = []
        for a, b in zip(stops, stops[1:]):
            candidates, leg_notes = self.leg_candidates(a.fence, b.fence)
            legs.append(candidates)
            notes += leg_notes
        depart = now if depart is None else depart
        if live and abs((depart - now).total_seconds()) > 30 * 60:
            notes.append("出發時間不是現在，即時路況改用歷史塞車地圖。")
            live = False
        schedule, results, sim_notes = self._simulate(stops, depart, legs, live, pairs)
        notes += [n for n in sim_notes if n not in notes]
        return self._format(stops, schedule, results, pairs, depart, notes)

    # ------------------------------------------------------------------ output
    def _format(self, stops, schedule, results, pairs, depart, notes) -> dict:
        p = self.predictor
        low, high = p.interval
        stop_rows = []
        for entry, stop in zip(schedule, stops):
            arrive = entry["arrive"]
            row = {"fence_id": stop.fence.fence_id, "name": stop.fence.name, "lon": stop.fence.lon, "lat": stop.fence.lat,
                   "radius_m": stop.fence.radius_m, "arrive": arrive.isoformat() if arrive is not None else None,
                   "depart": entry["depart"].isoformat() if entry["stop"] < len(stops) - 1 else None,
                   "dwell_min": stop.dwell_min if 0 < entry["stop"] < len(stops) - 1 else 0}
            stop_rows.append(row)
        legs = []
        for i, (evaluated, best) in enumerate(results):
            legs.append({
                "from": stops[i].fence.name, "to": stops[i + 1].fence.name, "chosen_route_id": evaluated[best]["route"].route_id,
                "candidates": [
                    {"route_id": e["route"].route_id, "distance_km": round(e["route"].distance_km, 1), "minutes": round(e["minutes"]),
                     "mean_fuel_L": round(e["mean_fuel"], 2), "lonlat": e["route"].lonlat[:: max(1, len(e["route"].lonlat) // 400)].round(5).tolist(),
                     "segments": _segment_rows(e["segments"])}
                    for e in evaluated
                ],
            })
        options = []
        dwell_hours = sum(s.dwell_min for s in stops[1:-1]) / 60
        end = schedule[-1]["arrive"]
        for vehicle, driver in pairs:
            vehicle_type = str(int(p.artifacts.vehicle_types[vehicle]))
            idle_l = dwell_hours * float(self.stop_idle.get("engine_on_share", {}).get(vehicle_type, 0.0)) * float(self.stop_idle.get("rate_l_per_h", {}).get(vehicle_type, 0.0))
            per_leg = [float(evaluated[best]["fuel"][(vehicle, driver.driver_id)].sum()) for evaluated, best in results]
            total = sum(per_leg) + idle_l
            options.append({
                "vehicle_id": vehicle, "vehicle_type": int(vehicle_type), "driver_id": driver.driver_id, "driver_name": driver.name or driver.driver_id,
                "predicted_fuel_L": round(total, 2), "prediction_interval_L": [round(total * low, 2), round(total * high, 2)],
                "leg_fuel_L": [round(v, 2) for v in per_leg], "stop_idle_fuel_L": round(idle_l, 2),
            })
        options.sort(key=lambda o: o["predicted_fuel_L"])
        # Dispatch priority: fuel -> vehicle condition -> driver safety.
        # 1) optional health gate re-ranks by road-condition grade -> dispatch_fuel_L.
        # 2) optional driver-safety gate adds a driver-safety cost on top -> dispatch_score.
        # Both default to no-op (unchanged) when their data/policy is absent.
        distance_km = round(sum(r[0][r[1]]["route"].distance_km for r in results), 1)
        sort_key = "predicted_fuel_L"
        policy = getattr(p, "health_policy", None)
        if policy is not None:
            from .dispatch_health import apply_health_gate
            options = apply_health_gate(options, p.artifacts.health, distance_km, policy=policy)
            sort_key = "dispatch_fuel_L"
        if self.safety_by_driver:
            from .dispatch_safety import apply_safety_gate
            options = apply_safety_gate(options, self.safety_by_driver, distance_km,
                                        policy=self.safety_policy, base_key=sort_key)
            sort_key = "dispatch_score"
        personal = any(isinstance(d.profile, DriverProfile) for _, d in pairs)
        compared = len(options)
        if personal:
            options = match_unique(options, sort_key=sort_key)  # a truck has one driver at a time, a driver one truck
        return {
            "model_version": p.meta["model_version"], "depart_time": depart.isoformat(), "arrive_time": end.isoformat(),
            "total_minutes": round((end - depart).total_seconds() / 60), "distance_km": distance_km,
            "stops": stop_rows, "legs": legs, "options": options, "pairs_compared": compared,
            "lowest_fuel_option": options[0], "highest_fuel_option": options[-1],
            "notes": notes,
            "driver_note": ("駕駛名單、車牌與行程分配為模擬資料；駕駛各道路的行為平均由分配到的真實行程計算。" if personal
                            else "駕駛類型由真實行為資料分群；駕駛名單與分配為模擬資料。"),
        }


def _segment_rows(segments: pd.DataFrame) -> list[dict]:
    rows = []
    for s in segments.itertuples():
        rows.append({
            "km": round(float(s.segment_km), 2), "road_band": s.road_band,
            "avg_slope": None if not math.isfinite(s.avg_slope) else round(float(s.avg_slope), 2),
            "congestion_index": round(float(s.congestion_index), 3), "congestion_source": s.congestion_source,
            "time_band": s.time_band, "predicted_minutes": round(float(s.predicted_minutes), 1),
        })
    return rows
