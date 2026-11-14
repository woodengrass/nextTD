import json
import subprocess
from collections import deque

import pytest
from native_session import NativeSession

CLIENTS = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_2 files\n"


@pytest.mark.parametrize("tabs", ["", "   ", "[]"])
def test_readiness_waits_for_nonempty_populated_tab_list(
    monkeypatch: pytest.MonkeyPatch, tabs: str
) -> None:
    session = NativeSession.__new__(NativeSession)
    session.binary = "zellij"
    session.target_env = {}
    outputs = deque([CLIENTS, tabs])

    def run(command: list[str], **_kwargs: str | bool | int | dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, outputs.popleft(), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert not session.ready(3)
    assert "tabs:" in session.startup_diagnostics


def test_readiness_waits_for_a_real_client_before_querying_tabs(monkeypatch: pytest.MonkeyPatch) -> None:
    session = NativeSession.__new__(NativeSession)
    session.binary = "zellij"
    session.target_env = {}
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: str | bool | int | dict[str, str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert not session.ready(3)
    assert calls == [["zellij", "action", "list-clients"]]


def test_readiness_does_not_hide_invalid_nonempty_json(monkeypatch: pytest.MonkeyPatch) -> None:
    session = NativeSession.__new__(NativeSession)
    session.binary = "zellij"
    session.target_env = {}
    outputs = deque([CLIENTS, "not-json"])

    def run(command: list[str], **_kwargs: str | bool | int | dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, outputs.popleft(), "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(json.JSONDecodeError):
        session.ready(3)


def test_readiness_rejects_client_output_misrouted_to_tab_query(monkeypatch: pytest.MonkeyPatch) -> None:
    session = NativeSession.__new__(NativeSession)
    session.binary = "zellij"
    session.target_env = {}
    outputs = deque([CLIENTS, CLIENTS])

    def run(command: list[str], **_kwargs: str | bool | int | dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, outputs.popleft(), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert not session.ready(3)


def test_readiness_rejects_tab_json_misrouted_to_client_query(monkeypatch: pytest.MonkeyPatch) -> None:
    session = NativeSession.__new__(NativeSession)
    session.binary = "zellij"
    session.target_env = {}
    outputs = deque(['[\n{"active": true}\n]', '[{"active": true}, {"active": false}, {"active": false}]'])

    def run(command: list[str], **_kwargs: str | bool | int | dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, outputs.popleft(), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert not session.ready(3)


@pytest.mark.parametrize(
    ("tabs", "expected"),
    [
        ('[{"active": true}]', False),
        ('[{"active": false}, {"active": false}, {"active": false}]', False),
        ('[{"active": false}, {"active": true}, {"active": false}]', True),
    ],
)
def test_readiness_requires_expected_tabs_and_an_active_tab(
    monkeypatch: pytest.MonkeyPatch, tabs: str, expected: bool
) -> None:
    session = NativeSession.__new__(NativeSession)
    session.binary = "zellij"
    session.target_env = {}
    outputs = deque([CLIENTS, tabs])

    def run(command: list[str], **_kwargs: str | bool | int | dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, outputs.popleft(), "")

    monkeypatch.setattr(subprocess, "run", run)
    assert session.ready(3) is expected


def test_readiness_reports_a_transient_tab_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session = NativeSession.__new__(NativeSession)
    session.binary = "zellij"
    session.target_env = {}
    outputs = deque([(0, CLIENTS, ""), (1, "", "server not ready")])

    def run(command: list[str], **_kwargs: str | bool | int | dict[str, str]) -> subprocess.CompletedProcess[str]:
        code, output, error = outputs.popleft()
        return subprocess.CompletedProcess(command, code, output, error)

    monkeypatch.setattr(subprocess, "run", run)
    assert not session.ready(3)
    assert "server not ready" in session.startup_diagnostics
