import os
import subprocess

from tdnext.core.errors import PluginError


def tab_name(plugin_id: str) -> str:
    return f"tdnext-header:{plugin_id}"


def action(*arguments: str) -> str:
    try:
        result = subprocess.run(
            [os.environ["TDNEXT_ZELLIJ"], "action", *arguments],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, KeyError) as exc:
        raise PluginError(f"Cannot control Zellij: {exc}") from exc
    if result.returncode:
        raise PluginError(f"Zellij action failed: {result.stderr.strip()}")
    return result.stdout


def restore_header_focus() -> None:
    action("restore-focus-from-header")


def open_header(plugin_id: str) -> None:
    action("open-tab-with-return-from-header", tab_name(plugin_id))


def back_from_header() -> None:
    action("return-to-origin")
