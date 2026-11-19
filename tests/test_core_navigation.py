"""Native core navigation integration (separate focused coverage).

Contract (plugin.py and navigation.py only; Rust and patches untouched):

- New sessions select ``state/bin/tdnext`` only when it is
  executable and ``<binary> action --help`` lists all five required native
  navigation commands. Anything else refuses to start without launching.
- A capable session sets ``TDNEXT_ZELLIJ``
  to the exact binary, and binds Alt+o to
  ``OpenTabWithReturn "tdnext-header:<id>"`` instead of the Python Run.
  ``shared_except "locked"`` and the no-header unbind are preserved.
- On a core session ``open_header`` calls
  ``open-tab-with-return-from-header <tab_name>`` atomically with no further
  query; ``back_from_header`` calls ``return-to-origin`` once with no
  fallback. Failures surface with no navigation.
"""

import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from test_desktop_host import default_context
from textual.events import MouseUp

from tdnext.api import ViewPlugin
from tdnext.core.errors import PluginError
from tdnext.core.models import PluginManifest
from tdnext.core.session import SESSION_ENV, SessionSnapshot, SessionView
from tdnext.plugins.desktop import navigation
from tdnext.plugins.desktop import plugin as desktop_plugin
from tdnext.plugins.desktop.app import DesktopApp
from tdnext.plugins.desktop.panes import HeaderPane
from tdnext.plugins.files.plugin import FilesPlugin

CORE_COMMANDS = (
    "open-tab-with-return",
    "open-tab-with-return-from-header",
    "restore-focus-from-header",
    "return-to-origin",
    "set-pane-fixed-width",
)
FULL_HELP = "\n".join((*CORE_COMMANDS, "list-tabs", ""))


def _write_core_binary(state: Path, *, executable: bool = True) -> Path:
    candidate = state / "bin" / desktop_plugin.CORE_BINARY_NAME
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"#!/bin/sh\nexit 0\n")
    candidate.chmod(0o755 if executable else 0o644)
    assert candidate.is_file() and os.access(candidate, os.X_OK) is executable
    assert bool(candidate.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)) is executable
    return candidate


def _probe_run(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, object]], **script: object) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        outcome = (
            script.get("version", ("1\n", 0, "")) if command[1:] == ["--tdnext-chrome-version"]
            else script.get("outcome", ("", 0, ""))
        )
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, tuple)
        stdout, code, error = outcome
        return subprocess.CompletedProcess(command, code, stdout, error)

    monkeypatch.setattr(subprocess, "run", run)


def test_probe_rejects_missing_binary_without_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(tmp_path / "state"))

    def fail(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"probe must not run without a binary: {command}")

    monkeypatch.setattr(subprocess, "run", fail)
    assert desktop_plugin.core_capable_binary() is None


def test_probe_rejects_non_executable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    _write_core_binary(state, executable=False)

    def fail(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"probe must not run for a non-executable: {command}")

    monkeypatch.setattr(subprocess, "run", fail)
    assert desktop_plugin.core_capable_binary() is None


def test_probe_ignores_existing_v1_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    legacy = state / "bin" / "zellij-core-navigation"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"#!/bin/sh\nexit 0\n")
    legacy.chmod(0o755)

    def fail(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"v1 binary must not be probed for new sessions: {command}")

    monkeypatch.setattr(subprocess, "run", fail)
    assert desktop_plugin.core_capable_binary() is None


@pytest.mark.parametrize(
    ("help_text", "capable"),
    [
        (FULL_HELP, True),
        ("prefix open-tab-with-return-from-header suffix\nreturn-to-origin\n", False),
        ("open-tab-with-return-from-header\nreturn-to-origin\n", False),
        ("open-tab-with-return\nreturn-to-origin\n", False),
        ("open-tab-with-return\n", False),
        ("return-to-origin\n", False),
        ("", False),
        ("open-tab-with-return\nopen-tab-with-return\n", False),
    ],
)
def test_probe_requires_all_command_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, help_text: str, capable: bool
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    candidate = _write_core_binary(state)
    calls: list[dict[str, object]] = []
    _probe_run(monkeypatch, calls, outcome=(help_text, 0, ""))
    assert (desktop_plugin.core_capable_binary() == candidate) is capable
    assert len(calls) == (2 if capable else 1)


