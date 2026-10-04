"""SpandrelRuntime.upscale_image — still in, PNG out, tiled by tile_size.

No torch here: the tiler is pure numpy and the runtime's two torch seams
(``_place_model``, ``_infer``) are monkeypatched with numpy fakes. The real
model runs only on the Task 9 pod.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget

_2X = ScaleTarget(kind="factor", value=2.0)


def _nn2x(batch: np.ndarray) -> np.ndarray:
    """Nearest-neighbour 2x on a uint8 NHWC batch — content-dependent on purpose.

    A zeros fake would make "tiled == untiled" vacuously true and hide seam
    and offset bugs.
    """
    return np.repeat(np.repeat(batch, 2, axis=1), 2, axis=2)


class _Counter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, ...]] = []

    def __call__(self, batch: np.ndarray) -> np.ndarray:
        self.calls.append(batch.shape)
        return _nn2x(batch)


@pytest.fixture
def fake_spandrel(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Inject a fake `spandrel` module so construction needs no weights."""
    model = MagicMock(name="SpandrelModel")
    model.scale = 2
    loader = MagicMock()
    loader.return_value.load_from_file = MagicMock(return_value=model)
    monkeypatch.setitem(
        sys.modules, "spandrel", types.SimpleNamespace(ModelLoader=loader)
    )
    return model


def _rt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tile_size: int,
    batch_size: int = 4,
) -> tuple[Any, _Counter]:
    """Runtime with both torch seams replaced by numpy fakes; returns (rt, infer_counter)."""
    from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

    weights = tmp_path / "fake.pth"
    weights.write_bytes(b"")
    rt = SpandrelRuntime(
        weights_path=weights,
        precision="fp32",
        tile_size=tile_size,
        batch_size=batch_size,
    )
    counter = _Counter()
    monkeypatch.setattr(rt, "_place_model", lambda: ("cpu", None))
    monkeypatch.setattr(rt, "_infer", lambda batch, device, dtype: counter(batch))
    return rt, counter


def _write(path: Path, arr: np.ndarray) -> Path:
    import imageio.v3 as iio

    iio.imwrite(path, arr)
    return path


def _read(path: Path) -> np.ndarray:
    import imageio.v3 as iio

    return np.asarray(iio.imread(path))


class TestTileUpscale:
    def test_tiled_equals_untiled_and_actually_tiles(self) -> None:
        # Bug caught: seams, off-by-one at the right/bottom remainder tiles,
        # overlap interiors copied from the wrong offset.
        from kinoforge.upscalers.spandrel._runtime import tile_upscale

        rng = np.random.default_rng(1)
        img = rng.integers(0, 255, (900, 1100, 3), dtype=np.uint8)
        whole = tile_upscale(img, scale=2, tile=0, overlap=32, infer=_nn2x)
        counter = _Counter()
        tiled = tile_upscale(img, scale=2, tile=512, overlap=32, infer=counter)
        assert whole.shape == (1800, 2200, 3)
        assert np.array_equal(whole, tiled)
        # 1100/512 -> 3 columns, 900/512 -> 2 rows: six tiles, six calls.
        assert len(counter.calls) == 6
        # The VRAM bound the tiler exists for: no patch wider than tile+2*overlap.
        assert all(h <= 512 + 64 and w <= 512 + 64 for (_, h, w, _) in counter.calls)

    def test_exactly_divisible_edges(self) -> None:
        from kinoforge.upscalers.spandrel._runtime import tile_upscale

        rng = np.random.default_rng(2)
        img = rng.integers(0, 255, (512, 1024, 3), dtype=np.uint8)
        counter = _Counter()
        tiled = tile_upscale(img, scale=2, tile=512, overlap=32, infer=counter)
        assert np.array_equal(tiled, _nn2x(img[None])[0])
        assert len(counter.calls) == 2

    def test_small_image_is_one_call(self) -> None:
        # Bug caught: a 100x70 still is needlessly cut into tiles, or tile=0
        # divides by zero.
        from kinoforge.upscalers.spandrel._runtime import tile_upscale

        img = np.full((70, 100, 3), 9, dtype=np.uint8)
        for tile in (0, 512):
            counter = _Counter()
            out = tile_upscale(img, scale=2, tile=tile, overlap=32, infer=counter)
            assert out.shape == (140, 200, 3)
            assert len(counter.calls) == 1


