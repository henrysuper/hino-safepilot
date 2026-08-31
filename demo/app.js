"use strict";

class DataProvider {
  async load(_viewName) {
    throw new Error("DataProvider.load must be implemented");
  }
}

class StaticJsonProvider extends DataProvider {
  constructor(basePath = "./sample-data") {
    super();
    this.basePath = basePath;
  }

  async load(viewName) {
    const response = await fetch(`${this.basePath}/${viewName}.synthetic.json`, {
      cache: "no-store",
    });
    if (!response.ok) {
      throw new Error(`Synthetic contract unavailable: ${viewName}`);
    }
    return response.json();
  }
}

const provider = new StaticJsonProvider();
const requiredFields = {
  predrive: ["prototype_marker", "daily_improvement_goal", "message"],
  driving: ["prototype_marker", "model_status", "prediction_contract"],
  postdrive: ["prototype_marker", "trip_summary", "ai_report"],
};

function assertContract(viewName, data) {
  if (
    !data ||
    data.prototype_marker !== "SYNTHETIC_DEMO" ||
    data.data_classification !== "synthetic" ||
    data.view !== viewName
  ) {
    throw new Error("Only synthetic demo contracts are accepted");
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

function renderPredrive(data) {
  const goal = data.daily_improvement_goal;
  const evidence = goal.evidence_summary;
  setText("goal-title", data.display.target_label);
  setText("goal-message", data.message.explanation);
  setText("goal-action", data.message.action);
  setText("goal-priority", goal.priority_score.toFixed(2));
  setText("goal-change", `${evidence.change_pct > 0 ? "+" : ""}${evidence.change_pct.toFixed(1)}%`);
  setText("goal-recent", evidence.recent_rate.toFixed(1));
  setText("goal-baseline", evidence.baseline_rate.toFixed(1));
  setText("goal-unit", data.display.rate_unit_label);
  document.getElementById("goal-progress").style.width = `${goal.priority_score * 100}%`;
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

function renderPostdrive(data) {
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
    `行程約 ${context.trip_progress_pct}% 處出現${context.event_label}；事件前 30 秒平均速度為 ${context.previous_speed_mean_kph} km/h，之後呈現${context.next_behavior_label}。`
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
    const [predrive, driving, postdrive] = await Promise.all(
      ["predrive", "driving", "postdrive"].map(async (viewName) => {
        const data = await provider.load(viewName);
        assertContract(viewName, data);
        return data;
      })
    );
    renderPredrive(predrive);
    renderDriving(driving);
    renderPostdrive(postdrive);
  } catch (error) {
    console.error("SafePilot synthetic demo load failed", error);
    showError();
  }
}

document.addEventListener("DOMContentLoaded", initialize);
