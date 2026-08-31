"""Chronological replay and one-to-one alert/event matching for offline policy research."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from typing import Iterable

import numpy as np

from .contracts import AlertPolicyConfig
from .engine import AlertPolicyEngine


def replay_revised_policy(trip_ids, timestamps, probabilities, config: AlertPolicyConfig) -> list[dict]:
    engine = AlertPolicyEngine(config)
    rows = sorted(zip(trip_ids, timestamps, probabilities), key=lambda row: (str(row[0]), row[1]))
    alerts = []
    for trip_id, timestamp, probability in rows:
        decision = engine.decide(str(trip_id), timestamp, float(probability))
        if decision.should_speak:
            alerts.append({
                "trip_id": str(trip_id),
                "timestamp": timestamp,
                "probability": float(probability),
                "reason": decision.reason.value,
            })
    return sorted(alerts, key=lambda row: row["timestamp"])


def replay_reference_policy(
    trip_ids,
    timestamps,
    probabilities,
    *,
    kind: str,
    threshold: float,
    cooldown_seconds: int = 0,
) -> list[dict]:
    grouped = defaultdict(list)
    for trip, timestamp, probability in zip(trip_ids, timestamps, probabilities):
        grouped[str(trip)].append((timestamp, float(probability)))
    alerts = []
    for trip, rows in grouped.items():
        last_alert = None
        episode_reference = None
        active = False
        for timestamp, probability in sorted(rows):
            if probability < threshold:
                active = False
                episode_reference = None
                continue
            speak = False
            reason = "threshold_crossing"
            if kind == "threshold_only":
                speak = True
            elif kind == "simple_cooldown":
                speak = last_alert is None or (timestamp - last_alert).total_seconds() >= cooldown_seconds
            elif kind == "original_user_policy":
                if not active:
                    speak = True
                    active = True
                    episode_reference = probability
                elif last_alert is not None and (timestamp - last_alert).total_seconds() >= cooldown_seconds:
                    speak = True
                elif episode_reference is not None and probability > episode_reference:
                    speak = True
                    reason = "risk_above_episode_reference"
            else:
                raise ValueError(f"Unknown reference policy: {kind}")
            if speak:
                alerts.append({"trip_id": trip, "timestamp": timestamp, "probability": probability, "reason": reason})
                last_alert = timestamp
    return sorted(alerts, key=lambda row: row["timestamp"])


def exposure_hours(trip_ids, timestamps, driving_fractions=None, anchor_seconds: int = 10) -> dict:
    fractions = np.ones(len(trip_ids), dtype=float) if driving_fractions is None else np.asarray(driving_fractions, dtype=float)
    fractions = np.nan_to_num(fractions, nan=0.0).clip(0.0, 1.0)
    anchor_based = float(np.sum(fractions >= 0.5) * anchor_seconds / 3600.0)
    grouped = defaultdict(list)
    for trip, timestamp, fraction in zip(trip_ids, timestamps, fractions):
        grouped[str(trip)].append((timestamp, float(fraction)))
    weighted_seconds = 0.0
    for rows in grouped.values():
        ordered = sorted(rows)
        for index, (timestamp, fraction) in enumerate(ordered):
            if index + 1 < len(ordered):
                delta = max(0.0, min(60.0, (ordered[index + 1][0] - timestamp).total_seconds()))
            else:
                delta = float(anchor_seconds)
            weighted_seconds += delta * fraction
    return {
        "eligible_anchor_based_hours": anchor_based,
        "telemetry_interval_weighted_hours": weighted_seconds / 3600.0,
        "telemetry_interval_weighted_status": "EXPERIMENTAL_ESTIMATE",
    }


def evaluate_alerts(alerts: list[dict], events: Iterable[dict], trip_ids, timestamps, exposure: dict, horizon_seconds: int = 60) -> dict:
    anchor_trips = defaultdict(list)
    for trip, timestamp in zip(trip_ids, timestamps):
        anchor_trips[str(trip)].append(timestamp)
    eligible_events = []
    for event in events:
        trip = str(event["trip_id"])
        onset = event["event_start_time"]
        if any(0 < (onset - timestamp).total_seconds() <= horizon_seconds for timestamp in anchor_trips.get(trip, [])):
            eligible_events.append({**event, "trip_id": trip})
    eligible_events.sort(key=lambda row: row["event_start_time"])
    unmatched = set(range(len(eligible_events)))
    matches = []
    duplicates = 0
    for alert_index, alert in enumerate(sorted(alerts, key=lambda row: row["timestamp"])):
        candidates = [
            index for index in unmatched
            if eligible_events[index]["trip_id"] == alert["trip_id"]
            and 0 < (eligible_events[index]["event_start_time"] - alert["timestamp"]).total_seconds() <= horizon_seconds
        ]
        if candidates:
            event_index = min(candidates, key=lambda index: eligible_events[index]["event_start_time"])
            unmatched.remove(event_index)
            matches.append((alert_index, event_index))
        else:
            # Diagnostic duplicate: an otherwise-unmatched alert also precedes an already matched event.
            if any(
                event["trip_id"] == alert["trip_id"]
                and 0 < (event["event_start_time"] - alert["timestamp"]).total_seconds() <= horizon_seconds
                for event in eligible_events
            ):
                duplicates += 1
    matched_event_indices = {event_index for _, event_index in matches}
    leads = np.asarray([
        (eligible_events[event_index]["event_start_time"] - sorted(alerts, key=lambda row: row["timestamp"])[alert_index]["timestamp"]).total_seconds()
        for alert_index, event_index in matches
    ], dtype=float)
    by_type = {}
    for event_name in sorted({str(event["event_name"]) for event in eligible_events}):
        indices = [index for index, event in enumerate(eligible_events) if str(event["event_name"]) == event_name]
        detected = sum(index in matched_event_indices for index in indices)
        by_type[event_name] = {"eligible_events": len(indices), "detected_events": detected, "coverage": detected / len(indices) if indices else None}
    false_alerts = len(alerts) - len(matches)
    hours_a = exposure["eligible_anchor_based_hours"]
    hours_b = exposure["telemetry_interval_weighted_hours"]
    rolling_max = 0
    by_trip_alerts = defaultdict(list)
    for alert in alerts:
        by_trip_alerts[alert["trip_id"]].append(alert["timestamp"])
    for rows in by_trip_alerts.values():
        window = deque()
        for timestamp in sorted(rows):
            while window and (timestamp - window[0]).total_seconds() > 600:
                window.popleft()
            window.append(timestamp)
            rolling_max = max(rolling_max, len(window))
    return {
        "voice_alert_count": len(alerts),
        "matched_alert_count": len(matches),
        "false_alert_count": false_alerts,
        "alert_precision": len(matches) / len(alerts) if alerts else None,
        "eligible_event_count": len(eligible_events),
        "detected_event_count": len(matches),
        "event_coverage": len(matches) / len(eligible_events) if eligible_events else None,
        "voice_alerts_per_hour_anchor_based": len(alerts) / hours_a if hours_a else None,
        "false_alerts_per_hour_anchor_based": false_alerts / hours_a if hours_a else None,
        "voice_alerts_per_hour_telemetry_weighted": len(alerts) / hours_b if hours_b else None,
        "false_alerts_per_hour_telemetry_weighted": false_alerts / hours_b if hours_b else None,
        "median_lead_time_seconds": float(np.median(leads)) if leads.size else None,
        "p25_lead_time_seconds": float(np.percentile(leads, 25)) if leads.size else None,
        "p75_lead_time_seconds": float(np.percentile(leads, 75)) if leads.size else None,
        "detected_at_least_10_seconds": int(np.sum(leads >= 10.0)),
        "detected_at_least_30_seconds": int(np.sum(leads >= 30.0)),
        "event_coverage_at_least_10_seconds": float(np.sum(leads >= 10.0) / len(eligible_events)) if eligible_events else None,
        "event_coverage_at_least_30_seconds": float(np.sum(leads >= 30.0) / len(eligible_events)) if eligible_events else None,
        "alerts_per_detected_event": len(alerts) / len(matches) if matches else None,
        "duplicate_alerts_for_event": duplicates,
        "duplicates_per_eligible_event": duplicates / len(eligible_events) if eligible_events else None,
        "max_alerts_in_rolling_10_minutes": rolling_max,
        "event_type_diagnostics": by_type,
        "matching_policy": "chronological_one_alert_to_one_earliest_unmatched_event_within_60s",
        "exposure": exposure,
    }
