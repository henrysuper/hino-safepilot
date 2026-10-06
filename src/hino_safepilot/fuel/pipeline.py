"""Stage orchestration. Derived data -> data/processed/fuel, reports -> reports/fuel."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .checks import behaviour_by_vehicle_band, fuel_level_cross_check, fuel_level_table, osm_vs_speed_limit, per_vehicle_table, segment_summary
from .config import FuelConfig, load_config
from .geo import MoiDem, RoadIndex, build_road_index
from .segments import GeoContext, build_segments, load_onsets, load_telemetry, time_split

LOGGER = logging.getLogger("hino_safepilot.fuel")


@dataclass(frozen=True)
class FuelPaths:
    repo_root: Path
    input_root: Path
    output_root: Path
    report_dir: Path
    config_path: Path

    @classmethod
    def resolve(
        cls,
        repo_root: Path,
        input_root: Path = Path("data/interim"),
        output_root: Path = Path("data/processed/fuel"),
        report_dir: Path = Path("reports/fuel"),
        config_path: Path = Path("config/fuel_v1.json"),
    ) -> "FuelPaths":
        repo = Path(repo_root).resolve()

        def absolute(path: Path) -> Path:
            return path.resolve() if path.is_absolute() else (repo / path).resolve()

        paths = cls(repo, absolute(input_root), absolute(output_root), absolute(report_dir), absolute(config_path))
        processed = (repo / "data" / "processed").resolve()
        if not paths.output_root.is_relative_to(processed) or paths.output_root == processed:
            raise ValueError("Fuel derived datasets must stay below data/processed/")
        for protected in (repo / "data" / "raw", repo / "data" / "reference"):
            if paths.report_dir.is_relative_to(protected.resolve()):
                raise ValueError("Refusing to write into immutable raw/reference data")
        return paths

    def config(self) -> FuelConfig:
        return load_config(self.config_path)

    def under_repo(self, relative: str) -> Path:
        return (self.repo_root / relative).resolve()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _log(event: str, **fields: Any) -> None:
    LOGGER.info(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))


def geo_context(paths: FuelPaths, config: FuelConfig) -> GeoContext:
    index_path = paths.under_repo(config.osm.index_path)
    if not index_path.is_file():
        pbf = paths.under_repo(config.osm.pbf_path)
        if not pbf.is_file():
            raise FileNotFoundError(f"OSM extract missing: {config.osm.pbf_path} (download Geofabrik taiwan-latest.osm.pbf)")
        _log("osm_index_build", **build_road_index(pbf, index_path, config.osm.densify_spacing_m))
    tif = paths.under_repo(config.dem.tif_path)
    if not tif.is_file():
        raise FileNotFoundError(f"MOI 20 m DEM missing: {config.dem.tif_path}")
    return GeoContext(
        roads=RoadIndex.load(index_path, config.osm.class_to_band),
        dem=MoiDem(tif, paths.under_repo(config.dem.tfw_path), config.dem.nodata),
        max_match_distance_m=config.osm.max_match_distance_m,
        cell_m=config.grid.cell_m,
        spacing_m=config.profile.spacing_m,
        smooth_points=config.profile.smooth_points,
    )


def vehicle_aliases(codes) -> dict[str, str]:
    return {code: f"V{i + 1:02d}" for i, code in enumerate(sorted(set(codes)))}


def run_segments(paths: FuelPaths, review_codes: tuple[str, ...] = ()) -> dict:
    """Step 1: build segment tables for each candidate length and the checkpoint-1 report."""

    config = paths.config()
    lengths_km = tuple(config.segment.candidate_lengths_km)
    started = time.perf_counter()
    telemetry = load_telemetry(paths.input_root)
    onsets = load_onsets(paths.input_root)
    geo = geo_context(paths, config)
    paths.output_root.mkdir(parents=True, exist_ok=True)
    aliases = vehicle_aliases(telemetry["enabled_code"].dropna())
    tables = {}
    for length in lengths_km:
        segments = build_segments(telemetry, onsets, config, geo, length)
        segments.to_parquet(paths.output_root / f"segments_{int(length)}km.parquet", index=False)
        tables[length] = segments
        _log("segments_built", length_km=length, rows=len(segments))
    main = tables[config.segment.length_km]
    test_start = time_split(main["start_time"], config.train_months)
    vehicle_type = telemetry.groupby("enabled_code")["vehicle_series_code"].agg(lambda s: s.mode().iloc[0]).to_dict()
    vehicles = per_vehicle_table(main, aliases)
    review = []
    for code in review_codes:
        alias = aliases.get(code)
        review.append({
            "vehicle": alias,
            "segments_table_row": next((v for v in vehicles if v["vehicle"] == alias), None),
            "all_segments_incl_out_of_range": _review_counts(main, code),
        })
    report = {
        "step": 1,
        "segment_length_km": config.segment.length_km,
        "summary": segment_summary(main, test_start),
        "other_lengths": {f"{int(k)}km": {"segments_after_l100_filter": int(v["l100_in_range"].sum())} for k, v in tables.items()},
        "osm_vs_speed_limit": osm_vs_speed_limit(main),
        "fuel_level_by_osm_band": fuel_level_table(main, "road_band"),
        "fuel_level_by_speed_limit_band": fuel_level_table(main, "speed_limit_band"),
        "per_vehicle": vehicles,
        "fuel_level_cross_check": fuel_level_cross_check(telemetry, aliases, vehicle_type),
        "city_behaviour_by_vehicle": behaviour_by_vehicle_band(main, aliases, "city"),
        "review_vehicles": review,
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }
    write_json(paths.report_dir / "checkpoint1_segments.json", report)
    # Private alias map stays with the private derived data (git-ignored).
    write_json(paths.output_root / "vehicle_aliases.json", aliases)
    return report


def _review_counts(segments: pd.DataFrame, code: str) -> dict:
    rows = segments[segments["enabled_code"] == code]
    return {
        "segments": int(len(rows)),
        "out_of_l100_range": int((~rows["l100_in_range"]).sum()),
        "trips": int(rows["trip_id"].nunique()),
        "km": round(float(rows["segment_km"].sum()), 1),
        "months": sorted(rows["start_time"].dt.strftime("%Y-%m").unique().tolist()),
        "l100_quantiles": {str(q): round(float(rows["L_per_100km"].quantile(q)), 1) for q in (0.1, 0.5, 0.9)} if len(rows) else {},
        "mean_idle_min_per_km": round(float(rows["idle_min_per_km"].mean()), 3) if len(rows) else None,
        "road_band_km": rows.groupby("road_band")["segment_km"].sum().round(1).to_dict(),
    }


# ---------------------------------------------------------------- steps 2-7
def _period_starts(segments: pd.DataFrame, config: FuelConfig) -> tuple[pd.Timestamp, pd.Timestamp]:
    test_start = time_split(segments["start_time"], config.train_months)
    train_months = segments.loc[segments["start_time"] < test_start, "start_time"].dt.to_period("M")
    validation_start = (train_months.max() - (config.validation_months - 1)).to_timestamp()
    return validation_start, test_start


def run_train(paths: FuelPaths) -> dict:
    """Steps 2-7: health, lookup tables, model selection, final fit and validation."""

    import xgboost as xgb

    from .artifacts import FEATURES_EXTENDED, FEATURES_SPEC, prepare_segments
    from .maps import free_flow_speeds
    from .model import (
        evaluate, feature_importance, fit_once, interval_coverage, probe_device, route_split_validation, trip_ratio_interval,
    )
    from .vehicles import HEALTH_COMPONENTS, health_indicators, health_scores, load_health_events, load_health_telemetry

    config = paths.config()
    started = time.perf_counter()
    rng = np.random.default_rng(config.model.random_seed)
    device = probe_device(config.model.device_preference, config.model.random_seed)["selected"]
    aliases = json.loads((paths.output_root / "vehicle_aliases.json").read_text(encoding="utf-8"))
    tables = {
        length: prepare_segments(pd.read_parquet(paths.output_root / f"segments_{int(length)}km.parquet"))
        for length in config.segment.candidate_lengths_km
    }
    reference = tables[config.segment.length_km]
    validation_start, test_start = _period_starts(reference, config)

    # Step 2: health from non-fuel signals, training period only.
    health_tel = load_health_telemetry(paths.input_root, test_start)
    indicators = health_indicators(health_tel, load_health_events(paths.input_root, test_start))
    scored = health_scores(indicators)
    health = scored["health_score"]
    vehicle_types = scored["vehicle_type"]

    telemetry = load_telemetry(paths.input_root)
    m = config.maps

    def free_flow(before: pd.Timestamp) -> pd.DataFrame:
        return free_flow_speeds(telemetry[telemetry["timestamp"] < before], config.grid.cell_m, m.free_flow_quantile,
                                m.free_flow_min_records, config.segment.moving_min_speed_kph)

    ff_fit, ff_train = free_flow(validation_start), free_flow(test_start)

    # Selection on the validation month (all tables refitted on earlier months).
    search = []
    best = None
    for length, segments in tables.items():
        fit_part = segments[segments["start_time"] < validation_start].reset_index(drop=True)
        valid_part = segments[(segments["start_time"] >= validation_start) & (segments["start_time"] < test_start)].reset_index(drop=True)
        for feature_set, columns in (("spec", FEATURES_SPEC), ("spec+road_shares", FEATURES_EXTENDED)):
            for candidate in config.model.candidates:
                fitted = fit_once(fit_part, ff_fit, health, vehicle_types, config, columns, candidate, device, valid=valid_part)
                metrics = evaluate(fitted, valid_part, config, rng)
                row = {
                    "segment_km": length, "feature_set": feature_set, "candidate": candidate, "best_iteration": fitted.best_iteration,
                    "valid_expected_trip_mape_ge10": metrics["expected"]["trips_ge_10km"].get("trip_mape_pct"),
                    "valid_expected_trip_wape_ge10": metrics["expected"]["trips_ge_10km"].get("trip_wape_pct"),
                    "valid_actual_trip_mape_ge10": metrics["actual"]["trips_ge_10km"].get("trip_mape_pct"),
                    "valid_trips_ge10": metrics["expected"]["trips_ge_10km"].get("trips"),
                }
                search.append(row)
                _log("selection", **{k: row[k] for k in ("segment_km", "feature_set", "best_iteration", "valid_expected_trip_mape_ge10", "valid_actual_trip_mape_ge10")})
                if best is None or row["valid_expected_trip_mape_ge10"] < best[0]["valid_expected_trip_mape_ge10"]:
                    best = (row, fitted, valid_part, columns)
    choice, selection_fit, selection_valid, columns = best
    interval = trip_ratio_interval(selection_fit, selection_valid, config.model.interval_quantiles, config.score.min_trip_km)

    # Final fit on the whole training period.
    length = choice["segment_km"]
    segments = tables[length]
    train = segments[segments["start_time"] < test_start].reset_index(drop=True)
    test = segments[segments["start_time"] >= test_start].reset_index(drop=True)
    rounds = max(50, int((choice["best_iteration"] + 1) * 1.1))
    final = fit_once(train, ff_train, health, vehicle_types, config, columns, choice["candidate"], device, rounds=rounds)
    test_metrics = evaluate(final, test, config, rng)
    test_normal = evaluate(final, test, config, rng, driver_type="normal")["expected"]
    by_type = {}
    for vehicle_type, part in test.groupby("vehicle_type"):
        by_type[str(int(vehicle_type))] = evaluate(final, part.reset_index(drop=True), config, rng)["expected"]["trips_ge_10km"]
    route_split = route_split_validation(segments, ff_train, health, vehicle_types, config, columns, choice["candidate"], rounds, device, rng)

    # Persist: model (rules only) + private lookup tables.
    model_dir = paths.under_repo("models/fuel/v1")
    model_dir.mkdir(parents=True, exist_ok=True)
    final.booster.save_model(model_dir / "fuel_model.json")
    artifact_dir = paths.output_root / "artifacts"
    final.artifacts.save(artifact_dir)
    meta = {
        "model_version": "v1.0",
        "segment_km": length,
        "features": list(columns),
        "candidate": choice["candidate"],
        "num_boost_round": rounds,
        "prediction_interval_ratio": list(interval),
        "interval_quantiles": list(config.model.interval_quantiles),
        "xgboost_version": xgb.__version__,
        "train_period": [str(segments["start_time"].min().date()), str((test_start - pd.Timedelta(days=1)).date())],
        "test_start": test_start.isoformat(),
        "config_version": config.version,
    }
    write_json(model_dir / "model_meta.json", meta)
    write_json(artifact_dir / "model_meta.json", meta)
    # The virtual driver roster is built by `hino-fuel drivers` (drivers.py), which runs after this stage.
    if final.artifacts.driver_trips is not None:
        final.artifacts.driver_trips.reset_index().to_parquet(artifact_dir / "driver_type_trips.parquet", index=False)

    # Reports (aggregates only, vehicles by alias).
    health_report = scored.copy()
    health_report.insert(0, "vehicle", health_report.index.map(aliases))
    health_rows = json.loads(health_report.reset_index(drop=True).round(3).to_json(orient="records", force_ascii=False))
    write_json(paths.report_dir / "checkpoint2_health.json", {
        "components": HEALTH_COMPONENTS,
        "method": "sub-score = 100 - 25 x (deviation above same-type median / fleet robust scale), capped 0-100; equal-weight mean",
        "train_period_end": test_start.isoformat(),
        "vehicles": sorted(health_rows, key=lambda r: (r["vehicle_type"], r["vehicle"])),
        "spread": {"min": float(health.min()), "median": float(health.median()), "max": float(health.max())},
    })
    offsets = final.artifacts.vehicle_offsets
    write_json(paths.report_dir / "vehicle_efficiency_offsets.json", sorted(
        [{"vehicle": aliases[c], "vehicle_type": int(vehicle_types[c]), "offset": round(float(v), 3)} for c, v in offsets.items()],
        key=lambda r: (r["vehicle_type"], r["vehicle"]),
    ))
    report = {
        "step": 7,
        "device": device,
        "periods": {"validation_start": validation_start.isoformat(), "test_start": test_start.isoformat()},
        "selection": {"criterion": "validation month, expected (dispatch) mode, trip MAPE on trips >= 10 km", "chosen": choice, "search": search},
        "segment_length_comparison": _length_comparison(search),
        "prediction_interval_ratio": interval,
        "test": {
            "segments": int(len(test)),
            "time_split": test_metrics,
            "expected_mode_with_normal_driver_type": test_normal,
            "expected_mode_by_vehicle_type_ge10km": by_type,
            "interval_coverage_ge10km": interval_coverage(final, test, interval, config.score.min_trip_km),
            "interval_nominal": config.model.interval_quantiles[1] - config.model.interval_quantiles[0],
        },
        "route_split": route_split,
        "feature_importance": feature_importance(final, test),
        "driver_type_multipliers": final.artifacts.driver_types.multipliers.reset_index().round(3).to_dict(orient="records"),
        "driver_type_edges": final.artifacts.driver_types.edges,
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }
    write_json(paths.report_dir / "checkpoint3_model.json", report)
    return report


def _length_comparison(search: list[dict]) -> list[dict]:
    rows = []
    for length in sorted({r["segment_km"] for r in search}):
        candidates = [r for r in search if r["segment_km"] == length]
        best = min(candidates, key=lambda r: r["valid_expected_trip_mape_ge10"])
        rows.append({"segment_km": length, "best_valid_expected_trip_mape_ge10": best["valid_expected_trip_mape_ge10"],
                     "best_valid_actual_trip_mape_ge10": best["valid_actual_trip_mape_ge10"], "feature_set": best["feature_set"]})
    return rows


# ---------------------------------------------------------------- section 6
def _load_model(paths: FuelPaths):
    import xgboost as xgb

    from .artifacts import FuelArtifacts

    model_dir = paths.under_repo("models/fuel/v1")
    booster = xgb.Booster()
    booster.load_model(model_dir / "fuel_model.json")
    meta = json.loads((model_dir / "model_meta.json").read_text(encoding="utf-8"))
    return booster, meta, FuelArtifacts.load(paths.output_root / "artifacts")


def run_score(paths: FuelPaths, examples: int = 4) -> dict:
    """Score test-period trips (>= 10 km) and write example narrated reports."""

    from .artifacts import prepare_segments
    from .narration import Narrator, template_trip_report
    from .scoring import period_scores, score_trips, trip_report_facts

    from .routing import load_env_file

    load_env_file(paths.repo_root / ".env")
    config = paths.config()
    booster, meta, artifacts = _load_model(paths)
    aliases = json.loads((paths.output_root / "vehicle_aliases.json").read_text(encoding="utf-8"))
    segments = prepare_segments(pd.read_parquet(paths.output_root / f"segments_{int(meta['segment_km'])}km.parquet"))
    test = segments[segments["start_time"] >= pd.Timestamp(meta["test_start"])]
    trips, seg = score_trips(test, artifacts, booster, meta["features"], config)
    trips.to_parquet(paths.output_root / "trip_scores_test.parquet", index=False)
    scored = trips[trips["scored"]]
    vehicles = period_scores(trips)
    vehicles.index = vehicles.index.map(aliases)
    narrator = Narrator()
    picks = scored.sort_values("fuel_score")
    chosen = pd.concat([picks.head(examples // 2), picks.tail(examples - examples // 2)])
    reports = []
    for _, trip in chosen.iterrows():
        facts = trip_report_facts(trip, seg[seg["trip_id"] == trip["trip_id"]], float(artifacts.health.get(trip["enabled_code"], np.nan)), config)
        facts["vehicle"] = aliases[trip["enabled_code"]]
        narration = narrator.trip_report(facts)
        reports.append({"facts": facts, "text": narration.text, "generation_mode": narration.generation_mode,
                        "template_text": template_trip_report(facts), "llm_error": narration.error})
    summary = {
        "scored_trips": int(len(scored)),
        "score_formula": f"{config.score.base} + (1 - actual/expected) x {config.score.scale}, clipped 0-100; trips >= {config.score.min_trip_km} km",
        "fuel_ratio_quantiles": {str(q): round(float(scored["fuel_ratio"].quantile(q)), 3) for q in (0.1, 0.25, 0.5, 0.75, 0.9)},
        "score_quantiles": {str(q): round(float(scored["fuel_score"].quantile(q)), 1) for q in (0.1, 0.25, 0.5, 0.75, 0.9)},
        "mean_score_by_type": scored.groupby("vehicle_type")["fuel_score"].mean().round(1).to_dict(),
        "score_vs_city_share_spearman": round(float(scored["fuel_score"].corr(scored["city_share"], method="spearman")), 3),
        "score_vs_slope_spearman": round(float(scored["fuel_score"].corr(scored["mean_slope"], method="spearman")), 3),
        "vehicle_period_scores": vehicles.round(3).reset_index(names="vehicle").to_dict(orient="records"),
        "examples": reports,
    }
    write_json(paths.report_dir / "fuel_scores_test.json", summary)
    return summary


# ---------------------------------------------------------------- section 5 demo
DEMO_TASKS = (
    # Public example locations only (no trip data leaves the machine).
    {"name": "桃園→台中（說明書範例）", "origin": [121.3010, 24.9937], "destination": [120.6736, 24.1477]},
    {"name": "台北車站→基隆港", "origin": [121.5170, 25.0478], "destination": [121.7419, 25.1316]},
    {"name": "台中車站→埔里", "origin": [120.6867, 24.1372], "destination": [120.9690, 23.9640]},
)


def run_dispatch_demo(paths: FuelPaths, depart_time: str = "2026-10-05T08:00:00+08:00", use_live_traffic: bool = False,
                      out_name: str | None = None) -> dict:
    """Run predict_trip_fuel on public demo tasks with one vehicle per type (mock assignment)."""

    from .fuel_predictor import FuelPredictor

    predictor = FuelPredictor.from_repo(paths.repo_root, with_live_traffic=use_live_traffic)
    aliases = json.loads((paths.output_root / "vehicle_aliases.json").read_text(encoding="utf-8"))
    offsets = predictor.artifacts.vehicle_offsets
    types = predictor.artifacts.vehicle_types
    # One typical vehicle per type: the one whose efficiency offset is closest to 1.
    vehicles = [(offsets[types == t] - 1).abs().idxmin() for t in sorted(types.unique())]
    results = []
    for task in DEMO_TASKS:
        result = predictor.predict_trip_fuel(task["origin"], task["destination"], depart_time, vehicles, ["eco", "normal", "aggressive"], use_live_traffic)
        for route in result["routes"]:
            for option in route["options"]:
                option["vehicle_id"] = aliases[option["vehicle_id"]]  # report aliases, not enabledCodes
                option.pop("segment_fuel_L", None)
        result["lowest_fuel_option"]["vehicle_id"] = aliases[result["lowest_fuel_option"]["vehicle_id"]]
        results.append({"task": task["name"], "result": result})
    payload = {"depart_time": depart_time, "use_live_traffic": use_live_traffic, "mock_label": "任務起訖點為示範資料", "tasks": results}
    write_json(paths.report_dir / (out_name or ("dispatch_demo_live.json" if use_live_traffic else "dispatch_demo.json")), payload)
    return payload


# ---------------------------------------------------------------- real-trip back-test
def run_backtest(paths: FuelPaths, n_trips: int = 60) -> dict:
    """Real-trip back-test of the full dispatch pipeline (user approved sending O-D pairs to ORS).

    1. Calibration on *training-period* real trips: terminal overhead per type
       (fuel/time outside driving segments) and a route factor that maps ORS-route
       predictions to whole-trip fuel.
    2. Evaluation on *test-period* real trips, before and after calibration.
    """

    from .artifacts import prepare_segments
    from .backtest import CachedOrs, backtest, fit_route_factors, select_trips, summarize, terminal_overhead, trip_endpoints
    from .fuel_predictor import FuelPredictor

    config = paths.config()
    raw = FuelPredictor.from_repo(paths.repo_root, with_live_traffic=False, calibrated=False)
    aliases = json.loads((paths.output_root / "vehicle_aliases.json").read_text(encoding="utf-8"))
    telemetry = load_telemetry(paths.input_root)
    test_start = pd.Timestamp(raw.meta["test_start"])
    known = set(raw.artifacts.vehicle_types.index)
    ors = CachedOrs(raw.ors, paths.output_root / "backtest_ors_cache.json")

    # 1. calibration (training period only)
    train_trips = trip_endpoints(telemetry, telemetry["timestamp"].min(), test_start)
    segments = prepare_segments(pd.read_parquet(paths.output_root / f"segments_{int(raw.meta['segment_km'])}km.parquet"))
    overhead = terminal_overhead(train_trips, segments[segments["start_time"] < test_start], config.score.min_trip_km)
    cal_trips, cal_funnel = select_trips(train_trips, known, n_trips, config.model.random_seed + 1, config.score.min_trip_km)
    cal_table, _ = backtest(raw, cal_trips, ors, aliases)
    factors = fit_route_factors(cal_table, overhead)
    calibration = {
        **factors,
        "overhead_L_by_type": {str(int(k)): round(float(v), 3) for k, v in overhead["overhead_L_per_trip"].items()} if factors["use_fuel_overhead"] else {},
        "overhead_min_by_type": {str(int(k)): round(float(v), 1) for k, v in overhead["overhead_min_per_trip"].items()} if factors["use_time_overhead"] else {},
        "fitted_on": "training-period real trips (before test_start)",
    }
    write_json(paths.output_root / "artifacts" / "dispatch_calibration.json", calibration)

    # 2. evaluation (test period)
    test_trips = trip_endpoints(telemetry, test_start)
    chosen, funnel = select_trips(test_trips, known, n_trips, config.model.random_seed, config.score.min_trip_km)
    raw_table, _ = backtest(raw, chosen, ors, aliases)
    calibrated = FuelPredictor.from_repo(paths.repo_root, with_live_traffic=False)
    table, details = backtest(calibrated, chosen, ors, aliases)
    table.to_parquet(paths.output_root / "backtest_trips.parquet", index=False)
    write_json(paths.output_root / "backtest_details.json", details)  # route geometry: private, stays in data/processed
    summary = {
        "funnel": funnel, "calibration_funnel": cal_funnel, "ors_live_calls": ors.live_calls,
        "calibration": calibration,
        "terminal_overhead_by_type": overhead.round(3).reset_index().to_dict(orient="records"),
        "metrics": summarize(table), "metrics_uncalibrated": summarize(raw_table),
        "errors": table["error"].dropna().tolist() if "error" in table else [],
        "trips": table.drop(columns=[c for c in ("error",) if c in table]).round(3).to_dict(orient="records"),
        "method": "real O-D -> ORS driving-hgv -> FuelPredictor (historical congestion at the real departure, normal driver type) vs real whole-trip fuel",
    }
    write_json(paths.report_dir / "backtest_real_trips.json", summary)
    summary["route_overlap"] = attach_route_overlap(paths)
    return summary


def attach_route_overlap(paths: FuelPaths) -> dict:
    """Add 'did the driver follow the ORS route?' analysis to the back-test report."""

    from .backtest import error_by_overlap, overlap_table

    table = pd.read_parquet(paths.output_root / "backtest_trips.parquet")
    cache = json.loads((paths.output_root / "backtest_ors_cache.json").read_text(encoding="utf-8"))
    trips = table[table["route_choice"] == "ors_first"][["trip_id"]].drop_duplicates()
    telemetry = load_telemetry(paths.input_root)
    overlap = overlap_table(telemetry[telemetry["trip_id"].isin(trips["trip_id"])], trips, cache)
    overlap.to_parquet(paths.output_root / "backtest_overlap.parquet", index=False)
    report_path = paths.report_dir / "backtest_real_trips.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["route_overlap"] = {
        "method": "share of the ORS route within 200 m of the real GPS track, and share of the track within 200 m of the route; group = min of both",
        "by_group": error_by_overlap(table, overlap),
        "median_route_followed_share": round(float(overlap["route_followed_share"].median()), 2),
        "median_track_on_route_share": round(float(overlap["track_on_route_share"].median()), 2),
    }
    overlap_by_trip = overlap.set_index("trip_id")
    for row in report["trips"]:
        if row["trip_id"] in overlap_by_trip.index:
            row["route_overlap"] = round(float(min(overlap_by_trip.loc[row["trip_id"]])), 2)
    write_json(report_path, report)
    return report["route_overlap"]


# ---------------------------------------------------------------- fences (圍籬)
def run_fences(paths: FuelPaths, name_top: int = 40) -> dict:
    """Derive fences from training-period trip endpoints; name the busiest ones by reverse geocoding."""

    from .backtest import trip_endpoints
    from .fences import FenceStore, Geocoder, build_habitual_routes, derive_fences, endpoints_from_trips
    from .routing import RoutingError, load_env_file

    load_env_file(paths.repo_root / ".env")
    config = paths.config()
    telemetry = load_telemetry(paths.input_root)
    test_start = time_split(telemetry["timestamp"], config.train_months)
    trips = trip_endpoints(telemetry, telemetry["timestamp"].min(), test_start)
    fences = derive_fences(endpoints_from_trips(trips))
    geocoder = Geocoder(paths.output_root / "geocode_cache.json")
    named = 0
    for fence in fences[:name_top]:
        try:
            label = geocoder.reverse(fence.lon, fence.lat)
        except RoutingError:
            label = None
        if label:
            fence.name = f"{fence.fence_id}｜{label}"
            named += 1
    FenceStore(paths.output_root).save_derived(fences)
    habitual = build_habitual_routes(trips, telemetry[telemetry["timestamp"] < test_start], fences)
    write_json(paths.output_root / "habitual_routes.json", habitual)  # real tracks: private
    from .backtest import stop_idle_model

    write_json(paths.output_root / "artifacts" / "stop_idle.json", stop_idle_model(telemetry[telemetry["timestamp"] < test_start]))
    return {"fences": len(fences), "named": named, "visits_covered": int(sum(f.visits for f in fences)), "endpoints": int(2 * len(trips)),
            "habitual_routes": len(habitual)}


def run_fence_test(paths: FuelPaths, max_pairs: int = 20, max_trips_per_pair: int = 8, min_trips: int = 2) -> dict:
    """Fence-to-fence dispatch test on real test-period trips.

    For each fence pair with >= ``min_trips`` real trips (>= 10 km), ORS plans one route
    between the fence centres (one call per pair, cached); every real trip of the pair is
    predicted on that route with its own vehicle and departure time and compared with its
    real fuel. Each trip's GPS track is compared with the planned route (overlap).
    """

    from .backtest import CachedOrs, route_overlap, trip_endpoints
    from .fences import FenceStore, assign_fence, habitual_candidate
    from .fuel_predictor import FuelPredictor

    config = paths.config()
    predictor = FuelPredictor.from_repo(paths.repo_root, with_live_traffic=False)
    aliases = json.loads((paths.output_root / "vehicle_aliases.json").read_text(encoding="utf-8"))
    fences = FenceStore(paths.output_root).all()
    if not fences:
        raise FileNotFoundError("No fences yet: run `hino-fuel fences` first")
    by_id = {f.fence_id: f for f in fences}
    telemetry = load_telemetry(paths.input_root)
    trips = trip_endpoints(telemetry, pd.Timestamp(predictor.meta["test_start"]))
    trips = trips[(trips["km"] >= config.score.min_trip_km) & trips["l_per_100km"].between(2, 120) & trips["enabled_code"].isin(predictor.artifacts.vehicle_types.index)]
    trips = trips.assign(o_fence=assign_fence(trips["o_lon"], trips["o_lat"], fences), d_fence=assign_fence(trips["d_lon"], trips["d_lat"], fences))
    paired = trips[(trips["o_fence"] != "") & (trips["d_fence"] != "") & (trips["o_fence"] != trips["d_fence"])]
    counts = paired.groupby(["o_fence", "d_fence"]).size().sort_values(ascending=False)
    pairs = counts[counts >= min_trips].head(max_pairs)
    ors = CachedOrs(predictor.ors, paths.output_root / "backtest_ors_cache.json")
    habitual_path = paths.output_root / "habitual_routes.json"
    habitual_routes = json.loads(habitual_path.read_text(encoding="utf-8")) if habitual_path.is_file() else {}
    rows, pair_rows = [], []
    for (o_id, d_id), _ in pairs.items():
        o, d = by_id[o_id], by_id[d_id]
        routes = ors.routes(f"fence:{o_id}->{d_id}", [o.lon, o.lat], [d.lon, d.lat])
        route = routes[0]
        habitual = habitual_candidate(habitual_routes, o_id, d_id)
        group = paired[(paired["o_fence"] == o_id) & (paired["d_fence"] == d_id)].sort_values("start_time").head(max_trips_per_pair)
        for trip in group.itertuples():
            candidates = [route] + ([habitual] if habitual is not None else [])
            result = predictor.predict_trip_fuel([o.lon, o.lat], [d.lon, d.lat], pd.Timestamp(trip.start_time).isoformat(), [trip.enabled_code], ["normal"], False, routes=candidates)
            option = result["routes"][0]["options"][0]
            habitual_l = result["routes"][1]["options"][0]["predicted_fuel_L"] if habitual is not None else None
            track = telemetry[telemetry["trip_id"] == trip.trip_id].sort_values("timestamp")
            track = track[np.isfinite(track["longitude_deg"]) & (track["longitude_deg"] > 100)]
            overlap = route_overlap(track[["longitude_deg", "latitude_deg"]].to_numpy(float), route.lonlat)
            rows.append({
                "pair": f"{o_id}→{d_id}", "vehicle": aliases[trip.enabled_code], "vehicle_type": int(trip.vehicle_type),
                "date": pd.Timestamp(trip.start_time).strftime("%Y-%m-%d %H:%M"), "actual_km": round(float(trip.km), 1),
                "route_km": round(route.distance_km, 1), "actual_fuel_L": round(float(trip.fuel_L), 2),
                "predicted_fuel_L": option["predicted_fuel_L"], "interval_L": option["prediction_interval_L"],
                "error_pct": round((option["predicted_fuel_L"] / trip.fuel_L - 1) * 100, 1),
                "route_overlap": round(min(overlap.values()), 2),
                "single_task": bool(trip.km <= 1.3 * route.distance_km),  # not a multi-stop round between the fences
                "habitual_fuel_L": habitual_l,
                "habitual_error_pct": round((habitual_l / trip.fuel_L - 1) * 100, 1) if habitual_l is not None else None,
            })
        part = [r for r in rows if r["pair"] == f"{o_id}→{d_id}"]
        actual = sum(r["actual_fuel_L"] for r in part)
        predicted = sum(r["predicted_fuel_L"] for r in part)
        pair_rows.append({
            "pair": f"{o_id}→{d_id}", "from": o.name, "to": d.name, "trips": len(part), "route_km": round(route.distance_km, 1),
            "actual_km_mean": round(float(np.mean([r["actual_km"] for r in part])), 1),
            "actual_fuel_L_mean": round(actual / len(part), 2), "predicted_fuel_L_mean": round(predicted / len(part), 2),
            "pair_error_pct": round((predicted / actual - 1) * 100, 1), "mean_route_overlap": round(float(np.mean([r["route_overlap"] for r in part])), 2),
            "geometry": [[round(float(a), 5), round(float(b), 5)] for a, b in route.lonlat[:: max(1, len(route.lonlat) // 300)]],
            "segments": result["routes"][0]["segments"],
        })
    table = pd.DataFrame(rows)

    def mape(frame):
        return round(float((frame["predicted_fuel_L"] / frame["actual_fuel_L"] - 1).abs().mean() * 100), 1) if len(frame) else None

    followed = table[table["route_overlap"] >= 0.8] if len(table) else table
    single = table[table["single_task"]] if len(table) else table
    with_habit = table.dropna(subset=["habitual_fuel_L"]) if len(table) else table
    summary = {
        "trip_mape_single_task_pct": mape(single), "trips_single_task": int(len(single)),
        "habitual_route": {
            "trips": int(len(with_habit)),
            "ors_route_mape_pct": mape(with_habit),
            "habitual_route_mape_pct": round(float((with_habit["habitual_fuel_L"] / with_habit["actual_fuel_L"] - 1).abs().mean() * 100), 1) if len(with_habit) else None,
            "note": "habitual route = median-length real GPS track between the same two fences in the training period",
        },
        "fences": len(fences), "test_trips_ge10km": int(len(trips)), "trips_fence_to_fence": int(len(paired)),
        "pairs_tested": len(pair_rows), "trips_tested": int(len(table)), "ors_live_calls": ors.live_calls,
        "trip_mape_pct": mape(table), "trip_mape_followed_route_pct": mape(followed), "trips_followed_route": int(len(followed)),
        "pair_mape_pct": round(float(np.mean([abs(p["pair_error_pct"]) for p in pair_rows])), 1) if pair_rows else None,
        "interval_coverage": round(float(np.mean([r["interval_L"][0] <= r["actual_fuel_L"] <= r["interval_L"][1] for r in rows])), 3) if rows else None,
        "pairs": pair_rows, "trips": rows,
    }
    write_json(paths.output_root / "fence_test.json", summary)  # contains fence names/geometry: private
    write_json(paths.report_dir / "fence_test_summary.json", {k: v for k, v in summary.items() if k not in ("pairs", "trips")})
    return summary


def run_build_graph(paths: FuelPaths) -> dict:
    """Build the quota-free local routing graph from the OSM extract."""

    from .local_router import build_graph

    config = paths.config()
    return build_graph(paths.under_repo(config.osm.pbf_path), paths.under_repo("data/osm/road_graph_v1.npz"))



# ---------------------------------------------------------------- virtual drivers
def model_dir_for(paths: FuelPaths, version: str | None = None) -> Path:
    """The fuel model (v1). The model never sees driver identity; drivers act through expected behaviour."""

    return paths.under_repo(f"models/fuel/{version or 'v1'}")


def _history_bin(history: pd.Series) -> pd.Series:
    return pd.cut(history, [-1, 0, 5, 20, np.inf], labels=["0", "1-5", "6-20", "21+"]).astype(str)


def run_drivers(paths: FuelPaths) -> dict:
    """Virtual drivers (mock roster, real behaviour): assignment, personal averages per road type,
    evaluation, new-driver curve and a replay. No model training: the v1 model is reused."""

    from .artifacts import prepare_segments
    from .drivers import (
        DRIVER_MULTS, MOCK_NOTE, apply_profile, assign_drivers, attach_drivers, build_roster, composite, daily_profile, daily_profiles,
        mock_plates, profiles_from_trips, segment_multipliers, trip_totals, vehicle_days,
    )
    from .maps import explode_grids
    from .model import predict_litres, trip_metrics

    config = paths.config()
    dc = config.drivers
    started = time.perf_counter()
    booster, meta, artifacts = _load_model(paths)
    columns = meta["features"]
    aliases = json.loads((paths.output_root / "vehicle_aliases.json").read_text(encoding="utf-8"))
    segments = prepare_segments(pd.read_parquet(paths.output_root / f"segments_{int(meta['segment_km'])}km.parquet"))
    _, test_start = _period_starts(segments, config)
    # Typical behaviour for each segment's roads and time (prior x 500 m grid factor), the dispatch baseline.
    rates = artifacts.expected_behaviour(segments, explode_grids(segments), None)

    # 1-2. Roster and vehicle-day assignment (此為模擬資料).
    vehicles_by_type: dict[int, list[str]] = {}
    for code, vehicle_type in artifacts.vehicle_types.items():
        vehicles_by_type.setdefault(int(vehicle_type), []).append(aliases[code])
    roster = build_roster(dc, vehicles_by_type, config.mock.label)
    days = vehicle_days(segments, rates, config.driver_types.ratio_cap)
    assignment = assign_drivers(days, roster, test_start, dc, aliases)
    segments["driver_id"] = attach_drivers(segments, assignment)
    unassigned = int(segments["driver_id"].isna().sum())
    keep = segments["driver_id"].notna().to_numpy()
    segments, rates = segments[keep].reset_index(drop=True), rates[keep].reset_index(drop=True)

    # 3. Personal averages per road type, from each driver's earlier days only.
    rows = trip_totals(segments, rates)
    daily = daily_profiles(rows, roster, dc)
    segments = segments.join(segment_multipliers(segments, daily))

    # 4. Test-period evaluation (expected = dispatch mode), same v1 model throughout.
    test = segments[segments["start_time"] >= test_start].reset_index(drop=True)
    unknown = test.drop(columns=list(DRIVER_MULTS))
    min_km = config.score.min_trip_km

    def predict(frame, **kwargs):
        return predict_litres(booster, artifacts.feature_frame(frame, "expected", **kwargs), columns, frame["segment_km"])

    predictions = {"A_unknown_driver": predict(unknown), "B_driver_average": predict(test)}
    new_ids = {d["driver_id"] for d in roster if d["new_driver"]}
    is_new = test["driver_id"].isin(new_ids).to_numpy()
    history_bin = _history_bin(test["driver_history_trips"]).to_numpy()
    groups = {"all": np.ones(len(test), dtype=bool), "established_drivers": ~is_new, "new_drivers": is_new}
    for label in ("0", "1-5", "6-20", "21+"):
        groups[f"new_drivers_history_{label}"] = is_new & (history_bin == label)
    evaluation = {name: {key: trip_metrics(test[mask], pred[mask], min_km) for key, pred in predictions.items()} for name, mask in groups.items()}
    per_driver = pd.DataFrame({"driver_id": test["driver_id"], "actual": test["fuel_L"], "unknown": predictions["A_unknown_driver"],
                               "driver": predictions["B_driver_average"]}).groupby("driver_id").sum()
    per_driver = per_driver[per_driver["unknown"] > 0]
    evaluation["driver_ranking"] = {
        "drivers": int(len(per_driver)),
        "spearman_predicted_vs_actual_effect": round(float((per_driver["driver"] / per_driver["unknown"]).corr(
            per_driver["actual"] / per_driver["unknown"], method="spearman")), 3) if len(per_driver) > 2 else None,
        "note": "測試期每位駕駛：預估的個人效應（依個人平均 ÷ 不知道是誰開）與實際效應（實際 ÷ 不知道是誰開）的等級相關。",
    }

    # 5. Per new driver: averages after the first K trips -> error on the remaining trips.
    pre_test = rows[rows["start_time"] < test_start]
    curves = []
    for driver in sorted(new_ids):
        mine = rows[rows["driver_id"] == driver]
        trip_order = mine.drop_duplicates("trip_id").sort_values("start_time")["trip_id"].tolist()
        for k in (0, 1, 3, 5, 10, 20):
            if k >= len(trip_order):
                break
            profile = profiles_from_trips(pd.concat([pre_test, mine]), roster, dc, first_k={driver: k})[driver]
            part = unknown[unknown["trip_id"].isin(set(trip_order[k:]))]
            if part.empty:
                continue
            curves.append({"driver_id": driver, "first_k_trips": k, "profile": profile.to_json(),
                           "remaining": trip_metrics(part, predict(part, driver_profile=profile), min_km)})

    # 6. Replay: for each test trip (its real truck), which driver of the series free that day would the model pick?
    base = artifacts.feature_frame(unknown, "expected")
    lookup = daily.set_index(["day", "driver_id"])
    busy = rows.groupby("day")["driver_id"].agg(set)
    trip_rows = rows.drop_duplicates("trip_id")
    test_trips = trip_rows[(trip_rows["start_time"] >= test_start) & (trip_rows["trip_km"] >= min_km)]
    rows_by_trip = unknown.groupby("trip_id").indices
    replay = []
    for trip in test_trips.itertuples():
        vehicle_type = int(trip.vehicle_type)
        candidates = [d["driver_id"] for d in roster if d["primary_type"] == vehicle_type
                      and (d["driver_id"] == trip.driver_id or d["driver_id"] not in busy.get(trip.day, set()))]
        index = rows_by_trip[trip.trip_id]
        part = base.iloc[index]
        bands = unknown["road_band"].iloc[index]
        stacked = [apply_profile(part, daily_profile(lookup.loc[(trip.day, d)]).row_multipliers(bands)) for d in candidates]
        litres = predict_litres(booster, pd.concat(stacked, ignore_index=True), columns,
                                pd.Series(np.tile(part["segment_km"].to_numpy(float), len(candidates))))
        totals = litres.reshape(len(candidates), len(part)).sum(axis=1)
        best = int(np.argmin(totals))
        replay.append({"vehicle_type": vehicle_type, "assigned": trip.driver_id, "best": candidates[best], "candidates": len(candidates),
                       "assigned_pred_L": float(totals[candidates.index(trip.driver_id)]), "best_pred_L": float(totals[best])})
    replay_frame = pd.DataFrame(replay)
    replay_summary = {}
    if not replay_frame.empty:
        for key, part in [("all", replay_frame), *[(str(t), g) for t, g in replay_frame.groupby("vehicle_type")]]:
            replay_summary[key] = {
                "trips": int(len(part)),
                "assigned_pred_L": round(float(part["assigned_pred_L"].sum()), 1),
                "system_pred_L": round(float(part["best_pred_L"].sum()), 1),
                "predicted_saving_pct": round(float((1 - part["best_pred_L"].sum() / part["assigned_pred_L"].sum()) * 100), 1),
                "same_driver_share": round(float((part["best"] == part["assigned"]).mean()), 3),
                "mean_candidates": round(float(part["candidates"].mean()), 1),
            }

    # 7. Current averages for dispatch ("now" = all known trips) + roster statistics.
    current = profiles_from_trips(rows, roster, dc)
    stats = trip_rows.groupby("driver_id").agg(trips=("trip_id", "size"), km=("trip_km", "sum"), days=("day", "nunique"))
    plates = mock_plates(vehicles_by_type)
    profiles_out = []
    for d in roster:
        p = current[d["driver_id"]]
        s = stats.loc[d["driver_id"]] if d["driver_id"] in stats.index else None
        profiles_out.append({**d, **p.to_json(), "home_plate": plates.get(d["home_vehicle"]),
                             "composite_multiplier": round(composite(p.multipliers), 3),
                             "trips": int(s["trips"]) if s is not None else 0, "km": round(float(s["km"]), 1) if s is not None else 0.0,
                             "days": int(s["days"]) if s is not None else 0})

    artifact_dir = paths.output_root / "artifacts"
    write_json(artifact_dir / "virtual_drivers.json", roster)
    write_json(artifact_dir / "driver_profiles.json", {"note": MOCK_NOTE, "drivers": profiles_out})
    write_json(artifact_dir / "mock_plates.json", {"note": "模擬車牌（此為模擬資料），不是真實車牌", "plates": plates})
    assignment.rename("driver_id").reset_index().to_parquet(paths.output_root / "driver_assignment.parquet", index=False)
    daily.to_parquet(paths.output_root / "driver_profiles_daily.parquet", index=False)

    report = {
        "stage": "drivers",
        "mock_note": MOCK_NOTE,
        "method": {
            "assignment": "每位虛擬駕駛屬於一個車系、平常開其中一台車。每個真實「車-日」分給同車系的一位駕駛：當天綜合行為倍率在同車系中的排名，"
                          f"越接近駕駛設定風格的機率越高（σ={dc.assignment_sigma}），平常開這台車的駕駛優先；一人一天只開一台車；新進駕駛從測試期才開始出車。",
            "profile": "駕駛個人平均 = 本人「當天以前」在高速 / 一般道路 / 市區各自的怠速、急加速、急減速、超速，÷ 同樣路段與時段的一般值。"
                       f"行程少時往最相似的 {dc.neighbours} 位資深駕駛（≥{dc.min_neighbour_trips} 趟）收縮，w = n/(n+{dc.profile_k_trips:g})；"
                       f"某種道路開得少時往本人的全路段平均收縮，w = 段數/(段數+{dc.profile_k_segments:g})。",
            "model": "油耗模型（v1）用真實路段的實際行為訓練，不看駕駛身分；駕駛只透過「預估行為 = 路段一般值 × 個人平均」影響預測，新增駕駛不必重新訓練。",
            "caveat": "分配方式是依行為設計的模擬情境，駕駛本來就被設計成風格一致；因此「知道是誰開」帶來的改善是模擬結果，不能當作真實車隊的證據。",
        },
        "roster": {"drivers": len(roster), "per_type": dict(dc.per_type), "new_drivers": sorted(new_ids),
                   "vehicle_days": int(len(days)), "unassigned_vehicle_days": int(assignment.isna().sum()), "unassigned_segments": unassigned},
        "evaluation_test_period_ge10km": evaluation,
        "new_driver_curves": curves,
        "replay": {"note": "模型估計值：同一趟行程換成當天沒出車的同車系駕駛會用多少油（反事實無法實測）。", "summary": replay_summary},
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }
    write_json(paths.report_dir / "drivers_eval.json", report)
    return report
