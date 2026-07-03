"""Unit tests for the vendored FlashVSR input-prep helpers."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest


def _install_torch_stub(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    torch = types.ModuleType("torch")

    class _Tensor:
        def __init__(self, shape: tuple[int, ...]) -> None:
            self.shape = shape
            self.dtype: Any = None
            self.device: Any = None

        def to(self, *args: Any, **kwargs: Any) -> _Tensor:
            return self

        def unsqueeze(self, dim: int) -> _Tensor:
            new_shape = list(self.shape)
            new_shape.insert(dim, 1)
            return _Tensor(tuple(new_shape))

        def permute(self, *dims: int) -> _Tensor:
            return _Tensor(tuple(self.shape[i] for i in dims))

    torch.Tensor = _Tensor  # type: ignore[attr-defined]
    torch.bfloat16 = "bf16-sentinel"  # type: ignore[attr-defined]
    torch.float16 = "fp16-sentinel"  # type: ignore[attr-defined]
    torch.float32 = "fp32-sentinel"  # type: ignore[attr-defined]

    class _Nn:
        class Module:
            def __init__(self) -> None: ...
            def __call__(self, *a: Any, **kw: Any) -> Any:
                return self.forward(*a, **kw)  # type: ignore[attr-defined]

        class Conv3d(Module):
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__()
                self.weight = _Tensor((1,))
                self.bias = _Tensor((1,))

            def forward(self, x: Any) -> Any:  # noqa: ARG002
                return _Tensor((1, 4, 1, 1, 1))

    torch.nn = _Nn  # type: ignore[attr-defined]

    def zeros(
        shape: tuple[int, ...], dtype: Any = None, device: str = "cpu"
    ) -> _Tensor:  # noqa: ARG001
        return _Tensor(shape)

    torch.zeros = zeros  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    return torch


def _install_imageio_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    ii = types.ModuleType("imageio.v3")

    def imopen(path: str, mode: str, plugin: str = "pyav") -> Any:  # noqa: ARG001
        class _Reader:
            metadata = {"fps": 24.0}

            def iter(self) -> Any:  # noqa: A003
                for _ in range(16):
                    yield [[0] * 16] * 16

            def close(self) -> None: ...
            def __enter__(self) -> _Reader:
                return self

            def __exit__(self, *a: Any) -> None: ...

        return _Reader()

    ii.imopen = imopen  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "imageio", types.ModuleType("imageio"))
    monkeypatch.setitem(sys.modules, "imageio.v3", ii)


def test_prepare_input_tensor_returns_5tuple(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RED: prepare_input_tensor returns (LQ, th, tw, F, fps)."""
    _install_torch_stub(monkeypatch)
    _install_imageio_stub(monkeypatch)
    from kinoforge.upscalers.flashvsr._input_prep import prepare_input_tensor

    src = tmp_path / "in.mp4"
    src.write_bytes(b"MP4")
    lq, th, tw, f, fps = prepare_input_tensor(str(src), scale=4)
    assert (th, tw) == (64, 64)
    assert f == 16
    assert fps == 24.0
    assert lq.shape[0] == 1


def test_prepare_input_tensor_scale_multiplies_dims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RED: th/tw scale exactly 4× source dims."""
    _install_torch_stub(monkeypatch)
    _install_imageio_stub(monkeypatch)
    from kinoforge.upscalers.flashvsr._input_prep import prepare_input_tensor

    src = tmp_path / "in.mp4"
    src.write_bytes(b"MP4")
    _, th, tw, _, _ = prepare_input_tensor(str(src), scale=4)
    assert th == 16 * 4 and tw == 16 * 4


def test_causal_lq4x_proj_weight_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED: state_dict keys match upstream 'weight' + 'bias'."""
    _install_torch_stub(monkeypatch)
    from kinoforge.upscalers.flashvsr._input_prep import Causal_LQ4x_Proj

    proj = Causal_LQ4x_Proj(in_channels=3, out_channels=16)
    assert hasattr(proj, "weight")
    assert hasattr(proj, "bias")


def test_module_top_import_does_not_require_torch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: `import kinoforge.upscalers.flashvsr._input_prep` works without torch installed."""
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    import importlib

    mod = importlib.import_module("kinoforge.upscalers.flashvsr._input_prep")
    assert mod is not None
