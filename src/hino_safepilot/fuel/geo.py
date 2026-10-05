"""Local geodata: TWD97 projection, MOI 20 m DEM and OSM nearest-road index.

Everything is read from local files (data/dem, data/osm), so no private trip
coordinate is ever sent to an external service.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree

ROAD_BANDS = ("highway", "normal", "city")
BAND_CODE = {name: i for i, name in enumerate(ROAD_BANDS)}

# Finer OSM road classes kept in the index so the class -> band mapping can be
# changed without re-reading the OSM extract.
ROAD_CLASSES = ("motorway", "trunk_fast", "trunk_other", "primary_secondary", "city")
CLASS_CODE = {name: i for i, name in enumerate(ROAD_CLASSES)}


def osm_road_class(tags) -> str | None:
    """Map OSM tags of a way to a fine road class (None = not a drivable road)."""

    highway = tags.get("highway", "")
    if highway in ("motorway", "motorway_link"):
        return "motorway"
    if highway in ("trunk", "trunk_link"):
        maxspeed = str(tags.get("maxspeed", "")).split()[0] if tags.get("maxspeed") else ""
        fast = tags.get("motorroad") == "yes" or (maxspeed.isdigit() and int(maxspeed) >= 80)
        return "trunk_fast" if fast else "trunk_other"
    if highway in ("primary", "primary_link", "secondary", "secondary_link"):
        return "primary_secondary"
    if highway in ("tertiary", "tertiary_link", "residential", "unclassified", "service", "living_street", "track", "road"):
        return "city"
    return None

_TO_TWD97 = Transformer.from_crs("EPSG:4326", "EPSG:3826", always_xy=True)


def to_twd97(lon, lat) -> tuple[np.ndarray, np.ndarray]:
    """WGS84 lon/lat -> TWD97 TM2 metres (EPSG:3826). NaN in, NaN out."""

    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    x, y = _TO_TWD97.transform(lon, lat)
    return np.asarray(x, dtype=float), np.asarray(y, dtype=float)


def valid_lonlat(lon, lat) -> np.ndarray:
    lon = np.asarray(lon, dtype=float)
    lat = np.asarray(lat, dtype=float)
    return np.isfinite(lon) & np.isfinite(lat) & (np.abs(lon) > 1e-6) & (np.abs(lat) > 1e-6)


def grid_cell_ids(x: np.ndarray, y: np.ndarray, cell_m: float) -> np.ndarray:
    gx = np.floor(np.asarray(x) / cell_m).astype(np.int64)
    gy = np.floor(np.asarray(y) / cell_m).astype(np.int64)
    return np.char.add(np.char.add(gx.astype(str), "_"), gy.astype(str))


def densify_path(x: np.ndarray, y: np.ndarray, spacing_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Insert points along each straight leg so no gap exceeds ``spacing_m``."""

    if len(x) < 2:
        return np.asarray(x, float), np.asarray(y, float)
    dx, dy = np.diff(x), np.diff(y)
    steps = np.maximum(1, np.ceil(np.hypot(dx, dy) / spacing_m).astype(int))
    leg = np.repeat(np.arange(len(dx)), steps)
    frac = (np.arange(steps.sum()) - np.repeat(np.cumsum(steps) - steps, steps) + 1) / np.repeat(steps, steps)
    out_x = np.concatenate([[x[0]], x[leg] + dx[leg] * frac])
    out_y = np.concatenate([[y[0]], y[leg] + dy[leg] * frac])
    return out_x, out_y


