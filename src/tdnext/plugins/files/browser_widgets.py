from collections.abc import Iterable
from datetime import datetime
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Final

from rich.cells import cell_len
from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.coordinate import Coordinate
from textual.events import Click, Resize
from textual.geometry import Size
from textual.message import Message
from textual.timer import Timer
from textual.widgets import DataTable, Static
from textual.widgets.data_table import ColumnKey, RowKey

from .listing import FileEntry
from .preview import FilePreview

BATCH_SIZE: Final = 256
BATCH_SECONDS: Final = 0.008


def single_line(value: str) -> str:
    return "".join(char if char.isprintable() else "?" for char in value)


class FileTable(DataTable):
    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("l,right", "select_cursor", "Open", show=False, priority=True),
        Binding("left", "parent_folder", "Parent", show=False, priority=True),
    ]

    class ParentRequested(Message):
        pass

    class PopulationFinished(Message):
        def __init__(self, revision: int) -> None:
            super().__init__()
            self.revision = revision

    def __init__(self, *, id: str) -> None:
        self._fixed_offsets: list[tuple[RowKey, int]] = []
        super().__init__(id=id, cursor_type="row", zebra_stripes=False)
        self.entries: tuple[FileEntry, ...] = ()
        self._column_keys: tuple[ColumnKey, ...] = ()
        self._column_widths: tuple[int, ...] = ()
        self._snapshot_revision = 0
        self._population_timer: Timer | None = None
        self._pending_select: Path | None = None
        self._restoring_selection = False

    def on_mount(self) -> None:
        self.configure_columns(self.scrollable_content_region.width)

    def on_resize(self, _event: Resize) -> None:
        self.configure_columns(self.scrollable_content_region.width)

    def watch_show_vertical_scrollbar(self, _show: bool) -> None:
        if self.is_mounted:
            self.configure_columns(self.scrollable_content_region.width)

    def set_entries(self, entries: tuple[FileEntry, ...], select: Path | None = None) -> None:
        self._snapshot_revision += 1
        if self._population_timer is not None:
            self._population_timer.stop()
            self._population_timer = None
        self.entries = entries
        self.clear()
        self._pending_select = select if entries else None
        if entries:
            self._populate_batch(self._snapshot_revision)

    def _populate_batch(self, revision: int) -> None:
        if revision != self._snapshot_revision:
            return
        start = self.row_count
        end = min(start + BATCH_SIZE, len(self.entries))
        deadline = monotonic() + BATCH_SECONDS
        for index in range(start, end):
            entry = self.entries[index]
            name = single_line(entry.path.name)
            cells = (escape(name + ("/" if entry.directory else "")), entry.size, entry.modified)
            row_key = self.add_row(*cells, key=f"{revision}:{index}")
            self._fixed_offsets.append((row_key, 0))
            if self._pending_select == entry.path:
                self._pending_select = None
                self._restoring_selection = True
                try:
                    self.move_cursor(row=index)
                finally:
                    self._restoring_selection = False
            if monotonic() >= deadline:
                end = index + 1
                break
        if end < len(self.entries):
            self._population_timer = self.set_timer(0.001, partial(self._populate_batch, revision))
        else:
            self._population_timer = None
            self._pending_select = None
            self.post_message(self.PopulationFinished(revision))

    def watch_cursor_coordinate(self, old_coordinate: Coordinate, new_coordinate: Coordinate) -> None:
        super().watch_cursor_coordinate(old_coordinate, new_coordinate)
        if old_coordinate != new_coordinate and not getattr(self, "_restoring_selection", False):
            self._pending_select = None

    @property
    def _y_offsets(self) -> list[tuple[RowKey, int]]:
        return self._fixed_offsets

    def _update_dimensions(self, new_rows: Iterable[RowKey]) -> None:
        # All Files rows have height one and fixed column widths; avoid rescanning the entire table per batch.
        width = sum(column.get_render_width(self) for column in self.columns.values())
        header_height = self.header_height if self.show_header else 0
        self.virtual_size = Size(width + self._row_label_column_width, self.row_count + header_height)

    def entry_for_key(self, row_key: RowKey) -> FileEntry | None:
        value = row_key.value
        if self.selection_pending or value is None or (
            not value.startswith(f"{self._snapshot_revision}:") or row_key not in self.rows
        ):
            return None
        return self.entries[int(value.partition(":")[2])]

    def _post_selected_message(self) -> None:
        if self.selection_pending:
            self.app.notify("The selected file is still loading; try again.", severity="warning")
            return
        super()._post_selected_message()

    @property
    def selection_pending(self) -> bool:
        return self._pending_select is not None

    def configure_columns(self, width: int) -> None:
        if not self._column_keys:
            self._column_keys = (
                self.add_column("Name", width=20), self.add_column("Size", width=9),
                self.add_column("Modified", width=16),
            )
        available = max(0, width - self._row_label_column_width - 2 * self.cell_padding * len(self._column_keys))
        if available >= 20 + 9 + 16:
            widths = (available - 9 - 16, 9, 16)
        elif available >= 18 + 9:
            widths = (available - 9, 9, 0)
        else:
            widths = (available, 0, 0)
        if widths == self._column_widths:
            return
        for key, column_width in zip(self._column_keys, widths, strict=True):
            self.columns[key].width = column_width
        self._column_widths = widths
        current_width = sum(column.get_render_width(self) for column in self.columns.values())
        self.virtual_size = Size(current_width + self._row_label_column_width, self.virtual_size.height)
        # Textual 8.2.8 has no public width setter; only rendered-cell caches depend on these widths.
        self._cell_render_cache.clear()
        self._row_render_cache.clear()
        self._line_cache.clear()
        self.refresh(layout=True)

    def action_parent_folder(self) -> None:
        self.post_message(self.ParentRequested())


