from __future__ import annotations

import numpy as np
import joblib
import pytest
import xgboost as xgb

from hino_safepilot.modeling.calibration import make_calibrator
from hino_safepilot.modeling.xgb_preprocessing import XGBPreprocessor
from hino_safepilot.modeling.xgboost_training import XGBRiskModelBundle, probe_xgboost_device


FEATURES = [
    "sensor_mean_60s",
    "constant_sensor",
    "seconds_since_last_pcs_onset_300s",
    "vehicle_series_code",
]


def values():
    return np.asarray([
        [1.0, 9.0, np.nan, 300],
        [2.0, 9.0, 20.0, 500],
        [np.nan, 9.0, 50.0, 700],
        [4.0, 9.0, np.nan, 300],
    ])


def test_xgb_preprocessing_leaves_sensor_nan_and_encodes_recency() -> None:
    processor = XGBPreprocessor().fit(values(), FEATURES)
    transformed = processor.transform(values())
    names = processor.get_feature_names_out()
    assert np.isnan(transformed[2, names.index("sensor_mean_60s")])
    assert transformed[0, names.index("seconds_since_last_pcs_onset_300s")] == 301.0
    assert transformed[0, names.index("no_prior_event__seconds_since_last_pcs_onset_300s")] == 1.0
    assert not any("scaler" in name for name in names)


def test_reduced_removes_only_train_exact_constants_and_type_ablation() -> None:
    reduced = XGBPreprocessor(reduced=True).fit(values(), FEATURES)
    assert "constant_sensor" not in reduced.get_feature_names_out()
    assert "sensor_mean_60s" in reduced.get_feature_names_out()
    no_type = XGBPreprocessor(include_vehicle_type=False).fit(values(), FEATURES)
    assert not any(name.startswith("vehicle_series_code") for name in no_type.get_feature_names_out())


def test_xgboost_cpu_path_trains_with_nan() -> None:
    processor = XGBPreprocessor().fit(values(), FEATURES)
    transformed = processor.transform(values())
    matrix = xgb.DMatrix(transformed, label=np.asarray([0, 0, 1, 1]), feature_names=processor.get_feature_names_out())
    booster = xgb.train({"objective": "binary:logistic", "tree_method": "hist", "device": "cpu", "seed": 7}, matrix, 3)
    predictions = booster.predict(matrix)
    assert np.all((predictions >= 0.0) & (predictions <= 1.0))


def test_device_probe_has_safe_fallback_contract() -> None:
    result = probe_xgboost_device("cpu", 7)
    assert result["status"] == "PASS"
    assert result["selected"] == "cpu"


def test_identity_future_and_post_trip_feature_names_fail_closed() -> None:
    for forbidden in ("vehicle_id", "future_speed_max", "post_trip_event_context"):
        with pytest.raises(ValueError):
            XGBPreprocessor().fit(np.column_stack([values(), np.ones(4)]), [*FEATURES, forbidden])


def test_xgboost_bundle_probability_bounds_reproducibility_and_reload(tmp_path) -> None:
    processor = XGBPreprocessor().fit(values(), FEATURES)
    transformed = processor.transform(values())
    labels = np.asarray([0, 0, 1, 1])
    matrix = xgb.DMatrix(transformed, label=labels, feature_names=processor.get_feature_names_out())
    booster = xgb.train({"objective": "binary:logistic", "tree_method": "hist", "device": "cpu", "seed": 7}, matrix, 3)
    raw = booster.predict(matrix)
    calibrator = make_calibrator("raw", 7).fit(raw, labels, fit_scope="synthetic_only")
    bundle = XGBRiskModelBundle(
        booster=booster,
        preprocessor=processor,
        calibrator=calibrator,
        raw_feature_names=FEATURES,
        model_version="synthetic-xgb",
        calibration_version="synthetic-raw",
        threshold_version="synthetic-policy",
        selected_threshold=0.6,
        elevated_threshold=0.4,
        top_factors=[],
    )
    first = bundle.predict_proba(values())
    second = bundle.predict_proba(values())
    assert np.all((first >= 0.0) & (first <= 1.0))
    assert np.array_equal(first, second)
    path = tmp_path / "xgb_bundle.joblib"
    joblib.dump(bundle, path)
    assert np.array_equal(first, joblib.load(path).predict_proba(values()))
