# Terminal Desktop Next

An isolated, non-web terminal desktop built with Python and Textual. Separate
local plugins supply the desktop shell, Settings, a Finder-inspired file manager,
an interactive Shell view, and the **real btop** monitor. This candidate is independent of the earlier
prototype and does not change the VM, system packages, shell startup files,
global Zellij configuration, or existing terminal sessions.

## Run the prepared candidate

The current checkout already has a local `.venv`, Zellij, btop, and enabled
plugins. From a UTF-8 terminal:

```sh
cd /home/jason-yang/work/terminal-desktop-next
.venv/bin/tdnext run .
```

Use a directory argument to start elsewhere. At 104 columns by 28 rows or
larger, with Zellij available and when not already inside Zellij, the launcher
creates a **new** Zellij session. Each enabled view gets its own native tab:
a global header pane, a persistent sidebar pane, a central plugin pane, and a
contextual footer pane.
The monitor's central pane runs btop itself, not a drawing or a substitute.
The Shell view directly executes the configured interactive shell in its own
central pane. Settings has its own native tab; returning to Monitor or Shell
does not replace or restart the program running there.

At smaller sizes, inside an existing Zellij session, without Zellij, or if its
launch fails, the Textual desktop runs directly. In that mode select Monitor
and activate **Open btop**; btop takes the full terminal and suspends the
desktop (including its header) until `q` returns to the desktop. Zellij launch
errors are printed before the fallback. Shell is different: its real PTY and
Ghostty-backed terminal stay embedded in the central workspace, with the header,
sidebar and footer still visible. Resize a
terminal if btop reports that its pane is too small.

## Setup from a fresh checkout

Requirements: Python 3.12+, `uv`, a UTF-8 terminal, and `curl` if the bundled
binary installers are used. No root access is needed. With uv already on PATH:

```sh
uv sync --locked
uv run tdnext enable files
uv run tdnext enable settings      # needed for an existing plugin state
uv run tdnext enable shell         # explicit opt-in; works without Zellij
uv run tdnext enable monitor       # installs a missing btop locally
uv run tdnext install-zellij       # optional, enables native-pane layout
uv run tdnext run .
```

If uv is not installed, an optional **project-local** bootstrap is:

```sh
curl -LsSf https://astral.sh/uv/0.9.5/install.sh | \
  env UV_INSTALL_DIR="$PWD/.tools/bin" UV_NO_MODIFY_PATH=1 sh
.tools/bin/uv sync --locked
.venv/bin/tdnext enable files
.venv/bin/tdnext enable settings
.venv/bin/tdnext enable shell
.venv/bin/tdnext enable monitor
.venv/bin/tdnext install-zellij
.venv/bin/tdnext run .
```

The `uv.lock` file records the PyPI dependency versions and hashes. Textual is
pinned to 8.2.8 and the fallback terminal widget to `ghostty-textual==0.1.0`
(which pins `pyghostty==0.1.4`, containing the native Ghostty VT library).
The environment lives in `.venv/`; app state, binaries,
Zellij data, and btop configuration live in `state/`. To isolate multiple
experiments set `TDNEXT_STATE_DIR` to a different writable directory. A first
run with no state enables the bundled Files, Settings and Desktop plugins; Shell
and Monitor are not enabled automatically. Existing state is never migrated to
enable either Shell or Settings. Run `tdnext enable shell` or
`tdnext enable settings` explicitly when needed. Disabling either stays effective
on later launches.

## Keyboard and file operations

The desktop's top header contains Settings when the plugin is enabled: click it
or press `Alt+o` to open it. In Settings, **Back** or `Esc` returns to the
previous workspace with its view state and contextual footer intact. In Zellij,
the originating tab and focused pane are recorded by stable ID, not tab position.
When the project-local custom Zellij is available, the server records the
origin directly, without a navigator plugin or permission prompt. Otherwise,
the existing legacy or optional broker route remains available. The shortcut
opens the first header action if other header-placed plugins are installed.

