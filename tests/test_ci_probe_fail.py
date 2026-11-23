"""Disposable CI gate probe; reverted immediately after the red run."""


def test_ci_probe_intentional_failure() -> None:
    expected = 1
    actual = 2
    assert expected == actual, "ci probe: intentional failure"
