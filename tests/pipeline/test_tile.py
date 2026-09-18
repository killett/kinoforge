"""Tests for kinoforge.pipeline.tile — spatial tiling for wide-source upscales.

FlashVSR's attention canvas is its 4x OUTPUT, so a 960x544 source (3840x2176
out) needs a ~40 GiB block mask no card holds. Tiles whose 4x canvas fits are
upscaled separately and feather-blended back. Every expected number here is
hand-derived from the contract: tile size = ceil((dim + (n-1)*overlap) / n)
rounded UP to the 32-px alignment FlashVSR's attention window demands; tile i
sits at round((dim - tile) * i / (n - 1)) (even), so the effective overlap is
>= the requested one and the last tile ends exactly at the canvas edge.
"""

from __future__ import annotations

import numpy as np
import pytest

from kinoforge.pipeline.tile import (
    TileSpec,
    crop_argv,
    plan_tiles,
    rawvideo_read_argv,
    rawvideo_write_argv,
    stitch_frames,
    tile_weights,
)


class TestPlanTiles:
    def test_960x544_two_by_two_with_32px_overlap(self) -> None:
        # Bug caught: an unaligned 496-px tile (the naive (960+32)/2) dies on
        # the booked card with "Dims must divide by window size".
        assert plan_tiles(960, 544, cols=2, rows=2, overlap=32) == [
            TileSpec(x=0, y=0, w=512, h=288),
            TileSpec(x=448, y=0, w=512, h=288),
            TileSpec(x=0, y=256, w=512, h=288),
            TileSpec(x=448, y=256, w=512, h=288),
        ]

    def test_single_tile_is_the_whole_aligned_canvas(self) -> None:
        # Bug caught: a 1x1 grid still cropping, or padding a legal canvas.
        assert plan_tiles(640, 352, cols=1, rows=1, overlap=32) == [
            TileSpec(x=0, y=0, w=640, h=352)
        ]

    def test_single_tile_refuses_an_unaligned_canvas(self) -> None:
        # Bug caught: 640x360 sails through and fails on the pod, as it did
        # live on 2026-09-18.
        with pytest.raises(ValueError, match="32"):
            plan_tiles(640, 360, cols=1, rows=1, overlap=0)

    @pytest.mark.parametrize(
        ("width", "height", "cols", "rows", "overlap"),
        [
            (960, 544, 2, 2, 32),
            (960, 544, 3, 1, 32),
            (1344, 768, 2, 2, 64),
            (960, 544, 2, 2, 0),
        ],
    )
    def test_tiles_are_aligned_cover_the_canvas_and_overlap_enough(
        self, width: int, height: int, cols: int, rows: int, overlap: int
    ) -> None:
        # Bug caught: a gap between tiles, a tile running past the canvas, an
        # overlap thinner than requested (no room to feather), or odd offsets
        # (chroma-misaligned crops).
        tiles = plan_tiles(width, height, cols=cols, rows=rows, overlap=overlap)
        assert len(tiles) == cols * rows
        for t in tiles:
            assert t.w % 32 == 0 and t.h % 32 == 0
            assert t.x % 2 == 0 and t.y % 2 == 0
            assert 0 <= t.x and t.x + t.w <= width
            assert 0 <= t.y and t.y + t.h <= height
        xs = sorted({t.x for t in tiles})
        ys = sorted({t.y for t in tiles})
        assert xs[0] == 0 and ys[0] == 0
        assert xs[-1] + tiles[0].w == width
        assert ys[-1] + tiles[0].h == height
        for a, b in zip(xs, xs[1:], strict=False):
            assert a + tiles[0].w - b >= overlap
        for a, b in zip(ys, ys[1:], strict=False):
            assert a + tiles[0].h - b >= overlap

    @pytest.mark.parametrize(
        ("cols", "rows", "overlap"),
        [(0, 1, 0), (1, 0, 0), (2, 2, -1), (2, 2, 960)],
    )
    def test_rejects_degenerate_grids(self, cols: int, rows: int, overlap: int) -> None:
        # Bug caught: an overlap as wide as the canvas (every tile IS the
        # canvas, two identical renders) or a negative overlap silently planned.
        with pytest.raises(ValueError):
            plan_tiles(960, 544, cols=cols, rows=rows, overlap=overlap)


class TestCropArgv:
    def test_crop_filter_is_w_h_x_y_lossless_and_silent(self) -> None:
        # Bug caught: ffmpeg's crop takes w:h:x:y — swapping to x:y:w:h crops
        # the wrong region; a lossy intermediate would feed FlashVSR a second
        # generation of the pixels.
        argv = crop_argv("src.mp4", TileSpec(x=448, y=256, w=512, h=288), "t3.mp4")
        assert argv[argv.index("-i") + 1] == "src.mp4"
        assert argv[argv.index("-vf") + 1] == "crop=512:288:448:256"
        assert argv[argv.index("-qp") + 1] == "0"
        assert "-an" in argv
        assert argv[-1] == "t3.mp4"


