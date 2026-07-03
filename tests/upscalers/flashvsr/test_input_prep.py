"""Unit tests for the vendored FlashVSR input-prep helpers.

Uses a rich ``torch`` stub (no real torch required) and stubs only
``imageio.v3`` so the module can be tested in the default pixi env.

The stub is precise enough to validate:
- That ``prepare_input_tensor`` actually reads frame content and builds a
  tensor from it (not all zeros).
- That the output shape contract ``(1, 3, F, H*scale, W*scale)`` is met.
- That ``Causal_LQ4x_Proj.forward`` delegates to the underlying ``nn.Conv3d``.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# _Tensor stub
# ---------------------------------------------------------------------------


class _Tensor:
    """Lightweight tensor stub carrying shape + a representative scalar value.

    Arithmetic operators propagate the transformed scalar so tests can verify
    that pixel content flows through normalisation (/ 255 * 2 - 1).
    """

    def __init__(self, shape: tuple[int, ...], sample: float = 0.0) -> None:
        self.shape = shape
        # _sample: a representative element value (used for max/min checks).
        self._sample = sample

    # -- coercion / reshape --

    def to(self, *args: Any, **kwargs: Any) -> _Tensor:
        return _Tensor(self.shape, self._sample)

    def unsqueeze(self, dim: int) -> _Tensor:
        new = list(self.shape)
        new.insert(dim, 1)
        return _Tensor(tuple(new), self._sample)

    def squeeze(self, dim: int) -> _Tensor:
        new = list(self.shape)
        del new[dim]
        return _Tensor(tuple(new), self._sample)

    def permute(self, *dims: int) -> _Tensor:
        new_shape = tuple(self.shape[i] for i in dims)
        return _Tensor(new_shape, self._sample)

    # -- arithmetic (propagate scalar transformation) --

    def __truediv__(self, other: float) -> _Tensor:
        return _Tensor(self.shape, self._sample / other)

    def __mul__(self, other: float) -> _Tensor:
        return _Tensor(self.shape, self._sample * other)

    def __sub__(self, other: float) -> _Tensor:
        return _Tensor(self.shape, self._sample - other)

    def __rtruediv__(self, other: float) -> _Tensor:
        return _Tensor(self.shape, other / self._sample)

    def __rmul__(self, other: float) -> _Tensor:
        return _Tensor(self.shape, other * self._sample)

    def __rsub__(self, other: float) -> _Tensor:
        return _Tensor(self.shape, other - self._sample)

    # -- reduction --

    def max(self) -> _ScalarResult:
        return _ScalarResult(self._sample)

    def min(self) -> _ScalarResult:
        return _ScalarResult(self._sample)

    def __repr__(self) -> str:
        return f"_Tensor(shape={self.shape}, sample={self._sample:.4f})"


class _ScalarResult:
    """Mimics the object returned by tensor.max() / tensor.min()."""

    def __init__(self, val: float) -> None:
        self._val = val

    def item(self) -> float:
        return self._val


# ---------------------------------------------------------------------------
# Stub installers
# ---------------------------------------------------------------------------


def _install_torch_stub(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install the torch stub into sys.modules.

    Args:
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The installed stub module.
    """
    torch = types.ModuleType("torch")
    torch.Tensor = _Tensor  # type: ignore[attr-defined]
    torch.bfloat16 = "bf16-sentinel"  # type: ignore[attr-defined]
    torch.float16 = "fp16-sentinel"  # type: ignore[attr-defined]
    torch.float32 = "fp32-sentinel"  # type: ignore[attr-defined]

    # from_numpy: derive shape + sample from first element of the array.
    def from_numpy(arr: Any) -> _Tensor:
        shape = arr.shape  # HWC
        try:
            sample = float(arr.flat[0])
        except Exception:
            sample = 0.0
        return _Tensor(shape, sample)

    torch.from_numpy = from_numpy  # type: ignore[attr-defined]

    # stack: concatenate along a new axis; propagate first tensor's sample.
    def stack(tensors: list[_Tensor], dim: int = 0) -> _Tensor:
        if not tensors:
            raise ValueError("stack expects a non-empty sequence")
        base = tensors[0].shape
        n = len(tensors)
        new_shape = list(base)
        new_shape.insert(dim, n)
        return _Tensor(tuple(new_shape), tensors[0]._sample)

    torch.stack = stack  # type: ignore[attr-defined]

    # zeros: explicit all-zero tensor (not used by the real impl, but kept as safety net).
    def zeros(
        shape: tuple[int, ...], dtype: Any = None, device: str = "cpu"
    ) -> _Tensor:
        return _Tensor(shape, 0.0)

    torch.zeros = zeros  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # nn functional stub — installed as a real module so that
    # ``import torch.nn.functional as F_nn`` resolves correctly.
    # ------------------------------------------------------------------
    nn_functional = types.ModuleType("torch.nn.functional")

    def interpolate(
        x: _Tensor,
        size: tuple[int, int] | None = None,
        mode: str = "bilinear",
        align_corners: bool | None = None,
    ) -> _Tensor:
        """Upscale spatial dims; preserve sample value."""
        b, c = x.shape[0], x.shape[1]
        th, tw = size if size is not None else (x.shape[-2], x.shape[-1])
        return _Tensor((b, c, th, tw), x._sample)

    nn_functional.interpolate = interpolate  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # nn stub with Conv3d
    # ------------------------------------------------------------------
    nn_mod = types.ModuleType("torch.nn")

    class Module:
        def __init__(self) -> None: ...

        def __call__(self, *a: Any, **kw: Any) -> Any:
            return self.forward(*a, **kw)  # type: ignore[attr-defined]

    class Conv3d(Module):
        """Stub Conv3d that returns a _Tensor with shape (B, out_ch, F, H, W)."""

        def __init__(self, in_ch: int, out_ch: int, *a: Any, **kw: Any) -> None:
            super().__init__()
            self._out_channels = out_ch
            self.weight = _Tensor((out_ch, in_ch, 3, 3, 3))
            self.bias = _Tensor((out_ch,))

        def forward(self, x: _Tensor) -> _Tensor:
            # (B, in_ch, F, H, W) → (B, out_ch, F, H, W)
            b, _c, f, h, w = x.shape
            return _Tensor((b, self._out_channels, f, h, w), x._sample)

    nn_mod.Module = Module  # type: ignore[attr-defined]
    nn_mod.Conv3d = Conv3d  # type: ignore[attr-defined]
    nn_mod.functional = nn_functional  # type: ignore[attr-defined]
    torch.nn = nn_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "torch.nn", nn_mod)
    monkeypatch.setitem(sys.modules, "torch.nn.functional", nn_functional)

    return torch


