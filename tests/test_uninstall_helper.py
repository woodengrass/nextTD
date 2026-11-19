"""Isolated tests for the name-based uninstall close-then-commit flow.

Close targets are own-uid processes whose Linux comm is exactly ``tdnext``:
the bundled modified Zellij binary is a real file named tdnext, the CLI
console script has comm tdnext, and the fallback desktop claims the name at
launch. Stock Zellij (comm zellij) is never matched. Real signals go only to
child processes this test spawned and owns, addressed by explicit PID, never
by sweeping host /proc; removal tests run against a fake proc tree.
"""

import os
import select
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tdnext.core.discovery import discover
from tdnext.core.errors import PluginError
from tdnext.core.manager import PluginManager
from tdnext.core.models import PluginState
from tdnext.core.state import StateStore
from tdnext.core.uninstall_helper import (
    PROCESS_NAME,
    ProcessDiscoveryError,
    close_by_name,
    close_pids,
    list_tdnext_pids,
    uninstall_with_close,
)


def _write_tdnext_script(path: Path) -> Path:
    import sys as _sys

    path.write_text(
        f"#!{_sys.executable}\n"
        "import sys\n"
        "print('ready', flush=True)\n"
        "sys.stdin.read()\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _spawn_tdnext(tmp_path: Path, name: str = "tdnext") -> subprocess.Popen[str]:
    binary = _write_tdnext_script(tmp_path / name)
    child = subprocess.Popen(
        [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, close_fds=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "ready"
    assert (Path("/proc") / str(child.pid) / "comm").read_text(encoding="utf-8").strip() == PROCESS_NAME
    return child


def test_list_finds_only_exact_comm_in_fake_proc(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid, comm in (("11", "tdnext"), ("12", "tdnextx"), ("13", "zellij"), ("14", "TDNEXT")):
        entry = proc / pid
        entry.mkdir()
        (entry / "comm").write_text(f"{comm}\n", encoding="utf-8")
    (proc / "self").mkdir()

    assert list_tdnext_pids(proc=proc, exclude=frozenset()) == (11,)


def test_list_excludes_given_pids(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid in ("21", "22"):
        entry = proc / pid
        entry.mkdir()
        (entry / "comm").write_text("tdnext\n", encoding="utf-8")

    assert list_tdnext_pids(proc=proc, exclude=frozenset({21})) == (22,)


def test_close_pids_closes_owned_tdnext_and_keeps_sleep(tmp_path: Path) -> None:
    owned = _spawn_tdnext(tmp_path)
    other = subprocess.Popen(["sleep", "60"])
    try:
        close_pids((owned.pid,), proc=Path("/proc"))
        assert owned.wait(timeout=10) is not None
        assert other.poll() is None
    finally:
        try:
            other.kill()
        except OSError:
            pass
        try:
            owned.kill()
        except OSError:
            pass
        other.wait(timeout=10)
        try:
            owned.wait(timeout=5)
        except Exception:
            pass


def test_close_by_name_with_empty_proc_touches_nothing(tmp_path: Path) -> None:
    close_by_name(proc=_empty_proc(tmp_path))


def test_close_by_name_never_matches_stock_zellij() -> None:
    assert PROCESS_NAME == "tdnext"
    assert PROCESS_NAME != "zellij"


def _install_sample(tmp_path: Path) -> tuple[PluginManager, Path]:
    source = tmp_path / "sources" / "sample"
    (source / "sample_plugin").mkdir(parents=True)
    (source / "plugin.toml").write_text(
        'id = "sample"\nname = "Sample"\nversion = "1"\ndescription = "Local test plugin"\n'
        'manifest_version = 1\napi_version = 1\ncapabilities = ["desktop.host"]\n'
        'host_interfaces = ["sample.view.v1"]\nentrypoint = "sample_plugin:Plugin"\n',
        encoding="utf-8",
    )
    (source / "sample_plugin" / "__init__.py").write_text(
        "class Plugin:\n    def run(self, context, views): pass\n", encoding="utf-8",
    )
    state = tmp_path / "state"
    manager = PluginManager(
        discover((tmp_path / "sources",)), StateStore(state / "plugins.json"), state / "bin",
    )
    manager.install("sample")
    manager.enable("sample")
    return manager, state


def _empty_proc(tmp_path: Path) -> Path:
    proc = tmp_path / "proc"
    proc.mkdir(exist_ok=True)
    return proc


def test_uninstall_with_close_commits_removal_on_empty_plan(tmp_path: Path) -> None:
    manager, state = _install_sample(tmp_path)
    settings = state / "sample-settings.json"
    settings.write_text('{"keep": true}', encoding="utf-8")

    affected = uninstall_with_close(state=state, plugin_id="sample", proc=_empty_proc(tmp_path))

    assert affected == frozenset({"sample"})
    assert manager.store.read() == PluginState()
    assert not manager.code_store.slot("sample").exists()
    assert settings.read_text(encoding="utf-8") == '{"keep": true}'


def test_uninstall_with_close_removes_legacy_bundled_plugin(tmp_path: Path) -> None:
    state = tmp_path / "state"
    store = StateStore(state / "plugins.json")
    store.write(PluginState(installed=frozenset({"monitor"}), enabled=frozenset({"monitor"})))
    settings = state / "monitor-settings.json"
    settings.write_text("keep", encoding="utf-8")

    assert uninstall_with_close(state=state, plugin_id="monitor", proc=_empty_proc(tmp_path)) == frozenset({"monitor"})
    assert store.read() == PluginState()
    assert settings.read_text(encoding="utf-8") == "keep"
    assert not (state / "plugin-code" / "monitor").exists()


def test_unknown_plugin_runs_no_close(tmp_path: Path) -> None:
    state = tmp_path / "state"
    manager = PluginManager(
        discover((tmp_path / "sources",)), StateStore(state / "plugins.json"), state / "bin",
    )
    before = manager.store.read()

    with pytest.raises(PluginError, match="Plugin not found"):
        uninstall_with_close(state=state, plugin_id="missing", proc=_empty_proc(tmp_path))

    assert manager.store.read() == before


def test_close_failure_aborts_before_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, state = _install_sample(tmp_path)

    def fail_close(*, proc: Path) -> None:
        raise PluginError("close failed")

    monkeypatch.setattr("tdnext.core.uninstall_helper.close_by_name", fail_close)
    with pytest.raises(PluginError, match="close failed"):
        uninstall_with_close(state=state, plugin_id="sample", proc=_empty_proc(tmp_path))

    assert manager.store.read().installed == frozenset({"sample"})
    assert manager.code_store.slot("sample").is_dir()


def test_proc_enumeration_failure_raises_instead_of_empty(tmp_path: Path) -> None:
    with pytest.raises(ProcessDiscoveryError):
        list_tdnext_pids(proc=tmp_path / "missing")


def test_uninstall_with_unreadable_proc_aborts_before_commit(tmp_path: Path) -> None:
    manager, state = _install_sample(tmp_path)

    with pytest.raises(ProcessDiscoveryError):
        uninstall_with_close(state=state, plugin_id="sample", proc=tmp_path / "missing")

    assert manager.store.read().installed == frozenset({"sample"})
    assert manager.code_store.slot("sample").is_dir()


def _await_ready(child: "subprocess.Popen[str]", *, timeout: float = 5.0) -> None:
    assert child.stdout is not None
    assert select.select([child.stdout], [], [], timeout)[0]
    assert child.stdout.readline().strip() == "ready"


def test_python_minus_m_claims_tdnext_comm() -> None:
    code = (
        "import sys; from tdnext.core.process_name import set_process_name; "
        "assert set_process_name(); print('ready', flush=True); sys.stdin.read()"
    )
    with subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        text=True, close_fds=True,
    ) as child:
        try:
            _await_ready(child)
            assert (Path("/proc") / str(child.pid) / "comm").read_text(encoding="utf-8").strip() == PROCESS_NAME
        finally:
            try:
                child.kill()
            except OSError:
                pass


def test_script_invocation_has_tdnext_comm(tmp_path: Path) -> None:
    owned = _spawn_tdnext(tmp_path)
    try:
        assert owned.poll() is None
    finally:
        try:
            owned.kill()
        except OSError:
            pass
        owned.wait(timeout=10)


def test_helper_close_kills_tdnext_parent_and_survives_itself(tmp_path: Path) -> None:
    parent = _spawn_tdnext(tmp_path, name="tdnext")
    code = (
        "import sys; from tdnext.core.uninstall_helper import close_pids; "
        f"close_pids(({parent.pid},)); print('done', flush=True)"
    )
    with subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE,
        text=True, close_fds=True,
    ) as helper:
        try:
            assert helper.stdout is not None
            assert select.select([helper.stdout], [], [], 15)[0]
            assert helper.stdout.readline().strip() == "done"
            assert helper.wait(timeout=10) == 0
        finally:
            try:
                helper.kill()
            except OSError:
                pass
    assert parent.wait(timeout=10) is not None
    assert parent.returncode == -signal.SIGTERM


def _modified_zellij_candidate() -> Path | None:
    from_env = os.environ.get("TDNEXT_CORE_BINARY", "").strip()
    if from_env:
        candidate = Path(from_env)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        return None
    root = Path(__file__).parents[1] / "state" / "bin"
    for name in ("zellij-core-navigation-v3", "zellij"):
        candidate = root / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def test_real_tdnext_binary_server_reports_tdnext_comm(tmp_path: Path) -> None:
    if not shutil.which("tmux"):
        pytest.skip("tmux is required for the isolated server comm check")
    source = _modified_zellij_candidate()
    if source is None:
        pytest.skip("no modified Zellij candidate binary available")
    binary = tmp_path / "tdnext"
    try:
        os.link(source, binary)
    except OSError:
        shutil.copy2(source, binary)
    binary.chmod(0o755)
    tag = f"comm-{uuid.uuid4().hex}"
    session = f"s-{uuid.uuid4().hex[:7]}"
    socket = tmp_path / "tmux.sock"
    data_dir = tmp_path / "zellij-data"
    # Short isolated socket dir: zellij socket paths must stay under the
    # 108-byte sun_path limit, which tmp_path-based dirs can exceed.
    with TemporaryDirectory(prefix="tdn-") as sockroot:
        socket_dir = Path(sockroot)
        env = {
            **os.environ, "TERM": "xterm-256color", "ZELLIJ_SOCKET_DIR": str(socket_dir),
            "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        }
        marker = f"TDNEXT_TEST_SESSION={tag}".encode()
        subprocess.run(
            ["tmux", "-S", str(socket), "-f", "/dev/null", "new-session", "-d", "-s", tag,
             "-x", "120", "-y", "36", "-e", f"TDNEXT_TEST_SESSION={tag}",
             str(binary), "--data-dir", str(data_dir), "-s", session],
            env=env, capture_output=True, check=True, timeout=10,
        )
        try:
            deadline = time.monotonic() + 20
            found = False
            while time.monotonic() < deadline:
                for entry in Path("/proc").iterdir():
                    if not entry.name.isdigit():
                        continue
                    try:
                        if marker not in (entry / "environ").read_bytes().split(b"\0"):
                            continue
                        if (entry / "comm").read_text(encoding="utf-8").strip() != PROCESS_NAME:
                            continue
                        if os.readlink(entry / "exe") != str(binary):
                            continue
                    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
                        continue
                    found = True
                    break
                if found:
                    break
                time.sleep(0.1)
            assert found, "no tagged process runs the real tdnext binary with comm tdnext"
        finally:
            subprocess.run(
                ["tmux", "-S", str(socket), "kill-session", "-t", tag],
                env=env, capture_output=True, check=False, timeout=10,
            )
            deadline = time.monotonic() + 10
            leftovers: list[Path] = []
            while time.monotonic() < deadline:
                leftovers = []
                try:
                    entries = tuple(Path("/proc").iterdir())
                except OSError:
                    break
                for entry in entries:
                    if not entry.name.isdigit():
                        continue
                    try:
                        if marker in (entry / "environ").read_bytes().split(b"\0"):
                            leftovers.append(entry)
                    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
                        continue
                if not leftovers:
                    break
                time.sleep(0.1)
            for entry in leftovers:
                try:
                    os.kill(int(entry.name), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    continue
