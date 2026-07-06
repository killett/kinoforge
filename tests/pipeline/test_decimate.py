"""decimate_video_fps: ffmpeg fps re-timing over a seekable temp file."""

import shutil
import subprocess

import pytest

from kinoforge.core.frames import ffprobe_fps
from kinoforge.pipeline.decimate import _decimate_argv, decimate_video_fps


def test_argv_uses_fps_filter_and_file_input():
    # Bug caught: reading pipe:0 fails on large mp4 (moov seek, exit 183).
    argv = _decimate_argv("/tmp/x.mp4", 24.0)
    joined = " ".join(argv)
    assert "fps=24" in joined
    assert "pipe:0" not in argv
    assert "-i" in argv and "/tmp/x.mp4" in argv


def test_ntsc_target_serialized_exactly():
    # Bug caught: str(29.97) rounding drifts; keep NTSC exact.
    argv = _decimate_argv("/tmp/x.mp4", 29.97)
    assert "fps=30000/1001" in " ".join(argv)


def test_seam_receives_argv_and_empty_stdin():
    seen = {}

    def run(argv, stdin):
        seen["argv"] = argv
        seen["stdin"] = stdin
        return b"OUT"

    out = decimate_video_fps(b"INPUT", 24.0, run=run)
    assert out == b"OUT"
    assert seen["stdin"] == b""  # bytes go via temp file, not stdin


def test_non_positive_target_raises():
    with pytest.raises(ValueError):
        decimate_video_fps(b"x", 0.0, run=lambda a, s: b"")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_hits_target_fps(tmp_path):
    # Generate a 1s 30fps clip, decimate to 15, assert probed rate.
    src = tmp_path / "src.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x64:rate=30:duration=1",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    out = decimate_video_fps(src.read_bytes(), 15.0)
    dst = tmp_path / "out.mp4"
    dst.write_bytes(out)
    assert round(ffprobe_fps(dst)) == 15
