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


from kinoforge.core.grid.compose import (  # noqa: E402
    InputProbe,
    LayoutCell,
    _build_filter_graph,
    _resolve_layout,
)


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, (1, 1)),
        (2, (1, 2)),
        (3, (2, 2)),
        (4, (2, 2)),
        (5, (2, 3)),
        (6, (2, 3)),
        (9, (3, 3)),
    ],
)
def test_resolve_layout_auto(n: int, expected: tuple[int, int]) -> None:
    assert _resolve_layout("auto", n=n) == expected


def test_resolve_layout_explicit_ok() -> None:
    assert _resolve_layout("2x3", n=5) == (2, 3)


def test_resolve_layout_explicit_too_small_raises() -> None:
    with pytest.raises(ValueError, match=r"R\*C=2 < N=3"):
        _resolve_layout("1x2", n=3)


def test_build_filter_graph_includes_per_cell_chain() -> None:
    probes = [
        InputProbe(width=512, height=512, fps=16.0, duration=2.5),
        InputProbe(width=512, height=512, fps=16.0, duration=2.5),
        InputProbe(width=512, height=512, fps=16.0, duration=2.5),
    ]
    cells = [
        LayoutCell(idx=0, caption="strength=0.5"),
        LayoutCell(idx=1, caption="strength=1.0"),
        LayoutCell(idx=2, caption="strength=1.5"),
    ]
    graph = _build_filter_graph(probes=probes, layout=(1, 3), cells=cells)
    assert "scale=512:512" in graph
    assert "tpad=stop_mode=clone" in graph
    # Caption escape: '=' is fine, but the ':' option separator MUST escape.
    # Caption "strength=0.5" has no ':' so the text= field renders verbatim.
    assert "text=strength=0.5" in graph
    assert "xstack=inputs=3" in graph


def test_build_filter_graph_caption_with_colon_is_escaped() -> None:
    probes = [InputProbe(width=512, height=512, fps=16.0, duration=2.0)]
    cells = [LayoutCell(idx=0, caption="strength:0.5")]
    graph = _build_filter_graph(probes=probes, layout=(1, 1), cells=cells)
    # The colon in the caption MUST be escaped so drawtext does not eat it.
    assert r"text=strength\:0.5" in graph


def test_build_filter_graph_empty_cells_raises() -> None:
    with pytest.raises(ValueError, match="at least one cell"):
        _build_filter_graph(probes=[], layout=(1, 1), cells=[])


def test_build_filter_graph_caption_omitted_when_none() -> None:
    probes = [InputProbe(width=512, height=512, fps=16.0, duration=2.0)]
    cells = [LayoutCell(idx=0, caption=None)]
    graph = _build_filter_graph(probes=probes, layout=(1, 1), cells=cells)
    assert "drawtext" not in graph, "no caption → no drawtext filter"
