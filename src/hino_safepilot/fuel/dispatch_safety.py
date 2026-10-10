"""派車駕駛安全門檻（在車況之後、油耗之上再加一層「駕駛安全」）。

為什麼獨立成一層
----------------
派車原本只看「油耗 + 車況(health)」。但車隊端已經有每位虛擬駕駛的**安全分數**
（0–100，越高越安全，來自事後安全評分 pipeline），這是「該不該把一趟任務交給
這位駕駛」真正該參考的維度——派一個很省油但開車高風險的駕駛，對車隊不是好決策。

本模組比照 ``dispatch_health.apply_health_gate`` 的設計：**純函式、確定性**，
放在車況門檻之後、唯一配對(match_unique)之前，對「含車況成本的油耗」再乘一層
駕駛安全風險成本，產生綜合排序鍵 ``dispatch_score``。不改任何既有模型或油耗欄位。

設計決策（與使用者確認）：
- **average 為主**：以駕駛整體平均安全分 average_score 決定門檻與懲罰。
- **recent 當警示**：近期平均比先前明顯退步、或趨勢 declining 時，加註警示並略增懲罰，
  但不單獨拿 recent 當排序主軸（recent 波動大）。
- **兩段式**：安全分低於 block 門檻 → heavy 任務硬排除；caution 區間 → 軟懲罰。
- 分數越低懲罰越大，方向與 health gate 一致（低分 → factor > 1 → 排後面）。
- **缺安全資料就整個 no-op**：安全視圖是可選、需另外產生的資料
  （data/processed/demo_views/drivers.local.json）。沒有時派車行為與未加本層完全相同。

資料來源：analytics.console_safety.safety_overview(...) 的 drivers 列，
每筆含 driver_id / average_score / recent_average_score / previous_average_score / trend。
門檻設定：config/dispatch_safety_v1.json（可選；缺檔用本模組預設值）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# 任務分級沿用車況那層的語意（依里程），長途對駕駛安全更敏感。
from .dispatch_health import TASK_HEAVY, TASK_LIGHT, TASK_STANDARD


@dataclass(frozen=True)
class SafetyPolicy:
    """駕駛安全門檻設定。分數 0–100，越高越安全。

    預設用「絕對分數」門檻（安全分是跨駕駛可比的 0–100 絕對量，不像 health 需相對同車型），
    但保留 quantile 模式以備車隊分布特殊時改用。
    """

    mode: str = "absolute"                 # "absolute" | "quantile"
    block_score: float = 60.0              # 低於此 -> block（長途硬排除）
    caution_score: float = 75.0            # 低於此 -> caution（軟懲罰）
    block_quantile: float = 0.10
    caution_quantile: float = 0.30
    # caution 駕駛的軟懲罰（視同多耗 X% 油），長途加重。
    caution_penalty_pct: dict = field(default_factory=lambda: {
        TASK_LIGHT: 3.0, TASK_STANDARD: 6.0, TASK_HEAVY: 12.0,
    })
    # 近期退步警示：recent 比 previous 低 >= 這麼多分，視為退步，額外加這麼多 % 懲罰。
    recent_decline_points: float = 7.0
    recent_decline_extra_pct: float = 4.0
    # heavy 任務時 block 駕駛是否硬排除。
    block_hard_exclude: bool = True

    @classmethod
    def load(cls, path: Path | None) -> "SafetyPolicy":
        if path and Path(path).is_file():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            known = {k: data[k] for k in data if k in cls.__dataclass_fields__}
            return cls(**known)
        return cls()


@dataclass(frozen=True)
class SafetyBands:
    """實際使用的分級門檻（absolute 直接用設定；quantile 由車隊安全分分布算）。"""
    block_below: float
    caution_below: float
    source: str

    def grade(self, score: float | None) -> str:
        # 沒有安全分數的駕駛 -> unknown（不懲罰、也不排除；只標記）。
        if score is None or not (score == score):
            return "unknown"
        if score < self.block_below:
            return "block"
        if score < self.caution_below:
            return "caution"
        return "ok"


def fit_bands(scores: list[float], policy: SafetyPolicy) -> SafetyBands:
    clean = [float(s) for s in scores if s is not None and s == s]
    if policy.mode == "quantile" and len(clean) >= 5:
        clean.sort()

        def q(p: float) -> float:
            idx = min(len(clean) - 1, max(0, int(round(p * (len(clean) - 1)))))
            return clean[idx]

        return SafetyBands(q(policy.block_quantile), q(policy.caution_quantile),
                           f"quantile(block={policy.block_quantile}, caution={policy.caution_quantile})")
    return SafetyBands(policy.block_score, policy.caution_score, "absolute")


def apply_safety_gate(
    options: list[dict],
    safety_by_driver: dict[str, dict] | None,
    distance_km: float,
    policy: SafetyPolicy | None = None,
    bands: SafetyBands | None = None,
    base_key: str = "dispatch_fuel_L",
    id_key: str = "driver_id",
) -> list[dict]:
    """對派車選項套用「駕駛安全」門檻（在車況之後）。

    每個 option 需含 ``base_key``（排序基準，預設 dispatch_fuel_L＝含車況成本的油耗；
    若無車況層則傳 predicted_fuel_L）與 ``id_key``（駕駛識別，預設 driver_id）。

    規則：
      - block   安全分偏低。heavy 任務硬排除（block_hard_exclude）；其餘級別標記並重罰。
      - caution 可派，加「安全風險成本」：× (1 + penalty)。
      - 近期退步 / trend=declining：額外加 recent_decline_extra_pct。
      - ok / unknown 不懲罰（unknown＝沒有安全資料的駕駛，保持中性）。

    回傳**新的 list**（不改原油耗欄位）：每個 option 補上
    safety_score / safety_recent / safety_grade / safety_trend / safety_excluded /
    safety_note / dispatch_score（＝base_key × 安全 factor，綜合排序鍵），
    並依 (health_excluded?, safety_excluded, dispatch_score) 排序。

    若 ``safety_by_driver`` 為空（缺安全資料）→ 完全 no-op：
    只把 dispatch_score 設為 base_key，不加任何欄位旗標、不改排序結果。
    """
    policy = policy or SafetyPolicy()

    # 缺資料：no-op。dispatch_score 退化為 base_key，排序與未加本層一致。
    if not safety_by_driver:
        out = []
        for opt in options:
            new = dict(opt)
            new["dispatch_score"] = float(opt.get(base_key, opt["predicted_fuel_L"]))
            out.append(new)
        out.sort(key=lambda o: (o.get("health_excluded", False), o["dispatch_score"]))
        return out

    if bands is None:
        bands = fit_bands([v.get("average_score") for v in safety_by_driver.values()], policy)
    task = _task_level(distance_km)
    penalty_pct = policy.caution_penalty_pct.get(task, policy.caution_penalty_pct.get(TASK_STANDARD, 6.0))

    graded = []
    for opt in options:
        did = opt.get(id_key)
        info = safety_by_driver.get(did) if did is not None else None
        avg = info.get("average_score") if info else None
        recent = info.get("recent_average_score") if info else None
        previous = info.get("previous_average_score") if info else None
        trend = info.get("trend") if info else None
        grade = bands.grade(avg)
        base = float(opt.get(base_key, opt["predicted_fuel_L"]))

        factor = 1.0
        excluded = False
        note = ""
        if grade == "block":
            if task == TASK_HEAVY and policy.block_hard_exclude:
                excluded = True
                note = f"駕駛安全分偏低（{avg:.0f}），長途任務不建議派遣。"
            else:
                factor = 1.0 + penalty_pct / 100.0 * 2
                note = f"駕駛安全分偏低（{avg:.0f}），僅在無其他駕駛可用時派遣。"
        elif grade == "caution":
            factor = 1.0 + penalty_pct / 100.0
            note = f"駕駛安全分中等（{avg:.0f}），{_task_label(task)}任務加計 {penalty_pct:.0f}% 安全風險成本。"

        # 近期退步警示（recent 當警示，不當主軸）：只在非排除時加碼。
        declining = (trend == "declining") or (
            recent is not None and previous is not None and (previous - recent) >= policy.recent_decline_points
        )
        if declining and not excluded:
            factor += policy.recent_decline_extra_pct / 100.0
            drop = f"近期退步" if trend == "declining" else f"近期較前期低 {previous - recent:.0f} 分"
            note = (note + f" 另：{drop}，加計 {policy.recent_decline_extra_pct:.0f}%。").strip()

        new = dict(opt)
        new["safety_score"] = round(avg, 1) if avg is not None else None
        new["safety_recent"] = round(recent, 1) if recent is not None else None
        new["safety_grade"] = grade
        new["safety_trend"] = trend
        new["safety_task_level"] = task
        new["safety_declining"] = bool(declining)
        new["safety_excluded"] = excluded
        new["safety_note"] = note
        new["dispatch_score"] = round(base * factor, 2)
        graded.append(new)

    # 排序：車況排除沉底 -> 安全排除沉底 -> 綜合分數升冪。
    graded.sort(key=lambda o: (o.get("health_excluded", False), o["safety_excluded"], o["dispatch_score"]))
    return graded


def _task_level(distance_km: float) -> str:
    """里程 -> 任務級別（與 dispatch_health 的里程界線一致：heavy>=80, light<=15）。"""
    if distance_km >= 80.0:
        return TASK_HEAVY
    if distance_km <= 15.0:
        return TASK_LIGHT
    return TASK_STANDARD


_TASK_LABELS = {TASK_LIGHT: "短途", TASK_STANDARD: "一般", TASK_HEAVY: "長途"}


def _task_label(task: str) -> str:
    return _TASK_LABELS.get(task, task)
