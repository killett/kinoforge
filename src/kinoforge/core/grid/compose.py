"""Grid composition — ffmpeg subprocess shell-out, no Python bindings.

The drawtext filter's special-char escaping is the bug-magnet: un-escaped
``:`` truncates the caption at the first colon (silent mis-parse, no
warning). This module owns the escape contract.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kinoforge.core.grid.errors import FfmpegInvocationError, FfmpegNotFoundError

_DRAWTEXT_ESCAPED = {
    "\\": r"\\",
    ":": r"\:",
    "'": r"\'",
    "%": r"\%",
    "\n": r"\n",
}


def _escape_drawtext(s: str) -> str:
    r"""Escape special chars for ffmpeg ``drawtext`` filter ``text=`` arg.

    The drawtext filter parses ``:`` as an option separator and ``\`` as
    an escape introducer, so un-escaped values silently corrupt the caption
    (e.g. ``"strength=0.5"`` truncates to ``"strength=0"``).

    Args:
        s: Raw caption string from the user's grid spec.

    Returns:
        ``s`` with every special char replaced by its escaped form.
        Backslash MUST be processed first to avoid double-escaping the
        escapes inserted for the other chars.
    """
    out = s.replace("\\", _DRAWTEXT_ESCAPED["\\"])
    for ch in (":", "'", "%", "\n"):
        out = out.replace(ch, _DRAWTEXT_ESCAPED[ch])
    return out


@dataclass(frozen=True)
class InputProbe:
    """ffprobe output for one input mp4."""

    width: int
    height: int
    fps: float
    duration: float


@dataclass(frozen=True)
class LayoutCell:
    """Caption + index of one cell in render order."""

    idx: int
    caption: str | None


def _resolve_layout(layout: str, *, n: int) -> tuple[int, int]:
    """Return ``(rows, cols)`` for the requested layout vs N cells.

    Args:
        layout: ``'RxC'`` literal or ``'auto'`` for sqrt+ceil.
        n: Number of cells to fit.

    Returns:
        ``(rows, cols)`` with ``rows*cols >= n``.

    Raises:
        ValueError: ``layout`` is explicit and ``rows*cols < n``.
    """
    if layout == "auto":
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        return rows, cols
    r_s, c_s = layout.split("x", 1)
    r, c = int(r_s), int(c_s)
    if r * c < n:
        raise ValueError(f"layout {layout!r}: R*C={r * c} < N={n}")
    return r, c


def _build_filter_graph(
    *,
    probes: list[InputProbe],
    layout: tuple[int, int],
    cells: list[LayoutCell],
) -> str:
    """Construct the ``-filter_complex`` value for the ffmpeg invocation.

    Per-input chain (one for every cell):
    ``[N:v] scale=W:H,pad=W:H:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=F,
    tpad=stop_mode=clone:stop_duration=D[,drawtext=...][vN]``.

    Followed by ``xstack=inputs=N:layout=...:fill=black`` to stitch.

    Args:
        probes: ffprobe output per input mp4. Same order as ``cells``.
        layout: ``(rows, cols)`` from :func:`_resolve_layout`.
        cells: Caption + idx per cell. Must match ``len(probes)``.

    Returns:
        The filter-graph string ready to pass to ``ffmpeg -filter_complex``.

    Raises:
        ValueError: ``cells`` empty or count mismatch with ``probes``.
    """
    if not cells:
        raise ValueError("at least one cell required to build filter graph")
    if len(cells) != len(probes):
        raise ValueError(
            f"cells/probes length mismatch: cells={len(cells)} probes={len(probes)}"
        )

    target_w = min(p.width for p in probes)
    target_h = min(p.height for p in probes)
    target_fps = max(p.fps for p in probes)
    target_dur = max(p.duration for p in probes)

    _, cols = layout
    n = len(cells)
    chains: list[str] = []
    for i, (probe, cell) in enumerate(zip(probes, cells, strict=True)):
        # tpad ADDS stop_duration seconds AFTER the original clip
        # (not "pad to duration"). So the per-input padding is
        # max(0, target_dur - this_input_dur), which is 0 for clips
        # already at the target length. Without the max() the
        # composed mp4 doubles in length when all inputs are the same
        # duration (observed live 2026-06-25 — Tier-4 5.06s cells
        # composed to 10.12s).
        pad_s = max(0.0, target_dur - probe.duration)
        chain = (
            f"[{i}:v]"
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={target_fps:g},"
            f"tpad=stop_mode=clone:stop_duration={pad_s:g}"
        )
        if cell.caption:
            esc = _escape_drawtext(cell.caption)
            chain += (
                f",drawtext=text={esc}:fontcolor=white:fontsize=h*0.05:"
                f"box=1:boxcolor=black@0.5:boxborderw=8:"
                f"x=(w-text_w)/2:y=20"
            )
        chain += f"[v{i}]"
        chains.append(chain)

    positions: list[str] = []
    for cell_i in range(n):
        r = cell_i // cols
        c = cell_i % cols
        x = "0" if c == 0 else "+".join(f"w{j}" for j in range(c))
        y = "0" if r == 0 else "+".join(f"h{j * cols}" for j in range(r))
        positions.append(f"{x}_{y}")
    layout_arg = "|".join(positions)
    inputs_chain = "".join(f"[v{i}]" for i in range(n))
    xstack = f"{inputs_chain}xstack=inputs={n}:layout={layout_arg}:fill=black[outv]"

    return ";".join(chains + [xstack])


def _check_ffmpeg() -> None:
    """Verify ``ffmpeg`` and ``ffprobe`` are on PATH; raise loud otherwise.

    Raises:
        FfmpegNotFoundError: Either binary missing.
    """
    for bin_name in ("ffmpeg", "ffprobe"):
        if shutil.which(bin_name) is None:
            raise FfmpegNotFoundError(
                f"{bin_name} not found on PATH. Install via "
                f"`pixi install` (ffmpeg pinned in pixi.toml) or "
                f"`apt-get install ffmpeg`."
            )


def _parse_fps(rate: str) -> float:
    """Parse ffprobe ``r_frame_rate`` (``'16/1'`` form) into float."""
    if "/" in rate:
        num, den = rate.split("/", 1)
        return float(num) / float(den) if float(den) != 0 else 0.0
    return float(rate)


def probe_inputs(paths: list[Path]) -> list[InputProbe]:
    """Run ffprobe on each path; return one :class:`InputProbe` per input.

    Args:
        paths: Resolved mp4 paths.

    Returns:
        Probes in the same order as ``paths``.

    Raises:
        FfmpegInvocationError: ffprobe exits non-zero or returns malformed JSON.
    """
    probes: list[InputProbe] = []
    for p in paths:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate,duration",
            "-of",
            "json",
            str(p),
        ]
        result = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise FfmpegInvocationError(
                f"ffprobe {p}: exit={result.returncode} stderr={result.stderr.strip()}"
            )
        try:
            data = json.loads(result.stdout)
            stream = data["streams"][0]
            probes.append(
                InputProbe(
                    width=int(stream["width"]),
                    height=int(stream["height"]),
                    fps=_parse_fps(stream["r_frame_rate"]),
                    duration=float(stream["duration"]),
                )
            )
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            raise FfmpegInvocationError(
                f"ffprobe {p}: malformed output: {result.stdout!r}"
            ) from e
    return probes


def compose_grid_mp4(
    *,
    inputs: list[Path],
    probes: list[InputProbe],
    cells: list[LayoutCell],
    layout: tuple[int, int],
    out_path: Path,
) -> None:
    """Compose ``inputs`` into one grid mp4 at ``out_path``.

    Args:
        inputs: Per-cell mp4 paths, same order as ``probes`` and ``cells``.
        probes: ffprobe results for each input.
        cells: Caption + idx per cell.
        layout: ``(rows, cols)`` from :func:`_resolve_layout`.
        out_path: Where to write the composed mp4. ``-y`` flag overwrites.

    Raises:
        FfmpegInvocationError: ffmpeg exits non-zero. Stderr written to
            ``<out_path>.stderr.txt`` for the executor's pickup. The
            stderr file is the only known-binary-free artifact written
            outside the output dir exempt zone (allow-listed in the
            AST-scan extension shipped in Task 11).
    """
    graph = _build_filter_graph(probes=probes, layout=layout, cells=cells)
    cmd: list[str] = ["ffmpeg"]
    for p in inputs:
        cmd += ["-i", str(p)]
    cmd += [
        "-filter_complex",
        graph,
        "-map",
        "[outv]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-an",
        "-y",
        str(out_path),
    ]
    result = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        stderr_path = out_path.with_suffix(out_path.suffix + ".stderr.txt")
        stderr_path.write_text(  # kinoforge:public-write
            result.stderr,
        )
        raise FfmpegInvocationError(
            f"ffmpeg exit={result.returncode}: {result.stderr.strip()[:500]}"
        )
