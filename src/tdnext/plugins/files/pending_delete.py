from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.messages import Prune
from textual.widget import Widget
from textual.widgets import Button, Label, Static

from .dialogs import ConfirmDelete


class SelectionWaiter(Widget):
    DEFAULT_CSS = "SelectionWaiter { display: none; }"

    def __init__(
        self, resolve: Callable[[], Awaitable[Path | None]], apply: Callable[[Path | None], None]
    ) -> None:
        super().__init__()
        self.resolve = resolve
        self.apply = apply
        self._finished = anyio.Event()

    async def on_mount(self) -> None:
        async with anyio.create_task_group() as tasks:
            if not self._finished.is_set():
                tasks.start_soon(self._resolve)
            await self._finished.wait()
            tasks.cancel_scope.cancel()

    async def _resolve(self) -> None:
        target = await self.resolve()
        if not self._finished.is_set():
            self.apply(target)
            self._finished.set()

    def cancel(self) -> None:
        self._finished.set()


class PendingDelete(ConfirmDelete):
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]
    DEFAULT_CSS = "PendingDelete #pending-delete-waiter-host { display: none; }"

    def __init__(self, resolve: Callable[[], Awaitable[Path | None]]) -> None:
        super().__init__("")
        self.target: Path | None = None
        self._waiter = SelectionWaiter(resolve, self._set_target)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Delete permanently?")
            yield Static("Loading selected file…", id="pending-delete-detail", markup=False)
            yield Vertical(id="pending-delete-waiter-host")
            with Horizontal():
                yield Button("Cancel", id="cancel", action="screen.cancel")
                yield Button("Delete", id="delete", variant="error", disabled=True)

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()
        self.query_one("#pending-delete-waiter-host", Vertical).mount(self._waiter)

    def _set_target(self, target: Path | None) -> None:
        if self._pruning or self.app.screen is not self:
            return
        self.target = target
        detail = self.query_one("#pending-delete-detail", Static)
        if target is None:
            detail.update("No current selection. Cancel and choose a file.")
            return
        self.target_name = target.name
        detail.update(f"{self.target_name}\nDirectories and their contents will be removed.")
        self.query_one("#delete", Button).disabled = False

    def action_cancel(self) -> None:
        self._waiter.cancel()
        self.dismiss(False)

    @on(Prune)
    def _cancel_waiter_on_prune(self) -> None:
        self._waiter.cancel()
