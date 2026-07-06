"""ffprobe_fps parses ffprobe's rational r_frame_rate into a float."""

import math
from collections.abc import Callable

import pytest

from kinoforge.core.errors import FrameExtractionError
from kinoforge.core.frames import ffprobe_fps


def _seam(out: bytes) -> Callable[[list[str]], bytes]:
    return lambda argv: out


def test_integer_rate():
    # Bug caught: naive float("16/1") raises; must divide the rational.
    assert ffprobe_fps("x.mp4", run=_seam(b"16/1\n")) == 16.0


def test_ntsc_rational_rate():
    # Bug caught: dropping the denominator would report 30000.0 fps.
    got = ffprobe_fps("x.mp4", run=_seam(b"30000/1001\n"))
    assert math.isclose(got, 29.97002997, rel_tol=1e-6)


def test_argv_targets_r_frame_rate():
    # Bug caught: probing avg_frame_rate returns 0/0 for VFR/streamed inputs.
    captured = {}

    def run(argv):
        captured["argv"] = argv
        return b"24/1\n"

    ffprobe_fps("clip.mp4", run=run)
    assert "r_frame_rate" in " ".join(captured["argv"])
    assert captured["argv"][-1] == "clip.mp4"


def test_unparseable_raises():
    with pytest.raises(FrameExtractionError):
        ffprobe_fps("x.mp4", run=_seam(b"N/A\n"))


def test_zero_denominator_raises():
    # Bug caught: "0/0" (no timing) must error, not ZeroDivisionError-crash.
    with pytest.raises(FrameExtractionError):
        ffprobe_fps("x.mp4", run=_seam(b"0/0\n"))
