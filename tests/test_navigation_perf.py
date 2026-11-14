"""Concurrency + subprocess-budget tests for navigation query gathering.

open_header/return_to_origin must overlap their independent Zellij queries
without changing resolution order, error precedence, or focus semantics.
"""
import json
import subprocess
import threading

import pytest

from tdnext.core.errors import PluginError
from tdnext.plugins.desktop import navigation

CLIENTS_FIRST = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_5 zsh\n"
CLIENTS_FOCUSED = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_7 zsh\n"
PANES = json.dumps([
    {"id": 5, "is_plugin": False, "is_floating": True, "tab_id": 0},
    {"id": 7, "is_plugin": False, "is_floating": False, "tab_id": 0},
    {"id": 9, "is_plugin": False, "is_floating": False, "tab_id": 2},
])
TABS = json.dumps([
    {"tab_id": 0, "name": "Files"},
    {"tab_id": 2, "name": "tdnext-header:settings"},
])
RETURN_TABS = json.dumps([
    {"tab_id": 0, "name": "Files"},
    {"tab_id": 2, "name": "tdnext-header:settings@0:terminal_7"},
])


def _response(command: list[str], stdout: str = "", code: int = 0, error: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, code, stdout, error)


def test_gather_runs_tasks_concurrently() -> None:
    release = threading.Event()

    def first() -> str:
        assert release.wait(timeout=5)
        return "first"

    def second() -> str:
        release.set()
        return "second"

    assert navigation.gather(first, second) == ("first", "second")


def test_gather_reports_first_failure_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")

    def run(command: list[str], **_kwargs: str | bool | int) -> subprocess.CompletedProcess[str]:
        if command[-1] == "--json" and "list-panes" in command:
            raise PluginError("panes boom")
        return _response(command, "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n")

    monkeypatch.setattr(navigation.subprocess, "run", run)
    with pytest.raises(PluginError, match="Cannot identify the client"):
        navigation.open_header("settings")


def _open_header_run(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    clients = [CLIENTS_FIRST, CLIENTS_FOCUSED]

    def run(command: list[str], **_kwargs: str | bool | int) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        tail = command[2:]
        if tail == ["list-clients"]:
            return _response(command, clients.pop(0))
        if tail == ["list-panes", "--json"]:
            return _response(command, PANES)
        if tail == ["list-tabs", "--json"]:
            return _response(command, TABS)
        return _response(command, "")

    monkeypatch.setattr(navigation.subprocess, "run", run)


def test_open_header_overlaps_refresh_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    _open_header_run(monkeypatch, calls)
    navigation.open_header("settings")
    tails = [command[2:] for command in calls]
    # Identity, focus restore, then the three overlapped refresh queries, then switch.
    assert tails[0] == ["list-clients"]
    assert tails[1] == ["focus-last-pane"]
    assert sorted(map(tuple, tails[2:5])) == sorted([
        ("list-clients",), ("list-panes", "--json"), ("list-tabs", "--json"),
    ])
    assert tails[5] == ["rename-tab-by-id", "2", "tdnext-header:settings@0:terminal_7"]
    assert tails[6] == ["go-to-tab-by-id", "2"]
    assert len(tails) == 7


def _return_run(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], final_clients: str) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    clients = iter([CLIENTS_FIRST, final_clients])

    def run(command: list[str], **_kwargs: str | bool | int) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        tail = command[2:]
        if tail == ["list-clients"]:
            return _response(command, next(clients))
        if tail == ["list-panes", "--json"]:
            return _response(command, PANES)
        if tail == ["list-tabs", "--json"]:
            return _response(command, RETURN_TABS)
        return _response(command, "")

    monkeypatch.setattr(navigation.subprocess, "run", run)


def test_return_to_origin_skips_refocus_when_pane_already_focused(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    _return_run(monkeypatch, calls, CLIENTS_FOCUSED)
    navigation.return_to_origin("settings")
    tails = [command[2:] for command in calls]
    assert tails[0] == ["list-tabs", "--json"]
    assert sorted(map(tuple, tails[1:3])) == sorted([("list-clients",), ("list-panes", "--json")])
    assert tails[3] == ["go-to-tab-by-id", "0"]
    assert tails[4] == ["list-clients"]
    assert len(tails) == 5


def test_return_to_origin_restores_focus_when_tab_switch_moves_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    _return_run(monkeypatch, calls, CLIENTS_FIRST)
    navigation.return_to_origin("settings")
    tails = [command[2:] for command in calls]
    assert tails[-2:] == [["list-clients"], ["focus-pane-id", "terminal_7"]]


def test_return_without_pane_suffix_issues_no_client_or_pane_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    legacy = json.dumps([{"tab_id": 0, "name": "Files"}, {"tab_id": 2, "name": "tdnext-header:settings@0"}])

    def run(command: list[str], **_kwargs: str | bool | int) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[2:] == ["list-tabs", "--json"]:
            return _response(command, legacy)
        return _response(command, "")

    monkeypatch.setattr(navigation.subprocess, "run", run)
    navigation.return_to_origin("settings")
    assert [command[2:] for command in calls] == [["list-tabs", "--json"], ["go-to-tab-by-id", "0"]]
