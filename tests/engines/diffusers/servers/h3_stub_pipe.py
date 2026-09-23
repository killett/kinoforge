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

The LoRA surface is modelled on the same terms. H3 ships two checkpoint
partitions with IDENTICAL module names — ``transformer`` (t2va / fl2va) and
``transformer_ref`` (ref2va) — and a workflow loads only its own, so which
partitions a pipe HOLDS is the fact the server's profile is built from.
:func:`stub_loader` holds one; :func:`stub_loader_dual` holds both.

The partitions are reached through ``__getattr__`` rather than set in
``__init__`` on purpose: three existing tests install a recorder on the CLASS
with ``monkeypatch.setattr(FakePipe, "transformer", ...)`` to drive
``set_attention_backend``, and an instance attribute would shadow it. Normal
attribute lookup finds a class attribute first and never reaches
``__getattr__``, so both uses coexist.
"""

from __future__ import annotations

from collections.abc import Sequence
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


class FakeModule:
    """One checkpoint partition, recording the two calls the LoRA seam makes.

    ``set_adapters`` is what weights an attached adapter; ``to`` is the dtype
    restore the H3 profile runs after every load. Both record rather than
    assert, per this module's recording discipline — the test decides what the
    record should say.
    """

    def __init__(self, name: str) -> None:
        """Name the partition and start both call logs empty.

        Args:
            name: The partition this module stands for.
        """
        self.name = name
        self.set_adapters_calls: list[tuple[list[str], list[float]]] = []
        self.dtype_calls: list[Any] = []

    def set_adapters(self, names: Sequence[str], weights: Sequence[float]) -> None:
        """Record one adapter-weighting call.

        Args:
            names: Adapter names, in activation order.
            weights: Per-adapter strengths, positionally matched to *names*.
        """
        self.set_adapters_calls.append((list(names), list(weights)))

    def to(self, dtype: Any) -> FakeModule:
        """Record a dtype cast and return self, as ``torch.nn.Module.to`` does.

        Args:
            dtype: The requested dtype.

        Returns:
            This module.
        """
        self.dtype_calls.append(dtype)
        return self


class FakePipe:
    """A ``ModularPipeline`` stand-in recording how it was built and called."""

    def __init__(
        self,
        *,
        partitions: Sequence[str] = ("transformer",),
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
            partitions: The checkpoint partitions this pipeline HOLDS. A t2va
                (or fl2va) pipeline holds ``transformer`` alone, ref2va holds
                ``transformer_ref`` alone; both together is the ambiguous case.
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
        self.partitions: dict[str, FakeModule] = {
            name: FakeModule(name) for name in partitions
        }
        self.lora_loads: list[dict[str, Any]] = []
        self.unload_calls = 0
        self.lora_raises: BaseException | None = None

    def __getattr__(self, name: str) -> Any:
        """Expose the held partitions as attributes, and nothing else.

        Only reached when normal lookup fails, so a class attribute a test
        installed (the ``set_attention_backend`` recorders) still wins.

        Args:
            name: The attribute being looked up.

        Returns:
            The held :class:`FakeModule` of that name.

        Raises:
            AttributeError: This pipeline does not hold that partition — which
                is exactly how the server reads "this workflow did not load
                it".
        """
        partitions = self.__dict__.get("partitions", {})
        if name in partitions:
            return partitions[name]
        raise AttributeError(name)

    def load_lora_weights(
        self,
        path: str,
        *,
        adapter_name: str,
        load_into_transformer_ref: bool = False,
    ) -> None:
        """Record one adapter load, in the spelling the H3 profile uses.

        Args:
            path: The LoRA file on disk.
            adapter_name: Positional adapter name from the shared seam.
            load_into_transformer_ref: H3's partition selector. Keyword-only
                and recorded verbatim: the whole point of the profile's
                ``load`` is which value this gets.

        Raises:
            BaseException: Whatever ``self.lora_raises`` holds, for the
                failure path.
        """
        self.lora_loads.append(
            {
                "path": path,
                "adapter_name": adapter_name,
                "load_into_transformer_ref": load_into_transformer_ref,
            }
        )
        if self.lora_raises is not None:
            raise self.lora_raises

    def unload_lora_weights(self) -> None:
        """Count one unload — the seam's replace-never-accumulate invariant."""
        self.unload_calls += 1

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


def _load(partitions: Sequence[str], workflow: str | None) -> tuple[Any, Any]:
    """Build a fake pipeline + manager the way the real ``_load`` builds them.

    Mirrors the real loading order — construct a manager, hand it to
    ``from_pretrained`` with the workflow, load the components, then enable auto
    offload — so the server's ordering and kwargs assertions mean something.

    Args:
        partitions: The checkpoint partitions the pipeline holds.
        workflow: The ``workflow=`` kwarg recorded on the build, or ``None``
            when no workflow was named (which is how BOTH partitions load).

    Returns:
        The fake pipeline and its fake components manager.
    """
    pipe = FakePipe(partitions=partitions, **STATE.get("pipe_kwargs", {}))
    manager = FakeComponentsManager()
    pipe.from_pretrained_kwargs = {
        "pretrained_model_name_or_path": "MiniMaxAI/MiniMax-H3",
        "workflow": workflow,
        "components_manager": manager,
    }
    pipe.load_components(dtype="bfloat16")
    manager.enable_auto_cpu_offload(device="cuda", memory_reserve_margin="12GB")
    STATE["pipe"] = pipe
    STATE["manager"] = manager
    return pipe, manager


def stub_loader() -> tuple[Any, Any]:
    """Stand in for the server's real ``_load``; returns ``(pipe, manager)``.

    The t2va case the server actually ships: ``workflow="t2va"`` loads
    ``transformer`` and leaves ``transformer_ref`` on the hub.

    Returns:
        The fake pipeline and its fake components manager.
    """
    return _load(("transformer",), "t2va")


def stub_loader_dual() -> tuple[Any, Any]:
    """A pipeline holding BOTH partitions — the ambiguous case.

    This is what omitting ``workflow=`` produces: the $66 mistake the server's
    ``_load`` docstring is built around. It exists here because it is the only
    shape in which a default target would have to be a guess, and the profile
    must refuse to make one.

    Returns:
        The fake pipeline and its fake components manager.
    """
    return _load(("transformer", "transformer_ref"), None)
