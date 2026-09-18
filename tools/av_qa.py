r"""Assert the QA gates on a generated clip, audio included.

Why this is a tool and not a snippet
------------------------------------
CLAUDE.md requires every output video to be eyeballed before a run is reported
green, and the MiniMax-H3 design doc adds an **audio arm** because H3 is the
first model in kinoforge that can produce a *silent* success — an MP4 whose exit
code, dimensions and duration are all perfect and whose soundtrack is missing,
mono, at the wrong rate, or digital silence. ``ffprobe`` alone cannot hear
silence, and dimensions cannot see pixels (the 2026-07-03 FlashVSR lesson: 24+
runs reported green while every output was false-colour garbage).

This ran as a scratch script for the 2026-09-18 H3 runs and caught three things
worth catching: a duration match to the millisecond, one clip with genuinely
decorrelated stereo (L/R correlation 0.32) and one that was effectively mono
(0.9956). The correlation check is the load-bearing one — a mux that wrote the
same channel twice passes every other audio assertion.

Usage::

    pixi run python tools/av_qa.py output/<clip>.mp4
    pixi run python tools/av_qa.py output/<clip>.mp4 --expect-rate 32000
    pixi run python tools/av_qa.py output/<clip>.mp4 --no-audio   # silent model

Exit code is 0 when every gate passes, 1 when any fails, so it composes into a
smoke harness. It never *judges* content: frame QA still needs eyes on a contact
sheet, and "the soundtrack suits the scene" is not something this can answer —
see ``--cut-scan`` for the one motion property it can measure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

#: ffmpeg/ffprobe are resolved through the same fallback ``_av_io`` uses: the
#: dev container has them on PATH, ``python:3.13-slim`` does not, and
#: ``imageio[ffmpeg]`` keeps its binary inside the package.
_FFMPEG_CANDIDATES = ("ffmpeg", "ffprobe")


def _default_run(argv: list[str]) -> bytes:
    """Run *argv* and return stdout. The single subprocess seam for this tool.

    Factored out so the three call sites share one implementation — and so the
    ``noqa`` sits on the actual call rather than on a formatter-relocatable
    lambda.

    Args:
        argv: Command vector; argv[0] comes from :func:`_binary`, never a user
            string, and no shell is used.

    Returns:
        Raw stdout bytes.
    """
    return subprocess.run(argv, capture_output=True, check=True).stdout  # noqa: S603


def _binary(name: str) -> str:
    """Return a runnable path for *name*, preferring PATH.

    Args:
        name: ``"ffmpeg"`` or ``"ffprobe"``.

    Returns:
        An absolute path, or the bare name if nothing better is found.
    """
    import shutil

    found = shutil.which(name)
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = str(imageio_ffmpeg.get_ffmpeg_exe())
        # imageio ships ffmpeg only; ffprobe has no equivalent, so fall through.
        return exe if name == "ffmpeg" else name
    except Exception:  # noqa: BLE001 — the caller reports the missing tool
        return name


def probe_streams(
    path: str | Path, *, run: Callable[[list[str]], bytes] | None = None
) -> dict:  # type: ignore[type-arg]
    """Return ffprobe's parsed JSON for *path*.

    Args:
        path: The media file.
        run: Subprocess seam returning stdout bytes; injected in tests so they
            never spawn a real binary.

    Returns:
        The decoded ffprobe payload.
    """
    argv = [
        _binary("ffprobe"),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    runner = run or _default_run
    return dict(json.loads(runner(argv).decode()))


def decode_pcm(
    path: str | Path,
    sample_rate: int,
    *,
    run: Callable[[list[str]], bytes] | None = None,
) -> np.ndarray:
    """Decode *path*'s audio to interleaved stereo float32 in [-1, 1].

    Args:
        path: The media file.
        sample_rate: Rate to resample to.
        run: Subprocess seam returning raw s16le bytes.

    Returns:
        A ``(samples, 2)`` float32 array; empty when there is no audio.
    """
    argv = [
        _binary("ffmpeg"),
        "-v",
        "error",
        "-i",
        str(path),
        "-f",
        "s16le",
        "-ac",
        "2",
        "-ar",
        str(sample_rate),
        "-",
    ]
    runner = run or _default_run
    raw = runner(argv)
    flat = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if flat.size < 2:
        return np.zeros((0, 2), dtype=np.float32)
    return flat[: flat.size // 2 * 2].reshape(-1, 2)


def audio_metrics(pcm: np.ndarray) -> dict[str, float]:
    """Return the measurable properties of a decoded soundtrack.

    ``lr_correlation`` is the one that matters most: a mux that duplicated one
    channel into both passes every level check while destroying the stereo the
    model generated. Near 1.0 means effectively mono — which may be the model's
    own output rather than a defect, so it is reported, not asserted.

    Args:
        pcm: ``(samples, 2)`` float array.

    Returns:
        peak / rms / per-channel peaks / L-R correlation.
    """
    if pcm.shape[0] == 0:
        return {
            "peak": 0.0,
            "rms": 0.0,
            "left_peak": 0.0,
            "right_peak": 0.0,
            "lr_correlation": 0.0,
        }
    left, right = pcm[:, 0], pcm[:, 1]
    if pcm.shape[0] > 1 and left.std() > 0 and right.std() > 0:
        corr = float(np.corrcoef(left, right)[0, 1])
    else:
        corr = 1.0
    return {
        "peak": float(np.abs(pcm).max()),
        "rms": float(np.sqrt((pcm**2).mean())),
        "left_peak": float(np.abs(left).max()),
        "right_peak": float(np.abs(right).max()),
        "lr_correlation": corr,
    }


def evaluate(
    probe: dict,  # type: ignore[type-arg]
    metrics: dict[str, float],
    *,
    expect_audio: bool = True,
    expect_rate: int | None = None,
    expect_channels: int = 2,
    duration_tolerance_s: float = 0.2,
) -> list[str]:
    """Return the list of gate failures; empty means PASS.

    Pure, so the whole verdict is testable without spawning a binary.

    Args:
        probe: ffprobe payload.
        metrics: Output of :func:`audio_metrics`.
        expect_audio: Whether an audio stream is required.
        expect_rate: Required sample rate, or None to accept any.
        expect_channels: Required channel count.
        duration_tolerance_s: Allowed video/audio duration gap.

    Returns:
        Human-readable failures, most structural first.
    """
    problems: list[str] = []
    streams = {s.get("codec_type"): s for s in probe.get("streams", [])}

    video = streams.get("video")
    if video is None:
        return ["NO VIDEO STREAM"]

    if not expect_audio:
        return problems

    audio = streams.get("audio")
    if audio is None:
        return ["NO AUDIO STREAM — a silent 'success' is the failure mode"]

    if int(audio.get("channels", 0)) != expect_channels:
        problems.append(
            f"audio channels={audio.get('channels')}, expected {expect_channels}"
        )
    if expect_rate is not None and int(audio.get("sample_rate", 0)) != expect_rate:
        problems.append(
            f"audio sample_rate={audio.get('sample_rate')}, expected {expect_rate}"
        )

    try:
        gap = abs(float(video.get("duration", 0)) - float(audio.get("duration", 0)))
        if gap > duration_tolerance_s:
            problems.append(
                f"video/audio durations differ by {gap:.3f}s "
                f"(> {duration_tolerance_s}s tolerance)"
            )
    except (TypeError, ValueError):
        problems.append("could not compare video/audio durations")

    if metrics["peak"] <= 0.01 or metrics["rms"] <= 1e-4:
        problems.append(
            f"DIGITAL SILENCE: peak={metrics['peak']:.5f} rms={metrics['rms']:.6f}"
        )
    if metrics["left_peak"] <= 0.001 or metrics["right_peak"] <= 0.001:
        problems.append(
            f"a channel is silent: L={metrics['left_peak']:.5f} "
            f"R={metrics['right_peak']:.5f}"
        )
    return problems


def scan_cuts(
    path: str | Path,
    *,
    run: Callable[[list[str]], bytes] | None = None,
    width: int = 96,
    height: int = 54,
) -> list[tuple[int, float]]:
    """Return frames whose delta from the previous one is an outlier.

    This exists because evenly-spaced frame sampling — the default contact-sheet
    QA — **cannot tell a hard cut from a fast camera move**. The 2026-09-18
    max-length H3 clip looked like a very fast push-in on a 6-frame sheet and was
    actually a hard cut at frame 272 (delta 70.05 against a median of 1.54).

    Args:
        path: The video.
        run: Subprocess seam returning raw gray8 frames.
        width: Downscale width for the trace.
        height: Downscale height.

    Returns:
        ``(frame_index, delta)`` for every frame whose delta exceeds 10x the
        median, worst first.
    """
    argv = [
        _binary("ffmpeg"),
        "-v",
        "error",
        "-i",
        str(path),
        "-vf",
        f"scale={width}:{height},format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    runner = run or _default_run
    raw = runner(argv)
    frames = np.frombuffer(raw, dtype=np.uint8)
    n = frames.size // (width * height)
    if n < 3:
        return []
    seq = frames[: n * width * height].reshape(n, height, width).astype(np.float32)
    delta = np.abs(np.diff(seq, axis=0)).mean(axis=(1, 2))
    median = float(np.median(delta))
    threshold = max(median * 10.0, 1.0)
    hits = [(int(i) + 1, float(d)) for i, d in enumerate(delta) if d > threshold]
    return sorted(hits, key=lambda h: -h[1])


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        0 when every gate passes, 1 otherwise.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", help="path to the clip")
    ap.add_argument("--expect-rate", type=int, default=None)
    ap.add_argument("--expect-channels", type=int, default=2)
    ap.add_argument(
        "--no-audio",
        action="store_true",
        help="the model is video-only (Wan, FlashVSR, RIFE); skip the audio arm",
    )
    ap.add_argument(
        "--cut-scan",
        action="store_true",
        help="also report hard cuts, which evenly-spaced frame QA cannot see",
    )
    args = ap.parse_args(argv)

    probe = probe_streams(args.video)
    streams = {s.get("codec_type"): s for s in probe.get("streams", [])}
    video = streams.get("video", {})
    print(f"file: {args.video}  {Path(args.video).stat().st_size / 1e6:.2f} MB")
    print(
        f"video: {video.get('codec_name')} {video.get('width')}x{video.get('height')} "
        f"fps={video.get('r_frame_rate')} frames={video.get('nb_frames')} "
        f"dur={video.get('duration')}"
    )

    metrics = audio_metrics(np.zeros((0, 2), dtype=np.float32))
    if not args.no_audio:
        audio = streams.get("audio")
        if audio is not None:
            print(
                f"audio: {audio.get('codec_name')} ch={audio.get('channels')} "
                f"rate={audio.get('sample_rate')} dur={audio.get('duration')}"
            )
            metrics = audio_metrics(
                decode_pcm(args.video, int(audio.get("sample_rate", 48000)))
            )
            print(
                "audio pcm: peak={peak:.4f} rms={rms:.5f} L={left_peak:.4f} "
                "R={right_peak:.4f} LR_corr={lr_correlation:.4f}".format(**metrics)
            )
            if metrics["lr_correlation"] > 0.99:
                print(
                    "  NOTE: L/R correlation > 0.99 — effectively MONO. Not "
                    "necessarily a defect (the model may have generated it that "
                    "way), but it is not stereo."
                )

    problems = evaluate(
        probe,
        metrics,
        expect_audio=not args.no_audio,
        expect_rate=args.expect_rate,
        expect_channels=args.expect_channels,
    )

    if args.cut_scan:
        cuts = scan_cuts(args.video)
        if cuts:
            print(f"hard cuts detected (frame, delta): {cuts[:5]}")
        else:
            print("no hard cuts detected")

    print("\nVERDICT:", "PASS" if not problems else "FAIL")
    for problem in problems:
        print("  WARN", problem)
    print(
        "\nNOTE: this checks the measurable gates only. Frame QA still needs eyes "
        "on a contact sheet, and whether the soundtrack SUITS the scene is not "
        "something this can answer."
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
