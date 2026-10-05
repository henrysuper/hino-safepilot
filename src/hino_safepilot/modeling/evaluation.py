"""Deterministic evaluation helpers for ranking, calibration, alerts, and lead time."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable

import numpy as np
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)


def reliability_table(y_true, probabilities, bins: int = 10) -> dict:
    y = np.asarray(y_true, dtype=int)
    p = np.clip(np.asarray(probabilities, dtype=float), 0.0, 1.0)
    edges = np.linspace(0.0, 1.0, bins + 1)
    rows = []
    ece = 0.0
    for index in range(bins):
        lower, upper = edges[index], edges[index + 1]
        mask = (p >= lower) & (p < upper if index < bins - 1 else p <= upper)
        count = int(mask.sum())
        if count:
            mean_probability = float(p[mask].mean())
            observed_rate = float(y[mask].mean())
            absolute_gap = abs(mean_probability - observed_rate)
            ece += count / len(y) * absolute_gap
        else:
            mean_probability = observed_rate = absolute_gap = None
        rows.append(
            {
                "lower": float(lower),
                "upper": float(upper),
                "count": count,
                "mean_probability": mean_probability,
                "observed_rate": observed_rate,
                "absolute_gap": absolute_gap,
            }
        )
    return {"bin_count": bins, "expected_calibration_error": float(ece), "bins": rows}


def ranking_calibration_metrics(y_true, probabilities, bins: int = 10) -> dict:
    y = np.asarray(y_true, dtype=int)
    p = np.clip(np.asarray(probabilities, dtype=float), 1e-15, 1 - 1e-15)
    return {
        "row_count": int(len(y)),
        "positive_count": int(y.sum()),
        "positive_rate": float(y.mean()),
        "pr_auc": float(average_precision_score(y, p)),
        "roc_auc": float(roc_auc_score(y, p)),
        "brier_score": float(brier_score_loss(y, p)),
        "log_loss": float(log_loss(y, p, labels=[0, 1])),
        "reliability": reliability_table(y, p, bins),
    }


def threshold_metrics(y_true, probabilities, threshold: float) -> dict:
    y = np.asarray(y_true, dtype=int)
    p = np.asarray(probabilities, dtype=float)
    predicted = (p >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, predicted, labels=[0, 1]).ravel()
    specificity = tn / (tn + fp) if tn + fp else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0
    fnr = fn / (fn + tp) if fn + tp else 0.0
    return {
        "threshold": float(threshold),
        "precision": float(precision_score(y, predicted, zero_division=0)),
        "recall": float(recall_score(y, predicted, zero_division=0)),
        "f1": float(f1_score(y, predicted, zero_division=0)),
        "specificity": float(specificity),
        "false_positive_rate": float(fpr),
        "false_negative_rate": float(fnr),
        "predicted_positive_rate": float(predicted.mean()),
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }


def threshold_sweep(y_true, probabilities, start: float, stop: float, step: float) -> list[dict]:
    count = int(round((stop - start) / step)) + 1
    thresholds = np.linspace(start, stop, count)
    return [threshold_metrics(y_true, probabilities, float(value)) for value in thresholds]


def select_operating_points(
    rows: list[dict], *, high_recall_minimum: float, low_alert_maximum_fpr: float
) -> dict[str, dict]:
    eligible_recall = [row for row in rows if row["recall"] >= high_recall_minimum]
    high_recall = max(
        eligible_recall or rows,
        key=lambda row: (row["precision"], row["threshold"], row["recall"]),
    )
    balanced = max(rows, key=lambda row: (row["f1"], row["recall"], row["threshold"]))
    eligible_burden = [row for row in rows if row["false_positive_rate"] <= low_alert_maximum_fpr]
    low_burden = max(
        eligible_burden or rows,
        key=lambda row: (row["recall"], row["precision"], -row["threshold"]),
    )
    return {"high_recall": high_recall, "balanced": balanced, "low_alert_burden": low_burden}


def driving_hours(
    driving_fraction: np.ndarray,
    *,
    anchor_exposure_seconds: float,
    driving_fraction_threshold: float,
) -> float:
    count = int((np.asarray(driving_fraction, dtype=float) >= driving_fraction_threshold).sum())
    return count * anchor_exposure_seconds / 3600.0


def alert_episode_metrics(
    y_true,
    probabilities,
    trip_ids: Iterable[str],
    timestamps: Iterable[datetime],
    threshold: float,
    cooldown_seconds: int,
    exposure_hours: float,
) -> dict:
    grouped: dict[str, list[tuple[datetime, float, int]]] = defaultdict(list)
    for trip, timestamp, probability, truth in zip(trip_ids, timestamps, probabilities, y_true):
        grouped[str(trip)].append((timestamp, float(probability), int(truth)))
    alerts: list[tuple[str, datetime, bool]] = []
    for trip, rows in grouped.items():
        last_alert = None
        for timestamp, probability, truth in sorted(rows):
            if probability < threshold:
                continue
            if last_alert is None or (timestamp - last_alert).total_seconds() >= cooldown_seconds:
                alerts.append((trip, timestamp, bool(truth)))
                last_alert = timestamp
    true_count = sum(int(value[2]) for value in alerts)
    false_count = len(alerts) - true_count
    divisor = exposure_hours if exposure_hours > 0 else float("nan")
    return {
        "policy_status": "EXPERIMENTAL_ALERT_POLICY",
        "cooldown_seconds": int(cooldown_seconds),
        "total_alert_episodes": len(alerts),
        "true_alert_episodes": true_count,
        "false_alert_episodes": false_count,
        "alerts_per_driving_hour": float(len(alerts) / divisor),
        "false_alert_episodes_per_driving_hour": float(false_count / divisor),
    }


def raw_false_positive_anchors_per_hour(
    y_true, probabilities, threshold: float, exposure_hours: float
) -> float:
    y = np.asarray(y_true, dtype=int)
    predicted = np.asarray(probabilities) >= threshold
    false_positives = int(((y == 0) & predicted).sum())
    return float(false_positives / exposure_hours) if exposure_hours > 0 else float("nan")


def warning_lead_time(
    events: Iterable[dict],
    probabilities,
    trip_ids: Iterable[str],
    timestamps: Iterable[datetime],
    threshold: float,
) -> dict:
    anchors: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for trip, timestamp, probability in zip(trip_ids, timestamps, probabilities):
        anchors[str(trip)].append((timestamp, float(probability)))
    for rows in anchors.values():
        rows.sort()
    leads: list[float] = []
    by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"eligible_events": 0, "detected": 0})
    eligible = 0
    detected = 0
    for event in events:
        trip = str(event["trip_id"])
        onset = event["event_start_time"]
        # Strictly preceding anchors only; event-at/onset and post-onset predictions are rejected.
        candidates = [
            (timestamp, probability)
            for timestamp, probability in anchors.get(trip, [])
            if 0 < (onset - timestamp).total_seconds() <= 60
        ]
        if not candidates:
            continue
        eligible += 1
        event_name = str(event["event_name"])
        by_type[event_name]["eligible_events"] += 1
        crossings = [timestamp for timestamp, probability in candidates if probability >= threshold]
        if crossings:
            first = min(crossings)
            lead = float((onset - first).total_seconds())
            leads.append(lead)
            detected += 1
            by_type[event_name]["detected"] += 1
    lead_array = np.asarray(leads, dtype=float)
    for values in by_type.values():
        values["coverage"] = (
            values["detected"] / values["eligible_events"] if values["eligible_events"] else None
        )
    return {
        "eligible_events": eligible,
        "detected_events": detected,
        "detection_coverage": detected / eligible if eligible else None,
        "median_lead_time_seconds": float(np.median(lead_array)) if leads else None,
        "p25_lead_time_seconds": float(np.percentile(lead_array, 25)) if leads else None,
        "p75_lead_time_seconds": float(np.percentile(lead_array, 75)) if leads else None,
        "detected_at_least_10_seconds": int((lead_array >= 10).sum()),
        "detected_at_least_30_seconds": int((lead_array >= 30).sum()),
        "coverage_at_least_10_seconds": float((lead_array >= 10).sum() / eligible) if eligible else None,
        "coverage_at_least_30_seconds": float((lead_array >= 30).sum() / eligible) if eligible else None,
        "event_type_diagnostics": dict(sorted(by_type.items())),
    }
