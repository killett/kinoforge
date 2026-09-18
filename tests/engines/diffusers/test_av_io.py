"""Behavior: a jointly-generated audio track survives encoding to MP4.

MiniMax-H3 is the first kinoforge model that emits audio alongside video, and
``_video_io.write_mp4`` — which every other model uses — takes frames and
nothing else. These tests guard the one line where that soundtrack could
silently vanish.

The FlashVSR precedent is why #3 exists: 24+ runs were reported green on exit
code and dimensions while every output was garbage, because nothing inspected
the payload. A container with an audio stream of pure silence would pass every
structural check here and be equally worthless.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from kinoforge.engines.diffusers.servers._av_io import (
    write_mp4_with_audio,
    write_wav,
)

_FPS = 24
_SR = 32000
_SECONDS = 1.0
_NFRAMES = int(_FPS * _SECONDS)


def _probe(path: Path) -> dict[str, Any]:
    """Return ffprobe's stream listing for *path* as a dict."""
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def _streams(path: Path, kind: str) -> list[dict[str, Any]]:
    return [s for s in _probe(path)["streams"] if s.get("codec_type") == kind]


@pytest.fixture
def frames() -> np.ndarray:
    """A moving gradient — distinguishable per-frame, so truncation shows up."""
    rng = np.random.default_rng(1234)
    base = rng.integers(0, 255, size=(64, 64, 3), dtype=np.uint8)
    return np.stack([np.roll(base, i, axis=1) for i in range(_NFRAMES)])


@pytest.fixture
def tone() -> np.ndarray:
    """A real stereo sine, not silence — amplitude must survive the round trip."""
    t = np.linspace(0, _SECONDS, int(_SR * _SECONDS), endpoint=False)
    left = 0.6 * np.sin(2 * np.pi * 440.0 * t)
    right = 0.6 * np.sin(2 * np.pi * 660.0 * t)
    return np.stack([left, right], axis=1)


def test_the_output_carries_both_a_video_and_an_audio_stream(
    tmp_path: Path, frames: np.ndarray, tone: np.ndarray
) -> None:
    # Bug caught: the whole reason this module exists — a mux that drops the
    # audio, leaving a video-only container that looks entirely normal.
    out = tmp_path / "av.mp4"
    write_mp4_with_audio(frames, tone, _FPS, _SR, out)
    assert len(_streams(out, "video")) == 1
    assert len(_streams(out, "audio")) == 1


def test_audio_and_video_durations_agree(
    tmp_path: Path, frames: np.ndarray, tone: np.ndarray
) -> None:
    # Bug caught: a sample-rate mismatch (e.g. writing 32 kHz audio into a
    # 48 kHz header) plays the track at the wrong speed, so it drifts out of
    # sync with frames that were generated in lockstep with it. Stream
    # existence cannot see this; duration can.
    out = tmp_path / "av.mp4"
    write_mp4_with_audio(frames, tone, _FPS, _SR, out)
    fmt = _probe(out)["format"]
    assert abs(float(fmt["duration"]) - _SECONDS) < 0.15


def test_the_muxed_audio_is_not_digital_silence(
    tmp_path: Path, frames: np.ndarray, tone: np.ndarray
) -> None:
    # Bug caught: an all-zero buffer written as "audio" satisfies every
    # structural assertion above and is worthless. This decodes the track back
    # and asserts real amplitude — the check the FlashVSR entries lacked when
    # 24 runs were called green on metadata alone.
    out = tmp_path / "av.mp4"
    write_mp4_with_audio(frames, tone, _FPS, _SR, out)
    raw = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(out),
            "-f",
            "s16le",
            "-ac",
            "2",
            "-ar",
            str(_SR),
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    decoded = np.frombuffer(raw, dtype=np.int16)
    assert decoded.size > 0
    # A 0.6-amplitude sine peaks near 19660 in int16. AAC is lossy, so assert
    # an order of magnitude, not equality — silence would read as ~0.
    assert np.abs(decoded).max() > 5000


def test_frame_count_and_dimensions_survive_the_mux(
    tmp_path: Path, frames: np.ndarray, tone: np.ndarray
) -> None:
    # Bug caught: -shortest trimming the VIDEO because the audio was fractionally
    # shorter, silently losing trailing frames; or an encoder rescale.
    out = tmp_path / "av.mp4"
    write_mp4_with_audio(frames, tone, _FPS, _SR, out)
    v = _streams(out, "video")[0]
    assert (v["width"], v["height"]) == (64, 64)
    counted = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert int(counted) == _NFRAMES


def test_stereo_layout_is_preserved(
    tmp_path: Path, frames: np.ndarray, tone: np.ndarray
) -> None:
    # Bug caught: collapsing (samples, 2) to mono. H3's headline claim is
    # STEREO audio; downmixing it silently would technically still "have audio".
    out = tmp_path / "av.mp4"
    write_mp4_with_audio(frames, tone, _FPS, _SR, out)
    assert _streams(out, "audio")[0]["channels"] == 2


def test_integer_audio_is_refused(tmp_path: Path, frames: np.ndarray) -> None:
    # Bug caught: an int16 buffer passed where floats are expected would be
    # scaled by 32767 again and wrap to noise. Refuse rather than mangle.
    bad = np.zeros((_SR, 2), dtype=np.int16)
    with pytest.raises(TypeError):
        write_mp4_with_audio(frames, bad, _FPS, _SR, tmp_path / "x.mp4")


def test_empty_audio_is_refused(tmp_path: Path) -> None:
    # Bug caught: a zero-sample track produces a container ffprobe reports as
    # having an audio stream of duration 0 — structurally valid, semantically
    # empty. Boundary case for the (samples, channels) contract.
    with pytest.raises(ValueError, match="zero samples"):
        write_wav(np.zeros((0, 2), dtype=np.float32), _SR, tmp_path / "x.wav")


def test_wav_clips_rather_than_wrapping_on_overshoot(tmp_path: Path) -> None:
    # Bug caught: a model overshooting [-1, 1] would wrap around int16 without
    # the clip — turning a loud passage into white noise. Assert the peak lands
    # at the int16 ceiling rather than wrapping negative.
    loud = np.full((100, 1), 2.5, dtype=np.float32)
    out = tmp_path / "loud.wav"
    write_wav(loud, _SR, out)
    import wave as _wave

    with _wave.open(str(out), "rb") as fh:
        data = np.frombuffer(fh.readframes(fh.getnframes()), dtype=np.int16)
    assert data.min() > 0, "overshoot wrapped to negative instead of clipping"
    assert data.max() == 32767
