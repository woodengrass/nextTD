import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tdnext.plugins.desktop import plugin
from tdnext.runtime import manager_for

ROOT = Path(__file__).parents[1]

CHROME_ENV = "TDNEXT_CORE_BINARY"
CHROME_VERSION = "1"


def chrome_capable(source: Path) -> bool:
    """Whether ``source`` passes the launcher capability gate.

    Applies the host gate from ``plugin`` (required commands plus
    chrome version) to an arbitrary candidate path, which
    ``plugin.core_capable_binary`` cannot probe on its own. The
    fixture gate and the launcher gate must stay identical, otherwise
    the fixture would bless a binary the desktop refuses to start.
    """
    try:
        listing = subprocess.run(
            [str(source), "action", "--help"],
            capture_output=True, text=True, check=False, timeout=plugin.CORE_PROBE_TIMEOUT,
        )
        version = subprocess.run(
            [str(source), "--tdnext-chrome-version"],
            capture_output=True, text=True, check=False, timeout=plugin.CORE_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return False
    if listing.returncode != 0 or version.returncode != 0:
        return False
    output = f"{listing.stdout or ''}\n{listing.stderr or ''}"
    return (
        set(plugin.CORE_REQUIRED_COMMANDS).issubset(output.split())
        and version.stdout.strip() == CHROME_VERSION
    )


def capable_core_source() -> Path | None:
    """Return the core binary without touching test state.

    The explicit ``TDNEXT_CORE_BINARY`` override wins when it passes
    the launcher gate. Otherwise resolution is the host's own
    ``plugin.core_capable_binary``: the repo-built ``state/bin/tdnext``
    or the packaged binary. An invalid explicit override and a missing
    default both yield ``None`` so the caller fails instead of
    skipping.
    """
    override = os.environ.get(CHROME_ENV, "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file() and os.access(candidate, os.X_OK) and chrome_capable(candidate):
            return candidate
        return None
    return plugin.core_capable_binary()


def core_wanted_message() -> str:
    """Failure text for a missing or incapable core binary."""
    override = os.environ.get(CHROME_ENV, "").strip()
    if override:
        return (
            f"{CHROME_ENV}={override!r} is not a capable core binary "
            f"(required commands plus chrome version {CHROME_VERSION})"
        )
    return (
        "no capable core binary found: build the repo core "
        "(scripts/build-zellij-core-navigation.sh writes state/bin/tdnext) or set "
        f"{CHROME_ENV} to a binary passing the launcher capability gate "
        f"(required commands plus chrome version {CHROME_VERSION})"
    )


def install_stage_core(state: Path, source: Path) -> Path:
    """Install the stage core as ``<state>/bin/tdnext`` and shadow ``zellij``.

    Always copies: a hard link would let the ``chmod`` below rewrite the
    repo-built source's own permission bits. The isolated ``bin/zellij``
    shadow makes the session under test launch on the stage server while
    every other path keeps using explicit binaries. Returns the installed
    path.
    """
    bin_dir = state / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    dest = bin_dir / "tdnext"
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    shutil.copy2(source, dest)
    dest.chmod(0o755)
    shadow = bin_dir / "zellij"
    if shadow.is_symlink() or shadow.exists():
        shadow.unlink()
    shadow.symlink_to(source)
    return dest


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
    source = capable_core_source()
    if source is None:
        pytest.fail(f"native test requires a capable core binary: {core_wanted_message()}")
    install_stage_core(state, source)
    # Managed install via the production manager (btop is already on PATH,
    # so no download consent is involved).
    plugins = manager_for(state)
    for plugin_id in ("desktop", "files", "monitor", "settings"):
        plugins.enable(plugin_id)
    assert plugins.store.read().enabled == frozenset({"desktop", "files", "monitor", "settings"})
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
