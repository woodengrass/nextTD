# Zellij 版面檔案需要存在的 layout 目錄

Date: 2026-11-18
Source: agent

## Need
以檔案啟動專用 Zellij 版面，避免大型版面超過 Linux 單一參數長度限制。

## Tried
- Zellij 0.45.1 在隔離的 HOME 與不存在的 layout 目錄下，以絕對路徑傳入版面檔案，實際啟動預設分頁。

## Decision
使用啟動時的暫存設定目錄及其中的 `layouts` 目錄，不修改使用者的 Zellij 設定。

## Revisit when
升級或修改 Zellij，且不再需要既有 layout 目錄。
