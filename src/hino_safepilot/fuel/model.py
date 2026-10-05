"""Step 7: XGBoost segment model, validation and selection.

* Target: segment L/100km, sample weight = segment km (so the loss is in litre
  terms and averages agree with total fuel / total km).
* Selection (segment length, feature set, hyper-parameters) uses the last
  training month as validation, with all lookup tables re-fitted on the
  earlier months. The test period (last 3 months) is scored once at the end.
* Two evaluation modes: ``actual`` behaviour/congestion (how well the physics
  is learned) and ``expected`` (what dispatch can know before departure).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
import xgboost as xgb

from .artifacts import FuelArtifacts, fit_artifacts
from .config import FuelConfig
from .maps import explode_grids


def probe_device(preference: str, seed: int) -> dict:
    data = xgb.DMatrix(np.array([[0.0], [1.0], [2.0]], dtype=np.float32), label=np.array([0.0, 1.0, 2.0]))
    try:
        xgb.train({"tree_method": "hist", "device": preference, "seed": seed}, data, 1)
        return {"requested": preference, "selected": preference}
    except Exception as error:  # pragma: no cover - hardware dependent
        if preference == "cpu":
            raise
        return {"requested": preference, "selected": "cpu", "fallback_reason": str(error)[:200]}


def params(candidate: dict, config: FuelConfig, device: str, columns=()) -> dict:
    out = {"objective": "reg:squarederror", "tree_method": "hist", "device": device, "seed": config.model.random_seed, "eval_metric": "mae"}
    for key, value in candidate.items():
        out[key] = int(value) if key in ("max_depth",) else float(value)
    if config.model.monotone_increasing and columns:
        # Physics prior: more idling / harsh driving / overspeed never lowers fuel.
        out["monotone_constraints"] = "(" + ",".join("1" if c in config.model.monotone_increasing else "0" for c in columns) + ")"
    return out


def dmatrix(features: pd.DataFrame, columns, segments: pd.DataFrame | None = None) -> xgb.DMatrix:
    label = segments["L_per_100km"].to_numpy(float) if segments is not None else None
    weight = segments["segment_km"].to_numpy(float) if segments is not None else None
    return xgb.DMatrix(features[list(columns)].to_numpy(np.float32), label=label, weight=weight, feature_names=list(columns), missing=np.nan)


def predict_litres(booster: xgb.Booster, features: pd.DataFrame, columns, segment_km: pd.Series) -> np.ndarray:
    rate = np.clip(booster.predict(dmatrix(features, columns)), 0, None)
    return rate * segment_km.to_numpy(float) / 100


# ------------------------------------------------------------------ metrics
def trip_metrics(segments: pd.DataFrame, predicted_l: np.ndarray, min_trip_km: float = 0.0) -> dict:
    frame = pd.DataFrame({"trip_id": segments["trip_id"].to_numpy(), "km": segments["segment_km"].to_numpy(),
                          "actual": segments["fuel_L"].to_numpy(), "pred": predicted_l})
    trips = frame.groupby("trip_id").sum()
    trips = trips[(trips["km"] >= min_trip_km) & (trips["actual"] > 0)]
    if trips.empty:
        return {"trips": 0}
    ape = (trips["pred"] - trips["actual"]).abs() / trips["actual"]
    return {
        "trips": int(len(trips)),
        "trip_mape_pct": float(ape.mean() * 100),
        "trip_median_ape_pct": float(ape.median() * 100),
        "trip_wape_pct": float((trips["pred"] - trips["actual"]).abs().sum() / trips["actual"].sum() * 100),
        "trip_bias_pct": float((trips["pred"].sum() / trips["actual"].sum() - 1) * 100),
        "segment_mae_l": float(np.abs(frame["pred"] - frame["actual"]).mean()),
    }


def ranking_accuracy(segments: pd.DataFrame, predicted_l: np.ndarray, rng: np.random.Generator, pairs: int = 20000,
                     min_trip_km: float = 10.0, min_gap_pct: float = 10.0) -> dict:
    """Pairwise ranking proxy: for two trips of similar length, does the model pick the
    one that actually used less fuel per km? Pairs whose actual L/100km differ by less
    than ``min_gap_pct`` are skipped (the 0.5 L counter cannot resolve them).

    The spec's "same trip, different combination" is unobservable in history (each
    trip ran once), so this cross-trip comparison is the closest measurable proxy.
    """

    frame = pd.DataFrame({"trip_id": segments["trip_id"].to_numpy(), "km": segments["segment_km"].to_numpy(),
                          "actual": segments["fuel_L"].to_numpy(), "pred": predicted_l})
    trips = frame.groupby("trip_id").sum()
    trips = trips[trips["km"] >= min_trip_km]
    if len(trips) < 2:
        return {"pairs": 0}
    km = trips["km"].to_numpy()
    actual = trips["actual"].to_numpy() / km
    pred = trips["pred"].to_numpy() / km
    i = rng.integers(0, len(trips), pairs)
    j = rng.integers(0, len(trips), pairs)
    similar = (i != j) & (np.abs(np.log(km[i] / km[j])) <= np.log(1.25))
    gap = np.abs(actual[i] - actual[j]) / np.minimum(actual[i], actual[j]) * 100 >= min_gap_pct
    keep = similar & gap
    correct = np.sign(pred[i] - pred[j])[keep] == np.sign(actual[i] - actual[j])[keep]
    return {"pairs": int(keep.sum()), "accuracy": float(correct.mean()) if keep.any() else None}


# ------------------------------------------------------------------ one experiment
@dataclass
class Fitted:
    booster: xgb.Booster
    columns: tuple[str, ...]
    best_iteration: int
    artifacts: FuelArtifacts


def fit_once(train: pd.DataFrame, free_flow: pd.DataFrame, health: pd.Series, vehicle_types: pd.Series, config: FuelConfig,
             columns, candidate: dict, device: str, rounds: int | None = None, valid: pd.DataFrame | None = None) -> Fitted:
    artifacts, lomo = fit_artifacts(train, free_flow, health, vehicle_types, config)
    x_train = artifacts.feature_frame(train, "actual", offsets=lomo)
    evals = []
    dtrain = dmatrix(x_train, columns, train)
    if valid is not None:
        x_valid = artifacts.feature_frame(valid, "actual")
        evals = [(dmatrix(x_valid, columns, valid), "valid")]
    booster = xgb.train(
        params(candidate, config, device, columns), dtrain,
        num_boost_round=rounds or config.model.num_boost_round,
        evals=evals, early_stopping_rounds=config.model.early_stopping_rounds if evals else None, verbose_eval=False,
    )
    best = int(booster.best_iteration) if evals else (rounds or config.model.num_boost_round) - 1
    if evals:
        booster = booster[: best + 1]
    return Fitted(booster, tuple(columns), best, artifacts)


def evaluate(fitted: Fitted, segments: pd.DataFrame, config: FuelConfig, rng: np.random.Generator, driver_type: str | None = None) -> dict:
    grid_rows = explode_grids(segments)
    out = {}
    for mode in ("actual", "expected"):
        features = fitted.artifacts.feature_frame(segments, mode, driver_type=driver_type if mode == "expected" else None, grid_rows=grid_rows)
        litres = predict_litres(fitted.booster, features, fitted.columns, segments["segment_km"])
        out[mode] = {
            "all_trips": trip_metrics(segments, litres),
            "trips_ge_10km": trip_metrics(segments, litres, config.score.min_trip_km),
            "ranking": ranking_accuracy(segments, litres, rng),
        }
    coverage = fitted.artifacts.behaviour_map.coverage(grid_rows)
    out["grid_history_coverage_km_weighted"] = float(np.average(coverage.reindex(segments.index).fillna(0), weights=segments["segment_km"]))
    return out


def trip_ratio_interval(fitted: Fitted, segments: pd.DataFrame, quantiles, min_trip_km: float, driver_type: str | None = None) -> tuple[float, float]:
    """Actual / predicted trip-fuel quantiles (expected mode) -> multiplicative interval."""

    features = fitted.artifacts.feature_frame(segments, "expected", driver_type=driver_type)
    litres = predict_litres(fitted.booster, features, fitted.columns, segments["segment_km"])
    trips = pd.DataFrame({"trip_id": segments["trip_id"].to_numpy(), "km": segments["segment_km"].to_numpy(),
                          "actual": segments["fuel_L"].to_numpy(), "pred": litres}).groupby("trip_id").sum()
    trips = trips[(trips["km"] >= min_trip_km) & (trips["pred"] > 0)]
    ratio = trips["actual"] / trips["pred"]
    return float(ratio.quantile(quantiles[0])), float(ratio.quantile(quantiles[1]))


def interval_coverage(fitted: Fitted, segments: pd.DataFrame, interval: tuple[float, float], min_trip_km: float) -> float:
    features = fitted.artifacts.feature_frame(segments, "expected")
    litres = predict_litres(fitted.booster, features, fitted.columns, segments["segment_km"])
    trips = pd.DataFrame({"trip_id": segments["trip_id"].to_numpy(), "km": segments["segment_km"].to_numpy(),
                          "actual": segments["fuel_L"].to_numpy(), "pred": litres}).groupby("trip_id").sum()
    trips = trips[trips["km"] >= min_trip_km]
    inside = (trips["actual"] >= trips["pred"] * interval[0]) & (trips["actual"] <= trips["pred"] * interval[1])
    return float(inside.mean()) if len(trips) else math.nan


def feature_importance(fitted: Fitted, segments: pd.DataFrame) -> list[dict]:
    features = fitted.artifacts.feature_frame(segments, "actual")
    contributions = fitted.booster.predict(dmatrix(features, fitted.columns), pred_contribs=True)[:, :-1]
    mean_abs = np.abs(contributions).mean(axis=0)
    gain = fitted.booster.get_score(importance_type="total_gain")
    total_gain = sum(gain.values()) or 1.0
    rows = [
        {"feature": name, "mean_abs_shap_l100": float(mean_abs[i]), "shap_share": float(mean_abs[i] / mean_abs.sum()),
         "gain_share": float(gain.get(name, 0.0) / total_gain)}
        for i, name in enumerate(fitted.columns)
    ]
    return sorted(rows, key=lambda r: r["shap_share"], reverse=True)


# ------------------------------------------------------------------ spatial route split
def spatial_blocks(segments: pd.DataFrame, block_km: float, cell_m: float) -> pd.Series:
    """Map each segment to the set of block ids its path touches."""

    factor = max(1, int(round(block_km * 1000 / cell_m)))

    def blocks(grids):
        out = set()
        for g in grids:
            gx, gy = (int(v) for v in g.split("_"))
            out.add(f"{gx // factor}_{gy // factor}")
        return out

    return segments["grid_ids"].map(blocks)


def route_split_validation(segments: pd.DataFrame, free_flow: pd.DataFrame, health: pd.Series, vehicle_types: pd.Series,
                           config: FuelConfig, columns, candidate: dict, rounds: int, device: str, rng: np.random.Generator) -> dict:
    """Hide random 5 km x 5 km blocks: segments touching them are never used to fit the
    model or any lookup table, then they are predicted in expected (dispatch) mode."""

    seg_blocks = spatial_blocks(segments, config.model.route_split_block_km, config.grid.cell_m)
    all_blocks = sorted(set().union(*seg_blocks))
    order = rng.permutation(len(all_blocks))
    folds = config.model.route_split_folds
    results = []
    for fold in range(folds):
        hidden = {all_blocks[i] for i in order[fold::folds]}
        touches = seg_blocks.map(lambda b: bool(b & hidden))
        train, test = segments[~touches], segments[touches]
        if train.empty or test.empty:
            continue
        fitted = fit_once(train.reset_index(drop=True), free_flow, health, vehicle_types, config, columns, candidate, device, rounds=rounds)
        test = test.reset_index(drop=True)
        metrics = evaluate(fitted, test, config, rng)
        results.append({"fold": fold, "train_segments": int(len(train)), "test_segments": int(len(test)), **metrics})
    summary = {}
    for mode in ("actual", "expected"):
        for key in ("trip_mape_pct", "trip_wape_pct"):
            values = [r[mode]["all_trips"][key] for r in results if r[mode]["all_trips"].get("trips")]
            summary[f"{mode}_{key}_mean"] = float(np.mean(values)) if values else None
    summary["grid_history_coverage_mean"] = float(np.mean([r["grid_history_coverage_km_weighted"] for r in results])) if results else None
    return {"folds": results, "summary": summary,
            "note": "Trips are evaluated on their hidden-block segments only; lookup tables are refitted without those blocks, so hidden grids fall back to road-type averages."}
