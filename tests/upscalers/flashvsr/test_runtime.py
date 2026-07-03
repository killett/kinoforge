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
        self.load_to_device_calls: list[Any] = []
        self.pipe_calls: list[dict[str, Any]] = []

    @classmethod
    def from_model_manager(cls, mm: Any, device: str = "cuda") -> _StubPipe:  # noqa: ARG003
        return cls()

    def enable_vram_management(self, **kwargs: Any) -> None:  # noqa: ARG002
        self.enable_vram_calls += 1

    def init_cross_kv(self) -> None:
        self.init_cross_kv_calls += 1

    def load_models_to_device(self, device: Any = "cuda") -> None:
        self.load_to_device_calls.append(device)
        if isinstance(device, str):
            self._device = device

    def to(self, device: str) -> _StubPipe:
        self._device = device
        return self

    def __call__(self, **kwargs: Any) -> Any:
        self.pipe_calls.append(kwargs)

        # Fake output tensor shape (1, 3, F, th, tw) bfloat16
        class _T:
            def __init__(self, num_frames: int, th: int, tw: int) -> None:
                self.shape = (1, 3, num_frames, th, tw)

            def cpu(self) -> _T:
                return self

            def to(self, *a: Any, **k: Any) -> _T:
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


@pytest.fixture()
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

        # Provide a comprehensive torch stub so that if _input_prep's
        # real prepare_input_tensor is ever called (e.g. due to module-cache
        # ordering after test_input_prep.py evicts _input_prep), it runs
        # cleanly and returns a recognisable sentinel shape.

        class _T:
            """Minimal tensor stub: carries shape, propagates through ops."""

            def __init__(self, shape: tuple[int, ...]) -> None:
                self.shape = shape

            def to(self, *a: Any, **k: Any) -> _T:
                return _T(self.shape)

            def permute(self, *dims: int) -> _T:
                return _T(tuple(self.shape[i] for i in dims))

            def unsqueeze(self, dim: int) -> _T:
                s = list(self.shape)
                s.insert(dim, 1)
                return _T(tuple(s))

            def squeeze(self, dim: int) -> _T:
                s = list(self.shape)
                del s[dim]
                return _T(tuple(s))

            def __truediv__(self, other: float) -> _T:
                return _T(self.shape)

            def __mul__(self, other: float) -> _T:
                return _T(self.shape)

            def __sub__(self, other: float) -> _T:
                return _T(self.shape)

            def cpu(self) -> _T:
                return _T(self.shape)

            def numpy(self) -> Any:
                import numpy as _np

                return _np.zeros(self.shape, dtype="uint8")

        def _from_numpy(arr: Any) -> _T:
            return _T(arr.shape)

        def _stack(tensors: list[Any], dim: int = 0) -> _T:
            base = list(tensors[0].shape)
            base.insert(dim, len(tensors))
            return _T(tuple(base))

        t.from_numpy = _from_numpy  # type: ignore[attr-defined]
        t.stack = _stack  # type: ignore[attr-defined]
        t.float32 = "fp32-sentinel"  # type: ignore[attr-defined]  # already set above

        nn_functional = types.ModuleType("torch.nn.functional")

        def _interpolate(
            x: Any,
            size: Any = None,
            mode: str = "bilinear",
            align_corners: bool | None = None,
        ) -> _T:
            b, c = x.shape[0], x.shape[1]
            th, tw = size if size is not None else (x.shape[-2], x.shape[-1])
            return _T((b, c, th, tw))

        nn_functional.interpolate = _interpolate  # type: ignore[attr-defined]

        nn_mod = types.ModuleType("torch.nn")
        nn_mod.functional = nn_functional  # type: ignore[attr-defined]
        t.nn = nn_mod  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "torch", t)
        monkeypatch.setitem(sys.modules, "torch.nn", nn_mod)
        monkeypatch.setitem(sys.modules, "torch.nn.functional", nn_functional)

    if "numpy" not in sys.modules:
        n = types.ModuleType("numpy")

        def zeros(shape: Any, dtype: str = "uint8") -> Any:  # noqa: ARG001
            return object()

        n.zeros = zeros  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "numpy", n)

    if "imageio.v3" not in sys.modules:
        ii = types.ModuleType("imageio.v3")

        def imwrite(
            path: str, data: Any, fps: float = 24.0, plugin: str = "pyav"
        ) -> None:  # noqa: ARG001
            Path(path).write_bytes(b"MP4-STUB")

        # imopen is needed in case _input_prep.prepare_input_tensor is ever
        # called without the stub (e.g. due to module-cache ordering across tests).
        def imopen(path: str, mode: str, plugin: str = "pyav") -> Any:  # noqa: ARG001
            class _Reader:
                metadata: dict[str, Any] = {"fps": 24.0}

                def iter(self) -> Any:
                    import numpy as _np

                    for _ in range(16):
                        yield _np.zeros((16, 16, 3), dtype="uint8")

                def close(self) -> None: ...

                def __enter__(self) -> _Reader:
                    return self

                def __exit__(self, *a: Any) -> None: ...

            return _Reader()

        ii.imwrite = imwrite  # type: ignore[attr-defined]
        ii.imopen = imopen  # type: ignore[attr-defined]
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
    """RED: __init__ pulls diffsynth lazily, not at module load.

    Bug caught: top-level ``import diffsynth`` in _runtime.py breaks the
    kinoforge-default env (diffsynth lives only in the live-flashvsr feature env).
    """
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
    """RED: enable_vram_management + init_cross_kv + load_models_to_device all fire.

    Bug caught: skipping init_cross_kv causes silent KV-cache misses on first
    inference, producing blurry/corrupted output that appears deceptively close
    to correct.
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    assert rt._pipe.enable_vram_calls == 1
    assert rt._pipe.init_cross_kv_calls == 1
    # load_models_to_device called at least once during construction
    assert len(rt._pipe.load_to_device_calls) >= 1


def test_upscale_produces_flashvsr_mp4_suffix(
    stub_diffsynth: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: upscale returns Path with .flashvsr.mp4 suffix.

    Bug caught: sibling-suffix collision with .upscaled.mp4 (spandrel's
    output naming) → later stage overwrites the earlier stage's artifact.
    """
    _stub_input_prep(monkeypatch)
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    out = rt.upscale(src, ScaleTarget(kind="factor", value=4.0), {})
    assert out.name == "in.flashvsr.mp4"
    assert out.exists()