@pytest.mark.parametrize("missing", CORE_COMMANDS)
def test_probe_rejects_each_missing_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    _write_core_binary(state)
    calls: list[dict[str, object]] = []
    help_text = "\n".join(command for command in CORE_COMMANDS if command != missing)
    _probe_run(monkeypatch, calls, outcome=(help_text, 0, ""))

    assert desktop_plugin.core_capable_binary() is None


@pytest.mark.parametrize("command", CORE_COMMANDS)
@pytest.mark.parametrize("replacement", ["prefix-{}", "{}-suffix"])
def test_probe_rejects_partial_command_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, replacement: str
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    _write_core_binary(state)
    calls: list[dict[str, object]] = []
    help_text = FULL_HELP.replace(f"{command}\n", f"{replacement.format(command)}\n")
    _probe_run(monkeypatch, calls, outcome=(help_text, 0, ""))

    assert desktop_plugin.core_capable_binary() is None


@pytest.mark.parametrize(
    "outcome",
    [
        ("open-tab-with-return\nreturn-to-origin\n", 1, ""),
        ("", 0, ""),
        subprocess.TimeoutExpired(["zellij-core-navigation"], 5),
        OSError("missing"),
    ],
)
def test_probe_fails_closed_on_bad_exit_or_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: object
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    _write_core_binary(state)
    calls: list[dict[str, object]] = []
    _probe_run(monkeypatch, calls, outcome=outcome)
    assert desktop_plugin.core_capable_binary() is None
    assert len(calls) == 1


def test_probe_uses_bounded_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    candidate = _write_core_binary(state)
    calls: list[dict[str, object]] = []
    _probe_run(monkeypatch, calls, outcome=(FULL_HELP, 0, ""))
    assert desktop_plugin.core_capable_binary() == candidate
    assert candidate == state / "bin" / desktop_plugin.CORE_BINARY_NAME
    assert calls[0]["command"] == [str(candidate), "action", "--help"]
    assert calls[0]["timeout"] == desktop_plugin.CORE_PROBE_TIMEOUT
    assert calls[1]["command"] == [str(candidate), "--tdnext-chrome-version"]
    assert calls[1]["timeout"] == desktop_plugin.CORE_PROBE_TIMEOUT


@pytest.mark.parametrize("version", [("", 0, ""), ("2\n", 0, ""), ("1\n", 1, ""), OSError("missing")])
def test_probe_rejects_unsupported_chrome_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: object
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("TDNEXT_STATE_DIR", str(state))
    _write_core_binary(state)
    calls: list[dict[str, object]] = []
    _probe_run(monkeypatch, calls, outcome=(FULL_HELP, 0, ""), version=version)

    assert desktop_plugin.core_capable_binary() is None