Settings opens on **General**, which groups **Icon appearance** and
**File editor**. **Provider** and **Style** control icon appearance.
**Editor:** controls which terminal editor Files uses, with choices
**Environment**, **Neovim (nvim)**, and **Vi (vi)**. **Environment** is the
default and preserves the existing behavior: it splits `$EDITOR` and falls
back to `vi` when `EDITOR` is unset. An empty `EDITOR` is reported instead
of launching. **Neovim (nvim)** and **Vi (vi)** use only that executable,
resolved through `state/bin` then `PATH`. When the named executable is
missing, opening a file reports an error and does not fall back to another
editor. The choice is saved in `state/editor.json` and applies on the next
file open without restarting the desktop. The selected editor runs with its
own existing user configuration; the desktop does not install Neovim plugins,
file-tree extensions, or edit dotfiles.
Use Tab or Shift+Tab to reach the **General** and **Plugins**
section choices, then Enter to open a section; either choice also works with
the mouse. Activating a section focuses its first control. **Plugins** contains
the plugin list and management actions; its `i`, `e`, `d`, and `u` shortcuts
only operate while that section is active. Back or Esc leaves Settings rather
than changing section, and changes remain saved. Long status or error messages
can be reached with Tab and scrolled with the arrow keys or mouse wheel.

In Zellij, `Alt+1` through `Alt+9` select the first nine **sidebar views**;
numbers without a corresponding view are not desktop shortcuts and never select
a header tab. The sidebar can also be focused and activated with the keyboard
or mouse. `Ctrl+q` closes only the new desktop session and `Ctrl+p` opens
Zellij's pane controls. Zellij handles these keys and `Alt+o` before the focused
plugin, except in its locked mode: `Ctrl+g` toggles locked mode, in which these
desktop bindings pass through to the focused application instead. Zellij cannot
see an application's internal dialogs, so its global navigation also works
while a pane has a dialog open; returning to that pane preserves the dialog.

### Native Settings shortcut (stock Zellij fallback)

New native sessions using stock Zellij 0.45.1 include one optional bundled broker pane
(`navigator-broker.wasm`) with the primary header tab. On Zellij 0.45.1,
verified flow: from a cold session, `Alt+o` opens Settings with the broker
permission prompt visible in its Settings-tab pane (`Allow? (y/n)` covering
reading and changing application state, command pipes, and runtime config).
Click the prompt pane to focus it, then press `y` to grant or `n` to deny.
The pane closes after your choice. The desktop never writes synthetic grants
or overwrites existing permission state.

The broker is warmed only when the permission cache already holds a prior
explicit grant for this exact binary path
(`state/zellij-cache/zellij/permissions.kdl` covering `ReadApplicationState`,
`ChangeApplicationState`, `Reconfigure`, and `ReadCliPipes`); a fresh, denied,
partial, or unreadable cache stays on the legacy route. The existing Python
`Alt+o` route remains in use until permission is granted and a new desktop
session is started.

A decision in the current cold session does not activate in that session and
never rebinds mid session. The cold session keeps using the legacy route.
To use the broker route after granting, close the desktop (`Ctrl+q` closes
only the new desktop session) and start a new desktop session with
`tdnext run .`. Existing sessions are not changed or migrated. This path
targets Zellij 0.45.1.

In the direct Textual fallback, `Alt+o` and `Ctrl+q` are reserved desktop keys
and take precedence over plugin bindings. `Alt+o` is ignored while a modal is
open, `Esc` belongs to the topmost modal, and `Ctrl+q` quits immediately even
from a modal. `F6` focuses the sidebar; use arrows and Enter to choose a view.
Other shortcuts belong to the focused plugin or its widgets. Tab normally moves
focus, but when Shell is focused it goes to the shell for completion or to the
interactive program. `Ctrl+t` changes the desktop theme when the focused plugin
does not claim it; inside Shell it is sent to the program instead.

