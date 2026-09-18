"""Spatial tiling for wide-source upscales.

FlashVSR's attention canvas is its 4x OUTPUT: a 960x544 source means a
3840x2176 canvas whose first streaming window needs a ~40 GiB block mask on a
~69 GiB baseline — no Modal card holds it, whatever the clip length
(2026-09-18, five attempts). Tiles whose 4x canvas fits the proven token window
are upscaled one at a time on the same pod and feather-blended back together on
the controller.

Contract (every number a test hand-derives from it): per axis with ``n`` tiles
and a requested ``overlap``, the tile size is ``ceil((dim + (n-1)*overlap)/n)``
rounded UP to the 32-px alignment the block-sparse attention window demands
(``Dims must divide by window size`` otherwise); tiles are spread evenly so the
last one ends exactly at the canvas edge, offsets are even (chroma-safe), and
the size is bumped by 32 until every neighbouring pair overlaps by at least the
requested amount. Feather ramps span the ACTUAL overlap between neighbours,
which is why the weights are derived from the whole tile list.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

_ALIGN = 32


@dataclass(frozen=True)
class TileSpec:
    """One source-pixel crop box.

    Attributes:
        x: Left edge (even).
        y: Top edge (even).
        w: Width, a multiple of 32.
        h: Height, a multiple of 32.
    """

    x: int
    y: int
    w: int
    h: int


def _axis(dim: int, n: int, overlap: int) -> tuple[int, list[int]]:
    """Tile size and offsets along one axis per the module contract."""
    if n == 1:
        if dim % _ALIGN:
            raise ValueError(
                f"a single tile must be a multiple of {_ALIGN} px; got {dim} "
                "(FlashVSR's attention window must divide the latent grid)"
            )
        return dim, [0]
    tile = -(-(dim + (n - 1) * overlap) // n)
    tile = -(-tile // _ALIGN) * _ALIGN
    while True:
        if tile >= dim:
            raise ValueError(
                f"{n} tiles with overlap {overlap} cannot split {dim} px at "
                f"{_ALIGN}-px alignment; use fewer tiles or less overlap"
            )
        span = dim - tile
        positions = [int(span * i / (n - 1)) for i in range(n)]
        positions = [p - p % 2 for p in positions]
        positions[-1] = span
        if all(
            a + tile - b >= overlap
            for a, b in zip(positions, positions[1:], strict=False)
        ):
            return tile, positions
        tile += _ALIGN


def plan_tiles(
    width: int, height: int, *, cols: int, rows: int, overlap: int
) -> list[TileSpec]:
    """Split a ``width x height`` canvas into a ``cols x rows`` grid of tiles.

    Args:
        width: Source width in pixels (even).
        height: Source height in pixels (even).
        cols: Tiles across; positive.
        rows: Tiles down; positive.
        overlap: Minimum overlap between neighbours, in source pixels; >= 0.

    Returns:
        Tiles in row-major order.

    Raises:
        ValueError: Degenerate grid, negative overlap, or an axis that cannot
            be split at 32-px alignment (including a 1x1 grid over an
            unaligned canvas).
    """
    if cols < 1 or rows < 1:
        raise ValueError(f"cols and rows must be positive, got {cols}x{rows}")
    if overlap < 0:
        raise ValueError(f"overlap must be >= 0, got {overlap}")
    tw, xs = _axis(width, cols, overlap)
    th, ys = _axis(height, rows, overlap)
    return [TileSpec(x=x, y=y, w=tw, h=th) for y in ys for x in xs]


def crop_argv(src_path: str, tile: TileSpec, out_path: str) -> list[str]:
    """Build the ffmpeg argv that cuts ``tile`` out of ``src_path`` losslessly.

    Args:
        src_path: Local source clip.
        tile: The crop box.
        out_path: Destination mp4.

    Returns:
        The argv; ``out_path`` is always its last token.
    """
    return [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        src_path,
        "-vf",
        f"crop={tile.w}:{tile.h}:{tile.x}:{tile.y}",
        "-an",
        "-c:v",
        "libx264",
        "-qp",
        "0",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        out_path,
    ]


def _ramp(n: int, *, rising: bool) -> np.ndarray:
    r = np.arange(n, dtype=np.float64) / n
    return r if rising else 1.0 - r


def tile_weights(
    tiles: Sequence[TileSpec], *, canvas_w: int, canvas_h: int
) -> list[np.ndarray]:
    """Feather weights for each tile, ``(h, w)`` float64 in ``[0, 1]``.

    Interior edges ramp linearly across the ACTUAL overlap with the neighbour
    (rising 0 -> 1 on a tile's left/top edge, falling 1 -> 0 on its right/
    bottom edge), so the weights of overlapping tiles sum to exactly 1;
    canvas borders keep full weight.

    Args:
        tiles: A regular grid from :func:`plan_tiles` (shared size, aligned
            rows and columns).
        canvas_w: Source width.
        canvas_h: Source height.

    Returns:
        One weight map per tile, in the same order.
    """
    xs = sorted({t.x for t in tiles})
    ys = sorted({t.y for t in tiles})
    del canvas_w, canvas_h  # borders are implied by the grid's first/last offsets
    out: list[np.ndarray] = []
    for t in tiles:
        wx = np.ones(t.w, dtype=np.float64)
        wy = np.ones(t.h, dtype=np.float64)
        ci, ri = xs.index(t.x), ys.index(t.y)
        if ci > 0:
            ov = xs[ci - 1] + t.w - t.x
            wx[:ov] = _ramp(ov, rising=True)
        if ci < len(xs) - 1:
            ov = t.x + t.w - xs[ci + 1]
            wx[t.w - ov :] = _ramp(ov, rising=False)
        if ri > 0:
            ov = ys[ri - 1] + t.h - t.y
            wy[:ov] = _ramp(ov, rising=True)
        if ri < len(ys) - 1:
            ov = t.y + t.h - ys[ri + 1]
            wy[t.h - ov :] = _ramp(ov, rising=False)
        out.append(wy[:, None] * wx[None, :])
    return out


def stitch_frames(
    streams: Sequence[Iterable[np.ndarray]],
    tiles: Sequence[TileSpec],
    *,
    canvas_w: int,
    canvas_h: int,
    scale: int,
) -> Iterator[np.ndarray]:
    """Blend per-tile upscaled frames into full canvas frames, one at a time.

    Args:
        streams: One iterable of ``(h*scale, w*scale, 3)`` uint8 frames per
            tile, in ``tiles`` order.
        tiles: The source-pixel tile boxes.
        canvas_w: Source width; the output is ``canvas_w * scale`` wide.
        canvas_h: Source height.
        scale: Integer upscale factor the tiles were rendered at.

    Yields:
        ``(canvas_h*scale, canvas_w*scale, 3)`` uint8 frames.

    Raises:
        ValueError: The streams do not all carry the same number of frames.
    """
    # float32 halves the memory traffic of the per-frame blend on a 25-Mpx
    # canvas (a 345-frame 3840x2176 stitch measured 466 s in float64) and is
    # exact for uint8 pixel values times ramp weights.
    weights = [
        np.repeat(np.repeat(w, scale, axis=0), scale, axis=1)[:, :, None].astype(
            np.float32
        )
        for w in tile_weights(tiles, canvas_w=canvas_w, canvas_h=canvas_h)
    ]
    iters = [iter(s) for s in streams]
    while True:
        frames = []
        for it in iters:
            frames.append(next(it, None))
        present = [f is not None for f in frames]
        if not any(present):
            return
        if not all(present):
            raise ValueError("tile streams carry different frame counts")
        acc = np.zeros((canvas_h * scale, canvas_w * scale, 3), dtype=np.float32)
        norm = np.zeros((canvas_h * scale, canvas_w * scale, 1), dtype=np.float32)
        for frame, tile, w in zip(frames, tiles, weights, strict=True):
            assert frame is not None  # noqa: S101 — checked above
            ys, xs = tile.y * scale, tile.x * scale
            h, wd = tile.h * scale, tile.w * scale
            acc[ys : ys + h, xs : xs + wd] += frame.astype(np.float32) * w
            norm[ys : ys + h, xs : xs + wd] += w
        yield (
            np.rint(acc / np.maximum(norm, np.float32(1e-6)))
            .clip(0, 255)
            .astype(np.uint8)
        )


def rawvideo_read_argv(path: str) -> list[str]:
    """Ffmpeg argv streaming ``path`` as rgb24 raw frames on stdout."""
    return [
        "ffmpeg",
        "-loglevel",
        "error",
        "-i",
        path,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]


def rawvideo_write_argv(
    width: int, height: int, fps: float, out_path: str
) -> list[str]:
    """Ffmpeg argv encoding rgb24 raw frames from stdin to h264 at ``-crf 10``."""
    return [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        f"{fps:g}",
        "-i",
        "pipe:0",
        "-c:v",
        "libx264",
        "-crf",
        "10",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        out_path,
    ]


def _frames_from(proc: subprocess.Popen[bytes], h: int, w: int) -> Iterator[np.ndarray]:
    """Yield rgb24 frames of ``h x w`` from a rawvideo reader's stdout."""
    assert proc.stdout is not None  # noqa: S101 — opened with stdout=PIPE
    size = h * w * 3
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            return
        yield np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)


