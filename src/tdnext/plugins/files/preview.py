"""Bounded, non-following previews of real filesystem entries."""

import codecs
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

MAX_BYTES: Final = 64 * 1024
MAX_LINES: Final = 300
MAX_LINE_CHARS: Final = 512


@dataclass(frozen=True, slots=True)
class FilePreview:
    path: Path
    kind: str
    size: int | None = None
    modified: float | None = None
    lines: tuple[str, ...] = ()
    message: str = ""
    truncated: bool = False


def read_preview(path: Path) -> FilePreview:
    """Read at most MAX_BYTES from a verified regular file; never follow links."""
    try:
        initial = path.lstat()
        if stat.S_ISLNK(initial.st_mode):
            return FilePreview(path, "Link", initial.st_size, initial.st_mtime, message=f"Link to {os.readlink(path)}")
        if stat.S_ISDIR(initial.st_mode):
            return FilePreview(path, "Folder", modified=initial.st_mtime, message="Press Enter to open this folder.")
        if not stat.S_ISREG(initial.st_mode):
            return FilePreview(path, "Special file", modified=initial.st_mtime, message="Preview unavailable.")

        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            return FilePreview(path, "File", message="Safe preview is unavailable on this platform.")
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | nofollow)
        with os.fdopen(fd, "rb", buffering=0) as stream:
            live = os.fstat(stream.fileno())
            if not stat.S_ISREG(live.st_mode) or (initial.st_dev, initial.st_ino) != (live.st_dev, live.st_ino):
                return FilePreview(path, "Unavailable", message="File changed while opening; refresh to try again.")
            data = stream.read(MAX_BYTES + 1)

        truncated = len(data) > MAX_BYTES
        decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="strict")
        try:
            text = decoder.decode(data[:MAX_BYTES], final=not truncated)
        except UnicodeDecodeError:
            return FilePreview(path, "Binary file", live.st_size, live.st_mtime, message="Text preview unavailable.")
        if any((ord(char) < 32 and char not in "\t\r\n") or 0x7F <= ord(char) < 0xA0 for char in text):
            return FilePreview(path, "Binary file", live.st_size, live.st_mtime, message="Text preview unavailable.")

        source_lines = text.splitlines()
        truncated = truncated or len(source_lines) > MAX_LINES
        lines: list[str] = []
        for line in source_lines[:MAX_LINES]:
            expanded = line[: MAX_LINE_CHARS + 1].expandtabs(4)
            if len(line) > MAX_LINE_CHARS or len(expanded) > MAX_LINE_CHARS:
                expanded = expanded[: MAX_LINE_CHARS - 1] + "…"
                truncated = True
            lines.append(expanded)
        return FilePreview(
            path, "Text file", live.st_size, live.st_mtime, tuple(lines),
            "Empty file." if not lines else "", truncated,
        )
    except OSError as exc:
        return FilePreview(path, "Unavailable", message=f"Cannot preview: {exc}")