class PreviewScroll(ScrollableContainer):
    def on_click(self, _event: Click) -> None:
        self.focus()


class PreviewPane(Vertical):
    DEFAULT_CSS = """
    PreviewPane { width: 40%; height: 1fr; background: $surface; }
    PreviewPane #preview-heading { height: 2; padding: 0 1; border-bottom: solid $foreground-muted; text-style: bold; }
    PreviewPane #preview-meta {
        height: 3; padding: 0 1; color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis;
    }
    PreviewPane #preview-scroll { height: 1fr; padding: 0 1; }
    PreviewPane #preview-content { height: auto; text-wrap: nowrap; text-overflow: ellipsis; }
    """

    def compose(self) -> ComposeResult:
        yield Static("PREVIEW", id="preview-heading", markup=False)
        yield Static("Select a file to preview.", id="preview-meta", markup=False)
        with PreviewScroll(id="preview-scroll"):
            yield Static(id="preview-content", markup=False)

    def show_message(self, message: str) -> None:
        self.query_one("#preview-meta", Static).update(single_line(message))
        content = self.query_one("#preview-content", Static)
        content.styles.width = "100%"
        content.update("")
        self.query_one(PreviewScroll).scroll_home(animate=False)

    def show_preview(self, preview: FilePreview) -> None:
        size = f"{preview.size:,} B" if preview.size is not None else "–"
        try:
            modified = (
                datetime.fromtimestamp(preview.modified).strftime("%Y-%m-%d %H:%M")
                if preview.modified is not None else "–"
            )
        except (OSError, OverflowError, ValueError):
            modified = "–"
        truncated = "  ·  truncated" if preview.truncated else ""
        self.query_one("#preview-meta", Static).update(
            f"{single_line(preview.path.name)}\n{preview.kind}  ·  {size}{truncated}\nModified {modified}"
        )
        content = Text(no_wrap=True, overflow="ellipsis")
        if preview.message:
            content.append(single_line(preview.message))
        for number, line in enumerate(preview.lines, 1):
            if number > 1 or preview.message:
                content.append("\n")
            content.append(f"{number:>3} │ ", style="dim")
            content.append(line)
        if preview.truncated:
            content.append("\n… Preview truncated (64 KiB / 300 lines / 512 characters per line).", style="dim")
        widget = self.query_one("#preview-content", Static)
        widget.styles.width = max((cell_len(line) for line in content.plain.splitlines()), default=1)
        widget.update(content)
        self.query_one(PreviewScroll).scroll_home(animate=False)
