"""Files pane contract after the native cutover.

The old Textual ``FilesPane`` is retired: ``tdnext.plugins.files.plugin:FilesPlugin``
only implements ``run_pane``, which execs the real ``tdnext-files`` Rust
binary. Folder and delete behaviour is covered by the Rust controller/action
suites plus ``test_native_files_process.py``. These tests pin what Python
still owns: the ``files`` plugin identity and the native launch boundary,
exercised through a real subprocess that execs a fake external binary.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tdnext.api import PluginContext
from tdnext.core.discovery import discover
from tdnext.core.errors import PluginError
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginState
from tdnext.core.state import StateStore
from tdnext.plugins.files.plugin import FilesPlugin


def manager_for(tmp_path: Path) -> PluginManager:
    roots = (Path(__file__).parents[1] / "src" / "tdnext" / "plugins",)
    manifests = discover(roots)
    return PluginManager(manifests, StateStore(tmp_path / "state.json"), tmp_path / "bin")


def test_files_plugin_stays_native_only_with_stable_id(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    assert manager.manifests["files"].id == "files"

    plugin = FilesPlugin()
    assert callable(plugin.run_pane)
    assert not hasattr(plugin, "make_view")


def test_run_pane_execs_real_subprocess_with_single_directory_argument(tmp_path: Path) -> None:
    manager = manager_for(tmp_path)
    manager.local_bin.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "argv.log"
    fake = manager.local_bin / "tdnext-files"
    fake.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{log_path}"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    directory = tmp_path / "work folder"
    directory.mkdir()
    enabled = frozenset({"desktop", "files"})
    StateStore(tmp_path / "plugins.json").write(PluginState(installed=enabled, enabled=enabled))
    repo_root = Path(__file__).parents[1]
    env = dict(os.environ)
    env.update(TDNEXT_STATE_DIR=str(tmp_path), TDNEXT_DIRECTORY=str(directory))
    env["PYTHONPATH"] = str(repo_root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-m", "tdnext.pane", "view", "files"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert log_path.read_text(encoding="utf-8").splitlines() == [str(directory)]


def test_run_pane_without_binary_is_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = manager_for(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert manager.executable("tdnext-files") is None
    context = PluginContext(tmp_path, manager)

    with pytest.raises(PluginError, match="tdnext-files is missing"):
        FilesPlugin().run_pane(context)
