# Terminal Desktop Next

Terminal Desktop Next is a native terminal desktop. The product is compiled
binaries: no Python is needed to stage or use it. Python remains only for
development of the legacy test suite (see Development).

A distribution directory holds the product siblings together: `tdnext` (tool
manager CLI), `tdnext-desktop` (launcher), `tdnext-core` (matching patched
core), `tdnext-settings`, and `tdnext-files`. The launcher
probes the core at startup and refuses anything but the matching patched
build; stock Zellij is not used.

## Requirements

- Linux x86-64 (the first officially supported platform)
- A terminal; a Nerd Font only if you select Codicons (see Icons below)

The project does not require root access or modify existing user settings or
terminal sessions. External tools are installed separately; tool-managed
downloads require your confirmation before anything is installed.

## Staging a distribution

```sh
make dist DIST=/path/to/dist ZELLIJ_SRC=/path/to/pinned-zellij
```

`DIST` must be a new directory. Building requires Rust 1.95, a C compiler,
`pkg-config`, OpenSSL development headers and a local `ZELLIJ_SRC` checkout
at the pinned commit. Move the resulting directory as a unit; it needs
neither the source tree nor those build tools to run.

## Installing a verified distribution (operator-local)

`scripts/install-verified-dist.sh` activates exactly the tarball that passed
verification, for a local or VM operator target. It is not live deployment:
it only writes under the given destination, keeps every previous release,
never touches user settings or sessions, and performs no network or publish
writes.

Trust boundary (exactly one of the two): the tarball bytes and anything
inside them (including any checksum shipped inside the archive) are
untrusted. Either `--verify` re-runs the shared gate `make verify-dist
ARCHIVE=...` (`scripts/verify-dist.py`) on a private immutable copy of the
same bytes before activation, and the digest the verifier prints must match
those bytes; or `--digest-file` supplies the producer-emitted
`<ARCHIVE>.sha256` obtained separately (for example the CI digest written
after the passed run). The digest file is never auto-read from the package
directory. Without a passing verification or a matching trusted digest the
script exits nonzero and leaves `current` untouched.

Usage (digest mode on the target needs only bash, tar and sha256sum, no
Python; `--verify` needs the verification workstation with this checkout's
Python/venv toolchain because it runs the real verifier):

```sh
bash scripts/install-verified-dist.sh --archive /path/to/tdnext-linux-x86_64.tar.gz \
  --dest /opt/tdnext --digest-file /path/to/tdnext-linux-x86_64.tar.gz.sha256
# or verify the same bytes first, then activate them:
bash scripts/install-verified-dist.sh --archive ... --dest ... --verify
# preview without switching:
bash scripts/install-verified-dist.sh --archive ... --dest ... --digest-file ... --dry-run
```

Layout unpacks to `<dest>/releases/<full-sha256>/` (exactly `tdnext/` with
the five executables plus the license, regular files only) and flips the
`<dest>/current` symlink with a same-filesystem atomic rename; a failed run
removes only its staging directory and never switches the pointer. An
existing release is reused only when every file's contents and modes
byte-match the freshly extracted candidate; a tampered release is refused.
Record-only: each release keeps its `digest.sha256` activation record.
Publish (release assets) and live deployment stay outside this script.

## Managing tools with `tdnext`

From a staged distribution (or place its directory on `PATH`):

```sh
./tdnext list
./tdnext enable files
./tdnext enable settings
./tdnext desktop .
```

Enabling a tool also enables its dependencies. Disabling or uninstalling a
dependency disables its enabled dependants. Use `tdnext install ID` to install
without enabling; `tdnext uninstall ID` removes its installed state.

Tools resolve state through the existing `TDNEXT_STATE_DIR` directory: set it
to reuse your current state, or leave it unset for the default location.
The manager reads the existing `plugins.json`, `appearance.json` and
`editor.json` formats. User-local views in `plugins/<name>/plugin.toml` now
declare a `command`, optional `args`, and `view_interfaces =
["tdnext.terminal-view.v1"]`. Old Python `entrypoint` and `dependency_hook`
fields are reported as unsupported; the manager does not rewrite them.

## Running the desktop

Start the desktop from the distribution:

```sh
tdnext desktop [DIRECTORY]
```

`tdnext desktop` builds its view list from enabled plugins. The desktop
can also start directly from its own config, independent of `tdnext`:

```sh
tdnext-desktop --config /path/to/desktop.json [DIRECTORY]
```

Without `--config`, the launcher reads `desktop.json` beside its executable.
If that file is absent, it opens `$SHELL`, or `/bin/sh` if unset. An explicit
config containing `{"views":[]}`, or only disabled views, opens an empty
desktop instead. Press `Ctrl+q` to exit. The desktop cannot be started inside
an existing Zellij session.

The launcher keeps session sockets in `$ZELLIJ_SOCKET_DIR` when set, otherwise in
`$XDG_RUNTIME_DIR/tdnext/zellij` (or `/tmp/tdnext-<uid>/zellij` without a runtime directory).
List those sessions with `ZELLIJ_SOCKET_DIR=<dir> tdnext-core list-sessions`.

Views are configured as independent programs, not shell strings; see
`native-desktop/README.md` for the `desktop.json` format. Programs inherit
the working directory and their arguments exactly; no shell interprets
configured commands. `TDNEXT_ZELLIJ` points at the running core and
`TDNEXT_DIRECTORY` carries the initial directory. When migrating a
hand-written view, replace the old Python `-m tdnext.pane view ...`
entrypoint with the sibling executable path (for example `tdnext-files`)
plus its executable arguments.

## Views

- Files (`tdnext-files`): browse and operate on files. Deletion is permanent:
  there is no trash or undo.
- Monitor: runs `btop`. Its full-screen keys apply after it takes over.
- Shell: runs your `$SHELL`, or `/bin/sh` when unset.
- Settings (`tdnext-settings`): manage tools, choose a file editor, and
  configure icons. Tool changes require a desktop restart.
- Icons: Codicons require a compatible Nerd Font installed and selected in
  your terminal; text icons work without one.

Keys:

- Open Settings with `Alt+o`; use Back or `Esc` to return.
- Use `Alt+1` through `Alt+9` to select sidebar views, `F7` to toggle the
  sidebar, and `Ctrl+q` to close the desktop session.
- In Files, use the arrow keys and Enter to navigate; press `?` for file
  operations.

## Development (Python, dev only)

Python and `uv` are needed only for the legacy checks, not for `make dist`
or any shipped executable. To run the full development gate:

```sh
make check ZELLIJ_SRC=/path/to/pinned-zellij
```

The bundled icon provider uses Microsoft Codicons, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), via the
[Nerd Fonts mappings](https://github.com/ryanoasis/nerd-fonts/tree/v3.5.1).
