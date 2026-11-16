"""Shared helpers for the user-approved process PATH contract tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tdnext.core.models import PluginManifest
from tdnext.core.session import SessionSnapshot, SessionView, session_token

PY = sys.executable
REPO_ROOT = Path(__file__).parents[1]
TDNEXT_BIN = REPO_ROOT / ".venv" / "bin" / "tdnext"

MARKERS = (
    "TDNEXT_PATH_BOOTSTRAPPED",
    "TDNEXT_ORIGINAL_PATH",
    "TDNEXT_ORIGINAL_PATH_PRESENT",
)


def base_env(tmp_path: Path, state: Path, *, path_state: str) -> dict[str, str]:
    """Build an isolated child environment with the requested PATH state."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TDNEXT_")}
    env.pop("BASH_ENV", None)
    env["TDNEXT_STATE_DIR"] = str(state)
    env["PYTHONPATH"] = str(tmp_path) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    if path_state == "absent":
        env.pop("PATH", None)
    elif path_state == "empty":
        env["PATH"] = ""
    else:
        env["PATH"] = "/usr/bin:/bin"
    return env


def fake_btop(state: Path) -> Path:
    """Install a fake version-reporting btop into the isolated state bin."""
    bin_dir = state / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    btop = bin_dir / "btop"
    btop.write_text("#!/bin/sh\necho 'btop version 1.4.8'\n", encoding="utf-8")
    btop.chmod(0o755)
    return btop


def probe_view(tmp_path: Path, state: Path) -> None:
    """Write a view probe that uses bare ``btop`` at import/init/runtime."""
    (tmp_path / "probeview_mod.py").write_text(
        "import os\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        "subprocess.run(['btop', '--version'], check=True, capture_output=True)\n"
        "if os.environ.get('TDNEXT_TEST_MARKER'):\n"
        "    Path(os.environ['TDNEXT_TEST_MARKER']).write_text(os.environ.get('PATH', '<unset>'))\n"
        "from tdnext.plugins.files.plugin import FilesPlugin\n"
        "class Probe(FilesPlugin):\n"
        "    def __init__(self):\n"
        "        subprocess.run(['btop', '--version'], check=True, capture_output=True)\n"
        "    def make_view(self, context):\n"
        "        subprocess.run(['btop', '--version'], check=True, capture_output=True)\n"
        "        return super().make_view(context)\n"
        "    def run_pane(self, context):\n"
        "        subprocess.run(['btop', '--version'], check=True, capture_output=True)\n"
        "        print('PANE_VIEW_RUNTIME_OK', flush=True)\n",
        encoding="utf-8",
    )
    plugin_dir = state / "plugins" / "probeview"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(
        "id = \"probeview\"\nname = \"Probe\"\nversion = \"0.1.0\"\n"
        "description = \"probe\"\nentrypoint = \"probeview_mod:Probe\"\n"
        "manifest_version = 1\napi_version = 1\n"
        "view_interfaces = [\"tdnext.textual-view.v1\"]\n"
        "capabilities = [\"desktop.view\", \"files.browse\"]\n"
        "requires_executables = [\"btop\"]\n",
        encoding="utf-8",
    )


def probe_provider(tmp_path: Path, state: Path) -> None:
    """Write a provider probe that uses bare ``btop`` at import/init/resolve."""
    (tmp_path / "probeicons_mod.py").write_text(
        "import os\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        "subprocess.run(['btop', '--version'], check=True, capture_output=True)\n"
        "if os.environ.get('TDNEXT_TEST_MARKER'):\n"
        "    Path(os.environ['TDNEXT_TEST_MARKER']).write_text(os.environ.get('PATH', '<unset>'))\n"
        "class Probe:\n"
        "    def __init__(self):\n"
        "        subprocess.run(['btop', '--version'], check=True, capture_output=True)\n"
        "    def resolve(self, key, mode):\n"
        "        subprocess.run(['btop', '--version'], check=True, capture_output=True)\n"
        "        if os.environ.get('TDNEXT_TEST_RESOLVE_MARKER'):\n"
        "            Path(os.environ['TDNEXT_TEST_RESOLVE_MARKER']).write_text('resolved')\n"
        "        return '[]'\n",
        encoding="utf-8",
    )
    plugin_dir = state / "plugins" / "probeicons"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.toml").write_text(
        "id = \"probeicons\"\nname = \"ProbeIcons\"\nversion = \"0.1.0\"\n"
        "description = \"probe\"\nentrypoint = \"probeicons_mod:Probe\"\n"
        "manifest_version = 1\napi_version = 1\n"
        "capabilities = [\"icons.provider\"]\nrequires_executables = [\"btop\"]\n",
        encoding="utf-8",
    )


def run_child(args: list[str], env: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run one isolated child process with a bounded timeout."""
    return subprocess.run(args, capture_output=True, text=True, env=env, check=False, timeout=timeout)


def probe_token() -> str:
    """Build a session token embedding the view probe manifest."""
    manifest = PluginManifest(
        id="probeview", name="Probe", version="0.1.0", description="probe",
        entrypoint="probeview_mod:Probe", requires_executables=("btop",),
        capabilities=("desktop.view", "files.browse"),
        view_interfaces=("tdnext.textual-view.v1",),
    )
    return session_token(SessionSnapshot(views=(SessionView(manifest=manifest, icon="[]"),)))
