"""Finder-inspired, keyboard-first local file manager."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from functools import partial
from pathlib import Path
from typing import assert_never

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable

from tdnext.api import PluginContext
from tdnext.core.errors import PluginError
from tdnext.core.local_env import restored_env

from . import filesystem as fs
from .browser import FilesBrowser
from .browser_widgets import FileTable
from .dialogs import ConfirmDelete, FileHelp, NameDialog
from .editor_preferences import EditorPreferencesStore
from .pending_delete import PendingDelete


class FilesView(Vertical):
    BINDINGS = [
        ("backspace", "parent", "Parent"),
        ("h", "parent", "Parent"),
        ("n", "new_folder", "Folder"),
        ("f", "new_file", "File"),
        ("r", "rename", "Rename"),
        ("d", "delete", "Delete"),
        ("y", "copy", "Copy"),
        ("x", "cut", "Cut"),
        ("p", "paste", "Paste"),
        ("period", "hidden", "Hidden"),
        ("ctrl+r", "reload", "Refresh"),
        ("e", "edit", "Edit"),
        ("g", "go", "Go to path"),
        ("v", "preview", "Preview"),
        ("b", "folders", "Folders"),
        ("question_mark", "help", "Help"),
        Binding("tab", "focus_out_next", "Next focus", priority=True, show=False),
        Binding("shift+tab", "focus_out_previous", "Previous focus", priority=True, show=False),
    ]
    DEFAULT_CSS = """
    FilesView { height: 1fr; padding: 0 1; }
    """

    def __init__(self, directory: Path, context: PluginContext | None = None) -> None:
        super().__init__()
        self.directory = directory
        # NOTE: ``_context`` is taken by Textual's MessagePump (a context-manager
        # method used on every mount), so the plugin context lives under a
        # collision-free name.
        self._plugin_context = context
        self.clipboard: fs.Clipboard | None = None
        self.show_hidden = False

    def compose(self) -> ComposeResult:
        yield FilesBrowser()

    def on_mount(self) -> None:
        self.reload_directory()
        self.query_one(FileTable).focus()

    def reload_directory(self, select: Path | None = None) -> None:
        self.query_one(FilesBrowser).load(self.directory, select, hidden=self.show_hidden)

    def selected(self) -> Path | None:
        return self.query_one(FilesBrowser).selected()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        table = self.query_one(FileTable)
        entry = table.entry_for_key(event.row_key) if event.data_table is table else None
        if entry is not None and entry.directory:
            self.directory = entry.path
            self.reload_directory()
        elif entry is not None:
            self.edit(entry.path)

    def on_files_browser_open_directory(self, event: FilesBrowser.OpenDirectory) -> None:
        self.directory = event.directory
        self.reload_directory()
        self.query_one(FileTable).focus()

    def on_file_table_parent_requested(self, _event: FileTable.ParentRequested) -> None:
        self.action_parent()

    def action_preview(self) -> None:
        self.query_one(FilesBrowser).toggle_preview()

    def action_folders(self) -> None:
        self.query_one(FilesBrowser).focus_folders()

    def focus_outside(self, *, backwards: bool) -> None:
        chain = self.screen.focus_chain
        focused = self.app.focused
        if focused not in chain:
            return
        index = chain.index(focused)
        direction = -1 if backwards else 1
        for offset in range(1, len(chain) + 1):
            target = chain[(index + direction * offset) % len(chain)]
            if self not in target.ancestors:
                self.screen.set_focus(target)
                return
        if backwards:
            self.screen.focus_previous()
        else:
            self.screen.focus_next()

    def action_focus_out_next(self) -> None:
        self.focus_outside(backwards=False)

    def action_focus_out_previous(self) -> None:
        self.focus_outside(backwards=True)

    def action_parent(self) -> None:
        previous = self.directory
        self.directory = self.directory.parent
        self.reload_directory(previous)

    def action_reload(self) -> None:
        self.reload_directory(self.selected())

    def action_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self.reload_directory(self.selected())

    def action_help(self) -> None:
        self.app.push_screen(FileHelp())

    def action_go(self) -> None:
        self.app.push_screen(NameDialog("Go to folder", str(self.directory)), self.go_to)

    def go_to(self, value: str | None) -> None:
        if value is None:
            return
        target = Path(value).expanduser()
        if not target.is_absolute():
            target = self.directory / target
        if not target.is_dir():
            self.app.notify(f"Not a directory: {target}", severity="error")
            return
        self.directory = target.resolve()
        self.reload_directory()

    def action_new_folder(self) -> None:
        if self.query_one(FilesBrowser).permit_file_action():
            self.app.push_screen(NameDialog("New folder name"), lambda name: self.create(name, folder=True))

    def action_new_file(self) -> None:
        if self.query_one(FilesBrowser).permit_file_action():
            self.app.push_screen(NameDialog("New file name"), lambda name: self.create(name, folder=False))

    def create(self, name: str | None, *, folder: bool) -> None:
        if name is None:
            return
        try:
            self.reload_directory(fs.create(self.directory, name, folder=folder))
        except (OSError, fs.FileActionError) as exc:
            self.app.notify(str(exc), severity="error")

    async def action_rename(self) -> None:
        item = await self.query_one(FilesBrowser).selected_for_action()
        if item is not None:
            self.app.push_screen(NameDialog("Rename to", item.name), lambda name: self.rename(item, name))

    def rename(self, item: Path, name: str | None) -> None:
        if name is None or name == item.name:
            return
        try:
            self.reload_directory(fs.rename(item, name))
        except (OSError, fs.FileActionError) as exc:
            self.app.notify(str(exc), severity="error")

    async def action_delete(self) -> None:
        browser = self.query_one(FilesBrowser)
        if not browser.permit_file_action():
            return
        item = browser.selected()
        if item is not None:
            self.app.push_screen(ConfirmDelete(item.name), lambda confirmed: self.delete(item, confirmed))
        elif browser.selection_loading:
            revision = browser.selection_revision
            pending = PendingDelete(partial(browser.wait_for_selection, revision))

            def finish(confirmed: bool | None) -> None:
                target = pending.target
                if not confirmed or target is None:
                    return
                if browser.selection_revision != revision or browser.selected() != target:
                    self.app.notify("Selection changed; delete was cancelled.", severity="warning")
                    return
                self.delete(target, True)

            self.app.push_screen(pending, finish)

    def delete(self, item: Path, confirmed: bool | None) -> None:
        if confirmed:
            try:
                fs.remove(item)
                self.reload_directory()
            except OSError as exc:
                self.app.notify(str(exc), severity="error")

    async def action_copy(self) -> None:
        await self.remember_selection(move=False)

    async def action_cut(self) -> None:
        await self.remember_selection(move=True)

    async def remember_selection(self, *, move: bool) -> None:
        item = await self.query_one(FilesBrowser).selected_for_action()
        if item is not None:
            self.clipboard = fs.Clipboard(item, move)
            self.app.notify(f"{'Cut' if move else 'Copied'}: {item.name}")

    def action_paste(self) -> None:
        if not self.query_one(FilesBrowser).permit_file_action():
            return
        if self.clipboard is None:
            self.app.notify("Copy or cut a file first.", severity="warning")
            return
        try:
            target = fs.paste(self.clipboard, self.directory)
            if self.clipboard.move:
                self.clipboard = None
            self.reload_directory(target)
        except (OSError, shutil.Error, fs.FileActionError) as exc:
            self.app.notify(str(exc), severity="error")

    async def action_edit(self) -> None:
        item = await self.query_one(FilesBrowser).selected_for_action()
        if item is not None:
            self.edit(item)

    def resolve_editor_command(self) -> list[str]:
        """Resolve the editor launch prefix from the current stored preference.

        Reads the preference on every call so an already-running Finder
        picks up changes. The ``environment`` choice preserves the legacy
        ``shlex.split(os.environ.get('EDITOR', 'vi'))`` behavior exactly;
        explicit ``nvim``/``vi`` resolve via the plugin manager (local bin
        first, then ``PATH``) and never fall back silently.
        """
        context = getattr(self, "_plugin_context", None)
        if context is None:
            command = shlex.split(os.environ.get("EDITOR", "vi"))
            if not command:
                raise fs.FileActionError("EDITOR is empty; set it to a terminal editor.")
            return command
        preferences = EditorPreferencesStore(context.manager.store.path.with_name("editor.json")).read()
        match preferences.editor:
            case "environment":
                command = shlex.split(os.environ.get("EDITOR", "vi"))
                if not command:
                    raise fs.FileActionError("EDITOR is empty; set it to a terminal editor.")
                return command
            case "nvim" | "vi":
                binary = context.manager.executable(preferences.editor)
                if binary is None:
                    raise fs.FileActionError(
                        f"Editor '{preferences.editor}' not found; install it or choose the environment editor."
                    )
                return [binary]
            case _:
                assert_never(preferences.editor)

    def edit(self, item: Path) -> None:
        if not item.is_file():
            self.app.notify("Select a regular file to edit.", severity="warning")
            return
        try:
            command = self.resolve_editor_command()
            file_arg = str(item.absolute()) if not item.is_absolute() else str(item)
            with self.app.suspend():
                result = subprocess.run([*command, file_arg], check=False, env=restored_env())
            if result.returncode:
                self.app.notify(f"Editor exited with status {result.returncode}", severity="error")
            self.reload_directory(item)
        except (OSError, ValueError, fs.FileActionError, PluginError) as exc:
            self.app.notify(f"Cannot start editor: {exc}", severity="error", markup=False)
