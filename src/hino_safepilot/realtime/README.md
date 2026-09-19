# Realtime 60 秒風險預測 Demo

把已訓練的 HINO 風險模型接上「即時外部情境」，做成一個可操作的 web / API demo：
給一個當下的位置與時間 → 抓即時路況（TDX，真實）與天氣（目前為合成）→ 用模型輸出「未來 60 秒風險機率」與風險等級。

這是**推論 / 展示用途**，不做任何模型訓練、不寫入任何私有遙測列、不把外部資料當成競賽原始資料。

---

## 快速開始

### 1. 準備憑證（放進 `.env`，不會進版控）

專案根目錄的 `.env`（已被 `.gitignore` 忽略）需要 TDX 憑證才能抓真實路況：

```
TDX_CLIENT_ID=你的_client_id
TDX_CLIENT_SECRET=你的_client_secret
CWA_API_KEY=            # 留空 → 天氣使用合成資料 (mock)
```

- TDX 憑證：到 TDX 運輸資料流通服務會員平台申請（免費），建立應用後取得 Client Id / Secret。
- CWA 天氣金鑰：氣象開放資料平臺會員申請；目前留空時，天氣會用「合成 provider」並明確標記 `is_mock=true`。

> 安全：`.env` 內含機密，切勿提交或外流。若 Secret 曾外洩，請到 TDX 平台重設 (regenerate)。

### 2. 啟動 web 伺服器

```
python -m hino_safepilot.realtime serve --host 127.0.0.1 --port 8000
```

瀏覽器開 `http://127.0.0.1:8000`，輸入經緯度（或按「用我的位置」）→「預測風險」。

### 3. 或用命令列做單次預測

```
python -m hino_safepilot.realtime predict --lat 25.047 --lon 121.517
```

---

## HTTP API

| 端點 | 說明 |
|---|---|
| `GET /` | Demo 網頁 |
| `GET /api/health` | 就緒狀態；回報是否有 TDX / CWA 憑證、天氣模式 |
| `GET /api/predict?lat=..&lon=..` | 單次 60 秒風險預測（JSON） |

`/api/predict` 參數：

| 參數 | 必填 | 預設 | 說明 |
|---|---|---|---|
| `lat`, `lon` | ✅ | — | 當下經緯度 |
| `city` | | `auto` | TDX 城市；`auto` 依經緯度自動判斷 |
| `vehicle_series` | | `300` | 車系代碼 |
| `heading` | | — | 行進方向 0–360 |
| `rain_mm` | | `0` | 合成 1 小時雨量（天氣為 mock 時使用） |
| `visibility_m` | | `10000` | 合成能見度 |
| `warmup_seconds` | | `300` | 合成暖機遙測長度（見下方限制） |
| `base_speed` | | `45` | 合成暖機基準車速 |

回應重點欄位：

```jsonc
{
  "risk_probability": 0.055,          // 未來 60 秒風險機率
  "risk_level": "low",                // low / elevated / high（原型 UI 分級）
  "prediction_horizon_sec": 60,
  "data_quality_status": "valid",     // valid / degraded / insufficient
  "model_version": "v0_logistic_1.0",
  "resolved_city": "Taipei",          // 自動判斷的查詢城市
  "road_info": {                       // 最近偵測器所在道路（真實 TDX）
    "road_name": "市民大道高架道路",
    "road_class_label": "市區快速道路/高架 (Urban expressway)",
    "bearing": "E",
    "distance_m": 0.0
  },
  "traffic_context": { "quality_status": "available", "road_speed_kph": 50.0, "is_mock": false },
  "weather_context": { "quality_status": "available", "is_mock": true },
  "context_used_by_model": false,      // 見下方「重要限制」
  "notes": [ ... ]
}
```

---

## 運作方式

1. **滾動遙測緩衝區**（`features.py`）：保存最近 300 秒的遙測點，重算模型訓練時用的窗口統計特徵（車速、引擎、事件…）。歷史不足的特徵留 NaN，交給模型內建的 imputer 處理，並把 `data_quality_status` 誠實降級。
2. **即時路況**（`live_context.py`）：用 TDX OAuth2 憑證抓 VDLive（車輛偵測器即時速度）＋靜態 VD（座標、道路名稱、道路類別）。
   - **自動城市 + 多來源 fallback**：先查所在縣市，若附近無偵測器，自動擴展到相鄰縣市與國道，找到最近的即時偵測器即停。
   - **快取 + 節流**：靜態資料整個 server 生命週期快取；即時資料 45 秒快取；來源間留間隔，避免觸發 TDX 限流。
