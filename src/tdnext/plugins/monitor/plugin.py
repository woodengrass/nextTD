"""The monitor view runs the real btop executable."""

import os
import subprocess

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import Button, Static

from tdnext.api import PluginContext
from tdnext.core.errors import PluginError
from tdnext.runtime import state_dir


def btop_environment() -> dict[str, str]:
    return {
        **os.environ,
        "XDG_CONFIG_HOME": str(state_dir() / "btop" / "config"),
        "XDG_DATA_HOME": str(state_dir() / "btop" / "data"),
        "XDG_CACHE_HOME": str(state_dir() / "btop" / "cache"),
    }


class MonitorView(Vertical):
    DEFAULT_CSS = """
    MonitorView { padding: 1 2; }
    MonitorView Static { height: auto; margin-bottom: 1; }
    """

    def __init__(self, context: PluginContext) -> None:
        super().__init__()
        self.context = context

    def compose(self) -> ComposeResult:
        yield Static("Monitor", classes="title")
        yield Static("Run btop full screen. Press q in btop to return to the desktop.")
        yield Button("Open btop", id="open-btop", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        binary = self.context.manager.executable("btop")
        if binary is None:
            self.app.notify("btop is missing. Run: tdnext enable monitor", severity="error")
            return
        try:
            with self.app.suspend():
                result = subprocess.run([binary], env=btop_environment(), check=False)
            if result.returncode:
                self.app.notify(f"btop exited with status {result.returncode}", severity="error")
        except OSError as exc:
            self.app.notify(f"Cannot start btop: {exc}", severity="error")


class MonitorPlugin:
    shortcuts = "btop: q quit   arrows select   ? help"
    fallback_shortcuts = "Enter open btop   q return inside btop"

    def make_view(self, context: PluginContext) -> Widget:
        return MonitorView(context)

    def run_pane(self, context: PluginContext) -> None:
        binary = context.manager.executable("btop")
        if binary is None:
            raise PluginError("btop is missing. Run: tdnext enable monitor")
        os.execve(binary, [binary], btop_environment())
