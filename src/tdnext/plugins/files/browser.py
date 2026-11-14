from dataclasses import dataclass
from functools import partial
from pathlib import Path

import anyio
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.message import Message
from textual.widgets import DataTable, OptionList, Static
from textual.worker import Worker, WorkerState

from .browser_widgets import FileTable, PreviewPane, PreviewScroll, single_line
from .listing import DirectorySnapshot, FileEntry, scan_directory
from .preview import read_preview
from .preview_controller import PreviewController


@dataclass(frozen=True, slots=True)
class ListingRequest:
    directory: Path
    hidden: bool
    select: Path | None
    revision: int


class FilesBrowser(Vertical):
    DEFAULT_CSS = """
    FilesBrowser { height: 1fr; }
    FilesBrowser #location { height: 2; padding-top: 1; text-style: bold; text-overflow: ellipsis; }
    FilesBrowser #file-panes { height: 1fr; }
    FilesBrowser #file-main { width: 1fr; height: 1fr; }
    FilesBrowser #file-parent { dock: left; width: 20; height: 1fr; margin-right: 1; background: $surface; }
    FilesBrowser #parent-heading, FilesBrowser #files-heading {
        height: 2; padding: 0 1; border-bottom: solid $foreground-muted; text-style: bold; text-overflow: ellipsis;
    }
    FilesBrowser #neighbours {
        height: 1fr; border: none; padding: 0; background: $surface;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    FilesBrowser #file-table { height: 1fr; }
    FilesBrowser #file-status { height: 1; color: $text-muted; text-overflow: ellipsis; }
    FilesBrowser PreviewPane { margin-left: 1; }
    FilesBrowser.compact PreviewPane { width: 1fr; margin-left: 0; }
    """

    class OpenDirectory(Message):
        def __init__(self, directory: Path) -> None:
            super().__init__()
            self.directory = directory

    def __init__(self) -> None:
        super().__init__()
        self.entries: tuple[FileEntry, ...] = ()
        self.neighbours: tuple[Path, ...] = ()
        self._listing_revision = 0
        self._listing_worker: Worker[DirectorySnapshot] | None = None
        self._listing_active: ListingRequest | None = None
        self._listing_pending: ListingRequest | None = None
        self._selection_changed = anyio.Event()
        self._compact_preview = False

    def compose(self) -> ComposeResult:
        yield Static(id="location", markup=False)
        with Horizontal(id="file-panes"):
            with Vertical(id="file-main"):
                yield Static("FILES", id="files-heading")
                yield FileTable(id="file-table")
                yield Static(id="file-status", markup=False)
            with Vertical(id="file-parent"):
                yield Static("FOLDERS", id="parent-heading")
                yield OptionList(id="neighbours", markup=False, compact=True)
            yield PreviewController(self.selected, read_preview)

    def on_mount(self) -> None:
        self.update_layout()

    def on_resize(self, _event: Resize) -> None:
        if self.is_mounted:
            self.update_layout()

    def update_layout(self) -> None:
        width = self.size.width
        compact = width < 58
        focused = self.app.focused
        self.set_class(compact, "compact")
        parent = self.query_one("#file-parent")
        main = self.query_one("#file-main")
        preview = self.query_one(PreviewPane)
        table = self.query_one(FileTable)
        preview_scroll = self.query_one(PreviewScroll)
        parent.display = width >= 96
        main.display = not compact or not self._compact_preview
        preview.display = not compact or self._compact_preview
        if focused is preview_scroll and not preview.display:
            table.focus()
        if (focused is table and not main.display) or (
            focused is self.query_one(OptionList) and not parent.display
        ):
            (table if main.display else preview_scroll).focus()

    def toggle_preview(self) -> None:
        preview = self.query_one(PreviewScroll)
        table = self.query_one(FileTable)
        if self.size.width < 58:
            self._compact_preview = not self._compact_preview
            self.update_layout()
            (preview if self._compact_preview else table).focus()
        elif self.app.focused is preview:
            table.focus()
        else:
            preview.focus()

    def focus_folders(self) -> None:
        if self.query_one("#file-parent").display:
            self.screen.set_focus(self.query_one(OptionList))
        else:
            self.app.notify("Folder column needs more width; use h to browse the parent.")

    def load(self, directory: Path, select: Path | None, *, hidden: bool) -> None:
        self._listing_revision += 1
        self._signal_selection_change()
        self.entries = ()
        self.neighbours = ()
        self.query_one("#location", Static).update(f"Files  /  {single_line(str(directory))}")
        self.query_one(FileTable).set_entries(())
        self.query_one(OptionList).clear_options()
        status = self.query_one("#file-status", Static)
        status.display = True
        status.update("Loading folder…")
        self.query_one(PreviewController).reset("Loading folder…")
        self._listing_pending = ListingRequest(directory, hidden, select, self._listing_revision)
        self._start_listing()

    def _start_listing(self) -> None:
        if self._listing_worker is not None or self._listing_pending is None:
            return
        request, self._listing_pending = self._listing_pending, None
        self._listing_active = request
        self._listing_worker = self.run_worker(
            partial(scan_directory, request.directory, hidden=request.hidden),
            name="files-directory", thread=True, exit_on_error=False,
        )

    def selected_entry(self) -> FileEntry | None:
        if self._listing_worker is not None and self._listing_worker.is_finished:
            self._finish_listing()
        table = self.query_one(FileTable)
        row = table.cursor_row
        return self.entries[row] if not table.selection_pending and 0 <= row < table.row_count else None

    def selected(self) -> Path | None:
        entry = self.selected_entry()
        return entry.path if entry is not None else None

    @property
    def selection_revision(self) -> int:
        return self._listing_revision

    @property
    def selection_loading(self) -> bool:
        return self._listing_worker is not None or self._listing_pending is not None or (
            self.query_one(FileTable).selection_pending
        )

    def _signal_selection_change(self) -> None:
        changed, self._selection_changed = self._selection_changed, anyio.Event()
        changed.set()

    async def wait_for_selection(self, revision: int) -> Path | None:
        while revision == self._listing_revision:
            changed = self._selection_changed
            selected = self.selected()
            if selected is not None or not self.selection_loading:
                return selected
            await changed.wait()
        return None

    def permit_file_action(self) -> bool:
        if self.app.focused is self.query_one(OptionList):
            self.app.notify("Focus the file list before using file actions.", severity="warning")
            return False
        return True

    async def selected_for_action(self) -> Path | None:
        if not self.permit_file_action():
            return None
        selected = self.selected()
        if self.query_one(FileTable).selection_pending:
            self.app.notify("The selected file is still loading; try again.", severity="warning")
            return None
        if selected is None and self.selection_loading:
            self.app.notify("Folder is still loading; try again.", severity="warning")
        return selected

    def _show_listing(self, snapshot: DirectorySnapshot, request: ListingRequest) -> None:
        preview = self.query_one(PreviewPane)
        if snapshot.error:
            preview.show_message(snapshot.error)
            self.query_one("#files-heading", Static).update("FILES")
            self.query_one("#file-status", Static).update(single_line(snapshot.error))
            return
        self.query_one("#files-heading", Static).update(f"FILES  ·  {len(snapshot.entries)}")
        self.entries = snapshot.entries
        table = self.query_one(FileTable)
        table.set_entries(self.entries, request.select)
        status = self.query_one("#file-status", Static)
        status.display = not self.entries or table.row_count < len(self.entries)
        status.update("Loading file list…" if self.entries else "Empty folder — n: folder, f: file")
        folder_paths = ([snapshot.directory.parent] if snapshot.directory.parent != snapshot.directory else [])
        folder_paths.extend(snapshot.neighbours)
        self.neighbours = tuple(folder_paths)
        options = self.query_one(OptionList)
        labels = [Text("  ..  Parent", no_wrap=True, overflow="ellipsis")] if (
            snapshot.directory.parent != snapshot.directory
        ) else []
        labels.extend(
            Text(f"{'>' if path == snapshot.directory else ' '} {single_line(path.name or '/')}")
            for path in snapshot.neighbours
        )
        options.set_options(labels)
        if snapshot.directory in self.neighbours:
            options.highlighted = self.neighbours.index(snapshot.directory)
        heading = "FOLDERS" if not snapshot.neighbour_error else "FOLDERS (unavailable)"
        self.query_one("#parent-heading", Static).update(heading)
        self.update_preview()

    def on_file_table_population_finished(self, event: FileTable.PopulationFinished) -> None:
        table = self.query_one(FileTable)
        if event.revision == table._snapshot_revision and self.entries:
            self.query_one("#file-status", Static).display = False
            self._signal_selection_change()
            self.update_preview()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        options = self.query_one(OptionList)
        if event.option_list is options and 0 <= event.option_index < len(self.neighbours) and (
            event.option_index < options.option_count and options.options[event.option_index] is event.option
        ):
            self.post_message(self.OpenDirectory(self.neighbours[event.option_index]))

    def on_data_table_row_highlighted(self, _event: DataTable.RowHighlighted) -> None:
        self._signal_selection_change()
        self.update_preview()

    def update_preview(self) -> None:
        target = self.selected()
        empty_message = (
            "Loading selected file…" if self.query_one(FileTable).selection_pending
            else "This folder is empty. Press n or f to create an item."
        )
        self.query_one(PreviewController).select(target, empty_message)

    def _finish_listing(self) -> None:
        worker, request = self._listing_worker, self._listing_active
        if worker is None:
            return
        self._listing_worker = None
        self._listing_active = None
        if request is not None and request.revision == self._listing_revision:
            if worker.result is not None:
                self._show_listing(worker.result, request)
            elif worker.error is not None:
                self.query_one(PreviewPane).show_message(f"Cannot read directory: {worker.error}")
        self._start_listing()
        self._signal_selection_change()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state not in {WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED}:
            return
        if event.worker is self._listing_worker:
            self._finish_listing()
