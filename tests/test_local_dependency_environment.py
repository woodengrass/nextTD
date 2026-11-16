import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_settings_ui import make_app

from tdnext.api import ViewPlugin
from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.core.errors import InstallationConsentRequired
from tdnext.core.local_env import (
    BOOTSTRAPPED_ENV,
    ORIGINAL_ENV,
    ORIGINAL_PRESENT_ENV,
    bootstrap,
    restored_env,
)
from tdnext.core.manager import PluginManager
from tdnext.core.presentation import snapshot_views
from tdnext.launcher import enabled_views
from tdnext.plugins.files.plugin import FilesPlugin

BTOP = Path(__file__).parents[1] / "state" / "bin" / "btop"
STARTED = threading.Event()
RELEASE = threading.Event()

PROBE_DEPS_TEMPLATE = (
    "import shutil\n"
    "class ProbeDependencies:\n"
    "    @staticmethod\n"
    "    def check(manager):\n"
    "        return None if manager.executable('btop') else 'Install fixture btop for probe'\n"
    "    @staticmethod\n"
    "    def install(manager):\n"
    "        from pathlib import Path\n"
    f"        source = Path({str(BTOP)!r})\n"
    "        manager.local_bin.mkdir(parents=True, exist_ok=True)\n"
    "        path = manager.local_bin / 'btop'\n"
    "        shutil.copy2(source, path)\n"
    "        path.chmod(0o755)\n"
    "        return path\n"
)

PROBE_HEADER = (
    "import subprocess\n"
    "def probe():\n"
    "    return subprocess.run(['btop', '--version'], check=True, capture_output=True, text=True).stdout\n"
    "probe()\n"
)



class BlockingView(FilesPlugin):
    def __init__(self) -> None:
        subprocess.run(["btop", "--version"], check=True, capture_output=True)
        STARTED.set()
        if not RELEASE.wait(timeout=10):
            raise TimeoutError("constructor did not resume")


def install_btop(name: str, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / name
    shutil.copy2(BTOP, path)
    return path


_BOOTSTRAP_MARKERS = (BOOTSTRAPPED_ENV, ORIGINAL_ENV, ORIGINAL_PRESENT_ENV)


def _bootstrap_tmp_bin(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, state: Path
) -> None:
    """Explicit bootstrap contract: <state>/bin on PATH, original PATH captured.

    Snapshot the bootstrap markers (whether present or absent) and restore them
    at teardown: monkeypatch records no undo entry for initially-absent vars, so
    without this a leaked TDNEXT_PATH_BOOTSTRAPPED would make later tests in the
    same process believe this deleted tmp bin is their bootstrapped state.
    """
    saved = {marker: os.environ.get(marker) for marker in _BOOTSTRAP_MARKERS}

    def _restore_markers() -> None:
        for marker, value in saved.items():
            if value is None:
                os.environ.pop(marker, None)
            else:
                os.environ[marker] = value

    request.addfinalizer(_restore_markers)
    monkeypatch.setenv("PATH", "")
    bootstrap(state)


def _enable_with_consent(manager: PluginManager, plugin_id: str) -> None:
    """Mirror the CLI consent flow for the probe's plugin-owned installer."""
    try:
        manager.enable(plugin_id)
    except InstallationConsentRequired as exc:
        manager.enable(plugin_id, approved=exc.actions)


@pytest.mark.parametrize("role", ["view", "provider"])
def test_local_binary_is_available_to_import_constructor_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, role: str
) -> None:
    if not BTOP.is_file():
        pytest.skip("requires the existing project-local btop binary")
    app, manager = make_app(tmp_path)
    name = f"tdnext_btop_probe_{role}"
    module = tmp_path / f"{name}.py"
    (tmp_path / f"{name}_deps.py").write_text(PROBE_DEPS_TEMPLATE, encoding="utf-8")
    hook = f"{name}_deps:ProbeDependencies"
    if role == "view":
        body = (
            "from tdnext.plugins.files.plugin import FilesPlugin\n"
            "class Probe(FilesPlugin):\n"
            "    def __init__(self): probe()\n"
            "    def make_view(self, context):\n"
            "        probe()\n"
            "        return super().make_view(context)\n"
        )
        manifest = manager.manifests["files"].model_copy(update={
            "id": "probe", "entrypoint": f"{name}:Probe", "requires_plugins": (),
            "requires_executables": ("btop",),
            "dependency_hook": hook,
        })
    else:
        body = (
            "class Probe:\n"
            "    def __init__(self): probe()\n"
            "    def resolve(self, key, mode):\n"
            "        probe()\n"
            "        return '[]'\n"
        )
        manifest = manager.manifests["icons"].model_copy(update={
            "id": "probe", "entrypoint": f"{name}:Probe", "requires_executables": ("btop",),
            "dependency_hook": hook,
        })
    module.write_text(PROBE_HEADER + body, encoding="utf-8")
    manager.manifests = {**manager.manifests, manifest.id: manifest}
    monkeypatch.syspath_prepend(str(tmp_path))
    _bootstrap_tmp_bin(monkeypatch, request, tmp_path)
    assert os.environ["PATH"] == str(manager.local_bin)
    _enable_with_consent(manager, "probe")
    assert os.environ["PATH"] == str(manager.local_bin)
    if role == "view":
        plugin = next(plugin for item, plugin in enabled_views(manager) if item.id == "probe")
        assert isinstance(plugin, ViewPlugin)
        assert plugin.make_view(app.context) is not None
    else:
        AppearanceStore(manager.store.path.with_name("appearance.json")).write(Appearance(provider_id="probe"))
        assert snapshot_views(manager, app.views).icon_for("files") == "[]"
    assert os.environ["PATH"] == str(manager.local_bin)
    assert restored_env()["PATH"] == ""