Shell runs `$SHELL -i`, or the account's configured shell (then `/bin/sh`) if
`SHELL` is unset. It starts in the directory passed to `tdnext run`, reads its
normal interactive startup configuration, and has the current user's ordinary
permissions. Arguments are passed directly, without interpolating the path or
shell into a command string. In fallback, F6 returns focus to the sidebar,
`Alt+o` opens Settings, and Shift+Page Up / Shift+Page Down scroll terminal
history. Closing Settings returns to the same shell and focused program. If the
shell exits, the fallback shows its exit status; press Enter to start a new one.
In native mode Zellij may show its exited-pane prompt instead. In fallback on
Linux with pidfds and readable `/proc`, closing the desktop signals observed,
verified members of the current foreground job and the owned shell using stable
process handles. This is bounded, best-effort cleanup: if the shell has already
exited, the foreground changes, or a process forks or detaches during cleanup,
some processes may survive. Cleanup never signals a group by a potentially
reused ID. Background and deliberately detached programs manage their own
lifetime. On systems without pidfds, cleanup relies on the PTY hangup; a shell
that ignores it cannot be force-terminated safely and reports a cleanup error.
A foreground program that ignores hangup may survive.

In Files, arrows select a row, Enter opens a folder or opens a file in the
terminal editor chosen in Settings General under **File editor**
(`Environment` preserves `$EDITOR`, default `vi`; explicit picks take effect
only in Files), and Backspace or `h`
goes up. The running Finder reads the saved choice on each open, so changing
it does not need a restart. In the direct Textual fallback the editor takes
the full terminal through `app.suspend()` and the desktop, including its
header, stays suspended until the editor exits. In a native Zellij layout the
same call suspends only the Files pane app, so other panes keep running. `g` opens a path; `n` creates a folder; `f` creates an empty file;
`r` renames; `e` edits; `y` copies; `x` cuts; `p` pastes; `.` toggles hidden
files; and `Ctrl+r` refreshes. Press `?` for the full contextual help.
Existing names are not intentionally overwritten. The clipboard is local to
the running Files view, not the system clipboard. `d` opens a confirmation
before permanently deleting a file, symlink, or entire directory tree; there
is no trash or undo. Ordinary filesystem permissions apply. Do not use it on
files you cannot afford to lose.

btop is a full interactive system monitor, including its own process-signal
controls. The desktop does not send signals itself. Consult btop's `?` help
before using destructive process actions. In a Zellij pane, quitting btop may
leave Zellij's exited-pane prompt; Enter can rerun the pane.

## Plugins and dependency lifecycle

The Plugins section of Settings lists discovered trusted plugins as Available, Installed or Enabled.
Installed IDs whose manifests have disappeared are listed as missing and can
be disabled or uninstalled in Settings or with the CLI.
Select one and use Install, Enable, Disable or Uninstall (mouse or the displayed
keyboard shortcuts). Installs and dependency checks run in a worker so the UI
remains responsive. Disabling a dependency confirms and reports enabled
dependants; uninstall requires confirmation and retains dependants' installed
state. Errors are shown in Settings, and a restart is required to apply changes
to the running desktop. The running Desktop and Settings plugins, including
dependencies whose removal would disable either, are protected in this UI;
the CLI remains available for intentional changes. Do not run simultaneous
management operations from separate sessions or the CLI: writes are not locked.

```sh
.venv/bin/tdnext list
.venv/bin/tdnext install monitor    # resolve and install, but do not enable
.venv/bin/tdnext enable monitor     # resolve, install if necessary, enable
.venv/bin/tdnext disable desktop    # cascades to enabled dependants
.venv/bin/tdnext uninstall desktop  # remove installed/enabled state, disable enabled dependants
.venv/bin/tdnext enable files       # also re-enables desktop
```

Each bundled plugin has a `plugin.toml` with a stable ID, importable entry
point, dependencies and explicit `capabilities`. A declaration has exactly one
structural role: `desktop.host`, `desktop.view`, or `icons.provider`. Domain
capabilities describe what the plugin actually does: `files.browse`,
`system.monitor`, `shell.interactive`, and `settings.manage`; the host's
`desktop.host` and the service's `icons.provider` are also meaningful
capabilities. Duplicate, malformed, or conflicting roles are rejected.
Capabilities describe functionality, not permissions: a plugin ID identifies a
plugin and is not a security credential.

