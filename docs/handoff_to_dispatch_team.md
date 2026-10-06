# 油耗模型交付說明(給派車/報告組員)

這份說明交付「油耗預測模型」給負責**派車決策**與**事後油耗報告**的組員。
請先讀完「最重要的三件事」,再看介面與安裝。

---

## 最重要的三件事

### 1. 兩個模型,用途不同 — 別用錯

| 模型 | 路徑 | 用在 | 為什麼 |
|---|---|---|---|
| **v1**(15 特徵) | `models/fuel/v1/` | **行前派車預測** | 出發前算得出所有特徵 |
| **v2**(19 特徵,+載重代理) | `models/fuel/v2_postonly/` | **事後油耗分析/報告** | 多了引擎負載/轉速,準度更高,但這些是行駛後才有的資料 |

- **行前派車** → 一定用 **v1**。v2 多的 4 個特徵(引擎負載/轉速)出發前不存在,硬用 v2 會缺特徵、算不出或得到虛假值。
- **事後報告**(行程跑完、要算這趟實際多省/多耗、或做車輛體檢)→ 用 **v2**,更準(事後整趟 MAPE 8.5% vs v1 的 ~11.6%)。
- v2 的 `model_meta.json` 已標 `do_not_use_for: 行前派車預測`,照著做就不會錯。

### 2. 金鑰自己申請,不要用交付包裡的

本交付包**不含** `.env` 與任何 API 金鑰(ORS / TDX / Gemini)。請自己免費申請並建立你自己的 `.env`
(需要哪些金鑰、變數名稱見 `docs/fuel_system.md` 的「安裝與執行」)。
沒有金鑰時系統會自動退回本機路網/歷史塞車地圖/固定範本,仍可運作。

### 3. 資料是私有的,別外流

模型、查表、OSM/DEM 內含真實場站座標與去識別化車輛資料,屬競賽私有資料。
不要 push 上公開 GitHub,不要把 enabledCode / 車牌 / 精確座標放進公開文件或簡報。

---

## 行前派車介面:`predict_trip_fuel`

```python
from hino_safepilot.fuel.fuel_predictor import predict_trip_fuel

result = predict_trip_fuel(
    origin=[121.3010, 24.9937],       # [經度, 緯度]
    destination=[120.6736, 24.1477],
    depart_time="2026-10-05T08:00:00+08:00",
    vehicle_ids=["<enabledCode1>", "<enabledCode2>"],  # 要比較的車(同車系)
    driver_types=["normal"],          # 駕駛類型;不分駕駛就用 normal
    use_live_traffic=False,           # True 用 TDX 即時路況(需金鑰)
    repo_root=".",
)
```

**回傳結構**(擇要):
```
result["routes"]            # 候選路線清單
  └ [i]["options"]          # 每條路線的 (車 × 駕駛) 選項,已依油耗升冪排序
        └ {"vehicle_id", "vehicle_type", "driver_type",
           "predicted_fuel_L",            # 預測整趟油耗(公升)
           "prediction_interval_L": [低, 高],   # 80% 區間
           "predicted_minutes"}
result["lowest_fuel_option"]  # 全部選項中最省的一個(建議派)
```

派車邏輯建議:在同車系候選車中,挑 `predicted_fuel_L` 最低者;需要一車一人配對時見 `drivers.match_unique`。

> 進階:多車 × 多駕駛一次算用 `FuelPredictor.predict_pairs(segments, pairs)`;
> 圍籬任務(多點停靠)用 `TaskPlanner`(見 `server.py` 的用法)。

---

## 事後報告:用 v2 做整趟油耗精算

事後要算「這趟實際油耗 vs 模型應有油耗」時,載入 v2 + 用 actual 模式特徵(含實際引擎負載)。
作法參考 `scripts/train_load_proxy_model.py` 的 `frame_plus(..., "actual")` 與預測段;
或 `scripts/error_diagnosis.py`(事後誤差拆解的完整範例)。

---

## 在你的機器跑起來

需求:Python 3.12。

```bash
py -3.12 -m venv .venv            # Windows;mac/Linux 用 python3.12 -m venv
# 啟用 venv 後:
pip install -e ".[dev,fuel]"      # 油耗系統依賴(xgboost 3.3.0、pyproj、osmium…)
# mac 若 xgboost 載入失敗:brew install libomp
```

交付包已含模型與查表,**不需重新訓練**即可呼叫 `predict_trip_fuel`。
若要自己重跑完整 pipeline(需 OSM/DEM,已附),見 `docs/fuel_system.md`。

---

## 交付包內容清單

| 類別 | 路徑 | 說明 |
|---|---|---|
| 行前模型 | `models/fuel/v1/` | 派車預測用 |
| 事後模型 | `models/fuel/v2_postonly/` | 事後報告用(含載重代理) |
| 查表 | `data/processed/fuel/artifacts/` | 行為/塞車地圖、車型基準、效率偏差、健康度 |
| 圍籬/慣用路線 | `data/processed/fuel/fences_derived.json`、`habitual_routes.json` | 派車以圍籬為單位 |
| 路網/高程 | `data/osm/`、`data/dem/` | 行前路線規劃 |
| 程式 | `src/hino_safepilot/`、`config/`、`pyproject.toml` | 介面與設定 |
| 文件 | `docs/fuel_system.md`(系統細節)、本檔 | 用法 |

**不含(刻意排除)**:`.env` 與金鑰、`models/fuel/v1_backup/`、原始競賽 Excel、個人識別資料。

> 本交付包**已含 OSM 路網與 DEM 高程**(`data/osm/`、`data/dem/`,約 540MB),
> 解壓後即可直接呼叫 `predict_trip_fuel`,無需自行下載地圖。壓縮檔約 650MB,請用雲端/隨身碟傳遞。

---

## 準度參考(測試期)

| 情境 | 模型 | 整趟 MAPE |
|---|---|---|
| 行前派車(熟悉路線) | v1 | ~11.6% |
| 行前派車(全新路線) | v1 | ~18.8% |
| 事後報告 | v2 | **8.5%**(700 系 10.1%) |

誤差主因:行前派車時「規劃路線 ≠ 實際路線」是大頭(非模型本身);700 系因載重變異較大,v2 的載重代理已顯著改善。
