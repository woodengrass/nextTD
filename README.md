# Terminal Desktop Next

Terminal Desktop Next is a plugin-oriented terminal desktop built with Python
and Textual. A desktop host provides the interface, while plugins provide views
and other services. Hosts and views declare compatible interfaces so different
hosts can support plugins through a compatible interface or adapter.

The bundled plugins include a file manager, an interactive shell, settings,
and a system monitor that runs btop. Plugins are trusted local code and run
with your user permissions; they are not sandboxed.

## Requirements

- Linux x86-64 (the first officially supported platform)
- Python 3.12+, [uv](https://docs.astral.sh/uv/), and a UTF-8 terminal
- `curl` if using the local binary installers

The project does not require root access or modify existing user settings or
terminal sessions. External tools can be installed separately; plugin-managed
downloads require your confirmation.

## Getting started

From the project root:

```sh
uv sync --locked
uv run tdnext enable desktop
uv run tdnext enable files
uv run tdnext enable settings
uv run tdnext enable shell
uv run tdnext enable monitor
uv run tdnext run .
```

Only a desktop host is required; enable the other plugins you want to use.
Enabling Monitor prompts before downloading btop if it is missing. Pass another
directory to `tdnext run` to start there.

Application state and locally installed binaries are stored in `state/`. Set
`TDNEXT_STATE_DIR` to use a different writable directory. The desktop uses a
new Zellij session when a compatible Zellij is available and the terminal is
large enough; otherwise it runs directly in the terminal. In direct mode, btop
temporarily takes over the terminal until you quit it.

## Using the desktop

- Open Settings with `Alt+o`; use Back or `Esc` to return.
- In a Zellij session, use `Alt+1` through `Alt+9` to select sidebar views and
  `Ctrl+q` to close the desktop session.
- In direct mode, use `F6` to focus the sidebar and `Ctrl+q` to quit.
- In Files, use the arrow keys and Enter to navigate; press `?` for file
  operations. Deletion is permanent: there is no trash or undo.

Settings lets you manage plugins, choose a file editor, and configure icons.
Plugin changes require a desktop restart. Codicons require a compatible Nerd
Font installed and selected in your terminal; text icons work without one.

## Plugin management

Use the Plugins section in Settings or the command line:

```sh
uv run tdnext list
uv run tdnext install monitor
uv run tdnext enable monitor
uv run tdnext disable monitor
uv run tdnext uninstall monitor
```

Enabling a plugin also enables its dependencies. Disabling or uninstalling a
dependency disables its enabled dependants. Avoid running plugin management
operations concurrently: state writes are not coordinated across processes.

## Development

```sh
uv run pytest -q
uv run ruff check .
uv run basedpyright
```

Native integration tests require Zellij 0.45.1, btop, and tmux; otherwise they
are skipped. The bundled icon provider uses Microsoft Codicons, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), via the
[Nerd Fonts mappings](https://github.com/ryanoasis/nerd-fonts/tree/v3.5.1).
