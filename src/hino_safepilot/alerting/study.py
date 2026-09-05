"""Phase 3C-3 policy-only development over the frozen XGBoost scorer."""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pyarrow.dataset as pads

from hino_safepilot.modeling.training import load_split, trip_time_mask
from hino_safepilot.modeling.xgboost_training import _events_for_scope

from .contracts import AlertPolicyConfig, AlertPolicyConfigV2
from .evaluation import (
    build_target_risk_episodes,
    evaluate_alerts,
    evaluate_tiered_policy,
    exposure_hours,
    replay_revised_policy,
    replay_tiered_policy,
    select_policy_candidate,
)


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def _candidate_id(config: AlertPolicyConfigV2) -> str:
    payload = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _candidate_configs(study_config: dict) -> list[AlertPolicyConfigV2]:
    search = study_config["search_space"]
    fixed = study_config["policy_fixed"]
    names = (
        "l1_enter_threshold",
        "l2_enter_threshold",
        "l3_enter_threshold",
        "hysteresis_margin",
        "ema_half_life_seconds",
        "l2_persistence_seconds",
        "l2_cooldown_seconds",
        "risk_slope_l3_threshold_per_second",
    )
    result = []
    for values in itertools.product(*(search[name] for name in names)):
        candidate = dict(zip(names, values))
        margin = candidate.pop("hysteresis_margin")
        result.append(
            AlertPolicyConfigV2(
                policy_version=study_config["policy_version"],
                l1_exit_threshold=round(max(0.0, candidate["l1_enter_threshold"] - margin), 10),
                l2_exit_threshold=round(max(0.0, candidate["l2_enter_threshold"] - margin), 10),
                l3_exit_threshold=round(max(0.0, candidate["l3_enter_threshold"] - margin), 10),
                **candidate,
                **fixed,
            )
        )
    return result


def _load_development(repo_root: Path, config: dict):
    dataset_root = repo_root / "data" / "processed" / "ml" / "v0"
    feature_names = _json(
        dataset_root / "metadata" / "resolved_feature_spec.json"
    )["feature_columns"]
    validation = load_split(dataset_root, "validation", feature_names)
    cutoff = datetime.fromisoformat("2025-09-16T00:00:00")
    development = validation.subset(
        trip_time_mask(validation, cutoff, before=False), "validation_late_policy"
    )
    model_path = repo_root / "models" / "risk" / "v1_xgboost" / "risk_model_bundle.joblib"
    if not model_path.exists():
        raise FileNotFoundError("Frozen Phase 3C-2 XGBoost bundle is required; retraining is forbidden")
    bundle = joblib.load(model_path)
    if bundle.model_version != config["model_version"]:
        raise RuntimeError("Configured model version does not match the frozen bundle")
    probabilities = bundle.predict_proba(development.X)
    return development, probabilities


def _exact_anchor_idling(repo_root: Path, data) -> np.ndarray:
    """Use organizer-defined current carStatus=2, never low-speed inference."""

    source = pads.dataset(
        repo_root / "data" / "interim" / "telemetry_normalized",
        format="parquet",
        partitioning="hive",
    )
    trip_ids = {str(value) for value in data.trip_ids}
    predicate = (
        pads.field("trip_id").isin(sorted(trip_ids))
        & (pads.field("timestamp") >= min(data.timestamps))
        & (pads.field("timestamp") <= max(data.timestamps))
        & (pads.field("vehicle_status") == 2)
    )
    table = source.to_table(columns=["trip_id", "timestamp"], filter=predicate)
    idling_keys = {
        (str(trip), timestamp)
        for trip, timestamp in zip(table["trip_id"].to_pylist(), table["timestamp"].to_pylist())
    }
    return np.asarray(
        [
            (str(trip), timestamp) in idling_keys
            for trip, timestamp in zip(data.trip_ids, data.timestamps)
        ],
        dtype=bool,
    )


def _events_with_composite_key(repo_root: Path, data) -> list[dict]:
    vehicle_by_trip = {
        str(trip): str(vehicle)
        for trip, vehicle in zip(data.trip_ids, data.vehicle_ids)
    }
    return [
        {
            **event,
            "enabled_code": vehicle_by_trip[str(event["trip_id"])],
            # Private dataset metadata intentionally retains only the derived trip hash.
            # It is an opaque journey component here and is never a model feature.
            "journey_code": str(event["trip_id"]),
        }
        for event in _events_for_scope(repo_root, data)
        if str(event["trip_id"]) in vehicle_by_trip
    ]


