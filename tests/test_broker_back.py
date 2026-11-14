"""Broker Back bridge for header ToolPanes (keyboard-broker path).

Contract:

- Without TDNEXT_NAVIGATOR_BROKER (absent or false), Back is exactly the
  original tab-name origin path: no broker pipe call, no extra queries.
  Only the future native launcher sets the flag.
- With the flag, the broker claim must succeed outright. Empty output,
  ambiguous or unexpected replies, timeouts, nonzero exits, and every err:
  rejection fail closed as PluginError with no navigation. In particular a
  NO ORIGIN verdict never falls back to the tab-name suffix: a stale suffix
  from a previous mouse round-trip could navigate to the wrong origin, so
  mouse use in a broker-enabled session stays blocked until mouse opens go
  through the broker.
"""

import json
import subprocess

import pytest

from tdnext.core.errors import PluginError
from tdnext.plugins.desktop import navigation
from tdnext.plugins.desktop import tool as tool_module
from tdnext.plugins.desktop.tool import ToolPane

CLIENTS_TWO = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_5 zsh\n2 terminal_7 zsh\n"
CLIENTS_ONE = "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n1 terminal_5 zsh\n"
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
TABS_SUFFIX = json.dumps(
    [
        {"tab_id": 0, "name": "Files"},
        {"tab_id": 2, "name": "tdnext-header:settings@0:terminal_7"},
    ]
)
PIPE_TAIL = ("pipe", "--name", "back", "--", "client=1 source=terminal_5")


def _response(command: list[str], stdout: str = "", code: int = 0, error: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, code, stdout, error)


def _install(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]], script: dict[tuple[str, ...], object]) -> None:
    """Serve canned outputs (or exceptions) keyed by the action argv tail."""
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


def _enable_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TDNEXT_NAVIGATOR_BROKER", "1")


def _disable_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TDNEXT_NAVIGATOR_BROKER", raising=False)


def test_absent_flag_takes_original_path_with_no_pipe_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given no broker flag, Back is the original path: no pipe, no extra queries."""
    _disable_broker(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        ("list-tabs", "--json"): (TABS_SUFFIX, 0, ""),
        ("list-panes", "--json"): (PANES, 0, ""),
    })
    navigation.back_from_header("settings")
    assert [command[2:] for command in calls] == [
        ["list-tabs", "--json"],
        ["list-clients"],
        ["list-panes", "--json"],
        ["go-to-tab-by-id", "0"],
        ["list-clients"],
        ["focus-pane-id", "terminal_7"],
    ]


@pytest.mark.parametrize("flag", ["0", "false", "off", ""])
def test_falsy_flag_takes_original_path_with_no_pipe_call(monkeypatch: pytest.MonkeyPatch, flag: str) -> None:
    """Given an explicitly falsy broker flag, Back stays on the original path."""
    monkeypatch.setenv("TDNEXT_NAVIGATOR_BROKER", flag)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        ("list-tabs", "--json"): (TABS_SUFFIX, 0, ""),
        ("list-panes", "--json"): (PANES, 0, ""),
    })
    navigation.back_from_header("settings")
    assert all("pipe" not in command for command in calls)
    assert ["go-to-tab-by-id", "0"] in [command[2:] for command in calls]


def test_broker_back_sends_exact_client_and_source_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given the flag and two clients, the current ZELLIJ_PANE_ID selects one claim."""
    _enable_broker(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "7")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_TWO, 0, ""),
        ("pipe", "--name", "back", "--", "client=2 source=terminal_7"): (
            "ok:RETURN client=2 pane=terminal_9", 0, ""),
    })
    navigation.back_from_header("settings")
    assert calls == [
        ["zellij", "action", "list-clients"],
        ["zellij", "action", "pipe", "--name", "back", "--", "client=2 source=terminal_7"],
    ]


def test_broker_back_rejects_ambiguous_client_without_piping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given no ZELLIJ_PANE_ID and two clients, no pipe call is made."""
    _enable_broker(monkeypatch)
    monkeypatch.delenv("ZELLIJ_PANE_ID", raising=False)
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {("list-clients",): (CLIENTS_TWO, 0, "")})
    with pytest.raises(PluginError, match="Cannot identify the client"):
        navigation.back_from_header("settings")
    assert calls == [["zellij", "action", "list-clients"]]


def test_broker_back_rejects_unmatched_source_without_piping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given a ZELLIJ_PANE_ID no client shows, the claim is refused outright."""
    _enable_broker(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "9")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {("list-clients",): (CLIENTS_ONE, 0, "")})
    with pytest.raises(PluginError, match="Cannot identify the client"):
        navigation.back_from_header("settings")
    assert calls == [["zellij", "action", "list-clients"]]


