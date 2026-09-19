"""CLI for the realtime 60-second risk demo.

Example:
    python -m hino_safepilot.realtime predict \
        --lat 25.047 --lon 121.517 --city Taipei --vehicle-series 300

It queries live TDX road condition (real credentials from .env), a synthetic
weather provider (until a CWA key exists), warms up the rolling telemetry
buffer with a short synthetic history so the windowed features are populated,
and prints the calibrated 60-second risk prediction plus the context envelope.

The telemetry warm-up here is synthetic demo input; in a real deployment the
buffer is fed by the vehicle's own live telemetry stream. Synthetic inputs are
reported honestly and never claimed as recorded competition data.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .engine import RealtimeInputs, RealtimeRiskEngine
from .features import TelemetryPoint
from .live_context import build_synthetic_weather_provider, build_tdx_provider


def _warmup_points(now: datetime, *, seconds: int, cadence: int, base_speed: float) -> list[TelemetryPoint]:
    """Build a synthetic driving history ending at `now` for feature windows."""
    points: list[TelemetryPoint] = []
    n = seconds // cadence
    for i in range(n, -1, -1):
        t = now - timedelta(seconds=i * cadence)
        # simple varying speed profile so windowed stats are non-degenerate
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


def _cmd_predict(repo_root: Path, args: argparse.Namespace) -> dict:
    now = (
        datetime.fromisoformat(args.now).astimezone(timezone.utc)
        if args.now
        else datetime.now(timezone.utc)
    )

    traffic_provider, traffic_notes, _road_info = build_tdx_provider(
        repo_root, city=args.city, timeout_seconds=args.timeout_seconds
    )
    weather_provider = build_synthetic_weather_provider(
        now=now, rainfall_1h_mm=args.rain_mm, visibility_m=args.visibility_m
    )

    engine = RealtimeRiskEngine(
        repo_root=repo_root,
        traffic_provider=traffic_provider,
        weather_provider=weather_provider,
    )

    for point in _warmup_points(
        now, seconds=args.warmup_seconds, cadence=args.cadence_seconds, base_speed=args.base_speed
    ):
        engine.observe(point)

    prediction = engine.predict(
        RealtimeInputs(
            anchor_timestamp=now,
            latitude_deg=args.lat,
            longitude_deg=args.lon,
            heading_deg=args.heading,
            vehicle_series_code=args.vehicle_series,
        )
    )
    result = prediction.to_dict()
    result["provider_notes"] = traffic_notes
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="hino-realtime")
    result.add_argument("--repo-root", type=Path, default=Path.cwd())
    commands = result.add_subparsers(dest="command", required=True)

    predict = commands.add_parser("predict", help="produce one 60s risk prediction")
    predict.add_argument("--lat", type=float, required=True, help="current latitude")
    predict.add_argument("--lon", type=float, required=True, help="current longitude")
    predict.add_argument("--heading", type=float, default=None, help="heading degrees 0-360")
    predict.add_argument("--now", type=str, default=None, help="ISO time (default: now UTC)")
    predict.add_argument("--city", type=str, default="Taipei", help="TDX city for VDLive")
    predict.add_argument("--vehicle-series", type=float, default=300.0, help="vehicle_series_code")
    predict.add_argument("--warmup-seconds", type=int, default=300)
    predict.add_argument("--cadence-seconds", type=int, default=10)
    predict.add_argument("--base-speed", type=float, default=45.0)
    predict.add_argument("--rain-mm", type=float, default=0.0, help="synthetic 1h rainfall mm")
    predict.add_argument("--visibility-m", type=float, default=10000.0)
    predict.add_argument("--timeout-seconds", type=float, default=30.0)

    serve = commands.add_parser("serve", help="run the local web/API demo server")
    serve.add_argument("--host", type=str, default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    if args.command == "serve":
        from .server import serve as run_server

        run_server(repo_root, host=args.host, port=args.port)
        return 0
    if args.command == "predict":
        payload = _cmd_predict(repo_root, args)
    else:
        payload = {"error": "unknown command"}
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
