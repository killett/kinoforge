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


class _StubDenoisingModel:
    """Duck-types the object returned by pipe.denoising_model()."""

    def __init__(self) -> None:
        self.LQ_proj_in: Any = None


class _StubVAE:
    """Duck-types pipe.vae with encoder/conv1 teardown attributes."""

    class _Model:
        encoder: Any = object()
        conv1: Any = object()

    def __init__(self) -> None:
        self.model = _StubVAE._Model()


class _StubPipe:
    """Duck-types diffsynth.FlashVSRFullPipeline for unit tests.

    Tracks every lifecycle-relevant call in ``lifecycle_calls`` so that
    ordering assertions can be made without counting individual lists.
    """

    def __init__(self) -> None:
        self._device = "cpu"
        self.enable_vram_calls = 0
        self.init_cross_kv_calls = 0
        self.load_to_device_calls: list[Any] = []
        self.to_calls: list[str] = []
        self.pipe_calls: list[dict[str, Any]] = []
        # Ordered log of every lifecycle-relevant call name.
        self.lifecycle_calls: list[str] = []
        self._denoising_model = _StubDenoisingModel()
        self.vae = _StubVAE()
        # Track whether grad was enabled during __call__
        self.grad_enabled_during_call: bool | None = None

    @classmethod
    def from_model_manager(cls, mm: Any, device: str = "cuda") -> _StubPipe:  # noqa: ARG003
        inst = cls()
        inst.lifecycle_calls.append("from_model_manager")
        return inst

    def denoising_model(self) -> _StubDenoisingModel:
        return self._denoising_model

    def enable_vram_management(self, **kwargs: Any) -> None:  # noqa: ARG002
        self.enable_vram_calls += 1
        self.lifecycle_calls.append("enable_vram_management")

    def init_cross_kv(self) -> None:
        self.init_cross_kv_calls += 1
        self.lifecycle_calls.append("init_cross_kv")

    def load_models_to_device(self, device: Any = "cuda") -> None:
        self.load_to_device_calls.append(device)
        self.lifecycle_calls.append("load_models_to_device")
        if isinstance(device, str):
            self._device = device

    def to(self, device: str, **kwargs: Any) -> _StubPipe:  # noqa: ARG002
        self._device = device
        self.to_calls.append(device)
        self.lifecycle_calls.append("to")
        return self

    def __call__(self, **kwargs: Any) -> Any:
        import torch

        self.grad_enabled_during_call = torch.is_grad_enabled()
        self.pipe_calls.append(kwargs)

        # Fake output tensor shape (1, 3, F, H, W) — returns float values in
        # [-1, 1] range to exercise the denormalisation path in upscale().
        # Use real numpy (always available in the test env) so that np.full,
        # .clip, and .astype work correctly in the denorm path.
        class _T:
            def __init__(self, num_frames: int, th: int, tw: int) -> None:
                self.shape = (1, 3, num_frames, th, tw)

            def cpu(self) -> _T:
                return self

            def float(self) -> _T:
                return self

            def to(self, *a: Any, **k: Any) -> _T:
                return self

            def numpy(self) -> Any:
                import numpy as np

                # Return a mixed-value array spanning [-1.0, 0.0, 1.0] so
                # the denorm test can distinguish (x+1)*127.5 from wrong
                # formulas such as x*127.5+127.5 (both yield 127 at x=0,
                # but differ at x=-1 and x=1).
                # Shape: (1, 3, F, H, W) — we write sentinel values into
                # three spatial positions across channel 0 / frame 0.
                arr = np.zeros(self.shape, dtype=np.float32)
                # Position (0, 0, 0, 0, 0) → -1.0 → denorm → 0
                arr[0, 0, 0, 0, 0] = -1.0
                # Position (0, 0, 0, 0, 1) → 0.0 → denorm → 127
                arr[0, 0, 0, 0, 1] = 0.0
                # Position (0, 0, 0, 0, 2) → 1.0 → denorm → 255
                arr[0, 0, 0, 0, 2] = 1.0
                return arr

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

        # is_grad_enabled: default True (unless inside no_grad context).
        # The _StubNoGrad context manager installed below patches this.
        _grad_enabled = [True]

        def _is_grad_enabled() -> bool:
            return _grad_enabled[0]

        t.is_grad_enabled = _is_grad_enabled  # type: ignore[attr-defined]

        class _StubNoGrad:
            """Minimal torch.no_grad() context-manager stub.

            Tracks enter/exit counts so tests can assert the context was used
            exactly once and properly exited.
            """

            enter_count: int = 0
            exit_count: int = 0

            def __enter__(self) -> _StubNoGrad:
                _grad_enabled[0] = False
                _StubNoGrad.enter_count += 1
                return self

            def __exit__(self, *args: Any) -> None:
                _grad_enabled[0] = True
                _StubNoGrad.exit_count += 1

        t.no_grad = _StubNoGrad  # type: ignore[attr-defined]

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

        def _torch_load(path: str, map_location: Any = None) -> dict[str, Any]:  # noqa: ARG001
            return {}

        t.load = _torch_load  # type: ignore[attr-defined]

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

        class _StubConv3d:
            def __init__(self, *a: Any, **k: Any) -> None:
                self.weight = object()
                self.bias = object()
                # Records every .to() call as (args, kwargs) for dtype assertions.
                self.to_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

            def __call__(self, x: Any) -> Any:
                return x

            def to(self, *a: Any, **k: Any) -> _StubConv3d:
                self.to_calls.append((a, k))
                return self

            def state_dict(self) -> dict[str, Any]:
                return {}

            def load_state_dict(self, d: Any, strict: bool = True) -> None:
                pass

        class _StubNN:
            Conv3d = _StubConv3d

        nn_mod = types.ModuleType("torch.nn")
        nn_mod.functional = nn_functional  # type: ignore[attr-defined]
        nn_mod.Conv3d = _StubConv3d  # type: ignore[attr-defined]
        t.nn = nn_mod  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "torch", t)
        monkeypatch.setitem(sys.modules, "torch.nn", nn_mod)
        monkeypatch.setitem(sys.modules, "torch.nn.functional", nn_functional)

    if "imageio.v3" not in sys.modules:
        ii = types.ModuleType("imageio.v3")

        def imwrite(
            path: str,
            data: Any,
            fps: float = 24.0,
            plugin: str = "pyav",
            codec: str = "libx264",
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
    """RED: lifecycle methods fire in the correct upstream order.

    Bug caught: skipping init_cross_kv causes silent KV-cache misses on first
    inference, producing blurry/corrupted output that appears deceptively close
    to correct.

    Order verified against infer_flashvsr_v1.1_full.py::init_pipeline:
      from_model_manager → to → enable_vram_management → init_cross_kv → load_models_to_device
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    assert rt._pipe.enable_vram_calls == 1
    assert rt._pipe.init_cross_kv_calls == 1
    # load_models_to_device called with the model-name list, not a device string
    assert ["dit", "vae"] in rt._pipe.load_to_device_calls

    # Verify ORDER: from_model_manager → to → enable_vram_management
    #               → init_cross_kv → load_models_to_device
    lc = rt._pipe.lifecycle_calls
    assert lc.index("from_model_manager") < lc.index("to"), (
        "pipe.to() must come after from_model_manager"
    )
    assert lc.index("to") < lc.index("enable_vram_management"), (
        "enable_vram_management must come after to()"
    )
    assert lc.index("enable_vram_management") < lc.index("init_cross_kv"), (
        "init_cross_kv must come after enable_vram_management"
    )
    assert lc.index("init_cross_kv") < lc.index("load_models_to_device"), (
        "load_models_to_device must come after init_cross_kv"
    )


def test_construct_wires_lq_proj_in(stub_diffsynth: None, tmp_path: Path) -> None:
    """RED: LQ_proj_in is injected on the denoising model after from_model_manager.

    Bug caught: omitting the projection injection means the conditioning tensor
    never passes through the LQ feature extraction layer — the diffusion model
    receives raw unprocessed frames (silent correctness failure, not a crash).
    """
    from kinoforge.upscalers.flashvsr._input_prep import Causal_LQ4x_Proj
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    assert rt._pipe._denoising_model.LQ_proj_in is not None, (
        "LQ_proj_in must be set on the denoising model after construction"
    )
    assert isinstance(rt._pipe._denoising_model.LQ_proj_in, Causal_LQ4x_Proj), (
        "LQ_proj_in must be an instance of Causal_LQ4x_Proj"
    )


def test_construct_loads_lq_ckpt_when_present(
    stub_diffsynth: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: LQ_proj_in.ckpt is loaded from weights_dir if the file exists.

    Bug caught: missing conditional load means the LQ projection always uses
    random-init weights even when the trained checkpoint is present — silent
    quality regression.
    """
    import torch

    # Write a fake checkpoint file so the existence check passes.
    ckpt_path = tmp_path / "LQ_proj_in.ckpt"
    ckpt_path.write_bytes(b"FAKE-CKPT")

    load_calls: list[str] = []

    def fake_load(path: str, map_location: str = "cpu") -> dict[str, Any]:  # noqa: ARG001
        load_calls.append(path)
        return {}

    monkeypatch.setattr(torch, "load", fake_load)

    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    assert len(load_calls) == 1, "torch.load should be called once for LQ_proj_in.ckpt"
    assert str(ckpt_path) in load_calls[0]


def test_construct_vae_encoder_teardown(stub_diffsynth: None, tmp_path: Path) -> None:
    """RED: VAE encoder and conv1 are None'd out after pipeline creation.

    Bug caught: retaining the encoder in VRAM causes OOM on tighter GPUs
    (VAE encoder is unused during inference — only the decoder is needed).
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    assert rt._pipe.vae.model.encoder is None, (
        "pipe.vae.model.encoder must be set to None (VRAM teardown)"
    )
    assert rt._pipe.vae.model.conv1 is None, (
        "pipe.vae.model.conv1 must be set to None (VRAM teardown)"
    )


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


def test_to_delegates_to_pipe_to_not_load_models(
    stub_diffsynth: None, tmp_path: Path
) -> None:
    """RED: ``.to("cuda")`` calls pipe.to() — NOT load_models_to_device().

    Bug caught: the prior implementation called ``load_models_to_device(device)``
    which takes a *list of model name strings*, not a device.  Passing ``"cuda"``
    (a str) would iterate char-by-char — ``["c","u","d","a"]`` — silently
    corrupting VRAM management on every LRU eviction.
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)

    # Clear counters from construction so we only measure the explicit .to() call.
    to_calls_before = list(rt._pipe.to_calls)
    load_calls_before = list(rt._pipe.load_to_device_calls)

    rt.to("cuda")

    # pipe.to() must have been called once more with "cuda"
    new_to_calls = rt._pipe.to_calls[len(to_calls_before) :]
    assert new_to_calls == ["cuda"], (
        f"pipe.to() must be called with 'cuda'; got {new_to_calls}"
    )

    # load_models_to_device must NOT have been called by .to()
    new_load_calls = rt._pipe.load_to_device_calls[len(load_calls_before) :]
    assert new_load_calls == [], (
        f"load_models_to_device must NOT be called by .to(); got {new_load_calls}"
    )

    rt.to("cpu")
    assert rt._pipe.to_calls[-1] == "cpu", "pipe.to() must accept 'cpu' for LRU offload"


