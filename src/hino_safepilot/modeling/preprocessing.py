"""Leakage-safe preprocessing for HINO-only tabular features."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.compose import ColumnTransformer
from sklearn.feature_selection import VarianceThreshold
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


FORBIDDEN_FEATURE_TOKENS = {
    "enabledcode",
    "carnum",
    "trip_id",
    "anchor_id",
    "source_row",
    "latitude",
    "longitude",
    "vehicle_id",
    "journeycode",
    "post_trip",
    "future_",
}


def validate_feature_names(feature_names: Iterable[str]) -> dict:
    names = list(feature_names)
    rejected = sorted(
        name
        for name in names
        if any(token in name.lower() for token in FORBIDDEN_FEATURE_TOKENS)
    )
    return {"status": "PASS" if not rejected else "FAIL", "rejected": rejected}


def recency_feature_names(feature_names: Iterable[str]) -> list[str]:
    return sorted(
        name
        for name in feature_names
        if name.startswith("seconds_since_last_") and name.endswith("_onset_300s")
    )


class RecencyNoEventEncoder(BaseEstimator, TransformerMixin):
    """Encode null event recency as beyond-window plus an explicit no-event flag."""

    def __init__(self, sentinel_seconds: float = 301.0):
        self.sentinel_seconds = sentinel_seconds

    def fit(self, values, y=None):
        array = np.asarray(values, dtype=float)
        if array.ndim != 2:
            raise ValueError("Recency input must be 2-D")
        self.n_features_in_ = array.shape[1]
        return self

    def transform(self, values):
        array = np.asarray(values, dtype=float)
        missing = np.isnan(array)
        filled = np.where(missing, self.sentinel_seconds, array)
        return np.hstack([filled, missing.astype(float)])

    def get_feature_names_out(self, input_features=None):
        if input_features is None:
            input_features = [f"x{i}" for i in range(self.n_features_in_)]
        names = [str(value) for value in input_features]
        return np.asarray(names + [f"no_prior_event__{name}" for name in names], dtype=object)


@dataclass(frozen=True)
class PreprocessingSpec:
    feature_names: list[str]
    recency_names: list[str]
    numeric_names: list[str]
    categorical_names: list[str]


def preprocessing_spec(feature_names: list[str]) -> PreprocessingSpec:
    privacy = validate_feature_names(feature_names)
    if privacy["status"] != "PASS":
        raise ValueError(f"Forbidden model features: {privacy['rejected']}")
    categorical = ["vehicle_series_code"]
    missing_category = [name for name in categorical if name not in feature_names]
    if missing_category:
        raise ValueError(f"Missing categorical feature: {missing_category}")
    recency = recency_feature_names(feature_names)
    numeric = [name for name in feature_names if name not in set(recency + categorical)]
    return PreprocessingSpec(feature_names, recency, numeric, categorical)


def build_preprocessor(
    feature_names: list[str], sentinel_seconds: float = 301.0
) -> tuple[ColumnTransformer, PreprocessingSpec]:
    spec = preprocessing_spec(feature_names)
    positions = {name: index for index, name in enumerate(feature_names)}
    numeric_indices = [positions[name] for name in spec.numeric_names]
    recency_indices = [positions[name] for name in spec.recency_names]
    categorical_indices = [positions[name] for name in spec.categorical_names]

    numeric = Pipeline(
        [
            ("imputer", SimpleImputer(strategy="median", add_indicator=True)),
            ("constant_filter", VarianceThreshold(threshold=0.0)),
            ("scaler", StandardScaler()),
        ]
    )
    recency = Pipeline(
        [
            ("no_event", RecencyNoEventEncoder(sentinel_seconds)),
            ("scaler", StandardScaler()),
        ]
    )
    categorical = OneHotEncoder(handle_unknown="ignore", sparse_output=True)
    transformer = ColumnTransformer(
        [
            ("numeric", numeric, numeric_indices),
            ("recency", recency, recency_indices),
            ("vehicle_type", categorical, categorical_indices),
        ],
        remainder="drop",
        sparse_threshold=0.3,
        verbose_feature_names_out=False,
    )
    return transformer, spec


def exact_constant_original_features(values: np.ndarray, spec: PreprocessingSpec) -> list[str]:
    """Target-blind report of exact constants in the training partition."""

    positions = {name: index for index, name in enumerate(spec.feature_names)}
    constants: list[str] = []
    for name in spec.numeric_names:
        column = values[:, positions[name]].astype(float)
        observed = column[~np.isnan(column)]
        if observed.size and np.unique(observed).size == 1:
            constants.append(name)
    return sorted(constants)
