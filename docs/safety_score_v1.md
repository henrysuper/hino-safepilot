# Safety Score V1 與行前安全提醒

> Status: Hackathon prototype · deterministic · 權重與參考值 `UNRESOLVED`（未經 HINO／domain expert 驗證）

設定檔：`config/safety_score_v1.json`
程式：`src/hino_safepilot/analytics/scoring.py`、`score_pipeline.py`

## 1. 行後安全分數（每趟 0–100）

輸入只有一趟的 `TripSummary`（`hino-analytics build` 的輸出），不呼叫 LLM。

### 分項

| 分項 | 權重 | 事件（項目權重 / 參考值） |
|---|---|---|
| 安全警示 | 0.40 | PCS 0.30 / 1、疲勞 0.25 / 3、手機 0.15 / 5、分心 0.15 / 10、車道偏移 0.10 / 12、安全帶 0.05 / 20 |
| 平順駕駛 | 0.25 | 急減速 0.60 / 60、急加速 0.40 / 10 |
| 速度控制 | 0.20 | 超速 1.00 / 35 |
| 車輛負荷 | 0.15 | 引擎過載 0.70 / 30、怠速比例 0.30 / 30% |

事件參考值單位為「次／100 km」。

### 公式

```text
rate_i          = count_i / max(distance_km, 10) × 100      （怠速用 idle_pct）
category_ratio  = Σ w_i × rate_i / ref_i  ÷  Σ w_i          （缺值項目不計入）
category_score  = 100 × 0.7 ^ category_ratio
overall         = Σ category_weight × category_score
```

- 零事件 = 100 分；剛好等於參考值 = 70 分；兩倍參考值 ≈ 49 分。事件越多分數只會越低。
- **最低計分距離 2 km**：更短的行程標記 `insufficient_exposure`，不給分。
- **距離下限 10 km**：避免短程 1 次事件就讓每 100 km 次數暴增（`RATE_DISTANCE_FLOOR_APPLIED`）。
- **高風險上限**：本趟出現 PCS 或疲勞事件，總分最高 79（不能拿 A/B）。
- 等級：A ≥ 90、B ≥ 80、C ≥ 70、D ≥ 60、E < 60。
- 扣分原因：分項失分依各事件的 `w_i × rate_i / ref_i` 比例分攤，再乘上分項權重，取前 3 名。

參考值刻意設在本地車隊分布的較高分位附近（整數化），**不是**安全標準。`behavior_goal_v0.json` 的 reference_rate 用途不同（挑選改善目標），不可混用。

## 2. 行前安全提醒

`build_predrive_briefing` 只讀取 `trip_end < 本趟出發時間` 的同車行程（vehicle-linked，非駕駛身分）。

| 欄位 | 定義 |
|---|---|
| 上一趟分數 | 出發前最近一趟有分數的行程 |
| 近 7 天平均 | 出發前 7 天內有分數行程的平均 |
| 趨勢 | 與再前 7 天平均相比，差 ≥ 3 分為進步／退步 |
| 最需加強分項 | 近 7 天分項平均最低者 |

提醒規則（高優先排前面）：

1. 近 7 天有 PCS／疲勞／手機／分心事件 → 對應的高優先提醒
2. 近 7 天平均比前一週下降 ≥ 3 分 → 高優先提醒
3. 最弱分項與操作建議
4. 近 7 天沒有可評分行程 → 建立基準提醒

既有 `DailyImprovementGoal` 在畫面上有獨立的「今日改善目標」卡片，不重複列入提醒清單。

## 3. 指令

```powershell
python -m hino_safepilot.analytics score                       # 產生 data/processed/safety_scores/
python -m hino_safepilot.analytics predrive  --trip-id <id>     # 行前提醒 JSON
python -m hino_safepilot.analytics postdrive --trip-id <id>     # 行後分數 + 報告 JSON
python -m hino_safepilot.analytics export-demo                  # 自動挑一趟中位數行程給 demo
python scripts/serve_demo.py                                    # http://127.0.0.1:8000/?source=local
```

`score` 拒絕覆寫既有輸出；要重算請先刪除 `data/processed/safety_scores/`。
AI 文字預設使用 mock；`--provider gemini` 在真實資料上須先取得人工核准。

## 4. 隱私

- 分數、提醒與 demo views 都只寫入 `data/processed/`（Git 忽略）。
- `export-demo` 的輸出不含 trip / vehicle 識別碼、時間戳或座標。
- `serve_demo.py` 只綁定 127.0.0.1，且 `/local-data/` 只對應 `*.local.json`。