# --------------------------------------------------------------------- DEM
class MoiDem:
    """Bilinear lookup in the MOI 20 m DEM GeoTIFF (TWD97, int16 metres).

    The world file (.tfw) gives the centre of the upper-left pixel.
    """

    def __init__(self, tif_path: Path, tfw_path: Path, nodata: float = -32767):
        import tifffile

        self.data = tifffile.memmap(str(tif_path), mode="r")
        a, _, _, e, c, f = (float(v) for v in Path(tfw_path).read_text().split())
        self.pixel_x, self.pixel_y, self.x0, self.y0 = a, e, c, f
        self.nodata = nodata

    @classmethod
    def from_arrays(cls, data: np.ndarray, x0: float, y0: float, pixel: float, nodata: float = -32767) -> "MoiDem":
        dem = cls.__new__(cls)
        dem.data, dem.pixel_x, dem.pixel_y, dem.x0, dem.y0, dem.nodata = data, pixel, -pixel, x0, y0, nodata
        return dem

    def elevation_xy(self, x, y) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        out = np.full(x.shape, np.nan)
        col = (x - self.x0) / self.pixel_x
        row = (y - self.y0) / self.pixel_y
        rows, cols = self.data.shape
        ok = np.isfinite(col) & np.isfinite(row) & (col >= 0) & (row >= 0) & (col < cols - 1) & (row < rows - 1)
        if not ok.any():
            return out
        c0 = np.floor(col[ok]).astype(int)
        r0 = np.floor(row[ok]).astype(int)
        dc, dr = col[ok] - c0, row[ok] - r0
        corners = []
        for rr, cc in ((r0, c0), (r0 + 1, c0), (r0, c0 + 1), (r0 + 1, c0 + 1)):
            values = np.asarray(self.data[rr, cc], dtype=float)
            values[values == self.nodata] = np.nan
            corners.append(values)
        out[ok] = (
            corners[0] * (1 - dr) * (1 - dc)
            + corners[1] * dr * (1 - dc)
            + corners[2] * (1 - dr) * dc
            + corners[3] * dr * dc
        )
        return out


# --------------------------------------------------------------------- OSM
def build_road_index(pbf_path: Path, out_path: Path, spacing_m: float) -> dict:
    """Extract drivable OSM ways, densify them and save points + fine road class."""

    import osmium

    lon: list[float] = []
    lat: list[float] = []
    way_start: list[int] = []
    way_band: list[int] = []
    ways = 0
    processor = (
        osmium.FileProcessor(str(pbf_path))
        .with_locations()
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        .with_filter(osmium.filter.KeyFilter("highway"))
    )
    for way in processor:
        road_class = osm_road_class(way.tags)
        if road_class is None:
            continue
        band = road_class
        coords = []
        for node in way.nodes:
            location = node.location
            if location.valid():
                coords.append((location.lon, location.lat))
        if len(coords) < 2:
            continue
        way_start.append(len(lon))
        way_band.append(CLASS_CODE[band])
        for a, b in coords:
            lon.append(a)
            lat.append(b)
        ways += 1
    x, y = to_twd97(np.asarray(lon), np.asarray(lat))
    starts = np.asarray(way_start + [len(lon)])
    xs, ys, bands = [], [], []
    for i in range(len(way_band)):
        px, py = densify_path(x[starts[i] : starts[i + 1]], y[starts[i] : starts[i + 1]], spacing_m)
        xs.append(px)
        ys.append(py)
        bands.append(np.full(len(px), way_band[i], dtype=np.int8))
    points_x = np.concatenate(xs)
    points_y = np.concatenate(ys)
    point_band = np.concatenate(bands)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_path, x=points_x, y=points_y, road_class=point_band, spacing_m=spacing_m, schema="road_class_v2")
    return {
        "ways": ways,
        "points": int(len(points_x)),
        "by_class_points": {ROAD_CLASSES[k]: int((point_band == k).sum()) for k in range(len(ROAD_CLASSES))},
    }