class TestTileWeights:
    def test_interior_edges_ramp_over_the_actual_overlap_and_borders_do_not(
        self,
    ) -> None:
        # Bug caught: feathering the canvas border (darkening the frame edge),
        # a hard step at an interior edge (a visible seam), or ramping over the
        # REQUESTED overlap when the aligned tiles actually overlap by more.
        tiles = [TileSpec(x=0, y=0, w=32, h=32), TileSpec(x=16, y=0, w=32, h=32)]
        left, right = tile_weights(tiles, canvas_w=48, canvas_h=32)
        assert left.shape == right.shape == (32, 32)
        # Overlap is canvas columns [16, 32): right tile columns 0-15, left 16-31.
        assert np.allclose(right[:, 16:], 1.0)
        assert np.allclose(right[0, :16], np.arange(16) / 16)
        assert np.allclose(left[:, :16], 1.0)
        assert np.allclose(left[0, 16:], 1.0 - np.arange(16) / 16)
        assert np.allclose(
            left[0, :], left[-1, :]
        )  # no vertical feather: y is a border

    def test_two_by_two_weights_sum_to_one_everywhere(self) -> None:
        # Bug caught: ramps that do not sum to 1 brighten or darken every
        # overlap band — the classic tiling seam. 128/2/32 aligns to 96-px
        # tiles at 0 and 32, so the actual overlap is 64, not 32.
        tiles = plan_tiles(128, 128, cols=2, rows=2, overlap=32)
        assert tiles[1].x == 32 and tiles[2].y == 32
        total = np.zeros((128, 128), dtype=np.float64)
        for t, w in zip(
            tiles, tile_weights(tiles, canvas_w=128, canvas_h=128), strict=True
        ):
            total[t.y : t.y + t.h, t.x : t.x + t.w] += w
        assert np.allclose(total, 1.0)


class TestStitchFrames:
    def test_stitching_the_tiles_of_a_frame_reproduces_it(self) -> None:
        # Bug caught: a tile placed at the wrong offset, at the wrong scale, or
        # blended with mismatched ramps — any of which changes pixels away from
        # the ground-truth canvas. Smooth content makes a misplacement of even
        # one pixel measurable.
        h, w = 96, 128
        yy, xx = np.mgrid[0:h, 0:w]
        canvas = np.stack([xx * 2, yy * 2, (xx + yy)], axis=-1).astype(np.uint8)
        tiles = plan_tiles(w, h, cols=2, rows=2, overlap=16)  # 96x64 tiles at 0/32
        # "Upscaled" tiles at scale 1: exact crops of the canvas.
        streams = [[canvas[t.y : t.y + t.h, t.x : t.x + t.w]] for t in tiles]

        out = list(stitch_frames(streams, tiles, canvas_w=w, canvas_h=h, scale=1))

        assert len(out) == 1
        assert out[0].dtype == np.uint8 and out[0].shape == (h, w, 3)
        assert np.array_equal(out[0], canvas)

    def test_scale_places_tiles_at_scaled_offsets(self) -> None:
        # Bug caught: using SOURCE offsets on the 4x canvas — every tile but
        # the first lands 3/4 short and the right/bottom of the frame is empty.
        h, w, s = 32, 32, 4
        # Hand-built specs (the planner would align these to 32); the stitcher
        # itself must honour any spec it is given.
        tiles = [TileSpec(x=0, y=0, w=16, h=32), TileSpec(x=16, y=0, w=16, h=32)]
        left = np.full((32 * s, 16 * s, 3), 10, dtype=np.uint8)
        right = np.full((32 * s, 16 * s, 3), 200, dtype=np.uint8)

        (out,) = stitch_frames(
            [[left], [right]], tiles, canvas_w=w, canvas_h=h, scale=s
        )

        assert out.shape == (h * s, w * s, 3)
        assert np.all(out[:, : 16 * s] == 10)
        assert np.all(out[:, 16 * s :] == 200)

    def test_mismatched_frame_counts_raise(self) -> None:
        # Bug caught: a tile stream one frame short silently truncating the
        # whole clip (zip semantics) instead of failing loudly.
        tiles = [TileSpec(x=0, y=0, w=32, h=32), TileSpec(x=32, y=0, w=32, h=32)]
        a = np.zeros((32, 32, 3), dtype=np.uint8)
        with pytest.raises(ValueError, match="frame"):
            list(stitch_frames([[a, a], [a]], tiles, canvas_w=64, canvas_h=32, scale=1))


class TestRawvideoArgv:
    def test_reader_streams_rgb24_frames_to_stdout(self) -> None:
        # Bug caught: a missing -pix_fmt (ffmpeg picks yuv420p, 1.5 bytes/px)
        # makes every frame the wrong byte length and the numpy reshape fails
        # or, worse, silently skews.
        argv = rawvideo_read_argv("tile.mp4")
        assert argv[argv.index("-i") + 1] == "tile.mp4"
        assert argv[argv.index("-f") + 1] == "rawvideo"
        assert argv[argv.index("-pix_fmt") + 1] == "rgb24"
        assert argv[-1] == "pipe:1"

    def test_writer_declares_geometry_rate_and_encodes_h264(self) -> None:
        # Bug caught: rawvideo on stdin has no header — without -s/-r/-pix_fmt
        # BEFORE -i ffmpeg cannot parse it; a default-crf encode would add a
        # lossy generation before the 1080p downscale.
        argv = rawvideo_write_argv(3840, 2176, 24.0, "stitched.mp4")
        i = argv.index("-i")
        assert argv[i + 1] == "pipe:0"
        pre = argv[:i]
        assert pre[pre.index("-s") + 1] == "3840x2176"
        assert pre[pre.index("-r") + 1] == "24"
        assert pre[pre.index("-pix_fmt") + 1] == "rgb24"
        assert pre[pre.index("-f") + 1] == "rawvideo"
        post = argv[i:]
        assert post[post.index("-c:v") + 1] == "libx264"
        assert post[post.index("-crf") + 1] == "10"
        assert post[post.index("-pix_fmt") + 1] == "yuv420p"
        assert argv[-1] == "stitched.mp4"