3. **天氣**：目前為 `SyntheticContextProvider`，快照一律標記 `is_mock=true`。填入 `CWA_API_KEY` 後可改接真實 CWA。
4. **模型推論**（`engine.py`）：組成 163 維特徵 → 已訓練的 `RiskModelBundle` → 校準機率 + 風險等級。

---

## 情境風險調整層（logit 加法，資料驅動）

基礎模型（`v0_hino`）本身不含天氣/路況特徵。為了讓外部情境**真正影響最終風險分數**又**不需重訓模型**，加了一層透明的調整：

```
調整後機率 = sigmoid( logit(模型機率) + Σ 情境 logit 貢獻 )
```

- 在 logit 尺度相加，數學上與邏輯迴歸一致、不會超過 [0,1]、多因子自然疊加；無情境時完全等於原模型。
- **路況/壅塞係數是從 HINO 資料本身估計的**（`estimate_context.py`）：比較不同壅塞程度下「60 秒內核心事件率」與整體率的對數比值。可稽核、有資料依據。
  - 執行：`python -m hino_safepilot.realtime.estimate_context`（讀 `data/interim`，寫 `config/context_risk_v0.json`）
  - 實測發現（反直覺但合理）：**順暢路段風險較高、嚴重壅塞風險較低**，因為核心事件多為急減速/PCS/車道偏移/疲勞/分心，車速快時更易發生。
- **天氣係數為假設值**（無 2025 歷史天氣可驗證），一律標記 `assumption_unvalidated`，可在 config 關閉。
- API 回傳 `context_adjustment.factors`，每個因子含 `logit_delta` 與 `source`（`data_estimated` / `assumption_unvalidated`），畫面分層顯示「基礎風險 → 各因子 → 調整後風險」。

要進一步做成「模型內學習」而非「後處理調整」，仍需依 `docs/external_context_plan.md` 的 `v1_hino_traffic` 重訓（需 2025 歷史情境資料）。程式也預留 `CONTEXT_FEATURE_MAP` 接點。

## 重要限制（誠實說明）

- **`context_used_by_model: false`**：基礎模型仍是純車輛特徵；情境是透過上述**後處理調整層**影響最終分數，而非模型內學到的。路況係數有資料依據，天氣係數為未驗證假設。
- **暖機遙測是合成的，但車速由即時路況驅動**：即時預測需要「過去 300 秒」的車輛遙測。Demo 用一段合成行車資料暖機。為了讓不同地點/路況產生不同的基礎風險，**暖機的基準車速取自該位置的即時 TDX 路段車速**（塞車路段→低速、順暢路段→高速），而非固定值。
  - 優先序：`base_speed` 手動參數 > 即時路段車速 > 預設值（該處無路況時）。
  - API 回傳 `warmup.base_speed_kph` 與 `warmup.source`（`live_road_speed` / `default_fallback` / `explicit_param`），畫面也會顯示，完全透明。
  - 誠實限制：模型有 163 個特徵，此法只讓「車速」一個維度隨地點變化，其餘（加速度、事件歷史、引擎狀態）仍為合成固定樣式，因此不同地點的基礎風險差異有限。要完整反映真實駕駛，仍須由車輛本身的即時遙測串流餵入緩衝區。
- **風險分數目前反映的是暖機資料的平順程度**，非真實駕駛風險。這是原型限制。
- **模型為研究原型**（`NOT_PRODUCTION_READY`）：誤報率偏高、警報閾值/時區等仍待人工核定。請勿當成生產級安全系統。
- **沒有 TDX 偵測器涵蓋的地點**（山區、離島、國外）會誠實回報 `missing`，並盡量提供最近的道路資訊。

---

## 檔案結構

```
src/hino_safepilot/realtime/
  engine.py         # 串接情境 provider + 模型推論
  features.py       # 滾動緩衝區 + 163 維特徵組裝
  live_context.py   # TDX 即時抓取、自動城市、快取/節流、合成天氣
  server.py         # stdlib http.server 的 web/API（無第三方框架）
  cli.py            # hino-realtime predict / serve
  web/              # 前端 index.html / app.js / styles.css
```
