# Zellij 0.45.1 core navigation patch

**Experimental, single-client sessions only.** The desktop selects this core
only when the header-source command is available. Multiple clients attached to
the same session, plugin API and multi-client protocol changes are unsupported.

Apply `zellij-0.45.1-core-navigation.patch` to the unmodified Zellij 0.45.1
source tree at commit `efd8fd5a89a20c07a111d248ad7fce53848d2c18` using
`git apply`. Zellij is MIT licensed; see the accompanying
`LICENSE.zellij-0.45.1` and retain the upstream license in the source tree.

## Commands

KDL keybindings (existing): `OpenTabWithReturn "tab name"` and
`ReturnToOrigin`, e.g. `bind "Alt o" { OpenTabWithReturn "tools"; }`.
Native CLI (new, works with no session for help):

* `zellij action open-tab-with-return <NAME>` maps to
  `Action::OpenTabWithReturn { name }` via `CliAction` and
  `Action::actions_from_cli`.
* `zellij action open-tab-with-return-from-header <NAME>` atomically records
  the most recent previous pane belonging to the current tab and switches.
  It is intended for the desktop header after a mouse click focuses it.
  It uses the header's inherited `ZELLIJ_PANE_ID` as the source, even if focus
  has already moved. If the source or a prior same-tab pane is unavailable,
  it does nothing.
* `zellij action return-to-origin` maps to `Action::ReturnToOrigin`.
* `zellij action restore-focus-from-header` atomically focuses and renders the
  most recent same-tab pane other than the inherited header pane, including a
  floating pane. A missing or closed source or target is a no-op.
  The screen resolves the source against currently active tabs and acts only
  when exactly one client matches; this handles a CLI connection ID that is
  not the attached client, without guessing across sessions.
* All appear in `zellij action --help` and per-command `--help`.

`Action::OpenTabWithReturn/ReturnToOrigin` also round-trip through the
protobuf IPC contract (tags 152/153; restore uses 154) and are routed in `route_action` to the
new `ScreenInstruction::OpenTabWithReturn/ReturnToOrigin` with completion
notification passthrough, like other tab actions.

## Semantics (per client, single-attached-client sessions)

Each client has one remembered origin and destination. An open from another
tab replaces it. Opening the current tab or a missing tab leaves it
unchanged. Return is ignored unless the client is currently on that recorded
destination. There it consumes a valid origin, or discards it if its tab or
pane no longer exists. On disconnect the client's own record is removed. If
that client was the final attached non-watcher, its record (including no
record) replaces a single detached continuation on that Screen. Unknown or
duplicate removals do not replace it. The first non-watcher attached after
there are no navigable clients adopts the continuation once, regardless of
client ID or its initial tab. Watchers do not adopt it. The origin tab, pane,
and destination must still exist; navigating away from the destination does
not consume the adopted record, and return remains guarded by the destination.
An earlier non-final client's record is never transferred to the final client.
Duplicate names use Zellij's existing first-match behavior. Separate sessions
remain isolated: continuations belong to their Screen and are never shared.
Closing either the origin or destination tab invalidates its navigation record
before a tab ID can be reused.
If a CLI `return-to-origin` arrives before any reattached client has sent a
key, the server selects the client only when exactly one regular client is
actually attached; pending CLI connections and watchers are excluded. With
multiple attached regular clients the fallback is not used. This matters for
the Settings Back button, which invokes the CLI from its pane; Escape can
establish the last keyboard-active client before routing.
The header mode uses screen's per-client pane history, including floating panes
hidden by the mouse transition, without changing `focus-last-pane` semantics.
Opening the current or a missing tab never changes the saved origin; the
header variant restores non-header focus when the current tab is the target.
Keyboard input handling treats both actions as blocking tab switches in
`input_handler`, matching `GoToTab`/`GoToTabName`.

In mirrored sessions, tab switches still move all connected clients, while
the saved origin belongs only to the initiating client. Floating-pane
visibility and some pane layout changes are tab-wide, not per-client:
restoring a pane can show or hide floating panes for other clients in the
origin tab. Mirrored navigation also moves all connected clients. The
per-client record does not make those underlying operations isolated.

## Pending-layout replay and renders

Navigation requests made while a tab layout is pending are deferred with
Zellij's existing pending-event queue and replayed with the stock FIFO
drain-and-requeue (`send_to_screen` in original order) at both
`screen_thread_main` replay sites, retaining their completion notifications.
No inline replay: the earlier helper that executed navigation events inline
while re-sending unrelated pending instructions was removed because it
reordered navigation ahead of already-queued events. Ordering relative to
other kinds of tab actions is still governed by Zellij's pre-existing
machinery (the separate `pending_tab_switches` set plus channel replay), so
exact interleaving with concurrent tab operations is not guaranteed beyond
what stock Zellij provides.

