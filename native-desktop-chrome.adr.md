# 桌面外框共用，內容工具各自執行

Date: 2026-11-19
Source: user

為了縮短操作等待時間，桌面須能在沒有 Python 的環境中啟動與操作，外觀與操作方式以既有 Python 版為準。原生桌面啟動失敗時，不退回 Python 桌面或 Direct 模式。

為了降低資源佔用，整個 tdnext 的執行都不需 Python。Settings 與 CLI 共用同一個 Rust 插件管理器，內容工具仍各自執行。

桌面外框、Files、Settings 共用同一套配色，以舊版截圖的 Python 基本 ANSI 外觀為準，ANSI 與 256 色都盡量接近。
