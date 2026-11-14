"""Entrypoint for individual native Zellij panes."""

import os
from pathlib import Path
from typing import Annotated

import typer

from .api import PluginContext
from .core.errors import PluginError
from .core.local_env import bootstrap as _bootstrap_local_path
from .core.session import SESSION_ENV, current_session
from .launcher import enabled_views
from .runtime import manager

cli = typer.Typer(pretty_exceptions_enable=False)


@cli.callback()
def pane_commands() -> None:
    """Run an enabled plugin in a native pane."""


@cli.command()
def view(active: Annotated[str, typer.Argument()], snapshot: str | None = None) -> None:
    plugins = manager()
    session = current_session(snapshot)
    context = PluginContext(Path(os.environ["TDNEXT_DIRECTORY"]), plugins, session)
    views = enabled_views(plugins, session)
    # A user shell or external program must not inherit the desktop's session snapshot.
    os.environ.pop(SESSION_ENV, None)
    for manifest, plugin in views:
        if manifest.id == active:
            plugin.run_pane(context)
            return
    raise PluginError(f"View not enabled: {active}")


def main() -> None:
    _bootstrap_local_path()
    try:
        cli(standalone_mode=False)
    except PluginError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
