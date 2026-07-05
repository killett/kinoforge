"""Tests for ffprobe_dims."""

import pytest

from kinoforge.core.errors import FrameExtractionError
from kinoforge.core.frames import ffprobe_dims


def test_parses_width_x_height() -> None:
    # Behaviour: ffprobe csv "1920x1080" -> (1920, 1080). Bug caught: swapping
    # width/height or off-by-one CSV parsing.
    calls: list[list[str]] = []

    def fake_run(argv: list[str]) -> bytes:
        calls.append(argv)
        return b"1920x1080\n"

    assert ffprobe_dims("clip.mp4", run=fake_run) == (1920, 1080)
    # argv targets the first video stream and requests width,height as WxH.
    assert "v:0" in calls[0]
    assert "stream=width,height" in calls[0]


def test_unparseable_output_raises() -> None:
    # Behaviour: "N/A" (no video stream) -> FrameExtractionError, not ValueError.
    with pytest.raises(FrameExtractionError, match="unparseable"):
        ffprobe_dims("clip.mp4", run=lambda argv: b"N/A\n")
