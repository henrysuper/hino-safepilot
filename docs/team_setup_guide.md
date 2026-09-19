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
