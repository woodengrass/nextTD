"""Command-line entry point for plugin management and the desktop."""

from pathlib import Path
from typing import Annotated

import typer

from .core.errors import PluginError
from .core.executables import install_executable
from .core.local_env import bootstrap as _bootstrap_local_path
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


@cli.command()
def install(plugin: str) -> None:
    """Install a plugin and its required plugins/executables, without enabling it."""
    typer.echo(f"Installed: {', '.join(manager().install(plugin))}")


@cli.command()
def enable(plugin: str) -> None:
    """Install and enable a plugin and all required dependencies."""
    typer.echo(f"Enabled: {', '.join(manager().enable(plugin))}")


@cli.command()
def disable(plugin: str) -> None:
    """Disable a plugin and every enabled dependent."""
    affected = manager().disable(plugin)
    typer.echo(f"Disabled: {', '.join(sorted(affected)) or '(already disabled)'}")


@cli.command()
def uninstall(plugin: str) -> None:
    """Uninstall a plugin and disable every enabled dependent, keeping dependants installed."""
    affected = manager().uninstall(plugin)
    typer.echo(f"Uninstalled: {plugin}; Disabled: {', '.join(sorted(affected)) or '(already disabled)'}")


@cli.command("install-zellij")
def install_zellij() -> None:
    """Install the pinned optional Zellij binary under the local state directory."""
    typer.echo(f"Installed Zellij: {install_executable('zellij', state_dir() / 'bin')}")


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
