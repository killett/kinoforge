"""C27 Task 9: --restart-loop-window-override flag on `kinoforge deploy`."""

from __future__ import annotations

import subprocess
import sys

from kinoforge.cli._main import _build_parser


def test_restart_loop_window_override_parses_positive_float() -> None:
    """Argparse accepts a positive float and binds it to the namespace."""
    parser = _build_parser()
    ns = parser.parse_args(
        ["deploy", "--config", "x.yaml", "--restart-loop-window-override", "240"]
    )
    assert ns.restart_loop_window_override == 240.0


def test_restart_loop_window_override_defaults_to_none_when_omitted() -> None:
    """Omitting the flag leaves the attr at None — no override persisted."""
    parser = _build_parser()
    ns = parser.parse_args(["deploy", "--config", "x.yaml"])
    assert ns.restart_loop_window_override is None


def test_restart_loop_window_override_rejects_negative_value_at_parse() -> None:
    """argparse exits rc=2 with a clear error when value is negative."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kinoforge",
            "deploy",
            "--config",
            "nonexistent.yaml",
            "--restart-loop-window-override",
            "-1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "must be >= 0" in result.stderr


def test_restart_loop_window_override_independent_of_stall_window_override() -> None:
    """Both flags coexist; setting one does not bind the other."""
    parser = _build_parser()
    ns = parser.parse_args(
        [
            "deploy",
            "--config",
            "x.yaml",
            "--restart-loop-window-override",
            "300",
        ]
    )
    assert ns.restart_loop_window_override == 300.0
    assert ns.stall_window_override is None
