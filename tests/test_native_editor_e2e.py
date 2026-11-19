"""Native end-to-end regression for the Settings editor selection plus real edits.

Each test launches a fresh isolated native session (private tmux socket and
per-test tagged processes via ``NativeSession``), selects the editor through
the real Settings UI keyboard flow, and then opens a real file through the
native Files pane with the real editor binary:

- nvim: ``EDITOR`` is intentionally empty while the preference selects
  ``nvim``; an nvim-only ``:lua vim.api...`` update proves nvim ran.
- vi: ``EDITOR`` is intentionally empty (the environment choice would refuse
  to launch) while the preference selects ``vi``; a typed line plus ``:wq``
  proves vi ran.

Both tests assert ``editor.json``, the edited file contents, Finder
restoration via pane screens, and that the session's existing btop survives.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from native_environment import capable_core_source, core_wanted_message
from native_environment import native_env as native_env
from native_session import NativeSession, wait_for

PROJECT_ROOT = Path(__file__).parents[1]
PROJECT_NVIM = PROJECT_ROOT / "state" / "bin" / "nvim"


def require_nvim() -> str:
    """Return the real nvim binary or skip when no real nvim exists."""
    if PROJECT_NVIM.is_file() and os.access(PROJECT_NVIM, os.X_OK):
        return str(PROJECT_NVIM)
    found = shutil.which("nvim")
    if found is None:
        pytest.skip("nvim is not available (no project-local nvim and none on PATH)")
    assert found is not None
    return found


def require_vi() -> str:
    """Return the real vi binary or skip when no real vi exists."""
    found = shutil.which("vi")
    if found is None:
        pytest.skip("vi is not available on PATH")
    assert found is not None
    return found


def resolve_binary(stock: str) -> str:
    """Return the stage-3 core binary; fail when it is missing or incapable.

    The editor flows run the real desktop, which only starts on the
    stage-3 core now. Falling back to the stock binary would mask that
    failure behind an unrelated legacy route.
    """
    del stock
    source = capable_core_source()
    if source is None:
        pytest.fail(f"editor test requires a capable core binary: {core_wanted_message()}")
    return str(source)


def isolate_home(env: dict[str, str], tmp_path: Path) -> None:
    """Point HOME and the remaining XDG state dir at tmp_path (no user data)."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    xdg_state = tmp_path / "xdg-state"
    xdg_state.mkdir(parents=True, exist_ok=True)
    env.update({"HOME": str(home), "XDG_STATE_HOME": str(xdg_state)})
    env.pop("BASH_ENV", None)


def settings_pane(session: NativeSession) -> str:
    """Return the terminal id of the Settings tool pane."""
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    tool = next(pane for pane in panes if "tdnext.pane view settings" in (pane.get("pane_command") or ""))
    return f"terminal_{tool['id']}"


def files_pane(session: NativeSession) -> str:
    """Return the terminal id of the Files view pane."""
    panes = json.loads(session.action("list-panes", "--all", "--json"))
    pane = next(item for item in panes if "tdnext.pane view files" in (item.get("pane_command") or ""))
    return f"terminal_{pane['id']}"


def files_screen(session: NativeSession, pane_id: str) -> str:
    """Dump the current Files pane screen."""
    return session.action("dump-screen", "--pane-id", pane_id)


def select_editor(session: NativeSession, pane_id: str, *, downs: int, choice: str) -> Path:
    """Select an editor through the real Settings UI and wait for editor.json."""
    wait_for(lambda: "Editor:" in session.action("dump-screen", "--pane-id", pane_id))
    session.send_key("Tab")
    session.send_key("Tab")
    session.send_key("Tab")
    session.send_key("Enter")
    try:
        wait_for(lambda: "Neovim (nvim)" in session.action("dump-screen", "--pane-id", pane_id))
    except AssertionError as exc:
        screen = session.action("dump-screen", "--pane-id", pane_id)
        raise AssertionError(f"Editor overlay did not open; screen={screen!r}") from exc
    for _ in range(downs):
        session.send_key("Down")
    session.send_key("Enter")
    editor_path = Path(session.env["TDNEXT_STATE_DIR"]) / "editor.json"

    def saved() -> bool:
        try:
            return f'"{choice}"' in editor_path.read_text(encoding="utf-8")
        except OSError:
            return False

    wait_for(saved)
    return editor_path


