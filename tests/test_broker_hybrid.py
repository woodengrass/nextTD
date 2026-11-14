"""Hybrid handoff: broker forget on open (primary header only) + exact NO ORIGIN fallback on back.

Contract (ordered coordination only, never atomic):

- Flag off: behavior untouched. open_header and back_from_header are exactly
  the original tab-name paths with no pipe call and no extra queries, even
  when TDNEXT_NAVIGATOR_HEADER is set.
- Flag on + primary header (TDNEXT_NAVIGATOR_HEADER set and matching, or
  unset for pre-hybrid sessions): open_header calls acknowledged forget after
  the origin==destination early-return and before the suffix rename/switch,
  reusing the already-selected client. Only the strict exact reply
  ``ok:FORGOT CLIENT=<selected>`` proceeds; anything else aborts with no
  rename and no switch.
- Flag on + secondary header (header env set to a different id): legacy
  paths with no pipe call.
- back_from_header falls back to legacy return_to_origin ONLY on the exact
  ``err:NO ORIGIN FOR CLIENT=<selected>`` verdict for the selected client.
  Empty, malformed, duplicate, other-client, timeout, transport, and all
  other broker errors fail closed with no navigation.
"""

import json
import subprocess

import pytest

from tdnext.core.errors import PluginError
from tdnext.plugins.desktop import navigation

CLIENTS_ONE = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_5 zsh\n"
CLIENTS_FIRST = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_5 zsh\n"
CLIENTS_FOCUSED = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_7 zsh\n"
PANES = json.dumps(
    [
        {"id": 5, "is_plugin": False, "is_floating": True, "tab_id": 0},
        {"id": 7, "is_plugin": False, "is_floating": False, "tab_id": 0},
        {"id": 9, "is_plugin": False, "is_floating": False, "tab_id": 2},
    ]
)
TABS_BARE = json.dumps(
    [
        {"tab_id": 0, "name": "Files"},
        {"tab_id": 2, "name": "tdnext-header:settings"},
    ]
)
TABS_OTHER_BARE = json.dumps(
    [
        {"tab_id": 0, "name": "Files"},
        {"tab_id": 2, "name": "tdnext-header:other"},
    ]
)
TABS_REENTRY = json.dumps(
    [
        {"tab_id": 0, "name": "tdnext-header:settings"},
        {"tab_id": 2, "name": "Files"},
    ]
)
TABS_SUFFIX = json.dumps(
    [
        {"tab_id": 0, "name": "Files"},
        {"tab_id": 2, "name": "tdnext-header:settings@0:terminal_7"},
    ]
)
FORGET_TAIL = ("pipe", "--name", "forget", "--", "client=1")
BACK_TAIL = ("pipe", "--name", "back", "--", "client=1 source=terminal_5")


def _response(command: list[str], stdout: str = "", code: int = 0, error: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, code, stdout, error)


def _install(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], script: dict[tuple[str, ...], object]) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        outcome = script.get(tuple(command[2:]), ("", 0, ""))
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, tuple)
        stdout, code, error = outcome
        return _response(command, stdout, code, error)

    monkeypatch.setattr(navigation.subprocess, "run", run)


def _install_open(
    monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], script: dict[tuple[str, ...], object]
) -> None:
    """Open-header script with sequential list-clients identity (first/focused)."""
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    clients = [CLIENTS_FIRST, CLIENTS_FOCUSED]

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        tail = tuple(command[2:])
        if tail == ("list-clients",):
            # Identity query first, refresh query second; later refresh/focus
            # reads reuse the focused client. Extra calls reuse the last.
            stdout = clients.pop(0) if clients else CLIENTS_FOCUSED
            return _response(command, stdout)
        outcome = script.get(tail, ("", 0, ""))
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, tuple)
        stdout, code, error = outcome
        return _response(command, stdout, code, error)

    monkeypatch.setattr(navigation.subprocess, "run", run)


def _enable_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDNEXT_NAVIGATOR_BROKER", "1")
    monkeypatch.setenv("TDNEXT_NAVIGATOR_HEADER", "settings")


