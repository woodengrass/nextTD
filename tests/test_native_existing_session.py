import os
from pathlib import Path

import pytest
from native_environment import native_env as native_env
from native_processes import tagged_processes
from native_session import NativeSession, wait_for


def test_existing_stock_session_survives_new_core_session(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    candidate = os.environ.get("TDNEXT_CORE_BINARY")
    if not candidate:
        pytest.skip("TDNEXT_CORE_BINARY is not set")
    binary = Path(candidate)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        pytest.fail("TDNEXT_CORE_BINARY is not an executable file")

    env, stock = native_env
    original = NativeSession(tmp_path, env, stock)
    try:
        monitor = wait_for(original.btop_pid)
        local_bin = Path(env["TDNEXT_STATE_DIR"]) / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        (local_bin / "zellij-core-navigation-v2").symlink_to(binary)
        newer = NativeSession(tmp_path, env, str(binary))
        try:
            assert original.name != newer.name
            assert not any(
                b"TDNEXT_CORE_NAVIGATION=1" in variables
                for _, variables, _ in tagged_processes(original.tmux_name)
            )
            assert any(
                b"TDNEXT_CORE_NAVIGATION=1" in variables
                for _, variables, _ in tagged_processes(newer.tmux_name)
            )
            newer.open_settings()
            newer.back_to("Files")
            original.open_settings()
            original.back_to("Files")
            assert original.btop_pid() == monitor
        finally:
            newer.close()
    finally:
        original.close()
