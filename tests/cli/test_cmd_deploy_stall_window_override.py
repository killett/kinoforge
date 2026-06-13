"""C26 Task 10: --stall-window-override flag on `kinoforge deploy`."""

from __future__ import annotations

import subprocess
import sys

from kinoforge.cli._main import _build_parser, _nonnegative_float


def test_stall_window_override_parses_positive_float() -> None:
    """Argparse accepts a positive float and binds it to the namespace."""
    parser = _build_parser()
    ns = parser.parse_args(
        ["deploy", "--config", "x.yaml", "--stall-window-override", "1800"]
    )
    assert ns.stall_window_override == 1800.0


def test_stall_window_override_defaults_to_none_when_omitted() -> None:
    """Omitting the flag leaves the attr at None."""
    parser = _build_parser()
    ns = parser.parse_args(["deploy", "--config", "x.yaml"])
    assert ns.stall_window_override is None


def test_stall_window_override_rejects_negative_value_at_parse() -> None:
    """argparse exits rc=2 with a clear error when value is negative."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kinoforge",
            "deploy",
            "--config",
            "nonexistent.yaml",
            "--stall-window-override",
            "-1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "must be >= 0" in result.stderr


def test_nonnegative_float_helper_accepts_zero() -> None:
    assert _nonnegative_float("0") == 0.0


def test_nonnegative_float_helper_rejects_non_float() -> None:
    import argparse

    import pytest

    with pytest.raises(argparse.ArgumentTypeError, match="is not a float"):
        _nonnegative_float("not-a-number")
