import os
from pathlib import Path

import typer

from tdnext.api import PluginContext
from tdnext.core.errors import PluginError
from tdnext.core.local_env import bootstrap as _bootstrap_local_path
from tdnext.core.session import SESSION_ENV, current_session
from tdnext.core.views import enabled_views
from tdnext.runtime import manager

from .contract import textual_views
from .tool import ToolPane

cli = typer.Typer(pretty_exceptions_enable=False)


@cli.callback()
def pane_commands() -> None:
    pass


@cli.command()
def tool(active: str, snapshot: str | None = None) -> None:
    plugins = manager()
    session = current_session(snapshot)
    views = textual_views([
        (manifest, plugin) for manifest, plugin in enabled_views(plugins, session)
        if manifest.id == active and manifest.placement == "header"
    ])
    os.environ.pop(SESSION_ENV, None)
    for manifest, plugin in views:
        if manifest.id == active and manifest.placement == "header":
            context = PluginContext(Path(os.environ["TDNEXT_DIRECTORY"]), plugins, session)
            ToolPane(plugin, context).run()
            return
    raise PluginError(f"Header view not enabled: {active}")


def main() -> None:
    _bootstrap_local_path()
    try:
        cli(standalone_mode=False)
    except PluginError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
