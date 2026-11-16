# 只啟動已啟用的插件，並允許沒有 view 的桌面

Status: active
Date: 2026-11-15
Source: user

## Context
桌面需要 host 才能提供 UI，但具體功能插件不是桌面存在的前提。

## Decision
啟動需要恰好一個已啟用的 Desktop host；沒有 view 時仍可啟動。只啟動已啟用的插件，不在啟動時自動啟用其他插件。

## Rejected
- 首次啟動自動啟用 Files 與 Settings：核心不應強制啟用非 Desktop 插件。
- 沒有 view 時拒絕啟動：空桌面仍應可用。
