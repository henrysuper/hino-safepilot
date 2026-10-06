"""AI 車隊決策（單程 A→B）：最佳車輛推薦、車輛排序、智慧路線、推薦理由。

這是一層**純組裝**的決策聚合器，不重算模型、不改任何既有流程：

1. 用 :func:`FuelPredictor.predict_trip_fuel` 取得「每條候選路線 × 每台車 × 每種駕駛類型」
   的油耗預測（已依 predicted_fuel_L 升冪排序）。
2. 從候選路線中挑出整體最省的那條（智慧路線），取它的 options。
3. 對這些 options 套 :func:`dispatch_health.apply_health_gate`（車況為主、油耗為輔），
   得到含車況風險成本的 ``dispatch_fuel_L``，並以它為**主排序鍵**。
4. 算出每台車相對「建議車」的 delta_L / delta_pct，組成車輛排序。
5. 把決策事實整理成 facts（供 narration 產生推薦理由，數字一律以 facts 為準）。

輸出的識別都還是 enabledCode；轉成前端別名（V01…）與模擬車牌由 server 層負責，
以維持「瀏覽器只看別名」的隱私界線。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .dispatch_health import HealthPolicy, apply_health_gate, classify_task, fit_bands
from .fuel_predictor import FuelPredictor

TASK_LEVEL_LABEL = {"light": "短途", "standard": "一般", "heavy": "長途/高速"}
GRADE_LABEL = {"ok": "良好", "caution": "注意", "block": "待檢修"}
DRIVER_TYPE_LABEL = {
    "very_eco": "非常省油", "eco": "省油", "normal": "一般",
    "aggressive": "激進", "very_aggressive": "非常激進",
}


@dataclass
class DispatchRequest:
    origin: list[float]            # [lon, lat]
    destination: list[float]       # [lon, lat]
    depart_time: str               # ISO 或 "YYYY-MM-DD HH:MM"
    vehicle_ids: list[str]         # enabledCode 清單（同車系候選車）
    driver_types: list[str]        # 要比較的駕駛類型；不分駕駛就用 ["normal"]
    use_live_traffic: bool = False
    origin_name: str | None = None
    destination_name: str | None = None


def _best_route(result: dict) -> dict:
    """挑整體最省的候選路線：以該路線 options 的最低 predicted_fuel_L 為準。"""

    routes = [r for r in result["routes"] if r.get("options")]
    if not routes:
        raise ValueError("predictor 未產生任何可用的路線/選項")
    return min(routes, key=lambda r: r["options"][0]["predicted_fuel_L"])


def _road_mix(segments: list[dict]) -> dict:
    """各路型佔總距離的比例，用來描述「這條路線長什麼樣」。"""

    total = sum(float(s.get("km") or 0.0) for s in segments) or 1.0
    mix: dict[str, float] = {}
    for s in segments:
        band = s.get("road_band") or "normal"
        mix[band] = mix.get(band, 0.0) + float(s.get("km") or 0.0)
    return {band: round(km / total * 100, 1) for band, km in sorted(mix.items(), key=lambda kv: -kv[1])}


def _congested_segments(segments: list[dict], top: int = 2) -> list[dict]:
    """塞車指數最低（最壅塞）的幾段，供理由點名。congestion_index 越小越塞。"""

    graded = [
        {"segment_no": i + 1, "road_band": s.get("road_band"),
         "congestion_index": s.get("congestion_index"), "source": s.get("congestion_source")}
        for i, s in enumerate(segments)
        if s.get("congestion_index") is not None
    ]
    graded.sort(key=lambda s: s["congestion_index"])
    return [s for s in graded if s["congestion_index"] is not None and s["congestion_index"] < 0.9][:top]


def recommend(
    predictor: FuelPredictor,
    request: DispatchRequest,
    policy: HealthPolicy | None = None,
    routes=None,
) -> dict:
    """產生單程派車決策。回傳 dict（識別仍為 enabledCode）。

    結構::

        {
          "origin"/"destination"/"origin_name"/"destination_name",
          "depart_time_local", "model_version",
          "route": {route_id, distance_km, minutes, road_mix, congested_segments,
                    segments, segment_geometry?},            # 智慧路線（最省那條）
          "alternatives": [...],                              # 被淘汰的候選路線摘要
          "recommended": {...option..., delta_L:0, delta_pct:0},
          "ranking": [ {...option..., rank, delta_L, delta_pct}, ... ],  # 依 dispatch_fuel_L
          "facts": {...},                                     # 給推薦理由（數字來源）
          "notes": [...],
        }
    """

    policy = policy or getattr(predictor, "health_policy", None) or HealthPolicy()

    result = predictor.predict_trip_fuel(
        origin=request.origin,
        destination=request.destination,
        depart_time=request.depart_time,
        vehicle_ids=request.vehicle_ids,
        driver_types=request.driver_types,
        use_live_traffic=request.use_live_traffic,
        routes=routes,
    )

    chosen = _best_route(result)
    distance_km = float(chosen["distance_km"])

    # --- 車況門檻（主排序鍵 dispatch_fuel_L）。health series index = enabledCode。
    health = predictor.artifacts.health
    bands = fit_bands(health, policy)
    task_level = classify_task(distance_km, policy)
    graded = apply_health_gate(
        chosen["options"], health, distance_km, policy=policy, bands=bands, id_key="vehicle_id"
    )

    # --- 車輛排序 + 與建議車的差距。
    best = graded[0]
    best_fuel = float(best["dispatch_fuel_L"])
    ranking = []
    for i, opt in enumerate(graded):
        delta_l = round(float(opt["dispatch_fuel_L"]) - best_fuel, 2)
        delta_pct = round(delta_l / best_fuel * 100, 1) if best_fuel else 0.0
        ranking.append({**opt, "rank": i + 1, "delta_L": delta_l, "delta_pct": delta_pct})

    recommended = {**ranking[0]}
    runner_up = ranking[1] if len(ranking) > 1 else None

    # 若「最省油（未計車況）」的車不是「建議車」，表示車況改變了結論，理由要點出。
    cheapest_by_fuel = min(chosen["options"], key=lambda o: o["predicted_fuel_L"])
    overridden = cheapest_by_fuel["vehicle_id"] != recommended["vehicle_id"] or \
        cheapest_by_fuel["driver_type"] != recommended["driver_type"]

    route_info = {
        "route_id": chosen["route_id"],
        "distance_km": distance_km,
        "minutes": recommended.get("predicted_minutes"),
        "ors_minutes_free_flow": chosen.get("ors_minutes_free_flow"),
        "road_mix": _road_mix(chosen["segments"]),
        "congested_segments": _congested_segments(chosen["segments"]),
        "segments": chosen["segments"],
    }

    alternatives = [
        {"route_id": r["route_id"], "distance_km": r["distance_km"],
         "best_fuel_L": r["options"][0]["predicted_fuel_L"]}
        for r in result["routes"] if r["route_id"] != chosen["route_id"] and r.get("options")
    ]

    facts = _build_facts(request, result, route_info, recommended, runner_up,
                         task_level, bands, overridden, cheapest_by_fuel)

    return {
        "origin": request.origin,
        "destination": request.destination,
        "origin_name": request.origin_name,
        "destination_name": request.destination_name,
        "depart_time_local": result["depart_time_local"],
        "model_version": result["model_version"],
        "route": route_info,
        "alternatives": alternatives,
        "recommended": recommended,
        "ranking": ranking,
        "facts": facts,
        "notes": result.get("notes", []),
        "driver_type_note": result.get("driver_type_note"),
    }


def _build_facts(request, result, route_info, recommended, runner_up, task_level,
                 bands, overridden, cheapest_by_fuel) -> dict:
    """整理推薦理由所需的事實（純數字 + 標籤，供 narration 使用）。"""

    interval = recommended.get("prediction_interval_L") or [None, None]
    congested = route_info["congested_segments"]
    return {
        "origin_name": request.origin_name,
        "destination_name": request.destination_name,
        "distance_km": route_info["distance_km"],
        "minutes": recommended.get("predicted_minutes"),
        "depart_time_local": result["depart_time_local"],
        "task_level": task_level,
        "task_level_label": TASK_LEVEL_LABEL.get(task_level, task_level),
        "candidates": len(result["routes"]),
        "fleet_candidates": len({o["vehicle_id"] for r in result["routes"] for o in r["options"]}),
        "recommended": {
            "vehicle_type": recommended.get("vehicle_type"),
            "driver_type": recommended.get("driver_type"),
            "driver_type_label": DRIVER_TYPE_LABEL.get(recommended.get("driver_type"), recommended.get("driver_type")),
            "predicted_fuel_L": recommended.get("predicted_fuel_L"),
            "dispatch_fuel_L": recommended.get("dispatch_fuel_L"),
            "interval_low_L": interval[0],
            "interval_high_L": interval[1],
            "minutes": recommended.get("predicted_minutes"),
            "health_grade": recommended.get("health_grade"),
            "health_grade_label": GRADE_LABEL.get(recommended.get("health_grade"), recommended.get("health_grade")),
            "health_score": recommended.get("health_score"),
            "health_note": recommended.get("health_note") or None,
        },
        "runner_up": None if runner_up is None else {
            "delta_L": runner_up["delta_L"],
            "delta_pct": runner_up["delta_pct"],
            "health_grade_label": GRADE_LABEL.get(runner_up.get("health_grade"), runner_up.get("health_grade")),
        },
        "health_overridden": bool(overridden),
        "cheapest_health_grade_label": GRADE_LABEL.get(cheapest_by_fuel.get("health_grade"),
                                                       cheapest_by_fuel.get("health_grade")) if overridden else None,
        "road_mix": route_info["road_mix"],
        "main_road_band_label": {"highway": "高速/快速道路", "normal": "一般道路", "city": "市區"}.get(
            next(iter(route_info["road_mix"]), None), None),
        "congested_count": len(congested),
        "has_live_traffic": any(s.get("source") == "tdx_live" for s in congested) or
                            any(s.get("congestion_source") == "tdx_live" for s in route_info["segments"]),
        "notes": result.get("notes", []),
    }
