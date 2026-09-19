"""Isolated probability calibrators fitted only on the calibration period."""

from __future__ import annotations

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression


def _logit(probabilities: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(probabilities, dtype=float), 1e-7, 1 - 1e-7)
    return np.log(clipped / (1 - clipped))


class RawCalibrator:
    method = "raw"

    def fit(self, probabilities, y, *, fit_scope: str):
        self.fit_scope_ = "not_applicable"
        return self

    def predict(self, probabilities):
        return np.clip(np.asarray(probabilities, dtype=float), 0.0, 1.0)


class SigmoidCalibrator:
    method = "sigmoid"

    def __init__(self, random_seed: int):
        self.random_seed = random_seed
        self.model = LogisticRegression(C=1_000_000.0, solver="lbfgs", random_state=random_seed)

    def fit(self, probabilities, y, *, fit_scope: str):
        self.model.fit(_logit(np.asarray(probabilities)).reshape(-1, 1), y)
        self.fit_scope_ = fit_scope
        return self

    def predict(self, probabilities):
        result = self.model.predict_proba(_logit(np.asarray(probabilities)).reshape(-1, 1))[:, 1]
        return np.clip(result, 0.0, 1.0)


class IsotonicCalibrator:
    method = "isotonic"

    def __init__(self):
        self.model = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)

    def fit(self, probabilities, y, *, fit_scope: str):
        self.model.fit(np.asarray(probabilities, dtype=float), y)
        self.fit_scope_ = fit_scope
        return self

    def predict(self, probabilities):
        return np.clip(self.model.predict(np.asarray(probabilities, dtype=float)), 0.0, 1.0)


def make_calibrator(method: str, random_seed: int):
    if method == "raw":
        return RawCalibrator()
    if method == "sigmoid":
        return SigmoidCalibrator(random_seed)
    if method == "isotonic":
        return IsotonicCalibrator()
    raise ValueError(f"Unsupported calibration method: {method}")

