from pathlib import Path

from native_environment import native_env as native_env
from native_session import NativeSession, wait_for


def test_existing_stock_session_survives_new_core_session(
    tmp_path: Path, native_env: tuple[dict[str, str], str]
) -> None:
    """Two independent sessions on one socket keep separate return tabs.

    Both sessions run the supported Rust desktop and patched core; the old
    stock-vs-core flag distinction is gone. What survives is isolation:
    each session navigates Settings open/back independently and the first
    session's btop survives.
    """
    env, stock = native_env
    original = NativeSession(tmp_path, env, stock)
    try:
        monitor = wait_for(original.btop_pid)
        newer = NativeSession(tmp_path, env, stock)
        try:
            assert original.name != newer.name
            newer.open_settings()
            newer.back_to("Files")
            original.open_settings()
            original.back_to("Files")
            assert original.btop_pid() == monitor
        finally:
            newer.close()
    finally:
        original.close()
