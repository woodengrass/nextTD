"""Mouse coverage for the real ``tdnext-files`` binary (default pytest discovery).

Replaces ``native-panes/files/tests/mouse_clicks.py`` (outside ``testpaths``)
with the same real file/select/open/sidebar/cancel/delete checks, using the
shared :mod:`tests.files_pty` harness instead of a copied one.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from files_pty import FilesPty, require_files_binary


@pytest.fixture()
def mouse_root(tmp_path: Path) -> dict[str, Path]:
    folder = tmp_path / "work"
    opened = tmp_path / "editor-arguments"
    sibling = tmp_path / "sibling"
    state_dir = tmp_path / "state"
    folder.mkdir()
    opened.mkdir()
    sibling.mkdir()
    state_dir.mkdir()
    (folder / "a-note").write_text("Keep this one", encoding="utf-8")
    (folder / "b-note").write_text("Preview this one", encoding="utf-8")
    (sibling / "inside-sibling").touch()
    return {"folder": folder, "opened": opened, "sibling": sibling, "state": state_dir}


def start_files(mouse_root: dict[str, Path]) -> FilesPty:
    binary = require_files_binary()
    editor = shlex.join(["ln", "-s", "-t", str(mouse_root["opened"])])
    pty = FilesPty(
        binary,
        mouse_root["folder"],
        mouse_root["state"],
        extra_env={"EDITOR": editor},
    )
    pty.wait_for("Keep this one")
    return pty


def test_select_then_open_file(mouse_root: dict[str, Path]) -> None:
    pty = start_files(mouse_root)
    try:
        pty.click_sgr("b-note ")
        pty.wait_for("Preview this one")
        assert list(mouse_root["opened"].iterdir()) == []
        pty.click_sgr("b-note ")
        # A key queued after the click acts as a barrier: Files handles it after
        # the foreground editor has exited and the terminal has been restored.
        pty.send("?")
        pty.wait_for("File shortcuts")
        assert (mouse_root["opened"] / "b-note").readlink() == mouse_root["folder"] / "b-note"
    finally:
        pty.close()


def test_open_sidebar_folder(mouse_root: dict[str, Path]) -> None:
    pty = start_files(mouse_root)
    try:
        pty.click_sgr("sibling")
        frame = pty.wait_for("inside-sibling")
        assert f"Files  /  {mouse_root['sibling']}" in frame
    finally:
        pty.close()


def test_cancel_then_confirm_delete(mouse_root: dict[str, Path]) -> None:
    pty = start_files(mouse_root)
    try:
        pty.click_sgr("b-note")
        pty.wait_for("Preview this one")
        pty.send("d")
        pty.click_sgr(" Cancel ")
        pty.send("?")
        pty.wait_for("File shortcuts")
        assert (mouse_root["folder"] / "b-note").read_text(encoding="utf-8") == "Preview this one"
        pty.send("?", "d")
        pty.click_sgr("Delete", same_row_as="Cancel")
        pty.wait_for("Deleted: b-note")
        assert not (mouse_root["folder"] / "b-note").exists()
        assert (mouse_root["folder"] / "a-note").read_text(encoding="utf-8") == "Keep this one"
    finally:
        pty.close()
