from __future__ import annotations

from datetime import datetime, timedelta

import joblib
import numpy as np
import pytest
from pydantic import ValidationError
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from hino_safepilot.modeling.calibration import make_calibrator
from hino_safepilot.modeling.contracts import (
    PredictionDataQuality,
    RiskLevel,
    RiskPredictionResult,
)
from hino_safepilot.modeling.evaluation import (
    alert_episode_metrics,
    select_operating_points,
    threshold_sweep,
    warning_lead_time,
)
from hino_safepilot.modeling.preprocessing import (
    RecencyNoEventEncoder,
    build_preprocessor,
    exact_constant_original_features,
    preprocessing_spec,
    validate_feature_names,
)
from hino_safepilot.modeling.training import RiskModelBundle


FEATURES = [
    "sensor_mean_60s",
    "constant_sensor",
    "seconds_since_last_pcs_onset_300s",
    "vehicle_series_code",
]


def synthetic_values():
    return np.asarray(
        [
            [1.0, 9.0, np.nan, 300],
            [2.0, 9.0, 20.0, 500],
            [np.nan, 9.0, np.nan, 700],
            [4.0, 9.0, 80.0, 300],
            [5.0, 9.0, 100.0, 500],
            [6.0, 9.0, np.nan, 700],
        ],
        dtype=float,
    )


def test_train_only_sensor_imputation_and_no_test_leakage() -> None:
    train = synthetic_values()
    test = np.asarray([[1000.0, 9.0, np.nan, 999]], dtype=float)
    preprocessor, _ = build_preprocessor(FEATURES)
    preprocessor.fit(train)
    numeric = preprocessor.named_transformers_["numeric"]
    sensor_position = preprocessing_spec(FEATURES).numeric_names.index("sensor_mean_60s")
    assert numeric.named_steps["imputer"].statistics_[sensor_position] == 4.0
    transformed = preprocessor.transform(test)
    assert transformed.shape[0] == 1
    assert list(preprocessor.named_transformers_["vehicle_type"].categories_[0]) == [300, 500, 700]


def test_recency_no_event_semantic_encoding() -> None:
    encoder = RecencyNoEventEncoder(301.0).fit(np.asarray([[np.nan], [10.0]]))
    result = encoder.transform(np.asarray([[np.nan], [10.0]]))
    assert result.tolist() == [[301.0, 1.0], [10.0, 0.0]]


def test_identity_feature_rejection() -> None:
    assert validate_feature_names(["can_speed_mean_60s"])["status"] == "PASS"
    result = validate_feature_names(["can_speed_mean_60s", "anchor_id", "latitude_deg"])
    assert result["status"] == "FAIL"
    assert result["rejected"] == ["anchor_id", "latitude_deg"]


def test_exact_constant_is_training_fitted_and_near_constant_retained() -> None:
    spec = preprocessing_spec(FEATURES)
    assert exact_constant_original_features(synthetic_values(), spec) == ["constant_sensor"]
    preprocessor, _ = build_preprocessor(FEATURES)
    preprocessor.fit(synthetic_values())
    names = list(preprocessor.get_feature_names_out(FEATURES))
    assert "constant_sensor" not in names
    assert "seconds_since_last_pcs_onset_300s" in names


def test_probability_bounds_and_serialization(tmp_path) -> None:
    values = synthetic_values()
    y = np.asarray([0, 0, 1, 0, 1, 1])
    preprocessor, _ = build_preprocessor(FEATURES)
    pipeline = Pipeline(
        [("preprocessor", preprocessor), ("model", LogisticRegression(random_state=7))]
    ).fit(values, y)
    raw = pipeline.predict_proba(values)[:, 1]
    calibrator = make_calibrator("raw", 7).fit(raw, y, fit_scope="synthetic_train")
    bundle = RiskModelBundle(
        pipeline=pipeline,
        calibrator=calibrator,
        feature_names=FEATURES,
        model_version="synthetic-v1",
        threshold_version="synthetic-threshold-v1",
        selected_threshold=0.5,
        high_recall_threshold=0.3,
    )
    path = tmp_path / "bundle.joblib"
    joblib.dump(bundle, path)
    loaded = joblib.load(path)
    probabilities = loaded.predict_proba(values)
    assert np.all((probabilities >= 0) & (probabilities <= 1))
    assert np.allclose(probabilities, bundle.predict_proba(values))


def test_threshold_sweep_and_operating_points() -> None:
    y = np.asarray([0, 0, 1, 1])
    p = np.asarray([0.1, 0.4, 0.6, 0.9])
    rows = threshold_sweep(y, p, 0.1, 0.9, 0.1)
    assert len(rows) == 9
    selected = select_operating_points(rows, high_recall_minimum=0.9, low_alert_maximum_fpr=0.1)
    assert set(selected) == {"high_recall", "balanced", "low_alert_burden"}
    assert selected["balanced"]["f1"] == 1.0


def test_calibration_fit_scope_is_explicit_and_isolated() -> None:
    p = np.asarray([0.1, 0.2, 0.7, 0.9])
    y = np.asarray([0, 0, 1, 1])
    calibrator = make_calibrator("sigmoid", 7).fit(
        p, y, fit_scope="synthetic_calibration_only"
    )
    assert calibrator.fit_scope_ == "synthetic_calibration_only"
    assert np.all((calibrator.predict(p) >= 0) & (calibrator.predict(p) <= 1))


def test_warning_lead_time_excludes_at_and_after_onset() -> None:
    onset = datetime(2025, 1, 1, 0, 1)
    events = [{"trip_id": "synthetic", "event_start_time": onset, "event_name": "decelerate"}]
    timestamps = [onset - timedelta(seconds=30), onset, onset + timedelta(seconds=10)]
    result = warning_lead_time(
        events,
        [0.8, 0.9, 0.9],
        ["synthetic"] * 3,
        timestamps,
        0.5,
    )
    assert result["detected_events"] == 1
    assert result["median_lead_time_seconds"] == 30.0


def test_event_after_onset_prediction_is_not_detection() -> None:
    onset = datetime(2025, 1, 1, 0, 1)
    events = [{"trip_id": "synthetic", "event_start_time": onset, "event_name": "ldws"}]
    result = warning_lead_time(
        events,
        [0.9],
        ["synthetic"],
        [onset + timedelta(seconds=1)],
        0.5,
    )
    assert result["eligible_events"] == 0
    assert result["detected_events"] == 0


def test_alert_episode_collapse() -> None:
    start = datetime(2025, 1, 1)
    result = alert_episode_metrics(
        [0, 0, 1, 0],
        [0.9, 0.8, 0.9, 0.9],
        ["synthetic"] * 4,
        [start + timedelta(seconds=value) for value in (0, 10, 59, 60)],
        0.5,
        60,
        1.0,
    )
    assert result["total_alert_episodes"] == 2
    assert result["false_alert_episodes"] == 2
    assert result["policy_status"] == "EXPERIMENTAL_ALERT_POLICY"


def test_risk_prediction_result_validation() -> None:
    result = RiskPredictionResult(
        risk_probability=0.7,
        risk_level=RiskLevel.HIGH,
        threshold_version="synthetic-threshold",
        model_version="synthetic-model",
        data_quality_status=PredictionDataQuality.VALID,
    )
    assert result.prediction_horizon_sec == 60
    assert result.predicted_event_type is None
    with pytest.raises(ValidationError):
        RiskPredictionResult(
            risk_probability=1.1,
            risk_level=RiskLevel.HIGH,
            threshold_version="x",
            model_version="x",
            data_quality_status=PredictionDataQuality.VALID,
        )