class TestToRgb:
    def test_alpha_is_dropped(self) -> None:
        # Bug caught: a 4-channel tensor reaches a 3-channel conv on the pod.
        from kinoforge.upscalers.spandrel._runtime import _to_rgb

        rgba = np.random.default_rng(0).integers(0, 255, (5, 6, 4), dtype=np.uint8)
        out = _to_rgb(rgba, Path("x.png"))
        assert out.shape == (5, 6, 3)
        assert np.array_equal(out, rgba[:, :, :3])

    def test_grey_is_broadcast(self) -> None:
        from kinoforge.upscalers.spandrel._runtime import _to_rgb

        out = _to_rgb(np.full((5, 6), 3, dtype=np.uint8), Path("g.png"))
        assert out.shape == (5, 6, 3)
        assert (out == 3).all()

    def test_rgb_passes_through(self) -> None:
        from kinoforge.upscalers.spandrel._runtime import _to_rgb

        rgb = np.zeros((5, 6, 3), dtype=np.uint8)
        assert _to_rgb(rgb, Path("c.png")) is rgb

    def test_two_channels_raise(self) -> None:
        from kinoforge.upscalers.spandrel._runtime import _to_rgb

        with pytest.raises(ValueError, match="shape"):
            _to_rgb(np.zeros((5, 6, 2), dtype=np.uint8), Path("bad.png"))


class TestUpscaleImage:
    def test_rgba_png_becomes_rgb_png_at_2x(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: output keeps the input suffix, is written as mp4, or
        # carries alpha.
        rng = np.random.default_rng(0)
        src = _write(
            tmp_path / "in.png", rng.integers(0, 255, (70, 100, 4), dtype=np.uint8)
        )
        rt, counter = _rt(tmp_path, monkeypatch, tile_size=512)
        out = rt.upscale_image(src, _2X, params={})
        assert out.name == "in.upscaled.png"
        assert _read(out).shape == (140, 200, 3)
        assert counter.calls == [(1, 70, 100, 3)]

    def test_jpeg_in_png_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock
    ) -> None:
        src = _write(tmp_path / "in.jpg", np.full((30, 40, 3), 90, dtype=np.uint8))
        rt, _ = _rt(tmp_path, monkeypatch, tile_size=512)
        out = rt.upscale_image(src, _2X, params={})
        assert out.suffix == ".png"
        assert _read(out).shape == (60, 80, 3)

    def test_large_image_is_tiled_through_infer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: upscale_image bypasses tile_upscale and sends the whole
        # still through the model (the OOM this feature exists to avoid).
        rng = np.random.default_rng(3)
        src = _write(
            tmp_path / "big.png", rng.integers(0, 255, (600, 1100, 3), dtype=np.uint8)
        )
        rt, counter = _rt(tmp_path, monkeypatch, tile_size=512)
        out = rt.upscale_image(src, _2X, params={})
        assert _read(out).shape == (1200, 2200, 3)
        assert len(counter.calls) == 6  # 3 columns x 2 rows


class TestRefusals:
    def test_height_target_refused_before_model_placement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock
    ) -> None:
        src = _write(tmp_path / "in.png", np.zeros((8, 8, 3), dtype=np.uint8))
        rt, counter = _rt(tmp_path, monkeypatch, tile_size=512)
        placed: list[bool] = []

        def _place_and_record() -> tuple[str, None]:
            placed.append(True)
            return "cpu", None

        monkeypatch.setattr(rt, "_place_model", _place_and_record)
        with pytest.raises(NotYetImplementedError, match="height"):
            rt.upscale_image(src, ScaleTarget(kind="height", value=1080.0), params={})
        assert placed == [] and counter.calls == []

    def test_scale_mismatch_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock
    ) -> None:
        src = _write(tmp_path / "in.png", np.zeros((8, 8, 3), dtype=np.uint8))
        rt, _ = _rt(tmp_path, monkeypatch, tile_size=512)
        with pytest.raises(UnsupportedScaleError):
            rt.upscale_image(src, ScaleTarget(kind="factor", value=4.0), params={})


class TestVideoPathUnchanged:
    def test_video_is_batched_not_tiled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: the image tiler is wired into the proven video path, or
        # the _infer extraction changed the batching.
        import imageio.v3 as iio

        frames = np.zeros((4, 48, 64, 3), dtype=np.uint8)
        src = tmp_path / "in.mp4"
        iio.imwrite(src, frames, fps=8, codec="libx264", macro_block_size=1)
        rt, counter = _rt(tmp_path, monkeypatch, tile_size=16, batch_size=4)
        out = rt.upscale(src, _2X, params={})
        assert out.name == "in.upscaled.mp4"
        assert counter.calls == [(4, 48, 64, 3)]
