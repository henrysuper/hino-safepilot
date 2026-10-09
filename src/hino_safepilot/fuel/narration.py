"""LLM wording for fuel reports. The LLM never computes numbers.

The fact JSON goes to an optional provider (Gemini, needs GEMINI_API_KEY and
``pip install -e .[ai]``). The reply is rejected if it contains a number that
is not in the facts, is too long, or the call fails; then a deterministic
Traditional-Chinese template is used.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

NUMBER = re.compile(r"\d+(?:\.\d+)?")

PROMPT = """你是商用車油耗教練，對象是駕駛本人。以下 FACTS_JSON 是模型已算好的單趟油耗分析。
規則：
1. 只能使用 FACTS_JSON 內的數字，不可自行計算、換算或新增數字；百分比與分數寫成整數（例如 43%、32 分）。
2. 繁體中文，口語、鼓勵，120 字以內，一段話，不要條列、不要括號註解欄位。
3. 不可出現英文欄位名稱或代碼（例如 highway、actual、typical）；道路請用 road_band_label，行為請用 label。
4. 若 score.vs_expected_pct > 0：先說多用幾 %、分數幾分，再說超用最多的路段（worst_segments 第一個、其 main_behaviour 與 advice）。
   若 <= 0：只說少用幾 %、分數幾分並鼓勵保持，不要談超用路段。
