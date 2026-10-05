"""Phase 3C-1 training orchestration with a one-way temporal test lock."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import joblib
import numpy as np
import pyarrow.dataset as pads
import pyarrow.parquet as pq
import sklearn
from sklearn.dummy import DummyClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from .calibration import make_calibrator
from .contracts import PredictionDataQuality, RiskLevel, RiskPredictionResult
from .evaluation import (
    alert_episode_metrics,
    driving_hours,
    ranking_calibration_metrics,
    raw_false_positive_anchors_per_hour,
    select_operating_points,
    threshold_metrics,
    threshold_sweep,
    warning_lead_time,
)
from .preprocessing import (
    build_preprocessor,
    exact_constant_original_features,
    validate_feature_names,
)


TARGET = "label_any_core_risk_60s"
DIAGNOSTIC_LABELS = [
    "label_decelerate_60s",
    "label_pcs_60s",
    "label_ldws_60s",
    "label_fatigue_60s",
    "label_distract_60s",
]
CORE_EVENTS = {7: "decelerate", 19: "pcs", 20: "ldws", 12: "fatigue", 16: "distract"}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _scope_digest(anchor_ids: np.ndarray) -> str:
    digest = hashlib.sha256()
    for value in sorted(str(item) for item in anchor_ids):
        digest.update(value.encode())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass
class SplitData:
    split: str
    feature_names: list[str]
    X: np.ndarray
    y: np.ndarray
    diagnostics: dict[str, np.ndarray]
    anchor_ids: np.ndarray
    trip_ids: np.ndarray
    vehicle_ids: np.ndarray
    timestamps: np.ndarray
    vehicle_types: np.ndarray
    vehicle_holdout: np.ndarray

    def subset(self, mask: np.ndarray, name: str | None = None) -> "SplitData":
        return SplitData(
            split=name or self.split,
            feature_names=self.feature_names,
            X=self.X[mask],
            y=self.y[mask],
            diagnostics={key: value[mask] for key, value in self.diagnostics.items()},
            anchor_ids=self.anchor_ids[mask],
            trip_ids=self.trip_ids[mask],
            vehicle_ids=self.vehicle_ids[mask],
            timestamps=self.timestamps[mask],
            vehicle_types=self.vehicle_types[mask],
            vehicle_holdout=self.vehicle_holdout[mask],
        )


def load_split(dataset_root: Path, split: str, feature_names: list[str]) -> SplitData:
    dataset_path = dataset_root / "dataset" / f"split={split}" / "part-00000.parquet"
    metadata_path = dataset_root / "metadata" / f"split={split}" / "part-00000.parquet"
    dataset_columns = ["anchor_id", *feature_names, TARGET, *DIAGNOSTIC_LABELS]
    metadata_columns = [
        "anchor_id",
        "trip_id",
        "vehicle_id",
        "anchor_timestamp",
        "vehicle_series_code",
        "vehicle_holdout_assignment",
    ]
    table = pq.read_table(dataset_path, columns=dataset_columns)
    metadata = pq.read_table(metadata_path, columns=metadata_columns)
    if table.num_rows != metadata.num_rows:
        raise RuntimeError(f"Dataset/metadata row mismatch for {split}")
    dataset_ids = np.asarray(table["anchor_id"].to_pylist(), dtype=object)
    metadata_ids = np.asarray(metadata["anchor_id"].to_pylist(), dtype=object)
    if not np.array_equal(dataset_ids, metadata_ids):
        raise RuntimeError(f"Dataset/metadata anchor alignment failure for {split}")
    X = np.column_stack(
        [table[name].to_numpy(zero_copy_only=False).astype(float) for name in feature_names]
    )
    return SplitData(
        split=split,
        feature_names=feature_names,
        X=X,
        y=table[TARGET].to_numpy(zero_copy_only=False).astype(np.int8),
        diagnostics={
            name: table[name].to_numpy(zero_copy_only=False).astype(np.int8)
            for name in DIAGNOSTIC_LABELS
        },
        anchor_ids=dataset_ids,
        trip_ids=np.asarray(metadata["trip_id"].to_pylist(), dtype=object),
        vehicle_ids=np.asarray(metadata["vehicle_id"].to_pylist(), dtype=object),
        timestamps=np.asarray(metadata["anchor_timestamp"].to_pylist(), dtype=object),
        vehicle_types=metadata["vehicle_series_code"].to_numpy(zero_copy_only=False),
        vehicle_holdout=np.asarray(
            metadata["vehicle_holdout_assignment"].to_pylist(), dtype=object
        ),
    )


def trip_time_mask(data: SplitData, cutoff: datetime, *, before: bool) -> np.ndarray:
    first: dict[str, datetime] = {}
    for trip, timestamp in zip(data.trip_ids, data.timestamps):
        key = str(trip)
        first[key] = min(first.get(key, timestamp), timestamp)
    if before:
        return np.asarray([first[str(trip)] < cutoff for trip in data.trip_ids], dtype=bool)
    return np.asarray([first[str(trip)] >= cutoff for trip in data.trip_ids], dtype=bool)


def build_pipeline(feature_names: list[str], config: dict, C: float, class_weight):
    preprocessor, spec = build_preprocessor(
        feature_names, config["recency_no_event_sentinel_seconds"]
    )
    model = LogisticRegression(
        C=C,
        class_weight=class_weight,
        solver=config["solver"],
        max_iter=config["max_iter"],
        tol=config["tolerance"],
        random_state=config["random_seed"],
    )
    return Pipeline([("preprocessor", preprocessor), ("model", model)]), spec


@dataclass
class RiskModelBundle:
    pipeline: Pipeline
    calibrator: Any
    feature_names: list[str]
    model_version: str
    threshold_version: str
    selected_threshold: float
    high_recall_threshold: float

    def predict_proba(self, values: np.ndarray) -> np.ndarray:
        raw = self.pipeline.predict_proba(values)[:, 1]
        return self.calibrator.predict(raw)

    def predict_result(
        self,
        values: np.ndarray,
        *,
        data_quality_status: PredictionDataQuality = PredictionDataQuality.VALID,
    ) -> RiskPredictionResult:
        probability = float(self.predict_proba(np.asarray(values).reshape(1, -1))[0])
        lower = min(self.high_recall_threshold, self.selected_threshold)
        if probability < lower:
            level = RiskLevel.LOW
        elif probability < self.selected_threshold:
            level = RiskLevel.ELEVATED
        else:
            level = RiskLevel.HIGH
        return RiskPredictionResult(
            risk_probability=probability,
            risk_level=level,
            threshold_version=self.threshold_version,
            model_version=self.model_version,
            data_quality_status=data_quality_status,
        )


def _gate(repo_root: Path, dataset_root: Path, feature_names: list[str]) -> dict:
    manifest = _json(dataset_root / "dataset_manifest.json")
    leakage = _json(dataset_root / "audit" / "dataset_leakage_audit.json")
    privacy = _json(dataset_root / "audit" / "privacy_audit.json")
    feature_privacy = validate_feature_names(feature_names)
    status = (
        "PASS"
        if manifest["status"] == "READY"
        and leakage["status"] == "PASS"
        and privacy["status"] == "PASS"
        and feature_privacy["status"] == "PASS"
        else "FAIL"
    )
    return {
        "status": status,
        "dataset_status": manifest["status"],
        "dataset_leakage_status": leakage["status"],
        "dataset_privacy_status": privacy["status"],
        "model_feature_privacy_status": feature_privacy["status"],
        "forbidden_features": feature_privacy["rejected"],
        "dataset_digest": _digest(manifest["semantic_content_digests"]),
        "protected_input_manifest": str(dataset_root / "dataset_manifest.json"),
    }


def _candidate_selection(train_fit: SplitData, train_dev: SplitData, config: dict) -> tuple[dict, list]:
    candidates = []
    best = None
    for C in config["candidate_C"]:
        for class_weight in config["candidate_class_weight"]:
            started = perf_counter()
            pipeline, _ = build_pipeline(train_fit.feature_names, config, C, class_weight)
            pipeline.fit(train_fit.X, train_fit.y)
            probabilities = pipeline.predict_proba(train_dev.X)[:, 1]
            metrics = ranking_calibration_metrics(
                train_dev.y, probabilities, config["calibration_bins"]
            )
            row = {
                "C": C,
                "class_weight": class_weight,
                "fit_scope": "train_before_2025-07",
                "evaluation_scope": "train_2025-07_development",
                "metrics": metrics,
                "runtime_seconds": perf_counter() - started,
                "converged": bool(pipeline.named_steps["model"].n_iter_[0] < config["max_iter"]),
            }
            candidates.append(row)
            if best is None or (
                metrics["pr_auc"], metrics["roc_auc"], -metrics["log_loss"]
            ) > (
                best["metrics"]["pr_auc"],
                best["metrics"]["roc_auc"],
                -best["metrics"]["log_loss"],
            ):
                best = row
    assert best is not None
    return best, candidates


def _calibration_selection(pipeline, calibration: SplitData, threshold_dev: SplitData, config):
    raw_calibration = pipeline.predict_proba(calibration.X)[:, 1]
    raw_threshold = pipeline.predict_proba(threshold_dev.X)[:, 1]
    calibrators = {}
    comparison = {}
    for method in config["calibration_methods"]:
        calibrator = make_calibrator(method, config["random_seed"])
        calibrator.fit(
            raw_calibration,
            calibration.y,
            fit_scope="validation_2025-09_early_calibration",
        )
        probabilities = calibrator.predict(raw_threshold)
        calibrators[method] = calibrator
        comparison[method] = ranking_calibration_metrics(
            threshold_dev.y, probabilities, config["calibration_bins"]
        )
    raw_pr = comparison["raw"]["pr_auc"]
    acceptable = [
        method
        for method in config["calibration_methods"]
        if comparison[method]["pr_auc"] >= raw_pr - 0.01
    ]
    selected = min(
        acceptable,
        key=lambda method: (
            comparison[method]["brier_score"],
            comparison[method]["log_loss"],
            -comparison[method]["pr_auc"],
        ),
    )
    return selected, calibrators[selected], comparison, raw_threshold


def _trusted_core_events(
    repo_root: Path,
    *,
    allowed_trip_ids: set[str] | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> list[dict]:
    source = pads.dataset(
        repo_root / "data" / "interim" / "events_onsets",
        format="parquet",
        partitioning="hive",
    )
    columns = [
        "trip_id",
        "event_type",
        "event_name",
        "event_start_time",
        "event_start_time_quality",
        "start_time_ambiguous",
        "start_year_mismatch_observed",
    ]
    predicate = None
    if allowed_trip_ids is not None:
        predicate = pads.field("trip_id").isin(sorted(allowed_trip_ids))
    if start_time is not None:
        clause = pads.field("event_start_time") >= start_time
        predicate = clause if predicate is None else predicate & clause
    if end_time is not None:
        clause = pads.field("event_start_time") <= end_time
        predicate = clause if predicate is None else predicate & clause
    table = source.to_table(columns=columns, filter=predicate)
    events = []
    for row in table.to_pylist():
        event_type = row["event_type"]
        if (
            event_type in CORE_EVENTS
            and row["trip_id"] is not None
            and row["event_start_time"] is not None
            and row["event_start_time_quality"] == "valid"
            and not row["start_time_ambiguous"]
            and not row["start_year_mismatch_observed"]
        ):
            events.append(
                {
                    "trip_id": row["trip_id"],
                    "event_type": int(event_type),
                    "event_name": CORE_EVENTS[int(event_type)],
                    "event_start_time": row["event_start_time"],
                }
            )
    return events


def _slice_metrics(data: SplitData, probabilities: np.ndarray, threshold: float, values) -> dict:
    result = {}
    for value in sorted(set(values)):
        mask = np.asarray(values) == value
        if mask.sum() == 0 or np.unique(data.y[mask]).size < 2:
            result[str(value)] = {"row_count": int(mask.sum()), "status": "INSUFFICIENT_SUPPORT"}
            continue
        result[str(value)] = {
            **ranking_calibration_metrics(data.y[mask], probabilities[mask]),
            **threshold_metrics(data.y[mask], probabilities[mask], threshold),
        }
    return result


def _coefficient_audit(
    pipeline: Pipeline, feature_names: list[str], feature_groups: dict[str, str]
) -> dict:
    transformed = list(
        pipeline.named_steps["preprocessor"].get_feature_names_out(feature_names)
    )
    coefficients = pipeline.named_steps["model"].coef_[0]

    def original_name(name: str) -> str:
        value = name
        if value.startswith("missingindicator_"):
            value = value.removeprefix("missingindicator_")
        if value.startswith("no_prior_event__"):
            value = value.removeprefix("no_prior_event__")
        if value.startswith("vehicle_series_code_"):
            return "vehicle_series_code"
        return value

    rows = [
        {
            "transformed_feature": str(name),
            "source_feature": original_name(str(name)),
            "feature_family": feature_groups.get(original_name(str(name)), "derived_indicator"),
            "coefficient": float(value),
            "absolute_coefficient": float(abs(value)),
            "interpretation": "association_only_not_causal",
        }
        for name, value in zip(transformed, coefficients)
    ]
    positive = sorted(rows, key=lambda row: row["coefficient"], reverse=True)[:20]
    negative = sorted(rows, key=lambda row: row["coefficient"])[:20]
    quality_terms = [
        row
        for row in rows
        if any(token in row["source_feature"] for token in ("density", "point_count", "valid_count", "gap"))
    ]
    return {
        "transformed_feature_count": len(rows),
        "top_positive": positive,
        "top_negative": negative,
        "largest_quality_or_density": sorted(
            quality_terms, key=lambda row: row["absolute_coefficient"], reverse=True
        )[:20],
        "causal_interpretation_allowed": False,
    }


def _temporal_stability(train: SplitData, config: dict, selected: dict) -> dict:
    months = sorted(set(timestamp.strftime("%Y-%m") for timestamp in train.timestamps))
    rows = {}
    for index in range(1, len(months)):
        history = set(months[:index])
        evaluation_month = months[index]
        fit_mask = np.asarray([timestamp.strftime("%Y-%m") in history for timestamp in train.timestamps])
        eval_mask = np.asarray(
            [timestamp.strftime("%Y-%m") == evaluation_month for timestamp in train.timestamps]
        )
        pipeline, _ = build_pipeline(
            train.feature_names, config, selected["C"], selected["class_weight"]
        )
        pipeline.fit(train.X[fit_mask], train.y[fit_mask])
        probabilities = pipeline.predict_proba(train.X[eval_mask])[:, 1]
        rows[evaluation_month] = {
            "fit_months": sorted(history),
            **ranking_calibration_metrics(train.y[eval_mask], probabilities),
        }
    return {"strategy": "expanding_month_forward_validation", "months": rows}


def _event_anchor_diagnostics(data: SplitData, probabilities: np.ndarray, threshold: float) -> dict:
    predicted = probabilities >= threshold
    result = {}
    for label, values in data.diagnostics.items():
        positives = values == 1
        result[label] = {
            "positive_anchor_count": int(positives.sum()),
            "detected_anchor_count": int((positives & predicted).sum()),
            "detection_coverage": float((positives & predicted).sum() / positives.sum())
            if positives.sum()
            else None,
            "standalone_model_claim": False,
        }
    return result


def train_phase3c1(
    repo_root: Path,
    *,
    config_path: Path | None = None,
    artifact_root: Path | None = None,
    report_root: Path | None = None,
) -> dict:
    started = perf_counter()
    repo_root = repo_root.resolve()
    config_path = (config_path or repo_root / "config" / "logistic_v0.json").resolve()
    artifact_root = (artifact_root or repo_root / "models" / "risk" / "v0_logistic").resolve()
    report_root = (report_root or repo_root / "reports" / "generated" / "phase3c1").resolve()
    dataset_root = repo_root / "data" / "processed" / "ml" / "v0"
    if artifact_root.exists() or report_root.exists():
        raise FileExistsError("Refusing to overwrite an existing Phase 3C-1 artifact/report root")
    config = _json(config_path)
    resolved_spec = _json(dataset_root / "metadata" / "resolved_feature_spec.json")
    feature_names = resolved_spec["feature_columns"]
    feature_groups = {row["name"]: row["group"] for row in resolved_spec["features"]}
    gate = _gate(repo_root, dataset_root, feature_names)
    if gate["status"] != "PASS":
        raise RuntimeError(f"Dataset gate failed: {gate}")

    staging_artifact = artifact_root.with_name(f".{artifact_root.name}-staging-{uuid4().hex}")
    staging_report = report_root.with_name(f".{report_root.name}-staging-{uuid4().hex}")
    staging_artifact.mkdir(parents=True)
    staging_report.mkdir(parents=True)
    test_lock = {
        "status": "LOCKED",
        "test_data_opened": False,
        "test_open_count": 0,
        "selection_scopes": {},
    }

    train = load_split(dataset_root, "train", feature_names)
    validation = load_split(dataset_root, "validation", feature_names)
    train_cutoff = datetime.fromisoformat(config["train_development_start"])
    train_fit = train.subset(trip_time_mask(train, train_cutoff, before=True), "train_fit")
    train_dev = train.subset(trip_time_mask(train, train_cutoff, before=False), "train_dev")
    calibration_cutoff = datetime.fromisoformat(
        config["validation_calibration_trip_start_before"]
    )
    calibration = validation.subset(
        trip_time_mask(validation, calibration_cutoff, before=True), "validation_calibration"
    )
    threshold_dev = validation.subset(
        trip_time_mask(validation, calibration_cutoff, before=False), "validation_threshold"
    )
    for scope in (train_fit, train_dev, calibration, threshold_dev):
        if len(scope.y) == 0 or np.unique(scope.y).size < 2:
            raise RuntimeError(f"Insufficient support in {scope.split}")

    selected_config, candidates = _candidate_selection(train_fit, train_dev, config)
    pipeline, preprocessing = build_pipeline(
        feature_names, config, selected_config["C"], selected_config["class_weight"]
    )
    exact_constants = exact_constant_original_features(train.X, preprocessing)
    pipeline.fit(train.X, train.y)
    selected_calibration, calibrator, calibration_comparison, raw_threshold = (
        _calibration_selection(pipeline, calibration, threshold_dev, config)
    )
    threshold_probabilities = calibrator.predict(raw_threshold)
    sweep = threshold_sweep(
        threshold_dev.y,
        threshold_probabilities,
        config["threshold_start"],
        config["threshold_stop"],
        config["threshold_step"],
    )
    operating_points = select_operating_points(
        sweep,
        high_recall_minimum=config["high_recall_minimum"],
        low_alert_maximum_fpr=config["low_alert_maximum_fpr"],
    )
    selected_policy = config["selected_prototype_policy"]
    selected_key = "balanced" if selected_policy == "balanced_max_f1" else selected_policy
    selected_threshold = operating_points[selected_key]["threshold"]

    driving_index = feature_names.index("vehicle_status_driving_fraction_60s")
    threshold_hours = driving_hours(
        threshold_dev.X[:, driving_index],
        anchor_exposure_seconds=config["driving_anchor_exposure_seconds"],
        driving_fraction_threshold=config["driving_fraction_threshold"],
    )
    for row in operating_points.values():
        row["raw_false_positive_anchors_per_driving_hour"] = (
            raw_false_positive_anchors_per_hour(
                threshold_dev.y, threshold_probabilities, row["threshold"], threshold_hours
            )
        )
        row["experimental_alert_episodes"] = alert_episode_metrics(
            threshold_dev.y,
            threshold_probabilities,
            threshold_dev.trip_ids,
            threshold_dev.timestamps,
            row["threshold"],
            config["experimental_alert_cooldown_seconds"],
            threshold_hours,
        )

    dummy_results = {}
    for strategy in ("prior", "most_frequent", "stratified"):
        dummy = DummyClassifier(strategy=strategy, random_state=config["random_seed"])
        dummy.fit(train.X[:, :1], train.y)
        probability = dummy.predict_proba(threshold_dev.X[:, :1])[:, 1]
        dummy_results[strategy] = ranking_calibration_metrics(threshold_dev.y, probability)

    bundle = RiskModelBundle(
        pipeline=pipeline,
        calibrator=calibrator,
        feature_names=feature_names,
        model_version=config["model_version"],
        threshold_version=config["threshold_version"],
        selected_threshold=selected_threshold,
        high_recall_threshold=operating_points["high_recall"]["threshold"],
    )
    reproducible_pipeline, _ = build_pipeline(
        feature_names, config, selected_config["C"], selected_config["class_weight"]
    )
    reproducible_pipeline.fit(train.X, train.y)
    raw_repro = reproducible_pipeline.predict_proba(calibration.X)[:, 1]
    reproducible_calibrator = make_calibrator(selected_calibration, config["random_seed"])
    reproducible_calibrator.fit(
        raw_repro,
        calibration.y,
        fit_scope="validation_2025-09_early_calibration",
    )
    repro_probabilities = reproducible_calibrator.predict(
        reproducible_pipeline.predict_proba(threshold_dev.X)[:, 1]
    )
    reproducibility_max_difference = float(
        np.max(np.abs(repro_probabilities - threshold_probabilities))
    )
    reproducibility = {
        "status": "PASS"
        if reproducibility_max_difference <= config["reproducibility_absolute_tolerance"]
        else "FAIL",
        "maximum_probability_difference": reproducibility_max_difference,
        "absolute_tolerance": config["reproducibility_absolute_tolerance"],
    }
    if reproducibility["status"] != "PASS":
        raise RuntimeError(f"Reproducibility failure: {reproducibility}")

    joblib.dump(bundle, staging_artifact / "risk_model_bundle.joblib", compress=3)
    _write_json(
        staging_artifact / "feature_schema.json",
        {
            "feature_spec_version": config["feature_spec_version"],
            "feature_count": len(feature_names),
            "feature_names": feature_names,
            "target": TARGET,
            "forbidden_identity_guard": gate,
        },
    )
    _write_json(
        staging_artifact / "threshold_config.json",
        {
            "threshold_version": config["threshold_version"],
            "selected_prototype_policy": selected_policy,
            "selected_threshold": selected_threshold,
            "operating_points": operating_points,
            "risk_level_policy_status": "EXPERIMENTAL_UI_PROTOTYPE",
            "alert_policy_status": "EXPERIMENTAL_ALERT_POLICY",
        },
    )
    reloaded: RiskModelBundle = joblib.load(staging_artifact / "risk_model_bundle.joblib")
    reload_difference = float(
        np.max(
            np.abs(
                reloaded.predict_proba(threshold_dev.X[:100])
                - bundle.predict_proba(threshold_dev.X[:100])
            )
        )
    )
    reload_status = "PASS" if reload_difference <= 1e-12 else "FAIL"
    if reload_status != "PASS":
        raise RuntimeError("Serialized artifact did not reproduce probabilities")
    contract_example = reloaded.predict_result(threshold_dev.X[0]).model_dump(mode="json")

    temporal_stability = _temporal_stability(train, config, selected_config)
    coefficient_audit = _coefficient_audit(pipeline, feature_names, feature_groups)

    test_lock["selection_scopes"] = {
        "model_candidate_fit": "train_before_2025-07",
        "model_candidate_evaluation": "train_2025-07",
        "final_preprocessing_and_model_fit": "train_full",
        "calibration_fit": "validation_2025-09_early_whole_trips",
        "calibration_evaluation_and_threshold_selection": "validation_2025-09_late_whole_trips",
        "test": "not_used",
    }
    frozen_configuration = {
        "frozen_before_test": True,
        "model_config": {
            "C": selected_config["C"],
            "class_weight": selected_config["class_weight"],
            "solver": config["solver"],
        },
        "calibration_method": selected_calibration,
        "selected_threshold": selected_threshold,
        "feature_schema_digest": _digest(feature_names),
        "dataset_digest": gate["dataset_digest"],
        "config_digest": _digest(config),
    }
    frozen_configuration["freeze_digest"] = _digest(frozen_configuration)
    _write_json(staging_report / "frozen_configuration_before_test.json", frozen_configuration)
    test_lock["status"] = "FROZEN_CONFIGURATION_WRITTEN"

    # One-way gate: this is the first and only load of the temporal test data.
    test = load_split(dataset_root, "test", feature_names)
    test_lock["test_data_opened"] = True
    test_lock["test_open_count"] = 1
    test_lock["test_used_for_selection"] = False
    test_lock["status"] = "ONE_TIME_EVALUATION_COMPLETE"
    test_probabilities = reloaded.predict_proba(test.X)
    test_metrics = {
        **ranking_calibration_metrics(test.y, test_probabilities, config["calibration_bins"]),
        **threshold_metrics(test.y, test_probabilities, selected_threshold),
    }
    test_hours = driving_hours(
        test.X[:, driving_index],
        anchor_exposure_seconds=config["driving_anchor_exposure_seconds"],
        driving_fraction_threshold=config["driving_fraction_threshold"],
    )
    test_metrics["driving_exposure_hours"] = test_hours
    test_metrics["raw_false_positive_anchors_per_driving_hour"] = (
        raw_false_positive_anchors_per_hour(
            test.y, test_probabilities, selected_threshold, test_hours
        )
    )
    test_metrics["experimental_alert_episodes"] = alert_episode_metrics(
        test.y,
        test_probabilities,
        test.trip_ids,
        test.timestamps,
        selected_threshold,
        config["experimental_alert_cooldown_seconds"],
        test_hours,
    )
    events = _trusted_core_events(repo_root)
    lead_time = warning_lead_time(
        events,
        test_probabilities,
        test.trip_ids,
        test.timestamps,
        selected_threshold,
    )
    event_anchor_diagnostics = _event_anchor_diagnostics(
        test, test_probabilities, selected_threshold
    )
    vehicle_type_metrics = _slice_metrics(
        test, test_probabilities, selected_threshold, test.vehicle_types
    )

    # Secondary unseen-vehicle audit: same frozen configuration, train-period development
    # vehicles only, calibration-period development vehicles only, test-period holdouts only.
    train_development_vehicle = train.subset(train.vehicle_holdout == "development")
    calibration_development_vehicle = calibration.subset(
        calibration.vehicle_holdout == "development"
    )
    test_holdout_vehicle = test.subset(test.vehicle_holdout == "holdout")
    secondary_pipeline, _ = build_pipeline(
        feature_names, config, selected_config["C"], selected_config["class_weight"]
    )
    secondary_pipeline.fit(train_development_vehicle.X, train_development_vehicle.y)
    secondary_calibrator = make_calibrator(selected_calibration, config["random_seed"])
    secondary_calibrator.fit(
        secondary_pipeline.predict_proba(calibration_development_vehicle.X)[:, 1],
        calibration_development_vehicle.y,
        fit_scope="validation_calibration_development_vehicles_only",
    )
    secondary_probabilities = secondary_calibrator.predict(
        secondary_pipeline.predict_proba(test_holdout_vehicle.X)[:, 1]
    )
    unseen_vehicle_audit = {
        "status": "SECONDARY_GENERALIZATION_AUDIT",
        "protocol": "fit train-period development vehicles; calibrate early-validation development vehicles; evaluate test-period holdout vehicles",
        "identity_used_as_feature": False,
        "row_count": int(len(test_holdout_vehicle.y)),
        **ranking_calibration_metrics(test_holdout_vehicle.y, secondary_probabilities),
        **threshold_metrics(
            test_holdout_vehicle.y, secondary_probabilities, selected_threshold
        ),
    }

    latency_samples = []
    for _ in range(300):
        latency_started = perf_counter()
        reloaded.predict_proba(test.X[:1])
        latency_samples.append((perf_counter() - latency_started) * 1000)
    latency = {
        "batch_size": 1,
        "iterations": len(latency_samples),
        "median_milliseconds": float(np.median(latency_samples)),
        "p95_milliseconds": float(np.percentile(latency_samples, 95)),
        "device": "cpu",
    }

    preprocessing_audit = {
        "status": "PASS",
        "fit_scope": "train_full_only",
        "fit_row_count": int(len(train.y)),
        "fit_anchor_scope_digest": _scope_digest(train.anchor_ids),
        "validation_rows_in_preprocessing_fit": 0,
        "test_rows_in_preprocessing_fit": 0,
        "recency_structural_features": preprocessing.recency_names,
        "recency_sentinel_seconds": config["recency_no_event_sentinel_seconds"],
        "sensor_missing_policy": "training_only_median_plus_missing_indicator",
        "vehicle_type_policy": "training_fitted_one_hot_handle_unknown_ignore",
        "removed_exact_constant_original_features": exact_constants,
        "near_constant_target_filtering_performed": False,
    }
    identity_audit = {
        **validate_feature_names(feature_names),
        "feature_count": len(feature_names),
        "vehicle_type_allowed": True,
        "direct_vehicle_identity_used": False,
    }
    completion_gates = {
        "preprocessing_leakage_audit": preprocessing_audit["status"],
        "identity_privacy_audit": identity_audit["status"],
        "training_reproducible": reproducibility["status"],
        "calibration_evaluated": "PASS",
        "threshold_analysis_complete": "PASS",
        "temporal_test_evaluated_exactly_once": "PASS"
        if test_lock["test_open_count"] == 1
        else "FAIL",
        "artifact_reload": reload_status,
        "prediction_contract": "PASS",
    }
    status = "BASELINE_READY" if all(value == "PASS" for value in completion_gates.values()) else "INVALID"

    report = {
        "status": status,
        "experiment_id": config["experiment_id"],
        "model_version": config["model_version"],
        "dataset_version": config["dataset_version"],
        "dataset_digest": gate["dataset_digest"],
        "feature_spec_version": config["feature_spec_version"],
        "preprocessing_version": config["preprocessing_version"],
        "threshold_version": config["threshold_version"],
        "model_training_scope": "HINO_only_Dummy_and_LogisticRegression",
        "advanced_models_trained": [],
        "external_api_calls": 0,
        "gate": gate,
        "validation_strategy": {
            "october_support": 0,
            "decision": "September whole-trip time subdivision",
            "calibration": {
                "rows": int(len(calibration.y)),
                "positives": int(calibration.y.sum()),
            },
            "threshold_development": {
                "rows": int(len(threshold_dev.y)),
                "positives": int(threshold_dev.y.sum()),
            },
        },
        "dummy_baselines": dummy_results,
        "logistic_candidates": candidates,
        "selected_logistic_config": selected_config,
        "preprocessing_audit": preprocessing_audit,
        "identity_audit": identity_audit,
        "calibration_comparison": calibration_comparison,
        "selected_calibration_method": selected_calibration,
        "threshold_sweep": sweep,
        "candidate_operating_points": operating_points,
        "selected_prototype_threshold": selected_threshold,
        "test_lock_audit": test_lock,
        "frozen_configuration": frozen_configuration,
        "temporal_test": test_metrics,
        "warning_lead_time": lead_time,
        "event_anchor_diagnostics": event_anchor_diagnostics,
        "vehicle_type_performance": vehicle_type_metrics,
        "temporal_stability": temporal_stability,
        "unseen_vehicle_evaluation": unseen_vehicle_audit,
        "coefficient_audit": coefficient_audit,
        "reproducibility": reproducibility,
        "artifact_reload": {
            "status": reload_status,
            "maximum_probability_difference": reload_difference,
        },
        "risk_prediction_contract_example": contract_example,
        "inference_latency": latency,
        "completion_gates": completion_gates,
        "runtime_seconds": perf_counter() - started,
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "numpy": np.__version__,
            "scikit_learn": sklearn.__version__,
            "joblib": joblib.__version__,
            "cpu_count": os.cpu_count(),
        },
        "unresolved": [
            "Production alert threshold and alert budget",
            "Driving-hour denominator approval",
            "Experimental 60-second cooldown approval",
            "Risk-level UI semantics and colors",
            "Official telemetry timezone",
            "Minimum support for event-specific claims",
        ],
    }
    _write_json(staging_report / "evaluation_report.json", report)
    _write_json(staging_report / "threshold_sweep.json", sweep)
    _write_json(staging_report / "preprocessing_leakage_audit.json", preprocessing_audit)
    _write_json(staging_report / "test_lock_audit.json", test_lock)
    _write_json(staging_report / "coefficient_audit.json", coefficient_audit)
    _write_json(staging_report / "calibration_report.json", calibration_comparison)
    _write_json(staging_report / "completion_gates.json", completion_gates)
    _write_json(staging_artifact / "experiment_manifest.json", report)
    _write_json(staging_artifact / "preprocessing_policy.json", preprocessing_audit)

    artifact_root.parent.mkdir(parents=True, exist_ok=True)
    report_root.parent.mkdir(parents=True, exist_ok=True)
    staging_artifact.replace(artifact_root)
    staging_report.replace(report_root)
    artifact_size = sum(path.stat().st_size for path in artifact_root.rglob("*") if path.is_file())
    report["model_artifact_size_bytes"] = artifact_size
    _write_json(report_root / "evaluation_report.json", report)
    _write_json(artifact_root / "experiment_manifest.json", report)
    final_artifact_size = sum(
        path.stat().st_size for path in artifact_root.rglob("*") if path.is_file()
    )
    if final_artifact_size != report["model_artifact_size_bytes"]:
        report["model_artifact_size_bytes"] = final_artifact_size
        _write_json(report_root / "evaluation_report.json", report)
        _write_json(artifact_root / "experiment_manifest.json", report)
    return report