def capture_core_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header_ids: tuple[str, ...], *, help_text: str = FULL_HELP
) -> tuple[str, str, dict[str, str], Path]:
    context = default_context(tmp_path, monkeypatch)
    state = tmp_path / "state"
    candidate = _write_core_binary(state)
    manifests = context.manager.manifests
    views: list[tuple[PluginManifest, ViewPlugin]] = [(manifests["files"], FilesPlugin())]
    views.extend(
        (manifests["settings"].model_copy(update={"id": plugin_id, "name": plugin_id}), FilesPlugin())
        for plugin_id in header_ids
    )
    session = SessionSnapshot(
        views=tuple(SessionView(manifest=manifest, icon="[]") for manifest, _ in views),
        protected_plugins=frozenset(),
        view_dependencies={},
        participants=tuple(manifest for manifest, _ in views),
    )
    context = replace(context, session=session)
    captured: list[tuple[str, str, dict[str, str]]] = []

    def run_zellij(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["action", "--help"]:
            assert command[0] == str(candidate)
            return subprocess.CompletedProcess(command, 0, help_text, "")
        if command[1:] == ["--tdnext-chrome-version"]:
            return subprocess.CompletedProcess(command, 0, "1\n", "")
        assert kwargs.get("check") is False
        env = kwargs["env"]
        assert isinstance(env, dict)
        config = Path(command[command.index("--config") + 1]).read_text(encoding="utf-8")
        assert "--layout-string" not in command
        layout_path = Path(command[command.index("--new-session-with-layout") + 1])
        assert layout_path.name == "layout.kdl"
        layout = layout_path.read_text(encoding="utf-8")
        captured.append((config, layout, env))
        return subprocess.CompletedProcess(command, 0)

    import shutil as _shutil

    monkeypatch.setattr(_shutil, "get_terminal_size", lambda *args, **kwargs: os.terminal_size((120, 36)))
    monkeypatch.setattr(subprocess, "run", run_zellij)
    monkeypatch.setenv(SESSION_ENV, "inherited-session-must-not-leak")
    monkeypatch.delenv("ZELLIJ", raising=False)

    desktop_plugin.DesktopPlugin().run(context, views)

    config, layout, env = captured[0]
    return config, layout, env, candidate


def test_capable_session_binds_open_tab_with_return_without_python_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, layout, env, candidate = capture_core_launch(tmp_path, monkeypatch, ("preferences",))

    assert 'bind "Alt o" {' in config
    assert 'OpenTabWithReturn "tdnext-header:preferences";' in config
    assert "tdnext.plugins.desktop.shortcut" not in config
    assert "Run " not in next(line for line in config.splitlines() if line.strip().startswith("bind"))
    assert 'shared_except "locked"' in config
    assert env["TDNEXT_ZELLIJ"] == str(candidate)


def test_capable_session_without_header_keeps_unbind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, layout, env, candidate = capture_core_launch(tmp_path, monkeypatch, ())

    bindings = [line.strip() for line in config.splitlines()]
    assert 'unbind "Alt o"' in bindings
    assert 'bind "Alt o" {' not in bindings
    assert "OpenTabWithReturn" not in config
    assert env["TDNEXT_ZELLIJ"] == str(candidate)


def test_large_layout_passed_via_file_keeps_argv_small(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """E2BIG regression: a >MAX_ARG_STRLEN layout must travel via file, not argv."""
    context = default_context(tmp_path, monkeypatch)
    views: list[tuple[PluginManifest, ViewPlugin]] = [(context.manager.manifests["files"], FilesPlugin())]
    launched: list[list[str]] = []

    def run_zellij(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        launched.append(command)
        assert kwargs.get("check") is False
        layout_file = Path(command[command.index("--new-session-with-layout") + 1])
        assert layout_file.read_text(encoding="utf-8") == "x" * 200_000
        return subprocess.CompletedProcess(command, 0)

    import shutil as _shutil

    monkeypatch.setattr(desktop_plugin, "layout_string", lambda *args, **kwargs: "x" * 200_000)
    monkeypatch.setattr(desktop_plugin, "core_capable_binary", lambda: Path("/fake/tdnext"))
    monkeypatch.setattr(_shutil, "get_terminal_size", lambda *args, **kwargs: os.terminal_size((120, 36)))
    monkeypatch.setattr(subprocess, "run", run_zellij)
    monkeypatch.delenv("ZELLIJ", raising=False)

    desktop_plugin.DesktopPlugin().run(context, views)

    assert len(launched) == 1
    assert "--layout-string" not in launched[0]
    assert max(len(part) for part in launched[0]) < 131072


def _run_without_capable_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, help_text: str | None,
) -> tuple[list[Path], list[list[str]]]:
    """Run without a capable core binary; startup is refused without launching."""
    context = default_context(tmp_path, monkeypatch)
    views = [(context.manager.manifests["files"], FilesPlugin())]
    fallback: list[Path] = []
    launched: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        launched.append(command)
        return subprocess.CompletedProcess(command, 0, help_text or "", "")

    if help_text is not None:
        _write_core_binary(tmp_path / "state")
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "get_terminal_size", lambda *args, **kwargs: os.terminal_size((120, 36)))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(DesktopApp, "run", lambda self: fallback.append(self.context.directory))
    monkeypatch.delenv("ZELLIJ", raising=False)

    with pytest.raises(PluginError, match="tdnext executable missing or unusable"):
        desktop_plugin.DesktopPlugin().run(context, views)
    return fallback, launched


@pytest.mark.parametrize("missing", CORE_COMMANDS)
def test_partial_help_refuses_start_without_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    help_text = "\n".join(command for command in CORE_COMMANDS if command != missing)
    fallback, launched = _run_without_capable_core(tmp_path, monkeypatch, help_text=help_text)

    assert fallback == []
    assert all("--layout-string" not in command for command in launched)


def _install_core(
    monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], script: dict[tuple[str, ...], object] | None = None
) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    script = script or {}

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        outcome = script.get(tuple(command[2:]), ("", 0, ""))
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, tuple)
        stdout, code, error = outcome
        return subprocess.CompletedProcess(command, code, stdout, error)

    monkeypatch.setattr(navigation.subprocess, "run", run)


