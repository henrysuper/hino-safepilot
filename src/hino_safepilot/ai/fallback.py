"""Traditional-Chinese deterministic rendering when an LLM is absent or fails."""

from __future__ import annotations

from .models import (
    AIDailyGoalMessage,
    AITripReport,
    SanitizedDailyGoalPayload,
    SanitizedTripReportPayload,
)


LABELS = {
    "accelerate": "急加速",
    "decelerate": "急減速",
    "speeding": "超速",
    "engine_overloading": "引擎過載",
    "idle": "怠速",
    "ldws": "車道偏移警示",
    "pcs": "預警式防護系統事件",
    "fatigue": "疲勞事件",
    "distract": "分心事件",
    "phone": "使用手機事件",
    "fuel_efficiency": "燃油效率",
}


class DeterministicReportRenderer:
    def trip_report(self, payload: SanitizedTripReportPayload) -> AITripReport:
        distance = payload.distance_km.value
        duration = payload.duration_minutes.value
        summary = "本趟行程的可用分析已完成。"
        if distance is not None and duration is not None:
            summary = f"本趟約 {distance:.1f} 公里、{duration:.0f} 分鐘；數值由確定性分析計算。"
        strongest = LABELS.get(payload.strongest_behavior or "", payload.strongest_behavior or "資料不足")
        weakest = LABELS.get(payload.weakest_behavior or "", payload.weakest_behavior or "資料不足")
        return AITripReport(
            summary=summary,
            strengths=[f"相對較佳項目：{strongest}"],
            weaknesses=[f"優先留意項目：{weakest}"],
            primary_risk=payload.weakest_behavior or "insufficient_data",
            recommendations=["依事件頻率與資料品質檢視下一趟可改善的操作。"],
            next_trip_focus=f"下一趟優先留意：{weakest}。",
        )

    def daily_goal(self, payload: SanitizedDailyGoalPayload) -> AIDailyGoalMessage:
        label = LABELS.get(payload.target_metric, payload.target_metric)
        return AIDailyGoalMessage(
            target_metric=payload.target_metric,
            title=f"今日目標：改善{label}",
            explanation="此目標由過去行程的確定性指標與車輛連結基線選出。",
            action=f"下一趟行程請優先留意{label}，並採取平順、預留反應時間的操作。",
        )
