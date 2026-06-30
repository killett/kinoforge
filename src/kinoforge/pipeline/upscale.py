"""UpscaleStage — PipelineState in, PipelineState out.

Reads ``state.artifacts["clip"]``, invokes the configured ``UpscalerEngine``,
writes ``state.artifacts["upscaled"]``. Defensive raise on
``ScaleTarget(kind="height")`` mirrors the engine-level raise so cfgs that
pass schema validation but ask for the height branch still fail before pod
work begins.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.interfaces import (
    Instance,
    PipelineState,
    UpscaleJob,
    UpscalerEngine,
)
from kinoforge.core.scale_target import ScaleTarget


@dataclass
class UpscaleStage:
    """A Stage that upscales the rendered clip in-place.

    Attributes:
        engine: Configured UpscalerEngine (already provisioned).
        scale: Parsed ScaleTarget. ``kind="height"`` raises
            :class:`NotYetImplementedError`.
        instance: Compute instance to pass through to the engine; None for
            local engines.
        cfg: Runtime config dict the engine interprets.
        cancel_token: Threaded through to ``engine.upscale``.
    """

    engine: UpscalerEngine
    scale: ScaleTarget
    instance: Instance | None
    cfg: dict[str, Any]
    cancel_token: CancelToken | None = None

    def run(self, state: PipelineState) -> PipelineState:
        """Run the upscale, returning a new state with ``upscaled`` populated."""
        if self.scale.kind == "height":
            raise NotYetImplementedError(
                f"--scale {int(self.scale.value)}p deferred to a later "
                f"session; use --scale Nx for v1"
            )
        clip = state.artifacts["clip"]
        job = UpscaleJob(source=clip, scale=self.scale)
        result = self.engine.upscale(
            self.instance, job, self.cfg, cancel_token=self.cancel_token
        )
        new_artifacts = dict(state.artifacts)
        new_artifacts["upscaled"] = result.artifact
        return replace(state, artifacts=new_artifacts)
