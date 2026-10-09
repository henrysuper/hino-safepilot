# Safety Score V1.2 與行前安全提醒

> Status: Hackathon prototype · deterministic · 權重與參考值 `UNRESOLVED`（未經 HINO／domain expert 驗證）

設定檔：`config/safety_score_v1.json`（version `safety-score-v1.2`）、`config/event_definitions_v1.json`（主辦方事件定義）
程式：`src/hino_safepilot/analytics/scoring.py`、`score_pipeline.py`

## 1. 行後安全分數（每趟 0–100）

輸入只有一趟的 `TripSummary`（`hino-analytics build` 的輸出），不呼叫 LLM。

### 分項

| 分項 | 權重 | 事件（項目權重 / 參考值） |
|---|---|---|
| 安全警示 | 0.40 | PCS 0.27 / 1、疲勞 0.22 / 3、手機 0.13 / 5、分心 0.13 / 10、車道偏移 0.10 / 12、鏡頭遮蔽 0.10 / 5、安全帶 0.05 / 20 |
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
- **最低計分距離 0.5 km**：更短的行程（多半是 0 km 的暖車或場站移車）標記 `insufficient_exposure`，不給分。
- **短程（0.5–2 km）**：照常評分，但標示「短程・權重低」。單趟分數僅供參考；所有多趟平均（駕駛平均、近 7 天平均、分項平均、每月平均、趨勢）都**依公里數加權**，不到 1 公里的行程合計只佔總里程 0.4%，不會把平均拉高。鏡頭遮蔽等上限規則對短程一樣有效（約 19% 的鏡頭遮蔽發生在 2 km 以下的行程）。
- **距離下限 10 km**：避免短程 1 次事件就讓每 100 km 次數暴增（`RATE_DISTANCE_FLOOR_APPLIED`）。
- **高風險上限**：本趟出現 PCS 或疲勞事件，總分最高 79（不能拿 A/B）；出現鏡頭遮蔽，總分最高 89（不能拿 A）。
- **DMS 可靠度**：出現鏡頭遮蔽（`lens_ward_off`）時，`dms_reliability = camera_blocked` 並加上 `DMS_CAMERA_BLOCKED_EVENTS_MAY_BE_UNDERCOUNTED`，因為遮蔽期間疲勞、手機、分心、抽菸、安全帶偵測可能失效。鏡頭遮蔽在車隊中集中於少數行程且常為長時間狀態，因此同時計入分項並設上限，避免「遮住鏡頭反而高分」。
- 等級：A ≥ 90、B ≥ 80、C ≥ 70、D ≥ 60、E < 60。
- 扣分原因：分項失分依各事件的 `w_i × rate_i / ref_i` 比例分攤，再乘上分項權重，取前 3 名。

參考值刻意設在本地車隊分布的較高分位附近（整數化），**不是**安全標準。

### 事件定義

扣分原因會附上 `config/event_definitions_v1.json` 的官方定義（由主辦方事件表 0929 整理，原始 Excel 只放在本機 `data/reference/`，不進 Git）。急加速、急減速、超速、引擎過載是車機依速度／負載門檻判定；疲勞、手機、分心、抽菸、安全帶、鏡頭遮蔽是 DMS 鏡頭判定，主辦方未提供數值門檻。`behavior_goal_v0.json` 的 reference_rate 用途不同（挑選改善目標），不可混用。

## 2. 行前安全提醒

`build_predrive_briefing` 只讀取 `trip_end < 本趟出發時間` 的同車行程（vehicle-linked，非駕駛身分）。

| 欄位 | 定義 |
|---|---|
| 上一趟分數 | 出發前最近一趟有分數的行程 |
| 近 7 天平均 | 出發前 7 天內有分數行程的平均（依公里數加權） |
| 趨勢 | 與再前 7 天平均相比，差 ≥ 3 分為進步／退步 |
| 最需加強分項 | 近 7 天分項平均最低者 |

提醒規則（高優先排前面）：

