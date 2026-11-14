import os
import shlex
from pathlib import Path

import anyio
import pytest
from ghostty_textual import TerminalView
from test_settings_ui import make_app
from textual.content import Content
from textual.widgets import ContentSwitcher, ListView, Static

from tdnext.plugins.desktop.app import DesktopApp
from tdnext.plugins.shell.plugin import ShellPlugin
from tdnext.plugins.shell.transport import PtySession
from tdnext.plugins.shell.view import ShellView


def screen_text(terminal: TerminalView) -> str:
    terminal.refresh_frame(force=True)
    return "\n".join(terminal.render_line(row).text for row in range(terminal.terminal.rows))


async def wait_for(predicate, *, timeout: float = 8) -> None:
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.02)


def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DesktopApp:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace ; $(not-a-command)"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("HISTFILE", str(home / "history"))
    monkeypatch.setenv("INPUTRC", "/dev/null")
    monkeypatch.delenv("BASH_ENV", raising=False)
    app, manager = make_app(tmp_path)
    views = [app.views[0], (manager.manifests["shell"], ShellPlugin()), app.views[-1]]
    return DesktopApp(type(app.context)(workspace, manager), views)


async def focus_shell(app: DesktopApp, pilot) -> tuple[ShellView, TerminalView, PtySession]:
    rail = app.query_one("#destinations", ListView)
    rail.focus()
    rail.index = 1
    await pilot.press("enter")
    view = app.query_one(ShellView)
    await wait_for(lambda: view.session is not None and app.focused is app.query_one(TerminalView))
    assert view.session is not None
    return view, app.query_one(TerminalView), view.session


@pytest.mark.anyio
async def test_real_shell_accepts_interactive_input_and_survives_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = isolated_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        view, terminal, session = await focus_shell(app, pilot)
        await session.send(b"read -r answer; printf 'USER_RESULT:%s\\n' \"$answer\"\n")
        await pilot.press("o", "k", "enter")
        await wait_for(lambda: "USER_RESULT:ok" in screen_text(terminal))
        await pilot.press("alt+o")
        assert app.query_one(ContentSwitcher).current == "view-settings"
        await pilot.press("escape")
        assert app.query_one(ContentSwitcher).current == "view-shell"
        assert app.focused is terminal
        assert view.session is session and session.process.poll() is None
        assert "USER_RESULT:ok" in screen_text(terminal)
        await pilot.press("f6")
        assert app.focused is app.query_one("#destinations", ListView)
        footer = app.query_one("#shortcuts", Static).render()
        assert isinstance(footer, Content)
        assert "Global:" in footer.plain


@pytest.mark.anyio
async def test_terminal_forwards_tab_and_control_keys_without_stealing_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = isolated_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        _view, terminal, session = await focus_shell(app, pilot)
        command = (
            b"python3 -c 'import sys; print(\"GO\"+\"ING\", flush=True); "
            b"print(\"READ:\"+repr(sys.stdin.readline()), flush=True)'\n"
        )
        await session.send(command)
        await wait_for(lambda: "GOING" in screen_text(terminal))
        theme = app.theme
        await pilot.press("a", "tab", "b", "ctrl+t", "enter")
        await wait_for(lambda: "READ:'a\\tb\\x14\\n'" in screen_text(terminal))
        assert app.focused is terminal and app.theme == theme


@pytest.mark.anyio
async def test_fullscreen_curses_program_receives_a_key_and_returns_to_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = isolated_app(tmp_path, monkeypatch)
    program = tmp_path / "interactive.py"
    program.write_text(
        "import curses\n"
        "def run(screen):\n"
        "    screen.addstr(0, 0, 'CURSES_READY')\n"
        "    screen.refresh()\n"
        "    return screen.getch()\n"
        "print('CURSES_RESULT:' + chr(curses.wrapper(run)), flush=True)\n",
        encoding="utf-8",
    )
    async with app.run_test(size=(100, 30)) as pilot:
        _view, terminal, session = await focus_shell(app, pilot)
        await session.send(f"python3 {shlex.quote(str(program))}\n".encode())
        await wait_for(lambda: "CURSES_READY" in screen_text(terminal))
        await pilot.press("z")
        await wait_for(lambda: "CURSES_RESULT:z" in screen_text(terminal))


@pytest.mark.anyio
async def test_shell_receives_workspace_and_pty_resize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = isolated_app(tmp_path, monkeypatch)
    async with app.run_test(size=(80, 24)) as pilot:
        _view, terminal, session = await focus_shell(app, pilot)
        await session.send(b"pwd\n")
        await wait_for(
            lambda: str(app.context.directory) in "".join(line.rstrip() for line in screen_text(terminal).splitlines())
        )
        await session.send(b"printf 'TERM:%s COLORTERM:%s\\n' \"$TERM\" \"$COLORTERM\"\n")
        await wait_for(lambda: "TERM:xterm-256color COLORTERM:truecolor" in screen_text(terminal))
        await pilot.resize_terminal(120, 36)
        await wait_for(lambda: terminal.terminal.cols == terminal.content_size.width and terminal.size.width > 80)
        rows, columns = terminal.content_size.height, terminal.content_size.width
        await session.send(b"stty size\n")
        await wait_for(lambda: f"{rows} {columns}" in screen_text(terminal))


@pytest.mark.anyio
async def test_shell_exit_is_reported_and_enter_starts_a_new_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = isolated_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        view, terminal, session = await focus_shell(app, pilot)
        await session.send(b"exit\n")
        await wait_for(lambda: view.finished)
        assert session.process.returncode == 0
        assert view.query_one("#shell-status", Static).display
        await pilot.press("enter")
        await wait_for(lambda: view.session is not None and view.session is not session)
        assert app.focused is terminal


@pytest.mark.anyio
async def test_closing_desktop_reaps_the_shell_and_foreground_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = isolated_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        _view, _terminal, session = await focus_shell(app, pilot)
        await session.send(b"sleep 60\n")
        await wait_for(lambda: session.master is not None and os.tcgetpgrp(session.master) != session.process.pid)
        assert session.master is not None
        foreground = os.tcgetpgrp(session.master)
        assert foreground > 0
    assert session.process.poll() is not None
    process_stat = Path(f"/proc/{foreground}/stat")
    assert not process_stat.exists(), process_stat.read_text(encoding="utf-8")
    with pytest.raises(ProcessLookupError):
        os.killpg(foreground, 0)


@pytest.mark.anyio
async def test_closing_desktop_terminates_a_foreground_job_ignoring_hangup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = isolated_app(tmp_path, monkeypatch)
    async with app.run_test(size=(100, 30)) as pilot:
        _view, terminal, session = await focus_shell(app, pilot)
        command = (
            b"python3 -c 'import signal,time; signal.signal(signal.SIGHUP,signal.SIG_IGN); "
            b"print(\"STUBBORN_\"+\"READY\",flush=True); time.sleep(60)'\n"
        )
        await session.send(command)
        await wait_for(lambda: "STUBBORN_READY" in screen_text(terminal))
        assert session.master is not None
        foreground = os.tcgetpgrp(session.master)
        assert foreground > 0 and foreground != session.process.pid
    assert session.process.poll() is not None
    process_stat = Path(f"/proc/{foreground}/stat")
    assert not process_stat.exists(), process_stat.read_text(encoding="utf-8")
    with pytest.raises(ProcessLookupError):
        os.killpg(foreground, 0)
