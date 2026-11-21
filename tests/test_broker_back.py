"""Return-to-origin Back contract (current boundary).

Retired: WASM navigator-broker pipe protocol, TDNEXT_NAVIGATOR_BROKER /
TDNEXT_NAVIGATOR_HEADER flag gating, forget handshake, NO ORIGIN fallback to
the tab-name suffix path, legacy open_header/return_to_origin query
sequences, and the Python probe/packaging of the patched core. The Rust
launcher plus its matching patched core own session setup; Python only asks
the running core to return once.

Retained: ``navigation.back_from_header()`` issues exactly one
``return-to-origin`` action via ``TDNEXT_ZELLIJ`` and surfaces transport or
core failures as ``PluginError`` with no fallback navigation. The header
ToolPane keeps Back on Escape and delegates through that call.
"""

import subprocess

import pytest

from tdnext.core.errors import PluginError
from tdnext.plugins.desktop import navigation
from tdnext.plugins.desktop import tool as tool_module
from tdnext.plugins.desktop.tool import ToolPane


def _install(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[list[str]],
    outcome: object = ("", 0, ""),
) -> None:
    monkeypatch.setenv("TDNEXT_ZELLIJ", "zellij")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, tuple)
        stdout, code, error = outcome
        return subprocess.CompletedProcess(command, code, stdout, error)

    monkeypatch.setattr(navigation.subprocess, "run", run)


def test_back_calls_return_to_origin_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Back issues exactly one return-to-origin action and returns cleanly."""
    calls: list[list[str]] = []
    _install(monkeypatch, calls)
    navigation.back_from_header()
    assert calls == [["zellij", "action", "return-to-origin"]]


def test_back_failure_surfaces_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A nonzero core exit surfaces with no second attempt."""
    calls: list[list[str]] = []
    _install(monkeypatch, calls, ("", 1, "gone"))
    with pytest.raises(PluginError, match="gone"):
        navigation.back_from_header()
    assert calls == [["zellij", "action", "return-to-origin"]]


@pytest.mark.parametrize(
    "outcome",
    [subprocess.TimeoutExpired(["zellij"], 5), OSError("boom"), KeyError("TDNEXT_ZELLIJ")],
)
def test_back_transport_surfaces_without_navigation(
    monkeypatch: pytest.MonkeyPatch, outcome: object
) -> None:
    """Timeout, transport, and missing-binary failures surface as PluginError."""
    calls: list[list[str]] = []
    if isinstance(outcome, KeyError):
        monkeypatch.delenv("TDNEXT_ZELLIJ", raising=False)
        monkeypatch.setattr(
            navigation.subprocess,
            "run",
            lambda command, **_kwargs: (_ for _ in ()).throw(AssertionError(f"must not run: {command}")),
        )
    else:
        _install(monkeypatch, calls, outcome)
    with pytest.raises(PluginError, match="Cannot control Zellij"):
        navigation.back_from_header()


def test_tool_pane_escape_binding_is_unchanged() -> None:
    """The Back binding stays on Escape; no broker binding exists."""
    assert ("escape", "close_view", "Back") in ToolPane.BINDINGS


def test_tool_pane_close_view_delegates_to_back_from_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Escape still closes via the header Back router with no arguments."""
    seen: list[bool] = []
    monkeypatch.setattr(tool_module, "back_from_header", lambda: seen.append(True))
    pane = ToolPane.__new__(ToolPane)
    pane.action_close_view()
    assert seen == [True]
