"""FlashVSR stream-length padding (pure helpers in ``_input_prep``).

The FlashVSRFullPipeline streams latents in ``(num_frames - 1) // 8 - 2``
iterations and returns ``8 * ((num_frames - 1) // 8) - 3`` frames — four
fewer than an ``8n+1`` input, and it rounds any other count to ``4k+1``
without padding the LQ tensor, which is the conv-size error a 40-frame chunk
hit live on 2026-09-18. Upstream's own script appends clones of the last
frame; these helpers pad UP so no real frame is ever lost.
"""

from __future__ import annotations

import pytest

from kinoforge.upscalers.flashvsr._input_prep import (
    pad_frames_to_stream_length,
    stream_frame_count,
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (40, 49),  # 44 -> next 8n+1
        (37, 41),  # 41 exactly: output 37 == source, no waste
        (45, 49),
        (77, 81),  # §24's 77-frame source lost 8 frames unpadded; now 81 in, 77 out
        (81, 89),
        (1, 9),
        (69, 73),  # 73 is already 8n+1 and 73 - 4 == 69: nothing wasted
    ],
)
def test_stream_frame_count_is_smallest_8n1_whose_output_covers_the_source(
    source: int, expected: int
) -> None:
    # Bug caught: rounding to 4k+1 (40 -> 41) or to the 8n+1 BELOW (upstream's
    # largest_8n1_leq) — either way the pipeline returns fewer frames than
    # the source has and the chunk join loses frames at every seam.
    assert stream_frame_count(source) == expected
    assert (expected - 1) % 8 == 0
    assert expected - 4 >= source


def test_stream_frame_count_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        stream_frame_count(0)


def test_pad_clones_the_last_frame_up_to_the_stream_length() -> None:
    # Bug caught: cloning the FIRST frame, inserting clones at the head, or
    # reordering — each shifts the output so frame i no longer matches
    # source frame i and the overlap trim cuts the wrong frames.
    frames = [object() for _ in range(40)]

    padded, total = pad_frames_to_stream_length(frames)

    assert total == 49
    assert len(padded) == 49
    assert all(a is b for a, b in zip(padded[:40], frames, strict=True))
    assert all(f is frames[-1] for f in padded[40:])
    assert frames == frames[:]  # input list untouched


def test_pad_refuses_an_empty_clip() -> None:
    with pytest.raises(ValueError):
        pad_frames_to_stream_length([])