1. 近 7 天有 PCS／疲勞／鏡頭遮蔽／手機／分心事件 → 對應的高優先提醒
2. 近 7 天平均比前一週下降 ≥ 3 分 → 高優先提醒
3. 最弱分項與操作建議
4. 近 7 天沒有可評分行程 → 建立基準提醒

既有 `DailyImprovementGoal` 在畫面上有獨立的「今日改善目標」卡片，不重複列入提醒清單。

## 3. 指令

```powershell
python -m hino_safepilot.analytics score                       # 產生 data/processed/safety_scores/
python -m hino_safepilot.analytics predrive  --trip-id <id>     # 行前提醒 JSON
python -m hino_safepilot.analytics postdrive --trip-id <id>     # 行後分數 + 報告 JSON
python -m hino_safepilot.analytics drivers                      # 對應虛擬駕駛（見第 5 節）
python -m hino_safepilot.analytics export-demo                  # 每位駕駛一個 demo 檔
python scripts/serve_demo.py                                    # http://127.0.0.1:8000（自動使用本機資料）
```

`score` 拒絕覆寫既有輸出；要重算請先刪除 `data/processed/safety_scores/`。若 `trip_summaries.jsonl` 是 V1.1 之前建立的（缺少 `lens_ward_off`／`smoking`），`score` 會停止並要求先刪除 `data/processed/analytics/` 重跑 `hino-analytics build`。
AI 文字預設使用 mock；`--provider gemini` 在真實資料上須先取得人工核准。

## 4. 隱私

- 分數、提醒與 demo views 都只寫入 `data/processed/`（Git 忽略）。
- `export-demo` 的輸出不含 trip / vehicle 識別碼、時間戳或座標。
- `serve_demo.py` 只綁定 127.0.0.1，且 `/local-data/` 只對應 `*.local.json`。

## 5. 安全歷史紀錄（虛擬駕駛）

資料沒有駕駛身分。`hino-fuel drivers`（油耗派車主控台）會建立**模擬駕駛名單**，並把每個真實的「車-日」分配給同車系的一位模擬駕駛。`hino-analytics drivers` 沿用同一份分配，所以安全紀錄和派車主控台對「誰開了哪一趟」的認定一致。

- 對應方式：行程的車輛 + 出發日期 → `driver_assignment.parquet` 的 `(enabled_code, day)` → 駕駛。沒有分配的車-日不歸給任何人（manifest 的 `unassigned_trip_count`）。
- 每位駕駛：平均分數、等級分布、四個分項平均、最常被扣分的 3 項、最近 10 趟 vs 前 10 趟趨勢（只用 2 km 以上的行程、依公里數加權；資料月份不連續，用趟數比天數穩定）、鏡頭遮蔽趟數、每月平均、逐趟紀錄（模擬車牌）。
- 駕駛名單、車牌與行程分配是**模擬資料**；分數來自真實行為。畫面與輸出都標示這點。

需要的油耗端輸出（Git 忽略，向油耗組拿或自行執行 `hino-fuel segments → train → drivers`）：

```text
data/processed/fuel/driver_assignment.parquet
data/processed/fuel/vehicle_aliases.json
data/processed/fuel/artifacts/virtual_drivers.json
data/processed/fuel/artifacts/mock_plates.json
```

```powershell
python -m hino_safepilot.analytics drivers        # 產生 data/processed/driver_safety/
python -m hino_safepilot.analytics export-demo    # drivers.local.json + driver-Dxx.local.json
```

Demo 頂端的「虛擬駕駛」選單控制整個頁面：

- **行前安全提醒**：這位駕駛「下一趟出發前」的提醒。今日改善目標與提醒清單只用**這位駕駛自己**過去的行程計算（把行程改用駕駛當作 profile key，沿用同一套 `goal_baselines`／`WeaknessScorer`）。
- **行後安全報告**：預設是最近一趟有分數的行程；可切換最近 15 趟有分數的行程。
- **安全歷史紀錄**：點選行程可展開扣分明細，並可「開啟這趟的完整安全報告」跳到行後報告。