def _install_imageio_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    num_frames: int = 16,
    src_h: int = 16,
    src_w: int = 16,
    fps: float = 24.0,
    fill_value: int = 0,
) -> None:
    """Install a ``imageio.v3`` stub whose reader yields numpy-like frames.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        num_frames: Number of frames the stub reader yields.
        src_h: Frame height in pixels.
        src_w: Frame width in pixels.
        fps: FPS reported in metadata.
        fill_value: Constant uint8 value to fill every pixel (0–255).
    """
    try:
        import numpy as _np

        def _make_frame() -> Any:
            return _np.full((src_h, src_w, 3), fill_value, dtype=_np.uint8)

    except ModuleNotFoundError:

        class _FakeFrame:
            """Minimal frame object that mimics numpy array behaviour."""

            shape = (src_h, src_w, 3)
            ndim = 3

            class _Flat:
                def __init__(self, val: int) -> None:
                    self._v = val

                def __getitem__(self, _: int) -> int:
                    return self._v

            @property
            def flat(self) -> _FakeFrame._Flat:
                return _FakeFrame._Flat(fill_value)

        def _make_frame() -> Any:
            return _FakeFrame()

    ii = types.ModuleType("imageio.v3")

    def imopen(path: str, mode: str, plugin: str = "pyav") -> Any:  # noqa: ARG001
        class _Reader:
            metadata: dict[str, Any] = {"fps": fps}

            def iter(self) -> Any:  # noqa: A003
                for _ in range(num_frames):
                    yield _make_frame()

            def close(self) -> None: ...

            def __enter__(self) -> _Reader:
                return self

            def __exit__(self, *a: Any) -> None: ...

        return _Reader()

    ii.imopen = imopen  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "imageio", types.ModuleType("imageio"))
    monkeypatch.setitem(sys.modules, "imageio.v3", ii)


