import os
import subprocess
import sys
from pathlib import Path

import pytest
from rich.cells import cell_len
from test_settings_ui import make_app, text_of
from textual.widgets import Static
from typer.testing import CliRunner

from tdnext import pane
from tdnext.core.appearance import Appearance, AppearanceStore
from tdnext.core.discovery import discover
from tdnext.core.errors import PluginError
from tdnext.core.models import PluginState
from tdnext.core.session import SESSION_ENV, SessionSnapshot, SessionView
from tdnext.core.views import enabled_views
from tdnext.plugins.icons.plugin import CodiconsProvider


def test_all_bundled_manifests_declare_meaningful_capabilities(tmp_path: Path) -> None:
    _app, manager = make_app(tmp_path)
    expected = {
        "desktop": "desktop.host", "files": "files.browse", "monitor": "system.monitor",
        "settings": "settings.manage", "shell": "shell.interactive", "icons": "icons.provider",
    }
    assert set(manager.manifests) == set(expected)
    for plugin_id, capability in expected.items():
        manifest = manager.manifests[plugin_id]
        assert manifest.capabilities and capability in manifest.capabilities
        if manifest.role == "desktop.view":
            assert manifest.icon_key


def test_duplicate_discovered_ids_are_rejected(tmp_path: Path) -> None:
    contents = (
        'id = "same"\nname = "Same"\nversion = "1"\ndescription = "Same"\n'
        "manifest_version = 1\napi_version = 1\n"
        'view_interfaces = ["tdnext.textual-view.v1"]\n'
        'entrypoint = "tdnext.plugins.files.plugin:FilesPlugin"\n'
        'capabilities = ["desktop.view", "files.browse"]\n'
    )
    for name in ("first", "second"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "plugin.toml").write_text(contents, encoding="utf-8")
    manifests = discover((tmp_path,))
    assert "same" not in manifests
    assert "same" in manifests.errors
    assert "Duplicate plugin id" in manifests.errors["same"]


def test_icon_service_is_not_a_view_and_disable_does_not_cascade(tmp_path: Path) -> None:
    _app, manager = make_app(tmp_path)
    before = manager.store.read()
    manager.enable("icons")
    assert [manifest.id for manifest, _ in enabled_views(manager)] == ["files", "settings"]
    assert manager.disable("icons") == frozenset({"icons"})
    assert manager.store.read().enabled == before.enabled
    manager.uninstall("icons")
    assert manager.store.read() == before


def test_pinned_codicons_and_compatibility_are_safe_single_cell_or_ascii() -> None:
    provider = CodiconsProvider()
    expected = {"files": "\uea83", "monitor": "\ueb31", "shell": "\uea85", "settings": "\ueb52"}
    for key, codepoint in expected.items():
        assert provider.resolve(key, "codicons") == codepoint
        assert cell_len(codepoint) == 1
        assert provider.resolve(key, "compatibility").isascii()
    assert provider.resolve("unknown", "codicons") == "[]"
    assert provider.resolve("unknown", "compatibility") == "[]"


def test_failed_appearance_replace_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AppearanceStore(tmp_path / "appearance.json")
    old = Appearance(provider_id="icons", icon_mode="compatibility")
    store.write(old)

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("tdnext.core.state.os.replace", fail_replace)
    with pytest.raises(PluginError, match="Cannot save appearance"):
        store.write(Appearance(provider_id="icons", icon_mode="codicons"))
    assert store.read() == old
    assert list(tmp_path.iterdir()) == [store.path]


@pytest.mark.anyio
@pytest.mark.parametrize("size", [(80, 24), (120, 36)])
@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_settings_selectors_save_with_mouse(
    tmp_path: Path, size: tuple[int, int], theme: str
) -> None:
    app, manager = make_app(tmp_path)
    manager.enable("icons")
    app.theme = theme
    async with app.run_test(size=size) as pilot:
        await pilot.click("#icon-provider")
        await pilot.press("down", "enter")
        await pilot.click("#icon-mode")
        await pilot.press("down", "enter")
        await pilot.pause()
        appearance = AppearanceStore(manager.store.path.with_name("appearance.json")).read()
        assert appearance == Appearance(provider_id="icons", icon_mode="codicons")
        assert "Restart desktop" in text_of(app.query_one("#plugin-feedback", Static))


@pytest.mark.anyio
async def test_settings_selectors_are_keyboard_operable_without_running_plugin_shortcuts(tmp_path: Path) -> None:
    app, manager = make_app(tmp_path)
    manager.enable("icons")
    before = manager.store.read()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press("shift+tab", "enter", "down", "shift+tab", "shift+tab", "enter", "d")
        assert manager.store.read() == before
        await pilot.press("enter", "down", "enter", "tab", "enter", "down", "enter")
        assert AppearanceStore(manager.store.path.with_name("appearance.json")).read() == Appearance(
            provider_id="icons", icon_mode="codicons"
        )


def test_native_child_uses_its_session_snapshot_after_state_changes(tmp_path: Path) -> None:
    _app, manager = make_app(tmp_path)
    manifest = manager.manifests["files"]
    session = SessionSnapshot(views=(SessionView(manifest=manifest, icon="[F]"),))
    manager.store.write(PluginState(installed=frozenset({"desktop"}), enabled=frozenset({"desktop"})))
    code = (
        "from tdnext.core.views import enabled_views; from tdnext.runtime import manager; "
        "from tdnext.core.session import current_session; "
        "print(','.join(m.id for m, _ in enabled_views(manager(), current_session()))); "
        "print(current_session().icon_for('files'))"
    )
    env = {**os.environ, "TDNEXT_STATE_DIR": str(tmp_path), SESSION_ENV: session.model_dump_json()}
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["files", "[F]"]


def test_native_external_program_does_not_inherit_session_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, manager = make_app(tmp_path)
    manifest = manager.manifests["files"]
    session = SessionSnapshot(views=(SessionView(manifest=manifest, icon="[F]"),))
    observed: list[str | None] = []
    monkeypatch.setattr(pane, "manager", lambda: manager)
    monkeypatch.setattr("tdnext.plugins.files.plugin.FilesPlugin.run_pane", lambda _self, _context: observed.append(
        os.environ.get(SESSION_ENV)
    ))
    monkeypatch.setenv("TDNEXT_DIRECTORY", str(tmp_path))
    monkeypatch.setenv(SESSION_ENV, session.model_dump_json())
    result = CliRunner().invoke(pane.cli, ["view", "files"])
    assert result.exit_code == 0, result.output
    assert observed == [None]
