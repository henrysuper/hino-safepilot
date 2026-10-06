# 本機 Dashboard

從專案根目錄，使用已安裝 fuel 選用依賴的 Python 環境啟動：

```powershell
hino-fuel serve --repo-root . --port 8765
```

開啟 http://127.0.0.1:8765/dashboard.html 。伺服器只綁定本機。

Dashboard 讀取同資料夾的 `dashboard_data.json`。本次合併經使用者明確授權，將該資料一併納入版本控制。

只查看 Dashboard 時，不需要啟動完整 fuel console，可在專案根目錄執行：

```powershell
.\.venv\Scripts\python.exe -m http.server 8001 --bind 127.0.0.1 --directory apps/fuel_console
```

然後開啟 http://127.0.0.1:8001/dashboard.html 。

完整 console 啟動需要本機 fuel 模型與相關衍生產物，請參考 `docs/fuel_console_guide.md`。若資料檔不存在，Dashboard 無法載入資料。遠端文件提到的 `scripts/export_dashboard.py` 未隨分支提供，其重新產生流程為 UNRESOLVED。
