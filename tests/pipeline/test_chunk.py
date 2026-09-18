"""Tests for kinoforge.pipeline.chunk — temporal chunk planning + ffmpeg argv.

Every expected value here is hand-derived from the chunk contract, never from
running the planner: chunk ``i`` keeps source frames
``[i*chunk_frames, min((i+1)*chunk_frames, total))`` and additionally RENDERS
``overlap`` frames before that range (clamped at 0) as a warm-up it then
discards, so ``count - trim_head`` summed over chunks equals ``total``.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import FrameExtractionError
from kinoforge.pipeline.chunk import (
    ChunkSpec,
    concat_argv,
    ffprobe_frames,
    plan_chunks,
    split_argv,
)


class TestPlanChunks:
    def test_345_frames_by_69_with_overlap_8(self) -> None:
        # Bug caught: an off-by-one in the overlap start (e.g. 62 instead of
        # 61) duplicates or drops a frame at every seam of the H3 max clip.
        assert plan_chunks(345, chunk_frames=69, overlap=8) == [
            ChunkSpec(start=0, count=69, trim_head=0),
            ChunkSpec(start=61, count=77, trim_head=8),
            ChunkSpec(start=130, count=77, trim_head=8),
            ChunkSpec(start=199, count=77, trim_head=8),
            ChunkSpec(start=268, count=77, trim_head=8),
        ]

    def test_ragged_tail_keeps_only_the_remaining_frames(self) -> None:
        # Bug caught: the last chunk sized to a full window (75, 45) reads
        # past the end, or is dropped so frames 80-99 never render.
        assert plan_chunks(100, chunk_frames=40, overlap=5) == [
            ChunkSpec(start=0, count=40, trim_head=0),
            ChunkSpec(start=35, count=45, trim_head=5),
            ChunkSpec(start=75, count=25, trim_head=5),
        ]

    def test_clip_no_longer_than_a_chunk_is_one_untrimmed_chunk(self) -> None:
        # Bug caught: a short clip is split anyway or has overlap frames
        # trimmed off its head, changing every proven <= 81-frame upscale.
        assert plan_chunks(50, chunk_frames=69, overlap=8) == [
            ChunkSpec(start=0, count=50, trim_head=0)
        ]
        assert plan_chunks(69, chunk_frames=69, overlap=8) == [
            ChunkSpec(start=0, count=69, trim_head=0)
        ]

    @pytest.mark.parametrize(
        ("total", "chunk_frames", "overlap"),
        [(100, 50, 10), (100, 40, 0), (345, 69, 8), (7, 3, 2), (81, 80, 8)],
    )
    def test_kept_ranges_tile_the_clip_exactly_once(
        self, total: int, chunk_frames: int, overlap: int
    ) -> None:
        # Bug caught: any seam gap or duplication — the kept range of chunk
        # i+1 must begin exactly where chunk i's kept range ended.
        specs = plan_chunks(total, chunk_frames=chunk_frames, overlap=overlap)
        expected_next = 0
        for spec in specs:
            first_kept = spec.start + spec.trim_head
            assert first_kept == expected_next
            expected_next = spec.start + spec.count
            assert spec.count <= chunk_frames + overlap
        assert expected_next == total

    @pytest.mark.parametrize(
        ("total", "chunk_frames", "overlap"),
        [(0, 10, 2), (100, 0, 2), (100, 10, -1), (100, 10, 10), (100, 10, 11)],
    )
    def test_rejects_degenerate_parameters(
        self, total: int, chunk_frames: int, overlap: int
    ) -> None:
        # Bug caught: overlap >= chunk_frames makes every chunk's kept range
        # empty-or-negative; the planner must refuse rather than loop or
        # emit chunks that render nothing.
        with pytest.raises(ValueError):
            plan_chunks(total, chunk_frames=chunk_frames, overlap=overlap)


class TestSplitArgv:
    def test_trim_is_frame_accurate_with_exclusive_end(self) -> None:
        # Bug caught: ffmpeg's trim end_frame is EXCLUSIVE; writing
        # end_frame=137 or 139 yields a 76- or 78-frame chunk, and the
        # concat then has a seam gap or duplicate.
        argv = split_argv(
            "src.mp4", ChunkSpec(start=61, count=77, trim_head=8), "c1.mp4"
        )
        assert argv[0] == "ffmpeg"
        assert argv[argv.index("-i") + 1] == "src.mp4"
        vf = argv[argv.index("-vf") + 1]
        assert vf == "trim=start_frame=61:end_frame=138,setpts=PTS-STARTPTS"
        assert argv[-1] == "c1.mp4"

    def test_chunk_is_written_losslessly_without_audio(self) -> None:
        # Bug caught: a default-crf intermediate re-encodes the source a
        # second time before FlashVSR ever sees it; an audio stream on the
        # chunk would be uploaded for nothing.
        argv = split_argv(
            "src.mp4", ChunkSpec(start=0, count=69, trim_head=0), "c0.mp4"
        )
        assert argv[argv.index("-qp") + 1] == "0"
        assert "-an" in argv


class TestConcatArgv:
    def test_trims_each_head_then_concats_video_only(self) -> None:
        # Bug caught: concat n= mismatch, a missing setpts reset (timestamp
        # gap -> stutter), or trimming the FIRST chunk's head too.
        argv = concat_argv(
            [("u0.mp4", 0), ("u1.mp4", 8), ("u2.mp4", 8)],
            "joined.mp4",
        )
        inputs = [argv[i + 1] for i, tok in enumerate(argv) if tok == "-i"]
        assert inputs == ["u0.mp4", "u1.mp4", "u2.mp4"]
        fc = argv[argv.index("-filter_complex") + 1]
        assert fc == (
            "[0:v]trim=start_frame=0,setpts=PTS-STARTPTS[v0];"
            "[1:v]trim=start_frame=8,setpts=PTS-STARTPTS[v1];"
            "[2:v]trim=start_frame=8,setpts=PTS-STARTPTS[v2];"
            "[v0][v1][v2]concat=n=3:v=1:a=0[v]"
        )
        assert argv[argv.index("-map") + 1] == "[v]"
        assert argv[-1] == "joined.mp4"

    def test_refuses_empty_part_list(self) -> None:
        # Bug caught: concat=n=0 is an ffmpeg error surfaced minutes later
        # as an opaque exit code instead of a clear ValueError now.
        with pytest.raises(ValueError):
            concat_argv([], "joined.mp4")


class TestFfprobeFrames:
    def test_parses_the_stream_frame_count(self) -> None:
        # Bug caught: returning the raw string, or probing the format block
        # (which has no frame count) instead of stream=nb_frames.
        seen: list[list[str]] = []

        def fake_run(argv: list[str]) -> bytes:
            seen.append(argv)
            return b"345\n"

        assert ffprobe_frames("x.mp4", run=fake_run) == 345
        assert seen[0][0] == "ffprobe"
        assert "stream=nb_frames" in seen[0]
        assert seen[0][-1] == "x.mp4"

    def test_unparseable_count_raises_frame_extraction_error(self) -> None:
        # Bug caught: a bare int() ValueError leaks for a stream whose
        # container reports N/A, instead of the module's own error type.
        with pytest.raises(FrameExtractionError):
            ffprobe_frames("x.mp4", run=lambda argv: b"N/A\n")
