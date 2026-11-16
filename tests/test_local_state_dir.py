"""Production-installed state directory selection.

Checkout behavior is unchanged (``<root>/state``), the explicit
``TDNEXT_STATE_DIR`` override keeps top priority, and a regular installed
wheel falls back to a user-writable XDG data location without touching
interpreter directories. Resolution alone never creates directories.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import tdnext.core.local_env as local_env
from tdnext.core.local_env import _is_project_checkout, resolve_state_dir


@pytest.fixture
def _clean_state_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.delenv("TDNEXT_STATE_DIR", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return tmp_path


def _point_at(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_env, "project_root", lambda: root)


def test_explicit_argument_beats_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_state_env: Path
) -> None:
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(tmp_path / "from-env"))
    assert resolve_state_dir(tmp_path / "from-arg") == tmp_path / "from-arg"


def test_env_override_beats_checkout_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_state_env: Path
) -> None:
    _point_at(local_env.project_root(), monkeypatch)
    wanted = tmp_path / "vm-state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(wanted))
    assert resolve_state_dir() == wanted


def test_checkout_default_preserved(
    monkeypatch: pytest.MonkeyPatch, _clean_state_env: Path
) -> None:
    root = local_env.project_root()
    assert _is_project_checkout(root)
    assert resolve_state_dir() == root / "state"


def test_single_marker_is_not_a_checkout(tmp_path: Path) -> None:
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    (lonely / "pyproject.toml").write_text('[project]\nname = "terminal-desktop-next"\n', encoding="utf-8")
    assert not _is_project_checkout(lonely)
    wrong_name = tmp_path / "wrong-name"
    (wrong_name / "src" / "tdnext").mkdir(parents=True)
    (wrong_name / "pyproject.toml").write_text('[project]\nname = "something-else"\n', encoding="utf-8")
    assert not _is_project_checkout(wrong_name)
    no_packaging = tmp_path / "no-packaging"
    (no_packaging / "src" / "tdnext").mkdir(parents=True)
    assert not _is_project_checkout(no_packaging)


def test_installed_defaults_to_xdg_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_state_env: Path
) -> None:
    plain = tmp_path / "site-packages-shim"
    plain.mkdir()
    _point_at(plain, monkeypatch)
    data_home = tmp_path / "data-home"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    resolved = resolve_state_dir()
    assert resolved == data_home / "tdnext"
    assert not resolved.exists()


@pytest.mark.parametrize("xdg_value", ["", "   ", "relative/path"])
def test_installed_ignores_bad_xdg_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_state_env: Path, xdg_value: str
) -> None:
    plain = tmp_path / "site-packages-shim"
    plain.mkdir()
    _point_at(plain, monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", xdg_value)
    home = Path(os.environ["HOME"])
    assert resolve_state_dir() == home / ".local" / "share" / "tdnext"


def test_installed_falls_back_to_home_local_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clean_state_env: Path
) -> None:
    plain = tmp_path / "site-packages-shim"
    plain.mkdir()
    _point_at(plain, monkeypatch)
    home = Path(os.environ["HOME"])
    resolved = resolve_state_dir()
    assert resolved == home / ".local" / "share" / "tdnext"
    assert not resolved.exists()
    assert not str(resolved).startswith(str(Path(sys.prefix).resolve()) + os.sep)
