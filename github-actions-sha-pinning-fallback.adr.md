# Workflow actions 以完整 SHA 釘住,暫不使用 lockfile

Status: active
Date: 2026-11-22
Source: agent

## Context
`gh actions-lock` 需全域安裝與 lockfile 提交,皆待核准後由 parent 處理。

## Decision
核准前所有 `uses:` 以完整 40 字元 SHA 加 `# vX.Y.Z` 註解釘住;lockfile 落地後改用 lockfile,本紀錄即失效。
