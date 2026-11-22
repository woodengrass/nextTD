"""Contract tests for persistent editor preferences."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from tdnext.core.editor_preferences import (
    EditorChoice,
    EditorPreferences,
    EditorPreferencesStore,
)
from tdnext.core.errors import PluginError
from tdnext.core.state import StateStore


def test_default_editor_is_environment() -> None:
    prefs = EditorPreferences()

    assert prefs.editor == "environment"


def test_preferences_are_immutable() -> None:
    prefs = EditorPreferences(editor="nvim")

    with pytest.raises(ValidationError):
        prefs.__setattr__("editor", "vi")


def test_rejects_unknown_editor_choice(tmp_path: Path) -> None:
    store = EditorPreferencesStore(tmp_path / "editor.json")
    store.path.write_text('{"editor": "emacs"}', encoding="utf-8")

    with pytest.raises(PluginError):
        store.read()


def test_missing_file_returns_default(tmp_path: Path) -> None:
    store = EditorPreferencesStore(tmp_path / "editor.json")

    assert store.read() == EditorPreferences(editor="environment")


def test_corrupt_file_raises_actionable_error(tmp_path: Path) -> None:
    store = EditorPreferencesStore(tmp_path / "editor.json")
    store.path.write_text("not-json", encoding="utf-8")

    with pytest.raises(PluginError, match="Cannot read editor preferences"):
        store.read()


@pytest.mark.parametrize("choice", ["environment", "nvim", "vi"])
def test_write_then_read_roundtrip(tmp_path: Path, choice: EditorChoice) -> None:
    store = EditorPreferencesStore(tmp_path / "editor.json")

    store.write(EditorPreferences(editor=choice))

    assert store.read() == EditorPreferences(editor=choice)


def test_store_file_is_sibling_of_manager_store(tmp_path: Path) -> None:
    manager_store = StateStore(tmp_path / "plugins.json")
    editor_store = EditorPreferencesStore(manager_store.path.with_name("editor.json"))

    assert editor_store.path.name == "editor.json"
    assert editor_store.path.parent == manager_store.path.parent


def test_write_preserves_sibling_state_files(tmp_path: Path) -> None:
    manager_store = StateStore(tmp_path / "plugins.json")
    manager_store.path.write_text('{"installed": [], "enabled": []}', encoding="utf-8")
    before = manager_store.path.read_text(encoding="utf-8")
    editor_store = EditorPreferencesStore(manager_store.path.with_name("editor.json"))

    editor_store.write(EditorPreferences(editor="nvim"))

    assert manager_store.path.read_text(encoding="utf-8") == before
    assert editor_store.read().editor == "nvim"
