# 前端 Dashboard 怎麼執行

這個資料夾有兩個不同的東西,別搞混:

| 檔案 | 用途 | 誰看 |
|------|------|------|
| **`dashboard.html`** | **簡報展示用**的靜態總覽(模型準度、車隊健康、派車示範、油耗分數) | 評審 / 簡報 |
| `index.html` + `app.js` | 互動式發派主控台(要跑 `hino-fuel serve`) | 發派人員操作 |

你簡報要用的是 **`dashboard.html`**。

---

## 執行 dashboard.html(兩步)

### 第 1 步:產生資料(資料已存在時可略過)

dashboard 讀的是同資料夾的 `dashboard_data.json`。它已經產生好了。
若資料有更新、要重新產生,在 **repo 根目錄**(`hino-safepilot/`)執行:

```bash
# 用 3.12 環境;xgboost 需要 libomp 路徑
DYLD_LIBRARY_PATH=/opt/homebrew/opt/libomp/lib .venv312/bin/python scripts/export_dashboard.py
```

(若要讓派車示範用真實模型油耗,先跑一次 `scripts/dispatch_demo_real.py` 再跑上面那行。)

### 第 2 步:開啟頁面

**不能直接雙擊 html**(瀏覽器會擋 fetch 讀 JSON)。要用本機小伺服器:

```bash
# 在這個 fuel_console 資料夾裡執行
cd apps/fuel_console
python3 -m http.server 8780 --bind 127.0.0.1
```

然後瀏覽器打開:**http://127.0.0.1:8780/dashboard.html**

按 `Ctrl + C` 停止伺服器。

---

## 常見問題

**頁面顯示「無法載入 dashboard_data.json」**
→ 代表你是直接雙擊開檔,或 JSON 不在同資料夾。請照上面用 `http.server` 開,並確認 `dashboard_data.json` 在這個資料夾。

**數字要怎麼更新?**
→ 重跑第 1 步的 `export_dashboard.py`,它會從 `reports/fuel/` 的最新產出重新整理 JSON。

**dashboard 和主控台差在哪?**
→ dashboard 是「唯讀展示」,主控台(`hino-fuel serve`,預設 http://127.0.0.1:8765)是「可互動操作派車」。簡報用 dashboard 比較穩,不依賴 ORS 線上資源。
