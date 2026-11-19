"""Command-line entry point for plugin management and the desktop."""

from pathlib import Path
from typing import Annotated

import typer

from .core.errors import InstallationConsentRequired, PluginError
from .core.local_env import bootstrap as _bootstrap_local_path
from .core.uninstall_helper import launch_uninstall_helper
from .runtime import manager, state_dir

cli = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


@cli.command("list")
def list_plugins() -> None:
    """Show discovered plugin manifests and their persisted state."""
    plugins = manager()
    state = plugins.store.read()
    for plugin in plugins.manifests.values():
        status = "enabled" if plugin.id in state.enabled else (
            "installed" if plugin.id in state.installed else "available"
        )
        typer.echo(f"{plugin.id:12} {status:10} {plugin.description}")
    for plugin_id in sorted(state.installed - plugins.manifests.keys()):
        status = "enabled" if plugin_id in state.enabled else "installed"
        typer.echo(f"{plugin_id:12} {status:10} Missing plugin manifest")
    for error in plugins.errors.values():
        typer.echo(f"Invalid plugin: {error}")


@cli.command()
def install(plugin: str) -> None:
    """Install a plugin and its required plugins/executables, without enabling it."""
    plugins = manager()
    try:
        result = plugins.install(plugin)
    except InstallationConsentRequired as exc:
        if not typer.confirm(f"{exc}. Proceed?", default=False):
            raise PluginError("Installation declined; plugin state was not changed") from exc
        result = plugins.install(plugin, approved=exc.actions)
    typer.echo(f"Installed: {', '.join(result)}")


@cli.command()
def enable(plugin: str) -> None:
    """Install and enable a plugin and all required dependencies."""
    plugins = manager()
    try:
        result = plugins.enable(plugin)
    except InstallationConsentRequired as exc:
        if not typer.confirm(f"{exc}. Proceed?", default=False):
            raise PluginError("Installation declined; plugin state was not changed") from exc
        result = plugins.enable(plugin, approved=exc.actions)
    typer.echo(f"Enabled: {', '.join(result)}")


@cli.command()
def disable(plugin: str) -> None:
    """Disable a plugin and every enabled dependent."""
    affected = manager().disable(plugin)
    typer.echo(f"Disabled: {', '.join(sorted(affected)) or '(already disabled)'}")


@cli.command()
def uninstall(plugin: str) -> None:
    """Uninstall a plugin and disable every enabled dependant, keeping dependants installed."""
    if not typer.confirm(
        "Uninstalling requires ending all TDNext sessions and their processes; "
        "unsaved work will be lost. Continue?", default=False,
    ):
        raise PluginError("Uninstall declined; plugin state was not changed")
    typer.echo(launch_uninstall_helper(state_dir(), plugin, keep_parent=True))


@cli.command()
def run(directory: Annotated[Path, typer.Argument()] = Path(".")) -> None:
    """Open the desktop at DIRECTORY."""
    from .launcher import launch

    launch(directory.resolve())


def main() -> None:
    _bootstrap_local_path()
    try:
        cli(standalone_mode=False)
    except PluginError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
