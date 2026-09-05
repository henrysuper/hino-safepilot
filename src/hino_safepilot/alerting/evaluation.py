"""Chronological replay and one-to-one alert/event matching for offline policy research."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Iterable

import numpy as np

from .contracts import (
    AlertLevel,
    AlertPolicyConfig,
    AlertPolicyConfigV2,
    DMSArbitrationInput,
    TieredAlertDecision,
)
from .engine import AlertPolicyEngine, TieredAlertPolicyEngine


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


def replay_tiered_policy(
    enabled_codes,
    journey_codes,
    timestamps,
    probabilities,
    config: AlertPolicyConfigV2,
    *,
    is_idling=None,
    dms_inputs=None,
    direct_critical=None,
    is_mock=None,
) -> list[TieredAlertDecision]:
    """Chronologically replay v2 without using labels or event truth."""

    count = len(probabilities)
    idling_values = [False] * count if is_idling is None else list(is_idling)
    dms_values = [None] * count if dms_inputs is None else list(dms_inputs)
    critical_values = [False] * count if direct_critical is None else list(direct_critical)
    mock_values = [False] * count if is_mock is None else list(is_mock)
    rows = list(
        zip(
            enabled_codes,
            journey_codes,
            timestamps,
            probabilities,
            idling_values,
            dms_values,
            critical_values,
            mock_values,
        )
    )
    if len(rows) != count:
        raise ValueError("all replay inputs must have the same length")
    rows.sort(key=lambda row: (str(row[0]), str(row[1]), row[2]))
    engine = TieredAlertPolicyEngine(config)
    return [
        engine.decide(
            str(enabled_code),
            str(journey_code),
            timestamp,
            float(probability),
            is_idling=bool(idling),
            dms=dms_value
            if isinstance(dms_value, DMSArbitrationInput) or dms_value is None
            else DMSArbitrationInput(**dms_value),
            direct_critical=bool(critical),
            is_mock=bool(mock),
        )
        for (
            enabled_code,
            journey_code,
            timestamp,
            probability,
            idling,
            dms_value,
            critical,
            mock,
        ) in rows
    ]


def _key(enabled_code: Any, journey_code: Any) -> tuple[str, str]:
    return str(enabled_code), str(journey_code)


def _event_key(event: dict) -> tuple[str, str]:
    return _key(
        event.get("enabled_code", ""),
        event.get("journey_code", event.get("trip_id", "")),
    )


def build_target_risk_episodes(
    enabled_codes,
    journey_codes,
    timestamps,
    targets,
    *,
    events: Iterable[dict] | None = None,
    is_mock=None,
    maximum_contiguous_gap_seconds: float = 30.0,
    label_horizon_seconds: float = 60.0,
) -> list[dict]:
    """Merge consecutive positive anchor rows into label-relative target regions.

    A zero row, a composite-trip change, or a telemetry gap beyond the configured
    continuity bound ends the region. When trusted onsets are supplied, the first
    onset that explains at least one positive anchor is retained for lead-time use.
    """

    mock_values = [False] * len(targets) if is_mock is None else list(is_mock)
    rows = list(zip(enabled_codes, journey_codes, timestamps, targets, mock_values))
    if len(rows) != len(targets):
        raise ValueError("all target episode inputs must have the same length")
    grouped: dict[tuple[str, str], list[tuple[datetime, int]]] = defaultdict(list)
    for enabled_code, journey_code, timestamp, target, mock in rows:
        if not mock:
            grouped[_key(enabled_code, journey_code)].append((timestamp, int(target)))

    event_rows: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for event in events or ():
        if not event.get("is_mock", False):
            event_rows[_event_key(event)].append(event)
    for values in event_rows.values():
        values.sort(key=lambda row: row["event_start_time"])

    episodes: list[dict] = []
    for key, values in sorted(grouped.items()):
        current: list[datetime] = []

        def finish() -> None:
            if not current:
                return
            explanatory = [
                event
                for event in event_rows.get(key, [])
                if any(
                    0 < (event["event_start_time"] - anchor).total_seconds()
                    <= label_horizon_seconds
                    for anchor in current
                )
            ]
            onset = min(
                (event["event_start_time"] for event in explanatory), default=None
            )
            episodes.append(
                {
                    "enabled_code": key[0],
                    "journey_code": key[1],
                    "episode_index": len(episodes) + 1,
                    "episode_start_time": current[0],
                    "episode_end_time": current[-1],
                    "positive_anchor_count": len(current),
                    "target_event_start_time": onset,
                    "target_event_start_times": sorted(
                        {event["event_start_time"] for event in explanatory}
                    ),
                    "label_horizon_seconds": label_horizon_seconds,
                    "event_names": sorted(
                        {str(event.get("event_name", "unknown")) for event in explanatory}
                    ),
                }
            )
            current.clear()

        previous_timestamp: datetime | None = None
        previous_target = 0
        for timestamp, target in sorted(values):
            continuous = bool(
                current
                and previous_target == 1
                and previous_timestamp is not None
                and 0 <= (timestamp - previous_timestamp).total_seconds()
                <= maximum_contiguous_gap_seconds
            )
            if target:
                if current and not continuous:
                    finish()
                current.append(timestamp)
            elif current:
                finish()
            previous_timestamp = timestamp
            previous_target = target
        finish()
    return episodes


def _decision_dict(value: TieredAlertDecision | dict) -> dict:
    if isinstance(value, TieredAlertDecision):
        return value.model_dump(mode="python")
    return dict(value)


def _rolling_counts(
    decisions: list[dict], voices: list[dict], *, level: str | None = None
) -> np.ndarray:
    by_trip_voice: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for row in voices:
        if level is None or str(row["emitted_level"]) == level:
            by_trip_voice[_key(row["enabled_code"], row["journey_code"])].append(
                row["timestamp"]
            )
    result: list[int] = []
    for key, rows in _group_decisions(decisions).items():
        voice_times = sorted(by_trip_voice.get(key, []))
        window: deque[datetime] = deque()
        voice_index = 0
        for row in rows:
            timestamp = row["timestamp"]
            while voice_index < len(voice_times) and voice_times[voice_index] <= timestamp:
                window.append(voice_times[voice_index])
                voice_index += 1
            while window and (timestamp - window[0]).total_seconds() >= 3600:
                window.popleft()
            result.append(len(window))
    return np.asarray(result, dtype=float)


def _lead_summary(values: Iterable[float]) -> dict:
    array = np.asarray(list(values), dtype=float)
    return {
        "count": int(array.size),
        "p25_seconds": float(np.percentile(array, 25)) if array.size else None,
        "median_seconds": float(np.median(array)) if array.size else None,
        "p75_seconds": float(np.percentile(array, 75)) if array.size else None,
    }


def _group_decisions(decisions: list[dict]) -> dict[tuple[str, str], list[dict]]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in decisions:
        grouped[_key(row["enabled_code"], row["journey_code"])].append(row)
    for values in grouped.values():
        values.sort(key=lambda row: row["timestamp"])
    return grouped


def evaluate_tiered_policy(
    decisions: Iterable[TieredAlertDecision | dict],
    target_episodes: list[dict],
    *,
    active_driving_hours: float,
    legacy_phase3c2_metrics: dict | None = None,
) -> dict:
    """Evaluate v2 at label-relative episode level, excluding all mock rows."""

    decision_rows = [_decision_dict(value) for value in decisions]
    official = [row for row in decision_rows if not row.get("is_mock", False)]
    official.sort(
        key=lambda row: (str(row["enabled_code"]), str(row["journey_code"]), row["timestamp"])
    )
    by_trip = _group_decisions(official)
    intervention_times: dict[int, datetime] = {}
    l1_times: dict[int, datetime] = {}
    for index, episode in enumerate(target_episodes):
        rows = by_trip.get(_key(episode["enabled_code"], episode["journey_code"]), [])
        qualifying = [
            row["timestamp"]
            for row in rows
            if episode["episode_start_time"] <= row["timestamp"] <= episode["episode_end_time"]
            and bool(row["intervention_active"])
        ]
        if qualifying:
            intervention_times[index] = min(qualifying)
        l1_qualifying = [
            row["timestamp"]
            for row in rows
            if episode["episode_start_time"] <= row["timestamp"] <= episode["episode_end_time"]
            and str(row["emitted_level"]) == AlertLevel.L1.value
        ]
        if l1_qualifying:
            l1_times[index] = min(l1_qualifying)

    phase3c3_collapsed_voice_rows: dict[tuple[str, str, str], dict] = {}
    voice_rows_by_episode_level: dict[tuple[str, str, str, str], dict] = {}
    for row in official:
        if row["voice_emitted"]:
            collapsed_key = (
                str(row["enabled_code"]),
                str(row["journey_code"]),
                str(row.get("episode_id") or row["timestamp"]),
            )
            phase3c3_collapsed_voice_rows.setdefault(collapsed_key, row)
            level_key = collapsed_key + (str(row["emitted_level"]),)
            voice_rows_by_episode_level.setdefault(level_key, row)
    voices = sorted(voice_rows_by_episode_level.values(), key=lambda row: row["timestamp"])

    matched_voice = 0
    original_region_matched_voice = 0
    voice_match_times: dict[int, datetime] = {}
    voice_match_onsets: dict[int, datetime] = {}
    voice_covered_episodes: set[int] = set()
    original_region_covered: set[int] = set()

    def next_valid_onset(episode: dict, timestamp: datetime) -> datetime | None:
        onsets = episode.get("target_event_start_times")
        if onsets is None:
            singleton = episode.get("target_event_start_time")
            onsets = [] if singleton is None else [singleton]
        candidates = [
            onset
            for onset in onsets
            if 0 < (onset - timestamp).total_seconds()
            <= float(episode.get("label_horizon_seconds", 60.0))
        ]
        return min(candidates, default=None)

    for voice in voices:
        region_candidates = [
            index
            for index, episode in enumerate(target_episodes)
            if index not in original_region_covered
            and _key(episode["enabled_code"], episode["journey_code"])
            == _key(voice["enabled_code"], voice["journey_code"])
            and episode["episode_start_time"] <= voice["timestamp"] <= episode["episode_end_time"]
        ]
        if region_candidates:
            original_selected = min(
                region_candidates,
                key=lambda index: target_episodes[index]["episode_start_time"],
            )
            original_region_covered.add(original_selected)
            original_region_matched_voice += 1
        candidates = [
            index
            for index, episode in enumerate(target_episodes)
            if index not in voice_covered_episodes
            and _key(episode["enabled_code"], episode["journey_code"])
            == _key(voice["enabled_code"], voice["journey_code"])
            and episode["episode_start_time"]
            <= voice["timestamp"]
            <= episode["episode_end_time"]
            and next_valid_onset(episode, voice["timestamp"]) is not None
        ]
        if candidates:
            selected = min(candidates, key=lambda index: target_episodes[index]["episode_start_time"])
            voice_covered_episodes.add(selected)
            matched_voice += 1
            voice_match_times[selected] = voice["timestamp"]
            voice_match_onsets[selected] = next_valid_onset(
                target_episodes[selected], voice["timestamp"]
            )

    episode_count = len(target_episodes)
    onsets_complete = bool(episode_count) and all(
        episode.get("target_event_start_time") is not None for episode in target_episodes
    )
    lead_values = np.asarray(
        [
            (
                target_episodes[index]["target_event_start_time"]
                - intervention_times[index]
            ).total_seconds()
            for index in intervention_times
            if target_episodes[index].get("target_event_start_time") is not None
        ],
        dtype=float,
    )
    timely_count = sum(value >= 3.0 for value in lead_values)
    def lead_values_for(first_times: dict[int, datetime]) -> list[float]:
        return [
            (target_episodes[index]["target_event_start_time"] - timestamp).total_seconds()
            for index, timestamp in first_times.items()
            if target_episodes[index].get("target_event_start_time") is not None
        ]

    intervention_lead = _lead_summary(lead_values)
    l1_lead = _lead_summary(lead_values_for(l1_times))
    voice_lead = _lead_summary(
        (voice_match_onsets[index] - timestamp).total_seconds()
        for index, timestamp in voice_match_times.items()
    )
    hours = float(active_driving_hours)
    advisory = [row for row in voices if str(row["emitted_level"]) == AlertLevel.L2.value]
    critical = [row for row in voices if str(row["emitted_level"]) == AlertLevel.L3.value]
    rolling_all = _rolling_counts(official, voices)
    rolling_l2 = _rolling_counts(official, voices, level=AlertLevel.L2.value)
    transitions = sum(bool(row["level_transition"]) for row in official)

    suppression = {
        "cooldown": 0,
        "voice_budget": 0,
        "idling": 0,
        "duplicate_episode": 0,
        "dms_arbitration": 0,
        "other": 0,
    }
    for row in official:
        reason = str(row["suppression_reason"])
        if reason == "suppressed_by_dms":
            suppression["dms_arbitration"] += 1
        elif reason in suppression:
            suppression[reason] += 1
        elif reason != "none":
            suppression["other"] += 1

    level_distribution = {
        level.value: sum(str(row["emitted_level"]) == level.value for row in official)
        for level in AlertLevel
    }
    false_voice = len(voices) - matched_voice
    l2_by_policy_episode: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in advisory:
        l2_by_policy_episode[
            _key(row["enabled_code"], row["journey_code"])
            + (str(row.get("episode_id")),)
        ] += 1

    result = {
        "evaluation_status": "PROVISIONAL_OFFLINE_POLICY_STUDY",
        "mock_rows_excluded": sum(row.get("is_mock", False) for row in decision_rows),
        "target_risk_episode_count": episode_count,
        "intervened_target_episode_count": len(intervention_times),
        "any_intervention_recall": len(intervention_times) / episode_count if episode_count else None,
        "intervention_recall": len(intervention_times) / episode_count if episode_count else None,
        "voice_covered_target_episode_count": len(voice_covered_episodes),
        "voice_coverage": len(voice_covered_episodes) / episode_count if episode_count else None,
        "timely_coverage_3s": timely_count / episode_count if onsets_complete else None,
        "timely_coverage_3s_status": "AVAILABLE" if onsets_complete else "UNAVAILABLE_INCOMPLETE_EVENT_ONSETS",
        "voice_alert_episode_count": len(voices),
        "phase3c3_original_policy_episode_collapsed_voice_count_backward_comparison": len(phase3c3_collapsed_voice_rows),
        "voice_episode_count_definition": "one emitted voice transition per (composite trip, policy episode, L2/L3 level); a genuine L3 escalation after L2 is counted separately",
        "advisory_voice_alert_episode_count": len(advisory),
        "critical_voice_alert_episode_count": len(critical),
        "voice_alerts_per_driving_hour": len(voices) / hours if hours > 0 else None,
        "advisory_voice_alerts_per_driving_hour": len(advisory) / hours if hours > 0 else None,
        "mean_advisory_voice_per_driving_hour": len(advisory) / hours if hours > 0 else None,
        "critical_voice_alerts_per_driving_hour": len(critical) / hours if hours > 0 else None,
        "offline_false_voice_count": false_voice,
        "offline_false_voice_per_driving_hour": false_voice / hours if hours > 0 else None,
        "label_unmatched_voice_per_driving_hour": false_voice / hours if hours > 0 else None,
        "voice_precision": matched_voice / len(voices) if voices else None,
        "phase3c3_original_region_match_voice_count_backward_comparison": original_region_matched_voice,
        "phase3c3_original_region_match_voice_precision_backward_comparison": original_region_matched_voice / len(voices) if voices else None,
        "voice_match_definition": "voice within the continuous positive target region and before an explaining target onset by no more than 60 seconds",
        "median_lead_time_seconds": float(np.median(lead_values)) if lead_values.size else None,
        "p25_lead_time_seconds": float(np.percentile(lead_values, 25)) if lead_values.size else None,
        "p75_lead_time_seconds": float(np.percentile(lead_values, 75)) if lead_values.size else None,
        "lead_time_metric_definition": "target event onset minus first any-level intervention within the continuous positive target region",
        "lead_time_distributions": {
            "first_any_intervention": intervention_lead,
            "first_l1_intervention": l1_lead,
            "first_emitted_voice": voice_lead,
            "first_l2_or_l3_voice": voice_lead,
        },
        "mean_voice_per_driving_hour": len(voices) / hours if hours > 0 else None,
        "p95_rolling_voice_count_60min": float(np.percentile(rolling_all, 95)) if rolling_all.size else 0.0,
        "max_rolling_voice_count_60min": int(np.max(rolling_all)) if rolling_all.size else 0,
        "p95_rolling_advisory_voice_count_60min": float(np.percentile(rolling_l2, 95)) if rolling_l2.size else 0.0,
        "max_rolling_advisory_voice_count_60min": int(np.max(rolling_l2)) if rolling_l2.size else 0,
        "rolling_voice_metric_audit": {
            "active_driving_hour_denominator": hours,
            "window_seconds": 3600,
            "window_stride": "one window ending at every non-mock policy decision timestamp",
            "partial_journeys": "included as observed; no extrapolation to a full hour",
            "zero_alert_windows_included": True,
            "cross_journey_state": False,
            "trip_boundary": "(enabled_code, journey_code)",
            "advisory_window_anchor_count": int(rolling_l2.size),
            "advisory_zero_count_anchor_fraction": float(np.mean(rolling_l2 == 0)) if rolling_l2.size else None,
        },
        "level_transition_count": transitions,
        "level_transitions_per_driving_hour": transitions / hours if hours > 0 else None,
        "level_distribution": level_distribution,
        "suppression_reason_counts": suppression,
        "same_episode_l2_voice_max_observed": max(l2_by_policy_episode.values(), default=0),
        "idling_statistics": {
            "decision_count": sum(bool(row["is_idling"]) for row in official),
            "voice_count": sum(bool(row["is_idling"]) and bool(row["voice_emitted"]) for row in official),
            "idling_suppression_count": suppression["idling"],
        },
        "active_driving_hours": hours,
        "offline_false_voice_definition": "voice episode unmatched to a continuous positive Label V0 target region; not a real-world accident false-alarm rate",
        "target_episode_definition": "continuous positive label_any_core_risk_60s anchor region within one composite trip",
    }
    if legacy_phase3c2_metrics is not None:
        result["phase3c2_risk_coverage_backward_comparison"] = legacy_phase3c2_metrics.get(
            "event_coverage"
        )
        result["phase3c2_legacy_metrics"] = legacy_phase3c2_metrics
    return result


_CONSTRAINTS = (
    ("any_intervention_recall", "any_intervention_recall_floor", "min"),
    ("timely_coverage_3s", "timely_coverage_3s_floor", "min"),
    (
        "mean_advisory_voice_per_driving_hour",
        "mean_advisory_voice_per_driving_hour_max",
        "max",
    ),
    (
        "p95_rolling_advisory_voice_count_60min",
        "p95_rolling_advisory_voice_count_max",
        "max",
    ),
    (
        "offline_false_voice_per_driving_hour",
        "offline_false_voice_per_driving_hour_max",
        "max",
    ),
    ("voice_precision", "voice_precision_min", "min"),
)


def acceptance_table(metrics: dict, criteria: dict) -> list[dict]:
    rows = []
    for metric_name, criterion_name, direction in _CONSTRAINTS:
        result = metrics.get(metric_name)
        target = criteria[criterion_name]
        passed = None if result is None else (
            result >= target if direction == "min" else result <= target
        )
        rows.append(
            {
                "metric": metric_name,
                "criterion": criterion_name,
                "operator": ">=" if direction == "min" else "<=",
                "target": target,
                "result": result,
                "status": "UNAVAILABLE" if passed is None else "PASS" if passed else "FAIL",
            }
        )
    rows.extend(
        [
            {
                "metric": "any_intervention_recall",
                "criterion": "any_intervention_recall_target",
                "operator": ">=",
                "target": criteria["any_intervention_recall_target"],
                "result": metrics.get("any_intervention_recall"),
                "status": "UNAVAILABLE"
                if metrics.get("any_intervention_recall") is None
                else "PASS"
                if metrics["any_intervention_recall"]
                >= criteria["any_intervention_recall_target"]
                else "FAIL",
            },
            {
                "metric": "max_rolling_advisory_voice_count_60min",
                "criterion": "advisory_voice_per_rolling_60min_hard_max",
                "operator": "<=",
                "target": criteria["advisory_voice_per_rolling_60min_hard_max"],
                "result": metrics.get("max_rolling_advisory_voice_count_60min"),
                "status": "PASS"
                if metrics.get("max_rolling_advisory_voice_count_60min", 0)
                <= criteria["advisory_voice_per_rolling_60min_hard_max"]
                else "FAIL",
            },
            {
                "metric": "median_lead_time_seconds",
                "criterion": "median_lead_time_target_seconds",
                "operator": ">=",
                "target": criteria["median_lead_time_target_seconds"],
                "result": metrics.get("median_lead_time_seconds"),
                "status": "UNAVAILABLE"
                if metrics.get("median_lead_time_seconds") is None
                else "PASS"
                if metrics["median_lead_time_seconds"]
                >= criteria["median_lead_time_target_seconds"]
                else "FAIL",
            },
            {
                "metric": "same_episode_l2_voice_max_observed",
                "criterion": "same_episode_l2_voice_max",
                "operator": "<=",
                "target": criteria["same_episode_l2_voice_max"],
                "result": metrics.get("same_episode_l2_voice_max_observed"),
                "status": "PASS"
                if metrics.get("same_episode_l2_voice_max_observed", 0)
                <= criteria["same_episode_l2_voice_max"]
                else "FAIL",
            },
        ]
    )
    return rows


def _lexicographic_key(candidate: dict) -> tuple:
    metrics = candidate["metrics"]
    high = lambda name: metrics.get(name) if metrics.get(name) is not None else float("-inf")
    low = lambda name: -metrics.get(name) if metrics.get(name) is not None else float("-inf")
    return (
        high("timely_coverage_3s"),
        high("any_intervention_recall"),
        low("offline_false_voice_per_driving_hour"),
        low("advisory_voice_alerts_per_driving_hour"),
        high("median_lead_time_seconds"),
        low("level_transitions_per_driving_hour"),
    )


def select_policy_candidate(candidates: list[dict], criteria: dict) -> dict:
    """Apply required constraints first, then the specified lexicographic order."""

    assessed = []
    for candidate in candidates:
        table = acceptance_table(candidate["metrics"], criteria)
        required = table[: len(_CONSTRAINTS)]
        failures = [row for row in required if row["status"] == "FAIL"]
        assessed.append(
            {
                **candidate,
                "acceptance": table,
                "feasible": not failures,
                "constraint_violations": [row["metric"] for row in failures],
                "constraint_violation_count": len(failures),
            }
        )
    feasible = [row for row in assessed if row["feasible"]]
    winner = max(feasible, key=_lexicographic_key) if feasible else None

    def dominates(left: dict, right: dict) -> bool:
        lk, rk = _lexicographic_key(left), _lexicographic_key(right)
        return all(a >= b for a, b in zip(lk, rk)) and any(
            a > b for a, b in zip(lk, rk)
        )

    pareto = [
        row
        for row in assessed
        if not any(dominates(other, row) for other in assessed if other is not row)
    ]
    violations = sorted(
        assessed,
        key=lambda row: (row["constraint_violation_count"], tuple(-x for x in _lexicographic_key(row))),
    )
    return {
        "winner": winner,
        "winner_status": "FEASIBLE_POLICY_SELECTED" if winner else "NO_FEASIBLE_POLICY",
        "feasible_candidate_count": len(feasible),
        "candidate_count": len(assessed),
        "pareto_frontier": pareto,
        "constraint_violation_table": violations,
        "selection_method": "required_constraints_then_lexicographic",
    }
