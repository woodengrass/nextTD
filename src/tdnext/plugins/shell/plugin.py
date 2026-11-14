"""Shell plugin entry point for native and fallback desktops."""

from textual.widget import Widget

from tdnext.api import PluginContext
from tdnext.core.errors import PluginError

from .launch import exec_shell
from .view import ShellView


class ShellPlugin:
    shortcuts = "Shell keys   Ctrl+g Zellij lock"
    fallback_shortcuts = "Tab complete   F6 sidebar   Shift+PgUp scroll"
    fallback_focus_hint = "F6 sidebar"

    def make_view(self, context: PluginContext) -> Widget:
        return ShellView(context.directory)

    def run_pane(self, context: PluginContext) -> None:
        try:
            exec_shell(context.directory)
        except OSError as exc:
            raise PluginError(f"Cannot start shell: {exc}") from exc