For compatibility, a manifest with no `capabilities` retains the old behavior:
the ID `desktop` is a host and every other ID is a view. Existing plugin state
is not rewritten to add the new service. Views may use `placement = "sidebar"`
(the default) or `"header"`; Settings is a header view. A view's optional
`icon_key` is a semantic name, not a font glyph. Bundled view keys are `files`,
`monitor`, `shell`, and `settings`. A provider resolves those keys; an unknown
key resolves to safe text. The older optional `icon` remains supported as
plain-text compatibility fallback: at most four printable ASCII or standard
terminal-drawing characters, without control sequences, emoji, or private-use
glyphs. Without either, `[]` is used. Text labels always remain visible.

Trusted local extensions can be declared under
`state/plugins/<directory>/plugin.toml`, with an entry point importable in the
same environment. Views implement `tdnext.api.ViewPlugin`; icon services
implement `tdnext.api.IconProvider.resolve(key, mode) -> str`, where `mode`
is `"compatibility"` or `"codicons"`. Both modes and an unknown-key fallback
must return safe, short terminal text; Codicons mode may also return private-use
glyphs. Services are validated before install or enable, never appear as tabs,
and need not be dependencies of views. Plugin code executes with the current
user's full permissions: this is **not a sandbox** or a remote marketplace.

### Icon appearance

1. In Settings, open **Plugins**, select **Codicons** in the list and **Enable** it (or
   run `.venv/bin/tdnext enable icons`). This optional service has no view or
   host dependants. Enabling it does not automatically select it.
2. Open **General**. In the **Provider** selector, choose **Codicons** (or another enabled
   icon provider). Choose **Compatibility** or **Codicons** in **Style**.
   Compatibility uses ASCII labels from the provider and is the default.
3. Restart the desktop to apply either selection. Preferences are saved
   atomically in `state/appearance.json`, independently of `plugins.json`.
   Selecting **None (text)**, disabling or uninstalling a provider, or an
   unavailable selected provider leaves the views usable via their legacy text
   fallback. A disabled or missing provider selection is retained rather than
   silently rewritten. A missing hard dependency of a view still stops launch.
   If an enabled manifest is missing and its role cannot be established from
   the selected provider or the known bundled service, launch reports the ID
   instead of guessing; use `tdnext disable <id>` or `tdnext uninstall <id>`
   to recover without removing other installed plugins.

**Codicons mode requires a compatible Nerd Font installed and selected in the
terminal application on the user's local computer**, not merely inside the VM
or remote host. The app does not install fonts or try to detect them; if glyphs
appear as boxes or have incorrect spacing, choose Compatibility. Native Zellij
panes and the Textual fallback use a session snapshot, so changing settings does
not alter an already open desktop. Snapshots are passed to internal native pane
commands rather than exported to unrelated new panes; a new public desktop
launch reads the current plugin state. Older sessions started before this fix
retain their original Zellij environment until they are restarted.
Native Settings refreshes saved appearance when its pane receives a terminal
focus report; if a terminal or multiplexer does not send that report, focusing
an appearance selector refreshes it without changing the desktop snapshot.