def read_text(path: Path) -> str:
    """Read a file, returning empty text while the editor still holds it."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def test_native_nvim_selected_in_settings_edits_real_file(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    nvim = require_nvim()
    assert os.access(nvim, os.X_OK)
    env, stock = native_env
    binary = resolve_binary(stock)
    isolate_home(env, tmp_path)
    env["EDITOR"] = ""
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    target = work / "nvim-proof.txt"
    target.write_text("original nvim line\n", encoding="utf-8")
    session = NativeSession(work, env, binary)
    try:
        monitor = wait_for(session.btop_pid)
        session.open_settings()
        pane = settings_pane(session)
        editor_path = select_editor(session, pane, downs=1, choice="nvim")
        assert '"nvim"' in editor_path.read_text(encoding="utf-8")
        session.back_to("Files")
        files = files_pane(session)
        wait_for(lambda: "nvim-proof.txt" in files_screen(session, files))
        session.send_key("e")
        wait_for(lambda: "All" in files_screen(session, files))
        session.send_key("G")
        session.send_key("o")
        wait_for(lambda: "-- INSERT --" in files_screen(session, files))
        session.send_key("NVIM_TYPED_LINE")
        session.send_key("Escape")
        wait_for(lambda: "-- INSERT --" not in files_screen(session, files))
        session.send_key(':lua vim.api.nvim_buf_set_lines(0, -1, -1, false, {"NVIM_LUA_OK"})')
        session.send_key("Enter")
        wait_for(lambda: "NVIM_LUA_OK" in files_screen(session, files))
        session.send_key(":wq")
        session.send_key("Enter")
        wait_for(lambda: "NVIM_TYPED_LINE" in read_text(target) and "NVIM_LUA_OK" in read_text(target))
        wait_for(
            lambda: "nvim-proof.txt" in files_screen(session, files)
            and "PREVIEW" in files_screen(session, files)
        )
        assert session.active_tab() == session.tab_id("Files")
        content = target.read_text(encoding="utf-8")
        lines = content.splitlines()
        assert "original nvim line" in lines
        assert "NVIM_TYPED_LINE" in lines
        assert "NVIM_LUA_OK" in lines
        assert ":lua" not in content
        assert session.btop_pid() == monitor
    finally:
        session.close()


def test_native_vi_selected_in_settings_edits_real_file(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    vi = require_vi()
    assert os.access(vi, os.X_OK)
    env, stock = native_env
    binary = resolve_binary(stock)
    isolate_home(env, tmp_path)
    env["EDITOR"] = ""
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    target = work / "vi-proof.txt"
    target.write_text("original vi line\n", encoding="utf-8")
    session = NativeSession(work, env, binary)
    try:
        monitor = wait_for(session.btop_pid)
        session.open_settings()
        pane = settings_pane(session)
        editor_path = select_editor(session, pane, downs=2, choice="vi")
        assert '"vi"' in editor_path.read_text(encoding="utf-8")
        session.back_to("Files")
        files = files_pane(session)
        wait_for(lambda: "vi-proof.txt" in files_screen(session, files))
        session.send_key("e")
        wait_for(
            lambda: "vi-proof.txt" in (screen := files_screen(session, files)) and "PREVIEW" not in screen
        )
        session.send_key("G")
        session.send_key("o")
        session.send_key("VI_TYPED_LINE")
        session.send_key("Escape")
        session.send_key(":wq")
        session.send_key("Enter")
        wait_for(lambda: "VI_TYPED_LINE" in read_text(target))
        wait_for(
            lambda: "vi-proof.txt" in files_screen(session, files)
            and "PREVIEW" in files_screen(session, files)
        )
        assert session.active_tab() == session.tab_id("Files")
        content = target.read_text(encoding="utf-8")
        lines = content.splitlines()
        assert "original vi line" in lines
        assert "VI_TYPED_LINE" in lines
        assert session.btop_pid() == monitor
    finally:
        session.close()
