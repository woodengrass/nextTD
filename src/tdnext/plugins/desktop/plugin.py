"""Desktop shell plugin entry point."""

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory

import tdnext
from tdnext.api import PluginContext, ViewPlugin
from tdnext.core.errors import PluginError
from tdnext.core.local_env import restored_env
from tdnext.core.models import PluginManifest
from tdnext.core.session import SESSION_ENV, SessionSnapshot, session_token
from tdnext.runtime import state_dir

from .contract import textual_views
from .navigation import tab_name

CORE_BINARY_NAME = "tdnext"
CORE_REQUIRED_COMMANDS = (
    "open-tab-with-return", "open-tab-with-return-from-header", "restore-focus-from-header", "return-to-origin",
    "set-pane-fixed-width",
)
CORE_PROBE_TIMEOUT = 5


def core_candidate_path() -> Path:
    """Project-local core navigation binary selected only when capable."""
    return state_dir() / "bin" / CORE_BINARY_NAME


def core_capable_binary() -> Path | None:
    """Return the core binary only when it offers the required commands.

    The executable must expose all required navigation commands and chrome
    protocol version 1. Failed or timed-out probes reject the candidate.
    """
    packaged = Path(tdnext.__file__).parent / "_native" / CORE_BINARY_NAME
    for candidate in (core_candidate_path(), packaged):
        try:
            if not (candidate.is_file() and os.access(candidate, os.X_OK)):
                continue
            result = subprocess.run(
                [str(candidate), "action", "--help"],
                capture_output=True, text=True, check=False, timeout=CORE_PROBE_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            continue
        if result.returncode == 0:
            output = f"{result.stdout or ''}\n{result.stderr or ''}"
            if set(CORE_REQUIRED_COMMANDS).issubset(output.split()):
                try:
                    version = subprocess.run(
                        [str(candidate), "--tdnext-chrome-version"],
                        capture_output=True, text=True, check=False, timeout=CORE_PROBE_TIMEOUT,
                    )
                except (OSError, subprocess.TimeoutExpired, UnicodeError):
                    continue
                if version.returncode == 0 and version.stdout.strip() == "1":
                    return candidate
    return None


def primary_header_id(views: Sequence[tuple[PluginManifest, ViewPlugin]]) -> str | None:
    """Return the first header-placed plugin id, or None when absent."""
    return next((manifest.id for manifest, _ in views if manifest.placement == "header"), None)


def layout_string(
    views: Sequence[tuple[PluginManifest, ViewPlugin]],
    session: SessionSnapshot | None = None,
) -> str:
    python = json.dumps(sys.executable)
    if not views:
        return (
            f'layout {{\n    tab name="Desktop" focus=true {{\n'
            f"        pane command={python} {{\n"
            '            args "-c" "import signal; print(\'No views enabled. '
            "Enable a view and restart the desktop.\', flush=True); signal.pause()\"\n"
            "        }\n    }\n}"
        )
    snapshot = f' "--snapshot" {json.dumps(session_token(session))}' if session is not None else ""
    tabs: list[str] = []
    ordered = [view for view in views if view[0].placement == "sidebar"] + [
        view for view in views if view[0].placement == "header"
    ]
    for index, (manifest, _) in enumerate(ordered):
        name = json.dumps(tab_name(manifest.id) if manifest.placement == "header" else manifest.name)
        plugin_id = json.dumps(manifest.id)
        focused = " focus=true" if index == 0 else ""
        tabs.append(f'''tab name={name}{focused} {{
    pane focus=true borderless=true command={python} {{
        args "-m" "tdnext.pane" "view" {plugin_id}{snapshot}
    }}
}}''')
    return "layout {\n" + "\n".join(tabs) + "\n}"


class DesktopPlugin:
    def run(self, context: PluginContext, views: Sequence[tuple[PluginManifest, object]]) -> None:
        textual = textual_views(views)
        core_binary = core_capable_binary()
        if core_binary is None:
            raise PluginError(
                f"Dedicated tdnext executable missing or unusable: {core_candidate_path()}; "
                "restore it under state/bin and retry instead of using stock Zellij"
            )
        zellij = str(core_binary)
        size = shutil.get_terminal_size((80, 24))
        if "ZELLIJ" not in os.environ and size.columns >= 104 and size.lines >= 28:
            data = state_dir() / "zellij-data"
            data.mkdir(parents=True, exist_ok=True)
            try:
                env = {
                    **restored_env(),
                    "TDNEXT_ZELLIJ": zellij,
                    "TDNEXT_PYTHON": sys.executable,
                    "TDNEXT_DIRECTORY": str(context.directory),
                    "XDG_CACHE_HOME": str(state_dir() / "zellij-cache"),
                }
                env.pop(SESSION_ENV, None)
                header_id = primary_header_id(textual)
                sidebar_count = sum(manifest.placement == "sidebar" for manifest, _plugin in textual)
                bindings = "\n".join(
                    f'        bind "Alt {index}" {{ GoToTab {index}; }}'
                    if index <= sidebar_count
                    else f'        unbind "Alt {index}"'
                    for index in range(1, 10)
                )
                if header_id is None:
                    header_binding = '        unbind "Alt o"'
                else:
                    header_binding = (
                        '        bind "Alt o" {\n'
                        f"            OpenTabWithReturn {json.dumps(tab_name(header_id))};\n"
                        "        }"
                    )
                template = Path(__file__).with_name("zellij.kdl").read_text(encoding="utf-8")
                with TemporaryDirectory(prefix="tdnext-zellij-") as directory:
                    # Zellij needs this directory even with an explicit layout file. See zellij-layout-directory.adr.md.
                    (Path(directory) / "layouts").mkdir()
                    config = Path(directory) / "zellij.kdl"
                    config.write_text(
                        template.replace("        // TDNEXT_SIDEBAR_BINDINGS", bindings).replace(
                            "        // TDNEXT_HEADER_BINDING", header_binding
                        ),
                        encoding="utf-8",
                    )
                    layout = layout_string(textual, context.session)
                    layout_file = Path(directory) / "layout.kdl"
                    layout_file.write_text(layout, encoding="utf-8")
                    ordered = [view for view in textual if view[0].placement == "sidebar"] + [
                        view for view in textual if view[0].placement == "header"
                    ]
                    chrome = [
                        {
                            "id": manifest.id,
                            "name": manifest.name,
                            "icon": (context.session.icon_for(manifest.id) if context.session else None)
                            or manifest.icon or "[]",
                            "shortcuts": plugin.shortcuts,
                            "placement": manifest.placement,
                            "tab_name": tab_name(manifest.id) if manifest.placement == "header" else manifest.name,
                        }
                        for manifest, plugin in ordered
                    ]
                    chrome_file = Path(directory) / "chrome.json"
                    chrome_file.write_text(json.dumps(chrome, ensure_ascii=False), encoding="utf-8")
                    env["TDNEXT_CHROME_FILE"] = str(chrome_file)
                    command = [
                        zellij, "--config", str(config), "--config-dir", directory, "--data-dir", str(data),
                        "--new-session-with-layout", str(layout_file),
                    ]
                    result = subprocess.run(command, env=env, check=False)
            except OSError as exc:
                print(f"Could not launch Zellij: {exc}. Using fullscreen fallback.", file=sys.stderr)
            else:
                if result.returncode == 0:
                    return
                print(f"Zellij exited with status {result.returncode}. Using fullscreen fallback.", file=sys.stderr)
        from .app import DesktopApp

        DesktopApp(context, textual).run()