第 4 個分頁「安全歷史紀錄」：選擇駕駛 → 摘要、每趟分數走勢、分項平均、常見扣分、行程紀錄（最近 15 趟可點開扣分明細）。尚未產生本機駕駛資料時，該分頁自動改用 Synthetic Demo 並註明。

### 5.1 同車系排名

每位駕駛依平均安全分數，在**同車系**內排名（1 = 最好）。有分數的行程不到 10 趟的駕駛不排名。排名對象是模擬駕駛，見 5.3。

### 5.2 目標達成回饋（行前目標 → 行後結果）

每一趟 2 km 以上、有分數的行程，先用**這趟出發前**、這位駕駛自己的行程算出當時的今日改善目標（與行前提醒同一套 `goal_baselines`／`WeaknessScorer`），再檢查這趟是否達成：

- 目標值 = 目標指標在出發前近 7 天的平均（`evidence_summary.recent_value`）。
- 這趟的值用與安全分數相同的算法（事件次數 ÷ max(里程, 10 km) × 100；怠速用怠速比例）。
- 「越高越差」的指標：這趟 ≤ 目標值即達成；「越低越差」（燃油效率）：≥ 目標值即達成。
- `streak` = 連續達成的趟數；未達成歸零，沒有目標的行程不影響連續紀錄。

畫面：行後報告最上方「這趟的目標達成了嗎？」、行前目標卡下方「上一趟目標」、安全歷史紀錄表的「目標」欄（✓／✗）。

> `UNRESOLVED`：「不高於近期平均」是偏寬鬆的門檻（真實資料中約 73% 的行程達成），用意是先建立正向回饋；是否改成「比平均再少 10%」等更嚴格的門檻，屬產品決策。

### 5.3 模擬資料的限制（簡報時必須說明）

模擬分配會依當天的行為風格挑選駕駛（`drivers.py` 的 `assign_drivers`），讓每位模擬駕駛的風格保持一致。因此**駕駛之間的差異與排名有一部分是分配規則造成的**，不能解讀為「找出了比較危險的駕駛」。每一趟的行為與分數是真實的；這部分展示的是流程，接上真實排班後即可直接套用。畫面頂端的說明文字已寫明這點。

## 6. 路況公平比較與路況調整後分數

`hino-analytics road` 讀取油耗組的路段（`hino-fuel segments` 產生的 `segments_5km.parquet`）與行為地圖（`data/processed/fuel/artifacts/`），對每一趟算出：

- **實際**：急加速、急減速次數、怠速分鐘、超速秒數（油耗組的路段計數）。
- **一般**：同樣道路類型 × 時段 × 500 公尺網格下，一般駕駛的期望值（油耗組的 prior × grid factor，未重新訓練）。

**路況調整後分數**：對急加速、急減速、超速、怠速四項，用「這趟路段的一般值 ÷ 全車隊的一般值」（限制在 0.5–2 倍）去除實際值，再套同一個安全分數公式。市區比重高的行程，急減速的「一般值」本來就高，調整後不會被不公平地扣分。其他項目（DMS、PCS、車道偏移、引擎過載）不調整。官方安全分數維持不變，路況調整後分數是並列的參考。

真實資料（1,878 趟有路段資料）：平均 +1.5 分；市區比重 ≥ 50% 的行程平均 +2.2 分，< 20% 的行程 +0.7 分。

## 7. 60 秒預警回放

`hino-analytics alerts` 用凍結的 XGBoost 模型（`v1_xgboost_1.0`，不重新訓練）為資料集中的每個 60 秒預測點打分，再逐趟重播 Alert Policy V1（進入門檻 0.5、冷卻 90 秒），並把每次提醒連到 60 秒內的下一個核心風險事件（急減速、PCS、車道偏移、疲勞、分心）。