def test_open_primary_calls_forget_before_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given the flag + primary header, forget is acknowledged before rename/switch."""
    _enable_primary(monkeypatch)
    calls: list[list[str]] = []
    _install_open(monkeypatch, calls, {
        ("list-panes", "--json"): (PANES, 0, ""),
        ("list-tabs", "--json"): (TABS_BARE, 0, ""),
        FORGET_TAIL: ("ok:FORGOT CLIENT=1", 0, ""),
    })
    navigation.open_header("settings")
    tails = [tuple(command[2:]) for command in calls]
    assert tails == [
        ("list-clients",),
        ("focus-last-pane",),
        ("list-clients",),
        ("list-panes", "--json"),
        ("list-tabs", "--json"),
        FORGET_TAIL,
        ("rename-tab-by-id", "2", "tdnext-header:settings@0:terminal_7"),
        ("go-to-tab-by-id", "2"),
    ]


def test_open_reentry_makes_no_forget_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given origin==destination, return early with no forget and no switch."""
    _enable_primary(monkeypatch)
    calls: list[list[str]] = []
    _install_open(monkeypatch, calls, {
        ("list-panes", "--json"): (PANES, 0, ""),
        ("list-tabs", "--json"): (TABS_REENTRY, 0, ""),
        FORGET_TAIL: ("ok:FORGOT CLIENT=1", 0, ""),
    })
    navigation.open_header("settings")
    assert all("pipe" not in command for command in calls)
    assert all("rename-tab-by-id" not in command for command in calls)
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_open_forget_rejection_aborts_without_rename_or_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given a broker err: on forget, abort before the suffix rename/switch."""
    _enable_primary(monkeypatch)
    calls: list[list[str]] = []
    _install_open(monkeypatch, calls, {
        ("list-panes", "--json"): (PANES, 0, ""),
        ("list-tabs", "--json"): (TABS_BARE, 0, ""),
        FORGET_TAIL: ("err:UNKNOWN CLIENT", 0, ""),
    })
    with pytest.raises(PluginError, match="UNKNOWN CLIENT"):
        navigation.open_header("settings")
    assert all("rename-tab-by-id" not in command for command in calls)
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_open_forget_wrong_ok_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given an ok: line for another client, abort: only the exact reply proceeds."""
    _enable_primary(monkeypatch)
    calls: list[list[str]] = []
    _install_open(monkeypatch, calls, {
        ("list-panes", "--json"): (PANES, 0, ""),
        ("list-tabs", "--json"): (TABS_BARE, 0, ""),
        FORGET_TAIL: ("ok:FORGOT CLIENT=2", 0, ""),
    })
    with pytest.raises(PluginError, match="unexpected reply"):
        navigation.open_header("settings")
    assert all("rename-tab-by-id" not in command for command in calls)
    assert all("go-to-tab-by-id" not in command for command in calls)


