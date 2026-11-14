"""Filesystem operations used by the file manager; no shell commands are involved."""

import contextlib
import ctypes
import errno
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


@dataclass(frozen=True, slots=True)
class FileActionError(Exception):
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class Clipboard:
    path: Path
    move: bool


def entries(directory: Path, *, hidden: bool) -> list[Path]:
    items = (item for item in directory.iterdir() if hidden or not item.name.startswith("."))
    return sorted(items, key=lambda item: (not item.is_dir(), item.name.casefold(), item.name))


def child_path(directory: Path, name: str) -> Path:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise FileActionError("Enter one non-empty name, without a slash.")
    target = directory / name
    if os.path.lexists(target):
        raise FileActionError(f"Already exists: {target.name}")
    return target


_AT_FDCWD = -100
_RENAME_NOREPLACE = 0x1


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically rename without replacing an existing destination.

    Uses renameat2(RENAME_NOREPLACE) on Linux so a destination created
    between the existence check and the rename fails with EEXIST instead
    of being overwritten. Fails safely (no overwrite) when the atomic
    mechanism is unavailable; callers must not fall back to plain rename.
    Raises FileActionError if the destination exists, OSError otherwise.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except OSError as exc:
        raise FileActionError(f"Atomic rename is not available: {exc}") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    ret = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if ret == 0:
        return
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileActionError(f"Already exists: {target.name}")
    if err in (errno.ENOSYS, errno.EINVAL):
        # Kernel/libc has no RENAME_NOREPLACE: fail instead of overwriting.
        raise FileActionError(f"Atomic rename is not supported here: {os.strerror(err)}")
    raise OSError(err, os.strerror(err), str(source))


def _copy_symlink_noreplace(source: Path, target: Path) -> None:
    """Atomically recreate a symlink; fails if the destination exists."""
    try:
        os.symlink(os.readlink(source), target)
    except FileExistsError as exc:
        raise FileActionError(f"Already exists: {target.name}") from exc


class _ExclusiveOutput:
    """Writable stream for an exclusively created target; removes it on failure."""

    def __init__(self, target: Path, fd: int) -> None:
        self._target = target
        self._fd = fd
        self._stream: BinaryIO | None = None

    def __enter__(self) -> BinaryIO:
        stream = os.fdopen(self._fd, "wb")
        self._stream = stream
        return stream

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()
        if exc_type is not None:
            with contextlib.suppress(OSError):
                os.unlink(self._target)


class _RegularSourceStream:
    """Readable stream for an open fd, validated as a regular file first."""

    def __init__(self, fd: int, source: Path) -> None:
        self._fd = fd
        self._source = source
        self._stream: BinaryIO | None = None

    def __enter__(self) -> BinaryIO:
        try:
            live = os.fstat(self._fd)
        except OSError as exc:
            os.close(self._fd)
            raise FileActionError(f"Cannot copy source: {self._source.name}") from exc
        if not stat.S_ISREG(live.st_mode):
            os.close(self._fd)
            raise FileActionError(f"Cannot copy special file: {self._source.name}")
        stream = os.fdopen(self._fd, "rb")
        self._stream = stream
        return stream

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()


