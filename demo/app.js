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
// One simulated driver is selected at a time; pre-drive, post-drive and history all follow it.
const params = new URLSearchParams(window.location.search);
const useLocalData = params.get("source") === "local";
const syntheticProvider = new StaticJsonProvider();
const localProvider = new StaticJsonProvider("./local-data", "local");
const state = { source: "synthetic", index: null, driver: null, reports: new Map(), drivingSample: null };
const contractMarkers = {
  synthetic: { prototype_marker: "SYNTHETIC_DEMO", data_classification: "synthetic" },
  local: { prototype_marker: "LOCAL_PRIVATE_DERIVED", data_classification: "private_local" },
};
const requiredFields = {
  predrive: ["prototype_marker", "daily_improvement_goal", "message", "briefing"],
  driving: ["prototype_marker", "model_status", "prediction_contract"],
  postdrive: ["prototype_marker", "trip_summary", "ai_report", "safety_score"],
  drivers: ["prototype_marker", "mock_note", "drivers"],
  driver: ["prototype_marker", "predrive", "reports"],
};

async function loadView(name, source) {
  const data = await (source === "local" ? localProvider : syntheticProvider).load(name);
  assertContract(name.startsWith("driver-") ? "driver" : name, data, source);
  return data;
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

function riskLevel(pct, thresholds) {
  if (pct >= thresholds.high * 100) return "高風險";
  if (pct >= thresholds.elevated * 100) return "需留意";
  return "低風險";
}

// Single series (risk per 10-second anchor) with policy thresholds, alert dots and event marks.
function renderRiskChart(container, timeline, thresholds) {
  container.replaceChildren();
  const width = 640, height = 230, left = 34, right = 12, top = 12, bottom = 28;
  const points = timeline.points;
  const maxMinute = Math.max(points[points.length - 1][0], ...timeline.events.map((e) => e.minute), 1);
  const minMinute = Math.min(points[0][0], 0);
  const x = (minute) => left + ((minute - minMinute) / (maxMinute - minMinute)) * (width - left - right);
  const y = (pct) => top + (1 - pct / 100) * (height - top - bottom);
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  for (const value of [0, 50, 100]) {
    const label = svgElement("text", { class: "axis-label", x: left - 6, y: y(value) + 4, "text-anchor": "end" });
    label.textContent = `${value}%`;
    svg.append(label);
  }
  svg.append(svgElement("line", { class: "threshold", x1: left, x2: width - right, y1: y(thresholds.policy_enter * 100), y2: y(thresholds.policy_enter * 100) }));
  svg.append(svgElement("line", { class: "threshold reset", x1: left, x2: width - right, y1: y(thresholds.policy_reset * 100), y2: y(thresholds.policy_reset * 100) }));
  for (const [minute, anchor] of [[minMinute, "start"], [maxMinute, "end"]]) {
    const label = svgElement("text", { class: "axis-label", x: x(minute), y: height - 8, "text-anchor": anchor });
    label.textContent = `第 ${Math.round(minute)} 分鐘`;
    svg.append(label);
  }
  svg.append(svgElement("polyline", { class: "risk-line", points: points.map(([m, pct]) => `${x(m)},${y(pct)}`).join(" ") }));
  for (const event of timeline.events) {
    const cx = x(event.minute), cy = y(0) - 2;
    svg.append(svgElement("path", { class: "event-mark", d: `M${cx - 5},${cy} L${cx + 5},${cy} L${cx},${cy - 8} Z` }));
  }
  for (const alert of timeline.alerts) {
    svg.append(svgElement("circle", { class: "alert-dot", cx: x(alert.minute), cy: y(alert.risk_pct), r: 5 }));
  }
  const hover = svgElement("line", { class: "hover-line hidden", y1: top, y2: height - bottom });
  svg.append(hover);
  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip hidden";
  svg.addEventListener("mousemove", (event) => {
    const box = svg.getBoundingClientRect();
    const minute = minMinute + ((event.clientX - box.left) / box.width * width - left) / (width - left - right) * (maxMinute - minMinute);
    let nearest = points[0];
    for (const point of points) if (Math.abs(point[0] - minute) < Math.abs(nearest[0] - minute)) nearest = point;
    const alert = timeline.alerts.find((a) => Math.abs(a.minute - nearest[0]) < 0.1);
    hover.setAttribute("x1", x(nearest[0]));
    hover.setAttribute("x2", x(nearest[0]));
    hover.classList.remove("hidden");
    tooltip.textContent = `第 ${nearest[0].toFixed(1)} 分鐘 · 預測風險 ${nearest[1]}%${alert ? " · 系統會提醒" : ""}`;
    tooltip.style.left = `${(x(nearest[0]) / width) * 100}%`;
    tooltip.style.top = `${(y(nearest[1]) / height) * 100}%`;
    tooltip.classList.remove("hidden");
  });
  svg.addEventListener("mouseleave", () => { hover.classList.add("hidden"); tooltip.classList.add("hidden"); });
  const legend = document.createElement("div");
  legend.className = "chart-legend";
  legend.innerHTML = '<span><i class="line"></i>預測風險</span><span><i class="dash"></i>提醒門檻 50%（橘線 40% 為解除）</span><span><i class="alert"></i>系統會提醒</span><span><i class="event"></i>實際發生事件</span>';
  container.append(legend, svg, tooltip);
}

function renderDrivingTrip(report, info) {
  const timeline = report.risk_timeline;
  const thresholds = info.thresholds;
  setText("driving-title", "這趟的 60 秒風險回放");
  setText("driving-for", `${state.driver.driver_name} · 第 ${report.ref} 趟 · ${report.date} · ${report.plate ?? "—"}`);
  setText(
    "driving-disclosure",
    `Evaluation Mode: Offline replay。用凍結模型 ${info.model_version} 與提醒規則 ${info.policy_version} 重播這趟行程；資料錄製時還沒有 SafePilot，駕駛當時沒有聽到提醒，未連接即時車輛。資料期間：${timeline.split_label}。`
  );
  setText("driving-badge", state.source === "local" ? "Offline Replay" : "Synthetic Demo");
  const peak = timeline.peak;
  setText("risk-score", `${peak.risk_pct}%`);
  setText("risk-dial-caption", "這趟最高風險");
  setText("risk-level", riskLevel(peak.risk_pct, thresholds));
  setText("risk-event", `出現在第 ${peak.minute.toFixed(1)} 分鐘`);
  document.getElementById("risk-dial").style.setProperty("--score", `${(peak.risk_pct / 100) * 360}deg`);
  setText("risk-horizon", "60 秒");
  setText("risk-model", info.model_version);
  setText("risk-label", "Core Any Risk · Label V0");
  setText("risk-cadence", "每 10 秒預測一次");

  document.getElementById("risk-chart-panel").classList.remove("hidden");
  renderRiskChart(document.getElementById("risk-chart"), timeline, thresholds);
  setText(
    "risk-chart-note",
    `共 ${timeline.points.length} 個預測點；系統會提醒 ${timeline.alerts.length} 次，這趟發生 ${timeline.events.length} 次核心風險事件（急減速、PCS、車道偏移、疲勞、分心）。`
  );

  setText("factor-label", "整體模型最重要的因素（關聯，非因果）");
  const factors = document.getElementById("risk-factors");
  factors.replaceChildren();
  for (const factor of info.top_factors) {
    const item = document.createElement("div");
    item.className = "factor-item";
    const title = document.createElement("strong");
    title.textContent = factor.label;
    const detail = document.createElement("span");
    detail.textContent = `${factor.family} · 重要度 ${factor.importance_pct}%`;
    item.append(title, detail);
    factors.append(item);
  }

  setText("timeline-label", `Alert Policy V1 · 這趟的提醒決定（${timeline.alerts.length} 次）`);
  const list = document.getElementById("alert-timeline");
  list.replaceChildren();
  if (!timeline.alerts.length) {
    const item = document.createElement("div");
    item.className = "timeline-item silent";
    const title = document.createElement("strong");
    title.textContent = `全程最高 ${peak.risk_pct}%`;
    const detail = document.createElement("span");
    detail.textContent = `沒有超過 ${Math.round(thresholds.policy_enter * 100)}% 提醒門檻，系統全程不需要出聲`;
    item.append(title, detail);
    list.append(item);
  }
  for (const alert of timeline.alerts) {
    const item = document.createElement("div");
    item.className = "timeline-item speaks";
    const title = document.createElement("strong");
    title.textContent = `第 ${alert.minute.toFixed(1)} 分鐘 · ${alert.risk_pct}%`;
    const detail = document.createElement("span");
    const decision = alert.escalation ? "風險再升高，升級提醒一次" : "超過門檻，開始一段新風險，播報";
    detail.textContent = `${decision} · ${alert.event_label ? `${alert.lead_seconds} 秒後發生${alert.event_label}` : "60 秒內沒有發生事件"}`;
    item.append(title, detail);
    list.append(item);
  }
}

function showDrivingStatic() {
  document.getElementById("driving-toolbar").classList.add("hidden");
  document.getElementById("driving-for").classList.add("hidden");
  document.getElementById("risk-chart-panel").classList.add("hidden");
  setText("driving-title", "未來 60 秒風險契約預覽");
  setText("driving-disclosure", "Evaluation Mode: Offline。下方時間線與風險數值皆是 Synthetic Demo，未連接即時車輛或 production alert engine。");
  setText("driving-badge", "Synthetic Demo");
  setText("risk-dial-caption", "模擬機率");
  setText("factor-label", "模擬 top factors");
  setText("timeline-label", "Alert Policy V1 · Synthetic Timeline");
  if (state.drivingSample) renderDriving(state.drivingSample);
}

function setupDriving(data) {
  const info = state.index.model_info;
  const playable = data.reports.filter((report) => report.risk_timeline);
  const select = document.getElementById("driving-select");
  select.replaceChildren();
  if (!info || !playable.length) {
    showDrivingStatic();
    return;
  }
  for (const report of [...playable].reverse()) {
    const option = document.createElement("option");
    option.value = String(report.ref);
    option.textContent = `第 ${report.ref} 趟 · ${report.date} · 提醒 ${report.risk_timeline.alerts.length} 次`;
    select.append(option);
  }
  const byRef = new Map(playable.map((report) => [report.ref, report]));
  select.onchange = () => renderDrivingTrip(byRef.get(Number(select.value)), info);
  document.getElementById("driving-toolbar").classList.remove("hidden");
  document.getElementById("driving-for").classList.remove("hidden");
  renderDrivingTrip(playable[playable.length - 1], info);
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
  if (data.short_trip && data.safety_score.status === "scored") {
    setText(
      "score-note",
      `短程（${data.trip_summary.distance_km} km）：分數僅供參考，在平均分數中依公里數加權，權重很低。`
    );
  }
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

const SVG_NS = "http://www.w3.org/2000/svg";

function svgElement(name, attributes) {
  const element = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
  return element;
}

function categoryBarRows(container, rows) {
  container.replaceChildren();
  for (const [label, score] of rows) {
    const row = document.createElement("div");
    row.className = "category-bar";
    const name = document.createElement("span");
    name.textContent = label;
    const track = document.createElement("div");
    track.className = "bar-track";
    const fill = document.createElement("i");
    fill.className = scoreTone(score);
    fill.style.width = `${score ?? 0}%`;
    track.append(fill);
    const value = document.createElement("strong");
    value.textContent = score ?? "—";
    row.append(name, track, value);
    container.append(row);
  }
}

// Single-series line: score per scored trip in time order, grade thresholds as reference lines.
function renderScoreChart(container, trips) {
  container.replaceChildren();
  const points = trips.filter((trip) => trip.score !== null && trip.score !== undefined);
  if (points.length === 0) {
    const empty = document.createElement("p");
    empty.className = "chart-empty";
    empty.textContent = "尚無可評分的行程。";
    container.append(empty);
    return;
  }
  const width = 560, height = 220, left = 34, right = 12, top = 12, bottom = 26;
  const lowest = Math.min(...points.map((trip) => trip.score));
  const floor = Math.max(0, Math.min(50, Math.floor((lowest - 5) / 10) * 10));
  const x = (i) => left + (points.length === 1 ? (width - left - right) / 2 : (i / (points.length - 1)) * (width - left - right));
  const y = (score) => top + (1 - (score - floor) / (100 - floor)) * (height - top - bottom);
  const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  for (const threshold of [100, 90, 80, 70, 60, 50].filter((value) => value >= floor)) {
    svg.append(svgElement("line", { class: "grid-line", x1: left, x2: width - right, y1: y(threshold), y2: y(threshold) }));
    const label = svgElement("text", { class: "axis-label", x: left - 6, y: y(threshold) + 4, "text-anchor": "end" });
    label.textContent = threshold;
    svg.append(label);
  }
  const first = svgElement("text", { class: "axis-label", x: left, y: height - 6 });
  first.textContent = points[0].date;
  const last = svgElement("text", { class: "axis-label", x: width - right, y: height - 6, "text-anchor": "end" });
  last.textContent = points[points.length - 1].date;
  svg.append(first, last);
  svg.append(svgElement("polyline", {
    class: "score-line",
    points: points.map((trip, i) => `${x(i)},${y(trip.score)}`).join(" "),
  }));
  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip hidden";
  const step = points.length > 1 ? (width - left - right) / (points.length - 1) : width;
  points.forEach((trip, i) => {
    const dot = svgElement("circle", { class: "score-dot", cx: x(i), cy: y(trip.score), r: 4 });
    const hit = svgElement("rect", {
      class: "hit", x: x(i) - Math.max(step, 12) / 2, y: top, width: Math.max(step, 12), height: height - top - bottom,
    });
    const show = () => {
      dot.classList.add("active");
      tooltip.textContent = `第 ${trip.ref} 趟 · ${trip.date} · ${trip.score} 分（${trip.grade}）`;
      tooltip.style.left = `${(x(i) / width) * 100}%`;
      tooltip.style.top = `${(y(trip.score) / height) * 100}%`;
      tooltip.classList.remove("hidden");
    };
    const hide = () => { dot.classList.remove("active"); tooltip.classList.add("hidden"); };
    hit.addEventListener("mouseenter", show);
    hit.addEventListener("mouseleave", hide);
    svg.append(dot, hit);
  });
  container.append(svg, tooltip);
}

function renderTripDetail(trip) {
  const panel = document.getElementById("drv-trip-detail");
  panel.replaceChildren();
  if (!trip.detail) {
    panel.classList.add("hidden");
    return;
  }
  const title = document.createElement("h4");
  title.textContent = `第 ${trip.ref} 趟 · ${trip.date} · ${trip.score ?? "—"} 分`;
  const bars = document.createElement("div");
  bars.className = "category-bars";
  categoryBarRows(bars, trip.detail.categories.map((c) => [c.label, c.score]));
  const list = document.createElement("ul");
  list.className = "deduction-list";
  for (const deduction of trip.detail.top_deductions) {
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
    list.append(item);
  }
  if (!trip.detail.top_deductions.length) {
    const item = document.createElement("li");
    item.textContent = trip.detail.status === "scored" ? "本趟沒有明顯扣分項目。" : "距離太短，不計算安全分數。";
    list.append(item);
  }
  panel.append(title, bars, list);
  if (state.reports.has(trip.ref)) {
    const open = document.createElement("button");
    open.type = "button";
    open.className = "open-report";
    open.textContent = "開啟這趟的完整安全報告 →";
    open.addEventListener("click", () => {
      showReport(trip.ref);
      activateView("postdrive");
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    panel.append(open);
  }
  panel.classList.remove("hidden");
}

function renderDriver(driver) {
  setText("drv-rank", rankText(driver));
  setText(
    "drv-road",
    driver.road_adjusted_average === undefined || driver.road_adjusted_average === null
      ? "路況調整：資料不足"
      : `路況調整後平均 ${driver.road_adjusted_average} 分（${driver.road_adjusted_trip_count} 趟）`
  );
  setText("drv-average", driver.average_score === null ? null : `${driver.average_score} 分`);
  setText("drv-recent-label", `最近 ${driver.recent_trips} 趟平均（2 km 以上）`);
  setText("drv-recent", driver.recent_average_score === null ? null : `${driver.recent_average_score} 分`);
  const trend = document.getElementById("drv-trend");
  trend.textContent = driver.trend_label;
  trend.className = `trend-chip trend-${driver.trend}`;
  setText("drv-trips", `${driver.trip_count} 趟（${driver.scored_trip_count} 趟有分數）／${driver.total_distance_km ?? 0} km`);
  setText("drv-camera", `${driver.camera_blocked_trip_count} 趟`);

  renderScoreChart(document.getElementById("drv-chart"), driver.trips.filter((trip) => !trip.short_trip));
  categoryBarRows(
    document.getElementById("drv-categories"),
    Object.entries(driver.category_averages)
  );
  const deductions = document.getElementById("drv-deductions");
  deductions.replaceChildren();
  for (const item of driver.frequent_deductions) {
    const row = document.createElement("li");
    const text = document.createElement("span");
    text.textContent = `${item.label} · 出現在 ${item.trips} 趟的主要扣分`;
    const points = document.createElement("strong");
    points.textContent = `共 -${item.points_lost} 分`;
    row.append(text, points);
    deductions.append(row);
  }
  if (!driver.frequent_deductions.length) {
    const row = document.createElement("li");
    row.textContent = "目前沒有扣分紀錄。";
    deductions.append(row);
  }

  const body = document.getElementById("drv-trip-rows");
  body.replaceChildren();
  renderTripDetail({ detail: null });
  for (const trip of [...driver.trips].reverse()) {
    const row = document.createElement("tr");
    const goalMark = { achieved: "✓", missed: "✗" }[trip.goal_status] ?? "—";
    const cells = [
      trip.ref, trip.date, trip.plate ?? "—",
      trip.distance_km === null ? "—" : `${trip.distance_km} km${trip.short_trip ? " · 短程" : ""}`,
      trip.score ?? "不評分", null, goalMark, trip.top_deduction ?? (trip.camera_blocked ? "鏡頭遮蔽" : "—"),
    ];
    cells.forEach((value, index) => {
      const cell = document.createElement("td");
      if (index === 5) {
        const badge = document.createElement("span");
        badge.className = gradeClass(trip.grade);
        badge.textContent = trip.grade ?? "—";
        cell.append(badge);
      } else if (index === 6) {
        cell.textContent = value;
        cell.className = `goal-mark ${trip.goal_status ?? ""}`;
        cell.title = { achieved: "達成出發前的改善目標", missed: "未達成出發前的改善目標" }[trip.goal_status] ?? "沒有改善目標";
      } else {
        cell.textContent = value;
        if (index === 0 || index === 3 || index === 4) cell.className = "num";
      }
      row.append(cell);
    });
    if (trip.detail) {
      row.className = "clickable";
      row.tabIndex = 0;
      const select = () => {
        body.querySelectorAll("tr.selected").forEach((r) => r.classList.remove("selected"));
        row.classList.add("selected");
        renderTripDetail(trip);
      };
      row.addEventListener("click", select);
      row.addEventListener("keydown", (event) => { if (event.key === "Enter") select(); });
    }
    body.append(row);
  }
}

function goalSentence(check) {
  const comparator = check.direction === "higher_is_worse" ? "≤" : "≥";
  return `目標：${check.target_label} ${comparator} ${check.goal_value} ${check.unit_label}（出發前你近 7 天的平均）· 這趟 ${check.trip_value} ${check.unit_label}`;
}

function renderGoalCheck(check) {
  const card = document.getElementById("goal-check");
  const badge = document.getElementById("goal-check-badge");
  if (!check || check.status === "no_goal" || check.status === "unavailable") {
    setText("goal-check-title", "這趟出發前沒有設定改善目標");
    setText("goal-check-detail", "近期資料不足以選出改善目標。");
    badge.textContent = "—";
    badge.className = "goal-badge";
    setText("goal-check-streak", "");
    card.classList.remove("hidden");
    return;
  }
  const achieved = check.status === "achieved";
  setText("goal-check-title", `${achieved ? "達成" : "未達成"}：${check.target_label}`);
  setText("goal-check-detail", goalSentence(check));
  badge.textContent = achieved ? "✓ 達成" : "✗ 未達成";
  badge.className = `goal-badge ${check.status}`;
  setText("goal-check-streak", achieved ? `連續達成 ${check.streak} 趟` : "下一趟再挑戰");
  card.classList.remove("hidden");
}

function renderLastGoal(check) {
  const line = document.getElementById("last-goal");
  if (!check || check.status === "no_goal" || check.status === "unavailable") {
    line.classList.add("hidden");
    return;
  }
  line.textContent = check.status === "achieved"
    ? `上一趟目標（${check.target_label}）✓ 達成，已連續 ${check.streak} 趟，繼續保持！`
    : `上一趟目標（${check.target_label}）✗ 未達成：這趟 ${check.trip_value}，目標 ${check.goal_value} ${check.unit_label}。今天再試一次。`;
  line.classList.remove("hidden");
}

function renderRoadContext(context, officialScore) {
  const card = document.getElementById("road-context");
  if (!context) {
    card.classList.add("hidden");
    return;
  }
  const delta = context.adjusted_score !== null && officialScore !== null ? context.adjusted_score - officialScore : null;
  const city = context.city_share_pct === null ? "" : `這趟約 ${context.city_share_pct}% 在市區道路；`;
  const change = delta === null ? "" : delta === 0 ? "路況調整後分數不變" : `路況調整後 ${context.adjusted_score} 分（${delta > 0 ? "+" : ""}${delta}）`;
  setText("road-summary", `${city}${change}`);
  const items = document.getElementById("road-items");
  items.replaceChildren();
  for (const item of context.items) {
    const box = document.createElement("div");
    box.className = `road-item ${item.you < item.typical ? "better" : item.you > item.typical ? "worse" : ""}`;
    const label = document.createElement("span");
    label.textContent = item.label;
    const you = document.createElement("strong");
    you.textContent = `你 ${item.you} ${item.unit}`;
    const typical = document.createElement("em");
    typical.textContent = `一般 ${item.typical} ${item.unit}`;
    box.append(label, you, typical);
    items.append(box);
  }
  card.classList.remove("hidden");
}

function renderAlertReview(review) {
  const card = document.getElementById("alert-review");
  if (!review) {
    card.classList.add("hidden");
    return;
  }
  const list = document.getElementById("alert-list");
  list.replaceChildren();
  card.classList.remove("hidden");
  if (!review.in_dataset) {
    setText("alert-summary", "這趟沒有足夠的連續資料做 60 秒預測");
    setText("alert-note", "60 秒預測需要過去 5 分鐘的連續車輛資料。");
    return;
  }
  if (review.alert_count === 0) {
    setText("alert-summary", "這趟全程風險都不高，系統不需要出聲提醒 👍");
  } else {
    setText(
      "alert-summary",
      `系統會提醒你 ${review.alert_count} 次，其中 ${review.event_followed_count} 次在 60 秒內真的發生了風險事件`
    );
  }
  for (const alert of review.alerts) {
    const item = document.createElement("li");
    item.className = alert.event_label ? "hit" : "";
    const time = document.createElement("span");
    time.className = "alert-time";
    time.textContent = `第 ${alert.trip_minute} 分鐘`;
    const risk = document.createElement("span");
    risk.className = "alert-risk";
    risk.textContent = `${alert.escalation ? "風險升高 · " : ""}預測風險 ${alert.risk_pct}%`;
    const outcome = document.createElement("span");
    outcome.className = "alert-outcome";
    outcome.textContent = alert.event_label
      ? `${alert.lead_seconds} 秒後發生${alert.event_label}，提醒能提早 ${alert.lead_seconds} 秒`
      : "60 秒內沒有發生事件";
    item.append(time, risk, outcome);
    list.append(item);
  }
  if (review.alert_count > review.alerts.length) {
    const more = document.createElement("li");
    more.textContent = `另有 ${review.alert_count - review.alerts.length} 次提醒未列出`;
    list.append(more);
  }
  setText(
    "alert-note",
    `回放：這些資料錄製時還沒有 SafePilot，駕駛當時並沒有聽到提醒。模型 v1_xgboost、提醒規則 Alert Policy V1（實驗性）。資料期間：${review.split_label}。`
  );
}

function rankText(driver) {
  return driver.series_rank
    ? `${driver.primary_type} 系 ${driver.series_ranked_size} 人中第 ${driver.series_rank} 名`
    : "評分行程不足 10 趟，暫不排名";
}

function showReport(ref) {
  const report = state.reports.get(ref);
  if (!report) return;
  assertContract("postdrive", report, state.source);
  document.getElementById("report-select").value = String(ref);
  setText("postdrive-for", `${state.driver.driver_name} · 第 ${ref} 趟 · ${report.date} · ${report.plate ?? "—"}`);
  setText("report-meta", `「第幾趟」是在全部 ${state.driver.trip_count} 趟中的順序（含太短不評分的行程）；可查看最近 ${state.reports.size} 趟有分數的完整報告`);
  renderPostdrive(report, state.source);
  renderGoalCheck(report.goal_check);
  renderRoadContext(report.road_context, report.safety_score.overall_score);
  renderAlertReview(report.alert_review);
}

async function selectDriver(driverId) {
  const entry = state.index.drivers.find((driver) => driver.driver_id === driverId);
  const data = await loadView(`driver-${driverId}`, state.source);
  assertContract("predrive", data.predrive, state.source);
  state.driver = entry;
  state.reports = new Map(data.reports.map((report) => [report.ref, report]));
  document.getElementById("driver-select").value = driverId;
  setText("driver-meta", `${entry.primary_type} 系${entry.new_driver ? " · 新進駕駛" : ""} · 平均 ${entry.average_score ?? "—"} 分 · ${rankText(entry)}`);

  setText("predrive-for", `${entry.driver_name} · 下一趟出發前`);
  renderPredrive(data.predrive);
  renderLastGoal(data.predrive.last_goal_check);

  const reportSelect = document.getElementById("report-select");
  reportSelect.replaceChildren();
  for (const report of [...data.reports].reverse()) {
    const option = document.createElement("option");
    option.value = String(report.ref);
    const score = report.safety_score.overall_score;
    option.textContent = `第 ${report.ref} 趟 · ${report.date} · ${score === null ? "不評分" : `${score} 分`}${report.short_trip ? "（短程）" : ""}`;
    reportSelect.append(option);
  }
  if (data.reports.length) showReport(data.reports[data.reports.length - 1].ref);
  setupDriving(data);

  renderDriver(entry);
}

async function loadDriverIndex() {
  if (useLocalData) {
    try {
      state.source = "local";
      return await loadView("drivers", "local");
    } catch (error) {
      console.warn("Local driver views unavailable, falling back to synthetic", error);
    }
  }
  state.source = "synthetic";
  const index = await loadView("drivers", "synthetic");
  if (useLocalData) {
    index.mock_note = `尚未產生本機駕駛資料（請先執行 hino-analytics drivers 與 export-demo），整頁改用 Synthetic Demo。${index.mock_note}`;
  }
  return index;
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
    const [driving, index] = await Promise.all([loadView("driving", "synthetic"), loadDriverIndex()]);
    state.drivingSample = driving;
    renderDriving(driving);
    state.index = index;
    document.getElementById("local-banner").classList.toggle("hidden", state.source !== "local");
    setText("drivers-mock-note", index.mock_note);

    const select = document.getElementById("driver-select");
    select.replaceChildren();
    const drivers = index.drivers.filter((driver) => driver.trip_count > 0);
    for (const driver of drivers) {
      const option = document.createElement("option");
      option.value = driver.driver_id;
      option.textContent = `${driver.driver_name}（共 ${driver.trip_count} 趟，${driver.scored_trip_count} 趟有分數）`;
      select.append(option);
    }
    select.addEventListener("change", () => selectDriver(select.value).catch(handleLoadError));
    document.getElementById("report-select").addEventListener("change", (event) => {
      showReport(Number(event.target.value));
    });
    const requested = params.get("driver");
    const first = drivers.find((driver) => driver.driver_id === requested) ?? drivers[0];
    if (first) await selectDriver(first.driver_id);
  } catch (error) {
    handleLoadError(error);
  }
}

function handleLoadError(error) {
  console.error("SafePilot demo load failed", error);
  showError();
}

document.addEventListener("DOMContentLoaded", initialize);
