# HINO SafePilot — Team Setup Guide

本文件說明如何從 GitHub 取得 HINO SafePilot、配置 HINO 私有資料、Python 環境與 Gemini API Key。

---

# 1. 取得 Repository 權限

本專案 GitHub Repository：

https://github.com/henrysuper/hino-safepilot

目前為 Private Repository。

請先確認已接受 GitHub collaborator 邀請，否則無法 clone repository。

---

# 2. Clone Repository

## 方法 A：使用 VS Code

1. 開啟 VS Code
2. 按 `Ctrl + Shift + G` 開啟 Source Control
3. 選擇：

   `Clone Repository`

4. 輸入：

   `https://github.com/henrysuper/hino-safepilot.git`

5. 選擇本機要存放的位置
6. Clone 完成後選擇：

   `Open`

---

## 方法 B：使用 Terminal

```powershell
cd "你想存放專案的位置"

git clone https://github.com/henrysuper/hino-safepilot.git

cd hino-safepilot

code .
```

## 切換到 safepilot 分支

目前開發中的功能（含安全分數）在 `safepilot` 分支：

```powershell
git switch safepilot
git pull origin safepilot
```

之後要更新程式碼，只要再執行一次 `git pull origin safepilot`。

---

# 3. 建立 Python 環境（只需做一次）

需要 **Python 3.12**（安裝時勾選「Add to PATH」）。在專案根目錄執行：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

> 本文件的指令都直接寫 `.venv\Scripts\python.exe`，不需要先啟用虛擬環境，也不會遇到 PowerShell 執行原則的錯誤。

---

# 4. 放入比賽資料（只需做一次）

比賽資料放在團隊的 Google 雲端硬碟，**不在 GitHub 上**。雲端硬碟的資料夾名稱和專案不同，請依下表複製到對應位置，**不要整包複製進 `data\raw`**：

| 雲端硬碟資料夾 | 複製到專案的 |
|---|---|
| `data raw` 內的檔案 | `data\raw\` |
| `data reference` 內的檔案 | `data\reference\` |
| `data interim` 內的資料夾 | `data\interim\` |
| `data processed` 內的資料夾 | `data\processed\` |
| `models risk` 內的檔案 | `models\risk\`（`v0_logistic`、`v1_xgboost` 各自對應） |
| `Event Type_0929補充.xlsx`（主辦方事件定義表） | `data\reference\` |

Google 雲端硬碟會在每個資料夾放 `desktop.ini`，讀取 parquet 時會出錯（`Parquet magic bytes not found`）。複製完請在專案根目錄執行一次清除：

```powershell
Get-ChildItem data, models -Recurse -Force -Filter desktop.ini | Remove-Item -Force
```

放好後確認以下檔案存在：

```text
data\processed\analytics\trip_summaries.jsonl
data\processed\analytics\improvement_goals.jsonl
```

並確認資料不會被 Git 追蹤：`git status` 不應出現任何 `data/` 開頭的檔案。

---

# 5. 確認環境正常

```powershell
.venv\Scripts\python.exe -m pytest
```

最後一行顯示全部 `passed` 即正常。

---

# 6. 執行安全分數與 Demo

### 6.0 重建行程分析（Safety Score V1.1 起需要做一次）

雲端硬碟上的 `data\processed\analytics` 是舊版，沒有統計「鏡頭遮蔽」和「抽菸」事件。請刪除後重建（約 20–30 秒）：

```powershell
Remove-Item -Recurse -Force data\processed\analytics
.venv\Scripts\python.exe -m hino_safepilot.analytics build
```

成功會印出 `trip_summary_count`。若跳過這步，6.1 會停止並提示要重建。

### 6.1 計算安全分數

```powershell
.venv\Scripts\python.exe -m hino_safepilot.analytics score
```

成功會印出評分統計，結果寫入 `data\processed\safety_scores\`（Git 忽略）。

### 6.1b 對應虛擬駕駛

需要油耗組的虛擬駕駛檔案（`data/processed/fuel/` 底下的 `driver_assignment.parquet`、`vehicle_aliases.json`、`artifacts/virtual_drivers.json`、`artifacts/mock_plates.json`）。放好後：

```powershell
.venv\Scripts\python.exe -m hino_safepilot.analytics drivers
```

這 4 個檔案在團隊雲端硬碟的 `1006` 資料夾（`1006\data\processed\fuel\`）。Demo 的真實資料模式以「虛擬駕駛」為單位，所以 6.2 需要先完成這一步；沒有這些檔案時，Demo 會整頁改用 Synthetic Demo。

### 6.1c 路況比較與 60 秒預警回放（選用）

路況比較需要油耗組的路網與高程資料，從雲端 `1006\data\` 複製：

- `osm\road_index_v2.npz` → `data\osm\`
- `dem\moi_dem20m\不分幅_全台及澎湖DEM\dem_20m.tif` 與 `dem_20m.tfw` → `data\dem\moi_dem20m\不分幅_全台及澎湖DEM\`
- `processed\fuel\artifacts\` 整個資料夾 → `data\processed\fuel\artifacts\`
- `processed\fuel\trip_scores_test.parquet` → `data\processed\fuel\`（安全 × 油耗卡片用）

```powershell
.venv\Scripts\python.exe -m pip install -e ".[fuel]"
.venv\Scripts\hino-fuel.exe segments --repo-root .
.venv\Scripts\python.exe -m hino_safepilot.analytics road
.venv\Scripts\python.exe -m hino_safepilot.analytics alerts
```

沒做這步也能跑 Demo，只是行後報告不會出現「路況公平比較」與「60 秒預警回放」。

### 6.2 產生 Demo 資料

```powershell
.venv\Scripts\python.exe -m hino_safepilot.analytics export-demo --provider mock
```

自動挑一趟分數接近中位數的行程。`--provider mock` 使用內建文字範本，不會把資料送到 Gemini；真實資料送 Gemini 前須先取得團隊核准。

### 6.3 開啟 Demo 網頁

```powershell
.venv\Scripts\python.exe scripts\serve_demo.py
```

| 網址 | 內容 |
|---|---|
| http://127.0.0.1:8000/?source=local | 本機真實分析結果 |
| http://127.0.0.1:8000 | Synthetic Data（對外展示用） |

在 PowerShell 按 `Ctrl + C` 關閉。

> ⚠️ 真實資料畫面僅限本機檢視，**請勿截圖或傳到群組**；對外展示請用 Synthetic 版本。

---

# 7. 常見問題

| 狀況 | 解決方式 |
|---|---|
| `Refusing to overwrite safety score output` | 已算過；要重算請刪除 `data\processed\safety_scores` 後重跑 6.1 |
| `Run hino-analytics build first` | `data\processed\analytics` 沒放好，回到第 4 節 |
| `Trip summaries do not track ['lens_ward_off', ...]` | 行程分析是舊版，做 6.0 |
| `Parquet magic bytes not found` | 有 `desktop.ini`，執行第 4 節的清除指令 |
| 網頁顯示「資料暫時無法取得」 | 確認網址有 `?source=local`，且 6.2 已執行 |
| `Address already in use` | 改用 `scripts\serve_demo.py --port 8001`，網址改成 8001 |
| `py` 不是可辨識的指令 | Python 安裝時未勾選 PATH，請重新安裝 |

### 調整分數規則

權重與參考值在 `config\safety_score_v1.json`，公式說明見 `docs\safety_score_v1.md`。修改後刪除 `data\processed\safety_scores`，再重跑 6.1、6.2。
