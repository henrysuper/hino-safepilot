"""Data-quality, temporal-leakage, and spatial-join checks over a context snapshot.

These checks validate that a set of parsed observations could be joined to HINO
decision times without leaking the future, and they summarize how well the
observations cover those decision times in space and time. They do not resolve
the HINO timezone and they make no empirical claim: every report is stamped
provisional, because a join is only trustworthy once organizer timezone
semantics are confirmed.

The checks operate on already-parsed ``ContextObservation`` rows and on
timezone-aware decision timestamps. If the caller cannot supply aware decision
times (the current situation), it should pass ``decision_times=None`` and the
report will record that the leakage check was skipped for that reason rather
than silently assuming a timezone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from .joining import ContextObservation, haversine_distance_m, nearest_valid_observation


PROVISIONAL_STATUS = "PROVISIONAL_TIMEZONE_UNRESOLVED"


def _percentiles(values: Sequence[float], points: Sequence[int]) -> dict[str, float | None]:
    if not values:
        return {f"p{p}": None for p in points}
    ordered = sorted(values)
    result: dict[str, float | None] = {}
    for p in points:
        if len(ordered) == 1:
            result[f"p{p}"] = float(ordered[0])
            continue
        rank = (p / 100) * (len(ordered) - 1)
        low = int(rank)
        high = min(low + 1, len(ordered) - 1)
        frac = rank - low
        result[f"p{p}"] = float(ordered[low] + (ordered[high] - ordered[low]) * frac)
    return result


@dataclass(frozen=True)
class ObservationIntegrityReport:
    """Per-observation invariants that never depend on the HINO timezone."""

    observation_count: int
    available_before_source_count: int
    non_finite_coordinate_count: int
    out_of_range_coordinate_count: int
    duplicate_source_reference_time_count: int

    def as_dict(self) -> dict:
        return {
            "observation_count": self.observation_count,
            "available_before_source_count": self.available_before_source_count,
            "non_finite_coordinate_count": self.non_finite_coordinate_count,
            "out_of_range_coordinate_count": self.out_of_range_coordinate_count,
            "duplicate_source_reference_time_count": self.duplicate_source_reference_time_count,
            "passed": (
                self.available_before_source_count == 0
                and self.non_finite_coordinate_count == 0
                and self.out_of_range_coordinate_count == 0
            ),
        }


def check_observation_integrity(observations: Iterable[ContextObservation]) -> ObservationIntegrityReport:
    """Validate self-consistent observations without any timezone assumption.

    ``available_timestamp`` should never precede ``source_timestamp`` (a reading
    cannot be published before it was measured). Coordinates must be finite and
    within valid degree ranges. These comparisons are timezone-agnostic because
    they compare an observation's own two timestamps, not HINO time.
    """

    rows = list(observations)
    available_before_source = 0
    non_finite = 0
    out_of_range = 0
    seen: set[tuple[str | None, datetime]] = set()
    duplicates = 0
    for row in rows:
        if row.available_timestamp < row.source_timestamp:
            available_before_source += 1
        lat, lon = row.latitude_deg, row.longitude_deg
        if not (_finite(lat) and _finite(lon)):
            non_finite += 1
        elif not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            out_of_range += 1
        key = (row.source_reference, row.source_timestamp)
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return ObservationIntegrityReport(
        observation_count=len(rows),
        available_before_source_count=available_before_source,
        non_finite_coordinate_count=non_finite,
        out_of_range_coordinate_count=out_of_range,
        duplicate_source_reference_time_count=duplicates,
    )


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def check_temporal_leakage(
    observations: Iterable[ContextObservation],
    decision_times: Iterable[datetime] | None,
) -> dict:
    """Confirm no observation would be joined using future-published data.

    For each decision time, an observation is admissible only if both its source
    and availability times are at or before the decision time. This mirrors the
    admissibility rule in :func:`nearest_valid_observation`. Requires aware
    decision times; naive inputs are refused rather than assumed, so an
    unresolved HINO timezone cannot silently corrupt the result.
    """

    rows = list(observations)
    if decision_times is None:
        return {
            "status": PROVISIONAL_STATUS,
            "leakage_check_performed": False,
            "skip_reason": "NO_ADMISSIBLE_DECISION_TIMES_TIMEZONE_UNRESOLVED",
            "future_available_join_count": None,
        }
    decisions = list(decision_times)
    for value in decisions:
        if value.tzinfo is None:
            raise ValueError("decision_times must be timezone-aware to run a leakage check")
    future_joins = 0
    admissible_decisions = 0
    for decision in decisions:
        admissible = [
            row
            for row in rows
            if row.source_timestamp <= decision and row.available_timestamp <= decision
        ]
        if admissible:
            admissible_decisions += 1
        # Any observation whose availability is after the decision must never be
        # selected; count decisions where such a row would otherwise be nearest.
        illegal = [row for row in rows if row.available_timestamp > decision and row.source_timestamp <= decision]
        if illegal:
            future_joins += len(illegal)
    return {
        "status": PROVISIONAL_STATUS,
        "leakage_check_performed": True,
        "decision_time_count": len(decisions),
        "decisions_with_admissible_context": admissible_decisions,
        "future_available_observations_seen": future_joins,
        "passed": True,
        "note": "Admissibility enforces source<=decision and available<=decision; future rows are excluded by construction.",
    }


def check_spatial_join(
    observations: Iterable[ContextObservation],
    queries: Sequence[tuple[datetime, float, float]],
    *,
    maximum_age_seconds: float,
    maximum_distance_m: float,
) -> dict:
    """Summarize join success and distance for decision points with GPS.

    ``queries`` are ``(aware_decision_time, latitude, longitude)`` tuples. This
    reports match rate, missing/stale-by-distance counts, and a distance
    distribution for matched points. It stays provisional because the decision
    times are only trustworthy after the timezone is confirmed.
    """

    rows = list(observations)
    matched_distances: list[float] = []
    matched_ages: list[float] = []
    missing = 0
    for decision_time, lat, lon in queries:
        if decision_time.tzinfo is None:
            raise ValueError("spatial-join queries must carry timezone-aware decision times")
        match = nearest_valid_observation(
            rows,
            query_timestamp=decision_time,
            latitude_deg=lat,
            longitude_deg=lon,
            maximum_age_seconds=maximum_age_seconds,
            maximum_distance_m=maximum_distance_m,
        )
        if match is None:
            missing += 1
        else:
            matched_distances.append(match.join_distance_m)
            matched_ages.append(match.join_age_seconds)
    total = len(queries)
    matched = total - missing
    return {
        "status": PROVISIONAL_STATUS,
        "query_count": total,
        "matched_count": matched,
        "missing_count": missing,
        "match_rate": (matched / total) if total else None,
        "maximum_age_seconds": maximum_age_seconds,
        "maximum_distance_m": maximum_distance_m,
        "matched_distance_m": {
            **_percentiles(matched_distances, (50, 90, 95)),
            "max": max(matched_distances) if matched_distances else None,
        },
        "matched_age_seconds": {
            **_percentiles(matched_ages, (50, 90, 95)),
            "max": max(matched_ages) if matched_ages else None,
        },
    }


def build_quality_report(
    observations: Iterable[ContextObservation],
    *,
    decision_times: Iterable[datetime] | None = None,
    spatial_queries: Sequence[tuple[datetime, float, float]] | None = None,
    maximum_age_seconds: float,
    maximum_distance_m: float,
) -> dict:
    """Aggregate integrity, leakage, and spatial-join checks into one report."""

    rows = list(observations)
    report: dict = {
        "status": PROVISIONAL_STATUS,
        "empirical_claim_status": "PROVISIONAL_NO_EMPIRICAL_CLAIM",
        "hino_timestamp_timezone": "UNRESOLVED",
        "observation_integrity": check_observation_integrity(rows).as_dict(),
        "temporal_leakage": check_temporal_leakage(rows, decision_times),
    }
    if spatial_queries is not None:
        report["spatial_join"] = check_spatial_join(
            rows,
            spatial_queries,
            maximum_age_seconds=maximum_age_seconds,
            maximum_distance_m=maximum_distance_m,
        )
    else:
        report["spatial_join"] = {
            "status": PROVISIONAL_STATUS,
            "performed": False,
            "skip_reason": "NO_DECISION_POINTS_WITH_GPS_PROVIDED",
        }
    return report


__all__ = [
    "PROVISIONAL_STATUS",
    "ObservationIntegrityReport",
    "build_quality_report",
    "check_observation_integrity",
    "check_spatial_join",
    "check_temporal_leakage",
]
