"""Quota-free fallback router on the local OpenStreetMap road network.

Built once from ``data/osm/taiwan-latest.osm.pbf`` into a compressed graph
(git-ignored). Edge cost = travel time with nominal truck speeds per road
class; one-way tags are respected. Used when ORS is unavailable (daily quota,
network) so dispatch planning never depends on a paid or rate-limited service.
It knows no truck restrictions, so ORS stays the primary planner.
"""

from __future__ import annotations

from array import array
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

from .geo import haversine_km, osm_road_class, to_twd97
from .routing import CandidateRoute, RoutingError

# Nominal truck speeds (km/h) per fine road class; only relative values matter for the path.
CLASS_SPEED_KPH = {"motorway": 85.0, "trunk_fast": 70.0, "trunk_other": 50.0, "primary_secondary": 45.0, "city": 25.0}
CLASS_ORDER = ("motorway", "trunk_fast", "trunk_other", "primary_secondary", "city")


def build_graph(pbf_path: Path, out_path: Path) -> dict:
    import osmium

    refs, lons, lats = array("q"), array("d"), array("d")
    starts, classes, oneway = array("q"), array("b"), array("b")
    processor = (
        osmium.FileProcessor(str(pbf_path)).with_locations()
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY)).with_filter(osmium.filter.KeyFilter("highway"))
    )
    for way in processor:
        road_class = osm_road_class(way.tags)
        if road_class is None or way.tags.get("access") in ("no", "private"):
            continue
        nodes = [(n.ref, n.location.lon, n.location.lat) for n in way.nodes if n.location.valid()]
        if len(nodes) < 2:
            continue
        tag = way.tags.get("oneway", "")
        direction = 1 if tag in ("yes", "true", "1") or way.tags.get("highway") in ("motorway", "motorway_link") else (-1 if tag == "-1" else 0)
        starts.append(len(refs))
        classes.append(CLASS_ORDER.index(road_class))
        oneway.append(direction)
        for ref, lon, lat in nodes:
            refs.append(ref)
            lons.append(lon)
            lats.append(lat)
    refs_np = np.frombuffer(refs, dtype=np.int64)
    unique, index = np.unique(refs_np, return_inverse=True)
    node_lon = np.zeros(len(unique))
    node_lat = np.zeros(len(unique))
    node_lon[index] = np.frombuffer(lons, dtype=np.float64)
    node_lat[index] = np.frombuffer(lats, dtype=np.float64)
    bounds = np.append(np.frombuffer(starts, dtype=np.int64), len(refs_np))
    way_class = np.frombuffer(classes, dtype=np.int8)
    way_oneway = np.frombuffer(oneway, dtype=np.int8)
    seg_from, seg_to, seg_class, seg_dir = [], [], [], []
    for i in range(len(way_class)):
        idx = index[bounds[i]: bounds[i + 1]]
        seg_from.append(idx[:-1])
        seg_to.append(idx[1:])
        seg_class.append(np.full(len(idx) - 1, way_class[i], dtype=np.int8))
        seg_dir.append(np.full(len(idx) - 1, way_oneway[i], dtype=np.int8))
    a, b = np.concatenate(seg_from), np.concatenate(seg_to)
    cls, direction = np.concatenate(seg_class), np.concatenate(seg_dir)
    np.savez_compressed(out_path, lon=node_lon, lat=node_lat, a=a.astype(np.int32), b=b.astype(np.int32), cls=cls, dir=direction)
    return {"nodes": int(len(unique)), "segments": int(len(a))}


