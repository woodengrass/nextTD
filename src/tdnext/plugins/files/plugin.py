import os

from tdnext.api import PluginContext
from tdnext.core.errors import PluginError


class FilesPlugin:
    shortcuts = (
        "Enter open   Backspace up   ? help   v preview   b folders   g path   n folder   r rename   "
        "y/x/p copy/move   d delete"
    )

    def run_pane(self, context: PluginContext) -> None:
        binary = context.manager.executable("tdnext-files")
        if binary is None:
            raise PluginError("tdnext-files is missing; install it in state/bin or on PATH")
        try:
            os.execv(binary, [binary, str(context.directory)])
        except OSError as exc:
            raise PluginError(f"Cannot start tdnext-files: {exc}") from exc
