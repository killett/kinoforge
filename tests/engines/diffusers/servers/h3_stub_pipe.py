"""In-memory MiniMax-H3 pipeline stub for the t2va server tests.

Installed via ``KINOFORGE_H3_LOAD_STUB``, which the server resolves with
``importlib.import_module``. It lives in its own module rather than inside the
test file for a concrete reason: a test module imported by dotted path from
production code can end up as a SECOND module object alongside the one pytest
collected, so the recording dict the test reads is not the one the stub wrote.
``tests/smoke/local_cpu/stub_pipe.py`` exists for the same reason.

The shapes returned here are the real ones, read from
``diffusers/modular_pipelines/minimax_h3/`` at v0.40.0:

* ``videos`` — ``(batch, frames, H, W, 3)`` float in [0, 1], which is what
  ``VideoProcessor.postprocess_video`` returns for ``output_type="np"``.
* ``audio`` — ``(1, 2, num_samples)``: batch, then the two stereo channels
  channel-major, as ``MiniMaxH3AudioDecodeStep`` emits them.
* ``sampling_rate`` — the audio VAE's own rate, 32000 for the released weights.
"""

from __future__ import annotations

from typing import Any

import numpy as np

#: Written by :func:`stub_loader`, read by the tests. Cleared per test.
STATE: dict[str, Any] = {}


class FakeComponentsManager:
    """Records the offload call the real ``ComponentsManager`` would receive."""

    def __init__(self) -> None:
        self.offload_calls: list[dict[str, Any]] = []

    def enable_auto_cpu_offload(self, **kwargs: Any) -> None:
        """Record an auto-CPU-offload request.

        Args:
            **kwargs: Whatever the server passed (``device``, margin, ...).
        """
        self.offload_calls.append(kwargs)


class FakePipe:
    """A ``ModularPipeline`` stand-in recording how it was built and called."""

    def __init__(
        self,
        *,
        frames: int = 8,
        height: int = 64,
        width: int = 96,
        samples: int = 1_000,
        sampling_rate: int = 32000,
    ) -> None:
        """Set the shapes and rate this fake returns.

        Defaults are deliberately TINY rather than the real 124x768x1344. The
        real canvas is a 1.5 GB float32 array per call, which makes the worker
        thread take seconds and turns these tests into a memory-pressure test.
        Nothing here depends on the size: the geometry the server forwards is
        pinned on the pipeline CALL kwargs, and the mux assertions are about
        dtype, rank, channel order and the audio transpose.

        Args:
            frames: Frame count of the fake video.
            height: Frame height.
            width: Frame width.
            samples: Audio samples per channel.
            sampling_rate: The rate reported alongside the audio.
        """
        self.from_pretrained_kwargs: dict[str, Any] = {}
        self.load_components_kwargs: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []
        self.order: list[str] = []
        self.raises: BaseException | None = None
        self._frames, self._h, self._w = frames, height, width
        self._samples, self._rate = samples, sampling_rate

    def load_components(self, **kwargs: Any) -> None:
        """Record a component-load request.

        Args:
            **kwargs: Whatever the server passed (``dtype``, ...).
        """
        self.load_components_kwargs = kwargs
        self.order.append("load_components")

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        """Return a fake t2va result in the real output shapes.

        Args:
            **kwargs: The generation request the server composed.

        Returns:
            ``videos`` / ``audio`` / ``sampling_rate``, per the module docstring.

        Raises:
            BaseException: Whatever ``self.raises`` holds, for the error path.
        """
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        videos = np.full((1, self._frames, self._h, self._w, 3), 0.5, dtype=np.float32)
        audio = np.zeros((1, 2, self._samples), dtype=np.float32)
        audio[0, 0, :] = 0.25  # distinguishable left channel
        audio[0, 1, :] = -0.25  # ... and right
        return {"videos": videos, "audio": audio, "sampling_rate": self._rate}


def stub_loader() -> tuple[Any, Any]:
    """Stand in for the server's real ``_load``; returns ``(pipe, manager)``.

    Mirrors the real loading order — construct a manager, hand it to
    ``from_pretrained`` with the workflow, load the components, then enable auto
    offload — so the server's ordering and kwargs assertions mean something.

    Returns:
        The fake pipeline and its fake components manager.
    """
    pipe = FakePipe(**STATE.get("pipe_kwargs", {}))
    manager = FakeComponentsManager()
    pipe.from_pretrained_kwargs = {
        "pretrained_model_name_or_path": "MiniMaxAI/MiniMax-H3",
        "workflow": "t2va",
        "components_manager": manager,
    }
    pipe.load_components(dtype="bfloat16")
    manager.enable_auto_cpu_offload(device="cuda", memory_reserve_margin="12GB")
    STATE["pipe"] = pipe
    STATE["manager"] = manager
    return pipe, manager
