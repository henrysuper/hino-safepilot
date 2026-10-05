"""Geofences (圍籬) and place-name lookup for dispatch origins / destinations.

The organizer data has no geofences (``fenceId`` is 100 % empty, FAQ Q1), so
fences are derived from where trips really start and end in the *training*
period: DBSCAN (haversine, 300 m) on all trip start/end points; each cluster
becomes a circular fence (centre + 90th-percentile radius). Users can add their
own fences. Fence coordinates are real depot / customer locations: private,
stored only under ``data/processed/fuel``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .geo import haversine_km, to_twd97

EARTH_RADIUS_M = 6_371_008.8


@dataclass
class Fence:
    fence_id: str
    name: str
    lon: float
    lat: float
    radius_m: float
    source: str  # demo (assumed public place) | derived (from real trip endpoints)
    visits: int = 0
    vehicles: int = 0
    types: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def derive_fences(endpoints: pd.DataFrame, eps_m: float = 300.0, min_samples: int = 8,
                  radius_bounds: tuple[float, float] = (100.0, 600.0)) -> list[Fence]:
    """endpoints: columns lon, lat, enabled_code, vehicle_type (one row per trip start or end)."""

    from sklearn.cluster import DBSCAN

    points = np.radians(endpoints[["lat", "lon"]].to_numpy(float))
    labels = DBSCAN(eps=eps_m / EARTH_RADIUS_M, min_samples=min_samples, metric="haversine").fit_predict(points)
    frame = endpoints.assign(label=labels)
    frame = frame[frame["label"] >= 0]
    x, y = to_twd97(frame["lon"], frame["lat"])
    frame = frame.assign(x=x, y=y)
    fences = []
    for label, group in frame.groupby("label"):
        cx, cy = group["x"].mean(), group["y"].mean()
        from pyproj import Transformer

        lon, lat = Transformer.from_crs("EPSG:3826", "EPSG:4326", always_xy=True).transform(cx, cy)
        radius = float(np.clip(np.quantile(np.hypot(group["x"] - cx, group["y"] - cy), 0.9), *radius_bounds))
        fences.append(Fence("", "", float(lon), float(lat), round(radius), "derived", int(len(group)), int(group["enabled_code"].nunique()),
                            ",".join(str(int(t)) for t in sorted(group["vehicle_type"].dropna().unique()))))
    fences.sort(key=lambda f: -f.visits)
    for i, fence in enumerate(fences, start=1):
        fence.fence_id = f"F{i:02d}"
        fence.name = f"圍籬 F{i:02d}"
    return fences


def endpoints_from_trips(trips: pd.DataFrame) -> pd.DataFrame:
    starts = trips[["o_lon", "o_lat", "enabled_code", "vehicle_type"]].set_axis(["lon", "lat", "enabled_code", "vehicle_type"], axis=1)
    ends = trips[["d_lon", "d_lat", "enabled_code", "vehicle_type"]].set_axis(["lon", "lat", "enabled_code", "vehicle_type"], axis=1)
    return pd.concat([starts, ends], ignore_index=True)


def assign_fence(lon, lat, fences: list[Fence]) -> np.ndarray:
    """Fence id containing each point (nearest centre within its radius), '' otherwise."""

    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    out = np.full(len(lon), "", dtype=object)
    best = np.full(len(lon), np.inf)
    for fence in fences:
        d = haversine_km(lat, lon, fence.lat, fence.lon) * 1000
        inside = (d <= fence.radius_m) & (d < best)
        out[inside] = fence.fence_id
        best[inside] = d[inside]
    return out


def load_demo_fences(path: Path) -> tuple[list[Fence], list[dict]]:
    """Assumed demo fences at public logistics places (tracked config) and preset tasks."""

    if not Path(path).is_file():
        return [], []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    fences = [Fence(f["fence_id"], f["name"], float(f["lon"]), float(f["lat"]), float(f["radius_m"]), "demo") for f in payload["fences"]]
    return fences, payload.get("tasks", [])


class FenceStore:
    """Demo fences (assumed, public places) + data fences (derived from real trip endpoints)."""

    def __init__(self, directory: Path, demo_path: Path | None = None):
        self.directory = Path(directory)
        self.derived_path = self.directory / "fences_derived.json"
        self.demo_path = demo_path

    def _read(self, path: Path) -> list[Fence]:
        return [Fence(**f) for f in json.loads(path.read_text(encoding="utf-8"))] if path.is_file() else []

    def demo(self) -> list[Fence]:
        return load_demo_fences(self.demo_path)[0] if self.demo_path else []

    def derived(self) -> list[Fence]:
        return self._read(self.derived_path)

    def all(self) -> list[Fence]:
        return self.demo() + self.derived()

    def save_derived(self, fences: list[Fence]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.derived_path.write_text(json.dumps([f.to_json() for f in fences], ensure_ascii=False, indent=1), encoding="utf-8")

    def find(self, key: str) -> Fence | None:
        key = str(key).strip()
        for fence in self.all():
            if key in (fence.fence_id, fence.name):
                return fence
        return None


# ------------------------------------------------------------------ geocoding (ORS / Pelias)
class Geocoder:
    """ORS geocoding: place text -> candidates, and point -> nearest address label (cached)."""

    SEARCH = "https://api.openrouteservice.org/geocode/search"
    REVERSE = "https://api.openrouteservice.org/geocode/reverse"

    def __init__(self, cache_path: Path, api_key: str | None = None, session=None, timeout_s: float = 20):
        self.api_key = api_key or os.environ.get("ORS_API_KEY")
        self.cache_path = Path(cache_path)
        self.cache = json.loads(self.cache_path.read_text(encoding="utf-8")) if self.cache_path.is_file() else {}
        self.session = session
        self.timeout_s = timeout_s

    def _get(self, url: str, params: dict) -> dict:
        import requests

        from .routing import RoutingError

        if not self.api_key:
            raise RoutingError("ORS_API_KEY is not set")
        response = (self.session or requests).get(url, params={**params, "api_key": self.api_key}, timeout=self.timeout_s)
        if response.status_code != 200:
            raise RoutingError(f"ORS geocode HTTP {response.status_code}")
        return response.json()

    def _remember(self, key: str, value) -> None:
        self.cache[key] = value
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")

    def search(self, text: str, size: int = 6) -> list[dict]:
        text = str(text).strip()[:100]
        if not text:
            return []
        key = f"search:{text}"
        if key not in self.cache:
            payload = self._get(self.SEARCH, {"text": text, "boundary.country": "TW", "size": size, "lang": "zh-TW"})
            self._remember(key, [
                {"label": f["properties"].get("label"), "lon": f["geometry"]["coordinates"][0], "lat": f["geometry"]["coordinates"][1],
                 "layer": f["properties"].get("layer")}
                for f in payload.get("features", [])
            ])
        return self.cache[key]

    def reverse(self, lon: float, lat: float) -> str | None:
        key = f"reverse:{lon:.4f},{lat:.4f}"
        if key not in self.cache:
            payload = self._get(self.REVERSE, {"point.lon": lon, "point.lat": lat, "size": 1, "lang": "zh-TW"})
            features = payload.get("features", [])
            self._remember(key, features[0]["properties"].get("label") if features else None)
        return self.cache[key]


# ------------------------------------------------------------------ habitual routes between fences
def build_habitual_routes(trips: pd.DataFrame, telemetry: pd.DataFrame, fences: list[Fence], min_trips: int = 2,
                          max_spread: float = 1.3, min_km: float = 3.0) -> dict:
    """Route drivers actually use between two fences, from training-period GPS tracks.

    Only pairs whose trips have consistent length (max km / min km <= ``max_spread``)
    qualify, so multi-stop delivery rounds that merely start and end at the same
    fences are excluded. The representative track is the trip with median length.
    """

    trips = trips.assign(o_fence=assign_fence(trips["o_lon"], trips["o_lat"], fences), d_fence=assign_fence(trips["d_lon"], trips["d_lat"], fences))
    trips = trips[(trips["o_fence"] != "") & (trips["d_fence"] != "") & (trips["o_fence"] != trips["d_fence"]) & (trips["km"] >= min_km)]
    routes = {}
    for (o, d), group in trips.groupby(["o_fence", "d_fence"]):
        if len(group) < min_trips or group["km"].max() / max(group["km"].min(), 1e-6) > max_spread:
            continue
        median_trip = group.iloc[(group["km"] - group["km"].median()).abs().argsort().iloc[0]]
        track = telemetry[telemetry["trip_id"] == median_trip["trip_id"]].sort_values("timestamp")
        track = track[(track["longitude_deg"] > 100) & (track["latitude_deg"] > 20)]
        lonlat = track[["longitude_deg", "latitude_deg"]].to_numpy(float)
        if len(lonlat) < 5:
            continue
        routes[f"{o}->{d}"] = {
            "lonlat": lonlat.round(5).tolist(), "km": round(float(median_trip["km"]), 1), "trips": int(len(group)),
            "km_range": [round(float(group["km"].min()), 1), round(float(group["km"].max()), 1)],
            "median_minutes": round(float(((group["end_time"] - group["start_time"]).dt.total_seconds() / 60).median()), 0),
        }
    return routes


def habitual_candidate(routes: dict, origin_fence: str, destination_fence: str):
    """CandidateRoute 'H' for a fence pair, or None."""

    from .routing import CandidateRoute

    entry = routes.get(f"{origin_fence}->{destination_fence}")
    if not entry:
        return None
    return CandidateRoute("H", np.asarray(entry["lonlat"], dtype=float), None, float(entry["km"]), float(entry["median_minutes"]))
