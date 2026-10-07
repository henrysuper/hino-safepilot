"""Post-trip replay of the frozen 60-second risk model and Alert Policy V1, per trip.

The telemetry was recorded without SafePilot, so drivers never heard these alerts. The
replay answers "when would SafePilot have warned, and what happened next" — it does not
claim the driver reacted. Train-split trips were seen by the model during training, so
their replay is optimistic and is labelled as such.
"""

from __future__ import annotations

import json
import shutil
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import joblib
import pyarrow.dataset as ds

from hino_safepilot.alerting.contracts import AlertPolicyConfig
from hino_safepilot.alerting.evaluation import replay_revised_policy
from hino_safepilot.modeling.training import load_split

from .pipeline import _write_jsonl


SPLITS = ("train", "validation", "test")
# Event families behind the Label V0 target `label_any_core_risk_60s`.
CORE_RISK_EVENTS = ("decelerate", "pcs", "ldws", "fatigue", "distract")
HORIZON_SECONDS = 60.0


def _core_onsets(interim_root: Path) -> dict[str, list[tuple[datetime, str]]]:
    dataset = ds.dataset(interim_root / "events_onsets", format="parquet", partitioning="hive")
    columns = [
        "trip_id",
        "event_name",
        "event_start_time",
        "event_start_time_quality",
        "start_time_ambiguous",
        "start_year_mismatch_observed",
    ]
    onsets: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for row in dataset.to_table(columns=columns).to_pylist():
        if (
            row["event_name"] in CORE_RISK_EVENTS
            and row["event_start_time"] is not None
            and row["event_start_time_quality"] == "valid"
            and not row["start_time_ambiguous"]
            and not row["start_year_mismatch_observed"]
        ):
            onsets[str(row["trip_id"])].append((row["event_start_time"], row["event_name"]))
    for events in onsets.values():
        events.sort()
    return onsets


def review_alerts(
    alerts: list[dict], onsets: dict[str, list[tuple[datetime, str]]], split: str
) -> dict[str, dict[str, Any]]:
    """Group replayed alerts by trip and link each to the next core-risk onset within 60 s."""

    reviews: dict[str, dict[str, Any]] = {}
    for alert in alerts:
        trip_id = alert["trip_id"]
        events = onsets.get(trip_id, [])
        start = alert["timestamp"]
        index = bisect_right([time for time, _ in events], start)
        following = events[index] if index < len(events) else None
        linked = (
            following
            if following is not None
            and (following[0] - start) <= timedelta(seconds=HORIZON_SECONDS)
            else None
        )
        review = reviews.setdefault(trip_id, {"split": split, "alerts": []})
        review["alerts"].append(
            {
                "timestamp": start.isoformat(),
                "probability": round(float(alert["probability"]), 3),
                "reason": alert["reason"],
                "event_name": linked[1] if linked else None,
                "lead_seconds": round((linked[0] - start).total_seconds()) if linked else None,
            }
        )
    for review in reviews.values():
        review["alert_count"] = len(review["alerts"])
        review["event_followed_count"] = sum(a["event_name"] is not None for a in review["alerts"])
    return reviews


def build_trip_alert_reviews(
    repo_root: Path, *, output_root: Path = Path("data/processed/trip_alerts")
) -> dict[str, Any]:
    """Score every dataset anchor with the frozen bundle and replay Alert Policy V1."""

    repo = repo_root.resolve()
    output = (repo / output_root).resolve()
    processed = (repo / "data" / "processed").resolve()
    if output == processed or processed not in output.parents:
        raise ValueError("Trip alert output must be below data/processed/")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite trip alert output: {output}")
    dataset_root = repo / "data" / "processed" / "ml" / "v0"
    model_root = repo / "models" / "risk" / "v1_xgboost"
    bundle_path = model_root / "risk_model_bundle.joblib"
    if not bundle_path.is_file():
        raise FileNotFoundError("Frozen XGBoost bundle is required; this command never retrains")
    features = json.loads((dataset_root / "metadata" / "resolved_feature_spec.json").read_text())[
        "feature_columns"
    ]
    bundle = joblib.load(bundle_path)
    policy = AlertPolicyConfig(**json.loads((model_root / "alert_policy_v1.json").read_text()))
    onsets = _core_onsets(repo / "data" / "interim")

    reviews: dict[str, dict[str, Any]] = {}
    anchors: Counter[str] = Counter()
    for split in SPLITS:
        data = load_split(dataset_root, split, features)
        anchors[split] = len(data.y)
        alerts = replay_revised_policy(
            data.trip_ids, data.timestamps, bundle.predict_proba(data.X), policy
        )
        reviews.update(review_alerts(alerts, onsets, split))
        for trip_id in {str(trip) for trip in data.trip_ids}:
            reviews.setdefault(
                trip_id,
                {"split": split, "alerts": [], "alert_count": 0, "event_followed_count": 0},
            )

    staging = output.with_name(f".{output.name}.staging-{uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        count = _write_jsonl(
            staging / "trip_alert_reviews.jsonl",
            ({"trip_id": trip_id, **review} for trip_id, review in sorted(reviews.items())),
        )
        by_split = {
            split: {
                "anchors": anchors[split],
                "trips": sum(r["split"] == split for r in reviews.values()),
                "trips_with_alerts": sum(
                    r["split"] == split and r["alert_count"] > 0 for r in reviews.values()
                ),
                "alerts": sum(r["alert_count"] for r in reviews.values() if r["split"] == split),
                "alerts_followed_by_event": sum(
                    r["event_followed_count"] for r in reviews.values() if r["split"] == split
                ),
            }
            for split in SPLITS
        }
        manifest = {
            "trip_alert_review_version": "trip-alert-review-v1",
            "model_version": bundle.model_version,
            "policy_version": policy.policy_version,
            "retraining_performed": False,
            "external_api_calls": 0,
            "replay_semantics": "offline replay; drivers did not receive these alerts",
            "train_split_is_in_sample": True,
            "trip_review_count": count,
            "by_split": by_split,
        }
        (staging / "trip_alert_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staging.replace(output)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return manifest