@pytest.mark.parametrize("reply", ["", "ok:FORGOT CLIENT=1\nok:FORGOT CLIENT=1", "hello"])
def test_open_forget_bad_shape_aborts(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    """Given empty/duplicate/garbage forget replies, abort with no navigation."""
    _enable_primary(monkeypatch)
    calls: list[list[str]] = []
    _install_open(monkeypatch, calls, {
        ("list-panes", "--json"): (PANES, 0, ""),
        ("list-tabs", "--json"): (TABS_BARE, 0, ""),
        FORGET_TAIL: (reply, 0, ""),
    })
    with pytest.raises(PluginError):
        navigation.open_header("settings")
    assert all("rename-tab-by-id" not in command for command in calls)
    assert all("go-to-tab-by-id" not in command for command in calls)


@pytest.mark.parametrize("outcome", [
    subprocess.TimeoutExpired(["zellij"], 5),
    OSError("boom"),
    ("", 1, "There is no active session!"),
])
def test_open_forget_transport_aborts(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    """Given forget transport/timeout failure, abort with no rename/switch."""
    _enable_primary(monkeypatch)
    calls: list[list[str]] = []
    script: dict[tuple[str, ...], object] = {
        ("list-panes", "--json"): (PANES, 0, ""),
        ("list-tabs", "--json"): (TABS_BARE, 0, ""),
        FORGET_TAIL: outcome,
    }
    _install_open(monkeypatch, calls, script)
    with pytest.raises(PluginError, match="forget pipe|Cannot contact"):
        navigation.open_header("settings")
    assert all("rename-tab-by-id" not in command for command in calls)
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_open_secondary_has_no_forget_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given the flag but a secondary header, the legacy path runs with no pipe."""
    _enable_primary(monkeypatch)
    calls: list[list[str]] = []
    _install_open(monkeypatch, calls, {
        ("list-panes", "--json"): (PANES, 0, ""),
        ("list-tabs", "--json"): (TABS_OTHER_BARE, 0, ""),
        FORGET_TAIL: ("ok:FORGOT CLIENT=1", 0, ""),
    })
    navigation.open_header("other")
    tails = [tuple(command[2:]) for command in calls]
    assert tails == [
        ("list-clients",),
        ("focus-last-pane",),
        ("list-clients",),
        ("list-panes", "--json"),
        ("list-tabs", "--json"),
        ("rename-tab-by-id", "2", "tdnext-header:other@0:terminal_7"),
        ("go-to-tab-by-id", "2"),
    ]


def test_open_flag_off_has_exact_old_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given no flag, open is exactly the original sequence even with the header env set."""
    monkeypatch.delenv("TDNEXT_NAVIGATOR_BROKER", raising=False)
    monkeypatch.setenv("TDNEXT_NAVIGATOR_HEADER", "settings")
    calls: list[list[str]] = []
    _install_open(monkeypatch, calls, {
        ("list-panes", "--json"): (PANES, 0, ""),
        ("list-tabs", "--json"): (TABS_BARE, 0, ""),
        FORGET_TAIL: ("ok:FORGOT CLIENT=1", 0, ""),
    })
    navigation.open_header("settings")
    tails = [tuple(command[2:]) for command in calls]
    assert tails == [
        ("list-clients",),
        ("focus-last-pane",),
        ("list-clients",),
        ("list-panes", "--json"),
        ("list-tabs", "--json"),
        ("rename-tab-by-id", "2", "tdnext-header:settings@0:terminal_7"),
        ("go-to-tab-by-id", "2"),
    ]


def test_back_exact_no_origin_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given the exact NO ORIGIN verdict for the selected client, use the suffix path."""
    _enable_primary(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        BACK_TAIL: ("err:NO ORIGIN FOR CLIENT=1", 0, ""),
        ("list-tabs", "--json"): (TABS_SUFFIX, 0, ""),
        ("list-panes", "--json"): (PANES, 0, ""),
    })
    navigation.back_from_header("settings")
    assert ["go-to-tab-by-id", "0"] in [command[2:] for command in calls]


def test_back_other_client_no_origin_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given NO ORIGIN for another client, fail closed with no suffix navigation."""
    _enable_primary(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        BACK_TAIL: ("err:NO ORIGIN FOR CLIENT=2", 0, ""),
        ("list-tabs", "--json"): (TABS_SUFFIX, 0, ""),
        ("list-panes", "--json"): (PANES, 0, ""),
    })
    with pytest.raises(PluginError, match="NO ORIGIN FOR CLIENT=2"):
        navigation.back_from_header("settings")
    assert all("go-to-tab-by-id" not in command for command in calls)


@pytest.mark.parametrize("reply", ["", "hello", "err:SOURCE MISMATCH expected=a focused=b"])
def test_back_non_exact_replies_do_not_fall_back(monkeypatch: pytest.MonkeyPatch, reply: str) -> None:
    """Given empty/malformed/other-err replies, fail closed without legacy."""
    _enable_primary(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        BACK_TAIL: (reply, 0, ""),
        ("list-tabs", "--json"): (TABS_SUFFIX, 0, ""),
        ("list-panes", "--json"): (PANES, 0, ""),
    })
    with pytest.raises(PluginError):
        navigation.back_from_header("settings")
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_back_ok_reply_does_not_fall_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given any broker ok:, the claim succeeded: return without legacy navigation."""
    _enable_primary(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        BACK_TAIL: ("ok:RETURN client=1 pane=terminal_7", 0, ""),
        ("list-tabs", "--json"): (TABS_SUFFIX, 0, ""),
        ("list-panes", "--json"): (PANES, 0, ""),
    })
    navigation.back_from_header("settings")
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_back_timeout_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given a back pipe timeout, fail closed: the broker may still have acted."""
    _enable_primary(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        BACK_TAIL: (subprocess.TimeoutExpired(["zellij"], 5)),
    })
    with pytest.raises(PluginError, match="return pipe"):
        navigation.back_from_header("settings")
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_back_secondary_has_no_pipe_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given the flag but a secondary header, Back is the legacy path with no pipe."""
    _enable_primary(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    other_tabs = json.dumps(
        [
            {"tab_id": 0, "name": "Files"},
            {"tab_id": 2, "name": "tdnext-header:other@0:terminal_7"},
        ]
    )
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        ("list-tabs", "--json"): (other_tabs, 0, ""),
        ("list-panes", "--json"): (PANES, 0, ""),
    })
    navigation.back_from_header("other")
    assert all("pipe" not in command for command in calls)
    assert ["go-to-tab-by-id", "0"] in [command[2:] for command in calls]
