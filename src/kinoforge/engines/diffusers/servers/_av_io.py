"""MP4 encoder that keeps a jointly-generated audio track.

``_video_io.write_mp4`` takes ``(T, H, W, 3)`` frames and nothing else, so a
model that emits video AND audio loses half its output there. MiniMax-H3 is the
first such model in kinoforge: it generates stereo audio in lockstep with the
frames, and that soundtrack is the model's headline capability.

This module is deliberately a SIBLING rather than an extension. ``write_mp4``
is on the Wan / FlashVSR / RIFE path and must not change shape.

Muxing strategy: encode video with imageio (identical settings to
``_video_io.write_mp4``, so the video track is bit-comparable), write the audio
to a temporary WAV with the stdlib ``wave`` module, then let ffmpeg combine the
two. The WAV hop avoids taking a new dependency for float->PCM conversion and
keeps the sample layout explicit.
"""

from __future__ import annotations

import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np


def _to_pcm16(audio: np.ndarray) -> np.ndarray:
    """Convert float audio in [-1, 1] to interleaved int16 PCM.

    Args:
        audio: Float array of shape ``(samples, channels)``.

    Returns:
        An int16 array of the same shape.
    """
    # Clip BEFORE scaling: a model that overshoots [-1, 1] would otherwise wrap
    # around int16 and turn a loud passage into white noise.
    clipped = np.clip(audio, -1.0, 1.0)
    pcm: np.ndarray = (clipped * 32767.0).astype(np.int16)
    return pcm


def write_wav(audio: np.ndarray, sample_rate: int, path: Path) -> None:
    """Write float audio to a 16-bit PCM WAV file.

    Args:
        audio: Float array shaped ``(samples, channels)``; 1-D is treated as
            mono and reshaped.
        sample_rate: Samples per second, e.g. ``32000``.
        path: Destination path.

    Raises:
        TypeError: ``audio`` is not a floating-point numpy array.
        ValueError: ``audio`` is not 1-D or 2-D, has zero samples, or
            ``sample_rate`` is not positive.
    """
    if not isinstance(audio, np.ndarray) or not np.issubdtype(audio.dtype, np.floating):
        raise TypeError(
            "write_wav expects a floating-point ndarray in [-1, 1]; got "
            f"{type(audio).__name__} dtype={getattr(audio, 'dtype', None)}"
        )
    if audio.ndim == 1:
        audio = audio[:, None]
    if audio.ndim != 2:
        raise ValueError(
            f"write_wav expects (samples, channels) or (samples,); got {audio.shape}"
        )
    if audio.shape[0] == 0:
        raise ValueError("write_wav got zero samples")
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be positive; got {sample_rate}")

    pcm = _to_pcm16(audio)
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(pcm.shape[1])
        fh.setsampwidth(2)  # int16
        fh.setframerate(sample_rate)
        fh.writeframes(pcm.tobytes())


def write_mp4_with_audio(
    frames: np.ndarray,
    audio: np.ndarray,
    fps: int,
    sample_rate: int,
    path: Path,
) -> None:
    """Encode *frames* and *audio* into a single MP4 with both streams.

    The video track is encoded with the same H.264 / yuv420p / crf=19 settings
    as :func:`_video_io.write_mp4`, so switching a model onto this function
    does not change its video output. Audio is AAC.

    Args:
        frames: 4-D uint8 array ``(num_frames, height, width, 3)``, RGB.
        audio: Float array ``(samples, channels)`` in [-1, 1].
        fps: Video frames per second.
        sample_rate: Audio samples per second.
        path: Destination MP4 path. Parent directory must exist.

    Raises:
        TypeError: ``frames`` is not a uint8 ndarray, or ``audio`` is not a
            floating-point ndarray.
        ValueError: ``frames`` is not ``(T, H, W, 3)``, or the audio is empty.
        RuntimeError: ffmpeg failed to mux the two streams.
    """
    import imageio.v3 as iio

    if not isinstance(frames, np.ndarray) or frames.dtype != np.uint8:
        raise TypeError(
            "write_mp4_with_audio expects a uint8 frame ndarray; got "
            f"{type(frames).__name__} dtype={getattr(frames, 'dtype', None)}"
        )
    if frames.ndim != 4 or frames.shape[-1] != 3:
        raise ValueError(
            f"write_mp4_with_audio expects (T, H, W, 3) frames; got {frames.shape}"
        )

    with tempfile.TemporaryDirectory() as td:
        video_only = Path(td) / "video.mp4"
        wav = Path(td) / "audio.wav"

        iio.imwrite(
            str(video_only),
            frames,
            fps=fps,
            codec="libx264",
            pixelformat="yuv420p",
            macro_block_size=1,
            ffmpeg_params=["-crf", "19"],
        )
        write_wav(audio, sample_rate, wav)

        # -shortest guards the case where the two streams disagree in length:
        # without it the container's duration is the LONGER stream and the
        # video freezes on its last frame while audio continues, which reads
        # as a model defect rather than a mux defect.
        argv = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video_only),
            "-i",
            str(wav),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(path),
        ]
        proc = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, check=False
        )
        if proc.returncode != 0 or not path.exists():
            raise RuntimeError(
                f"ffmpeg mux failed (rc={proc.returncode}): {proc.stderr.strip()[:500]}"
            )
