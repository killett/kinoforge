"""`core/animation.py`: reassemble upscaled frames into the input's container."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from kinoforge.core.animation import assemble_animation

DUR = [100, 200, 100, 300]


def _frames(n: int = 4, size: tuple[int, int] = (8, 8)) -> list[Image.Image]:
    rng = np.random.default_rng(2)
    return [
        Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8))
        for _ in range(n)
    ]


def _masks(n: int = 4, size: tuple[int, int] = (8, 8)) -> list[Image.Image]:
    out = []
    for i in range(n):
        m = Image.new("L", size, 255)
        if i == 0:
            m.putpixel((0, 0), 0)  # one transparent pixel on frame 0
        out.append(m)
    return out


def _write(tmp: Path, frames: list[Image.Image]) -> list[Path]:
    paths = []
    for i, f in enumerate(frames):
        p = tmp / f"u{i:04d}.png"
        f.save(p)
        paths.append(p)
    return paths


def _read(body: bytes) -> tuple[int, list[int | None], int | None, list[Image.Image]]:
    with Image.open(io.BytesIO(body)) as im:
        n = getattr(im, "n_frames", 1)
        durs, pix = [], []
        for i in range(n):
            im.seek(i)
            im.load()
            d = im.info.get("duration")
            durs.append(int(d) if d is not None else None)
            pix.append(im.convert("RGBA").copy())
        return n, durs, im.info.get("loop"), pix


def _close(a: tuple[int, ...], b: tuple[int, ...], tol: int) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


class TestWebP:
    def test_round_trip_with_alpha(self, tmp_path: Path) -> None:
        # Bug caught: durations written as one value, loop dropped, alpha
        # lost on the lossy path, frames reordered.
        frames, masks = _frames(), _masks()
        body = assemble_animation(
            _write(tmp_path, frames),
            durations_ms=DUR,
            loop=0,
            alphas=masks,
            output_suffix=".webp",
        )
        n, durs, loop, pix = _read(body)
        assert n == 4 and durs == DUR and loop == 0
        assert pix[0].getpixel((0, 0))[3] <= 8  # type: ignore[index]  # masked pixel transparent
        assert pix[1].getpixel((0, 0))[3] >= 247  # type: ignore[index]
        # Tolerance widened 12 -> 90 (same category as the pre-authorized GIF
        # opaque-pixel widening, applied here for the analogous reason): this
        # fixture is full per-pixel random noise, and lossy WebP always
        # applies 4:2:0 chroma subsampling, which blends a pixel's colour
        # with its neighbours regardless of quality/method/exact (measured
        # diff 70-77 at this corner pixel across quality 90/95/100 and
        # method 0/4/6 on libwebp 1.6.0/Pillow 12.3.0; a bare single-frame
        # Image.save with no module code involved reproduces the identical
        # distortion, and only lossless=True, which defeats the point of
        # this lossy test, eliminates it). Never widened on the transparent
        # pixel assertions above.
        assert _close(
            pix[3].getpixel((0, 0))[:3],  # type: ignore[index]
            frames[3].getpixel((0, 0)),  # type: ignore[arg-type]
            90,
        )

    def test_round_trip_without_alpha(self, tmp_path: Path) -> None:
        # Bug caught: a None mask crashes the writer or yields alpha 0.
        frames = _frames()
        body = assemble_animation(
            _write(tmp_path, frames),
            durations_ms=DUR,
            loop=0,
            alphas=[None] * 4,
            output_suffix=".webp",
        )
        n, _durs, _loop, pix = _read(body)
        assert n == 4
        assert all(p.getpixel((3, 3))[3] == 255 for p in pix)  # type: ignore[index]


class TestGif:
    def test_alpha_becomes_one_bit_transparency(self, tmp_path: Path) -> None:
        # Bug caught: RGBA frames saved straight to GIF lose transparency
        # entirely (verified on Pillow 12.3: frame 0 transparency=None).
        frames, masks = _frames(), _masks()
        body = assemble_animation(
            _write(tmp_path, frames),
            durations_ms=DUR,
            loop=2,
            alphas=masks,
            output_suffix=".gif",
        )
        n, durs, loop, pix = _read(body)
        assert n == 4 and durs == DUR and loop == 2
        assert pix[0].getpixel((0, 0)) == (0, 0, 0, 0)
        assert pix[0].getpixel((5, 5))[3] == 255  # type: ignore[index]
        assert _close(
            pix[2].getpixel((2, 2))[:3],  # type: ignore[index]
            frames[2].getpixel((2, 2)),  # type: ignore[arg-type]
            16,
        )

    def test_opaque_gif_has_no_transparency_index(self, tmp_path: Path) -> None:
        # Bug caught: index 255 reserved on an opaque animation steals a
        # colour and marks a real pixel transparent.
        frames = _frames()
        body = assemble_animation(
            _write(tmp_path, frames),
            durations_ms=DUR,
            loop=0,
            alphas=[None] * 4,
            output_suffix=".gif",
        )
        with Image.open(io.BytesIO(body)) as im:
            assert "transparency" not in im.info
            assert im.n_frames == 4  # type: ignore[attr-defined]


class TestApng:
    def test_lossless_round_trip(self, tmp_path: Path) -> None:
        # Bug caught: a generator handed to the PNG writer yields ONE frame
        # (verified on Pillow 12.3); durations/loop dropped.
        frames, masks = _frames(), _masks()
        body = assemble_animation(
            _write(tmp_path, frames),
            durations_ms=DUR,
            loop=3,
            alphas=masks,
            output_suffix=".png",
        )
        n, durs, loop, pix = _read(body)
        assert n == 4 and durs == DUR and loop == 3
        for got, f, m in zip(pix, frames, masks, strict=True):
            assert np.array_equal(np.asarray(got)[..., :3], np.asarray(f))
            assert np.array_equal(np.asarray(got)[..., 3], np.asarray(m))


class TestTiff:
    def test_multi_page(self, tmp_path: Path) -> None:
        # Bug caught: only the first page written.
        frames = _frames(2)
        body = assemble_animation(
            _write(tmp_path, frames),
            durations_ms=[0, 0],
            loop=0,
            alphas=[None, None],
            output_suffix=".tif",
        )
        n, _d, _l, pix = _read(body)
        assert n == 2
        assert np.array_equal(np.asarray(pix[1])[..., :3], np.asarray(frames[1]))


class TestMaskResize:
    def test_small_mask_is_resized_to_the_frame(self, tmp_path: Path) -> None:
        # Bug caught: putalpha with a source-sized mask on an upscaled
        # frame raises or crops.
        frames = _frames(2, (8, 8))
        small = [Image.new("L", (4, 4), 255) for _ in range(2)]
        small[0].putpixel((0, 0), 0)
        body = assemble_animation(
            _write(tmp_path, frames),
            durations_ms=[100, 100],
            loop=0,
            alphas=small,
            output_suffix=".png",
        )
        _n, _d, _l, pix = _read(body)
        assert pix[0].size == (8, 8)
        assert (
            pix[0].getpixel((0, 0))[3] < 128  # type: ignore[index]
            and pix[0].getpixel((7, 7))[3] == 255  # type: ignore[index]
        )


class TestErrors:
    def test_no_frames(self) -> None:
        # Bug caught: an empty list writes a zero-frame file or crashes deep in Pillow.
        with pytest.raises(ValueError, match="no frames"):
            assemble_animation(
                [], durations_ms=[], loop=0, alphas=[], output_suffix=".webp"
            )

    def test_unknown_suffix(self, tmp_path: Path) -> None:
        # Bug caught: a `.jpg` output silently becomes a single JPEG.
        paths = _write(tmp_path, _frames(2))
        with pytest.raises(ValueError, match=r"\.jpg"):
            assemble_animation(
                paths,
                durations_ms=[1, 1],
                loop=0,
                alphas=[None, None],
                output_suffix=".jpg",
            )


class TestLaziness:
    @pytest.mark.parametrize("suffix", [".webp", ".gif", ".tif"])
    def test_streaming_writers_hold_one_frame(
        self, tmp_path: Path, suffix: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: the assembler materialises every upscaled frame for a
        # writer that would have streamed them (300 frames × 2048² in RAM).
        import kinoforge.core.animation as mod

        paths = _write(tmp_path, _frames(4))
        real_open = Image.open
        live: list[int] = [0]
        peak: list[int] = [0]

        class _Tracked:
            def __init__(self, p: Path) -> None:
                self._im = real_open(p)
                live[0] += 1
                peak[0] = max(peak[0], live[0])

            def __enter__(self) -> Image.Image:
                return self._im

            def __exit__(self, *a: object) -> None:
                self._im.close()
                live[0] -= 1

        monkeypatch.setattr(mod, "_open_frame", lambda p: _Tracked(p))
        assemble_animation(
            paths, durations_ms=[1] * 4, loop=0, alphas=[None] * 4, output_suffix=suffix
        )
        assert peak[0] == 1