def test_upscale_height_target_raises(stub_diffsynth: None, tmp_path: Path) -> None:
    """RED: kind=height is not yet supported at runtime either.

    Bug caught: cfg-time gate bypassed via direct runtime call →
    silently proceeds and produces a wrong-dimension output.
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    with pytest.raises(NotYetImplementedError):
        rt.upscale(src, ScaleTarget(kind="height", value=1080), {})


def test_upscale_mismatched_scale_raises(stub_diffsynth: None, tmp_path: Path) -> None:
    """RED: scale != 4.0 raises UnsupportedScaleError (upstream fixed 4x).

    Bug caught: --scale 2x against a 4x-only checkpoint silently runs and
    produces under-upscaled output labeled as 2x.
    """
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
    """RED: ``params['prompt']`` logs a warning; does NOT raise.

    Bug caught: raising on prompt breaks multi-stage cfgs that pass
    the Wan generation prompt through job.params for observability.
    """
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
    """RED: ``.to("cuda")`` delegates to the wrapped pipe.

    Bug caught: no-op ``.to()`` implementation — LRU thinks the model is
    on CUDA but inference runs on CPU (silent, catastrophic slowdown).
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    # Record call count before explicit .to()
    calls_before = len(rt._pipe.load_to_device_calls)
    rt.to("cuda")
    assert len(rt._pipe.load_to_device_calls) > calls_before
    assert "cuda" in rt._pipe.load_to_device_calls
