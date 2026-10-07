"""Local inspection console: ``hino-fuel serve`` -> http://127.0.0.1:8765

Standard-library HTTP server bound to 127.0.0.1 only, with no authentication:
it is a single-user local tool and must not be exposed on a network. The
browser only ever sees vehicle aliases (V01...), never enabledCodes.
"""

from __future__ import annotations

import json
import math
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pandas as pd

from .geo import haversine_km
from .narration import Narrator, template_dispatch_report, template_trip_report
from .routing import RoutingError, load_env_file

MAX_BODY = 64 * 1024
WEB_DIR = Path(__file__).resolve().parents[3] / "apps" / "fuel_console"
# Windows can map .js/.css to text/plain in the registry; browsers then refuse to run app.js.
for _type, _ext in (("text/javascript", ".js"), ("text/css", ".css"), ("application/json", ".json")):
    mimetypes.add_type(_type, _ext)


def _clean(value):
    """JSON-safe: NaN/inf -> None, numpy -> python."""

    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    return value


def split_geometry(lonlat: np.ndarray, segment_km: list[float]) -> list[list[list[float]]]:
    """Split a route polyline into consecutive pieces of the given lengths (for map colouring)."""

    lon, lat = lonlat[:, 0], lonlat[:, 1]
    cumulative = np.concatenate([[0.0], np.cumsum(haversine_km(lat[:-1], lon[:-1], lat[1:], lon[1:]))])
    scale = cumulative[-1] / max(sum(segment_km), 1e-9)  # haversine vs projected length
    pieces, start = [], 0.0
    for km in segment_km:
        end = start + km * scale
        inside = (cumulative > start) & (cumulative < end)
        stations = np.concatenate([[start], cumulative[inside], [end]])
        pieces.append(np.column_stack([np.interp(stations, cumulative, lat), np.interp(stations, cumulative, lon)]).round(5).tolist())
        start = end
    return pieces


