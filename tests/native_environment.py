import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tdnext.core.models import PluginState
from tdnext.core.state import StateStore

ROOT = Path(__file__).parents[1]


def executable(name: str) -> str | None:
    local = ROOT / "state" / "bin" / name
    return str(local) if local.is_file() and os.access(local, os.X_OK) else shutil.which(name)


@pytest.fixture
def native_env(tmp_path: Path) -> Iterator[tuple[dict[str, str], str]]:
    zellij, btop = executable("zellij"), executable("btop")
    if not zellij or not btop or not shutil.which("tmux"):
        pytest.skip("native test requires Zellij 0.45.1, btop, and tmux")
    version = subprocess.run([zellij, "--version"], capture_output=True, text=True, check=True, timeout=5)
    if "0.45.1" not in version.stdout:
        pytest.skip("native test requires Zellij 0.45.1")
    state = tmp_path / "state"
    ids = frozenset({"desktop", "files", "monitor", "settings"})
    StateStore(state / "plugins.json").write(PluginState(installed=ids, enabled=ids))
    excluded = {"ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"}
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    with TemporaryDirectory(prefix="tdn-") as socket:
        env.update({
            "TERM": "xterm-256color", "TDNEXT_STATE_DIR": str(state), "ZELLIJ_SOCKET_DIR": socket,
            "TDNEXT_TEST_TMUX_SOCKET": str(Path(socket) / "tmux.sock"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_DATA_HOME": str(tmp_path / "data"), "PATH": f"{Path(zellij).parent}:{Path(btop).parent}:{env['PATH']}",
        })
        yield env, zellij
