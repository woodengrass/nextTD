"""Entrypoint for individual native Zellij panes."""

import os
from pathlib import Path
from typing import Annotated

import typer

from .api import PluginContext
from .core.discovery import instantiate
from .core.errors import PluginError
from .core.graph import dependency_order
from .core.local_env import bootstrap as _bootstrap_local_path
from .core.session import SESSION_ENV, current_session
from .runtime import manager
from .terminal_contract import TerminalViewPlugin

cli = typer.Typer(pretty_exceptions_enable=False)


@cli.callback()
def pane_commands() -> None:
    """Run an enabled plugin in a native pane."""


@cli.command()
def view(active: Annotated[str, typer.Argument()], snapshot: str | None = None) -> None:
    plugins = manager()
    session = current_session(snapshot)
    context = PluginContext(Path(os.environ["TDNEXT_DIRECTORY"]), plugins, session, active)
    if session is None:
        state = plugins.store.read()
        selected = plugins.manifests.get(active) if active in state.enabled else None
        if selected is not None and selected.role != "desktop.view":
            selected = None
    else:
        selected = next((view.manifest for view in session.views if view.manifest.id == active), None)
    # A user shell or external program must not inherit the desktop's session snapshot.
    os.environ.pop(SESSION_ENV, None)
    if selected is None:
        raise PluginError(f"View not enabled: {active}")
    if session is None:
        order = dependency_order(plugins.manifests, active)
        if not set(order) <= plugins.store.read().enabled:
            raise PluginError(f"Enabled plugin {active} has a disabled dependency")
        plugin = plugins.check_dependencies(order)[active]
    else:
        plugin = instantiate(selected)
    if not isinstance(plugin, TerminalViewPlugin) or not callable(plugin.run_pane):
        raise PluginError(f"View {active} does not implement the terminal view plugin contract")
    plugin.run_pane(context)


def main() -> None:
    _bootstrap_local_path()
    try:
        cli(standalone_mode=False)
    except PluginError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