def test_plugin_constructor_does_not_change_other_threads_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    if not BTOP.is_file():
        pytest.skip("requires the existing project-local btop binary")
    _app, manager = make_app(tmp_path)
    install_btop("btop", manager.local_bin)
    manifest = manager.manifests["files"].model_copy(update={
        "id": "probe", "entrypoint": f"{__name__}:BlockingView", "requires_plugins": (),
        "requires_executables": ("btop",),
    })
    manager.manifests = {**manager.manifests, manifest.id: manifest}
    _bootstrap_tmp_bin(monkeypatch, request, tmp_path)
    assert os.environ["PATH"] == str(manager.local_bin)
    STARTED.clear()
    RELEASE.clear()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.enable, "probe")
        try:
            assert STARTED.wait(timeout=5), "plugin constructor did not reach its subprocess"
            assert os.environ["PATH"] == str(manager.local_bin)
            assert restored_env()["PATH"] == ""
            assert "btop" in subprocess.run(
                ["btop", "--version"], check=True, capture_output=True, text=True
            ).stdout.lower()
        finally:
            RELEASE.set()
        future.result(timeout=10)
    assert os.environ["PATH"] == str(manager.local_bin)


def test_local_binary_is_available_when_a_view_runs_after_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    if not BTOP.is_file():
        pytest.skip("requires the existing project-local btop binary")
    app, manager = make_app(tmp_path)
    install_btop("btop", manager.local_bin)
    name = "tdnext_runtime_btop_probe"
    module = tmp_path / f"{name}.py"
    module.write_text(
        "import subprocess\n"
        "from tdnext.plugins.files.plugin import FilesPlugin\n"
        "class Probe(FilesPlugin):\n"
        "    def make_view(self, context):\n"
        "        subprocess.run(['btop', '--version'], check=True, capture_output=True)\n"
        "        return super().make_view(context)\n",
        encoding="utf-8",
    )
    manifest = manager.manifests["files"].model_copy(update={
        "id": "probe", "entrypoint": f"{name}:Probe", "requires_plugins": (),
        "requires_executables": ("btop",),
    })
    manager.manifests = {**manager.manifests, manifest.id: manifest}
    monkeypatch.syspath_prepend(str(tmp_path))
    _bootstrap_tmp_bin(monkeypatch, request, tmp_path)
    assert os.environ["PATH"] == str(manager.local_bin)
    manager.enable("probe")
    plugin = next(plugin for item, plugin in enabled_views(manager) if item.id == "probe")
    assert isinstance(plugin, ViewPlugin)
    assert plugin.make_view(app.context) is not None
    assert os.environ["PATH"] == str(manager.local_bin)
    assert restored_env()["PATH"] == ""
