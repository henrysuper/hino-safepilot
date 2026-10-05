"""External route and live-traffic providers (optional; keys from environment).

* ORS (OpenRouteService) driving-hgv: 1-3 candidate routes with elevation.
* TDX live traffic: section travel speeds for freeways and provincial highways.

Neither provider is needed for training. The predictor accepts any object with
the same methods, so tests and offline demos can inject fakes.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import OrsConfig, TdxConfig
from .geo import densify_path, haversine_km, to_twd97


class RoutingError(RuntimeError):
    pass


def load_env_file(path: Path) -> None:
    """Populate os.environ from a KEY=VALUE file without overriding existing values."""

    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass
class CandidateRoute:
    route_id: str
    lonlat: np.ndarray  # (n, 2) [lon, lat]
    elevation: np.ndarray | None
    distance_km: float
    ors_minutes: float


class OrsClient:
    _last_call = 0.0
    _blocked_until = 0.0
    min_interval_s = 1.6

    def __init__(self, config: OrsConfig, api_key: str | None = None, session=None):
        self.config = config
        self.api_key = api_key or os.environ.get("ORS_API_KEY")
        self.session = session

    def _post(self, body: dict) -> dict:
        import requests

        if not self.api_key:
            raise RoutingError("ORS_API_KEY is not set (put it in .env)")
        if time.time() < OrsClient._blocked_until:
            raise RoutingError("ORS daily quota exhausted (skipping calls for a while)")
        http = self.session or requests
        for attempt in range(3):
            # Free tier: 40 directions requests per minute -> keep >= 1.6 s between calls.
            wait = OrsClient._last_call + self.min_interval_s - time.time()
            if wait > 0 and self.session is None:
                time.sleep(wait)
            OrsClient._last_call = time.time()
            response = http.post(self.config.base_url, json=body, headers={"Authorization": self.api_key}, timeout=self.config.timeout_s)
            if response.status_code == 429 and attempt < 2 and self.session is None:
                time.sleep(20)  # per-minute limit: wait it out
                continue
            if response.status_code == 403 and "Quota" in response.text:
                OrsClient._blocked_until = time.time() + 15 * 60  # daily quota: fail fast to the local router
                raise RoutingError("ORS daily quota exhausted")
            if response.status_code != 200:
                raise RoutingError(f"ORS HTTP {response.status_code}: {response.text[:200]}")
            return response.json()
        raise RoutingError("ORS rate limit")  # pragma: no cover

    def route_via(self, coordinates: list[list[float]]) -> CandidateRoute:
        """One route through every [lon, lat] in order (stops as ORS waypoints, no alternatives)."""

        payload = self._post({"coordinates": [list(map(float, c)) for c in coordinates], "elevation": True})
        feature = payload["features"][0]
        coords = np.asarray(feature["geometry"]["coordinates"], dtype=float)
        summary = feature.get("properties", {}).get("summary", {})
        return CandidateRoute("VIA", coords[:, :2], coords[:, 2] if coords.shape[1] > 2 else None,
                              float(summary.get("distance", 0.0)) / 1000, float(summary.get("duration", 0.0)) / 60)

    def routes(self, origin: list[float], destination: list[float], avoid_tollways: bool = False) -> list[CandidateRoute]:
        """origin/destination are [lon, lat]. ``avoid_tollways`` plans around tolled freeways."""

        body = {"coordinates": [list(map(float, origin)), list(map(float, destination))], "elevation": True}
        if avoid_tollways:
            body["options"] = {"avoid_features": ["tollways"]}
        straight_km = float(haversine_km(origin[1], origin[0], destination[1], destination[0]))
        if straight_km * 1.2 <= self.config.max_alternative_km and self.config.alternatives > 1:
            body["alternative_routes"] = {
                "target_count": self.config.alternatives,
                "share_factor": self.config.share_factor,
                "weight_factor": self.config.weight_factor,
            }
        try:
            payload = self._post(body)
        except RoutingError:
            if "alternative_routes" not in body:
                raise
            body.pop("alternative_routes")  # longer than ORS allows for alternatives
            payload = self._post(body)
        routes = []
        for i, feature in enumerate(payload.get("features", [])):
            coords = np.asarray(feature["geometry"]["coordinates"], dtype=float)
            summary = feature.get("properties", {}).get("summary", {})
            routes.append(CandidateRoute(
                route_id=chr(ord("A") + i),
                lonlat=coords[:, :2],
                elevation=coords[:, 2] if coords.shape[1] > 2 else None,
                distance_km=float(summary.get("distance", 0.0)) / 1000,
                ors_minutes=float(summary.get("duration", 0.0)) / 60,
            ))
        if not routes:
            raise RoutingError("ORS returned no route")
        return routes


# ------------------------------------------------------------------ TDX
_WKT_POINT = re.compile(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)")


def parse_wkt_linestring(text: str) -> np.ndarray:
    return np.asarray([(float(a), float(b)) for a, b in _WKT_POINT.findall(text or "")], dtype=float).reshape(-1, 2)


def _records(payload) -> list[dict]:
    """Find the list of section records in a TDX response (field names vary by dataset)."""

    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict) and "SectionID" in value[0]:
                return value
    return []


def _first(record: dict, *keys):
    for key in keys:
        if record.get(key) not in (None, ""):
            return record[key]
    return None


class TdxClient:
    """Live section speeds (freeway + provincial highway) matched to route points.

    Matching (TDX section <-> ORS route): section shapes are densified to 20 m
    points in TWD97 and indexed; every 100 m route profile point takes the
    nearest section point within ``max_match_distance_m``. A route segment uses
    live traffic when at least half of its points matched, with the harmonic
    mean of the matched sections' live travel speeds.
    """

    NETWORKS = ("Freeway", "Highway")

    def __init__(self, config: TdxConfig, cache_dir: Path, client_id: str | None = None, client_secret: str | None = None, session=None):
        self.config = config
        self.cache_dir = Path(cache_dir)
        self.client_id = client_id or os.environ.get("TDX_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("TDX_CLIENT_SECRET")
        self.session = session
        self._token: tuple[str, float] | None = None
        self._index = None
        self._live: tuple[dict[str, float], float] | None = None

    def _http(self):
        import requests

        return self.session or requests

    def token(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        if not self.client_id or not self.client_secret:
            raise RoutingError("TDX_CLIENT_ID / TDX_CLIENT_SECRET are not set")
        response = self._http().post(self.config.token_url, data={
            "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret,
        }, timeout=self.config.timeout_s)
        if response.status_code != 200:
            raise RoutingError(f"TDX auth HTTP {response.status_code}: {response.text[:120]}")
        body = response.json()
        self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return self._token[0]

    def _get(self, path: str, retries: int = 4):
        """GET with exponential backoff on HTTP 429 (TDX rate limit)."""

        for attempt in range(retries + 1):
            response = self._http().get(f"{self.config.base_url}{path}", headers={"Authorization": f"Bearer {self.token()}", "Accept-Encoding": "gzip"},
                                        params={"$format": "JSON"}, timeout=self.config.timeout_s)
            if response.status_code == 429 and attempt < retries:
                self._sleep(2 ** attempt)
                continue
            if response.status_code != 200:
                raise RoutingError(f"TDX HTTP {response.status_code} for {path}")
            return response.json()
        raise RoutingError(f"TDX rate limit for {path}")  # pragma: no cover

    @staticmethod
    def _sleep(seconds: float) -> None:
        time.sleep(seconds)

    def _shapes(self) -> list[dict]:
        """Section shapes change rarely; cached on disk (public data, no trip data)."""

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / "tdx_section_shapes.json"
        if path.is_file() and time.time() - path.stat().st_mtime < 30 * 86400:
            return json.loads(path.read_text(encoding="utf-8"))
        shapes, complete = [], True
        for network in self.NETWORKS:
            try:
                records = _records(self._get(f"/v2/Road/Traffic/SectionShape/{network}"))
            except RoutingError:
                complete = False  # keep the networks that worked; retry the cache next time
                continue
            for record in records:
                geometry = _first(record, "Geometry", "SectionGeometry", "LineString")
                if geometry:
                    shapes.append({"key": f"{network}:{record['SectionID']}", "wkt": geometry})
        if complete and shapes:
            path.write_text(json.dumps(shapes), encoding="utf-8")
        return shapes

    def _build_index(self):
        from scipy.spatial import cKDTree

        xs, ys, keys = [], [], []
        for shape in self._shapes():
            lonlat = parse_wkt_linestring(shape["wkt"])
            if len(lonlat) < 2:
                continue
            x, y = to_twd97(lonlat[:, 0], lonlat[:, 1])
            dx, dy = densify_path(x, y, 20.0)
            xs.append(dx)
            ys.append(dy)
            keys.extend([shape["key"]] * len(dx))
        if not xs:
            raise RoutingError("No TDX section shapes available")
        self._index = (cKDTree(np.column_stack([np.concatenate(xs), np.concatenate(ys)])), np.asarray(keys))

    def live_speeds(self, max_age_s: float = 60.0) -> dict[str, float]:
        """Live section speeds; cached in memory for ``max_age_s`` (TDX updates each minute)."""

        if self._live and time.time() - self._live[1] < max_age_s:
            return self._live[0]
        speeds, failures = {}, []
        for network in self.NETWORKS:
            try:
                records = _records(self._get(f"/v2/Road/Traffic/Live/{network}"))
            except RoutingError as error:
                failures.append(str(error))
                continue
            for record in records:
                speed = _first(record, "TravelSpeed", "Speed", "AvgSpeed")
                try:
                    value = float(speed)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    speeds[f"{network}:{record['SectionID']}"] = value
        if not speeds:
            raise RoutingError("; ".join(failures) or "TDX returned no live speeds")
        self._live = (speeds, time.time())
        return speeds

    def match(self, x: np.ndarray, y: np.ndarray, speeds: dict[str, float]) -> np.ndarray:
        """Live speed (km/h) for each point, NaN where no section is within range."""

        if self._index is None:
            self._build_index()
        tree, keys = self._index
        distance, index = tree.query(np.column_stack([x, y]), k=1, distance_upper_bound=self.config.max_match_distance_m)
        out = np.full(len(x), np.nan)
        hit = np.isfinite(distance)
        out[hit] = [speeds.get(k, np.nan) for k in keys[index[hit]]]
        return out
