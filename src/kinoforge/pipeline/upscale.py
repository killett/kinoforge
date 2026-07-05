"""UpscaleStage — PipelineState in, PipelineState out.

Reads ``state.artifacts["clip"]``, invokes the configured ``UpscalerEngine``,
writes ``state.artifacts["upscaled"]``. A height target (``ScaleTarget(kind=
"height")``) is resolved here to a concrete factor plus an optional
``downscale_to`` stashed on the upscaled artifact's ``.meta`` for the orchestrator
materialize boundary to apply. Engines only ever receive ``kind="factor"``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import ScaleUnsatisfiableError
from kinoforge.core.frames import ffprobe_dims
from kinoforge.core.interfaces import (
    Artifact,
    Instance,
    PipelineState,
    UpscaleJob,
    UpscalerEngine,
    UpscaleResult,
)
from kinoforge.core.scale_resolver import resolve_height_target
from kinoforge.core.scale_target import ScaleTarget


@dataclass
class UpscaleStage:
    """A Stage that upscales the rendered clip in-place.

    Attributes:
        engine: Configured UpscalerEngine (already provisioned).
        scale: Parsed ScaleTarget. ``kind="height"`` is resolved here.
        instance: Compute instance passed to the engine; None for local engines.
        cfg: Runtime config dict the engine interprets.
        cancel_token: Threaded through to ``engine.upscale``.
        probe_dims: Injectable ``(path) -> (w, h)`` seam (tests override).
    """

    engine: UpscalerEngine
    scale: ScaleTarget
    instance: Instance | None
    cfg: dict[str, Any]
    cancel_token: CancelToken | None = None
    probe_dims: Callable[[str | Path], tuple[int, int]] = ffprobe_dims

    def run(self, state: PipelineState) -> PipelineState:
        """Run the upscale, returning a new state with ``upscaled`` populated."""
        clip = state.artifacts["clip"]
        if self.scale.kind == "factor":
            upscaled = self._run_engine(clip, self.scale).artifact
        else:
            upscaled = self._run_height(clip)
        new_artifacts = dict(state.artifacts)
        new_artifacts["upscaled"] = upscaled
        return replace(state, artifacts=new_artifacts)

    def _run_engine(self, clip: Artifact, scale: ScaleTarget) -> UpscaleResult:
        """Invoke the engine at a concrete factor scale."""
        job = UpscaleJob(source=clip, scale=scale)
        return self.engine.upscale(
            self.instance, job, self.cfg, cancel_token=self.cancel_token
        )

    def _run_height(self, clip: Artifact) -> Artifact:
        """Resolve a height target to a factor + optional downscale meta."""
        requested_h = int(self.scale.value)
        factors = tuple(
            s.value for s in self.engine.supported_scales if s.kind == "factor"
        )
        source_h = self._source_h(clip)

        if source_h is not None:
            plan = resolve_height_target(source_h, factors, requested_h)
            if plan.upscale_factor is None:
                return self._stash(clip, plan.downscale_to)
            result = self._run_engine(
                clip, ScaleTarget(kind="factor", value=plan.upscale_factor)
            )
            return self._stash(result.artifact, plan.downscale_to)

        # Remote source: dims unknown pre-run. Single-factor engines run their
        # sole factor and decide from the reported output_resolution; multi-factor
        # engines cannot pick a factor blind.
        if len(factors) != 1:
            raise ScaleUnsatisfiableError(
                source_h=-1,
                largest_factor=max(factors) if factors else 0.0,
                reached_h=-1,
                requested_h=requested_h,
            )
        result = self._run_engine(clip, ScaleTarget(kind="factor", value=factors[0]))
        output_h = int(result.output_resolution[1])
        if output_h < requested_h:
            raise ScaleUnsatisfiableError(
                source_h=int(result.input_resolution[1]),
                largest_factor=factors[0],
                reached_h=output_h,
                requested_h=requested_h,
            )
        downscale_to = None if output_h == requested_h else requested_h
        return self._stash(result.artifact, downscale_to)

    def _source_h(self, clip: Artifact) -> int | None:
        """Vertical resolution of a locally-readable source, else None."""
        uri = clip.uri
        if uri.startswith("file://"):
            return self.probe_dims(uri.removeprefix("file://"))[1]
        if uri.startswith("/"):
            return self.probe_dims(uri)[1]
        return None

    def _stash(self, artifact: Artifact, downscale_to: int | None) -> Artifact:
        """Attach ``downscale_to`` to the artifact meta (omit when None)."""
        if downscale_to is None:
            return artifact
        return replace(artifact, meta={**artifact.meta, "downscale_to": downscale_to})
