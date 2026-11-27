# 開始工作

以目前的 Git 根目錄為準。先讀 `GOALS.md` 和改動範圍內適用的 `*.adr.md`；建置與執行方式見 `README.md`。

## 開發流程

從 `main` 開功能分支。開發中先跑與改動相關的檢查，交付前在根目錄執行 `make check`。開 PR 後用 `gh pr checks <PR號碼> --watch --fail-fast` 查看結果，確認 `ci-required` 通過後再合併。

## 專案指令

- `make verify-dist ARCHIVE=<archive.tar.gz>`：只跑發行包驗收（`scripts/verify-dist.py`，有 venv 時用 venv 的 Python）。
- `make check` 需要 `PATH` 含 `~/.cargo/bin` 與 `uv` 0.9.5（`~/.local/bin`）；未指定 `ZELLIJ_SRC` 時會連網 shallow-fetch 釘選的 Zellij。
- `make check` 與 `make dist` 需要 `musl-gcc`（Ubuntu：`sudo apt install musl-tools`）；musl target 由 `rust-toolchain.toml` 讓 rustup 自動安裝，發行執行檔在 `target/x86_64-unknown-linux-musl/release/`。
- `make check ARCHIVE_OUT=<絕對路徑>`：全數通過後才輸出受測壓縮包與 `<路徑>.sha256`（`sha256sum` 格式）。
- `tests/golden/**/*.screen`（含 `tests/golden/ansi16/`）、`tests/golden.lock*`、`scripts/check-golden-lock.py` 一旦進入 base 即凍結；PR 更動一律失敗，無 `Spec-change:` 例外（首次加入需 `Spec-change:`；巢狀 `ansi16` 檔雜湊須釘在 `tests/golden.lock.ansi16.json`，由其專屬 exact-set guard 驗證）。

## 使用 VM

一般開發與 CI 不需要 VM。只有任務明確需要 VM 或 `partner-test` 基準時，才讀根目錄的 `./VM-INFO.md`。這是本機檔案，不提交到 Git；若檔案不存在，請指出缺少本機設定，不要猜測連線方式。保留 VM 既有的設定與工作階段，不得刪除或覆寫。