def test_upscale_wraps_pipe_in_no_grad(
    stub_diffsynth: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: upscale() calls pipe() inside torch.no_grad().

    Bug caught: bare pipe() call without no_grad builds a computation graph
    for the entire diffusion forward pass, consuming extra VRAM and CPU time
    with no benefit (VSR inference does not need gradients).
    """
    import torch

    _stub_input_prep(monkeypatch)
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    # Reset the no_grad stub counters before the run.
    torch.no_grad.enter_count = 0  # noqa: B010
    torch.no_grad.exit_count = 0  # noqa: B010

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    rt.upscale(src, ScaleTarget(kind="factor", value=4.0), {})

    # The pipe __call__ stub records grad_enabled status at call time.
    assert rt._pipe.grad_enabled_during_call is False, (
        "torch.is_grad_enabled() must be False inside the pipe() call — "
        "torch.no_grad() context must be active"
    )

    # Also verify the context was properly entered and exited (no leak).
    assert torch.no_grad.enter_count == 1, (
        "torch.no_grad().__enter__ must be called exactly once per upscale()"
    )
    assert torch.no_grad.exit_count == 1, (
        "torch.no_grad().__exit__ must be called exactly once (context not leaked)"
    )


def test_upscale_denormalises_output_tensor(
    stub_diffsynth: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED: upscale applies (x+1)*127.5 denorm before writing uint8 video.

    Bug caught: naïve ``.astype(np.uint8)`` on a [-1,1] float tensor truncates
    all negative values to 0 (most pixels) and clips 1.0 to 1 → produces a
    nearly-black video with a single non-zero row at the maximum. The error is
    silent — imageio writes a valid-format MP4 with incorrect pixel values.

    The stub pipe returns a mixed-value array with sentinel pixels at
    positions (F=0,H=0,W=0..2) → [-1.0, 0.0, 1.0].  This distinguishes
    the correct formula (x+1)*127.5 from wrong variants such as x*127.5+127.5
    (both agree at x=0 → 127, but diverge at x=-1 and x=1).
    """
    import numpy as np

    _stub_input_prep(monkeypatch)

    # Intercept the imwrite call to inspect the array passed to imageio.
    imwrite_calls: list[Any] = []

    import imageio.v3 as iio_stub

    def capturing_imwrite(
        path: str,
        data: Any,
        fps: float = 24.0,
        plugin: str = "pyav",
        codec: str = "libx264",
    ) -> None:
        imwrite_calls.append(data)
        Path(path).write_bytes(b"MP4-STUB")

    monkeypatch.setattr(iio_stub, "imwrite", capturing_imwrite)

    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "bfloat16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    rt.upscale(src, ScaleTarget(kind="factor", value=4.0), {})

    assert len(imwrite_calls) == 1, "imwrite must be called exactly once"
    video = imwrite_calls[0]

    # Shape must be (F, H, W, 3) after (1,3,F,H,W) → (F,H,W,3) rearrange.
    assert hasattr(video, "dtype"), "video passed to imwrite must be a numpy array"
    assert video.dtype == np.uint8, (
        f"expected uint8 output; got {video.dtype} — denorm path missing"
    )
    assert len(video.shape) == 4, f"expected (F, H, W, 3) shape; got {video.shape}"
    assert video.shape[-1] == 3, f"last dim must be 3 (channels); got {video.shape}"

    # Verify the exact denorm formula (x+1)*127.5 using the three sentinels:
    #   input -1.0 → (-1+1)*127.5 = 0   → uint8 0
    #   input  0.0 → (0+1)*127.5  = 127  → uint8 127
    #   input  1.0 → (1+1)*127.5  = 255  → uint8 255
    # Stub wrote sentinels into channel-0 of frame-0 at W=0,1,2.
    # After (1,3,F,H,W) → (F,H,W,3) the layout is video[frame, H, W, channel].
    pixel_neg1 = int(video[0, 0, 0, 0])  # W=0, channel 0 → input -1.0
    pixel_zero = int(video[0, 0, 1, 0])  # W=1, channel 0 → input  0.0
    pixel_pos1 = int(video[0, 0, 2, 0])  # W=2, channel 0 → input  1.0
    assert pixel_neg1 == 0, (
        f"denorm(-1.0) must be 0; got {pixel_neg1} — wrong formula or clip"
    )
    assert pixel_zero in (127, 128), (
        f"denorm(0.0) must be 127 or 128; got {pixel_zero} — wrong formula"
    )
    assert pixel_pos1 == 255, (
        f"denorm(1.0) must be 255; got {pixel_pos1} — wrong formula or clip"
    )


def test_lq_proj_in_dtype_tracks_precision(
    stub_diffsynth: None, tmp_path: Path
) -> None:
    """RED: LQ_proj_in is cast to the dtype derived from the precision arg.

    Bug caught: hardcoding ``dtype=torch.bfloat16`` in the .to() call causes
    a dtype mismatch when the user sets ``precision='fp32'`` — the pipe and
    all other weights are fp32 but the LQ projection is bfloat16, triggering a
    runtime dtype error on the first forward pass.
    """
    import torch

    from kinoforge.upscalers.flashvsr._input_prep import Causal_LQ4x_Proj
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "fp32", 24, 0, False)

    lq = rt._pipe._denoising_model.LQ_proj_in
    assert isinstance(lq, Causal_LQ4x_Proj), "LQ_proj_in must be a Causal_LQ4x_Proj"

    # _StubConv3d.to_calls records every (args, kwargs) pair from .to() calls.
    # We expect at least one call whose kwargs include dtype=torch.float32
    # (the fp32 sentinel in the stubbed torch module).
    dtype_kwargs = [k.get("dtype") for (_, k) in lq._conv.to_calls]
    assert torch.float32 in dtype_kwargs, (
        f"LQ_proj_in._conv.to() must receive dtype=torch.float32 for "
        f"precision='fp32'; recorded kwargs dtypes: {dtype_kwargs}"
    )
