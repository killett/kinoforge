"""Unit tests for kinoforge.core.grid.compose (escape + builders)."""

from __future__ import annotations

import pytest

from kinoforge.core.grid.compose import _escape_drawtext


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("plain", "plain"),
        ("", ""),
        ("a:b", r"a\:b"),
        ("a'b", r"a\'b"),
        ("a%b", r"a\%b"),
        ("a\\b", r"a\\b"),
        ("a:b'c%d\\e", r"a\:b\'c\%d\\e"),
        ("café", "café"),
        ("line1\nline2", r"line1\nline2"),
    ],
)
def test_escape_drawtext(raw: str, expected: str) -> None:
    # Bug: ffmpeg drawtext silently truncates at first un-escaped ':' —
    # caption "strength=0.5" would render as "strength=0" without escape.
    assert _escape_drawtext(raw) == expected
