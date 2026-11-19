"""Stage-3 packaged-core resolution: the chrome-version gate is required.

``core_capable_binary`` requires both the legacy ``action --help``
command listing and a bare ``--tdnext-chrome-version`` probe printing
``1``. A state-local binary that only answers the legacy listing is
stale and loses to the packaged core, exactly like a missing or broken
one.
"""

from pathlib import Path

import pytest

import tdnext
from tdnext.plugins.desktop import plugin

LEGACY_LISTING = (
    "open-tab-with-return open-tab-with-return-from-header "
    "restore-focus-from-header return-to-origin set-pane-fixed-width"
)
CHROME_VERSION_LINE = "printf '1\\n'"


@pytest.mark.parametrize("state_core", ["missing", "invalid", "stale", "capable"])
def test_packaged_core_is_used_without_overwriting_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_core: str
) -> None:
    package = tmp_path / "tdnext"
    packaged = package / "_native" / plugin.CORE_BINARY_NAME
    packaged.parent.mkdir(parents=True)
    # The packaged core stays capable under both the legacy listing probe
    # and the stage-3 chrome probe, so missing/invalid state binaries fall
    # back to it before and after the host migrates its probe.
    packaged.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--tdnext-chrome-version" ]; then '
        f"{CHROME_VERSION_LINE}; exit 0; fi\n"
        'if [ "$1" = "action" ] && [ "$2" = "--help" ]; then '
        f"printf '%s\\n' {LEGACY_LISTING}; exit 0; fi\n"
        "exit 1\n"
    )
    packaged.chmod(0o755)
    state = tmp_path / "state"
    local = state / "bin" / plugin.CORE_BINARY_NAME
    local_content = ""
    if state_core != "missing":
        local.parent.mkdir(parents=True)
        if state_core == "capable":
            local_content = packaged.read_text()
        elif state_core == "stale":
            local_content = (
                "#!/bin/sh\n"
                'if [ "$1" = "--tdnext-chrome-version" ]; then exit 1; fi\n'
                f"printf '%s\\n' {LEGACY_LISTING}\n"
            )
        else:
            local_content = "#!/bin/sh\nexit 1\n"
        local.write_text(local_content)
        local.chmod(0o755)
    monkeypatch.setattr(tdnext, "__file__", str(package / "__init__.py"))
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))

    assert plugin.core_capable_binary() == (local if state_core == "capable" else packaged)
    if state_core != "missing":
        assert local.read_text() == local_content
