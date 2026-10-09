# SafePilot 安全分數：一鍵產生本機資料（駕駛端 Demo 與後台「安全分數報告」分頁共用）
#
# 用法（在專案根目錄）：
#   powershell -ExecutionPolicy Bypass -File scripts\build_safety.ps1
#
# 會做的事：檢查必要檔案 -> 清掉 desktop.ini -> 刪除舊的輸出 -> 依序執行
#   build -> score -> (segments) -> road -> alerts -> drivers -> export-demo
# 所有輸出都在 data\processed\（不進 Git）。路況比較與 60 秒預測缺資料時會略過並說明。

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$Fuel = Join-Path $Root ".venv\Scripts\hino-fuel.exe"
$Log = Join-Path $Root "data\processed\build_safety.log"

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Missing($paths) { @($paths | Where-Object { -not (Test-Path (Join-Path $Root $_)) }) }

Say ""
Say "== SafePilot 安全分數：一鍵產生 ==" "Cyan"
Say "專案資料夾：$Root"

# ---------------------------------------------------------------- 1. 環境
if (-not (Test-Path $Py)) {
    Say "找不到 .venv。請先照 docs\team_setup_guide.md 第 3 節建立 Python 3.12 環境：" "Red"
    Say "  py -3.12 -m venv .venv"
    Say "  .venv\Scripts\python.exe -m pip install -e "".[dev,fuel]"""
    exit 1
}

# ---------------------------------------------------------------- 2. 必要與選用檔案
$required = @(
    @{ What = "行車資料（ETL 輸出）"; From = "比賽雲端資料夾的 data interim -> data\interim\";
       Paths = @("data\interim\telemetry_normalized", "data\interim\events_onsets") },
    @{ What = "虛擬駕駛分配"; From = "雲端 1006\data\processed\fuel\ -> data\processed\fuel\";
       Paths = @("data\processed\fuel\driver_assignment.parquet", "data\processed\fuel\vehicle_aliases.json",
                 "data\processed\fuel\artifacts\virtual_drivers.json", "data\processed\fuel\artifacts\mock_plates.json") }
)
$roadFiles = @("data\osm\road_index_v2.npz",
               "data\dem\moi_dem20m\不分幅_全台及澎湖DEM\dem_20m.tif",
               "data\dem\moi_dem20m\不分幅_全台及澎湖DEM\dem_20m.tfw",
               "data\processed\fuel\artifacts\road_behaviour_map_grid.parquet",
               "data\processed\fuel\artifacts\behaviour_prior.json")
$alertFiles = @("data\processed\ml\v0\dataset", "data\processed\ml\v0\metadata\resolved_feature_spec.json",
                "models\risk\v1_xgboost\risk_model_bundle.joblib", "models\risk\v1_xgboost\alert_policy_v1.json")

$blocked = $false
foreach ($item in $required) {
    $gone = Missing $item.Paths
    if ($gone.Count -gt 0) {
        $blocked = $true
        Say "缺少【$($item.What)】，請從 $($item.From) 複製：" "Red"
        $gone | ForEach-Object { Say "  - $_" }
    }
}
if ($blocked) { Say "補齊上面的檔案後再執行一次。" "Red"; exit 1 }

$doRoad = (Missing $roadFiles).Count -eq 0
if ($doRoad) {
    & $Py -c "import pandas, tifffile" 2>$null
    if ($LASTEXITCODE -ne 0) { $doRoad = $false; $roadNote = "缺少套件：執行 .venv\Scripts\python.exe -m pip install -e "".[fuel]""" }
} else {
    $roadNote = "缺少路網或高程檔（雲端 1006\data\osm、1006\data\dem、1006\data\processed\fuel\artifacts）"
}
$doAlerts = (Missing $alertFiles).Count -eq 0
if (-not $doAlerts) { $alertNote = "缺少 60 秒模型資料（雲端 data processed\ml、models risk）" }

# ---------------------------------------------------------------- 3. 清理
Get-ChildItem (Join-Path $Root "data"), (Join-Path $Root "models") -Recurse -Force -Filter desktop.ini -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
$outputs = @("analytics", "safety_scores", "driver_safety", "trip_alerts", "road_context", "demo_views")
foreach ($name in $outputs) {
    $path = Join-Path $Root "data\processed\$name"
    if (Test-Path $path) { Remove-Item -Recurse -Force $path }
}
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null
"SafePilot build_safety $(Get-Date -Format s)" | Out-File $Log -Encoding utf8
Say "已清除舊的輸出：data\processed\{$($outputs -join ', ')}"

# ---------------------------------------------------------------- 4. 執行
function Step($title, $exe, $arguments) {
    Say ""
    Say "▶ $title" "Cyan"
    "`n=== $title ===" | Out-File $Log -Append -Encoding utf8
    $quoted = ($arguments | ForEach-Object { if ($_ -match '\s') { """$_""" } else { $_ } }) -join " "
    cmd /c """$exe"" $quoted >> ""$Log"" 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Say "✗ 失敗：$title（完整紀錄：$Log）" "Red"
        Get-Content $Log -Tail 20 -Encoding utf8 | ForEach-Object { Say "  $_" }
        exit 1
    }
    Say "  完成" "Green"
}

Step "1/7 行程分析（build）" $Py @("-m", "hino_safepilot.analytics", "build")
Step "2/7 安全分數（score）" $Py @("-m", "hino_safepilot.analytics", "score")
if ($doRoad) {
    if (-not (Test-Path (Join-Path $Root "data\processed\fuel\segments_5km.parquet"))) {
        Step "3/7 路段切分（hino-fuel segments）" $Fuel @("segments", "--repo-root", $Root)
    } else {
        Say ""; Say "▶ 3/7 路段切分：已存在，略過" "Cyan"
    }
    Step "4/7 路況比較（road）" $Py @("-m", "hino_safepilot.analytics", "road")
} else {
    Say ""; Say "▶ 3–4/7 路況比較：略過（$roadNote）" "Yellow"
}
if ($doAlerts) {
    Step "5/7 60 秒預測回放（alerts）" $Py @("-m", "hino_safepilot.analytics", "alerts")
} else {
    Say ""; Say "▶ 5/7 60 秒預測：略過（$alertNote）" "Yellow"
}
Step "6/7 對應虛擬駕駛（drivers）" $Py @("-m", "hino_safepilot.analytics", "drivers")
Step "7/7 產生 Demo 資料（export-demo）" $Py @("-m", "hino_safepilot.analytics", "export-demo", "--provider", "mock")

# ---------------------------------------------------------------- 5. 結果
$drivers = (Get-ChildItem (Join-Path $Root "data\processed\demo_views") -Filter "driver-*.local.json").Count
Say ""
Say "== 全部完成：$drivers 位虛擬駕駛的資料已產生 ==" "Green"
if (-not $doRoad) { Say "（行後報告不會有「路況公平比較」）" "Yellow" }
if (-not $doAlerts) { Say "（行駛中頁面會顯示 Synthetic 預覽，行後報告不會有 60 秒預測）" "Yellow" }
Say "駕駛端：python scripts\serve_demo.py  ->  http://127.0.0.1:8000"
Say "後台：  .venv\Scripts\hino-fuel.exe serve --repo-root . --port 8765  ->  「安全分數報告」分頁"
Say "完整紀錄：$Log"
