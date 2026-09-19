"use strict";

const $ = (id) => document.getElementById(id);

function setStatus(msg) {
  $("status").textContent = msg || "";
}

function pct(x) {
  return (x * 100).toFixed(1) + "%";
}

function fmt(value, unit) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return value.toFixed(1) + (unit || "");
  return String(value);
}

function trafficStatusLabel(status) {
  const map = {
    available: "已取得 (available)",
    stale: "資料稍舊 (stale)",
    missing: "此次無鄰近即時資料 (missing)",
    error: "查詢錯誤 (error)",
    unsupported: "不支援 (unsupported)",
  };
  return map[status] || status || "—";
}

async function predict() {
  const lat = $("lat").value.trim();
  const lon = $("lon").value.trim();
  if (!lat || !lon) {
    setStatus("請輸入經緯度");
    return;
  }
  const params = new URLSearchParams({
    lat,
    lon,
    city: $("city").value,
    vehicle_series: $("vehicle_series").value || "300",
    rain_mm: $("rain_mm").value || "0",
  });
  // Only send base_speed if the user typed one; otherwise server uses live road speed.
  const bs = $("base_speed").value.trim();
  if (bs) params.set("base_speed", bs);

  $("predict").disabled = true;
  setStatus("查詢即時路況並計算風險中…（首次呼叫 TDX 需數秒）");
  try {
    const res = await fetch("/api/predict?" + params.toString());
    const data = await res.json();
    if (!res.ok) {
      setStatus("預測失敗：" + (data.detail || data.error || res.status));
      return;
    }
    render(data);
    setStatus("完成 · " + new Date().toLocaleTimeString());
  } catch (err) {
    setStatus("連線錯誤：" + err.message);
  } finally {
    $("predict").disabled = false;
  }
}

function render(d) {
  $("result").classList.remove("hidden");

  // Prefer the context-adjusted probability/level for the headline gauge.
  const adjProb = d.adjusted_risk_probability != null ? d.adjusted_risk_probability : d.risk_probability;
  const level = (d.adjusted_risk_level || d.risk_level || "low").toLowerCase();
  const levelEl = $("level");
  levelEl.className = "level " + level;
  const labels = { low: "低風險 LOW", elevated: "偏高 ELEVATED", high: "高風險 HIGH" };
  levelEl.textContent = labels[level] || level;

  $("prob").textContent = pct(adjProb || 0);
  $("quality").textContent = "資料品質：" + (d.data_quality_status || "—");
  $("model").textContent = "模型：" + (d.model_version || "—");

  // Risk composition breakdown
  const adj = d.context_adjustment || {};
  const adjUl = $("adjustment");
  adjUl.innerHTML = "";
  const wu = d.warmup || {};
  addRow(adjUl, "基礎模型風險", pct(d.risk_probability || 0));
  if (wu.base_speed_kph != null) {
    addRow(adjUl, "└ 暖機車速", wu.base_speed_kph + " km/h（" + (wu.source === "live_road_speed" ? "即時路段" : wu.source === "default_fallback" ? "預設" : "手動") + "）");
  }
  const factors = adj.factors || [];
  if (factors.length === 0) {
    addRow(adjUl, "情境調整", "無（無可用情境或影響為 0）");
  } else {
    for (const f of factors) {
      const dir = f.logit_delta > 0 ? "↑ 提高" : (f.logit_delta < 0 ? "↓ 降低" : "→ 無變化");
      const src = f.source === "data_estimated" ? "資料估計" : "假設值(未驗證)";
      addRow(adjUl, f.factor, `${dir}  (${src})`);
    }
  }
  addRow(adjUl, "調整後風險", pct(adjProb || 0));

  const t = d.traffic_context || {};
  const traffic = $("traffic");
  traffic.innerHTML = "";
  const hasTraffic = t.quality_status === "available" || t.quality_status === "stale";
  const road = d.road_info || {};
  const trafficRows = [
    ["所在道路", road.road_name || "—"],
    ["道路類別", road.road_class_label || "—"],
    ["方向", road.bearing || "—"],
    ["最近偵測器", road.distance_m != null ? road.distance_m + " m" : "—"],
    ["狀態", trafficStatusLabel(t.quality_status)],
    ["路段車速", fmt(t.road_speed_kph, " km/h")],
    ["資料新鮮度", fmt(t.freshness_seconds, " s")],
    ["查詢城市", d.resolved_city || "—"],
    ["來源", "TDX 運輸資料流通服務"],
    ["資料性質", hasTraffic ? (t.is_mock ? "合成 (mock)" : "真實 (real)") : "此次未取得路況"],
  ];
  for (const [k, v] of trafficRows) addRow(traffic, k, v);

  const w = d.weather_context || {};
  const weather = $("weather");
  weather.innerHTML = "";
  const weatherRows = [
    ["狀態", w.quality_status],
    ["1 小時雨量", fmt(w.rainfall_1h_mm, " mm")],
    ["能見度", fmt(w.visibility_m, " m")],
    ["風速", fmt(w.wind_speed_mps, " m/s")],
    ["來源", w.provider_name],
    ["合成資料 (mock)", w.is_mock ? "是" : "否"],
  ];
  for (const [k, v] of weatherRows) addRow(weather, k, v);

  const notes = $("notes");
  notes.innerHTML = "";
  const allNotes = (d.notes || []).concat(d.provider_notes || []);
  if (!hasTraffic) {
    allNotes.push(
      "此位置附近 5 公里內沒有匹配到即時偵測器。可能不在台灣、或該路段無 VD 涵蓋（如市區小巷、山區）。試試主要道路或高速公路附近的座標。"
    );
  }
  if (d.context_used_by_model === false) {
    allNotes.push("基礎模型不含天氣/路況特徵；情境影響是透過『logit 加法情境調整層』套用在模型輸出之上（路況係數由本資料估計，天氣係數為未驗證假設）。");
  }
  for (const n of allNotes) {
    const li = document.createElement("li");
    li.textContent = n;
    notes.appendChild(li);
  }
}

function addRow(ul, key, value) {
  const li = document.createElement("li");
  li.textContent = key + "：" + (value === null || value === undefined ? "—" : value);
  ul.appendChild(li);
}

function useMyLocation() {
  if (!navigator.geolocation) {
    setStatus("瀏覽器不支援定位");
    return;
  }
  setStatus("取得定位中…");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      $("lat").value = pos.coords.latitude.toFixed(4);
      $("lon").value = pos.coords.longitude.toFixed(4);
      setStatus("已帶入你的位置");
    },
    (err) => setStatus("定位失敗：" + err.message)
  );
}

$("predict").addEventListener("click", predict);
$("locate").addEventListener("click", useMyLocation);