5. 若 vehicle_health.low 為 true，加一句可能與車況有關；若 money.excess_twd 有值，說明金額並註明「此為模擬資料」。"""


DISPATCH_PROMPT = """你是車隊派車顧問，對象是派車組員。以下 FACTS_JSON 是模型已算好的單程派車推薦。
規則：
1. 只能使用 FACTS_JSON 內的數字，不可自行計算、換算或新增數字；百分比與分數寫成整數（例如 12%、78 分）。
2. 繁體中文，專業、精簡，120 字以內，一段話，不要條列、不要括號註解欄位。
3. 不可出現英文欄位名稱或代碼（例如 highway、normal、dispatch_fuel_L）；道路用 main_road_band_label，駕駛風格用 recommended.driver_type_label。
4. 先說建議派這台車（車系 recommended.vehicle_type 系、駕駛風格），預測整趟油耗 recommended.predicted_fuel_L 公升、約 recommended.minutes 分鐘。
5. 若 runner_up 有值，說比次佳省 runner_up.delta_L 公升（runner_up.delta_pct%）。
6. 若 health_overridden 為 true：說明最省油的車車況為 cheapest_health_grade_label，所以改派車況較佳者；否則若 recommended.health_grade 不是良好，點出車況為 recommended.health_grade_label 並已計入風險成本。
7. 可用一句描述路線：主要走 main_road_band_label；若 congested_count 大於 0，提一句沿途有壅塞路段。結尾註明駕駛與車牌為模擬資料。"""


def _litres(value) -> str:
    """Faithful litre wording: one decimal under 10 L so small trips never read as 0."""

    v = float(value or 0)
    return f"{v:.1f}" if 0 < v < 10 else f"{v:.0f}"


def template_dispatch_report(facts: dict) -> str:
    rec = facts["recommended"]
    parts = []
    where = ""
    if facts.get("origin_name") and facts.get("destination_name"):
        where = f"{facts['origin_name']}到{facts['destination_name']}這趟，"
    parts.append(
        f"{where}建議派 {rec['vehicle_type']} 系車、{rec['driver_type_label']}型駕駛，"
        f"預測整趟油耗 {_litres(rec['predicted_fuel_L'])} 公升、約 {rec['minutes']:.0f} 分鐘。"
    )
    runner = facts.get("runner_up")
    if runner and runner.get("delta_L"):
        parts.append(f"比次佳選擇省 {runner['delta_L']:.1f} 公升（約 {runner['delta_pct']:.0f}%）。")
    safety = facts.get("safety") or {}
    # 具名駕駛模式：優先講駕駛安全（因為派車是以具名駕駛為單位）。
    if safety:
        if safety.get("safety_overridden") and safety.get("cheapest_driver_name"):
            parts.append(f"油耗最省的駕駛安全分較低，已改派安全分較佳的 {safety['driver_name']}。")
        elif safety.get("safety_grade") and safety["safety_grade"] not in ("ok", "unknown"):
            parts.append(f"建議駕駛安全分為「{safety.get('safety_grade_label')}」，已計入安全風險成本後仍最適合。")
        elif safety.get("safety_declining"):
            parts.append("建議駕駛近期安全分略有退步，已納入考量。")
    elif facts.get("health_overridden") and facts.get("cheapest_health_grade_label"):
        parts.append(f"最省油的車車況為「{facts.get('cheapest_health_grade_label')}」，已改派車況較佳的車。")
    elif rec.get("health_grade") and rec["health_grade"] != "ok":
        parts.append(f"這台車車況為「{rec['health_grade_label']}」，已計入車況風險成本後仍最省。")
    if facts.get("main_road_band_label"):
        tail = f"路線主要走{facts['main_road_band_label']}"
        if facts.get("congested_count"):
            tail += "，沿途有壅塞路段需留意"
        parts.append(tail + "。")
    parts.append("駕駛與車牌為模擬資料。")
    return "".join(parts)


def _numbers(value, out: set[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        out.add(abs(float(value)))
    elif isinstance(value, str):
        out.update(float(n) for n in NUMBER.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            _numbers(item, out)
    elif isinstance(value, list):
        for item in value:
            _numbers(item, out)


def unsupported_numbers(text: str, facts: dict) -> list[str]:
    allowed: set[float] = {float(n) for n in range(0, 11)}
    _numbers(facts, allowed)
    bad = []
    for token in NUMBER.findall(text):
        decimals = len(token.split(".")[1]) if "." in token else 0
        tolerance = 0.5 * 10 ** (-decimals) + 1e-9
        if not any(abs(float(token) - a) <= tolerance for a in allowed):
            bad.append(token)
    return bad


def template_trip_report(facts: dict) -> str:
    score = facts["score"]
    pct = score["vs_expected_pct"]
    direction = f"多用 {pct:.0f}%" if pct is not None and pct > 0 else f"少用 {abs(pct or 0):.0f}%"
    lines = [f"這趟比一般表現{direction}，分數 {score['fuel_score']:.0f} 分。"]
    overused = pct is not None and pct > 0
    worst = [w for w in facts["worst_segments"] if w["main_behaviour"]]
    if overused and worst:
        w = worst[0]
        band = {"city": "市區", "normal": "一般道路", "highway": "快速/高速"}.get(w["road_band"], w["road_band"])
        nos = str(w["segment_no"])
        b = w["main_behaviour"]
        times = f"是一般駕駛的 {b['times_typical']} 倍" if b["times_typical"] and b["times_typical"] <= 20 else "明顯高於一般駕駛"
        lines.append(f"主要在第 {nos} 段（{band}），{b['label']}{times}。{w['advice']}")
    elif overused:
        lines.append("超用沒有集中在特定駕駛行為，可能與載重或路況變化有關。")
    else:
        lines.append("油耗低於同路線、同時段一般駕駛的水準，請繼續保持。")
    if facts["vehicle_health"]["low"]:
        lines.append(facts["vehicle_health"]["note"])
    money = facts["money"]
    if money["excess_twd"]:
        lines.append(f"超用油量約 {money['excess_twd']:.0f} 元（{money['mock_label']}）。")
    return "".join(lines)


@dataclass
class Narration:
    text: str
    generation_mode: str
    error: str | None = None


class Narrator:
    def __init__(self, provider: str | None = None, max_chars: int = 220):
        # "auto" (default): Gemini when GEMINI_API_KEY is configured, otherwise the template.
        chosen = (provider or os.environ.get("LLM_PROVIDER", "auto")).lower()
        if chosen in ("auto", "mock"):
            chosen = "gemini" if os.environ.get("GEMINI_API_KEY") else "none"
        self.provider = chosen
        self.max_chars = max_chars

    def _gemini(self, facts: dict, prompt: str = PROMPT) -> str:
        from google import genai  # optional dependency
        from google.genai import types

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=10000))
        response = client.models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            contents=f"{prompt}\n\nFACTS_JSON:\n{json.dumps(facts, ensure_ascii=False)}",
            config=types.GenerateContentConfig(temperature=0.2),
        )
        return (response.text or "").strip()

    def _generate(self, facts: dict, prompt: str, template: str) -> Narration:
        """Shared LLM-or-template path: the LLM may only reword, never add numbers."""

        error = None
        if self.provider == "gemini" and os.environ.get("GEMINI_API_KEY"):
            try:
                text = self._gemini(facts, prompt)
                bad = unsupported_numbers(text, facts)
                english = re.findall(r"[A-Za-z_]{4,}", text)  # leaked field names such as highway / typical
                if text and len(text) <= self.max_chars and not bad and not english:
                    return Narration(text, "llm")
                error = f"rejected: numbers={bad[:3]} english={english[:3]} length={len(text)}"
            except Exception as exc:  # provider boundary
                error = type(exc).__name__
        return Narration(template, "deterministic_template", error)

    def trip_report(self, facts: dict) -> Narration:
        return self._generate(facts, PROMPT, template_trip_report(facts))

    def dispatch_report(self, facts: dict) -> Narration:
        """單程派車推薦理由（最佳車輛/路線為何這樣選）。"""

        return self._generate(facts, DISPATCH_PROMPT, template_dispatch_report(facts))
