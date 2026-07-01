"""FlashVSRRuntime: LRU contract + scale validation + prompt-ignore behavior."""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget


class _StubPipe:
    """Duck-types StreamingDMDPipeline for unit tests."""

    def __init__(self, native_scale: float = 2.0) -> None:
        self.scale = native_scale
        self._device = "cpu"
        self.stream_calls: list[dict[str, Any]] = []

    @classmethod
    def from_pretrained(cls, weights_dir: str, **kwargs: Any) -> _StubPipe:  # noqa: ARG003
        return cls()

    def stream_upscale(
        self,
        input_path: str,
        output_path: str,
        window_size: int,
        tile: int | None,
    ) -> None:
        self.stream_calls.append(
            {
                "input_path": input_path,
                "output_path": output_path,
                "window_size": window_size,
                "tile": tile,
            }
        )
        Path(output_path).write_bytes(b"MP4-STUB")

    def to(self, device: str) -> _StubPipe:
        self._device = device
        return self


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a stub ``flashvsr.pipeline`` module.

    ``torch`` is also stubbed so the runtime's ``import torch`` succeeds
    in the kinoforge-default env (which does not ship torch).
    """
    fvsr_pipeline = types.ModuleType("flashvsr.pipeline")
    fvsr_pipeline.StreamingDMDPipeline = _StubPipe  # type: ignore[attr-defined]
    fvsr_pkg = types.ModuleType("flashvsr")
    fvsr_pkg.pipeline = fvsr_pipeline  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flashvsr", fvsr_pkg)
    monkeypatch.setitem(sys.modules, "flashvsr.pipeline", fvsr_pipeline)

    if "torch" not in sys.modules:
        torch_stub = types.ModuleType("torch")
        torch_stub.float16 = "fp16-sentinel"  # type: ignore[attr-defined]
        torch_stub.float32 = "fp32-sentinel"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "torch", torch_stub)


def test_construct_lazy_imports_flashvsr(stub_pipeline: None, tmp_path: Path) -> None:
    """RED: constructor pulls flashvsr only inside __init__, not at module load.

    Bug caught: top-level ``import flashvsr`` in _runtime.py breaks the
    kinoforge-default env (flashvsr lives only in the live-flashvsr feature env).
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(
        weights_dir=tmp_path,
        precision="fp16",
        window_size=24,
        tile_size=0,
        long_video_mode=False,
    )
    assert rt._native_scale == 2.0
    assert rt.vram_bytes == 8 * 1024**3


def test_upscale_produces_flashvsr_mp4_suffix(
    stub_pipeline: None, tmp_path: Path
) -> None:
    """RED: upscale returns Path with .flashvsr.mp4 suffix.

    Bug caught: sibling-suffix collision with .upscaled.mp4 (spandrel's
    output naming) → later stage overwrites the earlier stage's artifact.
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    rt = FlashVSRRuntime(tmp_path, "fp16", 24, 0, False)
    out = rt.upscale(src, ScaleTarget(kind="factor", value=2.0), {})
    assert out.name == "in.flashvsr.mp4"
    assert out.exists()


def test_upscale_height_target_raises(stub_pipeline: None, tmp_path: Path) -> None:
    """RED: kind=height is not yet supported at runtime either.

    Bug caught: cfg-time gate bypassed via direct runtime call →
    silently proceeds and produces a wrong-dimension output.
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "fp16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    with pytest.raises(NotYetImplementedError):
        rt.upscale(src, ScaleTarget(kind="height", value=1080), {})


def test_upscale_mismatched_scale_raises(stub_pipeline: None, tmp_path: Path) -> None:
    """RED: cfg scale must match checkpoint's native scale.

    Bug caught: --scale 4x against a 2x checkpoint silently runs and
    produces 2x output labeled as 4x.
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "fp16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    with pytest.raises(UnsupportedScaleError):
        rt.upscale(src, ScaleTarget(kind="factor", value=4.0), {})


def test_upscale_ignores_prompt_with_warning(
    stub_pipeline: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED: ``params['prompt']`` logs a warning; does NOT raise.

    Bug caught: raising on prompt breaks multi-stage cfgs that pass
    the Wan generation prompt through job.params for observability.
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "fp16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    with caplog.at_level(logging.WARNING):
        rt.upscale(
            src,
            ScaleTarget(kind="factor", value=2.0),
            {"prompt": "a field of wildflowers"},
        )
    assert any("prompt" in r.message and "ignored" in r.message for r in caplog.records)


def test_to_moves_underlying_pipe(stub_pipeline: None, tmp_path: Path) -> None:
    """RED: ``.to("cuda")`` delegates to the wrapped pipe.

    Bug caught: no-op ``.to()`` implementation — LRU thinks the model is
    on CUDA but inference runs on CPU (silent, catastrophic slowdown).
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "fp16", 24, 0, False)
    rt.to("cuda")
    assert rt._pipe._device == "cuda"  # noqa: SLF001