class ConsoleState:
    def __init__(self, paths):
        from .fuel_predictor import FuelPredictor
        from .pipeline import _load_model

        load_env_file(paths.repo_root / ".env")
        self.paths = paths
        self.config = paths.config()
        self.lock = threading.Lock()
        self.predictor = FuelPredictor.from_repo(paths.repo_root, with_live_traffic=True)
        # Dispatch health gate (vehicle condition first). Optional config; falls back to defaults.
        from .dispatch_health import HealthPolicy
        policy_path = paths.repo_root / "config" / "dispatch_health_v1.json"
        self.predictor.health_policy = HealthPolicy.load(policy_path if policy_path.is_file() else None)
        self.aliases = json.loads((paths.output_root / "vehicle_aliases.json").read_text(encoding="utf-8"))
        self.codes = {alias: code for code, alias in self.aliases.items()}
        self.booster, self.meta, self.artifacts = _load_model(paths)  # v1: scoring baseline (no driver identity)
        self.narrator = Narrator()
        profiles = paths.output_root / "artifacts" / "driver_profiles.json"
        self.drivers = ({d["driver_id"]: d for d in json.loads(profiles.read_text(encoding="utf-8"))["drivers"]}
                        if profiles.is_file() else {})
        plates = paths.output_root / "artifacts" / "mock_plates.json"
        self.plates = json.loads(plates.read_text(encoding="utf-8"))["plates"] if plates.is_file() else {}

    def _report(self, name: str):
        path = self.paths.report_dir / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    # ------------------------------------------------------------------ endpoints
    def meta_payload(self) -> dict:
        a = self.artifacts
        vehicles = [
            {"vehicle": self.aliases[code], "vehicle_type": int(a.vehicle_types[code]), "health_score": a.health.get(code),
             "efficiency_offset": a.vehicle_offsets.get(code, 1.0)}
            for code in sorted(a.vehicle_types.index, key=lambda c: self.aliases[c])
        ]
        model = self._report("checkpoint3_model.json") or {}
        drivers = [{k: d.get(k) for k in ("driver_id", "driver_name", "primary_type", "style_label", "new_driver",
                                          "trips", "days", "composite_multiplier", "neighbours", "own_weight", "multipliers", "by_road")}
                   for d in self.drivers.values()]
        return {
            "model": self.meta,
            "dispatch_model": {k: self.predictor.meta.get(k) for k in ("model_version", "features", "segment_km")},
            "vehicles": vehicles,
            "vehicle_types": sorted({v["vehicle_type"] for v in vehicles}),
            "drivers": drivers,
            "driver_types": a.driver_types.names,
            "llm_provider": self.narrator.provider,
            "validation": {k: model.get(k) for k in ("selection", "segment_length_comparison", "test", "route_split", "feature_importance",
                                                       "driver_type_multipliers", "prediction_interval_ratio", "periods")},
            "health": self._report("checkpoint2_health.json"),
            "segments_check": self._report("checkpoint1_segments.json"),
        }

    # ------------------------------------------------------------------ fences and tasks
    def local_router(self):
        """Local OSM router (loaded on first use); None if the graph has not been built."""

        if getattr(self, "_local_router", None) is None:
            from .local_router import LocalRouter

            graph = self.paths.under_repo("data/osm/road_graph_v1.npz")
            self._local_router = LocalRouter(graph) if graph.is_file() else False
            if self._local_router:
                # Habit-aware candidate: roads in grids the fleet drove in the training period cost half.
                self._local_router.set_familiar_grids(self.artifacts.behaviour_map.all_time.index, self.config.grid.cell_m, 0.5)
        return self._local_router or None

    def _store(self):
        from .fences import FenceStore

        return FenceStore(self.paths.output_root, self.paths.under_repo("config/fuel_demo_fences.json"))

    def fences(self) -> dict:
        from .fences import load_demo_fences

        store = self._store()
        _, tasks = load_demo_fences(self.paths.under_repo("config/fuel_demo_fences.json"))
        return {"fences": [f.to_json() for f in store.all()], "demo_tasks": tasks}

    def task(self, body: dict) -> dict:
        """Multi-stop fence task. body: {date, depart_time, stops:[{fence_id, dwell_min?}], vehicle_type, drivers?, live}.

        vehicle_type (series 300/500/700) is required; every virtual driver of that series is compared
        with every truck of that series; the answer keeps one driver per truck (``drivers`` may narrow the list).

        depart_time "HH:MM" (or ISO). With live=true and no depart_time the task departs now.
        """

        from .backtest import CachedOrs
        from .tasks import Stop, TaskPlanner

        store = self._store()
        raw_stops = body.get("stops") or []
        if not 2 <= len(raw_stops) <= 8:
            raise ValueError("a task needs 2-8 fence stops")
        date = str(body.get("date") or pd.Timestamp.now(tz="Asia/Taipei").date())

        def at(value):
            if not value:
                return None
            text = str(value)
            return pd.Timestamp(f"{date} {text}" if len(text) <= 5 else text).tz_localize(None)

        stops = []
        for entry in raw_stops:
            fence = store.find(entry.get("fence_id", ""))
            if fence is None:
                raise ValueError(f"unknown fence: {entry.get('fence_id')}")
            dwell = float(entry.get("dwell_min", 20) or 0)
            if not 0 <= dwell <= 480:
                raise ValueError("dwell time must be 0-480 minutes")
            stops.append(Stop(fence, dwell))
        depart = at(body.get("depart_time"))
        if depart is None and not body.get("live"):
            raise ValueError("depart_time is required (HH:MM)")
        vehicles, drivers = self._task_candidates(body)
        habitual_path = self.paths.output_root / "habitual_routes.json"
        habitual = json.loads(habitual_path.read_text(encoding="utf-8")) if habitual_path.is_file() else {}
        from .local_router import FallbackRoutes

        with self.lock:
            ors = CachedOrs(self.predictor.ors, self.paths.output_root / "task_ors_cache.json", pause_s=0.0)
            source = FallbackRoutes(ors, self.local_router())
            idle_path = self.paths.output_root / "artifacts" / "stop_idle.json"
            stop_idle = json.loads(idle_path.read_text(encoding="utf-8")) if idle_path.is_file() else None
            planner = TaskPlanner(self.predictor, source, habitual, avoid_toll_candidates=self.config.ors.avoid_toll_candidate,
                                  familiar_router=self.local_router(), stop_idle=stop_idle)
            result = planner.plan(stops, [self.codes[v] for v in vehicles], drivers, depart, bool(body.get("live")))
            if source.fallbacks:
                result["notes"].insert(0, f"ORS 暫時無法使用（多半是免費額度用完），{source.fallbacks} 段改用本機 OpenStreetMap 路網規劃（不含大型車限制）。")
        for option in result["options"]:
            option["vehicle_id"] = self.aliases[option["vehicle_id"]]
            option["plate"] = self.plates.get(option["vehicle_id"])  # simulated plate (此為模擬資料)
        result["lowest_fuel_option"] = result["options"][0]
        result["highest_fuel_option"] = result["options"][-1]
        for leg in result["legs"]:
            for candidate in leg["candidates"]:
                geometry = np.asarray(candidate.pop("lonlat"), dtype=float)
                candidate["segment_geometry"] = split_geometry(geometry, [s["km"] for s in candidate["segments"]]) if len(geometry) > 1 else []
        return result

    def dispatch(self, body: dict) -> dict:
        """單程 A→B 的 AI 派車決策（最佳車輛/排序/智慧路線/推薦理由）。

        body: {date, depart_time, origin_fence, destination_fence, vehicle_type,
               vehicles?, driver_types?, live}

        起訖點用圍籬（取圍籬中心經緯度）。vehicle_type（車系 300/500/700）必填：
        比較該車系所有車 × 指定駕駛類型，依含車況風險成本的油耗排序。
        """

        from .backtest import CachedOrs
        from .dispatch_advisor import DispatchRequest, recommend
        from .local_router import FallbackRoutes

        store = self._store()
        origin = store.find(body.get("origin_fence", ""))
        destination = store.find(body.get("destination_fence", ""))
        if origin is None or destination is None:
            raise ValueError("origin_fence 與 destination_fence 都要是有效圍籬")
        if origin.fence_id == destination.fence_id:
            raise ValueError("起點與終點不能是同一個圍籬")

        date = str(body.get("date") or pd.Timestamp.now(tz="Asia/Taipei").date())
        live = bool(body.get("live"))
        if live:
            depart_time = pd.Timestamp.now(tz="Asia/Taipei").isoformat()
        else:
            text = str(body.get("depart_time") or "").strip()
            if not text:
                raise ValueError("depart_time is required (HH:MM)")
            depart_time = f"{date} {text}" if len(text) <= 5 else text

        vehicle_codes, driver_types = self._dispatch_candidates(body)

        with self.lock:
            # 候選路線先取出來（含幾何），順便拿到 ORS 掉線的 fallback 訊息。
            ors = CachedOrs(self.predictor.ors, self.paths.output_root / "task_ors_cache.json", pause_s=0.0)
            source = FallbackRoutes(ors, self.local_router())
            route_key = f"dispatch:{origin.fence_id}->{destination.fence_id}"
            routes = source.routes(route_key, [origin.lon, origin.lat], [destination.lon, destination.lat])
            geometry_by_route = {r.route_id: np.asarray(r.lonlat, dtype=float) for r in routes}

            request = DispatchRequest(
                origin=[origin.lon, origin.lat],
                destination=[destination.lon, destination.lat],
                depart_time=depart_time,
                vehicle_ids=vehicle_codes,
                driver_types=driver_types,
                use_live_traffic=live,
                origin_name=origin.name,
                destination_name=destination.name,
            )
            # 把預先取得的候選路線餵進 predictor，避免重複呼叫 ORS。
            decision = recommend(self.predictor, request, policy=getattr(self.predictor, "health_policy", None),
                                 routes=routes)
            narration = self.narrator.dispatch_report(decision["facts"])
            if source.fallbacks:
                decision["notes"].insert(0, f"ORS 暫時無法使用（多半是免費額度用完），{source.fallbacks} 段改用本機 OpenStreetMap 路網規劃（不含大型車限制）。")

        # enabledCode -> alias (+ 模擬車牌)，瀏覽器只看別名。
        def to_alias(opt: dict) -> dict:
            new = dict(opt)
            new["vehicle_id"] = self.aliases.get(opt["vehicle_id"], opt["vehicle_id"])
            new["plate"] = self.plates.get(new["vehicle_id"])
            return new

        decision["recommended"] = to_alias(decision["recommended"])
        decision["ranking"] = [to_alias(o) for o in decision["ranking"]]

        # 智慧路線幾何：用選中路線的 polyline 切段上色（重用 split_geometry）。
        chosen = decision["route"]
        geometry = geometry_by_route.get(chosen["route_id"])
        if geometry is not None and len(geometry) > 1:
            chosen["segment_geometry"] = split_geometry(geometry, [s["km"] for s in chosen["segments"]])
        else:
            chosen["segment_geometry"] = []
        chosen["from"] = origin.name
        chosen["to"] = destination.name

        decision["text"] = narration.text
        decision["generation_mode"] = narration.generation_mode
        decision["llm_error"] = narration.error
        decision["template_text"] = template_dispatch_report(decision["facts"])
        decision.pop("facts", None)  # facts 僅供理由生成，不外送（含內部標籤）
        return decision

    def _dispatch_candidates(self, body: dict) -> tuple[list[str], list[str]]:
        """單程派車候選：某車系的所有車（enabledCode）+ 要比較的駕駛類型名稱。"""

        types = self.artifacts.vehicle_types
        try:
            vehicle_type = int(body.get("vehicle_type") or 0)
        except (TypeError, ValueError):
            raise ValueError("vehicle_type must be a number") from None
        available_types = sorted({int(t) for t in types})
        if vehicle_type not in available_types:
            raise ValueError(f"vehicle_type must be one of {available_types}")
        of_type = [code for code in types.index if int(types[code]) == vehicle_type]
        of_type.sort(key=lambda c: self.aliases.get(c, c))
        if not of_type:
            raise ValueError(f"no vehicles of series {vehicle_type}")
        # 可選：縮小到指定的車（傳 alias）。
        if body.get("vehicles"):
            wanted = set(body["vehicles"])
            picked = [c for c in of_type if self.aliases.get(c, c) in wanted]
            if not picked:
                raise ValueError("vehicles 不屬於所選車系或不存在")
            of_type = picked
        names = body.get("driver_types") or ["normal"]
        unknown = [d for d in names if d not in self.predictor.artifacts.driver_types.names]
        if unknown:
            raise ValueError(f"unknown driver types: {unknown}")
        if not 1 <= len(names) <= 5:
            raise ValueError("choose 1-5 driver types")
        return of_type, names

    def _task_candidates(self, body: dict) -> tuple[list[str], list]:
        """Vehicles of the required type (aliases) and licensed virtual drivers (or driver types if no roster)."""

        from .drivers import DriverProfile
        from .tasks import DriverChoice

        types = self.artifacts.vehicle_types
        try:
            vehicle_type = int(body.get("vehicle_type") or 0)
        except (TypeError, ValueError):
            raise ValueError("vehicle_type must be a number") from None
        available_types = sorted({int(t) for t in types})
        if vehicle_type not in available_types:
            raise ValueError(f"vehicle_type must be one of {available_types}")
        of_type = sorted(alias for code, alias in self.aliases.items() if code in types.index and int(types[code]) == vehicle_type)
        if not self.drivers:  # roster not built yet: compare driver types on every vehicle of the series
            names = body.get("driver_types") or ["normal"]
            if not 1 <= len(names) <= 5:
                raise ValueError("choose 1-5 driver types")
            return of_type, names
        # Every driver of the series x every truck of the series; the planner keeps one driver per truck.
        requested = body.get("drivers") or [d["driver_id"] for d in self.drivers.values() if d["primary_type"] == vehicle_type]
        unknown = [d for d in requested if d not in self.drivers]
        if unknown:
            raise ValueError(f"unknown drivers: {unknown}")
        wrong = [d for d in requested if self.drivers[d]["primary_type"] != vehicle_type]
        if wrong:
            raise ValueError(f"drivers not of series {vehicle_type}: {wrong}")
        if not 1 <= len(requested) <= 60:
            raise ValueError("choose 1-60 drivers")
        choices = [DriverChoice(d, DriverProfile.from_json(self.drivers[d]), self.drivers[d]["driver_name"]) for d in requested]
        return of_type, choices

    def safety_payload(self) -> dict:
        """Safety-score fleet overview (SafePilot driver-side outputs)."""

        from hino_safepilot.analytics.console_safety import safety_overview

        return safety_overview(self.paths.repo_root)

    def safety_driver(self, driver_id: str) -> dict:
        from hino_safepilot.analytics.console_safety import safety_driver

        return safety_driver(self.paths.repo_root, driver_id)

    def drivers_payload(self) -> dict:
        return {"drivers": list(self.drivers.values()), "report": self._report("drivers_eval.json")}

    def fence_test(self) -> dict:
        path = self.paths.output_root / "fence_test.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def backtest(self) -> dict:
        details_path = self.paths.output_root / "backtest_details.json"
        return {"summary": self._report("backtest_real_trips.json"),
                "details": json.loads(details_path.read_text(encoding="utf-8")) if details_path.is_file() else []}

    def scores(self) -> dict:
        path = self.paths.output_root / "trip_scores_test.parquet"
        trips = pd.read_parquet(path) if path.is_file() else pd.DataFrame()
        assignment_path = self.paths.output_root / "driver_assignment.parquet"
        assigned = (pd.read_parquet(assignment_path).set_index(["enabled_code", "day"])["driver_id"].to_dict()
                    if assignment_path.is_file() else {})
        rows = []
        for t in trips[trips["scored"]].sort_values("start_time").itertuples():
            rows.append({"trip_id": t.trip_id, "vehicle": self.aliases[t.enabled_code], "vehicle_type": int(t.vehicle_type),
                         "driver": assigned.get((t.enabled_code, pd.Timestamp(t.start_time).normalize())),
                         "date": pd.Timestamp(t.start_time).strftime("%Y-%m-%d %H:%M"), "km": round(t.km, 1),
                         "actual_fuel_L": round(t.actual_fuel_L, 2), "expected_fuel_L": round(t.expected_fuel_L, 2),
                         "fuel_ratio": round(t.fuel_ratio, 3), "fuel_score": round(t.fuel_score, 1)})
        return {"summary": self._report("fuel_scores_test.json"), "trips": rows}

    def report(self, body: dict) -> dict:
        from .artifacts import prepare_segments
        from .scoring import score_trips, trip_report_facts

        trip_id = str(body.get("trip_id", ""))
        segments = prepare_segments(pd.read_parquet(self.paths.output_root / f"segments_{int(self.meta['segment_km'])}km.parquet"))
        segments = segments[segments["trip_id"] == trip_id]
        if segments.empty:
            raise ValueError("unknown trip")
        with self.lock:
            trips, seg = score_trips(segments, self.artifacts, self.booster, self.meta["features"], self.config)
            trip = trips.iloc[0]
            facts = trip_report_facts(trip, seg, float(self.artifacts.health.get(trip["enabled_code"], np.nan)), self.config)
            facts["vehicle"] = self.aliases[trip["enabled_code"]]
            narration = self.narrator.trip_report(facts)
        segment_table = seg.sort_values("segment_index")[["segment_index", "road_band", "segment_km", "fuel_L", "expected_fuel_L",
                                                          "idle_min_per_km", "expected_idle_min_per_km", "avg_slope"]]
        return {"facts": facts, "text": narration.text, "generation_mode": narration.generation_mode, "llm_error": narration.error,
                "template_text": template_trip_report(facts), "segments": segment_table.round(3).to_dict(orient="records")}


