"""Private Phase 3C-4 metric audit, voice forensics, and context feasibility report."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.dataset as pads

from .contracts import AlertLevel, AlertPolicyConfigV2
from .evaluation import acceptance_table, evaluate_tiered_policy, replay_tiered_policy
from .study import (
    _active_hours,
    _events_with_composite_key,
    _exact_anchor_idling,
    _json,
    _legacy_v1,
    _load_development,
    _target_episodes,
    _write_json,
)


FORENSIC_FEATURES = (
    "can_speed_mean_30s",
    "can_speed_std_30s",
    "can_speed_trend_per_second_30s",
    "engine_rpm_mean_60s",
    "engine_load_mean_60s",
    "decelerate_onset_count_60s",
    "speeding_onset_count_60s",
    "fatigue_onset_count_60s",
    "phone_onset_count_60s",
    "distract_onset_count_60s",
    "pcs_onset_count_60s",
    "ldws_onset_count_60s",
    "vehicle_status_driving_fraction_60s",
    "vehicle_status_idling_fraction_60s",
)


def _safe_number(value: Any) -> float | None:
    number = float(value)
    return number if np.isfinite(number) else None


def _distribution(values: list[float | None]) -> dict:
    array = np.asarray([value for value in values if value is not None], dtype=float)
    return {
        "count": int(array.size),
        "p25": float(np.percentile(array, 25)) if array.size else None,
        "median": float(np.median(array)) if array.size else None,
        "p75": float(np.percentile(array, 75)) if array.size else None,
        "mean": float(np.mean(array)) if array.size else None,
    }


def _anonymous_maps(data) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    vehicles = {
        vehicle: f"vehicle_{index:03d}"
        for index, vehicle in enumerate(sorted({str(value) for value in data.vehicle_ids}), 1)
    }
    trips = {}
    for vehicle, trip in zip(data.vehicle_ids, data.trip_ids):
        key = (str(vehicle), str(trip))
        digest = hashlib.sha256(f"phase3c4|{key[0]}|{key[1]}".encode()).hexdigest()[:12]
        trips[key] = f"trip_{digest}"
    return vehicles, trips


def _voice_forensics(decisions, episodes, data) -> dict:
    vehicle_aliases, trip_aliases = _anonymous_maps(data)
    feature_index = {name: data.feature_names.index(name) for name in FORENSIC_FEATURES}
    anchor_index = {
        (str(vehicle), str(trip), timestamp): index
        for index, (vehicle, trip, timestamp) in enumerate(
            zip(data.vehicle_ids, data.trip_ids, data.timestamps)
        )
    }
    voice_rows = [row for row in decisions if row.voice_emitted and not row.is_mock]
    used_episodes: set[int] = set()
    records = []
    for row in sorted(voice_rows, key=lambda item: item.timestamp):
        def next_onset(episode: dict) -> datetime | None:
            onsets = episode.get("target_event_start_times") or []
            candidates = [
                onset
                for onset in onsets
                if 0 < (onset - row.timestamp).total_seconds()
                <= float(episode.get("label_horizon_seconds", 60.0))
            ]
            return min(candidates, default=None)

        candidates = [
            index
            for index, episode in enumerate(episodes)
            if index not in used_episodes
            and (str(episode["enabled_code"]), str(episode["journey_code"]))
            == (row.enabled_code, row.journey_code)
            and episode["episode_start_time"] <= row.timestamp <= episode["episode_end_time"]
            and next_onset(episode) is not None
        ]
        matched_index = min(candidates, key=lambda index: episodes[index]["episode_start_time"]) if candidates else None
        if matched_index is not None:
            used_episodes.add(matched_index)
        episode = episodes[matched_index] if matched_index is not None else None
        onset = next_onset(episode) if episode else None
        lead = (onset - row.timestamp).total_seconds() if onset is not None else None
        source_index = anchor_index[(row.enabled_code, row.journey_code, row.timestamp)]
        features = {
            name: _safe_number(data.X[source_index, index])
            for name, index in feature_index.items()
        }
        records.append(
            {
                "anonymized_vehicle_reference": vehicle_aliases[row.enabled_code],
                "trip_reference": trip_aliases[(row.enabled_code, row.journey_code)],
                "timestamp": row.timestamp,
                "target_matched": matched_index is not None,
                "alert_level": row.emitted_level.value,
                "raw_risk": row.raw_risk,
                "smoothed_risk": row.smoothed_risk,
                "risk_slope_per_second": row.risk_slope_per_second,
                "persistence_duration_seconds": row.persistence_duration_seconds,
                "lead_time_seconds": lead,
                "cooldown_remaining_seconds": row.cooldown_remaining_seconds,
                "cooldown_active": row.cooldown_remaining_seconds > 0,
                "vehicle_speed_kph": features["can_speed_mean_30s"],
                "is_idling": row.is_idling,
                "hino_type": int(data.vehicle_types[source_index]),
                "seen_unseen_assignment": str(data.vehicle_holdout[source_index]),
                "raw_clock_hour_timezone_unresolved": row.timestamp.hour,
                "suppression_reason": row.suppression_reason.value,
                "dms_active": row.dms_active,
                "policy_episode_id": row.episode_id,
                "hino_features": features,
                "is_mock": False,
            }
        )

    groups = {
        "matched_voice": [row for row in records if row["target_matched"]],
        "offline_false_voice": [row for row in records if not row["target_matched"]],
    }
    comparison = {}
    numeric = (
        "raw_risk",
        "smoothed_risk",
        "risk_slope_per_second",
        "persistence_duration_seconds",
        "vehicle_speed_kph",
        "raw_clock_hour_timezone_unresolved",
    )
    for name, rows in groups.items():
        comparison[name] = {
            "count": len(rows),
            "distributions": {key: _distribution([row[key] for row in rows]) for key in numeric},
            "hino_feature_distributions": {
                key: _distribution([row["hino_features"][key] for row in rows])
                for key in FORENSIC_FEATURES
            },
            "type_counts": dict(sorted(Counter(str(row["hino_type"]) for row in rows).items())),
            "vehicle_counts": dict(sorted(Counter(row["anonymized_vehicle_reference"] for row in rows).items())),
            "seen_unseen_counts": dict(sorted(Counter(row["seen_unseen_assignment"] for row in rows).items())),
        }
    median_deltas = []
    for key in numeric:
        matched = comparison["matched_voice"]["distributions"][key]["median"]
        false = comparison["offline_false_voice"]["distributions"][key]["median"]
        if matched is not None and false is not None:
            median_deltas.append({"feature": key, "matched_minus_false_median": matched - false})
    for key in FORENSIC_FEATURES:
        matched_distribution = comparison["matched_voice"]["hino_feature_distributions"][key]
        false_distribution = comparison["offline_false_voice"]["hino_feature_distributions"][key]
        matched = matched_distribution["median"]
        false = false_distribution["median"]
        if matched is None or false is None:
            continue
        matched_iqr = matched_distribution["p75"] - matched_distribution["p25"]
        false_iqr = false_distribution["p75"] - false_distribution["p25"]
        scale = (matched_iqr + false_iqr) / 2
        median_deltas.append(
            {
                "feature": f"hino_feature.{key}",
                "matched_minus_false_median": matched - false,
                "pooled_iqr_scaled_median_difference": (matched - false) / scale if scale > 0 else None,
            }
        )

    def match_rates(field: str) -> dict[str, dict]:
        totals = Counter(str(row[field]) for row in records)
        matched = Counter(str(row[field]) for row in records if row["target_matched"])
        return {
            key: {
                "voice_count": count,
                "matched_voice_count": matched[key],
                "voice_match_rate": matched[key] / count,
            }
            for key, count in sorted(totals.items())
        }

    return {
        "status": "PRIVATE_PROVISIONAL_DEVELOPMENT_FORENSICS",
        "voice_episode_definition": "one emitted L2/L3 voice per Alert Policy v2 policy episode",
        "match_definition": "one-to-one match to a continuous positive Label V0 target region in the same composite trip",
        "offline_false_voice_caveat": "label-relative proxy; not a real-world accident false-alarm rate",
        "raw_identifiers_exported": False,
        "record_count": len(records),
        "records": records,
        "matched_vs_false_comparison": comparison,
        "descriptive_match_rates": {
            "hino_type": match_rates("hino_type"),
            "anonymized_vehicle": match_rates("anonymized_vehicle_reference"),
            "seen_unseen_assignment": match_rates("seen_unseen_assignment"),
        },
        "descriptive_median_differences_not_causal": median_deltas,
        "largest_descriptive_hino_feature_separations": sorted(
            [
                row
                for row in median_deltas
                if row["feature"].startswith("hino_feature.")
                and row.get("pooled_iqr_scaled_median_difference") is not None
            ],
            key=lambda row: abs(row["pooled_iqr_scaled_median_difference"]),
            reverse=True,
        )[:8],
    }


def _context_feasibility(repo_root: Path, data, active_hours: float) -> dict:
    source = pads.dataset(
        repo_root / "data" / "interim" / "telemetry_normalized",
        format="parquet",
        partitioning="hive",
    )
    trip_ids = sorted({str(value) for value in data.trip_ids})
    predicate = (
        pads.field("trip_id").isin(trip_ids)
        & (pads.field("timestamp") >= min(data.timestamps))
        & (pads.field("timestamp") <= max(data.timestamps))
    )
    table = source.to_table(
        columns=["timestamp", "latitude_deg", "longitude_deg", "gps_coordinates_missing", "vehicle_status"],
        filter=predicate,
    )
    missing = np.asarray(table["gps_coordinates_missing"].to_pylist(), dtype=bool)
    lat = np.asarray(table["latitude_deg"].to_pylist(), dtype=float)
    lon = np.asarray(table["longitude_deg"].to_pylist(), dtype=float)
    valid = ~missing & np.isfinite(lat) & np.isfinite(lon)
    coarse_cells = len({(round(a, 1), round(b, 1)) for a, b in zip(lat[valid], lon[valid])})
    statuses = np.asarray(table["vehicle_status"].to_pylist(), dtype=object)
    credential_presence = {
        name: bool(os.getenv(name))
        for name in ("TDX_CLIENT_ID", "TDX_CLIENT_SECRET", "CWA_API_KEY", "NCDR_API_KEY")
    }
    return {
        "status": "HISTORICAL_CONTEXT_UNAVAILABLE",
        "hino_scope": {
            "timestamp_start": min(data.timestamps),
            "timestamp_end": max(data.timestamps),
            "timezone": "UNRESOLVED_NO_CONVERSION_ALLOWED",
            "telemetry_row_count": int(table.num_rows),
            "gps_available": bool(valid.any()),
            "gps_missing_count": int((~valid).sum()),
            "gps_missing_rate": float((~valid).mean()) if len(valid) else None,
            "coordinate_representation": "float degrees; precise traces remain private and are not reported",
            "coarse_0_1_degree_cell_count_private_aggregate": coarse_cells,
            "active_driving_row_fraction": float(np.mean(statuses == 1)) if len(statuses) else None,
            "policy_anchor_active_driving_hours": active_hours,
        },
        "credential_environment_variable_presence_only": credential_presence,
        "local_historical_snapshot_inventory": {"tdx": 0, "cwa": 0, "ncdr": 0},
        "sources": {
            "tdx": {
                "capability": "historical Traffic v2 after July 2021 excluding current day; VD/CMS/CCTV/ETag families",
                "access": "member API credentials and subscription required for historical service",
                "overlap_result": "UNVERIFIED_WITHOUT_LOCAL_SNAPSHOT_AND_RESOLVED_HINO_TIMEZONE",
            },
            "cwa": {
                "capability": "O-A0001-001 and O-A0002-001 advertise historyAPI; O-A0003-001 is current-only",
                "access": "historyapi requires Authorization credential",
                "overlap_result": "UNVERIFIED_WITHOUT_LOCAL_SNAPSHOT_AND_RESOLVED_HINO_TIMEZONE",
            },
            "ncdr": {
                "capability": "authenticated alert API plus member-gated history download",
                "optimization_status": "FUTURE_SOURCE_SAMPLE_AND_HISTORICAL_MACHINE_ACCESS_UNRESOLVED",
            },
        },
        "blocking_reasons": [
            "HINO timestamp timezone remains organizer-unresolved, so an exact external-time join would be unsafe.",
            "No admissible cached TDX/CWA/NCDR historical snapshot exists locally for the development scope.",
            "Required API credentials are absent; secret values were neither read nor reported.",
        ],
        "empirical_context_optimization_performed": False,
    }


def _markdown_table(metrics: dict, criteria: dict) -> str:
    lines = ["| Metric | Target | Result | Status |", "|---|---:|---:|---|"]
    for row in acceptance_table(metrics, criteria):
        lines.append(f"| {row['metric']} | {row['operator']} {row['target']} | {row['result']} | {row['status']} |")
    return "\n".join(lines)


def _render_forensics(value: dict) -> str:
    groups = value["matched_vs_false_comparison"]
    lines = [
        "# Phase 3C-4 False-Voice Forensics",
        "",
        f"Status: **{value['status']}**. Raw vehicle and journey identifiers are not exported.",
        "",
        f"Voice episodes: {value['record_count']}; matched: {groups['matched_voice']['count']}; label-unmatched: {groups['offline_false_voice']['count']}.",
        "",
        "| Feature | Matched median | False median | Difference |",
        "|---|---:|---:|---:|",
    ]
    for row in value["descriptive_median_differences_not_causal"]:
        if row["feature"].startswith("hino_feature."):
            continue
        key = row["feature"]
        lines.append(
            f"| {key} | {groups['matched_voice']['distributions'][key]['median']} | {groups['offline_false_voice']['distributions'][key]['median']} | {row['matched_minus_false_median']} |"
        )
    lines += [
        "",
        "Largest existing-HINO-feature median separations (scaled by pooled IQR):",
        "",
    ]
    lines.extend(
        f"- {row['feature']}: {row['pooled_iqr_scaled_median_difference']}"
        for row in value["largest_descriptive_hino_feature_separations"]
    )
    lines += [
        "",
        "These are small-sample descriptive associations, not causal evidence and not an empirical context-gate rule. Clock-hour comparisons retain unresolved timezone semantics.",
        "",
        "The JSON companion contains private per-episode records with anonymized references and past-only HINO features.",
    ]
    return "\n".join(lines)


def generate_phase3c4_report(
    repo_root: Path,
    phase3c3_config_path: Path,
    context_config_path: Path,
    report_root: Path,
) -> dict:
    phase3c3 = _json(phase3c3_config_path)
    context_config = _json(context_config_path)
    data, probabilities = _load_development(repo_root, phase3c3)
    idling = _exact_anchor_idling(repo_root, data)
    events = _events_with_composite_key(repo_root, data)
    episodes = _target_episodes(data, events, phase3c3)
    hours = _active_hours(data, phase3c3)
    legacy = _legacy_v1(repo_root, data, probabilities, events, phase3c3)
    policy = AlertPolicyConfigV2(**context_config["closest_phase3c3_diagnostic_policy"])
    decisions = replay_tiered_policy(
        data.vehicle_ids, data.trip_ids, data.timestamps, probabilities, policy, is_idling=idling
    )
    metrics = evaluate_tiered_policy(
        decisions, episodes, active_driving_hours=hours, legacy_phase3c2_metrics=legacy
    )
    acceptance = acceptance_table(metrics, phase3c3["acceptance_criteria"])
    required = acceptance[:6]
    feasible = all(row["status"] == "PASS" for row in required)
    forensics = _voice_forensics(decisions, episodes, data)
    feasibility = _context_feasibility(repo_root, data, hours)
    criteria = phase3c3["acceptance_criteria"]
    advisory_suppression_needed = math.ceil(
        max(
            0.0,
            metrics["advisory_voice_alert_episode_count"]
            - hours * criteria["mean_advisory_voice_per_driving_hour_max"],
        )
    )
    false_suppression_needed = math.ceil(
        max(
            0.0,
            metrics["offline_false_voice_count"]
            - hours * criteria["offline_false_voice_per_driving_hour_max"],
        )
    )
    ablation = {
        "status": "EMPIRICAL_CONTEXT_ABLATION_UNAVAILABLE",
        "policy_v2": {"status": "EMPIRICAL_DEVELOPMENT_BASELINE", "metrics": metrics, "l1_fallback_count": 0},
        "policy_plus_tdx": {"status": "UNAVAILABLE_NO_ADMISSIBLE_HISTORICAL_JOIN"},
        "policy_plus_cwa": {"status": "UNAVAILABLE_NO_ADMISSIBLE_HISTORICAL_JOIN"},
        "policy_plus_tdx_plus_cwa": {"status": "UNAVAILABLE_NO_ADMISSIBLE_HISTORICAL_JOIN"},
        "policy_plus_tdx_plus_cwa_plus_ncdr": {"status": "UNAVAILABLE_INSUFFICIENT_CAPABILITY_EVIDENCE"},
        "simple_context_rule": "IMPLEMENTED_AND_SYNTHETICALLY_TESTED_NOT_EMPIRICALLY_OPTIMIZED",
        "learned_voice_gate": "NOT_TRAINED_NO_ADMISSIBLE_CONTEXT_FEATURE_ROWS",
        "mock_improvement_claimed": False,
    }
    report = {
        "study_id": context_config["study_id"],
        "status": "PROVISIONAL_DEVELOPMENT_RESULT",
        "frozen_xgboost_modified": False,
        "fresh_holdout_exists": False,
        "phase3c3_reference_policy_status": "CLOSEST_NON_FEASIBLE_DIAGNOSTIC_ONLY",
        "policy_parameters": policy.model_dump(mode="json"),
        "metric_audit": {
            "target_event_start": "earliest trusted organizer event onset explaining a positive anchor in the continuous target region",
            "label_horizon_seconds": phase3c3["evaluation"]["label_horizon_seconds"],
            "episode_construction": "continuous positive Label V0 anchors; zero, trip change, or >30s gap closes the region",
            "first_qualifying_alert_legacy": "first L1/L2/L3 intervention inside the positive target region",
            "lead_time_distributions": metrics["lead_time_distributions"],
            "label_leakage_result": "NO_ENGINE_LEAKAGE_FOUND; labels/events are evaluator-only. The 41s value is an intervention lead metric dominated by L1, not a voice lead metric.",
            "voice_count_audit_result": "BUG_FIXED: Phase 3C-3 collapsed L2 then L3 voices in one policy episode. Burden now counts each de-duplicated level transition and preserves the old collapsed count under an explicit backward-comparison key.",
            "pre_event_expansion": "Label V0 intentionally marks anchors up to 60s before event onset; this supports prediction but makes any-intervention lead larger than voice lead.",
            "rolling_voice": metrics["rolling_voice_metric_audit"],
            "rolling_metric_explanation": "Mean advisory/hour is a fleet exposure rate. P95 samples a trailing trip-local window at every decision anchor; many anchors/trips have zero or one voice, while total voices divided by 39.63 active hours can exceed one. These are different estimands.",
        },
        "policy_only_metrics": metrics,
        "acceptance": acceptance,
        "feasible_policy": feasible,
        "failed_required_constraints": [row["metric"] for row in required if row["status"] == "FAIL"],
        "context_gate_diagnostic_target_not_a_rule": {
            "advisory_voice_suppressions_needed_over_development_scope": advisory_suppression_needed,
            "minimum_label_unmatched_voice_suppressions_needed_over_development_scope": false_suppression_needed,
            "advisory_voice_reduction_per_driving_hour_needed": max(
                0.0,
                metrics["advisory_voice_alerts_per_driving_hour"]
                - criteria["mean_advisory_voice_per_driving_hour_max"],
            ),
            "interpretation": "The gate must remove about 20 L2 voices in this scope, including at least 10 label-unmatched voices, before precision is considered; this is a diagnostic requirement, not a policy rule.",
        },
        "context_feasibility": feasibility,
        "ablation": ablation,
        "false_voice_forensics_summary": {
            key: value for key, value in forensics.items() if key != "records"
        },
        "limitations": [
            "Only about 20 vehicles are available and unseen-vehicle behavior was not tuned.",
            "There is no verified driver identity.",
            "No genuinely fresh holdout exists.",
            "Offline false voice is relative to Label V0, not a real-world accident false-alarm rate.",
            "DMS simultaneous state remains unavailable.",
            "Historical context overlap could not be validated without resolved HINO timezone and admissible snapshots.",
        ],
    }
    report_root.mkdir(parents=True, exist_ok=True)
    _write_json(report_root / "false_voice_forensics.json", forensics)
    (report_root / "false_voice_forensics.md").write_text(_render_forensics(forensics), encoding="utf-8")
    _write_json(report_root / "policy_report.json", report)
    metric_md = "\n".join([
        "# Phase 3C-4 Metric Audit", "", report["metric_audit"]["label_leakage_result"], "", report["metric_audit"]["voice_count_audit_result"], "",
        json.dumps(metrics["lead_time_distributions"], indent=2), "", report["metric_audit"]["rolling_metric_explanation"], "",
        json.dumps(metrics["rolling_voice_metric_audit"], indent=2), "",
    ])
    (report_root / "metric_audit.md").write_text(metric_md, encoding="utf-8")
    feasibility_md = "\n".join([
        "# Phase 3C-4 Context Feasibility", "", f"Status: **{feasibility['status']}**.", "",
        *[f"- {reason}" for reason in feasibility["blocking_reasons"]], "",
        "No empirical context join or optimization was performed; mock data was not substituted.",
    ])
    (report_root / "context_feasibility.md").write_text(feasibility_md, encoding="utf-8")
    baseline_metrics = ablation["policy_v2"]["metrics"]
    ablation_md = "\n".join([
        "# Phase 3C-4 Context Ablation", "",
        "| Variant | Status | Intervention recall | Timely coverage | Advisory voice/hour | False voice/hour | Voice precision | Voice coverage | Voice lead median | L1 fallback |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| Policy v2 | EMPIRICAL_DEVELOPMENT_BASELINE | {baseline_metrics.get('any_intervention_recall')} | {baseline_metrics.get('timely_coverage_3s')} | {baseline_metrics.get('advisory_voice_alerts_per_driving_hour')} | {baseline_metrics.get('offline_false_voice_per_driving_hour')} | {baseline_metrics.get('voice_precision')} | {baseline_metrics.get('voice_coverage')} | {baseline_metrics.get('lead_time_distributions', {}).get('first_emitted_voice', {}).get('median_seconds')} | 0 |",
        "| Policy + TDX | UNAVAILABLE_NO_ADMISSIBLE_HISTORICAL_JOIN | — | — | — | — | — | — | — | — |",
        "| Policy + CWA | UNAVAILABLE_NO_ADMISSIBLE_HISTORICAL_JOIN | — | — | — | — | — | — | — | — |",
        "| Policy + TDX + CWA | UNAVAILABLE_NO_ADMISSIBLE_HISTORICAL_JOIN | — | — | — | — | — | — | — | — |",
        "| Policy + TDX + CWA + NCDR | UNAVAILABLE_INSUFFICIENT_CAPABILITY_EVIDENCE | — | — | — | — | — | — | — | — |",
        "", "Only Policy v2 has empirical development metrics. No mock improvement is reported.",
    ])
    (report_root / "context_ablation.md").write_text(ablation_md, encoding="utf-8")
    policy_md = "\n".join([
        "# Phase 3C-4 Policy Report", "", f"Status: **{'FEASIBLE' if feasible else 'NO FEASIBLE POLICY'}**.", "",
        _markdown_table(metrics, phase3c3["acceptance_criteria"]), "",
        f"Failed required constraints: {', '.join(report['failed_required_constraints']) or 'none'}.", "",
        report["context_gate_diagnostic_target_not_a_rule"]["interpretation"], "",
        "Context variants are unavailable for empirical comparison; the simple gate/provider path is implementation- and synthetic-fixture-tested only.",
    ])
    (report_root / "policy_report.md").write_text(policy_md, encoding="utf-8")
    return report
