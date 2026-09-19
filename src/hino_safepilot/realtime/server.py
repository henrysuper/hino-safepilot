"""A tiny stdlib-only web/API demo for the realtime 60-second risk engine.

No third-party web framework: uses http.server so it adds no dependency and
runs anywhere Python does. Serves:

  GET  /                 -> the demo web page (static HTML/JS/CSS)
  GET  /api/health       -> readiness + whether TDX credentials are present
  GET  /api/predict?...  -> one 60s risk prediction as JSON

Query params for /api/predict:
  lat (required), lon (required), city (default Taipei),
  vehicle_series (default 300), heading, rain_mm, visibility_m,
  warmup_seconds, base_speed

Live TDX road condition uses real credentials from .env. Weather is a synthetic
provider (is_mock=true) until a CWA key exists. The engine and providers are the
same ones exercised by the CLI, so the web demo makes no separate claims.

This is a local demo server. It has no authentication and should be run only on
a trusted machine/network. It performs a single prediction per request and does
not persist any telemetry.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .engine import RealtimeInputs, RealtimeRiskEngine
from .features import TelemetryPoint
from .live_context import (
    build_synthetic_weather_provider,
    build_tdx_provider,
    load_dotenv,
    nearest_road_info,
    resolve_city,
)


STATIC_DIR = Path(__file__).parent / "web"


def _warmup_points(now: datetime, *, seconds: int, cadence: int, base_speed: float) -> list[TelemetryPoint]:
    points: list[TelemetryPoint] = []
    n = max(1, seconds // cadence)
    for i in range(n, -1, -1):
        t = now - timedelta(seconds=i * cadence)
        speed = max(0.0, base_speed + 8.0 * ((i % 5) - 2))
        points.append(
            TelemetryPoint(
                timestamp=t,
                can_speed_kph=speed,
                gps_speed_kph=max(0.0, speed - 1.0),
                engine_rpm=800.0 + speed * 20.0,
                engine_load_pct=25.0 + speed * 0.4,
                engine_coolant_temp_c=85.0,
                battery_mv=27000.0,
                fuel_level_pct=60.0,
                can_status=0.0,
                vehicle_status=1.0 if speed > 3 else 2.0,
            )
        )
    return points


def _first(params: dict, key: str, default: str | None = None) -> str | None:
    values = params.get(key)
    return values[0] if values else default


def _warmup_base_speed(engine, now, lat, lon, heading, *, explicit, default_speed):
    """Pick the warm-up base speed.

    Priority: explicit override > live road speed at this location > default.
    Using the live road speed makes the base-model risk vary by location, which
    is the whole point of Selection A. Returns (speed, source_label).
    """
    from ..context.models import ContextAvailability, ContextQuery

    if explicit is not None:
        return explicit, "explicit_param"
    try:
        snap = engine.traffic_provider.traffic(
            ContextQuery(context_timestamp=now, latitude_deg=lat, longitude_deg=lon, heading_deg=heading)
        )
        if snap.quality_status in (ContextAvailability.AVAILABLE, ContextAvailability.STALE) and snap.road_speed_kph is not None:
            # Clamp to a sane driving range for the synthetic profile.
            speed = max(0.0, min(float(snap.road_speed_kph), 110.0))
            return speed, "live_road_speed"
    except Exception:
        pass
    return default_speed, "default_fallback"


class _Handler(BaseHTTPRequestHandler):
    repo_root: Path = Path.cwd()

    # Silence default noisy logging; keep it minimal and secret-free.
    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._send_json(404, {"error": "not_found", "path": path.name})
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if route == "/app.js":
            self._send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
            return
        if route == "/styles.css":
            self._send_file(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
            return
        if route == "/api/health":
            load_dotenv(self.repo_root)
            self._send_json(
                200,
                {
                    "status": "ok",
                    "tdx_credentials_present": bool(
                        os.getenv("TDX_CLIENT_ID") and os.getenv("TDX_CLIENT_SECRET")
                    ),
                    "cwa_credentials_present": bool(os.getenv("CWA_API_KEY")),
                    "weather_mode": "real" if os.getenv("CWA_API_KEY") else "synthetic_mock",
                },
            )
            return
        if route == "/api/predict":
            self._handle_predict(params)
            return
        self._send_json(404, {"error": "not_found", "route": route})

    def _handle_predict(self, params: dict) -> None:
        lat = _first(params, "lat")
        lon = _first(params, "lon")
        if lat is None or lon is None:
            self._send_json(400, {"error": "missing_params", "required": ["lat", "lon"]})
            return
        try:
            lat_f = float(lat)
            lon_f = float(lon)
            city_param = _first(params, "city", "auto") or "auto"
            city = resolve_city(lat_f, lon_f) if city_param.lower() == "auto" else city_param
            vehicle_series = float(_first(params, "vehicle_series", "300"))
            heading = _first(params, "heading")
            heading_f = float(heading) if heading is not None else None
            rain_mm = float(_first(params, "rain_mm", "0"))
            visibility_m = float(_first(params, "visibility_m", "10000"))
            warmup_seconds = int(_first(params, "warmup_seconds", "300"))
            base_speed = float(_first(params, "base_speed", "45"))
        except ValueError as error:
            self._send_json(400, {"error": "bad_param", "detail": str(error)})
            return

        now = datetime.now(timezone.utc)
        try:
            traffic_provider, traffic_notes, road_info = build_tdx_provider(
                self.repo_root, city=city, latitude=lat_f, longitude=lon_f
            )
            weather_provider = build_synthetic_weather_provider(
                now=now, rainfall_1h_mm=rain_mm, visibility_m=visibility_m
            )
            engine = RealtimeRiskEngine(
                repo_root=self.repo_root,
                traffic_provider=traffic_provider,
                weather_provider=weather_provider,
            )
            # Selection A: drive the synthetic warm-up from the live road speed at
            # this location, so different places (congested vs free-flowing) yield
            # a genuinely different base-model risk, not a fixed number.
            base_speed_override = _first(params, "base_speed")
            warmup_speed, warmup_source = _warmup_base_speed(
                engine, now, lat_f, lon_f, heading_f,
                explicit=float(base_speed_override) if base_speed_override else None,
                default_speed=base_speed,
            )
            for point in _warmup_points(now, seconds=warmup_seconds, cadence=10, base_speed=warmup_speed):
                engine.observe(point)
            prediction = engine.predict(
                RealtimeInputs(
                    anchor_timestamp=now,
                    latitude_deg=lat_f,
                    longitude_deg=lon_f,
                    heading_deg=heading_f,
                    vehicle_series_code=vehicle_series,
                )
            )
        except Exception as error:  # never leak secrets in error text
            self._send_json(500, {"error": "prediction_failed", "detail": type(error).__name__})
            return

        result = prediction.to_dict()
        result["provider_notes"] = traffic_notes
        result["resolved_city"] = city
        result["road_info"] = (
            nearest_road_info(road_info, lat_f, lon_f) if road_info else None
        )
        result["warmup"] = {
            "base_speed_kph": round(warmup_speed, 1),
            "source": warmup_source,
            "note": (
                "暖機遙測車速取自即時路段車速（模擬你以此路況行駛）"
                if warmup_source == "live_road_speed"
                else "暖機遙測車速為預設值（此處無即時路況）"
                if warmup_source == "default_fallback"
                else "暖機遙測車速為手動指定"
            ),
        }
        result.setdefault("notes", []).append(f"warmup_base_speed={round(warmup_speed,1)}kph({warmup_source})")
        self._send_json(200, result)


def serve(repo_root: Path, host: str = "127.0.0.1", port: int = 8000) -> None:
    _Handler.repo_root = repo_root.resolve()
    load_dotenv(_Handler.repo_root)
    server = ThreadingHTTPServer((host, port), _Handler)
    print(f"HINO SafePilot realtime demo serving at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
