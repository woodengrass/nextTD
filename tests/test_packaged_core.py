from pathlib import Path

import pytest

from tdnext.plugins.desktop import plugin


@pytest.mark.parametrize("state_core", ["missing", "invalid", "stale", "capable"])
def test_packaged_core_is_used_without_overwriting_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state_core: str
) -> None:
    package = tmp_path / "tdnext"
    packaged = package / "_native" / plugin.CORE_BINARY_NAME
    packaged.parent.mkdir(parents=True)
    navigation = (
        "open-tab-with-return open-tab-with-return-from-header "
        "restore-focus-from-header return-to-origin"
    )
    packaged.write_text(f"#!/bin/sh\nprintf '%s\\n' {navigation} set-pane-fixed-width\n")
    packaged.chmod(0o755)
    state = tmp_path / "state"
    local = state / "bin" / plugin.CORE_BINARY_NAME
    local_content = ""
    if state_core != "missing":
        local.parent.mkdir(parents=True)
        if state_core == "capable":
            local_content = packaged.read_text()
        elif state_core == "stale":
            local_content = f"#!/bin/sh\nprintf '%s\\n' {navigation}\n"
        else:
            local_content = "#!/bin/sh\nexit 1\n"
        local.write_text(local_content)
        local.chmod(0o755)
    monkeypatch.setattr(plugin, "__file__", str(package / "plugins" / "desktop" / "plugin.py"))
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))

    assert plugin.core_capable_binary() == (local if state_core == "capable" else packaged)
    if state_core != "missing":
        assert local.read_text() == local_content
