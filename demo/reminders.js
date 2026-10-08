"use strict";

// Presentation-only contracts: never infer a future event category from outcome labels.
(function (root) {
  const categories = Object.freeze({
    forward: { title: "注意前方", icon: "↔", text: "請注意前方，放慢車速，保持安全車距。" },
    lane: { title: "注意車道", icon: "⇢", text: "請注意車道位置與周圍車況。" },
    attention: { title: "專注駕駛", icon: "◎", text: "請將注意力回到道路。" },
    fatigue: { title: "建議休息", icon: "☾", text: "偵測到疲勞警示，請於安全處停車休息。" },
    general: { title: "風險升高", icon: "△", text: "行駛風險升高，請留意路況。" },
  });
  const eventCategories = Object.freeze({ pcs: "forward", ldws: "lane", distract: "attention", phone: "attention", fatigue: "fatigue" });

  function prediction(minute, code) {
    return { minute, category: "general", source: "model_prediction", escalation: code === 2, ...categories.general };
  }

  function makeScenario(scenario, thresholdPct = 50) {
    const risks = [25, 40, 50, 62, 58, 45, 35, 30, 25];
    const points = risks.map((risk, index) => [index / 6, risk, index === 2 ? 1 : index >= 3 && index <= 6 ? 3 : index === 7 ? 4 : 0, null, null, null]);
    const minute = points[2][0];
    return {
      is_reminder_scenario: true, synthetic_threshold_pct: thresholdPct, points,
      detected_events: scenario.event_code ? [{ minute, event_code: scenario.event_code }] : [],
      events: scenario.event_code ? [{ minute, label: scenario.label }] : [],
      alerts: scenario.event_code ? [] : [{ minute, risk_pct: risks[2], escalation: false, event_label: null, lead_seconds: null }],
    };
  }

  function notifications(timeline, now) {
    const predicted = timeline.points.filter((p) => p[0] <= now && (p[2] === 1 || p[2] === 2))
      .map((p) => prediction(p[0], p[2]));
    // Explicit synthetic detections demonstrate the contract without inventing detectors.
    const detected = timeline.is_reminder_scenario === true
      ? (timeline.detected_events ?? []).filter((e) => e.minute <= now && eventCategories[e.event_code])
        .map((e) => {
          const threshold = timeline.synthetic_threshold_pct ?? 50;
          const crossing = timeline.points.find((p) => p[0] >= e.minute && p[0] <= now && p[1] >= threshold);
          if (!crossing) return null;
          const category = eventCategories[e.event_code];
          return { minute: crossing[0], category, source: "detected_event", ...categories[category] };
        }).filter(Boolean) : [];
    const generic = timeline.is_reminder_scenario
      ? predicted.filter((n) => !detected.some((e) => e.minute === n.minute)) : predicted;
    return [...generic, ...detected].sort((a, b) => a.minute - b.minute || Number(a.source === "detected_event") - Number(b.source === "detected_event"));
  }

  function frameNotifications(timeline, index) {
    const now = timeline.points[index][0];
    const previous = index ? timeline.points[index - 1][0] : -Infinity;
    const arrived = notifications(timeline, now).filter((n) => n.minute > previous);
    // Demonstration arbitration only: an explicit event supersedes a generic voice in this frame.
    const detected = arrived.filter((n) => n.source === "detected_event");
    const selected = detected.length ? detected : arrived;
    return [...new Map(selected.map((n) => [n.category, n])).values()];
  }

  root.SafePilotReminders = Object.freeze({ categories, prediction, makeScenario, notifications, frameNotifications });
})(globalThis);