def _active_hours(data, config: dict, mask: np.ndarray | None = None) -> float:
    use = np.ones(len(data.y), dtype=bool) if mask is None else mask
    driving = data.X[:, data.feature_names.index("vehicle_status_driving_fraction_60s")]
    count = int((np.nan_to_num(driving[use], nan=0.0) >= config["evaluation"]["driving_fraction_threshold"]).sum())
    return count * config["evaluation"]["anchor_exposure_seconds"] / 3600.0


def _target_episodes(data, events: list[dict], config: dict, mask=None) -> list[dict]:
    use = np.ones(len(data.y), dtype=bool) if mask is None else mask
    return build_target_risk_episodes(
        data.vehicle_ids[use],
        data.trip_ids[use],
        data.timestamps[use],
        data.y[use],
        events=events,
        maximum_contiguous_gap_seconds=config["evaluation"][
            "target_episode_maximum_contiguous_gap_seconds"
        ],
        label_horizon_seconds=config["evaluation"]["label_horizon_seconds"],
    )


def _legacy_v1(repo_root: Path, data, probabilities, events, config: dict) -> dict:
    policy = AlertPolicyConfig(
        **_json(repo_root / "models" / "risk" / "v1_xgboost" / "alert_policy_v1.json")
    )
    alerts = replay_revised_policy(data.trip_ids, data.timestamps, probabilities, policy)
    driving_index = data.feature_names.index("vehicle_status_driving_fraction_60s")
    exposure = exposure_hours(
        data.trip_ids,
        data.timestamps,
        data.X[:, driving_index],
        int(config["evaluation"]["anchor_exposure_seconds"]),
    )
    return evaluate_alerts(
        alerts,
        events,
        data.trip_ids,
        data.timestamps,
        exposure,
        horizon_seconds=int(config["evaluation"]["label_horizon_seconds"]),
    )


def _evaluate_candidate(
    policy: AlertPolicyConfigV2,
    data,
    probabilities,
    idling,
    episodes,
    hours: float,
    legacy: dict,
) -> tuple[dict, list]:
    decisions = replay_tiered_policy(
        data.vehicle_ids,
        data.trip_ids,
        data.timestamps,
        probabilities,
        policy,
        is_idling=idling,
    )
    metrics = evaluate_tiered_policy(
        decisions,
        episodes,
        active_driving_hours=hours,
        legacy_phase3c2_metrics=legacy,
    )
    return {
        "candidate_id": _candidate_id(policy),
        "policy": policy.model_dump(mode="json"),
        "metrics": metrics,
    }, decisions


def _slice_result(
    decisions,
    episodes,
    data,
    config: dict,
    mask: np.ndarray,
) -> dict:
    keys = {
        (str(vehicle), str(trip))
        for vehicle, trip in zip(data.vehicle_ids[mask], data.trip_ids[mask])
    }
    sliced_decisions = [
        row
        for row in decisions
        if (row.enabled_code, row.journey_code) in keys
    ]
    sliced_episodes = [
        row
        for row in episodes
        if (str(row["enabled_code"]), str(row["journey_code"])) in keys
    ]
    metrics = evaluate_tiered_policy(
        sliced_decisions,
        sliced_episodes,
        active_driving_hours=_active_hours(data, config, mask),
    )
    return {
        "row_count": int(mask.sum()),
        "trip_count": len({str(value) for value in data.trip_ids[mask]}),
        "target_episode_count": len(sliced_episodes),
        "metrics": metrics,
    }


def _robustness_slices(decisions, episodes, data, config: dict) -> dict:
    vehicles = sorted({str(value) for value in data.vehicle_ids})
    vehicle_rows = {}
    minimum = config["evaluation"]["meaningful_vehicle_minimum_target_episodes"]
    for index, vehicle in enumerate(vehicles, start=1):
        mask = np.asarray(data.vehicle_ids, dtype=str) == vehicle
        result = _slice_result(decisions, episodes, data, config, mask)
        result["interpretation_status"] = (
            "MEANINGFUL_SAMPLE" if result["target_episode_count"] >= minimum else "SMALL_SAMPLE_CAUTION"
        )
        vehicle_rows[f"vehicle_{index:03d}"] = result
    seen_unseen = {
        str(value): _slice_result(
            decisions,
            episodes,
            data,
            config,
            np.asarray(data.vehicle_holdout, dtype=str) == str(value),
        )
        for value in sorted({str(item) for item in data.vehicle_holdout})
    }
    by_type = {
        str(int(value)): _slice_result(
            decisions,
            episodes,
            data,
            config,
            np.asarray(data.vehicle_types) == value,
        )
        for value in sorted(set(data.vehicle_types))
    }
    return {
        "same_global_policy_parameters_for_all_slices": True,
        "vehicle_enabled_code_anonymized": vehicle_rows,
        "seen_unseen_assignment": seen_unseen,
        "hino_type": by_type,
    }


