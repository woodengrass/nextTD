import subprocess
from pathlib import Path

from tdnext.core.errors import PluginError
from tdnext.core.executables import Release, install_release
from tdnext.core.manager import PluginManager

BTOP_RELEASE = Release(
    "https://github.com/aristocratos/btop/releases/download/v1.4.8/"
    "btop-x86_64-unknown-linux-musl.tar.gz",
    "af589cf82cc9ea9c29ce050b9239a13bef2d1492256d476156e1054a42ce89ad",
    "./btop/bin/btop",
)


class MonitorDependencies:
    @staticmethod
    def check(manager: PluginManager) -> str | None:
        binary = manager.executable("btop")
        if binary is None:
            return (
                f"Download btop 1.4.8 from {BTOP_RELEASE.url}; "
                f"SHA-256 {BTOP_RELEASE.sha256}; extract {BTOP_RELEASE.member}; "
                f"install to {manager.local_bin / 'btop'}"
            )
        try:
            result = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PluginError(f"Cannot run btop at {binary}: {exc}") from exc
        if result.returncode or "btop" not in result.stdout.lower():
            raise PluginError(f"Executable at {binary} is not a working btop")
        return None

    @staticmethod
    def install(manager: PluginManager) -> Path:
        return install_release("btop", manager.local_bin, BTOP_RELEASE)
