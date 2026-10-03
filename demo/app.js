"use strict";

class DataProvider {
  async load(_viewName) {
    throw new Error("DataProvider.load must be implemented");
  }
}

class StaticJsonProvider extends DataProvider {
  constructor(basePath = "./sample-data", suffix = "synthetic") {
    super();
    this.basePath = basePath;
    this.suffix = suffix;
  }

  async load(viewName) {
    const response = await fetch(`${this.basePath}/${viewName}.${this.suffix}.json`, {
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error(`Demo contract unavailable: ${viewName}`);
    }
    return response.json();
  }
}

// ?source=local reads private views exported by `hino-analytics export-demo`, served
// only by scripts/serve_demo.py. The driving view always stays synthetic.
const useLocalData = new URLSearchParams(window.location.search).get("source") === "local";
const syntheticProvider = new StaticJsonProvider();
const localProvider = new StaticJsonProvider("./local-data", "local");
const localViews = new Set(["predrive", "postdrive"]);
const contractMarkers = {
  synthetic: { prototype_marker: "SYNTHETIC_DEMO", data_classification: "synthetic" },
  local: { prototype_marker: "LOCAL_PRIVATE_DERIVED", data_classification: "private_local" },
};
const requiredFields = {
  predrive: ["prototype_marker", "daily_improvement_goal", "message", "briefing"],
  driving: ["prototype_marker", "model_status", "prediction_contract"],
  postdrive: ["prototype_marker", "trip_summary", "ai_report", "safety_score"],
};

function sourceFor(viewName) {
  return useLocalData && localViews.has(viewName) ? "local" : "synthetic";
}

function assertContract(viewName, data, source) {
  const marker = contractMarkers[source];
  if (
    !data ||
    data.prototype_marker !== marker.prototype_marker ||
    data.data_classification !== marker.data_classification ||
    data.view !== viewName
  ) {
    throw new Error(`Only ${source} demo contracts are accepted here`);
  }
  for (const field of requiredFields[viewName]) {
    if (!(field in data)) throw new Error(`Missing required field: ${field}`);
  }
  if (viewName === "driving" && data.prediction_contract.is_simulation !== true) {
    throw new Error("Driving preview must be explicitly marked as simulation");
  }
}

function valueOrUnavailable(value, suffix = "") {
  return value === null || value === undefined || Number.isNaN(value)
    ? "暫無資料"
    : `${value}${suffix}`;
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = valueOrUnavailable(value);
}

function gradeClass(grade) {
  return grade ? `grade-badge grade-${grade}` : "grade-badge";
}

function scoreTone(score) {
  if (score === null || score === undefined || score >= 80) return "";
  return score >= 60 ? "mid" : "low";
}

function renderBriefing(briefing) {
  setText("brief-last-score", briefing.last_trip_score);
  const lastGrade = document.getElementById("brief-last-grade");
  lastGrade.textContent = briefing.last_trip_grade ?? "—";
  lastGrade.className = gradeClass(briefing.last_trip_grade);
  setText("brief-recent-label", `近 ${briefing.recent_days} 天平均`);
  setText("brief-recent-score", briefing.recent_average_score);
  const trend = document.getElementById("brief-trend");
  trend.textContent = briefing.trend_label;
  trend.className = `trend-chip trend-${briefing.trend}`;
  setText("brief-weakest", briefing.weakest_category_label);

  const list = document.getElementById("reminder-list");
  list.replaceChildren();
  for (const reminder of briefing.reminders) {
    const item = document.createElement("li");
    item.className = reminder.priority;
    item.textContent = reminder.message;
    list.append(item);
  }
}

function renderPredrive(data) {
  renderBriefing(data.briefing);
  setText("goal-title", data.display.target_label);
  if (!data.daily_improvement_goal || !data.message) {
    setText("goal-message", "近期資料不足以選出改善目標，今天以平穩駕駛為主。");
    setText("goal-action", "保持安全車距、依速限行駛。");
    return;
  }
  setText("goal-message", data.message.explanation);
  setText("goal-action", data.message.action);
}

function renderDriving(data) {
  const contract = data.prediction_contract;
  const score = contract.risk_probability;
  setText("risk-score", `${Math.round(score * 100)}%`);
  setText("risk-level", contract.risk_level);
  setText("risk-event", contract.predicted_events.join("、"));
  setText("risk-horizon", `${contract.prediction_horizon_sec} 秒`);
  setText("risk-model", contract.model_version);
  setText("risk-label", data.research_preview.candidate_label);
  setText("risk-cadence", data.research_preview.anchor_cadence);
  document.getElementById("risk-dial").style.setProperty("--score", `${score * 360}deg`);

  const factors = document.getElementById("risk-factors");
  factors.replaceChildren();
  for (const factor of contract.top_factors) {
    const item = document.createElement("div");
    item.className = "factor-item";
    const title = document.createElement("strong");
    title.textContent = factor.label;
    const detail = document.createElement("span");
    detail.textContent = factor.note;
    item.append(title, detail);
    factors.append(item);
  }

  const timeline = document.getElementById("alert-timeline");
  timeline.replaceChildren();
  for (const point of data.alert_policy_timeline) {
    const item = document.createElement("div");
    item.className = `timeline-item ${point.should_speak ? "speaks" : "silent"}`;
    const time = document.createElement("strong");
    time.textContent = `T+${point.relative_seconds}s · ${Math.round(point.risk_probability * 100)}%`;
    const decision = document.createElement("span");
    decision.textContent = point.decision_label;
    item.append(time, decision);
    timeline.append(item);
  }
}

function renderSafetyScore(score) {
  const ring = document.getElementById("score-ring");
  const grade = document.getElementById("score-grade");
  if (score.status !== "scored") {
    setText("score-overall", "—");
    grade.textContent = "—";
    grade.className = "grade-badge large";
    setText("score-note", "本趟行駛距離太短，不計算安全分數。");
    ring.style.setProperty("--score", "0deg");
  } else {
    const tone = scoreTone(score.overall_score);
    setText("score-overall", score.overall_score);
    grade.textContent = score.grade;
    grade.className = `grade-badge large grade-${score.grade}`;
    ring.style.setProperty("--score", `${(score.overall_score / 100) * 360}deg`);
    ring.style.setProperty(
      "--ring",
      tone === "low" ? "var(--red)" : tone === "mid" ? "var(--amber)" : "var(--green)"
    );
    setText(
      "score-note",
      score.grade === "A" || score.grade === "B"
        ? "表現穩定，繼續保持。"
        : "有明確可改善的項目，請看右側扣分原因。"
    );
  }

  const bars = document.getElementById("category-bars");
  bars.replaceChildren();
  for (const category of score.categories) {
    const row = document.createElement("div");
    row.className = "category-bar";
    const label = document.createElement("span");
    label.textContent = category.label;
    const track = document.createElement("div");
    track.className = "bar-track";
    const fill = document.createElement("i");
    fill.className = scoreTone(category.score);
    fill.style.width = `${category.score ?? 0}%`;
    track.append(fill);
    const value = document.createElement("strong");
    value.textContent = category.score ?? "—";
    row.append(label, track, value);
    bars.append(row);
  }

  const deductions = document.getElementById("deduction-list");
  deductions.replaceChildren();
  if (!score.top_deductions.length) {
    const item = document.createElement("li");
    item.textContent =
      score.status === "scored" ? "本趟沒有明顯扣分項目。" : "距離不足，無扣分明細。";
    deductions.append(item);
  }
  for (const deduction of score.top_deductions) {
    const item = document.createElement("li");
    const text = document.createElement("span");
    text.textContent = `${deduction.label}（${deduction.category_label}）· ${deduction.observed_value} ${deduction.unit_label}`;
    if (deduction.definition) {
      const definition = document.createElement("small");
      definition.className = "deduction-definition";
      definition.textContent = `定義：${deduction.definition}`;
      text.append(definition);
    }
    const points = document.createElement("strong");
    points.textContent = `-${deduction.points_lost} 分`;
    item.append(text, points);
    deductions.append(item);
  }
  const notes = score.applied_caps.map(
    (cap) => `本趟出現${cap.label}事件，總分上限為 ${cap.cap} 分。`
  );
  if (score.dms_reliability === "camera_blocked") {
    notes.push("車內鏡頭曾被遮蔽，疲勞、手機、分心等偵測可能偏少。");
  }
  const caps = document.getElementById("score-caps");
  caps.textContent = notes.join(" ");
  caps.classList.toggle("score-warning", notes.length > 0);
}

function renderPostdrive(data, source) {
  renderSafetyScore(data.safety_score);
  setText(
    "ai-label",
    source === "local" ? `AI 行程摘要 · ${data.generation_mode}` : "AI 行程摘要 · Synthetic"
  );
  const summary = data.trip_summary;
  const metrics = [
    ["行駛距離", summary.distance_km, " km"],
    ["行程時間", summary.duration_minutes, " min"],
    ["燃油使用", summary.fuel_used_l, " L"],
    ["燃油效率", summary.fuel_efficiency_km_per_l, " km/L"],
  ];
  const metricContainer = document.getElementById("trip-metrics");
  metricContainer.replaceChildren();
  for (const [label, value, suffix] of metrics) {
    const card = document.createElement("div");
    card.className = "trip-metric";
    const labelElement = document.createElement("span");
    labelElement.textContent = label;
    const valueElement = document.createElement("strong");
    valueElement.textContent = valueOrUnavailable(value, suffix);
    card.append(labelElement, valueElement);
    metricContainer.append(card);
  }

  const eventList = document.getElementById("event-list");
  eventList.replaceChildren();
  for (const event of summary.event_metrics) {
    const row = document.createElement("div");
    row.className = "event-row";
    const name = document.createElement("span");
    name.textContent = event.label;
    const count = document.createElement("strong");
    count.textContent = `${event.count} 次`;
    row.append(name, count);
    eventList.append(row);
  }
  setText("trip-strength", summary.strongest_behavior_label);
  setText("trip-weakness", summary.weakest_behavior_label);
  setText("ai-summary", data.ai_report.summary);
  setText("next-focus", data.ai_report.next_trip_focus);
  const context = summary.notable_event;
  setText(
    "event-context",
    context
      ? `行程約 ${context.trip_progress_pct}% 處出現${context.event_label}；事件前 30 秒平均速度為 ${valueOrUnavailable(context.previous_speed_mean_kph, " km/h")}，之後呈現${context.next_behavior_label}。`
      : "本趟沒有需要特別說明的事件。"
  );
}

function showError() {
  document.getElementById("global-error").classList.remove("hidden");
}

function activateView(viewName) {
  document.querySelectorAll(".view").forEach((view) => {
    view.classList.toggle("active", view.id === `${viewName}-view`);
  });
  document.querySelectorAll(".stage-button").forEach((button) => {
    const active = button.dataset.view === viewName;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
}

async function initialize() {
  document.querySelectorAll(".stage-button").forEach((button) => {
    button.addEventListener("click", () => activateView(button.dataset.view));
  });
  try {
    document.getElementById("local-banner").classList.toggle("hidden", !useLocalData);
    const [predrive, driving, postdrive] = await Promise.all(
      ["predrive", "driving", "postdrive"].map(async (viewName) => {
        const source = sourceFor(viewName);
        const provider = source === "local" ? localProvider : syntheticProvider;
        const data = await provider.load(viewName);
        assertContract(viewName, data, source);
        return data;
      })
    );
    renderPredrive(predrive);
    renderDriving(driving);
    renderPostdrive(postdrive, sourceFor("postdrive"));
  } catch (error) {
    console.error("SafePilot synthetic demo load failed", error);
    showError();
  }
}

document.addEventListener("DOMContentLoaded", initialize);
