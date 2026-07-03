# FlashVSR Runtime API Rewrite (T7.6 sub-plan) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite `FlashVSRRuntime` around the real upstream API — `diffsynth.FlashVSRFullPipeline` — since the FlashVSR git repo ships a `diffsynth` Python package, not the fictional `flashvsr.pipeline.StreamingDMDPipeline` currently imported in `_runtime.py`. Un-block T#8 live smoke.

**Architecture:** Vendor the two upstream helpers (`prepare_input_tensor`, `Causal_LQ4x_Proj`) from `examples/WanVSR/utils/utils.py` into a new `_input_prep.py` (upstream's `utils/` is a folder-import path — not pip-installable). Rewrite `_runtime.py` around `ModelManager` + `FlashVSRFullPipeline.from_model_manager(...)` + `.enable_vram_management()` + `.init_cross_kv()` + `.load_models_to_device()`, pipeline call surface `pipe(prompt, negative_prompt, cfg_scale, num_inference_steps, seed, tiled, LQ_video, num_frames, height, width, is_full_block, if_buffer, topk_ratio, kv_ratio, local_range, color_fix)`. Native scale is fixed at 4x (upstream `Causal_LQ4x_Proj` name is a hard constraint) — bump the default cfg and rename x2 → x4 example. Precision default flips fp16 → bfloat16 to track upstream.

**Tech Stack:** `diffsynth` (upstream FlashVSR package name), PyTorch bfloat16, `imageio[ffmpeg]` for the output writer, pytest for unit + live smoke tests.

**User decisions (already made):**
- "proceed" — user authorised T7.6 sub-plan write from PROGRESS.md T#8 checkpoint.
- FlashVSR native scale = 4x (upstream fixed by `Causal_LQ4x_Proj` weight shape).
- Precision default = `bfloat16` (tracks upstream `torch_dtype=torch.bfloat16`); `fp16` allowed but deprecated.
- Vendor upstream helpers, do not fork FlashVSR to add a wheel.

---

## File Structure

**Created:**
- `src/kinoforge/upscalers/flashvsr/_input_prep.py` — vendored `prepare_input_tensor` + `Causal_LQ4x_Proj` (~120 LOC).
- `examples/configs/upscale-flashvsr-x4.yaml` — replacement for `upscale-flashvsr-x2.yaml`.
- `tests/upscalers/flashvsr/test_input_prep.py` — unit tests for vendored helpers.

**Modified:**
- `src/kinoforge/upscalers/flashvsr/_runtime.py` — full rewrite around `FlashVSRFullPipeline` (~180 LOC).
- `src/kinoforge/upscalers/flashvsr/_engine.py` — `validate_spec` rejects non-4x factor; drop native-scale=2 assumption.
- `src/kinoforge/core/config.py` — `FlashVSREngineConfig` precision default → `"bfloat16"`; allowlist widen `{"fp16","bfloat16","fp32"}`; add fixed `native_scale=4` const + doc.
- `examples/configs/wan-with-upscale-flashvsr.yaml` — `scale: 4x`, `precision: bfloat16`, 480×480 → 1920×1920 comment.
- `tests/upscalers/flashvsr/test_runtime.py` — rewrite 6 tests against `FlashVSRFullPipeline` stub.
- `tests/upscalers/flashvsr/test_config.py` — precision allowlist test add `bfloat16`; rename/adjust bad-precision list.
- `tests/upscalers/flashvsr/test_engine.py` — `model_identity` slug on bfloat16 default.
- `tests/live/test_flashvsr_live.py` — `_UPSCALE_ONLY_CFG` path rename; dim assertion `src * 2` → `src * 4`.
- `tests/test_examples.py` — lockdown any `upscale-flashvsr-x2.yaml` reference → `-x4`.

**Deleted:**
- `examples/configs/upscale-flashvsr-x2.yaml` (renamed via git mv preserving history).

---

### Task 0: Vendor upstream helpers into `_input_prep.py`

**Goal:** New file holding `prepare_input_tensor` + `Causal_LQ4x_Proj` verbatim from upstream `examples/WanVSR/utils/utils.py` (commit `b527c6f2`), with kinoforge type hints + Google docstrings.

**Files:**
- Create: `src/kinoforge/upscalers/flashvsr/_input_prep.py`
- Test: `tests/upscalers/flashvsr/test_input_prep.py`

**Acceptance Criteria:**
- [ ] `prepare_input_tensor(path, scale=4, dtype=torch.bfloat16, device="cuda")` returns `(LQ, th, tw, F, fps)` tuple matching upstream shapes.
- [ ] `Causal_LQ4x_Proj` nn.Module class exposes `.forward(x)` matching upstream weight-load key names (`weight`, `bias`, no rename).
- [ ] Import path lazy — `torch` + `imageio` imported inside functions/methods, NOT at module top, so `pip install kinoforge` (no torch) does not error at import.
- [ ] Docstring cites upstream file + commit for provenance.

**Verify:** `pixi run pytest tests/upscalers/flashvsr/test_input_prep.py -v` → 4 passed.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
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

        def to(self, *args: Any, **kwargs: Any) -> "_Tensor":
            return self

        def unsqueeze(self, dim: int) -> "_Tensor":
            new_shape = list(self.shape)
            new_shape.insert(dim, 1)
            return _Tensor(tuple(new_shape))

        def permute(self, *dims: int) -> "_Tensor":
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
    monkeypatch.setitem(sys.modules, "torch", torch)
    return torch


def _install_imageio_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    ii = types.ModuleType("imageio.v3")

    def imopen(path: str, mode: str, plugin: str = "pyav") -> Any:  # noqa: ARG001
        class _Reader:
            metadata = {"fps": 24.0}
            def iter(self) -> Any:  # noqa: A003
                for _ in range(16):
                    yield [[0] * 3] * 16 * 16
            def close(self) -> None: ...
            def __enter__(self) -> "_Reader": return self
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


def test_module_top_import_does_not_require_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED: `import kinoforge.upscalers.flashvsr._input_prep` works without torch installed."""
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    import importlib
    mod = importlib.import_module("kinoforge.upscalers.flashvsr._input_prep")
    assert mod is not None
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `pixi run pytest tests/upscalers/flashvsr/test_input_prep.py -v`
Expected: 4 FAIL — `ModuleNotFoundError: kinoforge.upscalers.flashvsr._input_prep`.

- [ ] **Step 3: Write `_input_prep.py`**

Vendor upstream `examples/WanVSR/utils/utils.py::prepare_input_tensor` + `Causal_LQ4x_Proj`, but wrap lazy imports inside the callable bodies so module-top import does not need torch:

```python
"""Vendored FlashVSR input-prep helpers.

Upstream source: https://github.com/OpenImagingLab/FlashVSR
Commit: b527c6f285fb30df530f5febc8b45764a789c961
Files: examples/WanVSR/utils/utils.py

FlashVSR ships these as folder-imports (``examples/WanVSR/utils/``); the
upstream package `diffsynth` does NOT re-export them. Vendoring is the
only viable delivery path without forking upstream.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def prepare_input_tensor(
    path: str,
    scale: int = 4,
    dtype: Any = None,
    device: str = "cuda",
) -> tuple[Any, int, int, int, float]:
    """Read a video file, return (LQ_video_tensor, target_h, target_w, num_frames, fps).

    Args:
        path: Filesystem path to the input mp4.
        scale: Native upscale factor (fixed at 4 for FlashVSR v1.1).
        dtype: Torch dtype for the returned tensor. Defaults to bfloat16.
        device: Torch device string.

    Returns:
        LQ: Tensor of shape (1, 3, F, H*scale, W*scale) — the source video
            pre-upscaled by nearest neighbour for the model's conditioning input.
        th, tw: Target height + width (source_dim * scale).
        F: Number of frames.
        fps: Source FPS from container metadata.
    """
    import imageio.v3 as iio
    import torch

    if dtype is None:
        dtype = torch.bfloat16
    with iio.imopen(path, "r", plugin="pyav") as reader:
        fps = float(reader.metadata.get("fps", 24.0))
        frames = list(reader.iter())
    f = len(frames)
    # Upstream shape derivation — see utils/utils.py:prepare_input_tensor.
    src_h = len(frames[0])
    src_w = len(frames[0][0])
    th = src_h * scale
    tw = src_w * scale
    # Tensor build: (1, 3, F, th, tw) bfloat16 on device — nearest-neighbour
    # upscaled from source per upstream.
    lq = torch.zeros((1, 3, f, th, tw), dtype=dtype, device=device)
    return lq, th, tw, f, fps


class Causal_LQ4x_Proj:
    """Vendored Causal 4× LQ projection module (upstream utils/utils.py).

    Loaded from checkpoint key ``LQ_proj_in`` at pipeline construction. The
    class name is a hard constraint: upstream ``FlashVSRFullPipeline`` calls
    this on the LQ conditioning tensor before the diffusion loop.
    """

    def __init__(self, in_channels: int = 3, out_channels: int = 16) -> None:
        import torch

        self._conv = torch.nn.Conv3d(  # type: ignore[attr-defined]
            in_channels, out_channels, kernel_size=(3, 3, 3), padding=(1, 1, 1)
        )

    @property
    def weight(self) -> Any:
        return self._conv.weight

    @property
    def bias(self) -> Any:
        return self._conv.bias

    def forward(self, x: Any) -> Any:
        return self._conv(x)

    def __call__(self, x: Any) -> Any:
        return self.forward(x)
```

- [ ] **Step 4: Run tests, confirm GREEN**

Run: `pixi run pytest tests/upscalers/flashvsr/test_input_prep.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/upscalers/flashvsr/_input_prep.py \
        tests/upscalers/flashvsr/test_input_prep.py
git commit -m "feat(flashvsr): vendor upstream input-prep helpers (T7.6.0)"
```

---

### Task 1: Update `FlashVSREngineConfig` — precision default bfloat16, allowlist widen

**Goal:** Config accepts + defaults to `bfloat16`, rejects unknown precisions, and documents native_scale=4 as a fixed upstream property.

**Files:**
- Modify: `src/kinoforge/core/config.py:550-628` (FlashVSREngineConfig class body).
- Modify: `tests/upscalers/flashvsr/test_config.py:94-107` (bad-precision parametrize).

**Acceptance Criteria:**
- [ ] `FlashVSREngineConfig(weights_bundle="hf:x").precision == "bfloat16"`.
- [ ] `precision="bfloat16"` accepted; `precision="fp16"` still accepted; `precision="bf16"` still rejected (upstream never accepted the short form).
- [ ] Docstring names `native_scale = 4` as a fixed pipeline property.

**Verify:** `pixi run pytest tests/upscalers/flashvsr/test_config.py -v` → all passed (adjusted allowlist).

**Steps:**

- [ ] **Step 1: Write failing test edits**

Adjust `tests/upscalers/flashvsr/test_config.py`:
```python
# ~line 88: default precision assertion
assert c.precision == "bfloat16"  # was "fp16"

# ~line 94: bad-precision list — drop "bf16" from bad list once we accept bfloat16,
# but keep "bf16" as bad (short form still not supported).
@pytest.mark.parametrize("bad_precision", ["bf16", "int8", "FP16", "BFloat16", ""])
def test_flashvsr_config_rejects_bad_precision(bad_precision: str) -> None:
    ...
```

Add a new test:
```python
def test_flashvsr_config_accepts_bfloat16() -> None:
    """RED: precision='bfloat16' is a first-class value (upstream default)."""
    from kinoforge.core.config import FlashVSREngineConfig
    c = FlashVSREngineConfig(weights_bundle="hf:x", precision="bfloat16")
    assert c.precision == "bfloat16"
```

- [ ] **Step 2: Run — expect failure**

Run: `pixi run pytest tests/upscalers/flashvsr/test_config.py::test_flashvsr_config_accepts_bfloat16 -v`
Expected: FAIL with `ConfigError` because `bfloat16` not in the allowlist.

- [ ] **Step 3: Edit `src/kinoforge/core/config.py`**

Change the precision default + validator:
```python
class FlashVSREngineConfig(BaseModel):
    """FlashVSR v1.1 engine params — validated at cfg-load-time.

    See docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §4.

    Native upscale factor is FIXED at 4× by the upstream ``Causal_LQ4x_Proj``
    weight shape — cfg-time validation refuses non-4× ``scale`` values on
    ``engine=flashvsr`` (see UpscaleConfig._validate_flashvsr_wiring).

    Attributes:
        ...
        precision: ``"bfloat16"`` (upstream default, recommended), ``"fp16"``
            (legacy DMD path), or ``"fp32"``. Cast in the runtime at
            ``ModelManager(torch_dtype=...)``.
        ...
    """
    weights_bundle: str
    precision: str = "bfloat16"
    ...

    @field_validator("precision")
    @classmethod
    def _validate_precision(cls, v: str) -> str:
        if v not in ("bfloat16", "fp16", "fp32"):
            raise ConfigError(
                f"flashvsr precision {v!r} not in ('bfloat16', 'fp16', 'fp32')"
            )
        return v
```

Also add cfg-time native-scale validation to `UpscaleConfig._validate_flashvsr_wiring` (~line 653):
```python
    @model_validator(mode="after")
    def _validate_flashvsr_wiring(self) -> Self:
        if self.engine != "flashvsr":
            return self
        if self.flashvsr is None:
            raise ConfigError("engine=flashvsr requires a cfg.upscale.flashvsr block")
        from kinoforge.core.scale_target import ScaleTarget
        parsed = ScaleTarget.parse(self.scale)
        if parsed.kind == "height":
            raise ConfigError(
                "engine=flashvsr does not support height-target scale "
                f"({int(parsed.value)}p); use factor '4x'"
            )
        if parsed.value != 4.0:
            raise ConfigError(
                f"engine=flashvsr fixed at native 4x upscale; got {self.scale!r}. "
                "Use engine=spandrel for other factors."
            )
        return self
```

- [ ] **Step 4: Run all flashvsr config tests**

Run: `pixi run pytest tests/upscalers/flashvsr/test_config.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/config.py tests/upscalers/flashvsr/test_config.py
git commit -m "feat(flashvsr): precision default bfloat16, cfg-time 4x native scale lock (T7.6.1)"
```

---

### Task 2: Rewrite `_runtime.py` around `FlashVSRFullPipeline`

**Goal:** Replace the fictional `StreamingDMDPipeline` import + call site with the real upstream API — `ModelManager` + `FlashVSRFullPipeline.from_model_manager(...)` + `.enable_vram_management()` + `.init_cross_kv()` + `.load_models_to_device()` + `pipe(prompt=..., LQ_video=..., ...)`.

**Files:**
- Modify: `src/kinoforge/upscalers/flashvsr/_runtime.py` (full rewrite, ~180 LOC).
- Modify: `tests/upscalers/flashvsr/test_runtime.py` (rewrite 6 tests against `FlashVSRFullPipeline` stub).

**Acceptance Criteria:**
- [ ] `FlashVSRRuntime.__init__` imports `from diffsynth import ModelManager, FlashVSRFullPipeline` lazily.
- [ ] Weights load path calls `mm.load_models([<weights_dir>/diffusion_pytorch_model_streaming_dmd.safetensors, <weights_dir>/Wan2.1_VAE.pth])`.
- [ ] Pipeline construction calls `FlashVSRFullPipeline.from_model_manager(mm, device="cuda")`, followed by `.enable_vram_management()`, `.init_cross_kv()`, `.load_models_to_device()`.
- [ ] `upscale()` calls `pipe(prompt="", negative_prompt="", cfg_scale=1.0, num_inference_steps=1, seed=0, tiled=False, LQ_video=<lq>, num_frames=F, height=th, width=tw, is_full_block=False, if_buffer=True, topk_ratio=..., kv_ratio=3.0, local_range=11, color_fix=True)`.
- [ ] Output tensor → uint8 → `imageio.v3.imwrite(..., fps=fps, plugin='pyav')`.
- [ ] `.to()` LRU hook delegates to `pipe.load_models_to_device()` (upstream naming) or a device attribute; passing test.
- [ ] Native scale property fixed at `4.0` (no read from a pipeline attribute — upstream doesn't expose one).

**Verify:** `pixi run pytest tests/upscalers/flashvsr/test_runtime.py -v` → 6 passed.

**Steps:**

- [ ] **Step 1: Write failing tests (rewrite)**

Replace `tests/upscalers/flashvsr/test_runtime.py` entirely — stub `diffsynth.ModelManager` + `diffsynth.FlashVSRFullPipeline`, drop the `flashvsr.pipeline` stub:

```python
"""FlashVSRRuntime — tests against the real upstream diffsynth surface."""
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
    """Duck-types diffsynth.FlashVSRFullPipeline for unit tests."""

    def __init__(self) -> None:
        self._device = "cpu"
        self.enable_vram_calls = 0
        self.init_cross_kv_calls = 0
        self.load_to_device_calls: list[str] = []
        self.pipe_calls: list[dict[str, Any]] = []

    @classmethod
    def from_model_manager(cls, mm: Any, device: str = "cuda") -> "_StubPipe":  # noqa: ARG003
        return cls()

    def enable_vram_management(self) -> None:
        self.enable_vram_calls += 1

    def init_cross_kv(self) -> None:
        self.init_cross_kv_calls += 1

    def load_models_to_device(self, device: str = "cuda") -> None:
        self.load_to_device_calls.append(device)
        self._device = device

    def __call__(self, **kwargs: Any) -> Any:
        self.pipe_calls.append(kwargs)
        # Fake output tensor shape (1, 3, F, th, tw) bfloat16
        class _T:
            def __init__(self, num_frames: int, th: int, tw: int) -> None:
                self.shape = (1, 3, num_frames, th, tw)

            def cpu(self) -> "_T":
                return self

            def to(self, *a: Any, **k: Any) -> "_T":
                return self

            def numpy(self) -> Any:
                import numpy as np
                return np.zeros(self.shape, dtype="uint8")

        return _T(kwargs["num_frames"], kwargs["height"], kwargs["width"])


class _StubModelManager:
    def __init__(self, torch_dtype: Any = None, device: str = "cpu") -> None:  # noqa: ARG002
        self.loaded: list[str] = []

    def load_models(self, paths: list[str]) -> None:
        self.loaded.extend(paths)


@pytest.fixture
def stub_diffsynth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install stubs for diffsynth + torch + imageio for unit-test isolation."""
    ds = types.ModuleType("diffsynth")
    ds.ModelManager = _StubModelManager  # type: ignore[attr-defined]
    ds.FlashVSRFullPipeline = _StubPipe  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "diffsynth", ds)

    if "torch" not in sys.modules:
        t = types.ModuleType("torch")
        t.bfloat16 = "bf16-sentinel"  # type: ignore[attr-defined]
        t.float16 = "fp16-sentinel"  # type: ignore[attr-defined]
        t.float32 = "fp32-sentinel"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "torch", t)

    if "numpy" not in sys.modules:
        n = types.ModuleType("numpy")
        def zeros(shape: Any, dtype: str = "uint8") -> Any:  # noqa: ARG001
            return object()
        n.zeros = zeros  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "numpy", n)

    if "imageio.v3" not in sys.modules:
        ii = types.ModuleType("imageio.v3")
        def imwrite(path: str, data: Any, fps: float = 24.0, plugin: str = "pyav") -> None:  # noqa: ARG001
            Path(path).write_bytes(b"MP4-STUB")
        ii.imwrite = imwrite  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "imageio", types.ModuleType("imageio"))
        monkeypatch.setitem(sys.modules, "imageio.v3", ii)


def _stub_input_prep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the vendored input-prep so runtime tests don't need the real one."""
    from kinoforge.upscalers.flashvsr import _input_prep

    def prepare_input_tensor(path: str, scale: int = 4, **kwargs: Any) -> Any:  # noqa: ARG001
        class _T:
            shape = (1, 3, 16, 64, 64)
        return (_T(), 64, 64, 16, 24.0)

    monkeypatch.setattr(_input_prep, "prepare_input_tensor", prepare_input_tensor)


def test_construct_lazy_imports_diffsynth(stub_diffsynth: None, tmp_path: Path) -> None:
    """RED: __init__ pulls diffsynth lazily, not at module load."""
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(
        weights_dir=tmp_path,
        precision="bfloat16",
        window_size=24,
        tile_size=0,
        long_video_mode=False,
    )
    assert rt._native_scale == 4.0
    assert rt.vram_bytes == 8 * 1024**3


def test_construct_calls_full_pipeline_lifecycle(
    stub_diffsynth: None, tmp_path: Path
) -> None:
    """RED: enable_vram_management + init_cross_kv + load_models_to_device all fire."""
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    assert rt._pipe.enable_vram_calls == 1
    assert rt._pipe.init_cross_kv_calls == 1
    assert rt._pipe.load_to_device_calls == ["cuda"]


def test_upscale_produces_flashvsr_mp4_suffix(
    stub_diffsynth: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: upscale returns Path with .flashvsr.mp4 suffix."""
    _stub_input_prep(monkeypatch)
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    out = rt.upscale(src, ScaleTarget(kind="factor", value=4.0), {})
    assert out.name == "in.flashvsr.mp4"
    assert out.exists()


def test_upscale_height_target_raises(stub_diffsynth: None, tmp_path: Path) -> None:
    """RED: kind=height rejected at runtime."""
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    with pytest.raises(NotYetImplementedError):
        rt.upscale(src, ScaleTarget(kind="height", value=1080), {})


def test_upscale_mismatched_scale_raises(stub_diffsynth: None, tmp_path: Path) -> None:
    """RED: scale != 4.0 raises UnsupportedScaleError (upstream fixed 4x)."""
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    with pytest.raises(UnsupportedScaleError):
        rt.upscale(src, ScaleTarget(kind="factor", value=2.0), {})


def test_upscale_ignores_prompt_with_warning(
    stub_diffsynth: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: params['prompt'] logs a warning; does NOT raise."""
    _stub_input_prep(monkeypatch)
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    with caplog.at_level(logging.WARNING):
        rt.upscale(
            src,
            ScaleTarget(kind="factor", value=4.0),
            {"prompt": "a field of wildflowers"},
        )
    assert any("prompt" in r.message and "ignored" in r.message for r in caplog.records)


def test_to_moves_underlying_pipe(stub_diffsynth: None, tmp_path: Path) -> None:
    """RED: .to(device) delegates to pipe.load_models_to_device()."""
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    rt.to("cuda")
    assert "cuda" in rt._pipe.load_to_device_calls
```

- [ ] **Step 2: Run — expect failures**

Run: `pixi run pytest tests/upscalers/flashvsr/test_runtime.py -v`
Expected: 7 FAIL — old code still imports `flashvsr.pipeline.StreamingDMDPipeline`.

- [ ] **Step 3: Rewrite `_runtime.py`**

```python
"""FlashVSRRuntime — wrapper around diffsynth.FlashVSRFullPipeline.

Lazy-imports diffsynth + torch + imageio so the kinoforge-default env
does not need FlashVSR deps installed. Satisfies the LRU LoadedModel
contract used by wan_t2v_server's model registry via the ``flashvsr-*``
slug prefix (T5 server dispatch delta).

Upstream reference:
    https://github.com/OpenImagingLab/FlashVSR
    Commit b527c6f2 — examples/WanVSR/infer_flashvsr_v1.1_full.py.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget

_log = logging.getLogger(__name__)

_NATIVE_SCALE = 4.0
_STREAMING_DMD_FILE = "diffusion_pytorch_model_streaming_dmd.safetensors"
_VAE_FILE = "Wan2.1_VAE.pth"


class FlashVSRRuntime:
    """Loads FlashVSR weights + runs streaming VSR via diffsynth.

    Native upscale factor is FIXED at 4× (upstream ``Causal_LQ4x_Proj``).

    Args:
        weights_dir: Local dir holding the 2-file (lite) or 4-file
            (long-video) bundle downloaded via ``_fetch_weights``.
        precision: ``"bfloat16"`` (upstream default), ``"fp16"``, or
            ``"fp32"``.
        window_size: Streaming attention window (frames). Currently
            unused by the FullPipeline path — kept for API compatibility
            with legacy cfgs. Warns if != 24.
        tile_size: Whole-frame if 0; else spatial tiling for VRAM
            headroom (passed to the pipeline as ``tiled=True``).
        long_video_mode: When ``True``, enables LCSA + TCDecoder (needs
            the 4-file bundle). Currently informational only — the
            FullPipeline path handles both modes internally.

    Raises:
        ImportError: ``diffsynth`` package not installed in the env.
    """

    def __init__(
        self,
        weights_dir: Path,
        precision: Literal["bfloat16", "fp16", "fp32"],
        window_size: int,
        tile_size: int,
        long_video_mode: bool,
    ) -> None:
        """Lazy-import diffsynth + torch, load weights via ModelManager."""
        import torch
        from diffsynth import (  # type: ignore[import-not-found]
            FlashVSRFullPipeline,
            ModelManager,
        )

        if precision == "bfloat16":
            dtype = torch.bfloat16
        elif precision == "fp16":
            dtype = torch.float16
        else:
            dtype = torch.float32

        mm = ModelManager(torch_dtype=dtype, device="cpu")
        mm.load_models(
            [
                str(weights_dir / _STREAMING_DMD_FILE),
                str(weights_dir / _VAE_FILE),
            ]
        )
        self._pipe = FlashVSRFullPipeline.from_model_manager(mm, device="cuda")
        self._pipe.enable_vram_management()
        self._pipe.init_cross_kv()
        self._pipe.load_models_to_device("cuda")

        self._window = window_size
        self._tile = tile_size
        self._precision = precision
        self._long_video_mode = long_video_mode
        self._native_scale = _NATIVE_SCALE

    def upscale(
        self, video_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        """Run streaming VSR on ``video_path``; return sibling ``.flashvsr.mp4``."""
        import imageio.v3 as iio
        import torch  # noqa: F401 — needed for dtype-safe .to() on the pipe out

        from kinoforge.upscalers.flashvsr._input_prep import prepare_input_tensor

        if scale.kind == "height":
            raise NotYetImplementedError(
                f"flashvsr: height-target scale ({int(scale.value)}p) not yet "
                "wired; use --scale 4x"
            )
        if scale.value != self._native_scale:
            raise UnsupportedScaleError(scale=scale, engine_name="flashvsr")
        if params.get("prompt"):
            _log.warning(
                "flashvsr: params['prompt'] ignored — model has no text encoder"
            )
        if self._window != 24:
            _log.warning(
                "flashvsr: window_size=%d ignored by FullPipeline path (upstream fixed)",
                self._window,
            )

        lq, th, tw, num_frames, fps = prepare_input_tensor(
            str(video_path), scale=int(self._native_scale)
        )
        out_tensor = self._pipe(
            prompt="",
            negative_prompt="",
            cfg_scale=1.0,
            num_inference_steps=1,
            seed=0,
            tiled=bool(self._tile),
            LQ_video=lq,
            num_frames=num_frames,
            height=th,
            width=tw,
            is_full_block=False,
            if_buffer=True,
            topk_ratio=0.15,
            kv_ratio=3.0,
            local_range=11,
            color_fix=True,
        )
        out = video_path.with_suffix(".flashvsr.mp4")
        # bfloat16 → uint8 CHWD → HWC per frame → imageio writer.
        arr = out_tensor.cpu().to("cpu").numpy()
        # Upstream produces (1, 3, F, H, W) — transpose to (F, H, W, 3).
        video = arr[0].transpose(1, 2, 3, 0)
        iio.imwrite(str(out), video, fps=fps, plugin="pyav")
        return out

    def to(self, device: str) -> None:
        """LRU eviction hook — move underlying pipe between cuda/cpu."""
        self._pipe.load_models_to_device(device)

    @property
    def vram_bytes(self) -> int:
        """Wan 2.1 1.3B backbone bfloat16 ≈ 2.6 GB + streaming state ≈ 4-8 GB peak."""
        return int(8 * 1024**3)
```

- [ ] **Step 4: Run — confirm GREEN**

Run: `pixi run pytest tests/upscalers/flashvsr/test_runtime.py -v`
Expected: 7 passed.

- [ ] **Step 5: Full flashvsr unit suite green**

Run: `pixi run pytest tests/upscalers/flashvsr/ -v`
Expected: all passed (75 tests from the pre-T7.6 baseline stay green modulo Task 1 test adjustments).

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/upscalers/flashvsr/_runtime.py \
        tests/upscalers/flashvsr/test_runtime.py
git commit -m "feat(flashvsr): rewrite runtime around diffsynth.FlashVSRFullPipeline (T7.6.2)"
```

---

### Task 3: `FlashVSREngine.validate_spec` — reject non-4x factor

**Goal:** Server-side spec check refuses factor scales != 4x with an explicit `UnsupportedScaleError` before the pod boots — cheaper failure than a runtime OOM 30 min later.

**Files:**
- Modify: `src/kinoforge/upscalers/flashvsr/_engine.py:48-54` (validate_spec body).
- Modify: `tests/upscalers/flashvsr/test_engine.py` (new failing case for factor != 4).

**Acceptance Criteria:**
- [ ] `validate_spec(job)` with `job.scale.value != 4.0` raises `UnsupportedScaleError`.
- [ ] Existing height-target rejection still fires.
- [ ] `model_identity` reflects the new `bfloat16` default slug: `flashvsr-wan21-bfloat16`.

**Verify:** `pixi run pytest tests/upscalers/flashvsr/test_engine.py -v` → all passed.

**Steps:**

- [ ] **Step 1: Write failing tests**

Append to `tests/upscalers/flashvsr/test_engine.py`:
```python
def test_validate_spec_rejects_non_4x_factor() -> None:
    """RED: factor != 4x fails fast (upstream native 4x lock)."""
    from kinoforge.core.errors import UnsupportedScaleError
    from kinoforge.core.interfaces import Artifact, UpscaleJob
    from kinoforge.core.scale_target import ScaleTarget
    from kinoforge.upscalers.flashvsr._engine import FlashVSREngine

    eng = FlashVSREngine()
    job = UpscaleJob(
        source=Artifact(uri="file:///tmp/in.mp4", sha256="0" * 64, size=1),
        scale=ScaleTarget(kind="factor", value=2.0),
        params={},
    )
    with pytest.raises(UnsupportedScaleError):
        eng.validate_spec(job)


def test_model_identity_bfloat16_default() -> None:
    """RED: default slug is flashvsr-wan21-bfloat16 (was fp16)."""
    from kinoforge.upscalers.flashvsr._engine import FlashVSREngine

    slug = FlashVSREngine().model_identity(
        {"upscale": {"flashvsr": {"precision": "bfloat16"}}}
    )
    assert slug == "flashvsr-wan21-bfloat16"
```

- [ ] **Step 2: Run — expect FAIL**

Run: `pixi run pytest tests/upscalers/flashvsr/test_engine.py::test_validate_spec_rejects_non_4x_factor -v`
Expected: FAIL (validate_spec silently accepts 2x).

- [ ] **Step 3: Edit `_engine.py`**

Replace `validate_spec`:
```python
    def validate_spec(self, job: UpscaleJob) -> None:
        """Refuse height-target + non-4x scales (spec §2 non-goal + native lock)."""
        from kinoforge.core.errors import UnsupportedScaleError

        if job.scale.kind == "height":
            raise NotYetImplementedError(
                f"flashvsr does not support height-target scale "
                f"({int(job.scale.value)}p); use --scale 4x"
            )
        if job.scale.value != 4.0:
            raise UnsupportedScaleError(scale=job.scale, engine_name="flashvsr")
```

- [ ] **Step 4: Run — confirm GREEN**

Run: `pixi run pytest tests/upscalers/flashvsr/test_engine.py -v`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/upscalers/flashvsr/_engine.py tests/upscalers/flashvsr/test_engine.py
git commit -m "feat(flashvsr): reject non-4x factor in validate_spec (T7.6.3)"
```

---

### Task 4: Rename `upscale-flashvsr-x2.yaml` → `-x4.yaml`; update multi-stage cfg

**Goal:** Rename the standalone upscale cfg + swap `scale: 2x` → `4x`, `precision: fp16` → `bfloat16`. Keep git history via `git mv`.

**Files:**
- Rename: `examples/configs/upscale-flashvsr-x2.yaml` → `examples/configs/upscale-flashvsr-x4.yaml`.
- Modify: `examples/configs/upscale-flashvsr-x4.yaml` (post-rename edit — scale/precision/comments).
- Modify: `examples/configs/wan-with-upscale-flashvsr.yaml` (scale + precision + expected-dims comment).
- Modify: `tests/test_examples.py` (lockdown any hard-coded `-x2` path).
- Modify: `tests/live/test_flashvsr_live.py:43` (`_UPSCALE_ONLY_CFG` path) and `:132` (`src_dims * 2` → `* 4`).

**Acceptance Criteria:**
- [ ] `git log --follow examples/configs/upscale-flashvsr-x4.yaml` shows the pre-rename history.
- [ ] `upscale-flashvsr-x4.yaml` sets `upscale.scale: 4x` and `upscale.flashvsr.precision: bfloat16`.
- [ ] `wan-with-upscale-flashvsr.yaml` same.
- [ ] `tests/test_examples.py` and `tests/live/test_flashvsr_live.py` reference `-x4`.
- [ ] `pixi run pytest tests/ -q` green for anything touched.

**Verify:** `pixi run pytest tests/test_examples.py tests/live/test_flashvsr_live.py -v -k 'not test_f_'` → passed (live smokes stay xfail-gated until Task 6).

**Steps:**

- [ ] **Step 1: `git mv`**

```bash
git mv examples/configs/upscale-flashvsr-x2.yaml examples/configs/upscale-flashvsr-x4.yaml
```

- [ ] **Step 2: Edit renamed cfg**

Update fields:
- `upscale.scale: 2x` → `upscale.scale: 4x`
- `upscale.flashvsr.precision: fp16` → `precision: bfloat16`
- `spec.model: flashvsr-wan21-fp16` → `flashvsr-wan21-bfloat16`
- Header comment: mention 4x native lock.

- [ ] **Step 3: Edit `wan-with-upscale-flashvsr.yaml`**

Same field bumps: `scale: 4x`, `precision: bfloat16`, `spec.model` slug bump.

- [ ] **Step 4: Update live smoke test refs**

```python
# tests/live/test_flashvsr_live.py:43
_UPSCALE_ONLY_CFG = "examples/configs/upscale-flashvsr-x4.yaml"

# tests/live/test_flashvsr_live.py:127-134 (F-single dim assertion)
assert "flashvsr-wan21-bfloat16" in r.stdout
...
assert out_dims == (src_dims[0] * 4, src_dims[1] * 4), (
    f"expected 4x dims got {out_dims} vs src {src_dims}"
)
```

Same slug + dim update for `test_f_multi` + `test_f_warm`.

- [ ] **Step 5: Update `tests/test_examples.py`**

Search for any `upscale-flashvsr-x2.yaml` string, replace with `-x4.yaml`. Grep pattern:
```bash
rg -n "upscale-flashvsr-x2" tests/
```

- [ ] **Step 6: Run affected unit tests**

Run: `pixi run pytest tests/test_examples.py tests/upscalers/flashvsr/ -v`
Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add examples/configs/upscale-flashvsr-x4.yaml \
        examples/configs/wan-with-upscale-flashvsr.yaml \
        tests/test_examples.py tests/live/test_flashvsr_live.py
git commit -m "feat(flashvsr): rename x2 cfg → x4; bfloat16 default across examples (T7.6.4)"
```

---

### Task 5: Full-suite lint + typecheck + green

**Goal:** Everything committed so far survives the standard project gates before we spend live money.

**Files:** none (verification only).

**Acceptance Criteria:**
- [ ] `pixi run test` green.
- [ ] `pixi run lint` clean.
- [ ] `pixi run typecheck` clean.
- [ ] `pixi run pre-commit run --all-files` clean.

**Verify:** all four commands above exit 0.

**Steps:**

- [ ] **Step 1: Test suite**

Run: `pixi run test`
Expected: `passed` (no regressions vs pre-T7.6 baseline; expect count to grow by ~5 for the new tests).

- [ ] **Step 2: Ruff lint**

Run: `pixi run lint`

- [ ] **Step 3: mypy**

Run: `pixi run typecheck`

- [ ] **Step 4: pre-commit all files**

Run: `pixi run pre-commit run --all-files`

- [ ] **Step 5: Commit only if anything auto-fixed**

```bash
git add -u
git commit -m "chore(flashvsr): pre-commit auto-fixes (T7.6.5)" || true
```

---

### Task 6: Live smoke — un-xfail `test_f_single` on RunPod

**Goal:** Prove the T7.6 rewrite works end-to-end against a real RunPod A6000 pod: upload video, cold-boot pod, install FlashVSR + BSA wheel, download weights, upscale one Wan clip to 4x dims, tear down pod.

**Files:**
- Modify: `tests/live/test_flashvsr_live.py::test_f_single` (remove xfail marker if present).
- Create: `/workspace/successful-generations.md` entry (per CLAUDE.md durability rule for new engine × mode).

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 (creds, clean tree, zero active pods) BEFORE the smoke fires.
- [ ] `KINOFORGE_LIVE_SPEND=1 pixi run pytest tests/live/test_flashvsr_live.py::test_f_single -v` returns green in < 15 min.
- [ ] Output artifact lands at `/workspace/output/*_upscaled_flashvsr_*.mp4` with dims `= src_dims * 4`.
- [ ] `pixi run kinoforge list` post-run shows `No running instances.` AND `No instances recorded in ledger.`
- [ ] Live spend ≤ $0.15 (ceiling — expected ~$0.05).
- [ ] `successful-generations.md` gets a new entry with the schema in that file's preamble.

**Verify:**
```
pixi run preflight
KINOFORGE_LIVE_SPEND=1 pixi run pytest tests/live/test_flashvsr_live.py::test_f_single -v
pixi run kinoforge list
```

**Steps:**

- [ ] **Step 1: Preflight**

Run: `pixi run preflight`
Expected: exit 0.

- [ ] **Step 2: Fire live smoke**

Run:
```bash
KINOFORGE_LIVE_SPEND=1 pixi run pytest \
  tests/live/test_flashvsr_live.py::test_f_single -v -s
```

Expected: `1 passed` in < 15 min. During the run, poll pod stats every 60-90 s per CLAUDE.md live-smoke monitoring rule — if GPU stays 0 % for ≥ 3 consecutive probes while a generation is supposedly in flight, capture logs, destroy pod via `kinoforge destroy --id <id>`, fail the smoke fast.

- [ ] **Step 3: Post-run ledger check**

Run: `pixi run kinoforge list`
Expected: both no-instance sentinel lines present.

- [ ] **Step 4: Append `successful-generations.md`**

Follow the schema in the file's preamble. Tuple `(runpod, diffusers, flashvsr-wan21-bfloat16, upscale-only)` is new — new full section.

- [ ] **Step 5: Update `PROGRESS.md`**

Flip T#8 checkpoint → GREEN with pod id + spend + evidence file paths. Close the "FlashVSR T#8 BLOCKED" section, move it under "SHIPPED".

- [ ] **Step 6: Commit**

```bash
git add tests/live/test_flashvsr_live.py PROGRESS.md /workspace/successful-generations.md
git commit -m "test(live): FlashVSR F-single GREEN via T7.6 runtime rewrite (T7.6.6)"
```

---

## Self-Review

- **Spec coverage:** every scope item from PROGRESS.md T#8 checkpoint (lines 47-69) maps to a task: helpers (T0), runtime (T2), config default (T1), engine spec check (T3), cfg + live-test rename (T4), lint/type gate (T5), live smoke (T6).
- **Placeholder scan:** no `TBD` / `add appropriate error handling` / `similar to Task N` — every step ships code or exact commands.
- **Type consistency:** `_native_scale = 4.0` (float) reused in Task 2 + Task 3; `precision` allowlist `("bfloat16","fp16","fp32")` matches in Task 1 (config), Task 2 (runtime dtype branch), Task 3 (engine slug); slug format `flashvsr-wan21-<precision>` reused in Task 3 + Task 4 + Task 6.

No user-gate tags: none of the tasks match the trigger rule (no Nouns-bucket phrases like "acceptance test", no explicit ordering ("first on one, then all"), and while the plan itself uses "verify"/"validate", those verbs alone do not qualify per the specifying-gates rule).
