"""Filesystem snapshots for the Files browser's background directory worker."""

import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import filesystem as fs


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: Path
    directory: bool
    size: str
    modified: str


@dataclass(frozen=True, slots=True)
class DirectorySnapshot:
    directory: Path
    entries: tuple[FileEntry, ...] = ()
    neighbours: tuple[Path, ...] = ()
    error: str = ""
    neighbour_error: str = ""


def format_size(size: int) -> str:
    exact = f"{size:,} B"
    if len(exact) <= 9:
        return exact
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
        value /= 1024
        if value < 1024:
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    return f"{value:.0f} EiB"


def scan_directory(directory: Path, *, hidden: bool) -> DirectorySnapshot:
    try:
        paths = fs.entries(directory, hidden=hidden)
    except OSError as exc:
        return DirectorySnapshot(directory, error=f"Cannot read directory: {exc}")

    entries: list[FileEntry] = []
    for path in paths:
        try:
            info = path.lstat()
            is_directory = stat.S_ISDIR(info.st_mode) or (stat.S_ISLNK(info.st_mode) and path.is_dir())
            size = "–" if is_directory else format_size(info.st_size)
            modified = datetime.fromtimestamp(info.st_mtime).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            is_directory, size, modified = False, "–", "–"
        entries.append(FileEntry(path, is_directory, size, modified))

    if directory.parent == directory:
        return DirectorySnapshot(directory, tuple(entries), (directory,))
    try:
        neighbours = [path for path in fs.entries(directory.parent, hidden=hidden) if path.is_dir()]
        if directory not in neighbours:
            neighbours.append(directory)
            neighbours.sort(key=lambda path: (path.name.casefold(), path.name))
        return DirectorySnapshot(directory, tuple(entries), tuple(neighbours))
    except OSError as exc:
        return DirectorySnapshot(directory, tuple(entries), (directory,), neighbour_error=str(exc))
