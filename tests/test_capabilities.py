from pathlib import Path

import pytest
from pydantic import ValidationError

from tdnext.core.errors import PluginError
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginManifest, PluginState
from tdnext.core.state import StateStore


class BrokenProvider:
    def resolve(self, _key: str, _mode: str) -> int:
        return 42


def manifest_data(plugin_id: str) -> dict[str, str | int]:
    return {
        "id": plugin_id,
        "name": plugin_id,
        "version": "1",
        "description": plugin_id,
        "entrypoint": "tdnext.plugins.files.plugin:FilesPlugin",
        "manifest_version": 1,
        "api_version": 1,
    }


@pytest.mark.parametrize("capabilities", [[], ["desktop.view", "desktop.view"], ["files.browse"],
                                           ["desktop.view", "icons.provider"], ["desktop.invalid"], ["view"]])
def test_explicit_invalid_roles_are_rejected(capabilities: list[str]) -> None:
    with pytest.raises(ValidationError):
        PluginManifest.model_validate({**manifest_data("example"), "capabilities": capabilities})


def test_explicit_role_is_not_determined_by_plugin_id() -> None:
    manifest = PluginManifest.model_validate({
        **manifest_data("desktop"), "capabilities": ["desktop.view", "files.browse"]
    })
    assert manifest.role == "desktop.view"


def test_invalid_icon_provider_does_not_commit_state(tmp_path: Path) -> None:
    manifest = PluginManifest.model_validate({
        **manifest_data("provider"),
        "entrypoint": f"{__name__}:BrokenProvider",
        "capabilities": ["icons.provider"],
    })
    store = StateStore(tmp_path / "plugins.json")
    before = PluginState(installed=frozenset({"existing"}), enabled=frozenset({"existing"}))
    store.write(before)
    with pytest.raises(PluginError, match="icon provider contract"):
        PluginManager({manifest.id: manifest}, store, tmp_path / "bin").enable(manifest.id)
    assert store.read() == before
