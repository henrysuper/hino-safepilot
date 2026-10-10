"use strict";

const BAND_COLOR = { highway: "#1565c0", normal: "#2e7d32", city: "#ef6c00" };
const BAND_LABEL = { highway: "高速/快速", normal: "一般道路", city: "市區" };
const DRIVER_LABEL = { very_eco: "very_eco 非常省油", eco: "eco 省油", normal: "normal 一般", aggressive: "aggressive 激進", very_aggressive: "very_aggressive 非常激進" };
const state = { meta: null, map: null, tiles: null, layers: null, fenceLayer: null, fences: [], demoTasks: [], markers: [],
  stops: [], backtest: null, fenceTest: null, scores: null,
  smart: { map: null, tiles: null, layers: null, driverTypes: ["normal"] } };

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "–").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
const pct = (v, d = 1) => (v === null || v === undefined ? "–" : `${Number(v).toFixed(d)}%`);

function status(text, isError = false) { const el = $("status"); el.textContent = text; el.className = isError ? "error" : ""; }

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

/** rows: array of objects; cols: [{key, label, fmt?, cls?}] */
function table(rows, cols, opts = {}) {
  const head = cols.map((c) => `<th class="${c.cls || ""}" scope="col">${esc(c.label)}</th>`).join("");
  const body = rows.map((r, i) => {
    const cls = [opts.onClick ? "click" : "", opts.rowClass ? opts.rowClass(r) : ""].join(" ");
    const cells = cols.map((c) => `<td class="${c.cls || ""}">${c.html ? c.html(r) : esc(c.fmt ? c.fmt(r[c.key], r) : r[c.key])}</td>`).join("");
    return `<tr class="${cls}" data-i="${i}" ${opts.onClick ? 'tabindex="0"' : ""}>${cells}</tr>`;
  }).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function bindRows(container, rows, onClick) {
  container.querySelectorAll("tbody tr").forEach((tr) => {
    const go = () => { container.querySelectorAll("tr.sel").forEach((x) => x.classList.remove("sel")); tr.classList.add("sel"); onClick(rows[Number(tr.dataset.i)]); };
    tr.addEventListener("click", go);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
  });
}

function cards(items) {
  return `<div class="cards">${items.map(([label, value]) => `<div class="card"><b>${esc(value)}</b><span>${esc(label)}</span></div>`).join("")}</div>`;
}

// ------------------------------------------------------------------ map
function initMap() {
  state.map = L.map("map").setView([24.15, 120.9], 8);
  state.tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap contributors" });
  state.tiles.addTo(state.map);
  state.layers = L.layerGroup().addTo(state.map);
  state.fenceLayer = L.layerGroup().addTo(state.map);
  $("basemap").addEventListener("change", (e) => setBasemap(e.target.checked));
}

function setBasemap(on) { $("basemap").checked = on; if (on) state.tiles.addTo(state.map); else state.map.removeLayer(state.tiles); }

/** routes: [{route_id, segment_geometry, segments}] ; highlight: route id drawn thick.
 *  ctx: {map, layers} target (defaults to the main dispatch map). */
function drawRoutes(routes, highlight, keep = false, ctx = null) {
  const map = ctx ? ctx.map : state.map;
  const layers = ctx ? ctx.layers : state.layers;
  if (!keep) layers.clearLayers();
  const bounds = [];
  routes.forEach((route) => {
    const main = route.route_id === highlight;
    (route.segment_geometry || []).forEach((piece, i) => {
      const seg = route.segments[i] || {};
      const line = L.polyline(piece, { color: BAND_COLOR[seg.road_band] || "#777", weight: main ? 6 : 3, opacity: main ? 0.95 : 0.45, dashArray: main ? null : "6 6" });
      line.bindTooltip(`${route.label || `路線 ${route.route_id}`} 第 ${i + 1} 段｜${BAND_LABEL[seg.road_band] || "-"}｜坡度 ${num(seg.avg_slope, 2)}%｜塞車指數 ${num(seg.congestion_index, 2)}（${seg.congestion_source === "tdx_live" ? "TDX 即時" : "歷史地圖"}）`);
      line.addTo(layers);
      bounds.push(...piece);
    });
  });
  if (bounds.length) map.fitBounds(bounds, { padding: [20, 20] });
}

// ------------------------------------------------------------------ fences
const fenceById = (id) => state.fences.find((f) => f.fence_id === id);

async function loadFences() {
  const { fences, demo_tasks } = await api("/api/fences");
  state.fences = fences;
  state.demoTasks = demo_tasks || [];
  $("demo-task").innerHTML = `<option value="">（自行設定）</option>` + state.demoTasks.map((t, i) => `<option value="${i}">${esc(t.name)}</option>`).join("");
  state.fenceLayer.clearLayers();
  fences.forEach((f) => {
    const demo = f.source === "demo";
    const circle = L.circle([f.lat, f.lon], { radius: f.radius_m, color: demo ? "#c2185b" : "#7b1fa2", weight: 2, fillOpacity: 0.12 });
    const box = document.createElement("div");
    box.innerHTML = `<b>${esc(f.name)}</b><br>${demo ? "示範圍籬（假設）" : `資料圍籬：${esc(f.visits)} 次起訖、${esc(f.vehicles)} 台車`}｜半徑 ${esc(f.radius_m)} m<br>`;
    [["start", "設為起點"], ["stop", "加入停靠點"], ["end", "設為終點"]].forEach(([where, text]) => {
      const b = document.createElement("button"); b.type = "button"; b.textContent = text;
      b.addEventListener("click", () => { placeFence(f.fence_id, where); state.map.closePopup(); });
      box.appendChild(b);
    });
    circle.bindPopup(box).bindTooltip(f.name).addTo(state.fenceLayer);
  });
}

function fenceOptions(selected) {
  const group = (source, label) => `<optgroup label="${label}">` + state.fences.filter((f) => f.source === source)
    .map((f) => `<option value="${esc(f.fence_id)}" ${f.fence_id === selected ? "selected" : ""}>${esc(f.fence_id)}｜${esc(f.name.replace(/^F\d+｜/, ""))}</option>`).join("") + "</optgroup>";
  return `<option value="">（選擇圍籬）</option>` + group("demo", "示範圍籬（假設，公開地點）") + group("derived", "資料圍籬（由真實行程起訖自動建立）");
}

// ------------------------------------------------------------------ task stops
function renderStops() {
  $("stops").innerHTML = state.stops.map((s, i) => {
    const role = i === 0 ? "起點" : i === state.stops.length - 1 ? "終點" : `停靠 ${i}`;
    const middle = i > 0 && i < state.stops.length - 1;
    return `<li class="stop" data-i="${i}">
      <div class="stop-head"><b>${role}</b>${middle ? `<button type="button" class="rm" data-i="${i}" aria-label="移除${role}">移除</button>` : ""}</div>
      <label class="sr">圍籬 <select class="stop-fence" data-i="${i}" aria-label="${role}圍籬">${fenceOptions(s.fence_id)}</select></label>
      <div class="stop-times">
        ${middle ? `<label>停留（分） <input type="number" min="0" max="480" class="stop-dwell" data-i="${i}" value="${esc(s.dwell_min)}"></label>` : ""}
      </div></li>`;
  }).join("");
  document.querySelectorAll(".stop-fence").forEach((el) => el.addEventListener("change", () => { state.stops[el.dataset.i].fence_id = el.value; drawStops(); }));
  document.querySelectorAll(".stop-dwell").forEach((el) => el.addEventListener("change", () => { state.stops[el.dataset.i].dwell_min = Number(el.value); drawStops(); }));
  document.querySelectorAll(".rm").forEach((el) => el.addEventListener("click", () => { state.stops.splice(Number(el.dataset.i), 1); renderStops(); }));
  drawStops();
}

function placeFence(fenceId, where) {
  if (where === "start") state.stops[0].fence_id = fenceId;
  else if (where === "end") state.stops[state.stops.length - 1].fence_id = fenceId;
  else state.stops.splice(state.stops.length - 1, 0, { fence_id: fenceId, dwell_min: 20 });
  renderStops();
}

function drawStops() {
  // The task changed: the previous comparison no longer applies.
  $("dispatch-result").innerHTML = "";
  state.layers.clearLayers();
  document.querySelectorAll(".series-btn").forEach((b) => b.setAttribute("aria-pressed", "false"));
  state.markers.forEach((m) => m.remove());
  state.markers = state.stops.map((s, i) => {
    const f = fenceById(s.fence_id);
    if (!f) return null;
    const label = i === 0 ? "起" : i === state.stops.length - 1 ? "終" : String(i);
    return L.marker([f.lat, f.lon], { icon: L.divIcon({ className: "stop-pin", html: label, iconSize: [26, 26] }), title: f.name }).bindTooltip(f.name).addTo(state.map);
  }).filter(Boolean);
}

function loadDemoTask(index) {
  const t = state.demoTasks[index];
  if (!t) return;
  state.stops = t.stops.map((id) => ({ fence_id: id, dwell_min: (t.dwell_min || {})[id] ?? 20 }));
  if (t.depart) $("depart").value = t.depart;
  renderStops();
  const pts = state.stops.map((s) => fenceById(s.fence_id)).filter(Boolean).map((f) => [f.lat, f.lon]);
  if (pts.length) state.map.fitBounds(pts, { padding: [40, 40] });
}

const driverById = (id) => (state.meta.drivers || []).find((d) => d.driver_id === id);

function renderForm() {
  const today = new Date(); today.setMinutes(today.getMinutes() - today.getTimezoneOffset());
  $("task-date").value = today.toISOString().slice(0, 10);
  $("depart").value = "08:00";
  $("series").innerHTML = state.meta.vehicle_types.map((t) =>
    `<button type="button" class="series-btn" data-type="${esc(t)}" aria-pressed="false">${esc(t)} 系</button>`).join("");
  document.querySelectorAll(".series-btn").forEach((b) => b.addEventListener("click", () => runTask(Number(b.dataset.type))));
  state.stops = [{ fence_id: "", dwell_min: 0 }, { fence_id: "", dwell_min: 0 }];
  if (state.demoTasks.length) { $("demo-task").value = "0"; loadDemoTask(0); } else renderStops();
}

/** Choosing a series runs the comparison of every driver of that series right away. */
async function runTask(series) {
  if (state.stops.some((s) => !s.fence_id)) return status("起點、終點（和停靠點）都要選圍籬", true);
  const live = $("live").checked;
  if (!live && !$("depart").value) return status("請設定出發時間", true);
  const buttons = document.querySelectorAll(".series-btn");
  buttons.forEach((b) => { b.setAttribute("aria-pressed", String(Number(b.dataset.type) === series)); b.disabled = true; });
  status(`比較 ${series} 系駕駛中…`);
  try {
    const result = await api("/api/task", {
      date: $("task-date").value, depart_time: live ? null : $("depart").value, live, vehicle_type: series,
      stops: state.stops.map((s) => ({ fence_id: s.fence_id, dwell_min: s.dwell_min })),
    });
    renderTask(result, series);
    status("");
  } catch (err) { status(`預測失敗：${err.message}`, true); }
  finally { buttons.forEach((b) => { b.disabled = false; }); }
}

const hhmm = (iso) => (iso ? iso.slice(11, 16) : "–");

function drawTask(result, chosenOnly = false) {
  state.layers.clearLayers();
  const routes = [];
  result.legs.forEach((leg, li) => leg.candidates.forEach((c) => {
    if (chosenOnly && c.route_id !== leg.chosen_route_id) return;
    routes.push({ ...c, route_id: `${li}:${c.route_id}`, label: `第 ${li + 1} 段 ${leg.from}→${leg.to}｜${c.route_id}`, chosen: c.route_id === leg.chosen_route_id });
  }));
  routes.filter((r) => !r.chosen).forEach((r) => drawRoutes([r], null, true));
  routes.filter((r) => r.chosen).forEach((r) => drawRoutes([r], r.route_id, true));
}

// Illustrative fuel price (matches backend config default; mock figure for cost context only).
const MOCK_FUEL_PRICE = 29;
const ROUTE_ID_LABEL = {
  "本機路網": "本機路網", "本機路網-免收費": "本機路網（免收費）",
  "H": "慣用路線", "ORS-免收費": "ORS（免收費）",
};
const routeLabel = (id) => ROUTE_ID_LABEL[id] || (/^車隊/.test(id) ? id : (/^ORS/.test(id) ? id : (/^本機/.test(id) ? id : `路線 ${id}`)));

/** ① why this pairing was chosen (decision summary). */
function taskWhyBlock(result) {
  const rows = result.options;
  const best = rows[0], worst = rows[rows.length - 1];
  const saveL = worst.predicted_fuel_L - best.predicted_fuel_L;
  const saveTwd = Math.max(0, saveL) * MOCK_FUEL_PRICE;
  // did vehicle condition change the order? (best by raw fuel vs best after health gate)
  let healthNote = "";
  if (rows.some((r) => r.health_grade)) {
    const cheapestRaw = [...rows].sort((a, b) => a.predicted_fuel_L - b.predicted_fuel_L)[0];
    if (cheapestRaw.vehicle_id !== best.vehicle_id || cheapestRaw.driver_id !== best.driver_id) {
      healthNote = `<li>最省油的組合（${esc(cheapestRaw.plate || cheapestRaw.vehicle_id)}）車況為「${TASK_HEALTH[cheapestRaw.health_grade] || cheapestRaw.health_grade}」，計入車況風險成本後改以此組為建議。</li>`;
    } else if (best.health_grade && best.health_grade !== "ok") {
      healthNote = `<li>建議組車況為「${TASK_HEALTH[best.health_grade] || best.health_grade}」，已計入車況風險成本後仍最省。</li>`;
    }
  }
  return `<div class="tk-why">
      <div class="tk-why-head">為什麼建議這組</div>
      <ul>
        <li>從 <b>${esc(result.pairs_compared)}</b> 組「車 × 駕駛」中，挑含車況風險成本後整趟最省的一組。</li>
        <li>建議組 <b>${num(best.predicted_fuel_L, 1)} L</b>，比最耗的 ${esc(worst.driver_name)}（${num(worst.predicted_fuel_L, 1)} L）省 <b>${num(saveL, 1)} L</b>，約 <b>NT$ ${num(saveTwd, 0)}</b>（模擬油價 ${MOCK_FUEL_PRICE} 元/L）。</li>
        ${healthNote}
      </ul>
    </div>`;
}

/** ② smart-route decision per leg: candidates compared, chosen one, saving vs runner-up. */
function taskRouteBlock(result) {
  const legs = result.legs || [];
  if (!legs.length) return "";
  const body = legs.map((leg, li) => {
    const cands = [...(leg.candidates || [])].sort((a, b) => a.mean_fuel_L - b.mean_fuel_L);
    const chosen = cands.find((c) => c.route_id === leg.chosen_route_id) || cands[0];
    const runner = cands.find((c) => c.route_id !== chosen.route_id);
    const saveTxt = runner ? `，比次佳省 ${num(runner.mean_fuel_L - chosen.mean_fuel_L, 2)} L` : "";
    const list = cands.map((c) => {
      const isPick = c.route_id === chosen.route_id;
      return `<tr class="${isPick ? "best" : ""}">
          <td class="l">${esc(routeLabel(c.route_id))}${isPick ? ' <span class="tag llm">選用</span>' : ""}</td>
          <td>${num(c.distance_km, 1)} km</td><td>${num(c.minutes, 0)} 分</td><td>${num(c.mean_fuel_L, 2)} L</td></tr>`;
    }).join("");
    return `<details class="tk-leg"${li === 0 ? " open" : ""}>
        <summary>第 ${li + 1} 段：${esc(leg.from)} → ${esc(leg.to)}｜選用 <b>${esc(routeLabel(chosen.route_id))}</b>${saveTxt}</summary>
        <table><thead><tr><th class="l">候選路線</th><th>距離</th><th>時間</th><th>平均油耗</th></tr></thead><tbody>${list}</tbody></table>
      </details>`;
  }).join("");
  return `<div class="tk-sec-title">智慧路線怎麼挑的</div>${body}`;
}

/** ③ fuel breakdown of the recommended pairing: per-leg driving + stop idle, with 80% interval. */
function taskFuelBreakdown(result) {
  const best = result.options[0];
  const legs = result.legs || [];
  const rows = (best.leg_fuel_L || []).map((v, i) => ({
    label: legs[i] ? `第 ${i + 1} 段 ${legs[i].from} → ${legs[i].to}` : `第 ${i + 1} 段`, fuel: v,
  }));
  if (best.stop_idle_fuel_L > 0) rows.push({ label: "停靠怠速", fuel: best.stop_idle_fuel_L, idle: true });
  const total = best.predicted_fuel_L;
  const bars = rows.map((r) => {
    const pctw = total > 0 ? Math.min(100, (r.fuel / total) * 100) : 0;
    return `<div class="tk-brk-row">
        <span class="tk-brk-label">${esc(r.label)}</span>
        <span class="tk-brk-bar"><span class="tk-brk-fill ${r.idle ? "idle" : ""}" style="width:${pctw}%"></span></span>
        <span class="tk-brk-val">${num(r.fuel, 2)} L</span></div>`;
  }).join("");
  const iv = best.prediction_interval_L || [];
  const ivTxt = iv.length === 2 ? `${num(iv[0], 1)}–${num(iv[1], 1)} L` : "–";
  return `<div class="tk-sec-title">建議組油耗組成（${esc(best.plate || best.vehicle_id)}）</div>
    ${bars}
    <div class="tk-brk-total">整趟合計 <b>${num(total, 1)} L</b>　<span class="hint">80% 預測區間 ${ivTxt}</span></div>`;
}

/** ④ schedule timeline from stop arrive/depart/dwell. */
function taskTimeline(result) {
  const stops = result.stops || [];
  const items = stops.map((s, i) => {
    const role = i === 0 ? "起點" : i === stops.length - 1 ? "終點" : `停靠 ${i}`;
    const t = i === 0 ? hhmm(result.depart_time) : hhmm(s.arrive);
    const dwell = s.dwell_min ? `，停留 ${num(s.dwell_min, 0)} 分 → ${hhmm(s.depart)} 續行` : "";
    return `<li><span class="tk-tl-time">${esc(t)}</span><span class="tk-tl-dot"></span>
        <span class="tk-tl-text"><b>${esc(role)}</b>　${esc(s.name)}${esc(dwell)}</span></li>`;
  }).join("");
  return `<div class="tk-sec-title">行程時間軸（總時長約 ${num(result.total_minutes, 0)} 分）</div>
    <ul class="tk-timeline">${items}</ul>`;
}

const TASK_HEALTH = { ok: "良好", caution: "注意", block: "待檢修" };

/** Result: the recommended driver, then every driver of the series ranked by predicted fuel. */
function renderTask(result, series) {
  drawTask(result, true);
  const rows = result.options;
  const best = rows[0], worst = rows[rows.length - 1];
  let html = `<div class="recommend"><span>建議派</span><b>${esc(best.driver_name)}</b>
      <span>車牌 ${esc(best.plate || best.vehicle_id)}｜${esc(series)} 系｜預測油耗 ${num(best.predicted_fuel_L, 1)} L</span></div>
    <p class="hint">${esc(result.stops[0].name)} → ${esc(result.stops[result.stops.length - 1].name)}｜${num(result.distance_km)} km｜
      ${hhmm(result.depart_time)} 出發，約 ${hhmm(result.arrive_time)} 抵達</p>`;
  html += result.notes.map((n) => `<div class="note">${esc(n)}</div>`).join("");
  html += taskWhyBlock(result);
  const hasHealth = rows.some((r) => r.health_grade);
  const hasSafety = rows.some((r) => r.safety_grade && r.safety_grade !== "unknown");
  const healthLabel = { ok: "良好", caution: "注意", block: "待檢修" };
  const safetyLabel = { ok: "良好", caution: "注意", block: "偏低", unknown: "無資料" };
  const healthCol = {
    key: "health_grade", label: "車況",
    html: (r) => {
      if (!r.health_grade) return "–";
      const cls = r.health_grade === "ok" ? "llm" : "warn";
      const score = r.health_score != null ? `（${num(r.health_score, 0)}）` : "";
      const mark = r.health_excluded ? ' <span class="tag warn">不派</span>' : "";
      return `<span class="tag ${cls}">${healthLabel[r.health_grade] || r.health_grade}</span>${score}${mark}`;
    },
  };
  const safetyCol = {
    key: "safety_grade", label: "駕駛安全",
    html: (r) => {
      if (!r.safety_grade || r.safety_grade === "unknown") return "–";
      const cls = r.safety_grade === "ok" ? "llm" : "warn";
      const score = r.safety_score != null ? `（${num(r.safety_score, 0)}）` : "";
      const trend = r.safety_declining ? ' <span class="tag warn" title="近期退步">↓</span>' : "";
      const mark = r.safety_excluded ? ' <span class="tag warn">不派</span>' : "";
      return `<span class="tag ${cls}">${safetyLabel[r.safety_grade] || r.safety_grade}</span>${score}${trend}${mark}`;
    },
  };
  // 最終排序成本：有安全層用 dispatch_score，否則車況層 dispatch_fuel_L，否則原始油耗。
  const finalCost = (r) => (r.dispatch_score != null ? r.dispatch_score : (r.dispatch_fuel_L != null ? r.dispatch_fuel_L : r.predicted_fuel_L));
  const fuelCol = {
    key: "predicted_fuel_L", label: "預測油耗 (L)",
    html: (r) => {
      const fc = finalCost(r);
      const gated = fc != null && Math.abs(fc - r.predicted_fuel_L) > 0.01
        ? ` <span class="tag warn" title="含車況＋駕駛安全風險成本">→ ${num(fc, 1)}</span>` : "";
      return `${num(r.predicted_fuel_L, 1)}${gated}${r.rank === 1 ? ' <span class="tag llm">建議</span>' : ""}${r.rank === rows.length && rows.length > 1 ? ' <span class="tag warn">最耗</span>' : ""}`;
    },
  };
  const cols = [
    { key: "rank", label: "排名" },
    { key: "plate", label: "車牌", fmt: (v, r) => v || r.vehicle_id },
    { key: "vehicle_type", label: "系列", fmt: (v) => `${v} 系` },
    { key: "driver_name", label: "駕駛", cls: "l" },
  ];
  if (hasHealth) cols.push(healthCol);
  if (hasSafety) cols.push(safetyCol);
  cols.push(fuelCol);
  const rankTitle = hasSafety ? "車 × 駕駛 排名（含車況＋駕駛安全風險成本）" : "車 × 駕駛 排名（含車況風險成本）";
  html += `<div class="tk-sec-title">${rankTitle}</div>`;
  html += table(rows.map((r, i) => ({ ...r, rank: i + 1 })), cols,
    { rowClass: (r) => ((r.health_excluded || r.safety_excluded) ? "warn-row" : r.rank === 1 ? "best" : "") });
  // explainability blocks
  html += taskFuelBreakdown(result);
  html += taskRouteBlock(result);
  html += taskTimeline(result);
  $("dispatch-result").innerHTML = html;
}

// ------------------------------------------------------------------ smart dispatch (single trip A->B)
const DRIVER_TYPE_LABEL = { very_eco: "非常省油", eco: "省油", normal: "一般", aggressive: "激進", very_aggressive: "非常激進" };
const SMART_HEALTH = { ok: "良好", caution: "注意", block: "待檢修" };

function initSmartMap() {
  const s = state.smart;
  if (s.map) return;
  s.map = L.map("smart-map").setView([24.15, 120.9], 8);
  s.tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap contributors" });
  s.tiles.addTo(s.map);
  s.layers = L.layerGroup().addTo(s.map);
  $("smart-basemap").addEventListener("change", (e) => { if (e.target.checked) s.tiles.addTo(s.map); else s.map.removeLayer(s.tiles); });
}

function renderSmartForm() {
  const today = new Date(); today.setMinutes(today.getMinutes() - today.getTimezoneOffset());
  $("smart-date").value = today.toISOString().slice(0, 10);
  const options = state.fences.map((f) => `<option value="${esc(f.fence_id)}">${esc(f.name)}（${esc(f.fence_id)}）</option>`).join("");
  $("smart-origin").innerHTML = options;
  $("smart-destination").innerHTML = options;
  if (state.fences.length > 1) $("smart-destination").selectedIndex = 1;
  // driver-type toggles (multi-select); default normal on
  $("smart-drivers").innerHTML = (state.meta.driver_types || []).map((d) =>
    `<button type="button" class="series-btn" data-driver="${esc(d)}" aria-pressed="${d === "normal"}">${esc(DRIVER_TYPE_LABEL[d] || d)}</button>`).join("");
  $("smart-drivers").querySelectorAll(".series-btn").forEach((b) => b.addEventListener("click", () => {
    const on = b.getAttribute("aria-pressed") !== "true";
    b.setAttribute("aria-pressed", String(on));
    state.smart.driverTypes = [...$("smart-drivers").querySelectorAll('[aria-pressed="true"]')].map((x) => x.dataset.driver);
  }));
  // series buttons: picking one runs the recommendation
  $("smart-series").innerHTML = state.meta.vehicle_types.map((t) =>
    `<button type="button" class="series-btn" data-type="${esc(t)}" aria-pressed="false">${esc(t)} 系</button>`).join("");
  $("smart-series").querySelectorAll(".series-btn").forEach((b) => b.addEventListener("click", () => runSmart(Number(b.dataset.type))));
  // fit map to the two chosen fences
  const fit = () => {
    const o = fenceById($("smart-origin").value), d = fenceById($("smart-destination").value);
    const pts = [o, d].filter(Boolean).map((f) => [f.lat, f.lon]);
    if (pts.length) state.smart.map.fitBounds(pts, { padding: [40, 40] });
  };
  $("smart-origin").addEventListener("change", fit);
  $("smart-destination").addEventListener("change", fit);
  $("smart-live").addEventListener("change", (e) => { $("smart-depart").disabled = e.target.checked; $("smart-date").disabled = e.target.checked; });
  fit();
}

async function runSmart(series) {
  const origin = $("smart-origin").value, destination = $("smart-destination").value;
  if (!origin || !destination) return status("請選起點與終點圍籬", true);
  if (origin === destination) return status("起點與終點不能相同", true);
  const drivers = state.smart.driverTypes.length ? state.smart.driverTypes : ["normal"];
  const live = $("smart-live").checked;
  if (!live && !$("smart-depart").value) return status("請設定出發時間", true);
  const buttons = $("smart-series").querySelectorAll(".series-btn");
  buttons.forEach((b) => { b.setAttribute("aria-pressed", String(Number(b.dataset.type) === series)); b.disabled = true; });
  status(`推薦 ${series} 系最佳車輛中…`);
  try {
    const result = await api("/api/dispatch", {
      date: $("smart-date").value, depart_time: live ? null : $("smart-depart").value, live,
      vehicle_type: series, origin_fence: origin, destination_fence: destination, driver_types: drivers,
    });
    renderSmart(result, series);
    status("");
  } catch (err) { status(`推薦失敗：${err.message}`, true); }
  finally { buttons.forEach((b) => { b.disabled = false; }); }
}

function renderSmart(result, series) {
  const route = result.route || {};
  drawRoutes([{ ...route, label: `${route.from || "起"}→${route.to || "迄"}` }], route.route_id, false, { map: state.smart.map, layers: state.smart.layers });
  const rec = result.recommended, rows = result.ranking || [];
  const mode = result.generation_mode === "llm"
    ? '<span class="tag llm">Gemini 生成</span>' : '<span class="tag">固定範本</span>';

  let html = `<div class="recommend"><span>建議派</span><b>${esc(rec.plate || rec.vehicle_id)}</b>
      <span>${esc(series)} 系｜${esc(DRIVER_TYPE_LABEL[rec.driver_type] || rec.driver_type)}型駕駛｜預測油耗 ${num(rec.predicted_fuel_L, 1)} L（約 ${num(rec.predicted_minutes, 0)} 分）</span></div>`;
  html += `<p class="report-text">${esc(result.text)}</p><p>${mode}${result.llm_error ? ` <span class="hint">（LLM 未採用：${esc(result.llm_error)}）</span>` : ""}</p>`;
  html += `<p class="hint">${esc(route.from || "")} → ${esc(route.to || "")}｜${num(route.distance_km)} km｜路線 ${esc(route.route_id)}</p>`;
  (result.notes || []).forEach((n) => { html += `<div class="note">${esc(n)}</div>`; });

  const healthCol = {
    key: "health_grade", label: "車況",
    html: (r) => {
      if (!r.health_grade) return "–";
      const cls = r.health_grade === "ok" ? "llm" : "warn";
      const score = r.health_score != null ? `（${num(r.health_score, 0)}）` : "";
      const mark = r.health_excluded ? ' <span class="tag warn">不派</span>' : "";
      return `<span class="tag ${cls}">${SMART_HEALTH[r.health_grade] || r.health_grade}</span>${score}${mark}`;
    },
  };
  const fuelCol = {
    key: "dispatch_fuel_L", label: "決策油耗 (L)",
    html: (r) => {
      const gated = r.dispatch_fuel_L != null && r.predicted_fuel_L != null && Math.abs(r.dispatch_fuel_L - r.predicted_fuel_L) > 0.01
        ? ` <span class="hint" title="原始預測">(${num(r.predicted_fuel_L, 1)})</span>` : "";
      const tag = r.rank === 1 ? ' <span class="tag llm">建議</span>' : (r.rank === rows.length && rows.length > 1 ? ' <span class="tag warn">最耗</span>' : "");
      return `${num(r.dispatch_fuel_L, 1)}${gated}${tag}`;
    },
  };
  const deltaCol = { key: "delta_L", label: "與建議差",
    html: (r) => (r.rank === 1 ? "—" : `+${num(r.delta_L, 1)} L（+${num(r.delta_pct, 1)}%）`) };
  const cols = [
    { key: "rank", label: "排名" },
    { key: "plate", label: "車牌", fmt: (v, r) => v || r.vehicle_id },
    { key: "driver_type", label: "駕駛類型", cls: "l", fmt: (v) => DRIVER_TYPE_LABEL[v] || v },
    healthCol, fuelCol, deltaCol,
  ];
  html += table(rows, cols, { rowClass: (r) => (r.health_excluded ? "warn-row" : r.rank === 1 ? "best" : "") });

  if ((result.alternatives || []).length) {
    html += `<p class="hint">其他候選路線：${result.alternatives.map((a) =>
      `${esc(a.route_id)}（${num(a.distance_km)} km，最省 ${num(a.best_fuel_L, 1)} L）`).join("、")}。系統已選最省的那條。</p>`;
  }
  html += `<p class="hint">排序以「決策油耗」為準＝預測油耗加計車況風險成本；車況「待檢修」的車在長途任務會被排除。括號內為未計車況的原始預測。駕駛類型與車牌為模擬資料。</p>`;
  $("smart-result").innerHTML = html;
}

// ------------------------------------------------------------------ backtest
async function loadBacktest() {
  if (state.backtest) return;
  status("載入回測結果…");
  state.backtest = await api("/api/backtest");
  const s = state.backtest.summary;
  if (!s) { $("backtest-summary").innerHTML = "<p>尚未執行回測：<code>hino-fuel backtest</code></p>"; status(""); return; }
  const m = s.metrics.ors_first, c = s.metrics.closest_distance, raw = (s.metrics_uncalibrated || {}).ors_first;
  const cal = s.calibration || {};
  const calNote = raw ? `<div class="note">校正前（只算行駛段）：MAPE ${pct(raw.fuel_mape_pct)}、偏差 ${pct(raw.fuel_bias_pct)}、區間涵蓋 ${pct(raw.interval_coverage * 100, 0)}。
      校正（只用訓練期 ${cal.fit_trips} 趟真實行程）：路線油耗 ×${num(cal.route_fuel_factor, 3)}、行車時間 ×${num(cal.route_time_factor, 2)}，
      另加場站/停靠油耗 ${Object.entries(cal.overhead_L_by_type || {}).map(([t, v]) => `${t} 系 ${num(v, 2)} L`).join("、")}。</div>` : "";
  $("backtest-summary").innerHTML = `<p class="hint">${esc(s.method)}</p>` + calNote + cards([
    ["回測行程", m.trips], ["油耗 MAPE（ORS 首選路線）", pct(m.fuel_mape_pct)], ["油耗中位誤差", pct(m.fuel_median_ape_pct)],
    ["整體偏差", pct(m.fuel_bias_pct)], ["若路線長度與實際相同", pct(m.fuel_mape_if_route_length_matched_pct)],
    ["路線長度誤差中位", pct(m.route_km_abs_error_median_pct)], ["長度差 ≤15% 的行程 MAPE", `${pct(m.fuel_mape_on_those_trips_pct)}（${m.trips_with_route_km_within_15pct} 趟）`],
    ["80% 區間涵蓋", pct(m.interval_coverage * 100, 0)], ["實際落在五種駕駛範圍內", pct(m.actual_within_driver_type_range * 100, 0)],
    ["時間 MAPE", pct(m.minutes_mape_pct)], ["選最接近實際長度的路線 MAPE", pct(c.fuel_mape_pct)],
  ]) + `<p class="hint">篩選：測試期 ${s.funnel.test_trips} 趟 → ≥10 km ${s.funnel.ge_min_km} 趟 → 單程型（直線距離 ≥ 行駛距離 30%）${s.funnel.one_way_like} 趟 → 抽樣 ${s.funnel.sampled} 趟；本次實際呼叫 ORS ${s.ors_live_calls} 次（其餘來自快取）。</p>
     <p class="hint">分車型 MAPE：${Object.entries(m.by_type_fuel_mape_pct).map(([t, v]) => `${t} 系 ${pct(v)}`).join("、")}</p>` +
    (s.route_overlap ? `<h3>司機有沒有照 ORS 規劃的路線開？</h3><p class="hint">重疊度 = 規劃路線與實際 GPS 軌跡彼此距離 200 m 內的比例（取兩方向較小者）。</p>` +
      table(s.route_overlap.by_group, [
        { key: "group", label: "分組", cls: "l" }, { key: "trips", label: "趟數" }, { key: "fuel_mape_pct", label: "油耗 MAPE", fmt: (v) => pct(v) },
        { key: "fuel_median_ape_pct", label: "中位誤差", fmt: (v) => pct(v) }, { key: "fuel_bias_pct", label: "偏差", fmt: (v) => pct(v) },
        { key: "km_ratio_actual_over_route", label: "實際里程 ÷ 規劃里程", fmt: (v) => num(v, 2) }]) : "");
  const rows = s.trips.filter((r) => r.route_choice === "ors_first").map((r) => ({ ...r, err: (r.pred_normal_L / r.actual_fuel_L - 1) * 100 }));
  const el = $("backtest-table");
  el.innerHTML = `<div class="scroll">${table(rows, [
    { key: "date", label: "日期", cls: "l" }, { key: "vehicle", label: "車" }, { key: "vehicle_type", label: "車型" },
    { key: "actual_km", label: "實際 km", fmt: (v) => num(v) }, { key: "route_km", label: "ORS km", fmt: (v) => num(v) },
    { key: "actual_fuel_L", label: "實際 L", fmt: (v) => num(v, 1) }, { key: "pred_normal_L", label: "預測 L", fmt: (v) => num(v, 1) },
    { key: "err", label: "誤差", html: (r) => `<span class="${r.err > 0 ? "pos" : "neg"}">${num(r.err, 0)}%</span>` },
    { key: "route_overlap", label: "路線重疊", fmt: (v) => (v === undefined ? "–" : pct(v * 100, 0)) },
    { key: "pred_very_eco_L", label: "very_eco–very_agg", fmt: (v, r) => `${num(v, 1)}–${num(r.pred_very_aggressive_L, 1)}` },
  ], { onClick: true })}</div>`;
  bindRows(el, rows, showBacktestTrip);
  await loadFenceTest();
  status("");
}

async function loadFenceTest() {
  state.fenceTest = await api("/api/fence_test");
  const f = state.fenceTest;
  if (!f || !f.pairs) { $("fence-summary").innerHTML = "<p>尚未執行：<code>hino-fuel fences</code> 然後 <code>hino-fuel fence-test</code></p>"; return; }
  $("fence-summary").innerHTML = `<p class="hint">從訓練期真實起訖點自動建立 ${f.fences} 個圍籬；測試期 10 km 以上行程中有 ${f.trips_fence_to_fence} 趟是「從某圍籬開到另一圍籬」。
    每組圍籬只呼叫一次 ORS 規劃路線，再用每趟的真實車輛與出發時間預測，和真實油耗比較。</p>` + cards([
    ["測試圍籬組", f.pairs_tested], ["測試行程", f.trips_tested], ["單趟油耗 MAPE（全部）", pct(f.trip_mape_pct)],
    ["照規劃路線開的行程 MAPE", `${pct(f.trip_mape_followed_route_pct)}（${f.trips_followed_route} 趟）`],
    ["排除多點配送後 MAPE", `${pct(f.trip_mape_single_task_pct)}（${f.trips_single_task} 趟）`],
    ["有慣用路線的行程：ORS vs 慣用路線", f.habitual_route ? `${pct(f.habitual_route.ors_route_mape_pct)} → ${pct(f.habitual_route.habitual_route_mape_pct)}（${f.habitual_route.trips} 趟）` : "–"],
    ["圍籬組平均油耗誤差", pct(f.pair_mape_pct)], ["80% 區間涵蓋", pct(f.interval_coverage * 100, 0)],
  ]);
  const el = $("fence-table");
  el.innerHTML = `<div class="scroll">${table(f.pairs, [
    { key: "pair", label: "組", cls: "l" }, { key: "from", label: "起點圍籬", cls: "l" }, { key: "to", label: "終點圍籬", cls: "l" },
    { key: "trips", label: "趟數" }, { key: "route_km", label: "規劃 km", fmt: (v) => num(v) }, { key: "actual_km_mean", label: "實際 km（平均）", fmt: (v) => num(v) },
    { key: "actual_fuel_L_mean", label: "實際 L（平均）", fmt: (v) => num(v, 2) }, { key: "predicted_fuel_L_mean", label: "預測 L（平均）", fmt: (v) => num(v, 2) },
    { key: "pair_error_pct", label: "誤差", html: (r) => `<span class="${r.pair_error_pct > 0 ? "pos" : "neg"}">${num(r.pair_error_pct, 0)}%</span>` },
    { key: "mean_route_overlap", label: "路線重疊", fmt: (v) => pct(v * 100, 0) },
  ], { onClick: true })}</div><div id="fence-trips"></div>`;
  bindRows(el, f.pairs, showFencePair);
}

function showFencePair(pair) {
  const trips = state.fenceTest.trips.filter((t) => t.pair === pair.pair);
  $("fence-trips").innerHTML = `<h3>${esc(pair.from)} → ${esc(pair.to)}：每趟明細</h3>` + table(trips, [
    { key: "date", label: "日期", cls: "l" }, { key: "vehicle", label: "車" }, { key: "vehicle_type", label: "車型" },
    { key: "actual_km", label: "實際 km", fmt: (v) => num(v) }, { key: "actual_fuel_L", label: "實際 L", fmt: (v) => num(v, 2) },
    { key: "predicted_fuel_L", label: "預測 L（ORS）", fmt: (v) => num(v, 2) }, { key: "error_pct", label: "誤差", fmt: (v) => pct(v, 0) },
    { key: "habitual_error_pct", label: "誤差（慣用路線）", fmt: (v) => (v === null || v === undefined ? "–" : pct(v, 0)) },
    { key: "route_overlap", label: "路線重疊", fmt: (v) => pct(v * 100, 0) },
    { key: "single_task", label: "單趟任務", fmt: (v) => (v ? "是" : "否（多點）") }]) +
    `<button type="button" id="fence-pair-map">在地圖上看這條規劃路線</button>`;
  $("fence-pair-map").addEventListener("click", () => {
    switchTab("dispatch");
    setBasemap(false);
    const route = { route_id: pair.pair, segments: pair.segments, segment_geometry: splitForMap(pair.geometry, pair.segments) };
    drawRoutes([route], pair.pair);
    status("已在地圖畫出圍籬間的規劃路線（真實地點，預設關閉底圖）。");
  });
}

function showBacktestTrip(row) {
  const d = state.backtest.details.find((x) => x.trip_id === row.trip_id);
  if (!d) return;
  setBasemap(false); // real trip: keep the viewport away from the public tile server by default
  $("backtest-title").textContent = `${d.vehicle}（${d.vehicle_type} 系）｜${d.date}｜實際 ${d.actual_km} km、${d.actual_fuel_L} L`;
  const routes = d.routes.map((r) => ({ ...r, segment_geometry: splitForMap(r.geometry, r.segments) }));
  let html = `<div class="note">真實行程地圖預設關閉底圖，避免把行程範圍送到公開圖磚伺服器；可在「發派預測」分頁地圖下方開啟。</div>`;
  routes.forEach((r) => {
    html += `<h3>路線 ${esc(r.route_id)}｜${num(r.distance_km)} km <button type="button" class="show-bt" data-route="${esc(r.route_id)}">在地圖上強調</button></h3>`;
    html += table(r.options, [{ key: "driver_type", label: "駕駛類型", cls: "l" }, { key: "predicted_fuel_L", label: "預測 L", fmt: (v) => num(v, 2) },
      { key: "prediction_interval_L", label: "80% 區間", fmt: (v) => `${num(v[0], 1)}–${num(v[1], 1)}` }, { key: "predicted_minutes", label: "分鐘" }]);
  });
  $("backtest-detail").innerHTML = html;
  switchTab("dispatch", false);
  drawRoutes(routes, routes[0].route_id);
  switchTab("backtest", false);
  setTimeout(() => state.map.invalidateSize(), 50);
  document.querySelectorAll(".show-bt").forEach((b) => b.addEventListener("click", () => { switchTab("dispatch"); drawRoutes(routes, b.dataset.route); }));
  status("路線已畫在「發派預測」分頁的地圖上（按「在地圖上強調」切換過去）。");
}

/** Split a stored [lon,lat] polyline into pieces proportional to segment lengths. */
function splitForMap(geometry, segments) {
  const pts = geometry.map(([lon, lat]) => [lat, lon]);
  const dist = [0];
  for (let i = 1; i < pts.length; i++) dist.push(dist[i - 1] + state.map.distance(pts[i - 1], pts[i]));
  const total = dist[dist.length - 1], kmTotal = segments.reduce((a, s) => a + s.km, 0) || 1;
  const pieces = []; let start = 0, idx = 0;
  segments.forEach((s) => {
    const end = start + (s.km / kmTotal) * total, piece = [];
    while (idx < pts.length && dist[idx] <= end) { if (dist[idx] >= start) piece.push(pts[idx]); idx++; }
    if (idx < pts.length) piece.push(pts[idx]);
    pieces.push(piece.length > 1 ? piece : [pts[Math.max(0, idx - 1)], pts[Math.min(pts.length - 1, idx)]]);
    start = end; idx = Math.max(0, idx - 1);
  });
  return pieces;
}

// ------------------------------------------------------------------ scores
async function loadScores() {
  if (state.scores) return;
  status("載入分數…");
  state.scores = await api("/api/scores");
  const s = state.scores.summary;
  if (s) {
    $("score-summary").innerHTML = cards([
      ["計分行程（≥10 km）", s.scored_trips], ["分數中位數", num(s.score_quantiles["0.5"], 0)], ["分數 P10–P90", `${num(s.score_quantiles["0.1"], 0)}–${num(s.score_quantiles["0.9"], 0)}`],
      ["油耗比中位數", num(s.fuel_ratio_quantiles["0.5"], 2)], ["分數 vs 市區比例相關", num(s.score_vs_city_share_spearman, 2)], ["分數 vs 坡度相關", num(s.score_vs_slope_spearman, 2)],
    ]) + `<p class="hint">${esc(s.score_formula)}；應有油耗 = 同車、同路線、同時段與實際塞車下 normal 駕駛類型的預測。相關係數接近 0 表示分數沒有因路型或坡度被扣分。</p>`;
  }
  const el = $("score-table");
  el.innerHTML = `<div class="scroll">${table(state.scores.trips, [
    { key: "date", label: "日期", cls: "l" }, { key: "vehicle", label: "車" }, { key: "vehicle_type", label: "車型" },
    { key: "driver", label: "駕駛（模擬）", fmt: (v) => (v ? (driverById(v) || {}).driver_name || v : "–") }, { key: "km", label: "km", fmt: (v) => num(v) },
    { key: "actual_fuel_L", label: "實際 L", fmt: (v) => num(v, 1) }, { key: "expected_fuel_L", label: "應有 L", fmt: (v) => num(v, 1) },
    { key: "fuel_ratio", label: "油耗比", fmt: (v) => num(v, 2) }, { key: "fuel_score", label: "分數", fmt: (v) => num(v, 0) },
  ], { onClick: true })}</div>`;
  bindRows(el, state.scores.trips, showReport);
  status("點選一趟行程產生報告。");
}

/** Score colour band: green (good) / amber (ok) / red (poor). */
function scoreClass(score) {
  if (score == null) return "mid";
  return score >= 80 ? "good" : score >= 60 ? "mid" : "poor";
}

/** One worst-segment "problem card" with its main behaviour and advice. */
function problemCard(w) {
  const mb = w.main_behaviour;
  const times = mb && mb.times_typical != null && mb.times_typical <= 20
    ? `是一般駕駛的 <b>${num(mb.times_typical, 1)} 倍</b>` : "明顯高於一般駕駛";
  const impact = mb && mb.fuel_impact_L != null ? `，這段多耗約 <b>${num(mb.fuel_impact_L, 1)} L</b>` : "";
  const cause = mb ? `主因：${esc(mb.label)} ${times}${impact}。` : "此段超用未集中在單一駕駛行為，可能與載重或路況變化有關。";
  const advice = w.advice ? `<div class="rp-advice">建議：${esc(w.advice)}</div>` : "";
  return `<div class="rp-problem">
      <div class="rp-problem-head">第 ${esc(w.segment_no)} 段 ｜ ${esc(w.road_band_label)} ｜ ${num(w.km, 1)} km ｜ 超用 +${num(w.excess_pct, 0)}%</div>
      <div class="rp-problem-body">${cause}</div>${advice}
    </div>`;
}

/** Behaviour breakdown table for the worst segment's four behaviours. */
function behaviourTable(w) {
  const rows = (w.behaviours || []).map((b) => ({ ...b, isMain: w.main_behaviour && b.behaviour === w.main_behaviour.behaviour }));
  return table(rows, [
    { key: "label", label: "行為", cls: "l", html: (b) => (b.isMain ? `<b class="warn-text">${esc(b.label)}</b>` : esc(b.label)) },
    { key: "actual", label: "實際", fmt: (v) => num(v, 2) },
    { key: "typical", label: "一般", fmt: (v) => num(v, 2) },
    { key: "times_typical", label: "倍數", html: (b) => (b.times_typical == null ? "–" : `<span class="${b.exceeds ? "warn-text" : ""}">${num(b.times_typical, 1)}×</span>`) },
    { key: "fuel_impact_L", label: "油耗影響", html: (b) => {
        if (b.fuel_impact_L == null) return "–";
        const up = b.fuel_impact_L > 0.005;
        return `<span class="${up ? "pos" : "neg"}">${up ? "+" : ""}${num(b.fuel_impact_L, 1)} L</span>`;
      } },
  ]);
}

function reportHtml(r) {
  const f = r.facts, sc = f.score, tr = f.trip, vh = f.vehicle_health, money = f.money;
  const over = sc.vs_expected_pct != null && sc.vs_expected_pct > 0;
  const diffPct = sc.vs_expected_pct;
  // NOTE: in this app .pos is red (over-use) and .neg is green (saving).
  const diffCls = over ? "pos" : "neg";
  const diffText = diffPct == null ? "–" : `${diffPct > 0 ? "+" : ""}${num(diffPct, 0)}%`;
  const cls = scoreClass(sc.fuel_score);
  const mode = r.generation_mode === "llm" ? '<span class="tag llm">Gemini 生成</span>' : '<span class="tag">固定範本</span>';
  const width = Math.max(0, Math.min(100, Number(sc.fuel_score) || 0));

  // ① header
  let html = `<div class="report-card" id="report-card">`;
  html += `<div class="rp-head">
      <h3>行程報告</h3>
      <p class="hint">${esc(f.vehicle)}（${esc(tr.vehicle_type)} 系）｜${esc(tr.date)}｜${num(tr.km, 1)} km｜${esc(tr.segments)} 段</p>
    </div>`;

  // ② score gauge + three mini cards + narration
  html += `<div class="rp-score">
      <div class="rp-gauge">
        <div class="rp-score-num ${cls}">${num(sc.fuel_score, 0)}<span class="rp-score-max">/ 100</span></div>
        <div class="rp-bar"><div class="rp-bar-fill ${cls}" style="width:${width}%"></div></div>
      </div>
      <div class="rp-minis">
        <div class="rp-mini"><span>實際油耗</span><b>${num(sc.actual_fuel_L, 1)} L</b></div>
        <div class="rp-mini"><span>應有油耗</span><b>${num(sc.expected_fuel_L, 1)} L</b></div>
        <div class="rp-mini ${diffCls}-box"><span>vs 應有</span><b class="${diffCls}">${diffText}</b></div>
      </div>
    </div>`;
  html += `<div class="rp-narration">${esc(r.text)} <span class="rp-mode">${mode}${r.llm_error ? ` <span class="hint">（LLM 未採用：${esc(r.llm_error)}）</span>` : ""}</span></div>`;
  if (r.generation_mode === "llm") html += `<details class="rp-fold"><summary>對照：固定範本文字</summary><p>${esc(r.template_text)}</p></details>`;

  // ③ hotspots: good banner or problem cards
  html += `<h4 class="rp-sec">超用熱點</h4>`;
  const worst = f.worst_segments || [];
  if (!over || !worst.length) {
    html += `<div class="rp-good"><span class="rp-good-mark">✓</span>
        <div><b>本趟整體低於一般水準，沒有明顯超用路段。</b><div class="hint">良好駕駛，請保持平順給油與熄火怠速習慣。</div></div></div>`;
  } else {
    html += worst.map(problemCard).join("");
    // ④ behaviour breakdown for the first (worst) segment that has a main behaviour
    const detailSeg = worst.find((w) => (w.behaviours || []).length);
    if (detailSeg) {
      html += `<h4 class="rp-sec">主因路段 — 行為拆解（第 ${esc(detailSeg.segment_no)} 段）</h4>`;
      html += `<div class="scroll">${behaviourTable(detailSeg)}</div>`;
    }
  }

  // ⑤ cost + health
  const healthScore = vh.health_score;
  const healthLabel = healthScore == null ? "無資料" : (vh.low ? "偏低" : "正常");
  const healthCls = healthScore == null ? "mid" : (vh.low ? "poor" : "good");
  html += `<div class="rp-stats">
      <div class="rp-stat"><span>超用成本（模擬油價）</span><b>約 NT$ ${num(money.excess_twd, 0)}</b></div>
      <div class="rp-stat"><span>車輛健康度</span><b class="${healthCls}">${healthScore == null ? "–" : num(healthScore, 0)} ・ ${healthLabel}</b></div>
    </div>`;
  if (vh.low && vh.note) html += `<div class="note">${esc(vh.note)}</div>`;

  // ⑥ collapsibles: per-segment table + raw JSON
  html += `<details class="rp-fold"><summary>逐段實際 vs 應有（${esc((r.segments || []).length)} 段明細）</summary><div class="scroll">${table(r.segments.map((s) => ({ ...s, no: s.segment_index + 1 })), [
      { key: "no", label: "段" }, { key: "road_band", label: "道路", fmt: (v) => BAND_LABEL[v] || v }, { key: "segment_km", label: "km", fmt: (v) => num(v, 1) },
      { key: "fuel_L", label: "實際 L", fmt: (v) => num(v, 2) }, { key: "expected_fuel_L", label: "應有 L", fmt: (v) => num(v, 2) },
      { key: "idle_min_per_km", label: "怠速 分/km", fmt: (v) => num(v, 2) }, { key: "expected_idle_min_per_km", label: "一般怠速", fmt: (v) => num(v, 2) },
      { key: "avg_slope", label: "坡度 %", fmt: (v) => num(v, 1) },
    ])}</div></details>`;
  html += `<details class="rp-fold"><summary>交給 LLM 的結構化 JSON（稽核用）</summary><pre>${esc(JSON.stringify(f, null, 2))}</pre></details>`;

  html += `</div>`;  // .report-card
  // ⑦ actions (outside the printable card)
  html += `<div class="rp-actions no-print">
      <button type="button" id="rp-copy">複製報告文字</button>
      <button type="button" id="rp-print" class="ghost">列印</button>
    </div>`;
  return html;
}

async function showReport(row) {
  $("report").innerHTML = "<p>產生報告中…</p>";
  try {
    const r = await api("/api/report", { trip_id: row.trip_id });
    $("report").innerHTML = reportHtml(r);
    // copy = narration + the key numbers, as plain text for pasting into docs
    const f = r.facts, sc = f.score;
    $("rp-copy").addEventListener("click", () => {
      const lines = [
        `行程報告｜${f.vehicle}（${f.trip.vehicle_type} 系）｜${f.trip.date}｜${num(f.trip.km, 1)} km`,
        `油耗分數 ${num(sc.fuel_score, 0)}／100；實際 ${num(sc.actual_fuel_L, 1)} L、應有 ${num(sc.expected_fuel_L, 1)} L（vs 應有 ${sc.vs_expected_pct > 0 ? "+" : ""}${num(sc.vs_expected_pct, 0)}%）`,
        r.text,
      ];
      (f.worst_segments || []).forEach((w) => {
        const mb = w.main_behaviour;
        lines.push(`・第 ${w.segment_no} 段（${w.road_band_label}）超用 +${num(w.excess_pct, 0)}%${mb ? `，主因 ${mb.label}` : ""}${w.advice ? `；建議：${w.advice}` : ""}`);
      });
      navigator.clipboard.writeText(lines.join("\n")).then(
        () => status("報告文字已複製到剪貼簿。"),
        () => status("複製失敗，瀏覽器未授權剪貼簿。", true));
    });
    $("rp-print").addEventListener("click", () => window.print());
  } catch (err) { $("report").innerHTML = `<p class="pos">${esc(err.message)}</p>`; }
}

// ------------------------------------------------------------------ virtual drivers
const MULT_LABEL = { idle_min_per_km: "怠速", harsh_acc_per_km: "急加速", harsh_brake_per_km: "急減速", overspeed_share: "超速" };
const GROUP_LABEL = {
  all: "全部", established_drivers: "資深駕駛", new_drivers: "新進駕駛（全部）", "new_drivers_history_0": "新進：第 1 趟（尚無紀錄）",
  "new_drivers_history_1-5": "新進：已有 1–5 趟", "new_drivers_history_6-20": "新進：已有 6–20 趟", "new_drivers_history_21+": "新進：21 趟以上",
};

async function loadDrivers() {
  if (state.drivers) return;
  status("載入虛擬駕駛…");
  state.drivers = await api("/api/drivers");
  const { drivers, report } = state.drivers;
  if (!drivers.length || !report) { $("drivers-view").innerHTML = `<div class="panel"><p>尚未建立：<code>hino-fuel drivers</code></p></div>`; status(""); return; }
  const m = report.method, e = report.evaluation_test_period_ge10km;
  let html = `<div class="panel"><h2>虛擬駕駛（此為模擬資料）</h2><div class="note">${esc(report.mock_note)}</div>
    <ul class="hint"><li>${esc(m.assignment)}</li><li>${esc(m.profile)}</li><li>${esc(m.model)}</li></ul>
    <div class="note">${esc(m.caveat)}</div>` + cards([
      ["駕駛人數", report.roster.drivers], ["新進駕駛", report.roster.new_drivers.map((id) => (driverById(id) || {}).driver_name || id).join("、")],
      ["分配的車-日", report.roster.vehicle_days], ["駕駛排序等級相關（測試期）", num(e.driver_ranking.spearman_predicted_vs_actual_effect, 2)],
    ]) + `<p class="hint">${esc(e.driver_ranking.note)}接近 0 代表模型預估「誰比較省油」和實際結果對不太上。</p></div>`;
  const ROADS = [["highway", "高速"], ["normal", "一般"], ["city", "市區"]];
  html += `<div class="panel"><h3>駕駛名單與個人平均</h3><p class="hint">數字 = 這位駕駛的行為 ÷ 同樣路段、同時段的一般值（1.20 = 多 20%）。每格依序是 怠速 / 急加速 / 急減速 / 超速。行程少的駕駛會往「相似駕駛」收縮；「本人權重」= 自己紀錄占多少。</p>
    <div class="scroll">${table(drivers, [
      { key: "driver_name", label: "駕駛", cls: "l", html: (d) => `${esc(d.driver_name)}${d.new_driver ? ' <span class="tag llm">新進</span>' : ""}` },
      { key: "primary_type", label: "系列", fmt: (v) => `${v} 系` }, { key: "style_label", label: "設定風格", cls: "l" },
      { key: "trips", label: "趟數" }, { key: "days", label: "出車天數" },
      ...ROADS.map(([band, label]) => ({ key: band, label, html: (d) => Object.keys(MULT_LABEL).map((k) => num(((d.by_road || {})[band] || d.multipliers)[k], 2)).join(" / ") })),
      { key: "own_weight", label: "本人權重", fmt: (v) => pct(v * 100, 0) },
      { key: "neighbours", label: "相似駕駛", cls: "l", fmt: (v) => (v.length ? v.map((id) => (driverById(id) || {}).driver_name || id).join("、") : "–") },
    ])}</div></div>`;
  const keys = [["A_unknown_driver", "不知道是誰開"], ["B_driver_average", "用駕駛個人平均"]];
  const evalRows = Object.keys(GROUP_LABEL).filter((g) => e[g]).map((g) => ({ group: GROUP_LABEL[g], trips: e[g][keys[0][0]].trips,
    ...Object.fromEntries(keys.map(([k]) => [k, e[g][k].trip_mape_pct])) }));
  html += `<div class="split"><div class="panel grow"><h3>測試期整趟油耗 MAPE（發派模式，≥10 km）</h3>${table(evalRows, [
      { key: "group", label: "行程", cls: "l" }, { key: "trips", label: "趟數" }, ...keys.map(([k, label]) => ({ key: k, label, fmt: (v) => pct(v) }))])}
    <p class="hint">新進駕駛的分組依「出這趟之前已累積的趟數」。</p></div>`;
  const curves = report.new_driver_curves;
  html += `<div class="panel grow"><h3>新進駕駛：只看前 K 趟就預測之後的行程</h3>${table(curves, [
      { key: "driver_id", label: "駕駛", cls: "l", fmt: (v) => (driverById(v) || {}).driver_name || v }, { key: "first_k_trips", label: "K" },
      { key: "profile", label: "相似駕駛", cls: "l", fmt: (p) => (p.neighbours.length ? p.neighbours.map((id) => (driverById(id) || {}).driver_name || id).join("、") : "（無紀錄，用全隊平均）") },
      { key: "profile", label: "本人權重", fmt: (p) => pct(p.own_weight * 100, 0) },
      { key: "remaining", label: "之後趟數", fmt: (r) => r.trips }, { key: "remaining", label: "之後 MAPE", fmt: (r) => pct(r.trip_mape_pct) }])}</div></div>`;
  const rp = report.replay.summary;
  html += `<div class="panel"><h3>模擬派車重播（測試期）</h3><p class="hint">${esc(report.replay.note)}</p>${table(Object.entries(rp).map(([k, v]) => ({ k: k === "all" ? "全部" : `${k} 系`, ...v })), [
      { key: "k", label: "車型", cls: "l" }, { key: "trips", label: "趟數" }, { key: "mean_candidates", label: "平均可選駕駛" },
      { key: "assigned_pred_L", label: "原分配（預測 L）", fmt: (v) => num(v, 1) }, { key: "system_pred_L", label: "系統挑選（預測 L）", fmt: (v) => num(v, 1) },
      { key: "predicted_saving_pct", label: "預估節省", fmt: (v) => pct(v) }, { key: "same_driver_share", label: "與原分配相同", fmt: (v) => pct(v * 100, 0) }])}</div>`;
  $("drivers-view").innerHTML = html;
  status("");
}

// ------------------------------------------------------------------ model
function renderModel() {
  const v = state.meta.validation, t = v.test, ts = t.time_split;
  const fi = v.feature_importance || [], maxShare = Math.max(...fi.map((f) => f.shap_share), 0.01);
  let html = `<div class="panel"><h2>模型驗證（測試期 ${esc(v.periods.test_start.slice(0, 10))} 起）</h2>` + cards([
    ["段長（驗證月選出）", `${state.meta.model.segment_km} km`], ["整趟 MAPE 發派模式（≥10 km）", pct(ts.expected.trips_ge_10km.trip_mape_pct)],
    ["整趟 MAPE 事後模式", pct(ts.actual.trips_ge_10km.trip_mape_pct)], ["排序正確率", pct(ts.expected.ranking.accuracy * 100, 0)],
    ["沒跑過的路（路線切分）", pct(v.route_split.summary.expected_trip_mape_pct_mean)], ["80% 區間涵蓋", pct(t.interval_coverage_ge10km * 100, 0)],
  ]) + `<p class="hint">分車型 MAPE：${Object.entries(t.expected_mode_by_vehicle_type_ge10km).map(([k, x]) => `${k} 系 ${pct(x.trip_mape_pct)}（${x.trips} 趟）`).join("、")}</p></div>`;
  html += `<div class="split"><div class="panel grow"><h3>3 / 5 / 10 km 段長比較（驗證月）</h3>${table(v.segment_length_comparison, [
    { key: "segment_km", label: "段長 km" }, { key: "best_valid_expected_trip_mape_ge10", label: "發派模式 MAPE", fmt: (x) => pct(x) },
    { key: "best_valid_actual_trip_mape_ge10", label: "事後模式 MAPE", fmt: (x) => pct(x) }, { key: "feature_set", label: "特徵組", cls: "l" }])}
    <h3>特徵重要度（平均 |SHAP|）</h3>${table(fi, [{ key: "feature", label: "特徵", cls: "l" },
      { key: "shap_share", label: "佔比", html: (r) => `<span class="bar" style="width:${(r.shap_share / maxShare) * 120}px"></span> ${pct(r.shap_share * 100)}` }])}</div>`;
  html += `<div class="panel grow"><h3>駕駛類型倍率（相對一般駕駛行為）</h3>${table(v.driver_type_multipliers, [
    { key: "road_band", label: "道路", fmt: (x) => BAND_LABEL[x] || x }, { key: "driver_type", label: "類型", cls: "l" },
    { key: "idle_min_per_km", label: "怠速", fmt: (x) => num(x, 2) }, { key: "harsh_acc_per_km", label: "急加速", fmt: (x) => num(x, 2) },
    { key: "harsh_brake_per_km", label: "急減速", fmt: (x) => num(x, 2) }, { key: "overspeed_share", label: "超速", fmt: (x) => num(x, 2) }, { key: "trips", label: "行程數" }])}</div></div>`;
  html += `<div class="panel"><h3>車輛健康度與效率偏差（訓練期）</h3>${table(state.meta.vehicles, [
    { key: "vehicle", label: "車" }, { key: "vehicle_type", label: "車型" }, { key: "health_score", label: "健康度", fmt: (x) => num(x, 0) },
    { key: "efficiency_offset", label: "效率偏差", fmt: (x) => num(x, 3) }])}<p class="hint">效率偏差 1.08 = 比同型車平均多耗 8%；可能同時包含車況與常開這台車的駕駛習慣。</p></div>`;
  $("model-view").innerHTML = html;
}

// ------------------------------------------------------------------ tabs
// ------------------------------------------------------------------ 安全分數 (SafePilot safety score; simulated drivers)
const SF_TREND = { improving: "進步", stable: "持平", declining: "退步", unavailable: "資料不足" };
const SF_GOAL = { achieved: "✓", missed: "✗" };
const sfScoreClass = (v) => (v == null ? "mid" : v >= 85 ? "good" : v >= 70 ? "mid" : "poor");

async function loadSafety() {
  if (state.safety) return;
  status("載入安全分數…");
  state.safety = await api("/api/safety");
  const s = state.safety;
  if (!s.available) {
    $("safety-overview").innerHTML = `<p class="hint">${esc(s.message)}</p>`;
    status("");
    return;
  }
  renderSafetyOverview(s);
  state.safetyFilter = { series: "all", sort: "attention" };
  renderSafetyFilter();
  renderSafetyTable();
  status("點選駕駛查看安全報告。");
}

function renderSafetyOverview(s) {
  const f = s.fleet;
  const totalGraded = Object.values(f.grade_counts).reduce((a, b) => a + b, 0) || 1;
  const grades = ["A", "B", "C", "D", "E"].filter((g) => f.grade_counts[g]).map((g) => {
    const share = (f.grade_counts[g] / totalGraded) * 100;
    return `<span class="sf-grade-${g}" style="width:${share}%" title="${g}：${f.grade_counts[g]} 趟（${num(share, 0)}%）">${share >= 6 ? `${g} ${num(share, 0)}%` : ""}</span>`;
  }).join("");
  const r = s.rules;
  $("safety-overview").innerHTML = `<h2>車隊安全總覽</h2>
    <div class="note">${esc(s.mock_note)}</div>
    ${cards([
      ["車隊平均安全分數（依公里加權）", num(f.average_score, 1)], ["駕駛人數", f.drivers],
      [`有分數的行程（≥ ${r.short_trip_km} km）`, f.scored_full_trips], ["總里程 km", num(f.total_km, 0)],
      ["需要關注的駕駛", f.watch_count],
    ])}
    <h4>行程等級分布（≥ ${r.short_trip_km} km）</h4><div class="sf-grades" role="img" aria-label="行程等級分布">${grades}</div>
    <p class="hint">${["A", "B", "C", "D", "E"].filter((g) => f.grade_counts[g]).map((g) => `${g} ${f.grade_counts[g]} 趟`).join("　")}</p>
    <div class="sf-cols">
      <div><h4>每月車隊平均</h4><div class="scroll">${table(s.monthly, [
        { key: "month", label: "月份", cls: "l" }, { key: "trips", label: "行程" },
        { key: "average_score", label: "平均分數（加權）", fmt: (v) => num(v, 1) },
      ])}</div><p class="hint">資料月份不連續（主辦方資料只有部分月份）。</p></div>
      <div><h4>各車系比較</h4>${table(s.series, [
        { key: "series", label: "車系", cls: "l", fmt: (v) => `${v} 系` }, { key: "drivers", label: "駕駛" },
        { key: "average_score", label: "平均分數", fmt: (v) => num(v, 1) }, { key: "total_km", label: "里程 km", fmt: (v) => num(v, 0) },
        { key: "camera_blocked_trips", label: "鏡頭遮蔽趟數" },
      ])}<p class="hint">不同車系用途與路線不同，請以系內排名比較駕駛。</p></div>
    </div>
    <h4>需要關注的駕駛（${s.watch_list.length} 位）</h4>
    <div id="safety-watch" class="scroll">${table(s.watch_list, [
      { key: "driver_name", label: "駕駛", cls: "l" }, { key: "series", label: "車系", fmt: (v) => `${v} 系` },
      { key: "recent_average_score", label: "最近 10 趟", fmt: (v) => num(v, 0) },
      { key: "attention", label: "原因", cls: "l", fmt: (v) => v.join("；") },
    ], { onClick: true })}</div>
    <p class="hint">規則：最近 10 趟比前 10 趟低 ≥ ${r.decline_points} 分、最近 10 趟平均 < ${r.low_recent_score} 分、鏡頭遮蔽 ≥ ${r.camera_blocked_trips} 趟、最近改善目標達成率 ≤ ${r.low_goal_rate_pct}%。名單與分配為模擬資料，僅用於展示流程。</p>`;
  bindRows($("safety-watch"), s.watch_list, (row) => showSafetyDriver(row.driver_id));
}

function renderSafetyFilter() {
  const series = [...new Set(state.safety.drivers.map((d) => d.series))].sort();
  const f = state.safetyFilter;
  $("safety-filter").innerHTML = `<div class="series">
    ${["all", ...series].map((s) => `<button type="button" class="series-btn" data-series="${s}" aria-pressed="${f.series === String(s)}">${s === "all" ? "全部" : `${s} 系`}</button>`).join("")}
    <label class="hint">排序 <select id="safety-sort">
      <option value="attention">需要關注優先</option><option value="low">平均分數低→高</option><option value="high">平均分數高→低</option><option value="rank">系內排名</option>
    </select></label></div>`;
  $("safety-sort").value = f.sort;
  $("safety-sort").addEventListener("change", (e) => { f.sort = e.target.value; renderSafetyTable(); });
  $("safety-filter").querySelectorAll(".series-btn").forEach((b) => b.addEventListener("click", () => {
    f.series = b.dataset.series; renderSafetyFilter(); renderSafetyTable();
  }));
}

function renderSafetyTable() {
  const f = state.safetyFilter;
  let rows = state.safety.drivers.filter((d) => f.series === "all" || String(d.series) === f.series);
  const by = {
    attention: (a, b) => b.attention.length - a.attention.length || (a.recent_average_score ?? 999) - (b.recent_average_score ?? 999),
    low: (a, b) => (a.average_score ?? 999) - (b.average_score ?? 999),
    high: (a, b) => (b.average_score ?? -1) - (a.average_score ?? -1),
    rank: (a, b) => a.series - b.series || (a.series_rank ?? 999) - (b.series_rank ?? 999),
  }[f.sort];
  rows = [...rows].sort(by);
  const el = $("safety-table");
  el.innerHTML = `<div class="scroll">${table(rows, [
    { key: "driver_name", label: "駕駛", cls: "l", html: (d) => `${esc(d.driver_name)}${d.new_driver ? ' <span class="tag llm">新進</span>' : ""}${d.attention.length ? ' <span class="tag warn">關注</span>' : ""}` },
    { key: "series", label: "車系", fmt: (v) => `${v}` },
    { key: "series_rank", label: "系內排名", fmt: (v, d) => (v ? `${v}/${d.series_ranked_size}` : "–") },
    { key: "average_score", label: "平均", fmt: (v) => num(v, 0) },
    { key: "recent_average_score", label: "最近10趟", fmt: (v) => num(v, 0) },
    { key: "trend", label: "趨勢", fmt: (v) => SF_TREND[v] || "–" },
    { key: "goal_rate", label: "目標達成", fmt: (v) => (v == null ? "–" : `${v}%`) },
    { key: "camera_blocked_trip_count", label: "鏡頭遮蔽" },
    { key: "scored_trip_count", label: "評分趟數" },
    { key: "top_deduction", label: "主要扣分", cls: "l" },
  ], { onClick: true, rowClass: (d) => (d.attention.length ? "sf-watch" : "") })}</div>`;
  bindRows(el, rows, (row) => showSafetyDriver(row.driver_id));
}

function sfTrendChart(trips) {
  const pts = trips.filter((t) => t.score != null && !t.short_trip);
  if (pts.length < 2) return '<p class="hint">2 km 以上的評分行程不足，無法畫走勢。</p>';
  const W = 560, H = 180, L = 30, R = 8, T = 8, B = 22;
  const low = Math.max(0, Math.min(50, Math.floor((Math.min(...pts.map((p) => p.score)) - 5) / 10) * 10));
  const x = (i) => L + (i / (pts.length - 1)) * (W - L - R);
  const y = (v) => T + (1 - (v - low) / (100 - low)) * (H - T - B);
  const grid = [90, 80, 70, 60, 50].filter((v) => v >= low).map((v) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${L - 4}" y="${y(v) + 4}" text-anchor="end">${v}</text>`).join("");
  const line = pts.map((p, i) => `${x(i)},${y(p.score)}`).join(" ");
  const dots = pts.map((p, i) => `<circle class="dot" cx="${x(i)}" cy="${y(p.score)}" r="3"><title>第 ${p.ref} 趟 · ${esc(p.date)} · ${p.score} 分</title></circle>`).join("");
  return `<div class="sf-chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="每趟安全分數走勢">${grid}<polyline class="line" points="${line}"/>${dots}
    <text class="axis" x="${L}" y="${H - 6}">${esc(pts[0].date)}</text><text class="axis" x="${W - R}" y="${H - 6}" text-anchor="end">${esc(pts[pts.length - 1].date)}</text></svg></div>
    <p class="hint">只畫 2 km 以上的行程；虛線為等級門檻；滑鼠停在點上可看分數。</p>`;
}

async function showSafetyDriver(driverId) {
  status("載入駕駛報告…");
  const d = await api(`/api/safety_driver?id=${encodeURIComponent(driverId)}`);
  if (!d.available) { $("safety-report").innerHTML = `<p class="hint">${esc(d.message)}</p>`; status(""); return; }
  const s = d.summary, h = d.history, next = d.next_trip;
  const reportsByRef = new Map(d.reports.map((r) => [r.ref, r]));
  const cats = Object.entries(h.category_averages || {}).map(([label, v]) => `<div class="sf-cat"><span>${esc(label)}</span><div class="rp-bar"><div class="rp-bar-fill ${sfScoreClass(v)}" style="width:${v}%"></div></div><b>${num(v, 0)}</b></div>`).join("");
  const deductions = (h.frequent_deductions || []).map((x) => `<div class="rp-problem"><div class="rp-problem-head">${esc(x.label)}</div><div class="rp-problem-body">出現在 ${x.trips} 趟的主要扣分，共 -${num(x.points_lost, 1)} 分</div></div>`).join("") || '<div class="rp-good"><span class="rp-good-mark">✓</span>目前沒有扣分紀錄</div>';
  const goal = next && next.message ? `<p><b>${esc(next.display.target_label)}</b>：${esc(next.message.action)}</p>` : '<p class="hint">近期資料不足以選出改善目標。</p>';
  const reminders = next ? `<ul class="sf-reminders">${next.briefing.reminders.map((r) => `<li class="${r.priority}">${esc(r.message)}</li>`).join("")}</ul>` : "";
  const trips = [...h.trips].reverse();
  $("safety-report").innerHTML = `<div class="report-card">
    <div class="rp-head"><h3>${esc(s.driver_name)} 安全報告 ${s.new_driver ? '<span class="tag llm">新進</span>' : ""}${s.attention.length ? '<span class="tag warn">需要關注</span>' : ""}</h3>
      <span class="hint">${s.series} 系 · 共 ${s.trip_count} 趟（${s.scored_trip_count} 趟有分數）· ${num(s.total_distance_km, 0)} km · 模擬駕駛名單</span></div>
    ${s.attention.length ? `<div class="rp-problem"><div class="rp-problem-head">需要關注</div><div class="rp-problem-body">${s.attention.map(esc).join("<br>")}</div></div>` : ""}
    <div class="rp-score">
      <div class="rp-gauge"><span class="rp-score-num ${sfScoreClass(s.average_score)}">${num(s.average_score, 0)}</span><span class="rp-score-max">/ 100 平均安全分數（依公里加權）</span>
        <div class="rp-bar"><div class="rp-bar-fill ${sfScoreClass(s.average_score)}" style="width:${s.average_score || 0}%"></div></div></div>
      <div class="rp-minis">
        <div class="rp-mini"><span>最近 10 趟</span><b>${num(s.recent_average_score, 0)}</b></div>
        <div class="rp-mini"><span>趨勢</span><b>${SF_TREND[s.trend] || "–"}</b></div>
        <div class="rp-mini"><span>系內排名</span><b>${s.series_rank ? `${s.series_rank}/${s.series_ranked_size}` : "–"}</b></div>
        <div class="rp-mini"><span>目標達成</span><b>${s.goal_rate == null ? "–" : `${s.goal_rate}%`}</b></div>
      </div>
    </div>
    <p class="hint">路況調整後平均 ${num(s.road_adjusted_average, 0)} 分 · 鏡頭遮蔽 ${s.camera_blocked_trip_count} 趟</p>
    <h4 class="rp-sec">安全分數走勢</h4>${sfTrendChart(h.trips)}
    <div class="sf-cols"><div><h4 class="rp-sec">分項平均</h4>${cats}</div><div><h4 class="rp-sec">最常被扣分的項目</h4>${deductions}</div></div>
    <h4 class="rp-sec">下一趟出發前的改善目標與提醒</h4>${goal}${reminders}
    <h4 class="rp-sec">行程紀錄（最新在上；最近 15 趟有分數的行程可點開）</h4>
    <div id="safety-trips" class="scroll">${table(trips, [
      { key: "ref", label: "第幾趟" }, { key: "date", label: "日期", cls: "l" }, { key: "plate", label: "車牌（模擬）" },
      { key: "distance_km", label: "km", fmt: (v, t) => (v == null ? "–" : `${num(v, 1)}${t.short_trip ? " 短程" : ""}`) },
      { key: "score", label: "分數", fmt: (v) => (v == null ? "不評分" : num(v, 0)) }, { key: "grade", label: "等級" },
      { key: "goal_status", label: "目標", fmt: (v) => SF_GOAL[v] || "–" }, { key: "top_deduction", label: "主要扣分", cls: "l" },
    ], { onClick: true })}</div>
    <div id="safety-trip-detail"></div>
    <div class="rp-actions no-print"><button type="button" id="safety-print">列印這份報告</button></div>
    <p class="hint">${esc(d.mock_note)}</p></div>`;
  bindRows($("safety-trips"), trips, (t) => showSafetyTrip(reportsByRef.get(t.ref), t));
  $("safety-print").addEventListener("click", () => window.print());
  status("");
}

function showSafetyTrip(report, trip) {
  const el = $("safety-trip-detail");
  if (!report) { el.innerHTML = `<div class="sf-trip hint">第 ${trip.ref} 趟沒有完整報告（只保留最近 15 趟有分數的行程）。</div>`; return; }
  const sc = report.safety_score;
  const cats = sc.categories.map((c) => `<div class="sf-cat"><span>${esc(c.label)}</span><div class="rp-bar"><div class="rp-bar-fill ${sfScoreClass(c.score)}" style="width:${c.score || 0}%"></div></div><b>${num(c.score, 0)}</b></div>`).join("");
  const deds = sc.top_deductions.map((x) => `<li>${esc(x.label)}（${esc(x.category_label)}）${num(x.observed_value, 1)} ${esc(x.unit_label)}：<b>-${num(x.points_lost, 1)} 分</b>${x.definition ? `<br><span class="hint">定義：${esc(x.definition)}</span>` : ""}</li>`).join("") || "<li>沒有明顯扣分</li>";
  const g = report.goal_check;
  const goal = g && g.status !== "no_goal" && g.status !== "unavailable"
    ? `<p>出發前目標「${esc(g.target_label)}」：${g.status === "achieved" ? "✓ 達成" : "✗ 未達成"}（這趟 ${num(g.trip_value, 1)}，目標 ${num(g.goal_value, 1)} ${esc(g.unit_label)}；連續達成 ${g.streak} 趟）</p>` : "";
  const road = report.road_context ? `<p>路況調整後 ${num(report.road_context.adjusted_score, 0)} 分（市區 ${report.road_context.city_share_pct ?? "–"}%）：${report.road_context.items.map((i) => `${esc(i.label)} ${i.you}/${i.typical} ${esc(i.unit)}`).join("、")}（你/一般）</p>` : "";
  const risk = report.risk_summary ? `<p>60 秒預測：這趟最高 ${report.risk_summary.peak_risk_pct}%，系統會提醒 ${report.risk_summary.alerts} 次，其中 ${report.risk_summary.alerts_followed_by_event} 次 60 秒內真的發生事件（模擬行駛回放）。</p>` : "";
  el.innerHTML = `<div class="sf-trip"><h4>第 ${report.ref} 趟 · ${esc(report.date)} · ${esc(report.plate)} · ${num(sc.overall_score, 0)} 分（${esc(sc.grade)}）${report.short_trip ? '<span class="tag">短程</span>' : ""}</h4>
    ${cats}<ul>${deds}</ul>${goal}${road}${risk}
    ${sc.dms_reliability === "camera_blocked" ? '<p class="warn-text">車內鏡頭曾被遮蔽，疲勞、手機、分心等偵測可能偏少。</p>' : ""}</div>`;
}

function switchTab(name, focus = true) {
  document.querySelectorAll("nav button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".tab").forEach((s) => { s.hidden = s.id !== `tab-${name}`; });
  if (name === "dispatch") setTimeout(() => state.map.invalidateSize(), 30);
  if (name === "smart") setTimeout(() => state.smart.map && state.smart.map.invalidateSize(), 30);
  if (focus) {
    const run = { backtest: loadBacktest, scores: loadScores, safety: loadSafety, drivers: loadDrivers }[name];
    if (run) run().catch((e) => status(e.message, true));
  }
}

async function main() {
  initMap();
  initSmartMap();
  document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  $("dispatch-form").addEventListener("submit", (e) => e.preventDefault());
  $("smart-form").addEventListener("submit", (e) => e.preventDefault());
  $("demo-task").addEventListener("change", (e) => { if (e.target.value !== "") loadDemoTask(Number(e.target.value)); });
  $("add-stop").addEventListener("click", () => { state.stops.splice(state.stops.length - 1, 0, { fence_id: "", dwell_min: 20 }); renderStops(); });
  $("live").addEventListener("change", (e) => { $("depart").disabled = e.target.checked; $("task-date").disabled = e.target.checked; });
  try {
    await loadFences();
    state.meta = await api("/api/meta");
    renderForm();
    renderSmartForm();
    status("");
  } catch (err) { status(`載入失敗：${err.message}`, true); }
}

main();