class RoadIndex:
    """Nearest drivable OSM road for TWD97 points (densified-vertex KD-tree).

    ``class_to_band`` maps the stored fine road class to the three road bands.
    """

    def __init__(self, x: np.ndarray, y: np.ndarray, road_class: np.ndarray, class_to_band: dict[str, str] | None = None):
        self.tree = cKDTree(np.column_stack([x, y]))
        mapping = class_to_band or {c: c for c in ROAD_BANDS}
        lookup = np.full(len(ROAD_CLASSES) if class_to_band else len(ROAD_BANDS), -1, dtype=np.int8)
        names = ROAD_CLASSES if class_to_band else ROAD_BANDS
        for i, name in enumerate(names):
            lookup[i] = BAND_CODE[mapping[name]]
        self.band = lookup[np.asarray(road_class, dtype=np.int8)]

    @classmethod
    def load(cls, path: Path, class_to_band: dict[str, str]) -> "RoadIndex":
        data = np.load(path)
        if "road_class" not in data:
            raise ValueError(f"Road index {path} uses an old schema; delete it to rebuild")
        return cls(data["x"], data["y"], data["road_class"], class_to_band)

    def nearest(self, x, y, max_distance_m: float) -> tuple[np.ndarray, np.ndarray]:
        """Return (band code or -1, distance in metres) for each point."""

        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        band = np.full(len(x), -1, dtype=np.int8)
        distance = np.full(len(x), np.inf)
        ok = np.isfinite(x) & np.isfinite(y)
        if ok.any():
            d, i = self.tree.query(np.column_stack([x[ok], y[ok]]), k=1, distance_upper_bound=max_distance_m)
            hit = np.isfinite(d)
            idx = np.flatnonzero(ok)
            band[idx[hit]] = self.band[i[hit]]
            distance[idx] = d
        return band, distance


def majority_band(codes: np.ndarray) -> str | None:
    """Majority vote over matched points (-1 ignored). Ties: higher road class wins."""

    codes = np.asarray(codes)
    codes = codes[codes >= 0]
    if codes.size == 0:
        return None
    counts = np.bincount(codes, minlength=len(ROAD_BANDS))
    return ROAD_BANDS[int(np.argmax(counts))]  # argmax picks the lowest code = highest class on ties


def resample_path(x: np.ndarray, y: np.ndarray, spacing_m: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Points every ``spacing_m`` metres along a polyline (start and end included)."""

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    keep = np.isfinite(x) & np.isfinite(y)
    x, y = x[keep], y[keep]
    if len(x) == 0:
        return x, y, 0.0
    cumulative = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
    length = float(cumulative[-1])
    if length <= 0:
        return x[:1], y[:1], 0.0
    stations = np.append(np.arange(0.0, length, spacing_m), length)
    return np.interp(stations, cumulative, x), np.interp(stations, cumulative, y), length


def smooth(values: np.ndarray, window: int) -> np.ndarray:
    """Centred moving average that ignores NaN (window in samples)."""

    values = np.asarray(values, dtype=float)
    if window <= 1 or len(values) < 3:
        return values
    import pandas as pd

    return pd.Series(values).rolling(window, center=True, min_periods=1).mean().to_numpy()


def path_profile(x, y, *, roads: "RoadIndex | None", dem: MoiDem | None, spacing_m: float,
                 max_match_distance_m: float, smooth_points: int, cell_m: float) -> dict:
    """Distance-based description of a driven or planned path.

    The same function describes historical GPS tracks (training) and ORS route
    geometry (dispatch), so road band, climb and grid features are measured
    identically in both settings.
    """

    px, py, length_m = resample_path(x, y, spacing_m)
    result = {"length_m": length_m, "x": px, "y": py}
    if len(px) == 0:
        result.update(road_codes=np.array([], dtype=np.int8), elevation=np.array([]), grids=np.array([], dtype=str))
        return result
    result["road_codes"] = roads.nearest(px, py, max_match_distance_m)[0] if roads is not None else np.full(len(px), -1, dtype=np.int8)
    elevation = dem.elevation_xy(px, py) if dem is not None else np.full(len(px), np.nan)
    result["elevation"] = smooth(elevation, smooth_points)
    result["grids"] = grid_cell_ids(px, py, cell_m)
    return result


def climb_metrics(elevation: np.ndarray, length_m: float) -> tuple[float, float]:
    """(elevation_gain_m, avg_slope %) from a smoothed, evenly spaced profile."""

    z = np.asarray(elevation, dtype=float)
    z = z[np.isfinite(z)]
    if z.size < 2 or length_m <= 0:
        return float("nan"), float("nan")
    gain = float(np.clip(np.diff(z), 0, None).sum())
    return gain, float((z[-1] - z[0]) / length_m * 100)


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(v, dtype=float)) for v in (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))



