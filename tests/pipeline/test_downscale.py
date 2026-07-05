"""Tests for downscale_video_bytes."""

import shutil
import subprocess

import pytest

from kinoforge.core.frames import _default_run, ffprobe_dims
from kinoforge.pipeline.downscale import downscale_video_bytes


def test_argv_uses_lanczos_and_even_width() -> None:
    # Behaviour: the ffmpeg filter is scale=-2:{h}:flags=lanczos. Bug caught:
    # dropping -2 (odd width -> h264 reject) or using a blurrier default filter.
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], stdin: bytes) -> bytes:
        seen["argv"] = argv
        seen["stdin"] = stdin
        return b"OUT"

    out = downscale_video_bytes(b"IN", 1080, run=fake_run)
    assert out == b"OUT"
    assert "scale=-2:1080:flags=lanczos" in seen["argv"]  # type: ignore[operator]
    assert seen["stdin"] == b"IN"


@pytest.mark.parametrize("bad", [0, -2, 1081])
def test_rejects_non_positive_or_odd_height(bad: int) -> None:
    # Behaviour: target must be a positive even int (h264). Bug caught: passing
    # an odd height straight to ffmpeg and getting a cryptic encoder failure.
    with pytest.raises(ValueError, match="positive even"):
        downscale_video_bytes(b"IN", bad, run=lambda a, s: b"")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_downscales_to_target(tmp_path: object) -> None:
    # Behaviour: a real 256x256 clip downscaled to 128 -> (128,128), width even.
    # Bug caught: aspect distortion or wrong output height end-to-end.
    src = tmp_path / "src.mp4"  # type: ignore[operator]
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=256x256:duration=1:rate=8",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    out_bytes = downscale_video_bytes(src.read_bytes(), 128, run=_default_run)
    out = tmp_path / "out.mp4"  # type: ignore[operator]
    out.write_bytes(out_bytes)
    w, h = ffprobe_dims(out)
    assert h == 128
    assert w % 2 == 0
