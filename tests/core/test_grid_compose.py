"""Unit tests for kinoforge.core.grid.compose (escape + builders)."""

from __future__ import annotations

from pathlib import Path

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
    # tpad MUST be present but with stop_duration=0 for same-length inputs
    # (target_dur 2.5 - input 2.5 = 0). Without the max() guard the composed
    # mp4 would double in length to 5.0 s; live 2026-06-25 Tier-4 fire
    # hit this bug (5.06 s cells → 10.12 s composed grid).
    assert "tpad=stop_mode=clone:stop_duration=0" in graph
    assert "text=strength=0.5" in graph
    assert "xstack=inputs=3" in graph


def test_build_filter_graph_pads_short_clip_to_target_duration() -> None:
    probes = [
        InputProbe(width=512, height=512, fps=16.0, duration=2.0),
        InputProbe(width=512, height=512, fps=16.0, duration=5.0),
    ]
    cells = [LayoutCell(idx=0, caption=None), LayoutCell(idx=1, caption=None)]
    graph = _build_filter_graph(probes=probes, layout=(1, 2), cells=cells)
    # cell 0 must pad +3 s (target 5 - input 2); cell 1 must pad 0.
    assert "tpad=stop_mode=clone:stop_duration=3" in graph
    assert "tpad=stop_mode=clone:stop_duration=0" in graph


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


# ---------------------------------------------------------------------------
# Subprocess-mocked tests for Task 7
# ---------------------------------------------------------------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402

from kinoforge.core.grid.compose import (  # noqa: E402
    _check_ffmpeg,
    compose_grid_mp4,
    probe_inputs,
)
from kinoforge.core.grid.errors import (  # noqa: E402
    FfmpegInvocationError,
    FfmpegNotFoundError,
)


def test_check_ffmpeg_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(FfmpegNotFoundError, match="ffmpeg"):
        _check_ffmpeg()


def test_check_ffmpeg_present_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/ffmpeg" if name in ("ffmpeg", "ffprobe") else None,
    )
    _check_ffmpeg()


def test_probe_inputs_invokes_ffprobe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                '{"streams":[{"width":640,"height":480,'
                '"r_frame_rate":"16/1","duration":"3.0"}]}'
            ),
            stderr="",
        )

    monkeypatch.setattr("kinoforge.core.grid.compose.subprocess.run", fake_run)
    fake_mp4 = tmp_path / "x.mp4"
    fake_mp4.write_bytes(b"\x00" * 1024)

    probes = probe_inputs([fake_mp4])
    assert len(probes) == 1
    assert probes[0].width == 640
    assert probes[0].height == 480
    assert probes[0].fps == 16.0
    assert probes[0].duration == 3.0
    assert captured["cmd"][0] == "ffprobe"
    assert "-show_entries" in captured["cmd"]


def test_compose_grid_mp4_invokes_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:

    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        out_idx = cmd.index("-y") + 1
        Path(cmd[out_idx]).write_bytes(b"composed")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("kinoforge.core.grid.compose.subprocess.run", fake_run)

    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    out = tmp_path / "grid.mp4"

    probes = [
        InputProbe(width=512, height=512, fps=16.0, duration=2.0),
        InputProbe(width=512, height=512, fps=16.0, duration=2.0),
    ]
    cells = [LayoutCell(idx=0, caption="a"), LayoutCell(idx=1, caption="b")]
    compose_grid_mp4(
        inputs=[a, b],
        probes=probes,
        cells=cells,
        layout=(1, 2),
        out_path=out,
    )
    assert out.exists()
    cmd = captured["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-filter_complex" in cmd
    assert "libx264" in cmd
    assert "-an" in cmd
    assert "-y" in cmd


def test_compose_grid_mp4_failure_writes_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="filter parse error at line 3"
        )

    monkeypatch.setattr("kinoforge.core.grid.compose.subprocess.run", fake_run)
    out = tmp_path / "grid.mp4"
    a = tmp_path / "a.mp4"
    a.write_bytes(b"x")
    probe = InputProbe(width=512, height=512, fps=16.0, duration=2.0)
    cell = LayoutCell(idx=0, caption="x")

    with pytest.raises(FfmpegInvocationError, match="filter parse error"):
        compose_grid_mp4(
            inputs=[a],
            probes=[probe],
            cells=[cell],
            layout=(1, 1),
            out_path=out,
        )
    stderr_file = out.with_suffix(out.suffix + ".stderr.txt")
    assert stderr_file.read_text() == "filter parse error at line 3"
