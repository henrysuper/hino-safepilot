# HINO SafePilot

2026 和泰 AI 黑客松 — HINO 組

SafePilot 是以商用車駕駛為核心的 AI 安全駕駛教練原型。

系統目前聚焦三個階段：

1. **行駛前**：根據近期駕駛行為產生每日改善目標
2. **行駛中**：利用過去約 5 分鐘車聯網資訊，預測未來 60 秒安全風險
3. **行駛後**：分析整趟行程、事件前後行為，產生改善建議

> Status: Hackathon Prototype / Offline Research  
> 目前尚未進入 Production deployment。

---

## Current Status

### Completed

- HINO telemetry ETL 與資料品質檢查
- Journey / Event normalization
- Trip Analytics
- Vehicle-linked Behavior Baseline
- Daily Improvement Goal
- Post-trip Event Context Analysis
- GenAI report architecture
- 60-second Risk Label Design
- Leakage-safe ML Dataset
- Logistic Regression baseline
- XGBoost risk model
- SHAP explainability
- Alert Policy offline simulation
- Local HTML demo

### Risk Model

目前核心任務：

```text
Past ~5 minutes telemetry
        ↓
Risk Prediction Model
        ↓
Risk within next 60 seconds
```

目前已完成：

- Logistic Regression baseline
- XGBoost nonlinear model

目前 XGBoost 為 HINO-only offline model。

模型尚未整合：

- TDX 即時／歷史路況
- CWA 天氣
- NCDR 災害警示
- Google Routes
- Live iTRAQ stream

目前 Alert Policy 仍屬 experimental，尚未定義 production threshold。

---

## Architecture

```text
HINO Telemetry
      │
      ├── ETL / Normalization
      │
      ├── Trip Analytics
      │
      ├── Behavior Goal Engine
      │
      └── ML Feature Builder
                │
                ↓
        Risk Prediction Model
        Logistic / XGBoost
                │
                ↓
          AlertPolicyEngine
                │
                ↓
          Future Voice Layer

External Context (future)
TDX / CWA / NCDR
        │
        ↓
ContextAggregator
        │
        └────────→ Risk Engine

Post-trip
Analytics
   ↓
LLM Provider
   ↓
AI Trip Report
```

---

## Repository Structure

```text
hino-safepilot/
├── apps/
│   └── mobile/              # Future Expo application
├── config/                  # Version-controlled configuration
├── demo/                    # Local HTML prototype
├── docs/                    # Architecture / ML / design documents
├── models/                  # Model cards; private artifacts are ignored
├── scripts/                 # Development utilities
├── src/
│   └── hino_safepilot/
│       ├── ai/
│       ├── alerting/
│       ├── analytics/
│       ├── context/
│       ├── dataset/
│       ├── etl/
│       ├── label_research/
│       └── modeling/
├── tests/                   # Synthetic tests
├── .env.example
├── .gitignore
├── pyproject.toml
└── README.md
```

---

## Private Competition Data

**HINO 主辦方提供的資料不包含在此 Git repository。**

本 repository 不應包含：

- HINO 原始 telemetry Excel
- 主辦方 private reference files
- 真實車牌或車輛識別資訊
- 原始或精確 GPS 座標
- Derived private telemetry / Parquet
- Private model evaluation samples
- API Keys / `.env`

各隊員須透過主辦方或團隊核准的資料管道取得資料。

本地預期資料結構：

```text
data/
├── raw/
├── reference/
├── interim/
└── processed/
```

這些資料夾的私有內容皆由 `.gitignore` 排除。

---

## Environment

Recommended:

```text
Python 3.12
```

建立 virtual environment：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

安裝專案：

```powershell
pip install -e .
```

若 `pyproject.toml` 已定義 `dev` optional dependencies：

```powershell
pip install -e ".[dev]"
```

---

## Run Tests

```powershell
pytest
```

目前測試涵蓋：

- ETL
- Data quality
- Label generation
- Leakage prevention
- ML preprocessing
- Logistic / XGBoost contracts
- Alert policy
- Privacy
- Synthetic demo behavior

---

## Local Demo

目前提供簡單的 Local HTML Prototype：

```text
行前目標
行駛中 60 秒風險
行後報告
```

啟動：

```powershell
python -m http.server 8000 --bind 127.0.0.1 --directory demo
```

瀏覽：

```text
http://127.0.0.1:8000
```

### Demo Data

GitHub 版本的 HTML 使用：

**Synthetic Data**

原因：

1. 避免 HINO private data 外流
2. UI 與 backend 解耦
3. 避免將歷史 sample 誤認為即時 prediction

底層 analytics 與 ML model 已使用本地 HINO data 完成 offline validation。

---

## Branch Workflow

`main` 作為目前穩定整合版本。

開發新功能時請建立 branch：

```bash
git switch main
git pull origin main
git switch -c feature/<feature-name>
```

例如：

```bash
git switch -c feature/expo-ui
git switch -c feature/traffic-context
git switch -c research/alert-policy
```

完成後：

```bash
git add .
git commit -m "feat: implement <feature>"
git push -u origin feature/<feature-name>
```

再於 GitHub 建立 Pull Request 合併回：

```text
main
```

請避免直接在 `main` 開發。

---

## Suggested Team Branches

```text
feature/expo-ui
feature/fastapi-backend
feature/traffic-context
feature/weather-context
feature/genai-report
research/alert-policy
research/context-ablation
docs/presentation
```

---

## Environment Variables

請從：

```text
.env.example
```

建立：

```text
.env
```

`.env` 不得加入 Git。

未來可能包含：

```text
GEMINI_API_KEY=
GEMINI_MODEL=
TDX_CLIENT_ID=
TDX_CLIENT_SECRET=
CWA_API_KEY=
GOOGLE_MAPS_API_KEY=
```

---

## Current Limitations

目前仍未完成：

- Production Alert Policy
- Fresh unbiased holdout evaluation
- Historical / live TDX integration
- Historical / live CWA integration
- NCDR integration
- Real-time iTRAQ ingestion
- FastAPI production backend
- Expo mobile application
- Production voice alert
- Driver identity mapping
- HINO/domain expert validation

XGBoost 與 Alert Policy 目前皆屬：

**Offline experimental prototype**

---

## Development Principle

Safety-critical prediction 與 Alert Policy 必須保持 deterministic。

GenAI 主要負責：

- 行前改善目標文字
- 行後分析說明
- AI 教練問答

LLM 不負責直接決定：

- Risk probability
- Safety alert truth
- Vehicle control

---

## Team

2026 和泰 AI 黑客松 HINO 組