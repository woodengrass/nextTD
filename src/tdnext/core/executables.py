"""Pinned, checksum-verified, user-local executable installation."""

import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import PluginError


@dataclass(frozen=True, slots=True)
class Release:
    url: str
    sha256: str
    member: str


def install_release(name: str, local_bin: Path, release: Release) -> Path:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise PluginError(f"No bundled {name} release for this platform; install it on PATH.")
    curl = shutil.which("curl")
    if curl is None:
        raise PluginError(f"Cannot install {name}: curl is required for the verified download.")
    try:
        local_bin.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=f".{name}-", dir=local_bin) as work:
            archive = Path(work) / "release.tar.gz"
            result = subprocess.run(
                [curl, "--fail", "--location", "--silent", "--show-error", "--connect-timeout",
                 "10", "--max-time", "120", "--output", str(archive), release.url],
                capture_output=True, text=True, check=False, timeout=130,
            )
            if result.returncode:
                raise PluginError(f"Download of {name} failed: {result.stderr.strip()}")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if digest != release.sha256:
                raise PluginError(f"Checksum mismatch for {name}: expected {release.sha256}, got {digest}")
            with tarfile.open(archive, "r:gz") as bundle:
                member = bundle.getmember(release.member)
                if not member.isfile():
                    raise PluginError(f"Invalid {name} archive: executable is not a regular file")
                source = bundle.extractfile(member)
                if source is None:
                    raise PluginError(f"Invalid {name} archive: executable is unreadable")
                candidate = Path(work) / name
                with source, candidate.open("wb") as output:
                    shutil.copyfileobj(source, output)
            candidate.chmod(0o755)
            result = subprocess.run(
                [str(candidate), "--version"], capture_output=True, text=True,
                check=False, timeout=10,
            )
            if result.returncode:
                raise PluginError(f"Installed {name} could not run: {result.stderr.strip()}")
            destination = local_bin / name
            os.replace(candidate, destination)
            return destination
    except (OSError, subprocess.TimeoutExpired, tarfile.TarError, KeyError) as exc:
        raise PluginError(f"Failed to install {name}: {exc}") from exc