`switch_active_tab` renders itself, so the `OpenTabWithReturn` dispatch path
adds no extra render. `ReturnToOrigin` renders once after restoring the
origin pane (the switch's tab-change render is inherent to the shared
helper, as with stock `GoToTab` paths).

These actions are not serialized to the plugin action API, so plugin-reported
keybinding metadata and action events do not include them. The header command
assumes one attached client; it does not solve general mouse client identity.

## Verification

Reconnect regression (local v3, 2026-11-13):

* Against the existing v2 binary, `tests/test_native_core_reconnect.py` had
  one passing no-detach click control and three failing reconnect cases: actual
  Back click, Escape, and direct CLI return. Against the new locked release
  artifact all four passed, checking the original tab **and** pane. The
  isolated tmux surface was 120×36; the captured Back label occupied columns
  112–115 on row 4 and the click was at SGR coordinates `(113, 4)`.
* The runs use a private state directory, tmux socket, Zellij socket, and
  tagged processes. Logs and terminal captures for this local run are under
  `/tmp/opencode/zellij-v3-private-20261113-6a4c/v2-red*` and `v3-green*`.
* `cargo test --locked --no-default-features -p zellij-server --lib navigation_`
  passed 32 tests. `cargo test --locked --no-default-features -p zellij-utils
  --lib` passed 456 tests, with two ignored.
* The local release artifact is
  `/tmp/opencode/zellij-v3-private-20261113-6a4c/target-v3/release/zellij`
  (SHA-256 `77ab747df5235be5b5274be9a3ca9dac55b68903c0e960dffe1a93264a329065`).
  It was built with `cargo build --locked --release --no-default-features
  --bin zellij` using a private target directory; no existing binary was
  overwritten. The old build script still intentionally refuses to overwrite
  its v2 destination; it is not an installer for this v3 artifact.

To reproduce with the repository's isolated native harness:

```sh
TDNEXT_CORE_BINARY=/path/to/new/zellij .venv/bin/pytest -q tests/test_native_core_reconnect.py
```

To build a separate artifact from a local checkout containing the pinned
commit, from the project root with Rust 1.95 and native prerequisites on PATH:

```sh
ZELLIJ_SRC=/path/to/zellij-checkout
WORK=$(mktemp -d /tmp/opencode/zellij-core-v3-XXXXXXXX)
git -C "$ZELLIJ_SRC" archive efd8fd5a89a20c07a111d248ad7fce53848d2c18 | tar -x -C "$WORK"
git -C "$WORK" apply "$PWD/patches/zellij-0.45.1-core-navigation.patch"
CARGO_TARGET_DIR="$WORK/target-v3" cargo build --manifest-path "$WORK/Cargo.toml" --locked --release --no-default-features --bin zellij
sha256sum "$WORK/target-v3/release/zellij"
```

This creates a new private source and target directory and does not install
or replace any executable. Point `TDNEXT_CORE_BINARY` at that artifact to run
the isolated test; the desktop's existing binary selection is not changed.

Earlier verification of the original core patch:

* `cargo test -p zellij-utils --lib`: 456 passed, 0 failed (includes new
  CLI parse tests, CLI-to-Action mapping tests, keybind tests, IPC
  roundtrips).
* `cargo test --offline --locked -p zellij-server --lib navigation_ --no-default-features`: 20 passed, 0 failed
  (origin/destination checks, pane restoration, per-client isolation,
  disconnect cleanup, mirrored behavior, pending-layout deferral e2e).
* `cargo build --bin zellij` (debug) to
  `/tmp/opencode/zellij-core-nav-target/debug/zellij`; `zellij action
  --help` lists the navigation and restore commands with no session running.
* Note: the debug binary was built with `--no-default-features --features
  vendored_curl,web_server_capability` (i.e. defaults minus
  `plugins_from_target`), embedding the stock prebuilt plugins from
  `zellij-utils/assets/plugins`, because the pinned offline toolchain has no
  wasm32 target to rebuild plugins from source. CLI/IPC/screen behavior is
  unaffected.
* With `TDNEXT_CORE_BINARY=/tmp/opencode/zellij-core-nav-target/debug/zellij`,
  the isolated floating-header regression passed, and the combined
  `tests/test_native_core_header_focus.py`, `tests/test_native_core_navigation.py`,
  and `tests/test_native_core_locked.py` run passed all 12 cases.
