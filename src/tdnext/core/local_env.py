"""User-approved shared-process PATH for locally installed executables.

A desktop process (``tdnext run`` / ``python -m tdnext`` / native pane
processes) shares one process-wide ``PATH`` that starts with the absolute
``<state>/bin`` directory, so plugins can use locally installed executables
by bare name at import, construction, and runtime.

Bootstrap runs once at real execution boundaries (CLI/pane entry points),
never on import: importing these modules leaves the process environment
identical. User-facing surfaces (interactive shell, external editor, the
Zellij server and its arbitrary new panes) receive the exact original
``PATH`` (unset vs empty vs value) with bootstrap metadata removed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

BOOTSTRAPPED_ENV = "TDNEXT_PATH_BOOTSTRAPPED"
ORIGINAL_ENV = "TDNEXT_ORIGINAL_PATH"
ORIGINAL_PRESENT_ENV = "TDNEXT_ORIGINAL_PATH_PRESENT"

_VALID_PRESENT = ("0", "1")


def project_root() -> Path:
    """Resolve the repository project root (single shared source)."""
    return Path(__file__).resolve().parents[3]


def resolve_state_dir(explicit: Path | None = None) -> Path:
    """Resolve the absolute state directory honoring ``TDNEXT_STATE_DIR``."""
    if explicit is not None:
        base = explicit
    else:
        base = Path(os.environ.get("TDNEXT_STATE_DIR", str(project_root() / "state"))).expanduser()
    if not base.is_absolute():
        base = Path.cwd() / base
    return base


def state_bin(state_dir: Path | None = None) -> Path:
    """Resolve the absolute local ``bin`` directory for this process."""
    return resolve_state_dir(state_dir) / "bin"


def _markers_valid(source: Mapping[str, str]) -> bool:
    """Accept bootstrap metadata only when complete and self-consistent."""
    if source.get(BOOTSTRAPPED_ENV) != "1":
        return False
    present = source.get(ORIGINAL_PRESENT_ENV)
    if present not in _VALID_PRESENT:
        return False
    return present == "0" or ORIGINAL_ENV in source


def is_bootstrapped(env: Mapping[str, str] | None = None) -> bool:
    """Report whether this process already captured its original PATH."""
    source = os.environ if env is None else env
    return _markers_valid(source)


def bootstrap(state_dir: Path | None = None) -> Path:
    """Prepend the absolute state bin to PATH once; stable on reentry.

    Incomplete or forged markers are recaptured from the live environment so
    a real original PATH is never discarded. An absent incoming PATH resolves
    desktop lookup against ``os.defpath`` while the original stays absent.
    """
    if _markers_valid(os.environ):
        return state_bin(state_dir)
    if "PATH" in os.environ:
        original_present = "1"
        original_value = os.environ["PATH"]
    else:
        original_present = "0"
        original_value = ""
    os.environ[ORIGINAL_PRESENT_ENV] = original_present
    if original_present == "1":
        os.environ[ORIGINAL_ENV] = original_value
    else:
        os.environ.pop(ORIGINAL_ENV, None)
    bin_dir = state_bin(state_dir)
    if original_present == "1":
        if original_value != "":
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{original_value}"
        else:
            # Never introduce an empty component: it would silently add the
            # current directory to executable lookup for an empty original.
            os.environ["PATH"] = str(bin_dir)
    else:
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.defpath}"
    os.environ[BOOTSTRAPPED_ENV] = "1"
    return bin_dir


def original_path(env: Mapping[str, str] | None = None) -> tuple[bool, str | None]:
    """Return the captured original PATH as (present, value)."""
    source = os.environ if env is None else env
    if _markers_valid(source):
        if source.get(ORIGINAL_PRESENT_ENV) == "1":
            return True, source[ORIGINAL_ENV]
        return False, None
    if "PATH" in source:
        value = source["PATH"]
        return True, value
    return False, None


def restored_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy ``base`` with PATH restored and bootstrap metadata removed."""
    snapshot: dict[str, str] = dict(os.environ if base is None else base)
    present: bool
    value: str | None
    if _markers_valid(snapshot):
        present = snapshot[ORIGINAL_PRESENT_ENV] == "1"
        value = snapshot[ORIGINAL_ENV] if present else None
    else:
        present = "PATH" in snapshot
        value = snapshot.get("PATH")
    if present and value is not None:
        snapshot["PATH"] = value
    else:
        snapshot.pop("PATH", None)
    snapshot.pop(BOOTSTRAPPED_ENV, None)
    snapshot.pop(ORIGINAL_ENV, None)
    snapshot.pop(ORIGINAL_PRESENT_ENV, None)
    return snapshot
