"""Regression: PluginError must survive contextlib traceback assignment."""

import contextlib

from tdnext.core.errors import PluginError


@contextlib.contextmanager
def passthrough():
    yield


def test_plugin_error_keeps_detail_and_message() -> None:
    err = PluginError("missing dependency: foo")
    assert err.detail == "missing dependency: foo"
    assert str(err) == "missing dependency: foo"
    assert err.args == ("missing dependency: foo",)


def test_plugin_error_propagates_through_contextmanager() -> None:
    try:
        with passthrough():
            raise PluginError("missing dependency: foo")
    except PluginError as exc:
        assert exc.detail == "missing dependency: foo"
        assert exc.__traceback__ is not None
        return
    raise AssertionError("PluginError was masked passing through contextlib")


def test_plugin_error_supports_traceback_cause_and_notes() -> None:
    err = PluginError("dep failed")
    err.__traceback__ = None
    cause = ValueError("root")
    err.__cause__ = cause
    err.__context__ = cause
    err.add_note("while resolving plugins")
    assert err.__cause__ is cause
    assert err.__notes__ == ["while resolving plugins"]