def _reset_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evict the cached _input_prep module so stub changes take effect."""
    monkeypatch.delitem(
        sys.modules, "kinoforge.upscalers.flashvsr._input_prep", raising=False
    )


# ---------------------------------------------------------------------------
# prepare_input_tensor tests
# ---------------------------------------------------------------------------


def test_prepare_input_tensor_returns_5tuple(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """prepare_input_tensor returns (LQ, th, tw, F, fps) with the full shape contract."""
    _install_torch_stub(monkeypatch)
    _install_imageio_stub(monkeypatch, num_frames=16, src_h=16, src_w=16, fps=24.0)
    _reset_module(monkeypatch)
    from kinoforge.upscalers.flashvsr._input_prep import prepare_input_tensor

    src = tmp_path / "in.mp4"
    src.write_bytes(b"MP4")
    lq, th, tw, f, fps = prepare_input_tensor(str(src), scale=4, device="cpu")

    # Full tuple contract: shape must be (1, 3, F, th, tw).
    assert lq.shape == (1, 3, 16, 64, 64), f"unexpected LQ shape {lq.shape}"
    assert th == 64
    assert tw == 64
    assert f == 16
    assert fps == 24.0


def test_prepare_input_tensor_scale_multiplies_dims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """th/tw are exactly scale × source spatial dims."""
    _install_torch_stub(monkeypatch)
    _install_imageio_stub(monkeypatch, num_frames=8, src_h=20, src_w=30, fps=30.0)
    _reset_module(monkeypatch)
    from kinoforge.upscalers.flashvsr._input_prep import prepare_input_tensor

    src = tmp_path / "in.mp4"
    src.write_bytes(b"MP4")
    _, th, tw, _, _ = prepare_input_tensor(str(src), scale=4, device="cpu")

    assert th == 20 * 4, f"expected th=80, got {th}"
    assert tw == 30 * 4, f"expected tw=120, got {tw}"


def test_prepare_input_tensor_lq_depends_on_source_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LQ tensor content reflects source-frame pixel values — not all zeros.

    A frame filled with 255 normalises to +1.0 in [-1, 1].
    A frame filled with 0 normalises to -1.0.
    The two cases must produce distinct max() values, proving the LQ tensor
    carries the source pixel information rather than being unconditionally zero.
    """
    src = tmp_path / "vid.mp4"
    src.write_bytes(b"MP4")

    # --- bright frames (fill=255) ---
    _install_torch_stub(monkeypatch)
    _install_imageio_stub(monkeypatch, num_frames=4, src_h=8, src_w=8, fill_value=255)
    _reset_module(monkeypatch)
    from kinoforge.upscalers.flashvsr._input_prep import prepare_input_tensor

    lq_bright, *_ = prepare_input_tensor(
        str(src), scale=2, dtype="fp32-sentinel", device="cpu"
    )
    # fill=255 → 255/255.0*2.0-1.0 = 1.0 → stub _sample propagates to 1.0
    bright_max = lq_bright.max().item()
    assert bright_max > 0.9, (
        f"Expected LQ max near 1.0 for all-255 source, got {bright_max}"
    )

    # --- dark frames (fill=0) ---
    _install_torch_stub(monkeypatch)
    _install_imageio_stub(monkeypatch, num_frames=4, src_h=8, src_w=8, fill_value=0)
    _reset_module(monkeypatch)
    from kinoforge.upscalers.flashvsr._input_prep import prepare_input_tensor as prep2

    lq_dark, *_ = prep2(str(src), scale=2, dtype="fp32-sentinel", device="cpu")
    # fill=0 → 0/255.0*2.0-1.0 = -1.0 → stub _sample propagates to -1.0
    dark_max = lq_dark.max().item()
    assert dark_max < bright_max, (
        f"Dark LQ (max={dark_max}) should be less than bright LQ (max={bright_max})"
    )


# ---------------------------------------------------------------------------
# Causal_LQ4x_Proj tests
# ---------------------------------------------------------------------------


def test_causal_lq4x_proj_weight_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Projection exposes .weight and .bias matching upstream checkpoint keys."""
    _install_torch_stub(monkeypatch)
    _reset_module(monkeypatch)
    from kinoforge.upscalers.flashvsr._input_prep import Causal_LQ4x_Proj

    proj = Causal_LQ4x_Proj(in_dim=3, out_dim=16)
    assert hasattr(proj, "weight"), "expected .weight attribute"
    assert hasattr(proj, "bias"), "expected .bias attribute"


def test_causal_lq4x_proj_forward_delegates_to_conv3d(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """forward(x) delegates to the underlying nn.Conv3d with correct output shape.

    Feeds a ``(1, 3, 4, 8, 8)`` stub tensor and verifies:
    - Output channel dim == out_channels (16).
    - Spatial dims (F, H, W) are preserved (Conv3d padding=(1,1,1), kernel=(3,3,3)).
    - ``__call__`` produces the same shape as ``forward``.
    """
    _install_torch_stub(monkeypatch)
    _reset_module(monkeypatch)
    from kinoforge.upscalers.flashvsr._input_prep import Causal_LQ4x_Proj

    in_dim = 3
    out_dim = 16
    proj = Causal_LQ4x_Proj(in_dim=in_dim, out_dim=out_dim)

    x = _Tensor((1, in_dim, 4, 8, 8), sample=0.5)

    y = proj.forward(x)
    assert y.shape == (1, out_dim, 4, 8, 8), (
        f"unexpected output shape {y.shape}; expected (1, {out_dim}, 4, 8, 8)"
    )

    # __call__ must also delegate — same shape contract.
    y2 = proj(x)
    assert y2.shape == y.shape, "__call__ and forward must return same-shape tensors"


# ---------------------------------------------------------------------------
# Lazy-import acceptance criterion
# ---------------------------------------------------------------------------


def test_module_top_import_does_not_require_torch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``import kinoforge.upscalers.flashvsr._input_prep`` succeeds without torch pre-imported.

    Verifies the lazy-import constraint: torch must NOT be imported at module
    parse time.  We remove torch from ``sys.modules`` before the import to
    simulate a host where torch has not yet been loaded.  The module should
    still parse cleanly because all torch usage is inside function bodies.
    """
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.delitem(sys.modules, "torch.nn", raising=False)
    monkeypatch.delitem(sys.modules, "torch.nn.functional", raising=False)
    _reset_module(monkeypatch)

    mod = importlib.import_module("kinoforge.upscalers.flashvsr._input_prep")
    assert mod is not None, "module should load even without torch pre-imported"
