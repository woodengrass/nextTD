"""Process PATH contract via real isolated child entry points."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from local_path_fixtures import (
    MARKERS,
    PY,
    TDNEXT_BIN,
    base_env,
    fake_btop,
    probe_provider,
    probe_token,
    probe_view,
    run_child,
)


def test_imports_leave_the_process_environment_identical(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    env = base_env(tmp_path, state, path_state="normal")
    code = (
        "import os\n"
        "before = dict(os.environ)\n"
        "import tdnext.cli\n"
        "import tdnext.pane\n"
        "import tdnext.plugins.desktop.panes\n"
        "import tdnext.core.views\n"
        "import tdnext.runtime\n"
        "after = dict(os.environ)\n"
        "assert before == after\n"
        "print('IMPORT_PURE_OK')\n"
    )
    result = run_child([PY, "-c", code], env)
    assert result.returncode == 0, result.stderr
    assert "IMPORT_PURE_OK" in result.stdout


@pytest.mark.parametrize("path_state", ["absent", "empty", "normal"])
def test_child_cli_enable_uses_bare_btop_at_import_and_constructor(
    tmp_path: Path, path_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    fake_btop(state)
    marker = tmp_path / "path-marker.txt"
    monkeypatch.setenv("TDNEXT_TEST_MARKER", str(marker))
    probe_view(tmp_path, state)
    parent_path = os.environ.get("PATH", "<unset>")
    monkeypatch.delenv("TDNEXT_TEST_MARKER", raising=False)
    env = base_env(tmp_path, state, path_state=path_state)
    env["TDNEXT_TEST_MARKER"] = str(marker)
    result = run_child([PY, "-m", "tdnext", "enable", "probeview"], env)
    assert result.returncode == 0, result.stderr
    assert "probeview" in result.stdout
    observed = marker.read_text(encoding="utf-8")
    expected_bin = str(state / "bin")
    if path_state == "absent":
        assert observed == f"{expected_bin}{os.pathsep}{os.defpath}"
    else:
        assert observed == expected_bin or observed.startswith(expected_bin + os.pathsep)
    assert "::" not in observed
    assert not observed.split(os.pathsep)[0] == ""
    assert os.environ.get("PATH", "<unset>") == parent_path


@pytest.mark.parametrize("path_state", ["absent", "empty", "normal"])
def test_child_cli_install_and_provider_runtime_use_bare_btop(
    tmp_path: Path, path_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    fake_btop(state)
    marker = tmp_path / "provider-marker.txt"
    resolve_marker = tmp_path / "resolved.txt"
    monkeypatch.delenv("TDNEXT_TEST_MARKER", raising=False)
    probe_provider(tmp_path, state)
    env = base_env(tmp_path, state, path_state=path_state)
    env["TDNEXT_TEST_MARKER"] = str(marker)
    env["TDNEXT_TEST_RESOLVE_MARKER"] = str(resolve_marker)
    install = run_child([PY, "-m", "tdnext", "install", "probeicons"], env)
    assert install.returncode == 0, install.stderr
    enable = run_child([PY, "-m", "tdnext", "enable", "probeicons"], env)
    assert enable.returncode == 0, enable.stderr
    assert resolve_marker.read_text(encoding="utf-8") == "resolved"


def test_public_tdnext_binary_and_panes_main_share_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    fake_btop(state)
    marker = tmp_path / "pane-marker.txt"
    monkeypatch.delenv("TDNEXT_TEST_MARKER", raising=False)
    probe_view(tmp_path, state)
    env = base_env(tmp_path, state, path_state="empty")
    env["TDNEXT_TEST_MARKER"] = str(marker)
    binary = str(TDNEXT_BIN) if TDNEXT_BIN.is_file() else PY
    if binary == PY:
        args = [PY, "-m", "tdnext", "enable", "probeview"]
    else:
        args = [binary, "enable", "probeview"]
    result = run_child(args, env)
    assert result.returncode == 0, result.stderr
    panes_code = (
        "import sys\n"
        "sys.argv = ['tdnext.plugins.desktop.panes', 'tool', '--help']\n"
        "from tdnext.plugins.desktop.panes import main\n"
        "try:\n"
        "    main()\n"
        "except Exception:\n"
        "    pass\n"
        "import subprocess\n"
        "subprocess.run(['btop', '--version'], check=True, capture_output=True)\n"
        "print('PANES_MAIN_OK')\n"
    )
    pane_env = base_env(tmp_path, state, path_state="empty")
    panes = run_child([PY, "-c", panes_code], pane_env)
    assert panes.returncode == 0, panes.stderr
    assert "PANES_MAIN_OK" in panes.stdout


@pytest.mark.parametrize("path_state", ["absent", "empty", "normal"])
def test_native_pane_view_runs_plugin_runtime_by_bare_name(
    tmp_path: Path, path_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    fake_btop(state)
    marker = tmp_path / "run-pane-marker.txt"
    monkeypatch.delenv("TDNEXT_TEST_MARKER", raising=False)
    probe_view(tmp_path, state)
    token = probe_token()
    env = base_env(tmp_path, state, path_state=path_state)
    env["TDNEXT_TEST_MARKER"] = str(marker)
    env["TDNEXT_DIRECTORY"] = str(tmp_path)
    result = run_child([PY, "-m", "tdnext.pane", "view", "probeview", "--snapshot", token], env)
    assert result.returncode == 0, result.stderr
    assert "PANE_VIEW_RUNTIME_OK" in result.stdout
    observed = marker.read_text(encoding="utf-8")
    expected_bin = str(state / "bin")
    if path_state == "absent":
        assert observed == f"{expected_bin}{os.pathsep}{os.defpath}"
    else:
        assert observed == expected_bin or observed.startswith(expected_bin + os.pathsep)


def test_unrelated_parent_and_no_global_config(tmp_path: Path) -> None:
    state = tmp_path / "state"
    fake_btop(state)
    probe_view(tmp_path, state)
    parent_before = dict(os.environ)
    home = tmp_path / "home"
    env = base_env(tmp_path, state, path_state="normal")
    env["TDNEXT_TEST_MARKER"] = str(tmp_path / "m.txt")
    result = run_child([PY, "-m", "tdnext", "enable", "probeview"], env)
    assert result.returncode == 0, result.stderr
    assert os.environ == parent_before
    for rc in (".bashrc", ".profile", ".zshrc", ".zprofile"):
        assert not (home / rc).exists()
    for marker in MARKERS:
        assert os.environ.get(marker) == parent_before.get(marker)


def test_forged_markers_do_not_break_real_cli_enable(tmp_path: Path) -> None:
    state = tmp_path / "state"
    fake_btop(state)
    probe_view(tmp_path, state)
    marker = tmp_path / "forged-marker.txt"
    env = base_env(tmp_path, state, path_state="normal")
    env["TDNEXT_TEST_MARKER"] = str(marker)
    env["TDNEXT_PATH_BOOTSTRAPPED"] = "1"
    result = run_child([PY, "-m", "tdnext", "enable", "probeview"], env)
    assert result.returncode == 0, result.stderr
    observed = marker.read_text(encoding="utf-8")
    assert observed == f"{state / 'bin'}{os.pathsep}/usr/bin:/bin"
