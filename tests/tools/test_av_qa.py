"""Behavior: the output-QA gates fail on the ways a clip can be silently wrong.

The point of this tool is that exit code + dimensions + duration are NOT enough.
The 2026-07-03 FlashVSR runs were reported green on exit code and `1920x1920`
across 24+ attempts and ~$2 of spend while every output was false-colour
garbage; MiniMax-H3 adds the audio version of the same trap, where an MP4 can
have a perfect container and a missing, mono, mis-rated or silent soundtrack.

Every test here drives the pure ``evaluate`` / ``audio_metrics`` / ``scan_cuts``
core with injected bytes, so none of them spawns ffmpeg — matching the
``core.frames`` convention that subprocess seams stay injectable.
"""

from __future__ import annotations

import numpy as np

from tools.av_qa import audio_metrics, decode_pcm, evaluate, scan_cuts


def _probe(
    *,
    audio: bool = True,
    channels: int = 2,
    rate: int = 32000,
    video_dur: str = "5.166667",
    audio_dur: str = "5.152000",
) -> dict:  # type: ignore[type-arg]
    streams: list[dict] = [  # type: ignore[type-arg]
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1344,
            "height": 768,
            "duration": video_dur,
        }
    ]
    if audio:
        streams.append(
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": channels,
                "sample_rate": str(rate),
                "duration": audio_dur,
            }
        )
    return {"streams": streams, "format": {"duration": video_dur}}


def _loud(samples: int = 1000, *, decorrelated: bool = True) -> np.ndarray:
    rng = np.random.default_rng(0)
    left = rng.normal(0, 0.2, samples).astype(np.float32)
    right = (
        rng.normal(0, 0.2, samples).astype(np.float32) if decorrelated else left.copy()
    )
    return np.stack([left, right], axis=1)


def test_a_healthy_clip_passes_every_gate() -> None:
    """The real 2026-09-18 H3 shape is a PASS.

    Bug caught: the gates are tightened until nothing passes — a QA script that
    fails on a known-good clip gets disabled, and then it is guarding nothing.
    """
    assert evaluate(_probe(), audio_metrics(_loud()), expect_rate=32000) == []


def test_a_missing_audio_stream_fails() -> None:
    """A silent MP4 is refused, which is the whole reason this exists.

    Bug caught: the writer is `_video_io.write_mp4` (frames only) instead of
    `_av_io.write_mp4_with_audio`, so a joint-audio model publishes silent video
    that passes exit-code and dimension checks. Verified live: this is exactly
    what the Wan corpus looks like, and the H3 path must not regress to it.
    """
    problems = evaluate(_probe(audio=False), audio_metrics(_loud()))
    assert problems and "NO AUDIO STREAM" in problems[0]


def test_digital_silence_fails_even_though_the_stream_exists() -> None:
    """An audio stream of zeros is refused.

    Bug caught: the mux succeeds, ffprobe reports a perfectly good aac stereo
    32 kHz stream of the right duration, and there is no sound in it. ffprobe
    cannot hear silence; only decoding the PCM can.
    """
    silent = audio_metrics(np.zeros((1000, 2), dtype=np.float32))
    problems = evaluate(_probe(), silent, expect_rate=32000)
    assert any("DIGITAL SILENCE" in p for p in problems)


def test_one_dead_channel_fails() -> None:
    """A stereo stream with one silent side is refused.

    Bug caught: the (channels, samples) -> (samples, channels) transpose in
    `_av_io` is written as a reshape, which keeps the shape correct and scrambles
    the channels — and can leave one side empty.
    """
    half = _loud()
    half[:, 1] = 0.0
    problems = evaluate(_probe(), audio_metrics(half), expect_rate=32000)
    assert any("a channel is silent" in p for p in problems)


def test_a_duration_mismatch_fails() -> None:
    """Video and audio that disagree in length are refused.

    Bug caught: `-shortest` is dropped from the mux, so the container's duration
    becomes the LONGER stream — the video freezes on its last frame while audio
    continues, which reads as a model defect rather than a mux defect.
    """
    problems = evaluate(
        _probe(video_dur="5.166667", audio_dur="7.500000"),
        audio_metrics(_loud()),
        expect_rate=32000,
    )
    assert any("durations differ" in p for p in problems)


