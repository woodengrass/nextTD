# Terminal Desktop Next

Terminal Desktop Next is a terminal desktop entered through a native Rust
launcher running on a patched Zellij core. The launcher draws the desktop
chrome; content tools run as independent programs beside it. The Python
packages in this repository provide content tools (Files, Settings) and the
`tdnext` management CLI. They do not start the desktop.

A standalone desktop distribution is the launcher plus its matching patched
core shipped together. The launcher probes the core at startup and refuses
anything but the matching patched build; stock Zellij is not used.

## Requirements

- Linux x86-64 (the first officially supported platform)
- Rust 1.95+ to build the launcher
- A C compiler, `pkg-config`, and OpenSSL development headers, plus a local
  Zellij git checkout at the pinned commit, to build the patched core
- Python 3.12+ and [uv](https://docs.astral.sh/uv/) only for the Python
  content tools; building or running the desktop needs neither Python nor uv
- `curl` if using the local binary installers

The project does not require root access or modify existing user settings or
terminal sessions. External tools can be installed separately; plugin-managed
downloads require your confirmation.

## Building the desktop

Build the launcher:

```sh
cargo build --locked --release --manifest-path native-desktop/Cargo.toml
```

This produces `native-desktop/target/release/tdnext-desktop`.

Build the matching patched core from a local Zellij checkout. The script
verifies the checkout HEAD is the pinned commit, applies the patches in
`patches/`, and installs without overwriting:

```sh
scripts/build-zellij-core-navigation.sh <path-to-zellij-checkout> <dist>/tdnext-core
```

Ship the two binaries together: copy `tdnext-desktop` next to `tdnext-core`
(or pass another core with `--core`). A distribution without its matching
patched core is incomplete.

## Running the desktop

```sh
<dist>/tdnext-desktop [DIRECTORY]
```

Useful forms:

```sh
<dist>/tdnext-desktop --core /path/to/patched-core .
<dist>/tdnext-desktop --config /path/to/desktop.json .
```

Without `--config`, the launcher reads `desktop.json` beside its executable.
If that file is absent, it opens `$SHELL`, or `/bin/sh` if unset. An explicit
config containing `{"views":[]}`, or only disabled views, opens an empty
desktop instead. Press `Ctrl+q` to exit.

Views are configured as independent programs, not shell strings; see
`native-desktop/README.md` for the `desktop.json` format. Programs inherit
the working directory and their arguments exactly; no shell interprets
configured commands. `TDNEXT_ZELLIJ` points at the running core and
`TDNEXT_DIRECTORY` carries the initial directory. The core supports one
attached client per desktop session, and the desktop cannot be started
inside an existing Zellij session.

## Python content tools

The Python tools install and run independently of the Rust desktop: no Rust
toolchain, Zellij checkout, or core build is needed to manage them.

From the project root:

```sh
uv sync --locked
uv run tdnext list
uv run tdnext install files
uv run tdnext enable files
uv run tdnext disable files
uv run tdnext uninstall files
```

Enabling a tool also enables its dependencies. Disabling or uninstalling a
dependency disables its enabled dependants. Avoid running tool management
operations concurrently: state writes are not coordinated across processes.

Application state and locally installed binaries are stored in `state/`. Set
`TDNEXT_STATE_DIR` to use a different writable directory.

## Using the desktop

- Open Settings with `Alt+o`; use Back or `Esc` to return.
- Use `Alt+1` through `Alt+9` to select sidebar views, `F7` to toggle the
  sidebar, and `Ctrl+q` to close the desktop session.
- In Files, use the arrow keys and Enter to navigate; press `?` for file
  operations. Deletion is permanent: there is no trash or undo.

Settings lets you manage tools, choose a file editor, and configure icons.
Tool changes require a desktop restart. Codicons require a compatible Nerd
Font installed and selected in your terminal; text icons work without one.

## Development

```sh
cargo build --locked --release --manifest-path native-desktop/Cargo.toml
uv run pytest -q
uv run ruff check .
uv run basedpyright
```

The bundled icon provider uses Microsoft Codicons, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), via the
[Nerd Fonts mappings](https://github.com/ryanoasis/nerd-fonts/tree/v3.5.1).
