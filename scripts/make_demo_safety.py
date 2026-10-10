"""產生「展示用」駕駛安全視圖（demo / 合成資料，非真實安全評分）。

為什麼需要：真實安全視圖(data/processed/demo_views/drivers.local.json)需要原始遙測跑完整
pipeline(hino-analytics score/drivers/export-demo)才會產生，而交付包不含原始遙測。
本腳本依現有「模擬駕駛名冊」(virtual_drivers.json)合成一份安全分數視圖，讓派車的
『駕駛安全 gate』能在 demo 時啟用、看得到排序效果。所有輸出都標 mock。

產生兩個檔（safety_overview 需要兩者都存在才回 available=True）：
  data/processed/demo_views/drivers.local.json      —— 每位駕駛的安全摘要/歷史
  data/processed/safety_scores/trip_safety_scores.jsonl —— 每趟安全分（供車隊總覽彙整）

分數刻意設計：每個車系內都同時有 ok(>=75) / caution(60-75) / block(<60)，
並安排少數「近期退步」，確保 demo 的派車排名一定看得到「安全改變排序 / 長途被擋」。
用固定種子，結果可重現。
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROSTER = ROOT / "data" / "processed" / "fuel" / "artifacts" / "virtual_drivers.json"
VIEWS = ROOT / "data" / "processed" / "demo_views"
SCORES = ROOT / "data" / "processed" / "safety_scores"
MOCK_NOTE = "此為展示用合成安全資料（demo），非真實安全評分；供派車『駕駛安全』功能示範。"

GRADE_THRESHOLDS = {"A": 90, "B": 80, "C": 70, "D": 60}  # 對齊 config/safety_score_v1.json 的語意


def grade_for(score: float) -> str:
    for g in ("A", "B", "C", "D"):
        if score >= GRADE_THRESHOLDS[g]:
            return g
    return "E"


# 每個車系安排一組「目標平均分」，保證涵蓋 ok/caution/block 三帶（依車系內序位循環）。
# 位置 0..n-1 對應由安全到不安全。給定幾個代表值，其餘線性插補。
def series_targets(n: int) -> list[float]:
    # 讓每個車系都有：約 2 位 block(<60)、2-3 位 caution(60-75)、其餘 ok(>=75)
    anchors = [94, 90, 86, 82, 78, 74, 70, 66, 58, 54]
    out = []
    for i in range(n):
        out.append(anchors[i] if i < len(anchors) else max(50, 92 - i * 3))
    return out


# Demo 安排：把「最省油的駕駛」安全分壓低，製造「系統因安全把最省油的人改派掉」的亮點。
# D01 是 300 系最省油(style_target 0.025)；設成 block(<60)，demo 時建議會改派給高安全分的省油駕駛。
SAFETY_OVERRIDE = {"D01": 57.0}


def main() -> None:
    roster = json.loads(ROSTER.read_text(encoding="utf-8"))
    rng = random.Random(20261005)

    # 依車系分組，組內排序後給目標分（driver_id 小的較安全，純為 demo 可預期）。
    by_series: dict[int, list[dict]] = {}
    for d in roster:
        by_series.setdefault(int(d["primary_type"]), []).append(d)

    drivers_out = []
    trip_lines = []
    base_day = datetime(2025, 9, 1, 8, 0, 0)

    for series, members in sorted(by_series.items()):
        members = sorted(members, key=lambda d: d["driver_id"])
        targets = series_targets(len(members))
        for pos, d in enumerate(members):
            did = d["driver_id"]
            target = float(SAFETY_OVERRIDE.get(did, targets[pos]))
            # 每位駕駛合成 ~18-30 趟，分數圍繞 target 波動。
            n_trips = rng.randint(18, 30)
            trips = []
            scores_km = []
            # 刻意讓每個車系的第 1、第 5 位「近期退步」：後段分數下滑。
            declining = pos in (1, 5)
            for t in range(n_trips):
                frac = t / max(1, n_trips - 1)
                drift = -12.0 * frac if declining else rng.uniform(-3, 3)
                score = max(35.0, min(99.0, target + drift + rng.uniform(-4, 4)))
                score = round(score, 1)
                km = round(rng.uniform(8, 120), 1)
                ts = (base_day + timedelta(days=t * 3, hours=rng.randint(0, 8))).isoformat()
                grade = grade_for(score)
                trips.append({
                    "trip_id": f"demo-{did}-{t:02d}",
                    "trip_start": ts,
                    "vehicle_alias": d["home_vehicle"],
                    "mock_plate": None,
                    "distance_km": km,
                    "score_status": "scored",
                    "overall_score": score,
                    "grade": grade,
                    "top_deduction": rng.choice(["accelerate", "decelerate", "idle", "overspeed", None]),
                    "dms_reliability": round(rng.uniform(0.85, 1.0), 2),
                    "short_trip": km < 2.0,
                })
                scores_km.append((score, km))
                trip_lines.append(json.dumps({
                    "trip_id": f"demo-{did}-{t:02d}", "overall_score": score,
                    "distance_km": km, "grade": grade, "trip_start": ts,
                }, ensure_ascii=False))

            total_km = round(sum(k for _, k in scores_km), 1)
            avg = round(sum(s * k for s, k in scores_km) / total_km, 1)
            recent = trips[-10:]
            prev = trips[-20:-10] or trips[:-10] or trips
            recent_avg = round(sum(t["overall_score"] for t in recent) / len(recent), 1)
            prev_avg = round(sum(t["overall_score"] for t in prev) / len(prev), 1)
            trend = "declining" if prev_avg - recent_avg >= 3 else ("improving" if recent_avg - prev_avg >= 3 else "stable")
            trend_label = {"declining": "退步中", "improving": "進步中", "stable": "持平"}[trend]
            grade_counts: dict[str, int] = {}
            for t in trips:
                grade_counts[t["grade"]] = grade_counts.get(t["grade"], 0) + 1

            # 每月平均
            monthly: dict[str, list] = {}
            for t in trips:
                m = t["trip_start"][:7]
                monthly.setdefault(m, []).append((t["overall_score"], t["distance_km"]))
            monthly_avg = [
                {"month": m, "trips": len(v),
                 "average_score": round(sum(s * k for s, k in v) / sum(k for _, k in v), 1)}
                for m, v in sorted(monthly.items())
            ]

            drivers_out.append({
                "driver_id": did,
                "driver_name": d["driver_name"],
                "primary_type": series,
                "new_driver": d.get("new_driver", False),
                "trip_count": n_trips,
                "scored_trip_count": n_trips,
                "total_distance_km": total_km,
                "average_score": avg,
                "recent_average_score": recent_avg,
                "previous_average_score": prev_avg,
                "recent_trips": len(recent),
                "trend": trend,
                "trend_label": trend_label,
                "grade_counts": dict(sorted(grade_counts.items())),
                "category_averages": {},
                "frequent_deductions": [],
                "camera_blocked_trip_count": 0,
                "road_adjusted_average": avg,
                "series_rank": None,
                "series_ranked_size": None,
                "monthly_average_scores": monthly_avg,
                "trips": trips,
                "scoring_version": "demo-synthetic-v1",
                "mock_note": MOCK_NOTE,
            })

    VIEWS.mkdir(parents=True, exist_ok=True)
    SCORES.mkdir(parents=True, exist_ok=True)
    (VIEWS / "drivers.local.json").write_text(
        json.dumps({"mock_note": MOCK_NOTE, "drivers": drivers_out}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (SCORES / "trip_safety_scores.jsonl").write_text("\n".join(trip_lines) + "\n", encoding="utf-8")

    # 摘要：每個車系的分級分佈，確認 demo 看得到效果。
    summary = {}
    for series, members in sorted(by_series.items()):
        rows = [d for d in drivers_out if d["primary_type"] == series]
        bands = {"ok(>=75)": 0, "caution(60-75)": 0, "block(<60)": 0}
        for r in rows:
            a = r["average_score"]
            bands["ok(>=75)" if a >= 75 else "caution(60-75)" if a >= 60 else "block(<60)"] += 1
        summary[series] = {"drivers": len(rows), **bands,
                           "declining": sum(1 for r in rows if r["trend"] == "declining")}
    print(json.dumps({"written": {"drivers": len(drivers_out), "trip_lines": len(trip_lines)},
                      "by_series": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
