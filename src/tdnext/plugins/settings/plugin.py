import os
import subprocess

from textual.app import App, ComposeResult
from textual.events import AppFocus
from textual.widget import Widget

from tdnext.api import CloseView, PluginContext
from tdnext.core.errors import PluginError

from .view import SettingsView


class SettingsPane(App[None]):
    def __init__(self, context: PluginContext) -> None:
        super().__init__()
        self.context = context

    def compose(self) -> ComposeResult:
        yield SettingsView(self.context)

    def on_close_view(self, _event: CloseView) -> None:
        try:
            result = subprocess.run(
                [os.environ["TDNEXT_ZELLIJ"], "action", "return-to-origin"],
                capture_output=True, text=True, check=False, timeout=5,
            )
            if result.returncode:
                raise PluginError(f"Zellij action failed: {result.stderr.strip()}")
        except (KeyError, OSError, subprocess.TimeoutExpired, PluginError) as exc:
            self.notify(f"Cannot return to previous view: {exc}", severity="error")

    def on_app_focus(self, _event: AppFocus) -> None:
        view = self.query_one(SettingsView)
        view.on_native_activation()


class SettingsPlugin:
    shortcuts = "Esc Back   Tab Sections and controls"

    def make_view(self, context: PluginContext) -> Widget:
        return SettingsView(context)

    def run_pane(self, context: PluginContext) -> None:
        SettingsPane(context).run()
