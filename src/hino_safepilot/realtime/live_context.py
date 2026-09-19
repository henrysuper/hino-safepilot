"""Build live context providers from real credentials.

TDX (traffic): fetches VDLive (live vehicle-detector speeds/volumes) and the
matching static VD coordinates for a city, then constructs a TDXContextProvider
that the engine can query near the vehicle's GPS position. Credentials are read
from the environment (loaded from .env) by the existing TDXHistoricalDownloader,
which never logs or serializes them.

Weather: a synthetic provider is returned until a CWA key exists. Its snapshots
are explicitly flagged is_mock=True so no fake weather is ever counted as real.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..context.download import TDXHistoricalDownloader
from ..context.models import (
    ContextAvailability,
    DisasterContextSnapshot,
    TrafficContextSnapshot,
    WeatherContextSnapshot,
)
from ..context.providers import NullContextProvider, SyntheticContextProvider
from ..context.tdx import TDXContextProvider, parse_tdx_vd_live


TDX_BASE = "https://tdx.transportdata.tw/api/basic"
# VDLive (live) and VD (static metadata) v2 endpoints, JSON, per-city.
VDLIVE_URL = TDX_BASE + "/v2/Road/Traffic/Live/VD/City/{city}?%24format=JSON"
VD_STATIC_URL = TDX_BASE + "/v2/Road/Traffic/VD/City/{city}?%24format=JSON"
# Nationwide freeway (國道) live/static feeds, used as an always-on fallback so a
# point near a highway still gets coverage even if the local city VD is sparse.
FREEWAY_VDLIVE_URL = TDX_BASE + "/v2/Road/Traffic/Live/VD/Freeway?%24format=JSON"
FREEWAY_VD_STATIC_URL = TDX_BASE + "/v2/Road/Traffic/VD/Freeway?%24format=JSON"

# Adjacent cities to also query, so points near a boundary or in a detector-
# sparse area still match the nearest detector (which may be in a neighbor).
_CITY_NEIGHBORS: dict[str, list[str]] = {
    "Keelung": ["NewTaipei", "Taipei"],
    "Taipei": ["NewTaipei", "Keelung"],
    "NewTaipei": ["Taipei", "Keelung", "Taoyuan", "Yilan"],
    "Taoyuan": ["NewTaipei", "Hsinchu", "HsinchuCounty"],
    "Hsinchu": ["HsinchuCounty", "Taoyuan", "Miaoli"],
    "HsinchuCounty": ["Hsinchu", "Taoyuan", "Miaoli"],
    "Miaoli": ["HsinchuCounty", "Taichung", "Hsinchu"],
    "Taichung": ["Miaoli", "Changhua", "Nantou"],
    "Changhua": ["Taichung", "Nantou", "Yunlin"],
    "Nantou": ["Taichung", "Changhua", "Yunlin", "ChiayiCounty"],
    "Yunlin": ["Changhua", "ChiayiCounty", "Chiayi"],
    "Chiayi": ["ChiayiCounty", "Yunlin", "Tainan"],
    "ChiayiCounty": ["Chiayi", "Yunlin", "Tainan", "Nantou"],
    "Tainan": ["ChiayiCounty", "Kaohsiung", "Chiayi"],
    "Kaohsiung": ["Tainan", "Pingtung"],
    "Pingtung": ["Kaohsiung", "Taitung"],
    "Yilan": ["NewTaipei", "Hualien"],
    "Hualien": ["Yilan", "Taitung"],
    "Taitung": ["Hualien", "Pingtung"],
}

# Approximate lat/lon bounding boxes per TDX city (name matches TDX City path).
# Used to auto-select which city's detectors to query from a GPS point so the
# "use my location" flow does not silently query the wrong city.
_CITY_BOUNDS: list[tuple[str, float, float, float, float]] = [
    # city, lat_min, lat_max, lon_min, lon_max
    ("Keelung", 25.08, 25.20, 121.68, 121.80),
    ("Taipei", 24.95, 25.21, 121.45, 121.66),
    ("NewTaipei", 24.67, 25.30, 121.28, 122.01),
    ("Taoyuan", 24.75, 25.12, 120.97, 121.45),
    ("Hsinchu", 24.70, 24.90, 120.90, 121.06),
    ("HsinchuCounty", 24.55, 24.90, 120.90, 121.25),
    ("Miaoli", 24.30, 24.72, 120.65, 121.10),
    ("Taichung", 24.05, 24.45, 120.45, 121.05),
    ("Changhua", 23.85, 24.15, 120.30, 120.62),
    ("Nantou", 23.55, 24.20, 120.65, 121.30),
    ("Yunlin", 23.50, 23.85, 120.15, 120.70),
    ("Chiayi", 23.42, 23.55, 120.40, 120.52),
    ("ChiayiCounty", 23.20, 23.65, 120.10, 120.95),
    ("Tainan", 22.90, 23.40, 120.05, 120.55),
    ("Kaohsiung", 22.45, 23.28, 120.15, 121.05),
    ("Pingtung", 21.90, 22.85, 120.35, 120.90),
    ("Yilan", 24.30, 24.98, 121.30, 121.95),
    ("Hualien", 23.10, 24.35, 121.10, 121.65),
    ("Taitung", 22.00, 23.45, 120.75, 121.50),
]


def resolve_city(latitude: float | None, longitude: float | None, default: str = "Taipei") -> str:
    """Pick the TDX city whose bounding box contains the point (best-effort)."""
    if latitude is None or longitude is None:
        return default
    for city, lat_min, lat_max, lon_min, lon_max in _CITY_BOUNDS:
        if lat_min <= latitude <= lat_max and lon_min <= longitude <= lon_max:
            return city
    return default


def load_dotenv(repo_root: Path) -> None:
    """Minimal .env loader (no third-party dep). Only sets vars not already set."""
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


# --- Simple in-process caches to avoid TDX rate limits --------------------
# Static VD metadata rarely changes; cache it for the whole server lifetime.
# Live VD payloads change often; cache briefly so repeated clicks don't refetch.
_STATIC_CACHE: dict[str, Any] = {}
_LIVE_CACHE: dict[str, tuple[float, Any]] = {}
_LIVE_TTL_SECONDS = 45.0
_shared_downloader: TDXHistoricalDownloader | None = None


def _get_downloader(timeout_seconds: float) -> TDXHistoricalDownloader:
    """Reuse one downloader so the OAuth token is fetched as few times as possible."""
    global _shared_downloader
    if _shared_downloader is None:
        _shared_downloader = TDXHistoricalDownloader(timeout_seconds=timeout_seconds)
    return _shared_downloader


def _fetch_static(downloader: TDXHistoricalDownloader, key: str, url: str) -> Any:
    if key in _STATIC_CACHE:
        return _STATIC_CACHE[key]
    payload = downloader.download_json(url)
    _STATIC_CACHE[key] = payload
    return payload


def _fetch_live(downloader: TDXHistoricalDownloader, key: str, url: str) -> Any:
    import time as _time

    now = _time.monotonic()
    cached = _LIVE_CACHE.get(key)
    if cached is not None and (now - cached[0]) < _LIVE_TTL_SECONDS:
        return cached[1]
    payload = downloader.download_json(url)
    _LIVE_CACHE[key] = (now, payload)
    return payload


# TDX RoadClass code -> human label (Taiwan). Codes verified against live data
# (e.g. 市民大道高架道路 -> 2). Standard TDX road-class enumeration.
ROAD_CLASS_LABELS: dict[str, str] = {
    "0": "國道 (Freeway)",
    "1": "快速道路 (Expressway)",
    "2": "市區快速道路/高架 (Urban expressway)",
    "3": "省道 (Provincial highway)",
    "4": "縣道 (County road)",
    "5": "鄉道 (Township road)",
    "6": "市區道路 (Urban road)",
    "7": "村里道路 (Village road)",
    "8": "產業道路 (Industrial road)",
    "9": "其他 (Other)",
}


def road_class_label(code: str | None) -> str | None:
    if code is None:
        return None
    return ROAD_CLASS_LABELS.get(str(code), f"類別 {code}")


def _vd_static_locations(payload: Any) -> dict[str, tuple[float, float, str | None]]:
    """Map VDID -> (lat, lon, road_class) from the static VD dataset.

    RoadClass is a top-level field on each VD record (confirmed from live data),
    not inside DetectionLinks.
    """
    rows = payload.get("VDs", []) if isinstance(payload, dict) else payload
    locations: dict[str, tuple[float, float, str | None]] = {}
    for row in rows:
        vd_id = str(row.get("VDID"))
        lat = row.get("PositionLat")
        lon = row.get("PositionLon")
        road_class = row.get("RoadClass")
        road_class = str(road_class) if road_class is not None else None
        if lat is not None and lon is not None:
            locations[vd_id] = (float(lat), float(lon), road_class)
    return locations


def _vd_road_info(payload: Any) -> dict[str, dict[str, Any]]:
    """Map VDID -> {road_name, road_class, lat, lon, bearing} for enrichment."""
    rows = payload.get("VDs", []) if isinstance(payload, dict) else payload
    info: dict[str, dict[str, Any]] = {}
    for row in rows:
        vd_id = str(row.get("VDID"))
        lat = row.get("PositionLat")
        lon = row.get("PositionLon")
        if lat is None or lon is None:
            continue
        links = row.get("DetectionLinks") or []
        bearing = links[0].get("RoadDirection") if links and isinstance(links, list) else None
        info[vd_id] = {
            "road_name": row.get("RoadName"),
            "road_class": str(row.get("RoadClass")) if row.get("RoadClass") is not None else None,
            "lat": float(lat),
            "lon": float(lon),
            "bearing": bearing,
        }
    return info


def _has_nearby(observations: list, latitude: float, longitude: float, max_distance_m: float) -> bool:
    """True if any observation is within max_distance_m of the point."""
    from ..context.joining import haversine_distance_m

    for obs in observations:
        d = haversine_distance_m(latitude, longitude, obs.latitude_deg, obs.longitude_deg)
        if d <= max_distance_m:
            return True
    return False


def nearest_road_info(
    road_info: dict[str, dict[str, Any]], latitude: float, longitude: float
) -> dict[str, Any] | None:
    """Return road name/class of the nearest static VD to a point (advisory)."""
    from ..context.joining import haversine_distance_m

    best = None
    best_d = float("inf")
    for entry in road_info.values():
        d = haversine_distance_m(latitude, longitude, entry["lat"], entry["lon"])
        if d < best_d:
            best_d = d
            best = entry
    if best is None:
        return None
    return {
        "road_name": best["road_name"],
        "road_class": best["road_class"],
        "road_class_label": road_class_label(best["road_class"]),
        "bearing": best["bearing"],
        "distance_m": round(best_d, 1),
    }


def build_tdx_provider(
    repo_root: Path,
    *,
    city: str = "Taipei",
    latitude: float | None = None,
    longitude: float | None = None,
    timeout_seconds: float = 30.0,
    maximum_age_seconds: float = 1800.0,
    maximum_distance_m: float = 8000.0,
    include_neighbors: bool = True,
    include_freeway: bool = True,
) -> tuple[Any, list[str], dict[str, dict[str, Any]]]:
    """Return (provider, notes, road_info).

    Queries the resolved city plus its neighboring cities plus the nationwide
    freeway feed, merging all detectors. This avoids spurious "missing" results
    at city boundaries or in detector-sparse areas: if any source has a nearby
    live detector, the point matches. Falls back to Null provider only when no
    source yields any live observation.
    """
    import time as _time

    load_dotenv(repo_root)
    notes: list[str] = []
    empty_info: dict[str, dict[str, Any]] = {}
    if not os.getenv("TDX_CLIENT_ID") or not os.getenv("TDX_CLIENT_SECRET"):
        notes.append("tdx_credentials_absent; traffic disabled")
        return NullContextProvider(ContextAvailability.MISSING), notes, empty_info

    downloader = _get_downloader(timeout_seconds)

    # Ordered source list; we expand lazily and stop early once we have data, to
    # keep request volume low and avoid TDX rate limiting.
    ordered: list[tuple[str, str, str]] = [
        (city, VD_STATIC_URL.format(city=city), VDLIVE_URL.format(city=city))
    ]
    if include_neighbors:
        for neighbor in _CITY_NEIGHBORS.get(city, []):
            ordered.append((neighbor, VD_STATIC_URL.format(city=neighbor), VDLIVE_URL.format(city=neighbor)))
    if include_freeway:
        ordered.append(("Freeway", FREEWAY_VD_STATIC_URL, FREEWAY_VDLIVE_URL))

    all_observations: list = []
    road_info: dict[str, dict[str, Any]] = {}
    for index, (name, static_url, live_url) in enumerate(ordered):
        # small courtesy delay between distinct network sources to respect limits
        if index > 0:
            _time.sleep(0.4)
        try:
            static_payload = _fetch_static(downloader, name, static_url)
            live_payload = _fetch_live(downloader, name, live_url)
        except Exception as error:  # skip a failing source; never leaks secrets
            notes.append(f"source_skipped:{name}:{type(error).__name__}")
            continue
        vd_locations = _vd_static_locations(static_payload)
        if not vd_locations:
            continue
        road_info.update(_vd_road_info(static_payload))
        obs = parse_tdx_vd_live(live_payload, vd_locations=vd_locations, is_mock=False)
        all_observations.extend(obs)
        notes.append(f"{name}:live={len(obs)}")
        # Stop as soon as we have a live detector within range of the point.
        if latitude is not None and longitude is not None and _has_nearby(
            all_observations, latitude, longitude, maximum_distance_m
        ):
            notes.append(f"nearby_match_after:{name}")
            break

    if not all_observations:
        notes.append("no_live_observations_from_any_source; traffic disabled")
        return NullContextProvider(ContextAvailability.MISSING), notes, road_info

    notes.append(f"total_live_observations={len(all_observations)}")
    provider = TDXContextProvider(
        all_observations,
        maximum_age_seconds=maximum_age_seconds,
        maximum_distance_m=maximum_distance_m,
    )
    return provider, notes, road_info


def build_synthetic_weather_provider(
    *,
    now: datetime,
    rainfall_1h_mm: float = 0.0,
    visibility_m: float = 10000.0,
    wind_speed_mps: float = 3.0,
) -> SyntheticContextProvider:
    """A clearly-mock weather provider used until a real CWA key exists."""
    source = now
    traffic = TrafficContextSnapshot(
        context_timestamp=now,
        quality_status=ContextAvailability.MISSING,
        provider_name="synthetic",
        error_code="UNSUPPORTED",
        is_mock=True,
    )
    weather = WeatherContextSnapshot(
        context_timestamp=now,
        source_timestamp=source,
        available_timestamp=source,
        quality_status=ContextAvailability.AVAILABLE,
        provider_name="synthetic",
        source_dataset_version="synthetic-weather-v0",
        rainfall_1h_mm=rainfall_1h_mm,
        visibility_m=visibility_m,
        wind_speed_mps=wind_speed_mps,
        is_mock=True,
    )
    disaster = DisasterContextSnapshot(
        context_timestamp=now,
        quality_status=ContextAvailability.UNSUPPORTED,
        provider_name="synthetic",
        error_code="UNSUPPORTED",
        is_mock=True,
    )
    return SyntheticContextProvider(traffic, weather, disaster)
