"""Assemble the 163-feature model vector from a rolling telemetry buffer.

The frozen v0 model was trained on past-only windowed statistics over
[t-300s, t). For realtime inference we keep a rolling buffer of recent
telemetry points and recompute the same windowed aggregations at the anchor
time. Any feature we cannot yet populate (insufficient history, or event-onset
recency for events we do not track in realtime) is left as NaN; the model's
median imputer plus missing-indicator handles NaN exactly as during training,
so an incomplete window degrades quality rather than fabricating values.

Nothing here writes private rows or identifiers. It only holds a short buffer
of numeric telemetry in memory for the current session.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Deque

import numpy as np


WINDOWS = (30, 60, 120, 300)

# Event-onset families the training features reference. We do not have a live
# event-onset stream in this realtime path, so their counts/recency are left
# missing (NaN) and imputed. This is intentional and reported as degraded.
EVENT_FAMILIES = (
    "accelerate",
    "decelerate",
    "speeding",
    "engine_overloading",
    "fatigue",
    "phone",
    "distract",
    "pcs",
    "ldws",
)


@dataclass
class TelemetryPoint:
    """A single instantaneous telemetry sample fed into the rolling buffer."""

    timestamp: datetime
    can_speed_kph: float | None = None
    gps_speed_kph: float | None = None
    engine_rpm: float | None = None
    engine_load_pct: float | None = None
    engine_coolant_temp_c: float | None = None
    battery_mv: float | None = None
    fuel_level_pct: float | None = None
    can_status: float | None = None
    vehicle_status: float | None = None


_VALUE_FIELDS = (
    "can_speed",
    "gps_speed",
    "engine_rpm",
    "engine_load",
    "coolant_temp",
    "battery_mv",
    "fuel_level",
    "can_status",
    "vehicle_status",
)

_POINT_ATTR = {
    "can_speed": "can_speed_kph",
    "gps_speed": "gps_speed_kph",
    "engine_rpm": "engine_rpm",
    "engine_load": "engine_load_pct",
    "coolant_temp": "engine_coolant_temp_c",
    "battery_mv": "battery_mv",
    "fuel_level": "fuel_level_pct",
    "can_status": "can_status",
    "vehicle_status": "vehicle_status",
}


class RollingTelemetryBuffer:
    """Keeps the most recent 300 seconds of telemetry for windowed features."""

    def __init__(self, horizon_seconds: float = 300.0):
        self.horizon_seconds = horizon_seconds
        self._points: Deque[TelemetryPoint] = deque()
        self._trip_start: datetime | None = None

    def add(self, point: TelemetryPoint) -> None:
        if self._trip_start is None:
            self._trip_start = point.timestamp
        self._points.append(point)
        self._evict(point.timestamp)

    def _evict(self, now: datetime) -> None:
        boundary = now - timedelta(seconds=self.horizon_seconds)
        while self._points and self._points[0].timestamp < boundary:
            self._points.popleft()

    @property
    def point_count(self) -> int:
        return len(self._points)

    @property
    def trip_start(self) -> datetime | None:
        return self._trip_start

    def coverage_seconds(self, anchor: datetime) -> float:
        if not self._points:
            return 0.0
        return (anchor - self._points[0].timestamp).total_seconds()

    def _window_points(self, anchor: datetime, window: int) -> list[TelemetryPoint]:
        left = anchor - timedelta(seconds=window)
        return [p for p in self._points if left <= p.timestamp <= anchor]


def _values(points: list[TelemetryPoint], name: str) -> tuple[list[float], list[datetime]]:
    attr = _POINT_ATTR[name]
    values: list[float] = []
    times: list[datetime] = []
    for p in points:
        v = getattr(p, attr)
        if v is not None:
            values.append(float(v))
            times.append(p.timestamp)
    return values, times


def _trend(values: list[float], times: list[datetime]) -> float | None:
    if len(values) < 2:
        return None
    origin = times[0]
    xs = [(t - origin).total_seconds() for t in times]
    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(values)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if not denom:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / denom


def _aggs(values: list[float], times: list[datetime]) -> dict[str, float | None]:
    if not values:
        return {k: None for k in ("mean", "std", "min", "max", "median", "trend_per_second", "range", "valid_count", "out_of_range_count")}
    lo, hi = min(values), max(values)
    return {
        "mean": statistics.fmean(values),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "min": lo,
        "max": hi,
        "median": statistics.median(values),
        "trend_per_second": _trend(values, times),
        "range": hi - lo,
        "valid_count": float(len(values)),
        "out_of_range_count": 0.0,
    }


def build_feature_row(
    buffer: RollingTelemetryBuffer,
    anchor: datetime,
    feature_names: list[str],
    *,
    vehicle_series_code: float | None,
    context_overrides: dict[str, float] | None = None,
) -> tuple[np.ndarray, dict[str, float | int | None]]:
    """Compute the model feature vector (order = feature_names) from the buffer.

    Returns the ordered numpy row plus a dict of the named values for auditing.
    Missing values are NaN. ``context_overrides`` lets external context inject or
    adjust specific named features when a mapping is defined by the caller.
    """

    row: dict[str, float | int | None] = {}

    for window in WINDOWS:
        pts = buffer._window_points(anchor, window)
        count = len(pts)
        row[f"telemetry_point_count_{window}s"] = float(count)
        row[f"telemetry_density_per_min_{window}s"] = count / (window / 60)

        for name in ("can_speed", "gps_speed"):
            vals, times = _values(pts, name)
            stats = _aggs(vals, times)
            if name == "can_speed":
                aggs = ("mean", "std", "min", "max", "median", "trend_per_second", "range", "valid_count")
            else:
                aggs = ("mean", "std", "min", "max", "valid_count", "out_of_range_count")
            for agg in aggs:
                row[f"{name}_{agg}_{window}s"] = stats[agg]

        for fam in EVENT_FAMILIES:
            # No live event-onset stream: leave counts missing (imputed).
            row[f"{fam}_onset_count_{window}s"] = None
        row[f"speeding_observation_count_{window}s"] = None

    for fam in EVENT_FAMILIES:
        row[f"seconds_since_last_{fam}_onset_300s"] = None

    for window in (60, 300):
        pts = buffer._window_points(anchor, window)
        for name, aggs in {
            "engine_rpm": ("mean", "std", "max", "valid_count"),
            "engine_load": ("mean", "std", "max", "valid_count"),
            "coolant_temp": ("mean", "max", "valid_count"),
            "battery_mv": ("mean", "min", "valid_count"),
            "fuel_level": ("mean", "std", "valid_count"),
        }.items():
            vals, times = _values(pts, name)
            stats = _aggs(vals, times)
            for agg in aggs:
                row[f"{name}_{agg}_{window}s"] = stats[agg]

        can_vals, _ = _values(pts, "can_status")
        row[f"can_status_normal_fraction_{window}s"] = (
            sum(v == 0 for v in can_vals) / len(can_vals) if can_vals else None
        )
        row[f"can_status_abnormal_fraction_{window}s"] = (
            sum(v == 9 for v in can_vals) / len(can_vals) if can_vals else None
        )
        row[f"can_status_valid_count_{window}s"] = float(len(can_vals))

        status_vals, _ = _values(pts, "vehicle_status")
        for code, label in ((0, "parked"), (1, "driving"), (2, "idling")):
            row[f"vehicle_status_{label}_fraction_{window}s"] = (
                sum(v == code for v in status_vals) / len(status_vals) if status_vals else None
            )

    trip_start = buffer.trip_start
    row["elapsed_trip_seconds"] = (anchor - trip_start).total_seconds() if trip_start else 0.0
    row["history_coverage_seconds"] = buffer.coverage_seconds(anchor)
    left300 = buffer._window_points(anchor, 300)
    row["history_max_gap_seconds"] = _max_gap(left300, anchor)
    row["vehicle_series_code"] = vehicle_series_code

    if context_overrides:
        for key, value in context_overrides.items():
            if key in feature_names:
                row[key] = value

    ordered = np.array(
        [
            (np.nan if row.get(name) is None else float(row.get(name)))
            for name in feature_names
        ],
        dtype=float,
    )
    return ordered, row


def _max_gap(points: list[TelemetryPoint], anchor: datetime) -> float:
    if not points:
        return 300.0
    boundary = anchor - timedelta(seconds=300)
    times = [boundary] + [p.timestamp for p in points] + [anchor]
    return max((later - earlier).total_seconds() for earlier, later in zip(times, times[1:]))
