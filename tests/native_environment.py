import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tdnext.core.models import PluginState
from tdnext.core.state import StateStore

ROOT = Path(__file__).parents[1]


def executable(name: str) -> str | None:
    local = ROOT / "state" / "bin" / name
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which(name)


def resolve_core() -> str:
    candidate = os.environ.get("TDNEXT_CORE_BINARY") or str(ROOT / "native-desktop/target/release/tdnext-core")
    source = Path(candidate)
    if source.is_file() and os.access(source, os.X_OK):
        return str(source)
    raise pytest.skip.Exception(f"native test requires a patched core binary at {candidate}")


def resolve_desktop() -> str:
    desktop = ROOT / "native-desktop" / "target" / "release" / "tdnext-desktop"
    if desktop.is_file() and os.access(desktop, os.X_OK):
        return str(desktop)
    raise pytest.skip.Exception(f"native test requires the built desktop at {desktop}")


@pytest.fixture
def native_env(tmp_path: Path) -> Iterator[tuple[dict[str, str], str]]:
    core = resolve_core()
    desktop = resolve_desktop()
    python = sys.executable
    if not shutil.which("tmux"):
        pytest.skip("native test requires tmux")
    if shutil.which("btop") is None:
        pytest.skip("native test requires btop")
    version = subprocess.run([core, "--tdnext-chrome-version"], capture_output=True, text=True, check=True, timeout=5)
    assert version.stdout.strip() == "1", f"incompatible native chrome: {core}"
    state = tmp_path / "state"
    ids = frozenset({"desktop", "files", "monitor", "settings"})
    StateStore(state / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    excluded = {"ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"}
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    with TemporaryDirectory(prefix="tdn-") as socket:
        env.update({
            "TERM": "xterm-256color", "TDNEXT_STATE_DIR": str(state), "ZELLIJ_SOCKET_DIR": socket,
            "TDNEXT_TEST_TMUX_SOCKET": str(Path(socket) / "tmux.sock"),
            "TDNEXT_CORE_BINARY": core, "TDNEXT_DESKTOP_BINARY": desktop, "TDNEXT_PYTHON": python,
            "PYTHONPATH": str(ROOT / "src"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "PATH": f"{Path(core).parent}:{Path(python).parent}:{Path(desktop).parent}:{env['PATH']}",
        })
        yield env, core