def run_policy_search(repo_root: Path, config_path: Path) -> dict:
    config = _json(config_path)
    data, probabilities = _load_development(repo_root, config)
    idling = _exact_anchor_idling(repo_root, data)
    events = _events_with_composite_key(repo_root, data)
    episodes = _target_episodes(data, events, config)
    hours = _active_hours(data, config)
    legacy = _legacy_v1(repo_root, data, probabilities, events, config)
    candidates = []
    decisions_by_id = {}
    for policy in _candidate_configs(config):
        row, decisions = _evaluate_candidate(
            policy, data, probabilities, idling, episodes, hours, legacy
        )
        candidates.append(row)
        decisions_by_id[row["candidate_id"]] = decisions
    selection = select_policy_candidate(candidates, config["acceptance_criteria"])
    winner = selection["winner"]
    robustness_reference = winner or selection["constraint_violation_table"][0]
    robustness = _robustness_slices(
        decisions_by_id[robustness_reference["candidate_id"]], episodes, data, config
    )
    return {
        "study_id": config["study_id"],
        "status": "PROVISIONAL_POLICY_DEVELOPMENT",
        "dataset_split_provenance": {
            "development": "late September 2025 whole trips from the existing validation split",
            "existing_test": "November 2025 KNOWN_HOLDOUT COMPARATIVE BENCHMARK; already viewed",
            "fresh_holdout_exists": False,
            "fresh_holdout_validation": "PENDING",
            "model_retrained_or_modified": False,
        },
        "development_sample": {
            "row_count": int(len(data.y)),
            "trip_count": len({str(value) for value in data.trip_ids}),
            "vehicle_count": len({str(value) for value in data.vehicle_ids}),
            "target_episode_count": len(episodes),
            "active_driving_hours": hours,
            "idling_anchor_count": int(idling.sum()),
            "dms_live_state_available": False,
        },
        "baseline_alert_policy_v1_metrics": legacy,
        "selection": selection,
        "robustness_slices": robustness,
        "robustness_policy_status": "SELECTED_FEASIBLE_POLICY"
        if winner is not None
        else "CLOSEST_NON_FEASIBLE_DIAGNOSTIC_ONLY",
        "robustness_policy_candidate_id": robustness_reference["candidate_id"],
        "phase3c2_unseen_vehicle_model_diagnostics": {
            "source": "docs/adr/0006-phase-3c-2-xgboost-alert-policy.md and frozen model card",
            "xgboost_with_type_pr_auc": 0.4317,
            "xgboost_without_type_pr_auc": 0.4310,
            "logistic_pr_auc": 0.4322,
            "status": "PRESERVED_DIAGNOSTIC_NOT_OPTIMIZED_IN_PHASE3C3",
        },
        "limitations": [
            "Only about 20 vehicles are available.",
            "enabledCode is a vehicle identifier; verified driver identity and rotation are unavailable.",
            "No genuinely fresh holdout remains; validation is pending and all results are provisional.",
            "Offline false voice is relative to Label V0 horizons, not a real-world accident false-alarm rate.",
            "Simultaneous DMS state is unavailable; arbitration behavior is contract-tested with is_mock=true fixtures only.",
            "Offline policy replay uses the private derived trip hash as an opaque journey token because raw journeyCode is intentionally absent from model metadata.",
            "Unseen-vehicle behavior is reported but not addressed or tuned in this phase.",
        ],
        "all_candidate_results": candidates,
    }


