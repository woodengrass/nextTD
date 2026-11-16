# 插件以版本化介面配對，外部安裝須明確同意

Status: active
Date: 2026-11-15
Source: user

## Context
不同 host 架構需要與 view 宣告相容性，同時保留既有原生工作階段；外部工具不得未經確認下載。

## Decision
磁碟 manifest 明確宣告版本與 host/view 介面，啟用時選用既有相容 host 或唯一相容候選；舊 session 快照仍可解碼。插件提供自己的依賴檢查與安裝方式，執行安裝前取得明確同意，啟動不執行自動安裝。

## Rejected
- view 固定依賴 Desktop ID：無法支援其他相容 host。
- 啟動時自動安裝或啟用：違反明確操作與使用者同意的要求。
