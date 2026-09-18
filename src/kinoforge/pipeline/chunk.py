"""Temporal chunking for long-clip upscales.

FlashVSR holds the whole clip on the GPU, so its memory grows with the frame
count: 345 frames at 960x544 needed ~200 GB on 2026-09-18 and OOM'd an 80 GB
A100 at frame ~130. No card fixes that; splitting the clip in time does.

Contract (every number a test hand-derives from this paragraph): chunk ``i``
KEEPS source frames ``[i*chunk_frames, min((i+1)*chunk_frames, total))`` and
additionally RENDERS ``overlap`` frames before that range (clamped at 0) as a
warm-up for the streaming model, then discards them. ``trim_head`` is that
discard count, so ``sum(count - trim_head)`` over the chunks is exactly
``total`` and consecutive kept ranges abut with no gap or duplicate.

The ffmpeg argv builders are pure so the stage can drive them through the
same injectable ``run`` seam as :mod:`kinoforge.core.frames`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kinoforge.core.errors import FrameExtractionError
from kinoforge.core.frames import _default_probe_run


@dataclass(frozen=True)
class ChunkSpec:
    """One temporal chunk of a source clip.

    Attributes:
        start: First source frame rendered (0-based, inclusive).
        count: Frames rendered, warm-up included.
        trim_head: Leading rendered frames to discard after upscaling.
    """

    start: int
    count: int
    trim_head: int


def plan_chunks(
    total_frames: int, *, chunk_frames: int, overlap: int
) -> list[ChunkSpec]:
    """Split ``total_frames`` into overlapping chunks per the module contract.

    Args:
        total_frames: Frames in the source clip; must be positive.
        chunk_frames: Frames each chunk KEEPS; must be positive.
        overlap: Warm-up frames rendered before each kept range and then
            discarded; ``0 <= overlap < chunk_frames``.

    Returns:
        Chunks in temporal order. A clip no longer than ``chunk_frames`` is a
        single untrimmed chunk, so short clips are never touched.

    Raises:
        ValueError: Any argument outside the ranges above.
    """
    if total_frames <= 0:
        raise ValueError(f"total_frames must be positive, got {total_frames}")
    if chunk_frames <= 0:
        raise ValueError(f"chunk_frames must be positive, got {chunk_frames}")
    if overlap < 0 or overlap >= chunk_frames:
        raise ValueError(
            f"overlap must satisfy 0 <= overlap < chunk_frames; "
            f"got overlap={overlap} chunk_frames={chunk_frames}"
        )
    specs: list[ChunkSpec] = []
    for kept_start in range(0, total_frames, chunk_frames):
        kept_end = min(kept_start + chunk_frames, total_frames)
        start = max(kept_start - overlap, 0)
        specs.append(
            ChunkSpec(start=start, count=kept_end - start, trim_head=kept_start - start)
        )
    return specs


def split_argv(src_path: str, spec: ChunkSpec, out_path: str) -> list[str]:
    """Build the ffmpeg argv that cuts ``spec`` out of ``src_path`` losslessly.

    ``trim`` is frame-accurate and its ``end_frame`` is EXCLUSIVE. The chunk is
    written with ``-qp 0`` (lossless x264) and no audio, so the upscaler sees
    the source's own pixels rather than a second generation of them.

    Args:
        src_path: Local source clip.
        spec: The chunk to extract.
        out_path: Destination mp4.

    Returns:
        The argv; ``out_path`` is always its last token.
    """
    end = spec.start + spec.count
    return [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        src_path,
        "-vf",
        f"trim=start_frame={spec.start}:end_frame={end},setpts=PTS-STARTPTS",
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


def concat_argv(parts: list[tuple[str, int]], out_path: str) -> list[str]:
    """Build the ffmpeg argv that trims each part's head and joins them.

    One pass: every input gets ``trim=start_frame=<trim_head>`` plus a
    timestamp reset (without which the join carries gaps and stutters), then
    ``concat``. The join is re-encoded once at ``-crf 10`` — near-transparent,
    and the height-target lanczos downscale re-encodes it anyway.

    Args:
        parts: ``(upscaled_chunk_path, trim_head)`` in temporal order.
        out_path: Destination mp4.

    Returns:
        The argv; ``out_path`` is always its last token.

    Raises:
        ValueError: ``parts`` is empty.
    """
    if not parts:
        raise ValueError("concat_argv needs at least one part")
    argv = ["ffmpeg", "-y", "-loglevel", "error"]
    for path, _ in parts:
        argv += ["-i", path]
    chains = [
        f"[{i}:v]trim=start_frame={trim},setpts=PTS-STARTPTS[v{i}]"
        for i, (_, trim) in enumerate(parts)
    ]
    labels = "".join(f"[v{i}]" for i in range(len(parts)))
    fc = ";".join(chains) + f";{labels}concat=n={len(parts)}:v=1:a=0[v]"
    argv += [
        "-filter_complex",
        fc,
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-crf",
        "10",
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
        out_path,
    ]
    return argv


def ffprobe_frames(
    video_path: str | Path,
    *,
    run: Callable[[list[str]], bytes] = _default_probe_run,
) -> int:
    """Probe the frame count of the first video stream via ffprobe.

    Reads the container's ``nb_frames`` (mp4 always carries it) rather than
    decoding the whole clip with ``-count_frames``.

    Args:
        video_path: Path to the video file on disk.
        run: Injectable seam ``(argv) -> stdout`` so tests spawn no binary.

    Returns:
        Number of frames.

    Raises:
        FrameExtractionError: ffprobe missing / non-zero exit, or output that
            does not parse as an integer (``N/A`` for a stream without one).
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=nb_frames",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    raw = run(argv).decode(errors="replace").strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise FrameExtractionError(
            f"unparseable ffprobe frame count {raw!r} for {video_path}"
        ) from exc