- 這些資料錄製時還沒有 SafePilot，**駕駛當時沒有聽到提醒**；回放只回答「如果當時有 SafePilot，會在哪裡提醒、之後發生了什麼」，不宣稱駕駛有反應。
- 訓練期的行程模型看過，結果偏樂觀，畫面會標示資料期間。
- 測試期：514 次提醒中有 309 次（60%）在 60 秒內發生事件，與 Label V0 的標記完全一致。

## 8. 安全 × 油耗交叉分析

`export-demo` 若找到油耗組的 `data/processed/fuel/trip_scores_test.parquet`（測試期每趟油耗分數），會把安全分數和油耗比值（實際油耗 ÷ 同路線「一般駕駛」的預期油耗；預期值不含該駕駛自己的行為，所以駕駛風格的影響仍在比值中）對在一起，顯示在「安全歷史紀錄」的「安全 × 油耗」卡片。

真實資料結果（275 趟兩邊都有評分）：**沒有顯著關聯**（Spearman ρ = −0.07，p = 0.28）。依車系分開看方向都是「越安全越省油」（300 系 −0.13、500 系 −0.21、700 系 −0.19），但都未達 p < 0.05。D、E 級樣本少（19、6 趟）。

**目前不在 Demo 畫面顯示**（團隊決定，2026-10-07）；分析仍會輸出到本機的 `drivers.local.json` 的 `safety_fuel` 欄位，需要時可再放回畫面。原本的呈現方式：誠實當成發現——安全與省油是兩個獨立面向，因此 SafePilot（安全）與油耗主控台（省油）是互補的兩套回饋。少於 20 趟時不顯示；文字會依實際的顯著性與方向自動調整，不寫死結論。

## 9. V1.2 變更：0.5 km 門檻與依公里數加權

| | V1.1 | V1.2 |
|---|---|---|
| 最低計分距離 | 2 km | 0.5 km |
| 有分數的行程 | 2,125 趟（60%） | 2,774 趟（78%） |
| 駕駛平均分數 | 每趟算一次 | 依公里數加權 |
| 駕駛平均分數範圍（中位數） | 72–96（84） | 75–96（83） |
| 目標達成檢查 | 有分數的行程 | 只看 2 km 以上的行程 |

單趟等級分布中 A 的比例會上升（短程多半事件很少），所以對外說明分數時請以**駕駛的加權平均**為主，不要用單趟 A 的比例。

### 7.1 第 2 頁「未來 60 秒風險預測」：模擬行駛

第 2 頁跟著頂端選的駕駛切換，用**模擬行駛**展示預測：選一趟這位駕駛有足夠資料的行程，按「▶ 開始模擬行駛」，行程每 10 秒往前一個預測點（可選 10／30／60 倍速，也可拖拉時間軸）。畫面只顯示「現在」已知的資訊：

- 圓環：模型此刻預測的「未來 60 秒風險」（≥ 50% 高風險、≥ 35% 需留意）。
- 提醒決定：此刻 Alert Policy V1 的決定——播報（「前方注意，請放慢車速」／升級「風險持續升高，請立即減速」）、已提醒過不重複、需留意但不出聲、風險解除。勾選「語音提醒」會用瀏覽器語音念出播報。
- 模型現在看到的資料：近 1 分鐘平均車速、近 5 分鐘急減速與分心次數（都只來自「現在」以前）。
- 風險曲線只畫到「現在」；事件發生後才標出。每次提醒在 60 秒內顯示「等待結果」，之後揭曉「✔ N 秒後發生○○，提前預警」或「60 秒內沒有發生事件」。

這是用歷史資料逐點重現的**預測**：模型在每個點都只用過去 5 分鐘、不知道之後發生什麼；資料錄製時還沒有 SafePilot，所以駕駛當時沒有聽到提醒。`hino-analytics alerts` 會為每個預測點存下風險、提醒決定與上述三個輸入；舊版輸出請刪除 `data/processed/trip_alerts/` 後重跑。沒有回放資料時，第 2 頁退回原本的 Synthetic 預覽。
