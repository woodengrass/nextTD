"""Sequential-query regression for navigation Zellij control queries.

Oracle reproduced 2 cross-wired responses / 300 simultaneous queries on
Zellij 0.45.1 versus 0 / 120 sequential. Concurrent `zellij action`
subprocesses can receive each other's stdout, so open_header and
return_to_origin must issue their dependent control queries SEQUENTIALLY
in validation order. These tests fail against any concurrent implementation
and pass only with strictly ordered sequential queries.
"""

import json
import subprocess
import threading
import time

import pytest

from tdnext.core.errors import PluginError
from tdnext.plugins.desktop import navigation

CLIENTS_FIRST = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_5 zsh\n"
CLIENTS_FOCUSED = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_7 zsh\n"
PANES = json.dumps(
    [
        {"id": 5, "is_plugin": False, "is_floating": True, "tab_id": 0},
        {"id": 7, "is_plugin": False, "is_floating": False, "tab_id": 0},
        {"id": 9, "is_plugin": False, "is_floating": False, "tab_id": 2},
    ]
)
TABS = json.dumps(
    [
        {"tab_id": 0, "name": "Files"},
        {"tab_id": 2, "name": "tdnext-header:settings"},
    ]
)
RETURN_TABS = json.dumps(
    [
        {"tab_id": 0, "name": "Files"},
        {"tab_id": 2, "name": "tdnext-header:settings@0:terminal_7"},
    ]
)


def _response(command: list[str], stdout: str = "", code: int = 0, error: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, code, stdout, error)


class ConcurrencyProbe:
    """Tracks overlapping subprocess.run executions across threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def leave(self) -> None:
        with self._lock:
            self.active -= 1


def _install_open_header(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], probe: ConcurrencyProbe) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    clients = [CLIENTS_FIRST, CLIENTS_FOCUSED]

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        probe.enter()
        try:
            # Hold every query briefly so concurrently dispatched queries overlap.
            time.sleep(0.05)
            tail = command[2:]
            if tail == ["list-clients"]:
                return _response(command, clients.pop(0))
            if tail == ["list-panes", "--json"]:
                return _response(command, PANES)
            if tail == ["list-tabs", "--json"]:
                return _response(command, TABS)
            return _response(command, "")
        finally:
            probe.leave()

    monkeypatch.setattr(navigation.subprocess, "run", run)


def test_open_header_issues_queries_sequentially(monkeypatch: pytest.MonkeyPatch) -> None:
    """open_header must not overlap its dependent control queries."""
    calls: list[list[str]] = []
    probe = ConcurrencyProbe()
    _install_open_header(monkeypatch, calls, probe)
    navigation.open_header("settings")
    assert probe.max_active == 1, f"Zellij queries overlapped ({probe.max_active} concurrent)"
    tails = [command[2:] for command in calls]
    assert tails == [
        ["list-clients"],
        ["focus-last-pane"],
        ["list-clients"],
        ["list-panes", "--json"],
        ["list-tabs", "--json"],
        ["rename-tab-by-id", "2", "tdnext-header:settings@0:terminal_7"],
        ["go-to-tab-by-id", "2"],
    ]


def _install_return_to_origin(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], probe: ConcurrencyProbe) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    clients = iter([CLIENTS_FIRST, CLIENTS_FOCUSED])

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        probe.enter()
        try:
            time.sleep(0.05)
            tail = command[2:]
            if tail == ["list-clients"]:
                return _response(command, next(clients))
            if tail == ["list-panes", "--json"]:
                return _response(command, PANES)
            if tail == ["list-tabs", "--json"]:
                return _response(command, RETURN_TABS)
            return _response(command, "")
        finally:
            probe.leave()

    monkeypatch.setattr(navigation.subprocess, "run", run)


def test_return_to_origin_issues_queries_sequentially(monkeypatch: pytest.MonkeyPatch) -> None:
    """return_to_origin must not overlap its dependent control queries."""
    calls: list[list[str]] = []
    probe = ConcurrencyProbe()
    _install_return_to_origin(monkeypatch, calls, probe)
    navigation.return_to_origin("settings")
    assert probe.max_active == 1, f"Zellij queries overlapped ({probe.max_active} concurrent)"
    tails = [command[2:] for command in calls]
    assert tails == [
        ["list-tabs", "--json"],
        ["list-clients"],
        ["list-panes", "--json"],
        ["go-to-tab-by-id", "0"],
        ["list-clients"],
    ]


def test_open_header_swapped_cli_output_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swapped panes/tabs payloads must fail loudly, never navigate wrongly."""
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    clients = [CLIENTS_FIRST, CLIENTS_FOCUSED]

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        tail = command[2:]
        if tail == ["list-clients"]:
            return _response(command, clients.pop(0))
        if tail == ["list-panes", "--json"]:
            return _response(command, TABS)  # cross-wired: tabs JSON on panes channel
        if tail == ["list-tabs", "--json"]:
            return _response(command, PANES)  # cross-wired: panes JSON on tabs channel
        return _response(command, "")

    monkeypatch.setattr(navigation.subprocess, "run", run)
    with pytest.raises(PluginError, match="Cannot locate the current tab"):
        navigation.open_header("settings")


def test_return_to_origin_swapped_cli_output_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A panes payload that does not validate must preserve failure semantics."""
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        tail = command[2:]
        if tail == ["list-clients"]:
            return _response(command, CLIENTS_FIRST)
        if tail == ["list-panes", "--json"]:
            return _response(command, TABS)  # cross-wired: tabs JSON on panes channel
        if tail == ["list-tabs", "--json"]:
            return _response(command, RETURN_TABS)
        return _response(command, "")

    monkeypatch.setattr(navigation.subprocess, "run", run)
    with pytest.raises(PluginError, match="previous pane could not be identified"):
        navigation.return_to_origin("settings")


def test_open_header_malformed_panes_preserves_failure_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garbage on the panes channel must raise, never silently mis-navigate."""
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    clients = [CLIENTS_FIRST, CLIENTS_FOCUSED]

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        tail = command[2:]
        if tail == ["list-clients"]:
            return _response(command, clients.pop(0))
        if tail == ["list-panes", "--json"]:
            return _response(command, "not-json{{{")
        if tail == ["list-tabs", "--json"]:
            return _response(command, TABS)
        return _response(command, "")

    monkeypatch.setattr(navigation.subprocess, "run", run)
    with pytest.raises(PluginError, match="Cannot locate the current tab"):
        navigation.open_header("settings")


def test_open_header_first_failure_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sequential validation order: identity failure precedes query failures."""
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--json" and "list-panes" in command:
            raise PluginError("panes boom")
        return _response(command, "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n")

    monkeypatch.setattr(navigation.subprocess, "run", run)
    with pytest.raises(PluginError, match="Cannot identify the client"):
        navigation.open_header("settings")
