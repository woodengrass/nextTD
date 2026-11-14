import concurrent.futures
import errno
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from tdnext.plugins.files import filesystem as fs


def _hide_existing_rival(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a TOCTOU rival: child_path's check sees nothing there."""
    monkeypatch.setattr(os.path, "lexists", lambda _path: False)


def _run_bounded(action: Callable[[], object], timeout: float = 5.0) -> object:
    """Run action in a worker and fail (not hang) if it blocks beyond timeout."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(action)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        raise AssertionError(f"operation hung for more than {timeout}s") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def test_entries_sort_folders_first_and_hide_dotfiles(tmp_path: Path) -> None:
    (tmp_path / "z-folder").mkdir()
    (tmp_path / "a-file").touch()
    (tmp_path / ".hidden").touch()
    assert [path.name for path in fs.entries(tmp_path, hidden=False)] == ["z-folder", "a-file"]


def test_create_and_rename_refuse_existing_names(tmp_path: Path) -> None:
    source = fs.create(tmp_path, "source", folder=False)
    fs.create(tmp_path, "existing", folder=False)
    with pytest.raises(fs.FileActionError):
        fs.rename(source, "existing")
    assert source.exists()


def test_copy_and_move_real_files(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source = source_dir / "notes.txt"
    source.write_text("real content", encoding="utf-8")
    copied = fs.paste(fs.Clipboard(source, move=False), target_dir)
    assert copied.read_text(encoding="utf-8") == "real content"
    copied.unlink()
    moved = fs.paste(fs.Clipboard(source, move=True), target_dir)
    assert moved.read_text(encoding="utf-8") == "real content"
    assert not source.exists()


def test_copy_directory_into_descendant_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    child = source / "child"
    child.mkdir(parents=True)
    with pytest.raises(fs.FileActionError):
        fs.paste(fs.Clipboard(source, move=False), child)


def test_remove_symlink_preserves_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "contents").touch()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    fs.remove(link)
    assert (target / "contents").exists()


def test_create_race_keeps_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rival = tmp_path / "new.txt"
    rival.write_text("existing", encoding="utf-8")
    _hide_existing_rival(monkeypatch)
    with pytest.raises(fs.FileActionError):
        fs.create(tmp_path, "new.txt", folder=False)
    assert rival.read_text(encoding="utf-8") == "existing"


def test_create_race_keeps_existing_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real.txt"
    real.write_text("existing", encoding="utf-8")
    rival = tmp_path / "new.txt"
    rival.symlink_to(real)
    _hide_existing_rival(monkeypatch)
    with pytest.raises(fs.FileActionError):
        fs.create(tmp_path, "new.txt", folder=False)
    assert rival.is_symlink()
    assert real.read_text(encoding="utf-8") == "existing"


def test_rename_race_keeps_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    rival = tmp_path / "dest.txt"
    rival.write_text("existing", encoding="utf-8")
    _hide_existing_rival(monkeypatch)
    with pytest.raises(fs.FileActionError):
        fs.rename(source, "dest.txt")
    assert rival.read_text(encoding="utf-8") == "existing"
    assert source.read_text(encoding="utf-8") == "source"


def test_rename_race_keeps_existing_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "inner.txt").write_text("source", encoding="utf-8")
    rival = tmp_path / "dest"
    rival.mkdir()
    marker = rival / "keep.txt"
    marker.write_text("existing", encoding="utf-8")
    _hide_existing_rival(monkeypatch)
    with pytest.raises(fs.FileActionError):
        fs.rename(source, "dest")
    assert marker.read_text(encoding="utf-8") == "existing"
    assert (source / "inner.txt").read_text(encoding="utf-8") == "source"


def test_rename_race_keeps_existing_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    real = tmp_path / "real.txt"
    real.write_text("existing", encoding="utf-8")
    rival = tmp_path / "dest.txt"
    rival.symlink_to(real)
    _hide_existing_rival(monkeypatch)
    with pytest.raises(fs.FileActionError):
        fs.rename(source, "dest.txt")
    assert rival.is_symlink()
    assert real.read_text(encoding="utf-8") == "existing"
    assert source.read_text(encoding="utf-8") == "source"


def test_paste_copy_race_keeps_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_dir = tmp_path / "src"
    dest_dir = tmp_path / "dst"
    source_dir.mkdir()
    dest_dir.mkdir()
    source = source_dir / "notes.txt"
    source.write_text("source", encoding="utf-8")
    rival = dest_dir / "notes.txt"
    rival.write_text("existing", encoding="utf-8")
    _hide_existing_rival(monkeypatch)
    with pytest.raises(fs.FileActionError):
        fs.paste(fs.Clipboard(source, move=False), dest_dir)
    assert rival.read_text(encoding="utf-8") == "existing"
    assert source.read_text(encoding="utf-8") == "source"


def test_paste_move_race_keeps_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_dir = tmp_path / "src"
    dest_dir = tmp_path / "dst"
    source_dir.mkdir()
    dest_dir.mkdir()
    source = source_dir / "notes.txt"
    source.write_text("source", encoding="utf-8")
    rival = dest_dir / "notes.txt"
    rival.write_text("existing", encoding="utf-8")
    _hide_existing_rival(monkeypatch)
    with pytest.raises(fs.FileActionError):
        fs.paste(fs.Clipboard(source, move=True), dest_dir)
    assert rival.read_text(encoding="utf-8") == "existing"
    assert source.read_text(encoding="utf-8") == "source"


def test_paste_copy_race_keeps_existing_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "srcdir"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "a.txt").write_text("source", encoding="utf-8")
    dest_dir = tmp_path / "dst"
    dest_dir.mkdir()
    rival = dest_dir / "srcdir"
    rival.mkdir()
    marker = rival / "keep.txt"
    marker.write_text("existing", encoding="utf-8")
    _hide_existing_rival(monkeypatch)
    with pytest.raises(fs.FileActionError):
        fs.paste(fs.Clipboard(source, move=False), dest_dir)
    assert marker.read_text(encoding="utf-8") == "existing"
    assert (source / "sub" / "a.txt").read_text(encoding="utf-8") == "source"


def test_paste_move_race_keeps_existing_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "srcdir"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "a.txt").write_text("source", encoding="utf-8")
    dest_dir = tmp_path / "dst"
    dest_dir.mkdir()
    rival = dest_dir / "srcdir"
    rival.mkdir()
    marker = rival / "keep.txt"
    marker.write_text("existing", encoding="utf-8")
    _hide_existing_rival(monkeypatch)
    with pytest.raises(fs.FileActionError):
        fs.paste(fs.Clipboard(source, move=True), dest_dir)
    assert marker.read_text(encoding="utf-8") == "existing"
    assert (source / "sub" / "a.txt").read_text(encoding="utf-8") == "source"


def test_paste_copy_race_keeps_existing_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "link"
    real = tmp_path / "real.txt"
    real.write_text("source", encoding="utf-8")
    source.symlink_to(real)
    dest_dir = tmp_path / "dst"
    dest_dir.mkdir()
    rival = dest_dir / "link"
    rival.write_text("existing", encoding="utf-8")
    _hide_existing_rival(monkeypatch)
    with pytest.raises(fs.FileActionError):
        fs.paste(fs.Clipboard(source, move=False), dest_dir)
    assert rival.read_text(encoding="utf-8") == "existing"
    assert source.is_symlink()


def test_paste_copy_fifo_fails_fast_leaves_no_target(tmp_path: Path) -> None:
    source = tmp_path / "pipe"
    os.mkfifo(source)
    dest_dir = tmp_path / "dst"
    dest_dir.mkdir()
    with pytest.raises(fs.FileActionError):
        _run_bounded(lambda: fs.paste(fs.Clipboard(source, move=False), dest_dir))
    assert not os.path.lexists(dest_dir / "pipe")
    assert stat.S_ISFIFO(os.lstat(source).st_mode)


def test_paste_copy_race_source_swapped_to_fifo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("source", encoding="utf-8")
    dest_dir = tmp_path / "dst"
    dest_dir.mkdir()
    real_open = os.open
    swapped = {"done": False}

    def swap_to_fifo(path: str | os.PathLike[str], flags: int, *args: int) -> int:
        if not swapped["done"] and os.fspath(path) == os.fspath(source) and not (flags & os.O_CREAT):
            swapped["done"] = True
            os.unlink(source)
            os.mkfifo(source)
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", swap_to_fifo)
    with pytest.raises(fs.FileActionError):
        _run_bounded(lambda: fs.paste(fs.Clipboard(source, move=False), dest_dir))
    assert swapped["done"]
    assert not os.path.lexists(dest_dir / "notes.txt")
    assert stat.S_ISFIFO(os.lstat(source).st_mode)


def test_paste_copy_directory_with_fifo_fails_safe(tmp_path: Path) -> None:
    source = tmp_path / "srcdir"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "ok.txt").write_text("source", encoding="utf-8")
    os.mkfifo(source / "sub" / "pipe")
    dest_dir = tmp_path / "dst"
    dest_dir.mkdir()
    with pytest.raises(fs.FileActionError):
        _run_bounded(lambda: fs.paste(fs.Clipboard(source, move=False), dest_dir))
    assert not os.path.lexists(dest_dir / "srcdir")
    assert (source / "sub" / "ok.txt").read_text(encoding="utf-8") == "source"
    assert stat.S_ISFIFO(os.lstat(source / "sub" / "pipe").st_mode)


def test_paste_move_directory_with_fifo_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "srcdir"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "ok.txt").write_text("source", encoding="utf-8")
    os.mkfifo(source / "sub" / "pipe")
    dest_dir = tmp_path / "dst"
    dest_dir.mkdir()
    with pytest.raises(fs.FileActionError):
        _run_bounded(lambda: fs.paste(fs.Clipboard(source, move=True), dest_dir))
    assert not os.path.lexists(dest_dir / "srcdir")
    assert (source / "sub" / "ok.txt").read_text(encoding="utf-8") == "source"
    assert stat.S_ISFIFO(os.lstat(source / "sub" / "pipe").st_mode)


def test_paste_move_fifo_cross_device_fails_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "pipe"
    os.mkfifo(source)
    dest_dir = tmp_path / "dst"
    dest_dir.mkdir()

    def force_cross_device(src: Path, _dst: Path) -> None:
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), str(src))

    monkeypatch.setattr(fs, "_rename_noreplace", force_cross_device)
    with pytest.raises(fs.FileActionError):
        _run_bounded(lambda: fs.paste(fs.Clipboard(source, move=True), dest_dir))
    assert not os.path.lexists(dest_dir / "pipe")
    assert stat.S_ISFIFO(os.lstat(source).st_mode)
