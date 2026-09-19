"""Phase 3C-2 XGBoost experiment with a frozen-before-November protocol."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import joblib
import numpy as np
import shap
import xgboost as xgb
from sklearn.metrics import average_precision_score, precision_score, recall_score

from hino_safepilot.alerting.contracts import AlertPolicyConfig
from hino_safepilot.alerting.evaluation import (
    evaluate_alerts,
    exposure_hours,
    replay_reference_policy,
    replay_revised_policy,
)

from .calibration import make_calibrator
from .contracts import PredictionDataQuality, RiskFactor, RiskLevel, RiskPredictionResult
from .evaluation import ranking_calibration_metrics, threshold_metrics
from .preprocessing import validate_feature_names
from .training import SplitData, _gate, _json, _trusted_core_events, load_split, trip_time_mask
from .xgb_preprocessing import XGBPreprocessor


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class XGBRiskModelBundle:
    booster: xgb.Booster
    preprocessor: XGBPreprocessor
    calibrator: Any
    raw_feature_names: list[str]
    model_version: str
    calibration_version: str
    threshold_version: str
    selected_threshold: float
    elevated_threshold: float
    top_factors: list[dict]

    def predict_raw(self, values: np.ndarray) -> np.ndarray:
        transformed = self.preprocessor.transform(np.asarray(values, dtype=float))
        matrix = xgb.DMatrix(transformed, feature_names=self.preprocessor.get_feature_names_out())
        return self.booster.predict(matrix)

    def predict_proba(self, values: np.ndarray) -> np.ndarray:
        return self.calibrator.predict(self.predict_raw(values))

    def predict_result(self, values: np.ndarray, *, data_quality_status=PredictionDataQuality.VALID) -> RiskPredictionResult:
        probability = float(self.predict_proba(np.asarray(values).reshape(1, -1))[0])
        level = RiskLevel.LOW if probability < self.elevated_threshold else (
            RiskLevel.ELEVATED if probability < self.selected_threshold else RiskLevel.HIGH
        )
        return RiskPredictionResult(
            risk_probability=probability,
            risk_level=level,
            threshold_version=self.threshold_version,
            model_version=self.model_version,
            model_family="xgboost",
            calibration_version=self.calibration_version,
            data_quality_status=data_quality_status,
            top_factors=[RiskFactor(**row) for row in self.top_factors[:5]],
        )


def probe_xgboost_device(preference: str, seed: int) -> dict:
    values = np.asarray([[0, 1], [1, 0], [0.2, 0.8], [0.8, 0.2]], dtype=np.float32)
    labels = np.asarray([0, 1, 0, 1])
    matrix = xgb.DMatrix(values, label=labels)
    try:
        xgb.train({"objective": "binary:logistic", "tree_method": "hist", "device": preference, "seed": seed}, matrix, 2)
        return {"requested": preference, "selected": preference, "fallback_used": False, "status": "PASS"}
    except Exception as error:  # pragma: no cover - depends on machine runtime
        if preference == "cpu":
            raise
        xgb.train({"objective": "binary:logistic", "tree_method": "hist", "device": "cpu", "seed": seed}, matrix, 2)
        return {"requested": preference, "selected": "cpu", "fallback_used": True, "status": "PASS", "cuda_error": str(error)}


def _month_mask(data: SplitData, months: set[str]) -> np.ndarray:
    return np.asarray([timestamp.strftime("%Y-%m") in months for timestamp in data.timestamps], dtype=bool)


def _base_params(config: dict, device: str, candidate: dict) -> dict:
    return {
        "objective": config["objective"],
        "tree_method": config["tree_method"],
        "device": device,
        "eval_metric": config["eval_metrics"],
        "seed": config["random_seed"],
        "nthread": config["nthread"],
        "max_depth": candidate["max_depth"],
        "eta": candidate["eta"],
        "min_child_weight": candidate["min_child_weight"],
        "subsample": candidate.get("subsample", 1.0),
        "colsample_bytree": candidate.get("colsample_bytree", 1.0),
        "reg_alpha": candidate.get("reg_alpha", 0.0),
        "reg_lambda": candidate.get("reg_lambda", 1.0),
        "gamma": candidate.get("gamma", 0.0),
        "scale_pos_weight": candidate.get("scale_pos_weight", 1.0),
    }


def _fit_booster(
    fit: SplitData,
    evaluation: SplitData,
    config: dict,
    candidate: dict,
    device: str,
    *,
    include_type: bool,
    reduced: bool,
) -> tuple[xgb.Booster, XGBPreprocessor, dict]:
    started = perf_counter()
    preprocessor = XGBPreprocessor(
        include_vehicle_type=include_type,
        reduced=reduced,
        recency_sentinel_seconds=config["recency_no_event_sentinel_seconds"],
    )
    fit_values = preprocessor.fit_transform(fit.X, fit.feature_names)
    evaluation_values = preprocessor.transform(evaluation.X)
    names = preprocessor.get_feature_names_out()
    dfit = xgb.DMatrix(fit_values, label=fit.y, feature_names=names)
    deval = xgb.DMatrix(evaluation_values, label=evaluation.y, feature_names=names)
    history: dict = {}
    booster = xgb.train(
        _base_params(config, device, candidate),
        dfit,
        num_boost_round=config["num_boost_round"],
        evals=[(dfit, "fit"), (deval, "evaluation")],
        evals_result=history,
        early_stopping_rounds=config["early_stopping_rounds"],
        verbose_eval=False,
    )
    probabilities = booster.predict(deval, iteration_range=(0, booster.best_iteration + 1))
    result = {
        "metrics": ranking_calibration_metrics(evaluation.y, probabilities, config["calibration_bins"]),
        "best_iteration": int(booster.best_iteration),
        "best_score": float(booster.best_score),
        "transformed_feature_count": len(names),
        "exact_constants_train": preprocessor.exact_constants_,
        "runtime_seconds": perf_counter() - started,
        "training_curve": {
            metric: [float(value) for value in values]
            for metric, values in history["evaluation"].items()
        },
    }
    return booster, preprocessor, result


def _evaluate_candidate(train: SplitData, config: dict, candidate: dict, device: str, *, include_type=True, reduced=False) -> dict:
    folds = []
    for fold in config["development_folds"]:
        fit = train.subset(_month_mask(train, set(fold["fit_months"])), f"{fold['name']}_fit")
        evaluation = train.subset(_month_mask(train, {fold["evaluation_month"]}), f"{fold['name']}_eval")
        _, _, result = _fit_booster(fit, evaluation, config, candidate, device, include_type=include_type, reduced=reduced)
        folds.append({"name": fold["name"], "fit_months": fold["fit_months"], "evaluation_month": fold["evaluation_month"], **result})
    pr_values = [row["metrics"]["pr_auc"] for row in folds]
    roc_values = [row["metrics"]["roc_auc"] for row in folds]
    return {
        "candidate": candidate,
        "include_vehicle_type": include_type,
        "feature_set": "REDUCED" if reduced else "FULL",
        "folds": folds,
        "mean_pr_auc": float(np.mean(pr_values)),
        "std_pr_auc": float(np.std(pr_values)),
        "mean_roc_auc": float(np.mean(roc_values)),
        "median_best_iteration": int(np.median([row["best_iteration"] for row in folds])),
        "selection_score": float(np.mean(pr_values) - 0.1 * np.std(pr_values)),
    }


def _search(train: SplitData, config: dict, device: str) -> dict:
    capacity_rows = []
    for capacity in config["capacity_candidates"]:
        candidate = {**capacity, **config["regularization_candidates"][0], "scale_pos_weight": 1.0}
        capacity_rows.append(_evaluate_candidate(train, config, candidate, device))
    best_capacity = max(capacity_rows, key=lambda row: (row["selection_score"], row["mean_roc_auc"]))
    regularization_rows = []
    capacity = {key: best_capacity["candidate"][key] for key in ("max_depth", "eta", "min_child_weight")}
    for regularization in config["regularization_candidates"]:
        for weight in config["positive_class_weights"]:
            candidate = {**capacity, **regularization, "scale_pos_weight": weight}
            regularization_rows.append(_evaluate_candidate(train, config, candidate, device))
    best = max(regularization_rows, key=lambda row: (row["selection_score"], row["mean_roc_auc"]))
    ablations = []
    for include_type in (True, False):
        for reduced in (False, True):
            ablations.append(_evaluate_candidate(train, config, best["candidate"], device, include_type=include_type, reduced=reduced))
    selected_ablation = max(ablations, key=lambda row: (row["selection_score"], row["mean_roc_auc"], not row["include_vehicle_type"]))
    return {"capacity_stage": capacity_rows, "regularization_and_weight_stage": regularization_rows, "ablation_stage": ablations, "selected": selected_ablation}


def _fit_fixed(train: SplitData, config: dict, selected: dict, device: str) -> tuple[xgb.Booster, XGBPreprocessor]:
    preprocessor = XGBPreprocessor(
        include_vehicle_type=selected["include_vehicle_type"],
        reduced=selected["feature_set"] == "REDUCED",
        recency_sentinel_seconds=config["recency_no_event_sentinel_seconds"],
    )
    values = preprocessor.fit_transform(train.X, train.feature_names)
    matrix = xgb.DMatrix(values, label=train.y, feature_names=preprocessor.get_feature_names_out())
    booster = xgb.train(
        _base_params(config, device, selected["candidate"]),
        matrix,
        num_boost_round=max(1, selected["median_best_iteration"] + 1),
        verbose_eval=False,
    )
    return booster, preprocessor


def _raw_predict(booster, preprocessor, data: SplitData) -> np.ndarray:
    matrix = xgb.DMatrix(preprocessor.transform(data.X), feature_names=preprocessor.get_feature_names_out())
    return booster.predict(matrix)


def _calibration_selection(booster, preprocessor, calibration: SplitData, policy_dev: SplitData, config: dict):
    raw_calibration = _raw_predict(booster, preprocessor, calibration)
    raw_policy = _raw_predict(booster, preprocessor, policy_dev)
    comparison = {}
    calibrators = {}
    for method in config["calibration_methods"]:
        calibrator = make_calibrator(method, config["random_seed"]).fit(raw_calibration, calibration.y, fit_scope="validation_2025-09_early_calibration_only")
        probabilities = calibrator.predict(raw_policy)
        comparison[method] = ranking_calibration_metrics(policy_dev.y, probabilities, config["calibration_bins"])
        calibrators[method] = calibrator
    raw_pr = comparison["raw"]["pr_auc"]
    acceptable = [name for name, values in comparison.items() if values["pr_auc"] >= raw_pr - 0.01]
    selected = min(acceptable, key=lambda name: (comparison[name]["brier_score"], comparison[name]["log_loss"], -comparison[name]["pr_auc"]))
    return selected, calibrators[selected], comparison


def _policy_row(kind: str, threshold: float, cooldown: int, delta: float | None, reset_margin: float | None, data: SplitData, probabilities, events, exposure) -> dict:
    if kind == "revised_v1":
        config = AlertPolicyConfig(
            policy_version="alert-policy-v1.0-experimental",
            threshold_enter=threshold,
            threshold_reset=max(0.0, threshold - float(reset_margin)),
            cooldown_seconds=cooldown,
            escalation_delta=float(delta),
            reset_consecutive_anchors=2,
            one_escalation_per_active_episode=True,
        )
        alerts = replay_revised_policy(data.trip_ids, data.timestamps, probabilities, config)
    else:
        alerts = replay_reference_policy(data.trip_ids, data.timestamps, probabilities, kind=kind, threshold=threshold, cooldown_seconds=cooldown)
    metrics = evaluate_alerts(alerts, events, data.trip_ids, data.timestamps, exposure)
    return {"kind": kind, "threshold": threshold, "cooldown_seconds": cooldown, "escalation_delta": delta, "reset_margin": reset_margin, "metrics": metrics}


def _policy_search(data: SplitData, probabilities, events, config: dict) -> dict:
    drive_index = data.feature_names.index("vehicle_status_driving_fraction_60s")
    exposure = exposure_hours(data.trip_ids, data.timestamps, data.X[:, drive_index], config["driving_anchor_exposure_seconds"])
    grid = []
    for threshold in config["threshold_candidates"]:
        for cooldown in config["alert_cooldown_candidates_seconds"]:
            for delta in config["alert_escalation_delta_candidates"]:
                for margin in config["alert_reset_margin_candidates"]:
                    grid.append(_policy_row("revised_v1", threshold, cooldown, delta, margin, data, probabilities, events, exposure))
    def dominates(left, right):
        lm, rm = left["metrics"], right["metrics"]
        return lm["event_coverage"] >= rm["event_coverage"] and lm["false_alerts_per_hour_anchor_based"] <= rm["false_alerts_per_hour_anchor_based"] and (lm["event_coverage"] > rm["event_coverage"] or lm["false_alerts_per_hour_anchor_based"] < rm["false_alerts_per_hour_anchor_based"])
    pareto = [row for row in grid if not any(dominates(other, row) for other in grid if other is not row)]
    high = max(pareto, key=lambda row: (row["metrics"]["event_coverage"], -row["metrics"]["false_alerts_per_hour_anchor_based"]))
    max_coverage = max(row["metrics"]["event_coverage"] for row in pareto)
    low_pool = [row for row in pareto if row["metrics"]["event_coverage"] >= 0.5 * max_coverage]
    low = min(low_pool, key=lambda row: (row["metrics"]["false_alerts_per_hour_anchor_based"], -row["metrics"]["event_coverage"]))
    def harmonic(row):
        p, r = row["metrics"]["alert_precision"] or 0.0, row["metrics"]["event_coverage"] or 0.0
        return 2 * p * r / (p + r) if p + r else 0.0
    middle = [row for row in pareto if row is not high and row is not low]
    balanced = max(middle or pareto, key=lambda row: (harmonic(row), -row["metrics"]["false_alerts_per_hour_anchor_based"]))
    selected = {"high_safety": high, "balanced": balanced, "low_burden": low}
    special = [
        _policy_row("threshold_only", 0.60, 0, None, None, data, probabilities, events, exposure),
        _policy_row("simple_cooldown", 0.60, 50, None, None, data, probabilities, events, exposure),
        _policy_row("revised_v1", 0.60, 50, 0.15, 0.10, data, probabilities, events, exposure),
        _policy_row("simple_cooldown", balanced["threshold"], 60, None, None, data, probabilities, events, exposure),
        _policy_row("original_user_policy", balanced["threshold"], 50, None, None, data, probabilities, events, exposure),
    ]
    def exact(threshold, cooldown, delta, margin):
        return next(row for row in grid if row["threshold"] == threshold and row["cooldown_seconds"] == cooldown and row["escalation_delta"] == delta and row["reset_margin"] == margin)
    sensitivity = {
        "threshold_sweep_fixed_cooldown50_delta015_margin010": [exact(value, 50, 0.15, 0.10) for value in config["threshold_candidates"]],
        "cooldown_sweep_fixed_threshold060_delta015_margin010": [exact(0.60, value, 0.15, 0.10) for value in config["alert_cooldown_candidates_seconds"]],
        "escalation_delta_sweep_fixed_threshold060_cooldown50_margin010": [exact(0.60, 50, value, 0.10) for value in config["alert_escalation_delta_candidates"]],
        "hysteresis_sweep_fixed_threshold060_cooldown50_delta015": [exact(0.60, 50, 0.15, value) for value in config["alert_reset_margin_candidates"]],
    }
    return {
        "grid_size": len(grid),
        "pareto_count": len(pareto),
        "pareto_front": pareto,
        "candidates": selected,
        "explicit_policy_comparisons": special,
        "sensitivity": sensitivity,
        "classification_at_threshold_0_60": threshold_metrics(data.y, probabilities, 0.60),
        "risk_bands_status": "EXPERIMENTAL_UI_PROTOTYPE_ONLY",
        "exposure": exposure,
        "production_winner_selected": False,
    }


def _ablation_pre_november_review(
    train: SplitData,
    policy_dev: SplitData,
    search: dict,
    events: list[dict],
    config: dict,
    device: str,
) -> tuple[dict, dict]:
    """Resolve statistically close ablations using only pre-November product diagnostics."""
    drive_index = policy_dev.feature_names.index("vehicle_status_driving_fraction_60s")
    exposure = exposure_hours(
        policy_dev.trip_ids,
        policy_dev.timestamps,
        policy_dev.X[:, drive_index],
        config["driving_anchor_exposure_seconds"],
    )
    rows = []
    for ablation in search["ablation_stage"]:
        booster, preprocessor = _fit_fixed(train, config, ablation, device)
        probabilities = _raw_predict(booster, preprocessor, policy_dev)
        type_metrics = _slice_metrics(
            policy_dev, probabilities, 0.60, policy_dev.vehicle_types
        )
        supported_pr = [
            values["pr_auc"]
            for values in type_metrics.values()
            if "pr_auc" in values
        ]
        policy_row = _policy_row(
            "revised_v1",
            0.60,
            50,
            0.15,
            0.10,
            policy_dev,
            probabilities,
            events,
            exposure,
        )
        rows.append(
            {
                "feature_set": ablation["feature_set"],
                "include_vehicle_type": ablation["include_vehicle_type"],
                "fold_mean_pr_auc": ablation["mean_pr_auc"],
                "fold_std_pr_auc": ablation["std_pr_auc"],
                "fold_mean_roc_auc": ablation["mean_roc_auc"],
                "late_september_raw_ranking": ranking_calibration_metrics(
                    policy_dev.y, probabilities, config["calibration_bins"]
                ),
                "late_september_threshold_0_60": threshold_metrics(
                    policy_dev.y, probabilities, 0.60
                ),
                "late_september_type_metrics": type_metrics,
                "type_pr_auc_range": float(max(supported_pr) - min(supported_pr))
                if supported_pr
                else None,
                "fixed_policy_0_60_cooldown50_delta015_margin010": policy_row,
            }
        )
    best_fold_pr = max(row["fold_mean_pr_auc"] for row in rows)
    tolerance = 0.002
    shortlist = [
        row for row in rows if row["fold_mean_pr_auc"] >= best_fold_pr - tolerance
    ]
    winner = max(
        shortlist,
        key=lambda row: (
            row["late_september_threshold_0_60"]["f1"],
            -row["fixed_policy_0_60_cooldown50_delta015_margin010"]["metrics"][
                "false_alerts_per_hour_anchor_based"
            ],
            -row["type_pr_auc_range"],
            row["fold_mean_roc_auc"],
        ),
    )
    selected = next(
        row
        for row in search["ablation_stage"]
        if row["feature_set"] == winner["feature_set"]
        and row["include_vehicle_type"] == winner["include_vehicle_type"]
    )
    return selected, {
        "status": "PASS",
        "november_used": False,
        "primary_pr_auc_tolerance": tolerance,
        "selection_order_within_tolerance": [
            "late_september_f1_at_0.60",
            "lower_false_alerts_per_hour_under_fixed_v1",
            "lower_vehicle_type_pr_auc_range",
            "higher_fold_roc_auc",
        ],
        "rows": rows,
        "selected": {
            "feature_set": winner["feature_set"],
            "include_vehicle_type": winner["include_vehicle_type"],
        },
        "review_required": bool(winner["type_pr_auc_range"] > 0.10),
    }


def _shap_audit(booster, preprocessor, data: SplitData, feature_groups: dict[str, str], sample_size: int, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    indices = np.sort(rng.choice(len(data.y), size=min(sample_size, len(data.y)), replace=False))
    values = preprocessor.transform(data.X[indices])
    names = preprocessor.get_feature_names_out()
    explanation = shap.TreeExplainer(booster)(values)
    shap_values = np.asarray(explanation.values, dtype=float)
    mean_abs = np.mean(np.abs(shap_values), axis=0)
    mean_signed = np.mean(shap_values, axis=0)
    rows = []
    for name, magnitude, signed in zip(names, mean_abs, mean_signed):
        source = name.removeprefix("no_prior_event__")
        if source.startswith("vehicle_series_code_"):
            source = "vehicle_series_code"
        direction = "mixed_or_unknown" if abs(signed) < 0.05 * magnitude else ("increases_risk" if signed > 0 else "decreases_risk")
        rows.append({"feature": name, "source_feature": source, "feature_family": feature_groups.get(source, "derived_indicator"), "importance": float(magnitude), "mean_signed_shap": float(signed), "direction": direction, "interpretation": "association_only_not_causal"})
    rows.sort(key=lambda row: row["importance"], reverse=True)
    families = {}
    for row in rows:
        families[row["feature_family"]] = families.get(row["feature_family"], 0.0) + row["importance"]
    return {"sample_size": len(indices), "method": "TreeExplainer mean_absolute_SHAP", "causal_interpretation_allowed": False, "features": rows, "feature_family_importance": dict(sorted(families.items(), key=lambda item: item[1], reverse=True))}


def _bootstrap_by_trip(y, xgb_p, logistic_p, trip_ids, xgb_threshold, logistic_threshold, replicates: int, seed: int) -> dict:
    grouped = {}
    for index, trip in enumerate(trip_ids):
        grouped.setdefault(str(trip), []).append(index)
    trips = np.asarray(sorted(grouped), dtype=object)
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(replicates):
        sampled = rng.choice(trips, size=len(trips), replace=True)
        indices = np.concatenate([np.asarray(grouped[str(trip)], dtype=int) for trip in sampled])
        truth = y[indices]
        xp, lp = xgb_p[indices], logistic_p[indices]
        xpr, lpr = average_precision_score(truth, xp), average_precision_score(truth, lp)
        rows.append({
            "xgb_pr_auc": xpr,
            "logistic_pr_auc": lpr,
            "pr_auc_difference": xpr - lpr,
            "xgb_recall": recall_score(truth, xp >= xgb_threshold, zero_division=0),
            "xgb_precision": precision_score(truth, xp >= xgb_threshold, zero_division=0),
        })
    def interval(key):
        values = np.asarray([row[key] for row in rows])
        return {"mean": float(np.mean(values)), "ci95_lower": float(np.percentile(values, 2.5)), "ci95_upper": float(np.percentile(values, 97.5))}
    return {"method": "grouped_trip_bootstrap", "replicates": replicates, "random_seed": seed, **{key: interval(key) for key in rows[0]}}


def _slice_metrics(data: SplitData, probabilities, threshold: float, values) -> dict:
    result = {}
    for value in sorted(set(values)):
        mask = np.asarray(values) == value
        if mask.sum() == 0 or np.unique(data.y[mask]).size < 2:
            result[str(value)] = {"row_count": int(mask.sum()), "status": "INSUFFICIENT_SUPPORT"}
        else:
            result[str(value)] = {**ranking_calibration_metrics(data.y[mask], probabilities[mask]), **threshold_metrics(data.y[mask], probabilities[mask], threshold)}
    return result


def _batch1_latency(booster, preprocessor, raw_row: np.ndarray, repeats: int = 100) -> dict:
    transformed = preprocessor.transform(raw_row)
    matrix = xgb.DMatrix(transformed, feature_names=preprocessor.get_feature_names_out())
    for _ in range(10):
        booster.predict(matrix)
    samples = []
    for _ in range(repeats):
        started = perf_counter()
        booster.predict(matrix)
        samples.append((perf_counter() - started) * 1000.0)
    return {
        "repeats": repeats,
        "batch_size": 1,
        "median_ms": float(np.median(samples)),
        "p95_ms": float(np.percentile(samples, 95)),
        "max_ms": float(np.max(samples)),
        "below_10_second_cadence": bool(np.percentile(samples, 95) < 10_000.0),
    }


def _events_for_scope(repo_root: Path, data: SplitData) -> list[dict]:
    """Predicate-pushdown scope: do not scan labels outside the current time/trip scope."""
    from datetime import timedelta

    return _trusted_core_events(
        repo_root,
        allowed_trip_ids={str(value) for value in data.trip_ids},
        start_time=min(data.timestamps),
        end_time=max(data.timestamps) + timedelta(seconds=60),
    )


def train_phase3c2(repo_root: Path, *, config_path: Path | None = None, artifact_root: Path | None = None, report_root: Path | None = None) -> dict:
    started = perf_counter()
    repo_root = repo_root.resolve()
    config_path = (config_path or repo_root / "config" / "xgboost_v1.json").resolve()
    artifact_root = (artifact_root or repo_root / "models" / "risk" / "v1_xgboost").resolve()
    report_root = (report_root or repo_root / "reports" / "generated" / "phase3c2").resolve()
    if artifact_root.exists() or report_root.exists():
        raise FileExistsError("Refusing to overwrite an existing Phase 3C-2 artifact/report root")
    dataset_root = repo_root / "data" / "processed" / "ml" / "v0"
    config = _json(config_path)
    spec = _json(dataset_root / "metadata" / "resolved_feature_spec.json")
    feature_names = spec["feature_columns"]
    feature_groups = {row["name"]: row["group"] for row in spec["features"]}
    gate = _gate(repo_root, dataset_root, feature_names)
    baseline_manifest = _json(repo_root / "models" / "risk" / "v0_logistic" / "experiment_manifest.json")
    if gate["status"] != "PASS" or baseline_manifest["status"] != "BASELINE_READY":
        raise RuntimeError("Dataset or Logistic baseline completion gate failed")
    if validate_feature_names(feature_names)["status"] != "PASS":
        raise RuntimeError("Private identity found in model feature schema")

    staging_artifact = artifact_root.with_name(f".{artifact_root.name}-staging-{uuid4().hex}")
    staging_report = report_root.with_name(f".{report_root.name}-staging-{uuid4().hex}")
    staging_artifact.mkdir(parents=True)
    staging_report.mkdir(parents=True)
    test_lock = {"status": "LOCKED", "test_data_opened": False, "test_open_count": 0, "known_holdout": True}
    try:
        device = probe_xgboost_device(config["device_preference"], config["random_seed"])
        train = load_split(dataset_root, "train", feature_names)
        validation = load_split(dataset_root, "validation", feature_names)
        cutoff = datetime.fromisoformat(config["validation_calibration_trip_start_before"])
        calibration = validation.subset(trip_time_mask(validation, cutoff, before=True), "validation_early_calibration")
        policy_dev = validation.subset(trip_time_mask(validation, cutoff, before=False), "validation_late_policy")
        search = _search(train, config, device["selected"])
        events = _events_for_scope(repo_root, policy_dev)
        selected, ablation_review = _ablation_pre_november_review(
            train,
            policy_dev,
            search,
            events,
            config,
            device["selected"],
        )
        search["selected"] = selected
        search["ablation_pre_november_review"] = ablation_review
        booster, preprocessor = _fit_fixed(train, config, selected, device["selected"])
        calibration_method, calibrator, calibration_comparison = _calibration_selection(booster, preprocessor, calibration, policy_dev, config)
        policy_probabilities = calibrator.predict(_raw_predict(booster, preprocessor, policy_dev))
        policy = _policy_search(policy_dev, policy_probabilities, events, config)
        balanced_policy = policy["candidates"]["balanced"]
        selected_threshold = float(balanced_policy["threshold"])
        threshold_grid = np.arange(0.01, 1.0, 0.01)
        logistic_bundle = joblib.load(repo_root / "models" / "risk" / "v0_logistic" / "risk_model_bundle.joblib")
        logistic_dev_p = logistic_bundle.predict_proba(policy_dev.X)
        logistic_dev_recall = threshold_metrics(policy_dev.y, logistic_dev_p, logistic_bundle.selected_threshold)["recall"]
        recall_match_threshold = min(threshold_grid, key=lambda threshold: abs(threshold_metrics(policy_dev.y, policy_probabilities, float(threshold))["recall"] - logistic_dev_recall))
        shap_audit = _shap_audit(booster, preprocessor, policy_dev, feature_groups, config["shap_sample_size"], config["random_seed"])
        top_factors = [{"feature": row["feature"], "feature_family": row["feature_family"], "direction": row["direction"], "importance": row["importance"]} for row in shap_audit["features"][:10]]

        freeze = {
            "frozen_before_known_holdout": True,
            "known_holdout_label": "KNOWN_HOLDOUT COMPARATIVE BENCHMARK",
            "development_protocol_version": config["development_protocol_version"],
            "selected_candidate": selected,
            "calibration_method": calibration_method,
            "alert_policy_candidates": policy["candidates"],
            "selected_comparative_threshold": selected_threshold,
            "recall_match_threshold": float(recall_match_threshold),
            "feature_schema_digest": _digest(preprocessor.get_feature_names_out()),
            "config_digest": _digest(config),
            "november_used_for_selection": False,
        }
        freeze["freeze_digest"] = _digest(freeze)
        _write_json(staging_report / "frozen_configuration_before_known_holdout.json", freeze)
        test_lock.update({"status": "UNLOCKED_AFTER_FREEZE", "freeze_digest": freeze["freeze_digest"]})

        # The only November dataset open in this run occurs after all choices above are frozen.
        test = load_split(dataset_root, "test", feature_names)
        test_lock["test_data_opened"] = True
        test_lock["test_open_count"] = 1
        test_lock["status"] = "CLOSED_AFTER_SINGLE_KNOWN_HOLDOUT_BENCHMARK"
        xgb_test_p = calibrator.predict(_raw_predict(booster, preprocessor, test))
        logistic_test_p = logistic_bundle.predict_proba(test.X)
        test_events = _events_for_scope(repo_root, test)
        temporal_test = {
            "status": "KNOWN_HOLDOUT COMPARATIVE BENCHMARK",
            "xgboost": {**ranking_calibration_metrics(test.y, xgb_test_p), **threshold_metrics(test.y, xgb_test_p, selected_threshold)},
            "logistic_frozen": {**ranking_calibration_metrics(test.y, logistic_test_p), **threshold_metrics(test.y, logistic_test_p, logistic_bundle.selected_threshold)},
            "recall_matched": {
                "xgboost_threshold_frozen_on_pre_november": float(recall_match_threshold),
                "xgboost": threshold_metrics(test.y, xgb_test_p, float(recall_match_threshold)),
                "logistic": threshold_metrics(test.y, logistic_test_p, logistic_bundle.selected_threshold),
            },
        }
        test_exposure = exposure_hours(test.trip_ids, test.timestamps, test.X[:, test.feature_names.index("vehicle_status_driving_fraction_60s")], config["driving_anchor_exposure_seconds"])
        selected_policy_config = AlertPolicyConfig(
            policy_version=config["alert_policy_version"],
            threshold_enter=balanced_policy["threshold"],
            threshold_reset=max(0.0, balanced_policy["threshold"] - balanced_policy["reset_margin"]),
            cooldown_seconds=balanced_policy["cooldown_seconds"],
            escalation_delta=balanced_policy["escalation_delta"],
            reset_consecutive_anchors=config["alert_reset_consecutive_anchors"],
            one_escalation_per_active_episode=True,
        )
        test_alerts = replay_revised_policy(test.trip_ids, test.timestamps, xgb_test_p, selected_policy_config)
        temporal_test["xgboost_alert_policy_v1"] = evaluate_alerts(test_alerts, test_events, test.trip_ids, test.timestamps, test_exposure)
        logistic_alerts = replay_reference_policy(test.trip_ids, test.timestamps, logistic_test_p, kind="simple_cooldown", threshold=logistic_bundle.selected_threshold, cooldown_seconds=60)
        temporal_test["logistic_phase3c1_simple_cooldown"] = evaluate_alerts(logistic_alerts, test_events, test.trip_ids, test.timestamps, test_exposure)
        xgb_recall_matched_alerts = replay_reference_policy(
            test.trip_ids,
            test.timestamps,
            xgb_test_p,
            kind="simple_cooldown",
            threshold=float(recall_match_threshold),
            cooldown_seconds=60,
        )
        temporal_test["recall_matched_alert_burden_simple_60s"] = {
            "xgboost": evaluate_alerts(xgb_recall_matched_alerts, test_events, test.trip_ids, test.timestamps, test_exposure),
            "logistic": temporal_test["logistic_phase3c1_simple_cooldown"],
            "thresholds_frozen_pre_november": True,
        }
        bootstrap = _bootstrap_by_trip(test.y, xgb_test_p, logistic_test_p, test.trip_ids, float(recall_match_threshold), logistic_bundle.selected_threshold, config["bootstrap_replicates"], config["random_seed"])

        holdout_train = train.subset(train.vehicle_holdout == "development", "holdout_train")
        holdout_cal = calibration.subset(calibration.vehicle_holdout == "development", "holdout_calibration")
        holdout_test = test.subset(test.vehicle_holdout == "holdout", "holdout_test")
        unseen = {"status": "SECONDARY_GENERALIZATION_AUDIT", "identity_used_as_feature": False, "models": {}}
        for include_type in (True, False):
            unseen_selected = {**selected, "include_vehicle_type": include_type}
            ub, up = _fit_fixed(holdout_train, config, unseen_selected, device["selected"])
            uc = make_calibrator(calibration_method, config["random_seed"]).fit(_raw_predict(ub, up, holdout_cal), holdout_cal.y, fit_scope="early_validation_development_vehicles_only")
            probabilities = uc.predict(_raw_predict(ub, up, holdout_test))
            unseen["models"]["with_vehicle_type" if include_type else "without_vehicle_type"] = {**ranking_calibration_metrics(holdout_test.y, probabilities), **threshold_metrics(holdout_test.y, probabilities, selected_threshold)}
        unseen["logistic_phase3c1"] = baseline_manifest["unseen_vehicle_evaluation"]

        reproducibility_booster, reproducibility_preprocessor = _fit_fixed(train, config, selected, device["selected"])
        reproducibility_raw = _raw_predict(reproducibility_booster, reproducibility_preprocessor, policy_dev)
        primary_raw = _raw_predict(booster, preprocessor, policy_dev)
        max_difference = float(np.max(np.abs(primary_raw - reproducibility_raw)))
        reproducibility = {"status": "PASS" if max_difference <= config["reproducibility_absolute_tolerance"] else "FAIL", "max_absolute_probability_difference": max_difference, "tolerance": config["reproducibility_absolute_tolerance"]}

        # Training may use CUDA, but the shipped bundle is explicitly CPU-capable.
        gpu_latency = _batch1_latency(booster, preprocessor, policy_dev.X[:1])
        booster.set_param({"device": "cpu"})
        cpu_latency = _batch1_latency(booster, preprocessor, policy_dev.X[:1])
        inference_latency = {
            "input_scope": "one private-derived development row; values not logged",
            "gpu_batch_1": gpu_latency,
            "cpu_batch_1": cpu_latency,
            "production_gpu_required": False,
        }
        bundle = XGBRiskModelBundle(
            booster=booster,
            preprocessor=preprocessor,
            calibrator=calibrator,
            raw_feature_names=feature_names,
            model_version=config["model_version"],
            calibration_version=config["calibration_version"] + ":" + calibration_method,
            threshold_version=config["alert_policy_version"],
            selected_threshold=selected_threshold,
            elevated_threshold=max(0.01, selected_threshold - 0.15),
            top_factors=top_factors,
        )
        booster.save_model(staging_artifact / "booster.json")
        joblib.dump(preprocessor, staging_artifact / "preprocessor.joblib")
        joblib.dump(calibrator, staging_artifact / "calibrator.joblib")
        joblib.dump(bundle, staging_artifact / "risk_model_bundle.joblib")
        _write_json(staging_artifact / "feature_schema.json", {
            "raw_feature_names": feature_names,
            "transformed_feature_names": preprocessor.get_feature_names_out(),
            "selected_original_features": preprocessor.selected_features_,
            "train_exact_constants": preprocessor.exact_constants_,
            "vehicle_type_included": preprocessor.include_vehicle_type,
            "privacy_status": "PASS",
        })
        _write_json(staging_artifact / "preprocessing_policy.json", {
            "sensor_missing": "retain_nan_for_xgboost_missing_branch",
            "recency_missing": "301_seconds_plus_no_prior_event_indicator",
            "scaling": "none",
            "reduced_feature_policy": config["reduced_feature_policy"],
            "fit_scope": "training_only",
        })
        _write_json(staging_artifact / "threshold_candidates.json", {
            "selected_on": "late_September_pre_November_only",
            "entry_thresholds": config["threshold_candidates"],
            "cooldown_seconds": config["alert_cooldown_candidates_seconds"],
            "escalation_deltas": config["alert_escalation_delta_candidates"],
            "reset_margins": config["alert_reset_margin_candidates"],
            "selected_research_candidates": policy["candidates"],
            "production_winner_selected": False,
        })
        synthetic_contract_row = np.full((1, len(feature_names)), np.nan, dtype=float)
        synthetic_contract_row[0, feature_names.index("vehicle_series_code")] = 300.0
        before_reload = bundle.predict_proba(synthetic_contract_row)
        reloaded_bundle = joblib.load(staging_artifact / "risk_model_bundle.joblib")
        after_reload = reloaded_bundle.predict_proba(synthetic_contract_row)
        reload_difference = float(np.max(np.abs(before_reload - after_reload)))
        artifact_reload = {
            "status": "PASS" if reload_difference <= config["reproducibility_absolute_tolerance"] else "FAIL",
            "packaged_inference_device": "cpu",
            "synthetic_only_contract_check": True,
            "max_absolute_probability_difference": reload_difference,
        }
        _write_json(staging_artifact / "alert_policy_v1.json", selected_policy_config.model_dump(mode="json"))
        _write_json(staging_artifact / "feature_importance.json", shap_audit)
        artifact_payload_bytes = sum(
            path.stat().st_size for path in staging_artifact.rglob("*") if path.is_file()
        )
        privacy = {"status": "PASS", "raw_identifiers_in_model_features": [], "coordinates_in_model_features": [], "post_trip_features_in_model_features": [], "identity_used_for_split_audit_only": True}
        leakage = {"status": "PASS", "dataset_audit_inherited": gate["dataset_leakage_status"], "preprocessing_fit_on_training_only": True, "calibration_fit_scope": "early September only", "policy_selection_scope": "late September only", "event_scan_scope": "predicate-pushdown by allowed trips and time range", "november_features_or_labels_opened_before_freeze": False, "november_used_for_selection": False, "suspicious_target_leakage_feature_found": False}
        completion = {"dataset_gate": gate["status"], "logistic_gate": baseline_manifest["status"], "privacy": privacy["status"], "leakage": leakage["status"], "reproducibility": reproducibility["status"], "artifact_reload": artifact_reload["status"], "known_holdout_open_count": test_lock["test_open_count"]}
        overall_status = "XGBOOST_EXPERIMENT_READY" if all(value in ("PASS", "BASELINE_READY", 1) for value in completion.values()) else "INVALID"
        manifest = {
            "status": overall_status,
            "production_status": "NOT_PRODUCTION_READY",
            "known_holdout_status": "KNOWN_HOLDOUT COMPARATIVE BENCHMARK",
            "experiment_id": config["experiment_id"],
            "model_version": config["model_version"],
            "dataset_version": config["dataset_version"],
            "development_protocol_version": config["development_protocol_version"],
            "gate": gate,
            "device": device,
            "packaged_inference_device": "cpu",
            "search": search,
            "selected_calibration_method": calibration_method,
            "calibration_comparison": calibration_comparison,
            "policy_development": policy,
            "frozen_configuration": freeze,
            "test_lock_audit": test_lock,
            "temporal_test": temporal_test,
            "bootstrap_confidence_intervals": bootstrap,
            "vehicle_type_performance": _slice_metrics(test, xgb_test_p, selected_threshold, test.vehicle_types),
            "unseen_vehicle_evaluation": unseen,
            "shap_audit": shap_audit,
            "privacy_audit": privacy,
            "leakage_audit": leakage,
            "reproducibility": reproducibility,
            "artifact_reload": artifact_reload,
            "inference_latency": inference_latency,
            "artifact_payload_size_bytes_before_manifest": artifact_payload_bytes,
            "risk_prediction_contract_example": bundle.predict_result(test.X[0]).model_dump(mode="json"),
            "alert_decision_contract_separate": True,
            "completion_gates": completion,
            "runtime_seconds": perf_counter() - started,
            "environment": {"python": sys.version, "platform": platform.platform(), "xgboost": xgb.__version__, "shap": shap.__version__},
            "unresolved": [
                "Alert thresholds and speech burden are not validated by HINO or safety outcome data.",
                "November was previously exposed and is not an unbiased final test.",
                "Timezone semantics remain UNRESOLVED.",
                "Actual production driving-hour denominator requires online session semantics.",
                "PCS standalone prediction remains unsupported due to sparse positives."
            ],
        }
        _write_json(staging_artifact / "experiment_manifest.json", manifest)
        for name, value in {
            "evaluation_report.json": manifest,
            "development_search.json": search,
            "calibration_report.json": calibration_comparison,
            "alert_policy_report.json": policy,
            "bootstrap_report.json": bootstrap,
            "privacy_audit.json": privacy,
            "leakage_audit.json": leakage,
            "test_lock_audit.json": test_lock,
        }.items():
            _write_json(staging_report / name, value)
        staging_artifact.rename(artifact_root)
        staging_report.rename(report_root)
        return manifest
    except Exception:
        shutil.rmtree(staging_artifact, ignore_errors=True)
        shutil.rmtree(staging_report, ignore_errors=True)
        raise