The bundled provider uses the Codicons mappings in Nerd Fonts **v3.5.1**,
which vendors Microsoft Codicons **0.0.45**: folder `U+EA83`, pulse `U+EB31`,
terminal `U+EA85`, and settings `U+EB52`. Codicons © Microsoft Corporation,
licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); see
[Microsoft's v0.0.45 mapping](https://github.com/microsoft/vscode-codicons/blob/v0.0.45/src/template/mapping.json)
and [Nerd Fonts v3.5.1](https://github.com/ryanoasis/nerd-fonts/tree/v3.5.1).
The codepoints are reused without modifying glyph outlines; no font is bundled.

Install and enable use a topological dependency order; each plugin's declared
executables are checked or installed before its entry point is imported or
constructed. The local `state/bin` directory is not automatically added to
the process `PATH`: a plugin that calls an executable by bare name during
import, construction, or runtime may still fail when it is absent from the
user's PATH. A view can use `context.manager.executable(name)` to obtain the
local executable's absolute path and pass an explicit child environment instead.
Cycles, missing
manifests, import errors, missing executables, failed downloads and corrupt
state are reported; the plugin state is not committed on failure. Enabling
also installs/enables required plugins. Disabling a dependency disables its
enabled transitive dependants, but does not delete their files or binaries.
Uninstall removes only the target from installed (and from enabled when
enabled) and disables its enabled transitive dependants; dependants stay
installed, shared binaries, manifests, and user files are untouched, and
`install`/`enable` reinstalls the target without cleanup. Disable keeps the
target installed; uninstall does not.
State writes use atomic replacement; concurrent management commands are not
coordinated and should not be run at the same time. If a previously available
executable is removed, launch reports the missing dependency.

## External executable provenance

The manager first checks `state/bin` and then PATH. A btop executable must
successfully identify itself via `--version`; it is never replaced with htop.
When missing, `enable monitor` or `install monitor` downloads the following
official Linux x86_64 musl archive using curl, verifies its pinned SHA-256,
extracts only the regular executable, and checks that it runs before saving it:

- btop 1.4.8: <https://github.com/aristocratos/btop/releases/tag/v1.4.8>
  - `btop-x86_64-unknown-linux-musl.tar.gz`
  - archive SHA-256 `af589cf82cc9ea9c29ce050b9239a13bef2d1492256d476156e1054a42ce89ad`
  - this official static build has no GPU support.
- Optional Zellij 0.45.1 no-web:
  <https://github.com/zellij-org/zellij/releases/tag/v0.45.1>
  - `zellij-no-web-x86_64-unknown-linux-musl.tar.gz`
  - archive SHA-256 `d7bda1e18c30a688833ae7627f1d6a253bbba5349a4bc48e4f0ec008aaf75ed1`

Checksums are pinned release-asset digests, not signatures or an independent
trust root. Automatic binary installation is supported only on Linux x86_64;
on other platforms install genuine compatible executables on PATH. No sudo,
setuid bits, system package manager, or global PATH edits are used.

### Custom Zellij core

The patch `patches/zellij-0.45.1-core-navigation.patch` (notes in
`patches/zellij-0.45.1-core-navigation.md`, upstream MIT license in
`patches/LICENSE.zellij-0.45.1`) is never built or installed automatically.
To rebuild it project-locally:

```sh
scripts/build-zellij-core-navigation.sh <path-to-zellij-git-checkout>
```

The argument must be an existing local Zellij git checkout whose HEAD is
exactly `efd8fd5a89a20c07a111d248ad7fce53848d2c18`. The script exports that
committed HEAD with `git archive` to a temporary directory (working-tree
changes are ignored and the checkout is never modified), applies the patch
with `git apply`, builds
`cargo build --locked --release --no-default-features --bin zellij`, checks
`zellij action --help` lists the native open, header-open, focus, and return commands,
and atomically installs
`state/bin/zellij-core-navigation-v2` only if that path is absent (it never
overwrites). It needs Rust 1.95 plus the upstream native prerequisites (a C
compiler, pkg-config, OpenSSL dev headers) on PATH; `PATH` and `CARGO_HOME`
are used as inherited. Cargo may download locked dependencies unless offline
mode is enabled. The script performs no sudo, Git configuration, commit, or
session operations.

New desktop sessions prefer this binary when it supports all four commands.
They use native navigation without loading the broker; running sessions keep
their original executable and route. A missing or incompatible custom binary
leaves the stock route unchanged. The native route assumes one attached client
per Zellij session; independent sessions remain separate.

## Verification and limitations

```sh
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/basedpyright
```

Native integration tests require Zellij 0.45.1, btop, and tmux; they use isolated
state and sockets and otherwise skip. The desktop is a first version: there is
no file trash/undo, no sandbox, and no multi-process state-write serialization.
The terminal determines font, colors, character support, mouse behavior, and
accessibility semantics. The Ghostty wheel supports Linux x86_64/aarch64 with
glibc 2.27+ and macOS 13+ on x86_64/arm64; Windows, 32-bit ARM and musl/Alpine
are not supported by that widget. This integration was verified on Linux x86_64.
The current widget uses mouse input locally for text selection and scrollback;
it does not forward mouse reporting to terminal applications, so use their
keyboard controls. Terminal applications may reserve their own keys and need
more space than a narrow workspace provides.
