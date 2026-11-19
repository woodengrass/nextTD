import subprocess

import pytest

from tdnext.core.errors import PluginError
from tdnext.plugins.desktop import navigation


def test_action_returns_stdout_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: str | bool | int) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "[]", "")

    monkeypatch.setattr(navigation.subprocess, "run", run)
    assert navigation.action("list-tabs", "--json") == "[]"
    assert len(calls) == 1


def test_action_failure_raises_once_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: str | bool | int) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "There is no active session!\n")

    monkeypatch.setattr(navigation.subprocess, "run", run)
    with pytest.raises(PluginError, match="There is no active session"):
        navigation.action("go-to-tab-by-id", "3")
    assert len(calls) == 1


def test_action_transport_error_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")

    def run(command: list[str], **_kwargs: str | bool | int) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 5)

    monkeypatch.setattr(navigation.subprocess, "run", run)
    with pytest.raises(PluginError, match="Cannot control Zellij"):
        navigation.action("restore-focus-from-header")
