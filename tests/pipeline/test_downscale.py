"""Tests for downscale_video_bytes."""

import shutil
import subprocess

import pytest

from kinoforge.core.frames import _default_run, ffprobe_dims
from kinoforge.pipeline.downscale import downscale_video_bytes


def test_argv_uses_lanczos_and_reads_input_file() -> None:
    # Behaviour: filter is scale=-2:{h}:flags=lanczos and input comes from a
    # seekable FILE (-i <path>), not stdin. Bug caught: dropping -2 (odd width
    # -> h264 reject), a blurrier filter, OR piping a large mp4 via pipe:0 which
    # fails demux (moov atom needs seeking) -> exit 183 on the real 1920² upscale.
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], stdin: bytes) -> bytes:
        seen["argv"] = argv
        seen["stdin"] = stdin
        return b"OUT"

    out = downscale_video_bytes(b"IN", 1080, run=fake_run)
    assert out == b"OUT"
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert "scale=-2:1080:flags=lanczos" in argv
    assert "-i" in argv
    # Input is a temp file path, not stdin bytes.
    assert seen["stdin"] == b""
    assert argv[argv.index("-i") + 1] != "pipe:0"


@pytest.mark.parametrize("bad", [0, -2, 1081])
def test_rejects_non_positive_or_odd_height(bad: int) -> None:
    # Behaviour: target must be a positive even int (h264). Bug caught: passing
    # an odd height straight to ffmpeg and getting a cryptic encoder failure.
    with pytest.raises(ValueError, match="positive even"):
        downscale_video_bytes(b"IN", bad, run=lambda a, s: b"")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_downscales_large_moov_at_end(tmp_path: object) -> None:
    # Behaviour: a LARGE 1920x1920 clip (moov atom at end, like a real FlashVSR
    # 4x upscale) downscales to 1080 -> (1080,1080), width even. Bug caught: the
    # old stdin-pipe path failed to demux large mp4 ('partial file / unspecified
    # pixel format', exit 183) because a non-seekable pipe can't reach the moov
    # atom (live smoke run 3, 2026-07-05). A 256x256 fixture was small enough to
    # slip through and missed this; the size here is what makes the test bite.
    src = tmp_path / "src.mp4"  # type: ignore[operator]
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=1920x1920:duration=3:rate=16",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    out_bytes = downscale_video_bytes(src.read_bytes(), 1080, run=_default_run)
    out = tmp_path / "out.mp4"  # type: ignore[operator]
    out.write_bytes(out_bytes)
    w, h = ffprobe_dims(out)
    assert h == 1080
    assert w % 2 == 0
