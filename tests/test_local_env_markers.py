"""Bootstrap marker validation for the shared process PATH."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def _isolated_os_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    saved = dict(os.environ)
    for marker in (
        "TDNEXT_PATH_BOOTSTRAPPED",
        "TDNEXT_ORIGINAL_PATH",
        "TDNEXT_ORIGINAL_PATH_PRESENT",
    ):
        monkeypatch.delenv(marker, raising=False)
    yield
    os.environ.clear()
    os.environ.update(saved)


def test_bootstrap_recaptures_marker_only_forgery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_os_env
) -> None:
    from tdnext.core.local_env import bootstrap, is_bootstrapped, restored_env

    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TDNEXT_PATH_BOOTSTRAPPED", "1")
    assert not is_bootstrapped()
    bin_dir = bootstrap()
    assert os.environ["PATH"] == f"{bin_dir}{os.pathsep}/usr/bin:/bin"
    assert is_bootstrapped()
    assert restored_env()["PATH"] == "/usr/bin:/bin"


def test_bootstrap_recaptures_missing_original_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_os_env
) -> None:
    from tdnext.core.local_env import bootstrap, is_bootstrapped, restored_env

    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TDNEXT_PATH_BOOTSTRAPPED", "1")
    monkeypatch.setenv("TDNEXT_ORIGINAL_PATH_PRESENT", "1")
    assert not is_bootstrapped()
    bin_dir = bootstrap()
    assert os.environ["PATH"] == f"{bin_dir}{os.pathsep}/usr/bin:/bin"
    assert restored_env()["PATH"] == "/usr/bin:/bin"


def test_bootstrap_recaptures_invalid_present_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_os_env
) -> None:
    from tdnext.core.local_env import bootstrap, is_bootstrapped, restored_env

    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TDNEXT_PATH_BOOTSTRAPPED", "1")
    monkeypatch.setenv("TDNEXT_ORIGINAL_PATH_PRESENT", "yes")
    monkeypatch.setenv("TDNEXT_ORIGINAL_PATH", "/usr/bin:/bin")
    assert not is_bootstrapped()
    bin_dir = bootstrap()
    assert os.environ["PATH"] == f"{bin_dir}{os.pathsep}/usr/bin:/bin"
    assert restored_env()["PATH"] == "/usr/bin:/bin"


def test_bootstrap_is_idempotent_for_valid_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_os_env
) -> None:
    from tdnext.core.local_env import bootstrap, restored_env

    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    first = bootstrap()
    second = bootstrap()
    assert first == second
    assert os.environ["PATH"] == f"{first}{os.pathsep}/usr/bin:/bin"
    assert restored_env()["PATH"] == "/usr/bin:/bin"


def test_absent_path_uses_defpath_and_restores_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_os_env
) -> None:
    from tdnext.core.local_env import bootstrap, is_bootstrapped, restored_env

    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    monkeypatch.delenv("PATH", raising=False)
    bin_dir = bootstrap()
    assert os.environ["PATH"] == f"{bin_dir}{os.pathsep}{os.defpath}"
    assert is_bootstrapped()
    assert "PATH" not in restored_env()
