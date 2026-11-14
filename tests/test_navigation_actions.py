import subprocess
from collections import deque

import pytest

from tdnext.core.errors import PluginError
from tdnext.plugins.desktop import navigation


@pytest.mark.parametrize("first", [(0, "", ""), (1, "", "There is no active session!\n")])
def test_read_only_query_waits_for_a_real_response(
    monkeypatch: pytest.MonkeyPatch, first: tuple[int, str, str]
) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    results = deque([first, (0, "[]", "")])
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: str | bool | int) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        code, output, error = results.popleft()
        return subprocess.CompletedProcess(command, code, output, error)

    monkeypatch.setattr(navigation.subprocess, "run", run)
    monkeypatch.setattr(navigation.time, "sleep", lambda _seconds: None)
    assert navigation.action("list-tabs", "--json") == "[]"
    assert len(calls) == 2


def test_query_timeout_reports_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    times = iter([0.0, 6.0])
    monkeypatch.setattr(navigation.time, "monotonic", lambda: next(times))

    def run(command: list[str], **_kwargs: str | bool | int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(navigation.subprocess, "run", run)
    with pytest.raises(PluginError, match="list-tabs.*empty output"):
        navigation.action("list-tabs", "--json")


def test_failed_mutating_action_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: str | bool | int) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "There is no active session!\n")

    monkeypatch.setattr(navigation.subprocess, "run", run)
    with pytest.raises(PluginError, match="There is no active session"):
        navigation.action("go-to-tab-by-id", "3")
    assert len(calls) == 1
