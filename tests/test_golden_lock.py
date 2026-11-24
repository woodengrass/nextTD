"""Targeted guard tests for the frozen golden lock (CI-independent, tmp dirs only)."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import golden_desktop

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK = REPO_ROOT / "tests" / "golden.lock.json"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden"


def _load_checker():  # scripts/check-golden-lock.py has a hyphen; load by path.
    spec = importlib.util.spec_from_file_location(
        "check_golden_lock", REPO_ROOT / "scripts" / "check-golden-lock.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_golden(tmp_path: Path) -> Path:
    fixture = tmp_path / "golden"
    shutil.copytree(GOLDEN_DIR, fixture)
    return fixture


def test_exact_frozen_set_verifies() -> None:
    assert _load_checker().verify(LOCK, GOLDEN_DIR) == []


def test_lock_pins_reference_provenance() -> None:
    provenance = json.loads(LOCK.read_text(encoding="utf-8"))["provenance"]
    assert provenance["source_sha256"] == golden_desktop.SOURCE_SHA
    assert provenance["core_sha256"] == golden_desktop.CORE_SHA
    assert "22c965e" in provenance["note"]


def test_rejects_trailing_blank_modification(tmp_path: Path) -> None:
    fixture = _copy_golden(tmp_path)
    target = fixture / "files_120x38.screen"
    data = target.read_bytes()
    target.write_bytes(data[:-1] + (b" " if data.endswith(b"\n") else b"\n"))
    problems = _load_checker().verify(LOCK, fixture)
    assert any(p.startswith("modified: files_120x38.screen") for p in problems)


def test_rejects_omission(tmp_path: Path) -> None:
    fixture = _copy_golden(tmp_path)
    (fixture / "shell_120x38.screen").unlink()
    problems = _load_checker().verify(LOCK, fixture)
    assert "missing: shell_120x38.screen" in problems


def test_rejects_rename_as_missing_plus_extra(tmp_path: Path) -> None:
    fixture = _copy_golden(tmp_path)
    (fixture / "shell_120x38.screen").rename(fixture / "shell_renamed.screen")
    problems = _load_checker().verify(LOCK, fixture)
    assert "missing: shell_120x38.screen" in problems
    assert "extra: shell_renamed.screen" in problems


def test_rejects_extra_file(tmp_path: Path) -> None:
    fixture = _copy_golden(tmp_path)
    (fixture / "tenth.screen").write_bytes(b"extra")
    assert _load_checker().verify(LOCK, fixture) == ["extra: tenth.screen"]
