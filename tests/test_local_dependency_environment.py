import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_settings_ui import make_app

from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.core.presentation import snapshot_views
from tdnext.launcher import enabled_views
from tdnext.plugins.files.plugin import FilesPlugin

BTOP = Path(__file__).parents[1] / "state" / "bin" / "btop"
STARTED = threading.Event()
RELEASE = threading.Event()


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


@pytest.mark.parametrize("role", ["view", "provider"])
def test_local_binary_is_available_to_import_constructor_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    if not BTOP.is_file():
        pytest.skip("requires the existing project-local btop binary")
    app, manager = make_app(tmp_path)
    name = f"tdnext_btop_probe_{role}"
    module = tmp_path / f"{name}.py"
    header = (
        "import subprocess\n"
        "def probe():\n"
        "    return subprocess.run(['btop', '--version'], check=True, capture_output=True, text=True).stdout\n"
        "probe()\n"
    )
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
        })
    module.write_text(header + body, encoding="utf-8")
    manager.manifests = {**manager.manifests, manifest.id: manifest}
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("tdnext.core.manager.install_executable", install_btop)
    manager.enable("probe")
    assert os.environ["PATH"] == ""
    if role == "view":
        plugin = next(plugin for item, plugin in enabled_views(manager) if item.id == "probe")
        assert plugin.make_view(app.context) is not None
    else:
        AppearanceStore(manager.store.path.with_name("appearance.json")).write(Appearance(provider_id="probe"))
        assert snapshot_views(manager, app.views).icon_for("files") == "[]"
    assert os.environ["PATH"] == ""


def test_plugin_constructor_does_not_change_other_threads_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not BTOP.is_file():
        pytest.skip("requires the existing project-local btop binary")
    _app, manager = make_app(tmp_path)
    install_btop("btop", manager.local_bin)
    manifest = manager.manifests["files"].model_copy(update={
        "id": "probe", "entrypoint": f"{__name__}:BlockingView", "requires_plugins": (),
        "requires_executables": ("btop",),
    })
    manager.manifests = {**manager.manifests, manifest.id: manifest}
    monkeypatch.setenv("PATH", "")
    STARTED.clear()
    RELEASE.clear()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.enable, "probe")
        try:
            assert STARTED.wait(timeout=5), "plugin constructor did not reach its subprocess"
            assert os.environ["PATH"] == ""
            with pytest.raises(FileNotFoundError):
                subprocess.run(["btop", "--version"], check=True, capture_output=True)
        finally:
            RELEASE.set()
        future.result(timeout=10)
    assert os.environ["PATH"] == ""


def test_local_binary_is_available_when_a_view_runs_after_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    monkeypatch.setenv("PATH", "")
    manager.enable("probe")
    plugin = next(plugin for item, plugin in enabled_views(manager) if item.id == "probe")
    assert plugin.make_view(app.context) is not None
    assert os.environ["PATH"] == ""