def _copy_file_noreplace(source: Path, target: Path) -> None:
    """Copy a single file/symlink to a path that must not exist, atomically.

    Special files (FIFOs, sockets, devices, directories) are refused with
    FileActionError instead of being opened: opening a FIFO for reading
    would block the UI forever. The source is opened with O_NONBLOCK and
    O_NOFOLLOW and re-validated with fstat, so a rival swapping a regular
    source for a FIFO (or symlink) between the checks cannot cause a hang
    or an overwrite. A partially copied destination is unlinked on failure.
    """
    try:
        initial = os.lstat(source)
    except FileNotFoundError as exc:
        raise FileActionError(f"Source no longer exists: {source.name}") from exc
    if stat.S_ISLNK(initial.st_mode):
        _copy_symlink_noreplace(source, target)
        return
    if not stat.S_ISREG(initial.st_mode):
        raise FileActionError(f"Cannot copy special file: {source.name}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        src_fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | nofollow)
    except FileNotFoundError as exc:
        raise FileActionError(f"Source no longer exists: {source.name}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _copy_symlink_noreplace(source, target)
            return
        if exc.errno == errno.ENXIO:
            raise FileActionError(f"Cannot copy special file: {source.name}") from exc
        raise
    with _RegularSourceStream(src_fd, source) as incoming:
        try:
            dest_fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError as exc:
            raise FileActionError(f"Already exists: {target.name}") from exc
        with _ExclusiveOutput(target, dest_fd) as out:
            shutil.copyfileobj(incoming, out, length=1024 * 1024)
    with contextlib.suppress(OSError):
        shutil.copystat(source, target, follow_symlinks=False)


def _ensure_tree_copyable(source: Path) -> None:
    """Refuse trees containing special files before copying or moving them."""
    stack = [source]
    while stack:
        current = stack.pop()
        try:
            entry_stat = os.lstat(current)
        except FileNotFoundError as exc:
            raise FileActionError(f"Source no longer exists: {source.name}") from exc
        mode = entry_stat.st_mode
        if stat.S_ISLNK(mode) or stat.S_ISREG(mode):
            continue
        if stat.S_ISDIR(mode):
            try:
                with os.scandir(current) as scanner:
                    names = sorted(entry.name for entry in scanner)
            except (FileNotFoundError, NotADirectoryError) as exc:
                raise FileActionError(f"Source no longer exists: {source.name}") from exc
            stack.extend(current / name for name in names)
            continue
        raise FileActionError(f"Cannot copy special file: {current.name}")


def _copy_tree_entry(src: Path, dst: Path) -> None:
    """Copy one tree entry without following symlinks or opening FIFOs."""
    try:
        entry_stat = os.lstat(src)
    except FileNotFoundError as exc:
        raise FileActionError(f"Source no longer exists: {src.name}") from exc
    mode = entry_stat.st_mode
    if stat.S_ISLNK(mode):
        _copy_symlink_noreplace(src, dst)
    elif stat.S_ISDIR(mode):
        try:
            os.mkdir(dst)
        except FileExistsError as exc:
            raise FileActionError(f"Already exists: {dst.name}") from exc
        try:
            with os.scandir(src) as scanner:
                names = sorted(entry.name for entry in scanner)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise FileActionError(f"Source no longer exists: {src.name}") from exc
        for name in names:
            _copy_tree_entry(src / name, dst / name)
        with contextlib.suppress(OSError):
            shutil.copystat(src, dst, follow_symlinks=False)
    elif stat.S_ISREG(mode):
        _copy_file_noreplace(src, dst)
    else:
        raise FileActionError(f"Cannot copy special file: {src.name}")


def _copy_dir_noreplace(source: Path, target: Path) -> None:
    """Copy a directory tree; fails if the destination exists (atomic mkdir)."""
    _ensure_tree_copyable(source)
    try:
        os.mkdir(target)
    except FileExistsError as exc:
        raise FileActionError(f"Already exists: {target.name}") from exc
    try:
        with os.scandir(source) as scanner:
            names = sorted(entry.name for entry in scanner)
        for name in names:
            _copy_tree_entry(source / name, target / name)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _move_noreplace(source: Path, target: Path) -> None:
    """Move without ever replacing an existing destination.

    Same-filesystem moves use renameat2(RENAME_NOREPLACE). Cross-device
    moves fall back to exclusive copy + delete so the destination is
    still never overwritten; the source is removed only after a
    successful copy.
    """
    if source.is_dir() and not source.is_symlink():
        _ensure_tree_copyable(source)
    try:
        _rename_noreplace(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        if source.is_dir() and not source.is_symlink():
            _copy_dir_noreplace(source, target)
            shutil.rmtree(source)
        else:
            _copy_file_noreplace(source, target)
            if source.is_dir() and not source.is_symlink():
                shutil.rmtree(source)
            else:
                source.unlink()
    return None


def create(directory: Path, name: str, *, folder: bool) -> Path:
    target = child_path(directory, name)
    try:
        if folder:
            target.mkdir()
        else:
            # O_EXCL fails atomically if a rival created the path (file,
            # directory, or symlink) after child_path checked.
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            os.close(fd)
    except FileExistsError as exc:
        raise FileActionError(f"Already exists: {target.name}") from exc
    return target


def rename(source: Path, name: str) -> Path:
    target = child_path(source.parent, name)
    _rename_noreplace(source, target)
    return target


def remove(source: Path) -> None:
    if source.is_dir() and not source.is_symlink():
        shutil.rmtree(source)
    else:
        source.unlink()


def paste(clipboard: Clipboard, directory: Path) -> Path:
    source = clipboard.path
    target = child_path(directory, source.name)
    if source.is_dir() and not source.is_symlink() and directory.resolve().is_relative_to(source.resolve()):
        raise FileActionError("Cannot copy or move a directory into itself.")
    if clipboard.move:
        _move_noreplace(source, target)
    elif source.is_dir() and not source.is_symlink():
        _copy_dir_noreplace(source, target)
    else:
        _copy_file_noreplace(source, target)
    return target
