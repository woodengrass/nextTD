# 桌面外框共用，內容工具各自執行

Date: 2026-11-19
Source: user

為了縮短操作等待時間，桌面須能在沒有 Python 的環境中啟動與操作，外觀與操作方式以既有 Python 版為準。原生桌面啟動失敗時，不退回 Python 桌面或 Direct 模式。

這項限制只適用於桌面外框。內容工具獨立執行，可以使用 Python。Files 已另行決定只保留 Rust 版；Settings 仍可使用 Python。
