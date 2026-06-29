"""Tests for the ExtrasNotInstalled error class."""

from __future__ import annotations

from kinoforge.core.errors import ExtrasNotInstalled, KinoforgeError


def test_is_kinoforge_error_subclass() -> None:
    # Bug caught: someone defines ExtrasNotInstalled as a bare Exception,
    # breaking `except KinoforgeError` catchers in the orchestrator.
    assert issubclass(ExtrasNotInstalled, KinoforgeError)


def test_str_includes_extras_name_and_hint() -> None:
    # Bug caught: error message drops the install_hint and the operator
    # sees only "kinoforge[seedvr] extras not installed" with no
    # remediation guidance.
    err = ExtrasNotInstalled(
        extras_name="seedvr",
        install_hint="vendoring lands in Phase 2",
    )
    msg = str(err)
    assert "seedvr" in msg
    assert "vendoring lands in Phase 2" in msg


def test_attributes_accessible_for_programmatic_handling() -> None:
    # Bug caught: caller wants to log only the extras_name or branch
    # on it but the attributes were never stored on self.
    err = ExtrasNotInstalled(extras_name="seedvr", install_hint="install with X")
    assert err.extras_name == "seedvr"
    assert err.install_hint == "install with X"
