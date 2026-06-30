"""Tests for SpandrelRuntime — frame-loop video upscale wrapper."""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget


@pytest.fixture
def _fake_spandrel(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Inject a fake `spandrel` module so tests don't need real weights."""
    fake_model = MagicMock(name="SpandrelModel")
    fake_model.scale = 2

    def _fake_call(tensor: Any) -> Any:
        import torch

        if isinstance(tensor, torch.Tensor):
            n, c, h, w = tensor.shape
            return torch.zeros(
                (n, c, h * 2, w * 2), dtype=tensor.dtype, device=tensor.device
            )
        raise TypeError(f"unexpected input: {type(tensor)}")

    fake_model.side_effect = _fake_call
    fake_model.return_value = None

    fake_loader = MagicMock(name="ModelLoader")
    fake_loader_instance = MagicMock()
    fake_loader_instance.load_from_file = MagicMock(return_value=fake_model)
    fake_loader.return_value = fake_loader_instance

    fake_spandrel = types.SimpleNamespace(ModelLoader=fake_loader)
    monkeypatch.setitem(sys.modules, "spandrel", fake_spandrel)
    return fake_model


def _write_dummy_mp4(
    path: Path, width: int = 64, height: int = 48, frames: int = 4
) -> None:
    """Write a tiny mp4 using imageio.ffmpeg.

    Frames are solid colors so the test focuses on dimensions, not content.
    """
    import imageio.v3 as iio

    data = np.zeros((frames, height, width, 3), dtype=np.uint8)
    for i in range(frames):
        data[i, :, :, i % 3] = 200
    iio.imwrite(path, data, fps=8, codec="libx264", macro_block_size=1)


def _probe_dims(path: Path) -> tuple[int, int]:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(path),
        ],
        text=True,
    ).strip()
    w, h = out.split("x")
    return int(w), int(h)


class TestConstruction:
    def test_does_not_import_spandrel_at_module_load(self) -> None:
        # Bug caught: a regression that moves `from spandrel import ...`
        # to module-top crashes `import kinoforge` on hosts that don't
        # have the spandrel package installed (e.g. dev workstation).
        sys.modules.pop("spandrel", None)
        from kinoforge.upscalers.spandrel import _runtime  # noqa: F401

    def test_lazy_import_fires_inside_constructor(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: lazy-import contract drifts — spandrel becomes
        # required at class-body eval time instead of construction time.
        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")
        rt = SpandrelRuntime(
            weights_path=weights, precision="fp16", tile_size=512, batch_size=4
        )
        assert rt is not None


class TestPrecisionCast:
    def test_fp16_upscale_casts_model_to_half_before_inference(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: SpandrelRuntime declares precision="fp16" and casts
        # INPUT tensors to fp16, but never casts the model weights →
        # PyTorch "Input type (c10::Half) and bias type (float) should be
        # the same" at first inference. upscale() MUST recast model
        # weights so input/weight dtypes agree.
        torch = pytest.importorskip("torch")

        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")
        rt = SpandrelRuntime(
            weights_path=weights, precision="fp16", tile_size=512, batch_size=4
        )
        video = tmp_path / "in.mp4"
        _write_dummy_mp4(video)
        rt.upscale(video, ScaleTarget(kind="factor", value=2.0), params={})
        rt._model.to.assert_any_call(torch.float16)

    def test_fp32_upscale_casts_model_to_float_before_inference(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: a one-line fix that always casts to float16 breaks
        # the fp32 path. precision=="fp32" must cast model to torch.float32
        # (idempotent on already-fp32 weights but explicit so a future
        # fp16-default load does not corrupt fp32-requested inference).
        torch = pytest.importorskip("torch")

        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")
        rt = SpandrelRuntime(
            weights_path=weights, precision="fp32", tile_size=512, batch_size=4
        )
        video = tmp_path / "in.mp4"
        _write_dummy_mp4(video)
        rt.upscale(video, ScaleTarget(kind="factor", value=2.0), params={})
        rt._model.to.assert_any_call(torch.float32)
        for call in rt._model.to.call_args_list:
            assert torch.float16 not in call.args


class TestUpscale:
    def test_factor_2x_returns_2x_resolution_mp4(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: runtime emits a same-size or 4x mp4 because the
        # batch loop doesn't honour the model's declared scale.
        pytest.importorskip("torch")
        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        src = tmp_path / "in.mp4"
        _write_dummy_mp4(src, width=64, height=48, frames=4)
        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")

        rt = SpandrelRuntime(
            weights_path=weights, precision="fp16", tile_size=512, batch_size=4
        )
        out = rt.upscale(src, ScaleTarget(kind="factor", value=2.0), params={})
        assert out.exists()
        w, h = _probe_dims(out)
        assert (w, h) == (128, 96)

    def test_height_target_refused(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: runtime accepts `kind="height"` and produces
        # off-scale output that the asserts downstream can't validate.
        # Symmetric with the SeedVR2Runtime refusal.
        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        src = tmp_path / "in.mp4"
        _write_dummy_mp4(src, width=64, height=48, frames=2)
        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")

        rt = SpandrelRuntime(
            weights_path=weights, precision="fp16", tile_size=512, batch_size=4
        )
        with pytest.raises(NotYetImplementedError, match="height"):
            rt.upscale(src, ScaleTarget(kind="height", value=1080.0), params={})

    def test_scale_mismatch_refused(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: cfg asks for 4x but the loaded weights only
        # support 2x. Without an explicit check the runtime would
        # silently emit a 2x clip and the operator wouldn't notice
        # until visual review.
        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        src = tmp_path / "in.mp4"
        _write_dummy_mp4(src, width=64, height=48, frames=2)
        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")

        rt = SpandrelRuntime(
            weights_path=weights, precision="fp16", tile_size=512, batch_size=4
        )
        with pytest.raises(UnsupportedScaleError):
            rt.upscale(src, ScaleTarget(kind="factor", value=4.0), params={})


class TestDeviceMove:
    def test_to_device_delegates_to_underlying_model(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: SpandrelRuntime.to() forgets to forward; LRU
        # registry's CPU eviction call becomes a no-op and the pod
        # OOMs the next time a Wan generation starts.
        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")

        rt = SpandrelRuntime(
            weights_path=weights, precision="fp16", tile_size=512, batch_size=4
        )
        rt.to("cpu")
        _fake_spandrel.to.assert_called_with("cpu")