def make_handler(state: ConsoleState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HinoFuelConsole/1.0"

        def log_message(self, fmt, *args):  # keep the console quiet (no request bodies logged)
            return

        def _send(self, status: int, payload, content_type="application/json; charset=utf-8"):
            data = payload if isinstance(payload, bytes) else json.dumps(_clean(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _static(self):
            name = "index.html" if self.path.split("?")[0] in ("/", "") else self.path.lstrip("/").split("?")[0]
            target = (WEB_DIR / name).resolve()
            if not target.is_relative_to(WEB_DIR) or not target.is_file():
                return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            self._send(HTTPStatus.OK, target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")

        def do_GET(self):
            from urllib.parse import parse_qs, urlsplit

            url = urlsplit(self.path)
            query = parse_qs(url.query)
            routes = {
                "/api/meta": state.meta_payload, "/api/backtest": state.backtest, "/api/scores": state.scores,
                "/api/fences": state.fences, "/api/fence_test": state.fence_test, "/api/drivers": state.drivers_payload,
            }
            if url.path == "/api/safety":
                return self._send(HTTPStatus.OK, state.safety_payload())
            if url.path == "/api/safety_driver":
                try:
                    return self._send(HTTPStatus.OK, state.safety_driver(query.get("id", [""])[0]))
                except ValueError as error:
                    return self._send(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            handler = routes.get(url.path)
            if handler is None:
                return self._static()
            try:
                self._send(HTTPStatus.OK, handler())
            except RoutingError as error:
                self._send(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            except Exception as error:  # surface the reason to the local user
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(error).__name__}: {error}"})

        def do_POST(self):
            handler = {"/api/task": state.task, "/api/report": state.report, "/api/dispatch": state.dispatch}.get(self.path)
            if handler is None:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY:
                return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                self._send(HTTPStatus.OK, handler(body))
            except (ValueError, KeyError, TypeError) as error:
                self._send(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except RoutingError as error:
                self._send(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            except Exception as error:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(error).__name__}: {error}"})

    return Handler


def serve(paths, port: int = 8765) -> None:
    state = ConsoleState(paths)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    print(f"Fuel console: http://127.0.0.1:{port}  (local only, no authentication; Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