def test_the_wrong_sample_rate_fails() -> None:
    """A rate other than the expected one is refused.

    Bug caught: the mux hardcodes 32000 while the model's audio VAE reports
    something else, so the soundtrack plays at the wrong speed — perfectly in
    sync with itself, so neither a duration nor a silence check would catch it.
    """
    problems = evaluate(_probe(rate=48000), audio_metrics(_loud()), expect_rate=32000)
    assert any("sample_rate" in p for p in problems)


def test_mono_is_measured_rather_than_asserted() -> None:
    """Duplicated channels show up as correlation ~1.0, and are NOT a failure.

    This is the check a lazy mux would otherwise sail through: every level
    assertion passes when one channel is copied into both. It is reported rather
    than failed because the model legitimately produced a near-mono soundtrack
    on the 2026-09-18 max-length clip (0.9956) and a decorrelated one on the
    5-second clip (0.3190) — so mono is a property of the generation, not proof
    of a bug.

    Bug caught: the correlation is not computed at all, and a
    write-one-channel-twice regression becomes invisible.
    """
    mono = audio_metrics(_loud(decorrelated=False))
    stereo = audio_metrics(_loud(decorrelated=True))
    assert mono["lr_correlation"] > 0.99
    assert stereo["lr_correlation"] < 0.5
    # ... and neither is a gate failure.
    assert evaluate(_probe(), mono, expect_rate=32000) == []


def test_no_audio_mode_skips_the_audio_arm() -> None:
    """``--no-audio`` lets the silent Wan/FlashVSR/RIFE corpus pass.

    Bug caught: the tool is wired into an existing video-only smoke and fails
    every one of them, so it gets removed instead of scoped.
    """
    assert (
        evaluate(_probe(audio=False), audio_metrics(_loud()), expect_audio=False) == []
    )


def test_a_missing_video_stream_fails_first() -> None:
    """No video is reported as such, not as an audio problem.

    Bug caught: the audio branch runs first and reports "NO AUDIO STREAM" for a
    file that is actually not a video at all, sending the reader down the wrong
    path.
    """
    assert evaluate({"streams": []}, audio_metrics(_loud())) == ["NO VIDEO STREAM"]


def test_scan_cuts_finds_a_hard_cut_and_ignores_smooth_motion() -> None:
    """A hard cut is an outlier; a pan is not.

    Bug caught: THE 2026-09-18 finding. Evenly-spaced frame sampling cannot tell
    a hard cut from a fast camera move — the max-length H3 clip looked like a
    very fast push-in on a 6-frame contact sheet and was actually a cut at frame
    272 (delta 70.05 against a median of 1.54). Without this, a two-shot output
    gets QA'd as one continuous take.
    """
    w, h = 96, 54
    # 20 frames drifting slowly, then one abrupt change, then drift again.
    frames = []
    for i in range(20):
        frames.append(np.full((h, w), 40 + i, dtype=np.uint8))
    for i in range(10):
        frames.append(np.full((h, w), 200 + i, dtype=np.uint8))
    raw = np.stack(frames).tobytes()

    hits = scan_cuts("ignored", run=lambda _a: raw, width=w, height=h)
    assert hits, "the cut was not detected"
    assert hits[0][0] == 20, hits
    # The smooth drift either side must not register.
    assert len(hits) == 1, hits


def test_decode_pcm_handles_a_track_with_no_audio() -> None:
    """An empty decode yields an empty array, not a crash.

    Bug caught: `np.frombuffer(b"")` then `.reshape(-1, 2)` on an odd count
    raises, so the tool dies on exactly the silent-video case it is meant to
    report.
    """
    assert decode_pcm("ignored", 32000, run=lambda _a: b"").shape == (0, 2)
    assert (
        audio_metrics(decode_pcm("ignored", 32000, run=lambda _a: b""))["peak"] == 0.0
    )
