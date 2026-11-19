import json
import os
import subprocess
import sys

from tdnext.core.session import SESSION_ENV


def test_shortcut_entrypoint_forwards_target_without_loading_ui_or_modifying_environment() -> None:
    script = """
import json, os, runpy, sys
from tdnext.plugins.desktop import navigation
original = dict(os.environ)
result = {}
def open_header(target):
    result['target'] = target
    result['environment_unchanged'] = dict(os.environ) == original
    result['heavy_modules'] = sorted(name for name in sys.modules if name == 'textual' or name.startswith('textual.')
        or name == 'typer' or name.startswith('typer.') or name == 'tdnext.launcher'
        or name == 'tdnext.plugins.desktop.panes' or name.startswith('tdnext.plugins.settings.')
        or name.startswith('tdnext.plugins.files.'))
navigation.open_header = open_header
sys.argv = ['tdnext.plugins.desktop.shortcut', 'preferences']
runpy.run_module('tdnext.plugins.desktop.shortcut', run_name='__main__')
print(json.dumps(result))
"""
    env = {**os.environ, SESSION_ENV: "unchanged-sentinel", "PYTHONDONTWRITEBYTECODE": "1"}

    process = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False)

    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {"target": "preferences", "environment_unchanged": True, "heavy_modules": []}


def test_shortcut_entrypoint_surfaces_navigation_errors() -> None:
    env = {key: value for key, value in os.environ.items() if key != "TDNEXT_ZELLIJ"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    process = subprocess.run(
        [sys.executable, "-m", "tdnext.plugins.desktop.shortcut", "settings"],
        env=env, capture_output=True, text=True, check=False,
    )

    assert process.returncode == 1
    assert "Cannot control Zellij" in process.stderr
