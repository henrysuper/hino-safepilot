"""派車健康度硬門檻(車況為主、油耗為輔)。

為什麼獨立成一層
----------------
審查(reports/fuel/health_offset_collinearity.json)顯示:
- health_score 與實際油耗的同車型內相關僅 ~0.06,所以它在油耗迴歸裡幾乎沒作用
  (文件量到 ~3% 重要度)。
- 但 health_score 量的是「車輛可靠度 / 故障風險」(水溫、DTC、過載、引擎時數、電瓶),
  正是派車「以車況為主」真正該依據的維度。

因此健康度不該被埋在油耗模型裡,而應該在派車時當成**獨立的前置關卡**:
先用車況決定「這台車能不能派、能派什麼等級的任務」,再用油耗預測在合格車裡挑最省的。

本模組是**純函式、確定性**的一層,放在油耗排序(predicted_fuel_L)之前或之後都可套用,
不修改任何既有模型或預測流程(符合 repo「安全/派車計算需 deterministic」原則)。

資料來源:data/processed/fuel/artifacts/vehicles.parquet(含 health_score,私有,git-ignored)。
門檻設定:config/dispatch_health_v1.json(可選;缺檔用本模組預設值)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


# 任務分級:依里程判斷任務對車況的要求(長途對可靠度更敏感)
TASK_LIGHT = "light"      # 短途市區,對車況較寬容
TASK_STANDARD = "standard"
TASK_HEAVY = "heavy"      # 長途/高速,對車況要求高


@dataclass(frozen=True)
class HealthPolicy:
    """健康度門檻設定。分數為 0-100(越高越健康,已對同車型中心化)。

    門檻以「全車隊 health 分布的分位數」為預設,避免寫死絕對分數——
    因為 health 是相對同車型的分數,絕對值會隨車隊而變。
    也可用 absolute 模式直接指定分數門檻。
    """

    mode: str = "quantile"                 # "quantile" | "absolute"
    block_quantile: float = 0.10           # 最差 10% -> block(建議進廠)
    caution_quantile: float = 0.30         # 其次 30% -> caution(可派但降權)
    block_score: float = 60.0              # absolute 模式用
    caution_score: float = 75.0
    # caution 車的軟懲罰:等效「視同多耗 X% 油」,長途任務加重
    caution_penalty_pct: dict = field(default_factory=lambda: {
        TASK_LIGHT: 2.0, TASK_STANDARD: 5.0, TASK_HEAVY: 10.0,
    })
    # 任務分級的里程界線(km)
    heavy_min_km: float = 80.0
    light_max_km: float = 15.0
    # heavy 任務時,block 車絕對不可派;caution 車是否允許(預設允許但重罰)
    block_hard_exclude: bool = True

    @classmethod
    def load(cls, path: Path | None) -> "HealthPolicy":
        if path and Path(path).is_file():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            known = {k: data[k] for k in data if k in cls.__dataclass_fields__}
            return cls(**known)
        return cls()


@dataclass(frozen=True)
class HealthBands:
    """由車隊 health 分布算出的實際分級門檻。"""
    block_below: float
    caution_below: float
    source: str

    def grade(self, score: float) -> str:
        if score is None or not (score == score):   # NaN -> 保守視為 caution
            return "caution"
        if score < self.block_below:
            return "block"
        if score < self.caution_below:
            return "caution"
        return "ok"


def classify_task(distance_km: float, policy: HealthPolicy) -> str:
    if distance_km >= policy.heavy_min_km:
        return TASK_HEAVY
    if distance_km <= policy.light_max_km:
        return TASK_LIGHT
    return TASK_STANDARD


def fit_bands(health: pd.Series, policy: HealthPolicy) -> HealthBands:
    """由全車隊 health 分布定分級門檻。"""
    h = pd.to_numeric(health, errors="coerce").dropna()
    if policy.mode == "absolute" or len(h) < 5:
        return HealthBands(policy.block_score, policy.caution_score, "absolute")
    return HealthBands(
        block_below=float(h.quantile(policy.block_quantile)),
        caution_below=float(h.quantile(policy.caution_quantile)),
        source=f"quantile(block={policy.block_quantile}, caution={policy.caution_quantile})",
    )


def load_health(artifacts_dir: Path) -> pd.Series:
    """enabled_code -> health_score。"""
    v = pd.read_parquet(Path(artifacts_dir) / "vehicles.parquet")
    return v.set_index("enabled_code")["health_score"]


def apply_health_gate(
    options: list[dict],
    health: pd.Series,
    distance_km: float,
    policy: HealthPolicy | None = None,
    bands: HealthBands | None = None,
    id_key: str = "vehicle_id",
) -> list[dict]:
    """對派車選項套用健康度門檻(車況為主、油耗為輔)。

    每個 option 需含 ``predicted_fuel_L`` 與車輛識別(``id_key``,預設 vehicle_id;
    值須是 health 索引用的 enabled_code,或先自行映射)。

    規則:
      - block   任務級別為 heavy 時硬剔除(block_hard_exclude);其餘級別標記但沉底。
      - caution 可派,但加「健康度風險成本」:dispatch_fuel_L = predicted_fuel_L × (1 + penalty)。
      - ok      不動。

    回傳**新的 list**(不改原 dict 的 predicted_fuel_L):每個 option 補上
    health_score / health_grade / dispatch_fuel_L / health_note,並依
    (是否被排除, dispatch_fuel_L) 排序。原始油耗排序資訊保留在 predicted_fuel_L。
    """
    policy = policy or HealthPolicy()
    if bands is None:
        bands = fit_bands(health, policy)
    task = classify_task(distance_km, policy)
    penalty_pct = policy.caution_penalty_pct.get(task, policy.caution_penalty_pct.get(TASK_STANDARD, 5.0))

    graded = []
    for opt in options:
        vid = opt.get(id_key)
        score = float(health.get(vid)) if vid in health.index else None
        grade = bands.grade(score)
        base = float(opt["predicted_fuel_L"])
        excluded = False
        note = ""
        factor = 1.0
        if grade == "block":
            if task == TASK_HEAVY and policy.block_hard_exclude:
                excluded = True
                note = "車況分數偏低,長途任務不建議派遣;建議安排檢修。"
            else:
                factor = 1.0 + penalty_pct / 100.0 * 2  # 非 heavy 也重罰
                note = "車況分數偏低,建議優先安排檢修;僅在無其他車可用時派遣。"
        elif grade == "caution":
            factor = 1.0 + penalty_pct / 100.0
            note = f"車況分數中等,{task} 任務加計 {penalty_pct:.0f}% 健康度風險成本。"
        new = dict(opt)
        new["health_score"] = round(score, 1) if score is not None else None
        new["health_grade"] = grade
        new["health_task_level"] = task
        new["health_excluded"] = excluded
        new["dispatch_fuel_L"] = round(base * factor, 2)
        new["health_note"] = note
        graded.append(new)

    # 排序:未排除者優先,再依「含健康度成本的油耗」升冪
    graded.sort(key=lambda o: (o["health_excluded"], o["dispatch_fuel_L"]))
    return graded


def summarize_fleet(health: pd.Series, policy: HealthPolicy | None = None) -> dict:
    """車隊健康度分級總覽(供主控台/報告)。"""
    policy = policy or HealthPolicy()
    bands = fit_bands(health, policy)
    rows = []
    counts = {"ok": 0, "caution": 0, "block": 0}
    for code, score in health.items():
        g = bands.grade(float(score) if score == score else None)
        counts[g] += 1
        rows.append({"enabled_code": code, "health_score": round(float(score), 1), "grade": g})
    rows.sort(key=lambda r: r["health_score"])
    return {
        "bands": {"block_below": round(bands.block_below, 1), "caution_below": round(bands.caution_below, 1),
                  "source": bands.source},
        "counts": counts,
        "vehicles": rows,
    }