def test_open_header_core_opens_atomically_with_no_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    _install_core(monkeypatch, calls)
    navigation.open_header("settings")
    assert [command[2:] for command in calls] == [
        ["open-tab-with-return-from-header", "tdnext-header:settings"],
    ]


def test_open_header_core_failure_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    _install_core(monkeypatch, calls, {("open-tab-with-return-from-header", "tdnext-header:settings"): ("", 1, "boom")})
    with pytest.raises(PluginError, match="boom"):
        navigation.open_header("settings")
    assert [command[2:] for command in calls] == [["open-tab-with-return-from-header", "tdnext-header:settings"]]


def test_back_from_header_core_calls_return_once_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    _install_core(monkeypatch, calls)
    navigation.back_from_header()
    assert [command[2:] for command in calls] == [["return-to-origin"]]


def test_back_from_header_core_failure_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    _install_core(monkeypatch, calls, {("return-to-origin",): ("", 1, "gone")})
    with pytest.raises(PluginError, match="gone"):
        navigation.back_from_header()
    assert [command[2:] for command in calls] == [["return-to-origin"]]


def test_header_pane_mouse_and_tool_escape_implementation_preserved() -> None:
    assert HeaderPane([]).mouse_down is False
    assert callable(HeaderPane.on_mouse_down)
    assert callable(HeaderPane.on_mouse_up)
    assert callable(HeaderPane.on_header_bar_open_view)
    from tdnext.plugins.desktop.tool import ToolPane

    assert ("escape", "close_view", "Back") in ToolPane.BINDINGS


@pytest.mark.anyio
@pytest.mark.parametrize("release_only", [False, True], ids=["full-click", "release-only"])
async def test_blank_header_click_restores_focus_from_header(
    monkeypatch: pytest.MonkeyPatch, release_only: bool
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(navigation, "action", lambda *args: calls.append(args))

    async with HeaderPane([]).run_test(size=(120, 2)) as pilot:
        if release_only:
            await pilot._post_mouse_events([MouseUp], "#desktop-label", offset=(10, 0), button=1)
        else:
            await pilot.click("#desktop-label", offset=(10, 0))

    assert calls == [("restore-focus-from-header",)]


@pytest.mark.anyio
@pytest.mark.parametrize("release_only", [False, True], ids=["full-click", "release-only"])
async def test_settings_header_click_opens_once_without_focus_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_only: bool
) -> None:
    context = default_context(tmp_path, monkeypatch)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(navigation, "action", lambda *args: calls.append(args))
    views: list[tuple[PluginManifest, ViewPlugin]] = [(context.manager.manifests["settings"], FilesPlugin())]

    async with HeaderPane(views).run_test(size=(120, 2)) as pilot:
        if release_only:
            await pilot._post_mouse_events([MouseUp], "#header-settings", offset=(1, 0), button=1)
        else:
            await pilot.click("#header-settings", offset=(1, 0))

    assert calls == [("open-tab-with-return-from-header", "tdnext-header:settings")]


def test_missing_binary_refuses_start_without_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback, launched = _run_without_capable_core(tmp_path, monkeypatch, help_text=None)

    assert fallback == []
    assert launched == []
