"""A real Ghostty viewport connected to an owned interactive PTY."""

from pathlib import Path

import anyio
from ghostty_textual import TerminalView
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static
from textual.worker import Worker, WorkerState

from .transport import PtySession


class ShellView(Vertical):
    BINDINGS = [Binding("enter", "restart_shell", "Restart shell", priority=True, show=False)]
    DEFAULT_CSS = """
    ShellView { height: 1fr; }
    ShellView #shell-status { height: 1; color: $text-muted; text-overflow: ellipsis; }
    """

    def __init__(self, directory: Path) -> None:
        super().__init__()
        self.directory = directory
        self.session: PtySession | None = None
        self.worker: Worker[int] | None = None
        self.finished = False
        self.stopping = False

    def compose(self) -> ComposeResult:
        yield TerminalView(
            send=self.send,
            resize_transport=self.resize_pty,
            reserved_keys=frozenset({"f6", "f7", "alt+o", "ctrl+q"}),
            id="shell-terminal",
        )
        yield Static(id="shell-status", markup=False)

    def on_mount(self) -> None:
        self.query_one("#shell-status", Static).display = False
        self.call_after_refresh(self.start_session)

    def start_session(self) -> None:
        if self.stopping or self.session is not None:
            return
        try:
            session = PtySession(self.directory)
        except OSError as exc:
            self.show_status(f"Cannot start shell: {exc}. Enter: retry   F6: sidebar")
            return
        self.session = session
        self.finished = False
        self.query_one("#shell-status", Static).display = False
        terminal = self.query_one(TerminalView)
        columns, rows = terminal.sync_terminal_size()
        session.resize(columns, rows)
        self.worker = self.run_worker(self.watch_session(session), name="shell-pty", exit_on_error=False)

    async def watch_session(self, session: PtySession) -> int:
        try:
            code = await session.watch(self.query_one(TerminalView).feed)
        finally:
            with anyio.CancelScope(shield=True):
                await session.aclose()
        if not self.stopping and self.session is session:
            self.session = None
            self.show_status(f"Shell exited ({code}). Enter: restart   F6: sidebar")
        return code

    async def on_unmount(self) -> None:
        self.stopping = True
        if self.session is not None:
            with anyio.CancelScope(shield=True):
                await self.session.aclose()

    async def send(self, data: bytes) -> None:
        if self.session is not None:
            await self.session.send(data)

    def resize_pty(self, columns: int, rows: int) -> None:
        if self.session is not None:
            self.session.resize(columns, rows)

    def show_status(self, message: str) -> None:
        self.finished = True
        status = self.query_one("#shell-status", Static)
        status.update(message)
        status.display = True

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "restart_shell":
            return self.finished
        return super().check_action(action, parameters)

    async def action_restart_shell(self) -> None:
        terminal = self.query_one(TerminalView)
        await terminal.reset_io()
        terminal.hard_reset()
        self.start_session()
        terminal.focus()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is self.worker and event.state == WorkerState.ERROR and not self.stopping:
            self.session = None
            self.show_status(f"Shell failed: {event.worker.error}. Enter: retry   F6: sidebar")
