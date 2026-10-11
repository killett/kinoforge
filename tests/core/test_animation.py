"""`core/animation.py`: reassemble upscaled frames into the input's container."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import kinoforge.core.animation as animation
import kinoforge.core.image_dir as image_dir
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


def _flat_frames(n: int = 4, size: tuple[int, int] = (8, 8)) -> list[Image.Image]:
    """Flat, distinct per-frame colours: spatially smooth content that a
    lossy codec's 4:2:0 chroma subsampling does not fight (unlike full
    per-pixel random noise), while still differing frame-to-frame so a
    scrambled frame order still fails an RGB-closeness assertion.
    """
    return [Image.new("RGB", size, (40 * i + 30, 200 - 40 * i, 90)) for i in range(n)]


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
        # lost on the lossy path, frames reordered. The fixture is flat,
        # distinct-per-frame colour (not random noise): lossy WebP's 4:2:0
        # chroma subsampling blends a pixel's colour with its spatial
        # neighbours regardless of quality/method, so a fully-random-noise
        # fixture makes an RGB-closeness check meaningless at any tight
        # tolerance (measured diff 70-77/255 even at quality=100 on random
        # noise) while also being too weak a check (~20% chance two
        # unrelated random pixels land within a wide tolerance by chance) to
        # reliably catch a swapped-frame bug. Flat colour keeps the
        # assertion both meaningful (frames differ, so a swap still trips
        # it) and achievable at the brief's tolerance of 12.
        frames, masks = _flat_frames(), _masks()
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
        assert _close(
            pix[3].getpixel((0, 0))[:3],  # type: ignore[index]
            frames[3].getpixel((0, 0)),  # type: ignore[arg-type]
            12,
        )

    def test_round_trip_without_alpha(self, tmp_path: Path) -> None:
        # Bug caught: a None mask crashes the writer or yields alpha 0; RGB
        # corrupted on the no-alpha WebP path (e.g. a channel swap, or a
        # frame-order bug the alpha-only check cannot see since every frame
        # is uniformly opaque).
        frames = _flat_frames()
        body = assemble_animation(
            _write(tmp_path, frames),
            durations_ms=DUR,
            loop=0,
            alphas=[None] * 4,
            output_suffix=".webp",
        )
        n, _durs, _loop, pix = _read(body)
        assert n == 4
        for p, f in zip(pix, frames, strict=True):
            assert p.getpixel((3, 3))[3] == 255  # type: ignore[index]
            assert _close(
                p.getpixel((3, 3))[:3],  # type: ignore[index]
                f.getpixel((3, 3)),  # type: ignore[arg-type]
                12,
            )


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
        # colour and marks a real pixel transparent; durations dropped on
        # the no-alpha GIF path.
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
        _n, durs, loop, _pix = _read(body)
        assert durs == DUR and loop == 0

    def test_absent_loop_writes_no_extension(self, tmp_path: Path) -> None:
        # Bug caught: a play-once input (loop=None, no Netscape extension)
        # ships as an infinite loop because the writer always receives a
        # `loop=` keyword.
        frames = _flat_frames(2)
        body = assemble_animation(
            _write(tmp_path, frames),
            durations_ms=[100, 100],
            loop=None,
            alphas=[None, None],
            output_suffix=".gif",
        )
        with Image.open(io.BytesIO(body)) as im:
            assert im.info.get("loop") is None


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
    @pytest.mark.parametrize("suffix", [".tif", ".TIFF"])
    def test_multi_page(self, tmp_path: Path, suffix: str) -> None:
        # Bug caught: only the first page written; an upper-case suffix
        # (".TIFF") not recognised by the writer lookup.
        frames = _frames(2)
        body = assemble_animation(
            _write(tmp_path, frames),
            durations_ms=[0, 0],
            loop=0,
            alphas=[None, None],
            output_suffix=suffix,
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

    def test_length_mismatch(self, tmp_path: Path) -> None:
        # Bug caught: frames/durations/alphas silently truncated via zip()
        # instead of raising on a caller bug.
        paths = _write(tmp_path, _frames(2))
        with pytest.raises(ValueError, match="length mismatch"):
            assemble_animation(
                paths,
                durations_ms=[1],
                loop=0,
                alphas=[None, None],
                output_suffix=".webp",
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


class TestParity:
    def test_animated_suffixes_have_writers(self) -> None:
        # Bug caught: a suffix added to the planner's _ANIMATED_SUFFIXES
        # without a matching assembler writer raises `ValueError: no
        # animated writer` only AFTER every frame has been paid for on the
        # GPU — the two sets must be kept in lockstep.
        assert set(animation._WRITERS) == image_dir._ANIMATED_SUFFIXES
        # Guard the guard: a sweep matching nothing (both sets empty) would
        # pass the equality check above for free.
        assert len(image_dir._ANIMATED_SUFFIXES) >= 5