class LocalRouter:
    def __init__(self, graph_path: Path, avoid_toll_factor: float = 4.0):
        data = np.load(graph_path)
        self.avoid_toll_factor = avoid_toll_factor
        self.lon, self.lat = data["lon"], data["lat"]
        a, b, cls, direction = data["a"], data["b"], data["cls"], data["dir"]
        km = haversine_km(self.lat[a], self.lon[a], self.lat[b], self.lon[b])
        speed = np.array([CLASS_SPEED_KPH[c] for c in CLASS_ORDER])[cls]
        self.hours = np.maximum(km / speed, 1e-7)
        self.km = km
        self.is_motorway = cls == 0
        fwd = direction >= 0
        back = direction <= 0
        self.rows = np.concatenate([a[fwd], b[back]])
        self.cols = np.concatenate([b[fwd], a[back]])
        self.edge = np.concatenate([np.flatnonzero(fwd), np.flatnonzero(back)])
        n = len(self.lon)
        self.graphs = {}
        for avoid in (False, True):
            weight = self.hours[self.edge] * (avoid_toll_factor if avoid else 1.0) ** self.is_motorway[self.edge]
            self.graphs[avoid] = csr_matrix((weight, (self.rows, self.cols)), shape=(n, n))
        # Snap only to nodes of the largest connected component.
        _, labels = connected_components(self.graphs[False], directed=False)
        main = np.argmax(np.bincount(labels))
        self.snap_nodes = np.flatnonzero(labels == main)
        x, y = to_twd97(self.lon[self.snap_nodes], self.lat[self.snap_nodes])
        self.tree = cKDTree(np.column_stack([x, y]))

    def set_familiar_grids(self, grid_ids, cell_m: float = 500.0, discount: float = 0.5) -> int:
        """Habit-aware routing: edges whose midpoint lies in a grid the fleet drove (training
        period) cost ``discount`` x their travel time. Returns the number of familiar edges."""

        keys = set()
        for g in grid_ids:
            gx, gy = (int(v) for v in str(g).split("_"))
            keys.add(gx * 1_000_000 + gy)
        mid_lon = (self.lon[self.rows] + self.lon[self.cols]) / 2
        mid_lat = (self.lat[self.rows] + self.lat[self.cols]) / 2
        x, y = to_twd97(mid_lon, mid_lat)
        key = np.floor(x / cell_m).astype(np.int64) * 1_000_000 + np.floor(y / cell_m).astype(np.int64)
        familiar = np.isin(key, np.fromiter(keys, dtype=np.int64))
        n = len(self.lon)
        self.familiar_share = float(familiar.mean())
        for avoid in (False, True):
            weight = self.hours[self.edge] * (self.avoid_toll_factor if avoid else 1.0) ** self.is_motorway[self.edge] * np.where(familiar, discount, 1.0)
            self.graphs[("habit", avoid)] = csr_matrix((weight, (self.rows, self.cols)), shape=(n, n))
        return int(familiar.sum())

    def nearest_node(self, lon: float, lat: float, max_m: float = 2000.0) -> int:
        x, y = to_twd97([lon], [lat])
        distance, i = self.tree.query([x[0], y[0]])
        if distance > max_m:
            raise RoutingError("point is too far from the local road network")
        return int(self.snap_nodes[i])

    def route(self, origin, destination, avoid_tollways: bool = False, route_id: str = "本機路網", habit: bool = False) -> CandidateRoute:
        source, target = self.nearest_node(*origin), self.nearest_node(*destination)
        graph = self.graphs[("habit", avoid_tollways)] if habit and ("habit", avoid_tollways) in self.graphs else self.graphs[avoid_tollways]
        # Bound the search: road travel time is rarely > 3x the straight line at 20 km/h.
        straight_km = float(haversine_km(self.lat[source], self.lon[source], self.lat[target], self.lon[target]))
        limit = 3.0 * max(straight_km, 2.0) / 20.0 * (self.avoid_toll_factor if avoid_tollways else 1.0)
        dist, pred = dijkstra(graph, directed=True, indices=source, return_predecessors=True, limit=limit)
        if not np.isfinite(dist[target]):
            dist, pred = dijkstra(graph, directed=True, indices=source, return_predecessors=True)  # rare: search everything
        if not np.isfinite(dist[target]):
            raise RoutingError("no local route found")
        path = [target]
        while path[-1] != source:
            path.append(pred[path[-1]])
        path = np.asarray(path[::-1])
        km = float(haversine_km(self.lat[path[:-1]], self.lon[path[:-1]], self.lat[path[1:]], self.lon[path[1:]]).sum())
        # Travel time on the real (undiscounted) edge times along the path.
        hours = float(np.sum(haversine_km(self.lat[path[:-1]], self.lon[path[:-1]], self.lat[path[1:]], self.lon[path[1:]]) / 40.0))
        minutes = float(dist[target]) * 60 if not habit else hours * 60
        return CandidateRoute(route_id, np.column_stack([self.lon[path], self.lat[path]]), None, km, minutes)

    def routes(self, origin, destination, avoid_tollways: bool = False) -> list[CandidateRoute]:
        return [self.route(origin, destination, avoid_tollways, "本機路網-免收費" if avoid_tollways else "本機路網")]


class FallbackRoutes:
    """Route source: primary (cached ORS) first, local OSM router when it fails."""

    def __init__(self, primary, local: LocalRouter | None):
        self.primary, self.local = primary, local
        self.fallbacks = 0

    def routes(self, key, origin, destination, avoid_tollways: bool = False):
        try:
            return self.primary.routes(key, origin, destination, avoid_tollways=avoid_tollways)
        except RoutingError:
            if self.local is None:
                raise
            self.fallbacks += 1
            return self.local.routes(origin, destination, avoid_tollways)
