"""Post-trip-only event context.

This module intentionally reads observations after an event. It must never be imported
by a future prediction feature pipeline.
"""

from __future__ import annotations

from bisect import bisect_left
from datetime import timedelta
from typing import Any

from .models import EventContext


def _value(row: dict[str, Any], name: str) -> float | None:
    value = row.get(name)
    return float(value) if value is not None and row.get(f"{name}_quality") == "valid" else None


def _speed(row: dict[str, Any]) -> float | None:
    return _value(row, "can_speed_kph") or _value(row, "gps_speed_kph")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def build_event_contexts(
    telemetry_rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    maximum: int = 20,
    selected_events: list[dict[str, Any]] | None = None,
) -> list[EventContext]:
    """Describe event surroundings for post-trip explanation, including future 30 s."""

    if not telemetry_rows or not events:
        return []
    times = [row["timestamp"] for row in telemetry_rows]
    trip_start, trip_end = times[0], times[-1]
    trip_duration = max((trip_end - trip_start).total_seconds(), 1.0)
    contexts: list[EventContext] = []
    sorted_events = sorted(events, key=lambda row: row["event_start_time"])
    rendered_events = sorted(
        selected_events if selected_events is not None else events,
        key=lambda row: row["event_start_time"],
    )
    for event in rendered_events[:maximum]:
        event_time = event["event_start_time"]
        index = bisect_left(times, event_time)
        nearest_candidates = [i for i in (index - 1, index) if 0 <= i < len(times)]
        nearest_index = min(
            nearest_candidates,
            key=lambda i: abs((times[i] - event_time).total_seconds()),
        )
        nearest = telemetry_rows[nearest_index]
        warnings: list[str] = []
        if abs((nearest["timestamp"] - event_time).total_seconds()) > 60:
            warnings.append("NO_TELEMETRY_WITHIN_60_SECONDS")

        before_start = bisect_left(times, event_time - timedelta(seconds=30))
        after_end = bisect_left(times, event_time + timedelta(seconds=30))
        before_rows = telemetry_rows[before_start:index]
        after_rows = telemetry_rows[index:after_end]
        before_speeds = [speed for row in before_rows if (speed := _speed(row)) is not None]
        after_speeds = [speed for row in after_rows if (speed := _speed(row)) is not None]
        before_loads = [
            load
            for row in before_rows
            if (load := _value(row, "engine_load_pct")) is not None
        ]
        trend = None
        if len(before_speeds) >= 2 and len(before_rows) >= 2:
            elapsed = (before_rows[-1]["timestamp"] - before_rows[0]["timestamp"]).total_seconds()
            if elapsed > 0:
                trend = (before_speeds[-1] - before_speeds[0]) / elapsed * 60
        before_mean, after_mean = _mean(before_speeds), _mean(after_speeds)
        if before_mean is None or after_mean is None:
            behavior = "unavailable"
        elif after_mean > before_mean + 2:
            behavior = "speed_increased"
        elif after_mean < before_mean - 2:
            behavior = "speed_decreased"
        else:
            behavior = "speed_stable"

        recent_count = sum(
            event_time - timedelta(seconds=30) <= prior["event_start_time"] < event_time
            for prior in sorted_events
        )
        relative_distance = None
        valid_mileage = [
            row
            for row in telemetry_rows[: nearest_index + 1]
            if _value(row, "total_mileage_km") is not None
        ]
        if valid_mileage:
            first = next(
                (
                    _value(row, "total_mileage_km")
                    for row in telemetry_rows
                    if _value(row, "total_mileage_km") is not None
                ),
                None,
            )
            last = _value(valid_mileage[-1], "total_mileage_km")
            if first is not None and last is not None and last >= first:
                relative_distance = last - first
            else:
                warnings.append("RELATIVE_DISTANCE_UNAVAILABLE_AFTER_RESET")

        speed_limit = _value(nearest, "gps_speed_limit")
        if speed_limit is not None and speed_limit <= 0:
            speed_limit = None
            warnings.append("ZERO_SPEED_LIMIT_TREATED_UNAVAILABLE")
        contexts.append(
            EventContext(
                event_candidate_reference=event["event_onset_candidate_id"],
                event_name=event.get("event_name") or "unknown",
                event_time=event_time,
                trip_minute=(event_time - trip_start).total_seconds() / 60,
                trip_progress_pct=min(
                    100, max(0, (event_time - trip_start).total_seconds() / trip_duration * 100)
                ),
                relative_distance_km=relative_distance,
                previous_30s_speed_mean_kph=before_mean,
                previous_30s_speed_max_kph=max(before_speeds) if before_speeds else None,
                previous_30s_speed_trend_kph_per_min=trend,
                previous_30s_engine_load_mean=_mean(before_loads),
                recent_event_count=recent_count,
                current_telemetry_timestamp=nearest["timestamp"],
                current_speed_kph=_speed(nearest),
                current_speed_limit_kph=speed_limit,
                current_engine_load_pct=_value(nearest, "engine_load_pct"),
                next_30s_speed_mean_kph=after_mean,
                next_30s_behavior=behavior,
                quality_warnings=warnings,
            )
        )
    return contexts
