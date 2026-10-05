"""XGBoost-native, train-fitted preprocessing without scaling or sensor imputation."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .preprocessing import recency_feature_names, validate_feature_names


@dataclass
class XGBPreprocessor:
    include_vehicle_type: bool = True
    reduced: bool = False
    recency_sentinel_seconds: float = 301.0
    input_features_: list[str] = field(default_factory=list, init=False)
    selected_features_: list[str] = field(default_factory=list, init=False)
    exact_constants_: list[str] = field(default_factory=list, init=False)
    vehicle_categories_: list[float] = field(default_factory=list, init=False)
    output_features_: list[str] = field(default_factory=list, init=False)

    def fit(self, values: np.ndarray, feature_names: list[str]) -> "XGBPreprocessor":
        privacy = validate_feature_names(feature_names)
        if privacy["status"] != "PASS":
            raise ValueError(f"Forbidden model features: {privacy['rejected']}")
        array = np.asarray(values, dtype=float)
        if array.ndim != 2 or array.shape[1] != len(feature_names):
            raise ValueError("Feature matrix and feature names do not align")
        self.input_features_ = list(feature_names)
        constants = []
        for index, name in enumerate(feature_names):
            if name == "vehicle_series_code":
                continue
            observed = array[:, index][~np.isnan(array[:, index])]
            if observed.size and np.unique(observed).size == 1:
                constants.append(name)
        self.exact_constants_ = sorted(constants)
        self.selected_features_ = [
            name
            for name in feature_names
            if (self.include_vehicle_type or name != "vehicle_series_code")
            and (not self.reduced or name not in self.exact_constants_)
        ]
        if self.include_vehicle_type:
            index = feature_names.index("vehicle_series_code")
            self.vehicle_categories_ = sorted(
                float(value) for value in np.unique(array[:, index][~np.isnan(array[:, index])])
            )
        recency = set(recency_feature_names(self.selected_features_))
        output = []
        for name in self.selected_features_:
            if name == "vehicle_series_code":
                output.extend(
                    f"vehicle_series_code_{int(value)}" for value in self.vehicle_categories_
                )
            else:
                output.append(name)
                if name in recency:
                    output.append(f"no_prior_event__{name}")
        self.output_features_ = output
        return self

    def transform(self, values: np.ndarray) -> np.ndarray:
        if not self.input_features_:
            raise RuntimeError("XGBPreprocessor is not fitted")
        array = np.asarray(values, dtype=float)
        positions = {name: index for index, name in enumerate(self.input_features_)}
        recency = set(recency_feature_names(self.selected_features_))
        columns: list[np.ndarray] = []
        for name in self.selected_features_:
            source = array[:, positions[name]]
            if name == "vehicle_series_code":
                columns.extend((source == category).astype(np.float32) for category in self.vehicle_categories_)
            elif name in recency:
                missing = np.isnan(source)
                columns.append(
                    np.where(missing, self.recency_sentinel_seconds, source).astype(np.float32)
                )
                columns.append(missing.astype(np.float32))
            else:
                # XGBoost handles NaN natively; do not learn an imputer outside the model.
                columns.append(source.astype(np.float32))
        return np.column_stack(columns).astype(np.float32, copy=False)

    def fit_transform(self, values: np.ndarray, feature_names: list[str]) -> np.ndarray:
        return self.fit(values, feature_names).transform(values)

    def get_feature_names_out(self) -> list[str]:
        if not self.output_features_:
            raise RuntimeError("XGBPreprocessor is not fitted")
        return list(self.output_features_)

