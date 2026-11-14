"""Validate trusted icon services and their terminal-safe output at the boundary."""

from rich.cells import cell_len

from tdnext.api import IconMode, IconProvider

from .errors import PluginError


def safe_icon(value: str, mode: IconMode) -> bool:
    if not isinstance(value, str) or not value or cell_len(value) > 4:
        return False
    return all(
        "!" <= char <= "~"
        or "\u2500" <= char <= "\u259f"
        or (mode == "codicons" and "\ue000" <= char <= "\uf8ff")
        for char in value
    )


def check_provider(plugin: IconProvider, plugin_id: str) -> None:
    if not callable(plugin.resolve):
        raise PluginError(f"{plugin_id} does not implement the icon provider contract")
    try:
        for mode in ("compatibility", "codicons"):
            for key in ("files", "tdnext.unknown"):
                if not safe_icon(plugin.resolve(key, mode), mode):
                    raise PluginError(f"{plugin_id} returned an unsafe icon")
    except Exception as exc:
        # A provider is extension code; report any failure before state is committed.
        raise PluginError(f"{plugin_id} does not implement the icon provider contract: {exc}") from exc
