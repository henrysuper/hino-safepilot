# 油耗派車主控台 — 怎麼跑起來（給拉 code 的人）

分支：`feature/ai-dispatch-console`　｜　跑起來後開 http://127.0.0.1:8765

---

## ⚠️ 先看這個：光拉 git 跑不起來

GitHub 上**只有程式碼**。模型、查表、路網、場站資料（`data/`、`models/`）是競賽私有資料，
**沒有放進 git**（被 `.gitignore` 排除）。你一定要另外拿到這些資料夾，放回專案裡才能跑。

要跟交付方拿、並放回原位的資料夾（放在專案根目錄 `fuel_handoff/` 底下）：

```
fuel_handoff/
├─ data/        ← 必要（OSM 路網、DEM 高程、查表、圍籬…約 900MB）
├─ models/      ← 必要（油耗模型 v1 / v2）
└─ reports/     ← 選用（沒有也能跑，只是「回測／分數／模型驗證」分頁會空白）
```

> 這些資料請走雲端／隨身碟傳，**不要**上傳公開網路。

---

## 三步跑起來（Windows / PowerShell）

需求：**Python 3.12 或 3.13**。

```powershell
# 1) 進專案資料夾
cd <你放的路徑>\fuel_handoff

# 2) 建虛擬環境並裝套件
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install `
  joblib narwhals numpy openpyxl pyarrow pydantic scikit-learn shap scipy `
  threadpoolctl xgboost osmium pandas pyproj requests tifffile google-genai

# 3) 啟動服務
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m hino_safepilot.fuel serve --repo-root $PWD --port 8765
```

看到這行就成功了：

```
Fuel console: http://127.0.0.1:8765  (local only ...)
```

用瀏覽器打開 **http://127.0.0.1:8765**。

> **為什麼不用 `pip install -e .`？** 如果你的使用者名稱／路徑含有 `$` 符號，`pip install -e .`
> 會因 setuptools 把 `$...` 當變數解析而失敗。所以改成「直接裝套件 + 設 `PYTHONPATH`」這招，最穩。
> 路徑沒有 `$` 的人也可以照用。

mac / Linux 的差異：`python3.12 -m venv .venv`，啟用後 `pip install ...`，
`PYTHONPATH=src python -m hino_safepilot.fuel serve --repo-root . --port 8765`。
（mac 若 xgboost 載入失敗：`brew install libomp`）

---

## 金鑰（可不設）

不設任何金鑰也能跑：路線走本機 OSM 路網、塞車用歷史地圖、報告文字用固定範本。
想要更好的效果再自己免費申請，在專案根目錄建 `.env`：

```
ORS_API_KEY=...        # 線上路線規劃（OpenRouteService）
TDX_CLIENT_ID=...      # 即時路況（TDX）
TDX_CLIENT_SECRET=...
GEMINI_API_KEY=...     # 讓推薦理由／報告改用 Gemini 生成
```

`.env` **不要**進 git（已被 `.gitignore` 擋）。

---

## 分頁一覽

| 分頁 | 做什麼 | 要哪些資料 |
|------|--------|-----------|
| 發派預測 | 多站圍籬任務，比較車×駕駛並解釋決策/路線/時間軸 | data、models |
| 智慧派車 | 單程 A→B 最佳車輛推薦 + 推薦理由 | data、models |
| 真實行程回測 | 測試期回測結果 | reports |
| 油耗分數與報告 | 每趟油耗分數 + 行程報告卡 | reports、data |
| 虛擬駕駛 | 駕駛行為與評估 | reports |
| 模型驗證 | 模型指標 | reports |

> 這是本機單人工具，只綁 `127.0.0.1`、沒有登入驗證，**不要**對外網開放。

---

## 跑不起來？

- **`ModuleNotFoundError: hino_safepilot`** → 忘了設 `PYTHONPATH`（見第 3 步）。
- **某些分頁空白** → 缺 `reports/`（跟交付方拿）。
- **預測/推薦報錯缺檔** → 缺 `data/` 或 `models/`，放回專案根目錄。
- **路線很慢** → 沒設 ORS 金鑰時走本機路網，距離遠的第一次會慢；設了 ORS 金鑰就快。
- **port 8765 被占用** → 換個埠：`--port 8780`。