def stitch_videos(
    tile_paths: Sequence[str],
    tiles: Sequence[TileSpec],
    *,
    canvas_w: int,
    canvas_h: int,
    scale: int,
    fps: float,
    out_path: str,
) -> None:
    """Feather-blend upscaled tile videos into one canvas video via ffmpeg pipes.

    Each tile clip is decoded to raw rgb24 by its own ffmpeg process; frames
    are blended with :func:`stitch_frames` and piped to a single encoder, so
    no full-clip array is ever held in memory.

    Args:
        tile_paths: Upscaled tile clips, in ``tiles`` order.
        tiles: The source-pixel tile boxes.
        canvas_w: Source width.
        canvas_h: Source height.
        scale: Integer factor the tiles were upscaled by.
        fps: Frame rate for the output container.
        out_path: Destination mp4.

    Raises:
        RuntimeError: A reader or the writer exited non-zero.
        ValueError: The tile clips carry different frame counts.
    """
    readers = [
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            rawvideo_read_argv(p), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for p in tile_paths
    ]
    writer = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        rawvideo_write_argv(canvas_w * scale, canvas_h * scale, fps, out_path),
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert writer.stdin is not None  # noqa: S101 — opened with stdin=PIPE
    try:
        streams = [
            _frames_from(r, t.h * scale, t.w * scale)
            for r, t in zip(readers, tiles, strict=True)
        ]
        for frame in stitch_frames(
            streams, tiles, canvas_w=canvas_w, canvas_h=canvas_h, scale=scale
        ):
            writer.stdin.write(frame.tobytes())
    finally:
        writer.stdin.close()
        codes: list[tuple[str, int, bytes]] = []
        for r, p in zip(readers, tile_paths, strict=True):
            _, err = r.communicate()
            codes.append((p, r.returncode, err))
        _, werr = writer.communicate()
        codes.append((out_path, writer.returncode, werr))
    bad: Any = [(p, rc, err[:300]) for p, rc, err in codes if rc != 0]
    if bad:
        raise RuntimeError(f"ffmpeg stitch failed: {bad}")