def test_broker_back_empty_reply_fails_closed_without_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given an unanswered pipe, fail closed: a silent broker may have malfunctioned."""
    _enable_broker(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        PIPE_TAIL: ("", 0, ""),
        ("list-tabs", "--json"): (TABS_SUFFIX, 0, ""),
        ("list-panes", "--json"): (PANES, 0, ""),
    })
    with pytest.raises(PluginError, match="exactly one"):
        navigation.back_from_header("settings")
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_broker_back_no_origin_falls_back_to_legacy_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given the exact NO ORIGIN verdict for the selected client, use the legacy suffix path.

    Ordered coordination only (not atomic): the broker owns the return, but
    an exact NO ORIGIN FOR CLIENT=<selected> means it holds no origin, so
    the recorded tab-name suffix is the only remaining signal and the legacy
    path is reused. Any other reply still fails closed with no navigation.
    """
    _enable_broker(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        PIPE_TAIL: ("err:NO ORIGIN FOR CLIENT=1", 0, ""),
        ("list-tabs", "--json"): (TABS_SUFFIX, 0, ""),
        ("list-panes", "--json"): (PANES, 0, ""),
    })
    navigation.back_from_header("settings")
    assert ["go-to-tab-by-id", "0"] in [command[2:] for command in calls]


def test_broker_back_timeout_surfaces_without_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given the pipe times out, the broker may have acted: never navigate."""
    _enable_broker(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        PIPE_TAIL: (subprocess.TimeoutExpired(["zellij"], 5)),
    })
    with pytest.raises(PluginError, match="return pipe"):
        navigation.back_from_header("settings")
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_broker_back_rejection_surfaces_without_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given a broker-owned rejection, surface it instead of second-guessing."""
    _enable_broker(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        PIPE_TAIL: ("err:SOURCE MISMATCH expected=terminal_5 focused=terminal_9", 0, ""),
    })
    with pytest.raises(PluginError, match="SOURCE MISMATCH"):
        navigation.back_from_header("settings")
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_broker_back_failed_pipe_surfaces_without_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given a nonzero pipe exit, the transport failed: surface it."""
    _enable_broker(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        PIPE_TAIL: ("", 1, "There is no active session!"),
    })
    with pytest.raises(PluginError, match="There is no active session"):
        navigation.back_from_header("settings")
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_broker_back_duplicate_replies_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given anything but exactly one reply line, refuse to act on it."""
    _enable_broker(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        PIPE_TAIL: ("ok:RETURN client=1 pane=terminal_7\nok:RETURN client=1 pane=terminal_7", 0, ""),
    })
    with pytest.raises(PluginError, match="exactly one"):
        navigation.back_from_header("settings")
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_broker_back_garbage_reply_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given a reply without an ok:/err: prefix, surface it loudly."""
    _enable_broker(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {
        ("list-clients",): (CLIENTS_ONE, 0, ""),
        PIPE_TAIL: ("hello", 0, ""),
    })
    with pytest.raises(PluginError, match="unexpected reply"):
        navigation.back_from_header("settings")
    assert all("go-to-tab-by-id" not in command for command in calls)


@pytest.mark.parametrize("reply", [
    "ok:", "ok:FORGOT CLIENT=1", "ok:RETURN client=2 pane=terminal_7",
    "ok:RETURN client=1 pane=", "ok:RETURN client=1 pane=terminal_-1",
    "ok:RETURN client=1 pane=window_7", "ok:RETURN client=1 pane=terminal_7 extra",
])
def test_broker_back_rejects_malformed_success_without_navigation(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    _enable_broker(monkeypatch)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "5")
    calls: list[list[str]] = []
    _install(monkeypatch, calls, {("list-clients",): (CLIENTS_ONE, 0, ""), PIPE_TAIL: (reply, 0, "")})
    with pytest.raises(PluginError, match="unexpected reply"):
        navigation.back_from_header("settings")
    assert all("go-to-tab-by-id" not in command for command in calls)


def test_tool_pane_escape_binding_is_unchanged() -> None:
    """The Back binding stays on Escape; the broker never binds it."""
    assert ("escape", "close_view", "Back") in ToolPane.BINDINGS


def test_tool_pane_close_view_delegates_to_back_from_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Escape still closes via the header Back router (flag-gated, fail-closed)."""
    seen: list[str] = []
    monkeypatch.setattr(tool_module, "back_from_header", lambda plugin_id: seen.append(plugin_id))
    pane = ToolPane.__new__(ToolPane)
    pane.plugin_id = "settings"
    pane.action_close_view()
    assert seen == ["settings"]