def _compact_candidate(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {
        "candidate_id": row["candidate_id"],
        "policy": row["policy"],
        "metrics": row["metrics"],
        "feasible": row.get("feasible"),
        "constraint_violations": row.get("constraint_violations", []),
        "acceptance": row.get("acceptance", []),
    }


def compact_report(search_report: dict) -> dict:
    selection = search_report["selection"]
    closest = (
        selection["constraint_violation_table"][0]
        if selection["winner"] is None and selection["constraint_violation_table"]
        else None
    )
    return {
        key: value
        for key, value in search_report.items()
        if key not in {"selection", "all_candidate_results"}
    } | {
        "selected_policy": _compact_candidate(selection["winner"]),
        "closest_non_feasible_policy": _compact_candidate(closest),
        "selection_summary": {
            "winner_status": selection["winner_status"],
            "selection_method": selection["selection_method"],
            "candidate_count": selection["candidate_count"],
            "feasible_candidate_count": selection["feasible_candidate_count"],
            "pareto_frontier": [
                _compact_candidate(row) for row in selection["pareto_frontier"][:20]
            ],
            "constraint_violation_table": [
                _compact_candidate(row)
                for row in selection["constraint_violation_table"][:20]
            ],
        },
    }


def render_markdown(report: dict) -> str:
    selected = report["selected_policy"]
    reference = selected or report.get("closest_non_feasible_policy")
    lines = [
        "# Phase 3C-3 Alert Policy V2 Report",
        "",
        f"Status: **{report['selection_summary']['winner_status']}**. Results are provisional offline engineering evidence.",
        "",
        "## Dataset and split provenance",
        "",
        "- Development: late September 2025 whole trips from the existing validation split.",
        "- Existing test: November 2025, already viewed and retained only as a known-holdout comparative benchmark.",
        "- Genuinely fresh holdout: no; validation remains pending.",
        "- Frozen XGBoost model retrained or modified: no.",
        "",
        "## Alert Policy V1 baseline",
        "",
        f"- Risk Coverage (`event_coverage`, unchanged): {report['baseline_alert_policy_v1_metrics'].get('event_coverage')}",
        f"- Voice alerts/hour: {report['baseline_alert_policy_v1_metrics'].get('voice_alerts_per_hour_anchor_based')}",
        f"- False alerts/hour: {report['baseline_alert_policy_v1_metrics'].get('false_alerts_per_hour_anchor_based')}",
        "",
        "## Alert Policy V2 selection",
        "",
    ]
    if selected is None:
        lines.extend(
            [
                "No candidate satisfied every available required constraint; no winner was selected and no threshold was relaxed.",
                "",
                "The machine-readable report contains the Pareto frontier and ranked constraint-violation table.",
                "",
                f"Closest non-feasible diagnostic candidate: `{reference['candidate_id']}`.",
                "",
                "```json",
                json.dumps(reference["policy"], indent=2),
                "```",
                "",
                "### Acceptance criteria for the closest non-feasible candidate",
                "",
                "| Metric | Target | Result | Status |",
                "|---|---:|---:|---|",
            ]
        )
        for row in reference["acceptance"]:
            lines.append(
                f"| {row['metric']} | {row['operator']} {row['target']} | {row['result']} | {row['status']} |"
            )
        metrics = reference["metrics"]
        lines.extend(
            [
                "",
                "### Closest-candidate diagnostics (not a winner)",
                "",
                f"- Intervention recall / Voice Coverage: {metrics.get('any_intervention_recall')} / {metrics.get('voice_coverage')}",
                f"- Advisory / critical / total voice per driving hour: {metrics.get('advisory_voice_alerts_per_driving_hour')} / {metrics.get('critical_voice_alerts_per_driving_hour')} / {metrics.get('voice_alerts_per_driving_hour')}",
                f"- Rolling voice mean/hour / P95 / maximum: {metrics.get('mean_voice_per_driving_hour')} / {metrics.get('p95_rolling_voice_count_60min')} / {metrics.get('max_rolling_voice_count_60min')}",
                f"- Offline false voice/hour / voice precision: {metrics.get('offline_false_voice_per_driving_hour')} / {metrics.get('voice_precision')}",
                f"- Lead time p25 / median / p75: {metrics.get('p25_lead_time_seconds')} / {metrics.get('median_lead_time_seconds')} / {metrics.get('p75_lead_time_seconds')}",
                f"- L0/L1/L2/L3 row distribution: {metrics.get('level_distribution')}",
                f"- Suppression counts: {metrics.get('suppression_reason_counts')}",
            ]
        )
    else:
        metrics = selected["metrics"]
        lines.extend(
            [
                f"Selected candidate: `{selected['candidate_id']}`",
                "",
                "```json",
                json.dumps(selected["policy"], indent=2),
                "```",
                "",
                "Intervention Coverage and Voice Coverage are separate:",
                "",
                f"- Intervention recall: {metrics.get('any_intervention_recall')}",
                f"- Voice coverage: {metrics.get('voice_coverage')}",
                f"- Timely coverage at 3 seconds: {metrics.get('timely_coverage_3s')}",
                f"- Advisory voice/hour: {metrics.get('advisory_voice_alerts_per_driving_hour')}",
                f"- Critical voice/hour: {metrics.get('critical_voice_alerts_per_driving_hour')}",
                f"- Offline false voice/hour: {metrics.get('offline_false_voice_per_driving_hour')}",
                f"- Voice precision: {metrics.get('voice_precision')}",
                f"- Lead time p25 / median / p75: {metrics.get('p25_lead_time_seconds')} / {metrics.get('median_lead_time_seconds')} / {metrics.get('p75_lead_time_seconds')}",
                f"- Rolling voice mean/hour / P95 / max: {metrics.get('mean_voice_per_driving_hour')} / {metrics.get('p95_rolling_voice_count_60min')} / {metrics.get('max_rolling_voice_count_60min')}",
                f"- Level distribution: {metrics.get('level_distribution')}",
                f"- Suppression reasons: {metrics.get('suppression_reason_counts')}",
                "",
                "### Acceptance criteria",
                "",
                "| Metric | Target | Result | Status |",
                "|---|---:|---:|---|",
            ]
        )
        for row in selected["acceptance"]:
            lines.append(
                f"| {row['metric']} | {row['operator']} {row['target']} | {row['result']} | {row['status']} |"
            )
    lines.extend(["", "## Robustness and limitations", ""])
    lines.append(
        "The machine-readable report applies the same global parameters to anonymized vehicle, seen/unseen-assignment, and Type 300/500/700 slices with sample counts. When no feasible winner exists these are explicitly diagnostic slices of the closest non-feasible candidate. No subgroup-specific tuning was performed."
    )
    slices = report.get("robustness_slices")
    if slices:
        lines.extend(
            [
                "",
                f"- Anonymized enabledCode slices: {len(slices['vehicle_enabled_code_anonymized'])}",
                f"- Existing seen/unseen-assignment slices: {len(slices['seen_unseen_assignment'])}",
                "",
                "| HINO Type | Rows | Target episodes | Intervention recall | Advisory voice/hour |",
                "|---:|---:|---:|---:|---:|",
            ]
        )
        for type_name, row in slices["hino_type"].items():
            lines.append(
                f"| {type_name} | {row['row_count']} | {row['target_episode_count']} | {row['metrics'].get('any_intervention_recall')} | {row['metrics'].get('advisory_voice_alerts_per_driving_hour')} |"
            )
    lines.append("")
    lines.extend(f"- {value}" for value in report["limitations"])
    lines.extend(
        [
            "",
            "DMS arbitration has no empirical HINO result because simultaneous live DMS state is unavailable. Only explicitly mock-tagged unit fixtures exercise that contract.",
            "",
        ]
    )
    return "\n".join(lines)


def generate_policy_report(
    repo_root: Path, config_path: Path, report_root: Path
) -> dict:
    search = run_policy_search(repo_root, config_path)
    report = compact_report(search)
    report_root.mkdir(parents=True, exist_ok=True)
    _write_json(report_root / "policy_search.json", search)
    _write_json(report_root / "policy_report.json", report)
    if report["selected_policy"] is not None:
        _write_json(
            report_root / "selected_policy.json", report["selected_policy"]["policy"]
        )
    elif report["closest_non_feasible_policy"] is not None:
        _write_json(
            report_root / "closest_non_feasible_policy.json",
            report["closest_non_feasible_policy"]["policy"],
        )
    (report_root / "policy_report.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    return report


def evaluate_specified_policy(
    repo_root: Path, study_config_path: Path, policy_config_path: Path
) -> dict:
    config = _json(study_config_path)
    data, probabilities = _load_development(repo_root, config)
    idling = _exact_anchor_idling(repo_root, data)
    events = _events_with_composite_key(repo_root, data)
    episodes = _target_episodes(data, events, config)
    legacy = _legacy_v1(repo_root, data, probabilities, events, config)
    policy = AlertPolicyConfigV2(**_json(policy_config_path))
    result, _ = _evaluate_candidate(
        policy,
        data,
        probabilities,
        idling,
        episodes,
        _active_hours(data, config),
        legacy,
    )
    return result
