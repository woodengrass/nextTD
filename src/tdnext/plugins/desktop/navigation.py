import os
import subprocess

from tdnext.core.errors import PluginError


def back_from_header() -> None:
    try:
        result = subprocess.run(
            [os.environ["TDNEXT_ZELLIJ"], "action", "return-to-origin"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, KeyError) as exc:
        raise PluginError(f"Cannot control Zellij: {exc}") from exc
    if result.returncode:
        raise PluginError(f"Zellij action failed: {result.stderr.strip()}")
