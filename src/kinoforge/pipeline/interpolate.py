"""InterpolateStage — PipelineState in, PipelineState out.

Reads ``state.artifacts["clip"]``, raises its frame rate to ``target_fps`` and
writes ``state.artifacts["interpolated"]``. For a locally-readable source it
probes the fps + frame count and routes via the pure fps resolver: an upshift
calls the engine, a downshift decimates locally (no GPU), an equal rate passes
through. A remote (http) source always calls the engine (the server probes and
plans). Mirrors :mod:`kinoforge.pipeline.upscale`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kinoforge.core.cancel import CancelToken
from kinoforge.core.fps_resolver import resolve_fps_target
from kinoforge.core.frames import ffprobe_fps
from kinoforge.core.interfaces import (
    Artifact,
    Instance,
    InterpolateJob,
    InterpolatorEngine,
    PipelineState,
)
from kinoforge.pipeline.decimate import decimate_video_fps


def _default_count(path: str | Path) -> int:
    """Probe the video's frame count via ffprobe (nb_read_packets)."""
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_packets",
        "-show_entries",
        "stream=nb_read_packets",
        "-of",
        "csv=p=0",
        str(path),
    ]
    out = subprocess.run(argv, capture_output=True, check=True)  # noqa: S603
    return int(out.stdout.decode().strip())


def _read_file_bytes(path: str | Path) -> bytes:
    """Read *path* off disk (default ``read_bytes`` seam)."""
    return Path(path).read_bytes()


@dataclass
class InterpolateStage:
    """A Stage that raises the clip's frame rate to ``target_fps``.

    Attributes:
        engine: Configured InterpolatorEngine (already provisioned).
        target_fps: Requested output frame rate.
        instance: Compute instance passed to the engine; None for local engines.
        cfg: Runtime config dict the engine interprets.
        cancel_token: Threaded through to ``engine.interpolate``.
        probe_fps: Injectable ``(path) -> fps`` seam (tests override).
        probe_count: Injectable ``(path) -> frame count`` seam.
        decimate: Injectable ``(bytes, fps) -> bytes`` re-timing seam.
        read_bytes: Injectable ``(path) -> bytes`` reader seam.
        publish: ``(bytes) -> uri`` sink for a locally-decimated artifact.
    """

    engine: InterpolatorEngine
    target_fps: float
    instance: Instance | None
    cfg: dict[str, Any]
    cancel_token: CancelToken | None = None
    probe_fps: Callable[[str | Path], float] = ffprobe_fps
    probe_count: Callable[[str | Path], int] = _default_count
    decimate: Callable[[bytes, float], bytes] = decimate_video_fps
    read_bytes: Callable[[str | Path], bytes] = _read_file_bytes
    publish: Callable[[bytes], str] | None = None

    def run(self, state: PipelineState) -> PipelineState:
        """Run interpolation, returning a new state with ``interpolated`` set."""
        clip = state.artifacts["clip"]
        local = self._local_path(clip)
        if local is not None:
            interpolated = self._run_local(clip, local)
        else:
            interpolated = self._run_engine(clip)
        new_artifacts = dict(state.artifacts)
        new_artifacts["interpolated"] = interpolated
        return replace(state, artifacts=new_artifacts)

    def _local_path(self, clip: Artifact) -> str | None:
        uri = clip.uri
        if uri.startswith("file://"):
            return uri.removeprefix("file://")
        if uri.startswith("/"):
            return uri
        return None

    def _run_local(self, clip: Artifact, path: str) -> Artifact:
        source_fps = self.probe_fps(path)
        count = self.probe_count(path)
        plan = resolve_fps_target(
            source_fps,
            self.target_fps,
            self.engine.capability,
            source_frame_count=count,
        )
        if plan.skip_gpu:
            if plan.decimate_to is None:
                return clip  # passthrough (equal fps)
            out = self.decimate(self.read_bytes(path), plan.decimate_to)
            if self.publish is None:
                raise ValueError("InterpolateStage needs a publish seam to decimate")
            return replace(clip, uri=self.publish(out))
        return self._run_engine(clip)

    def _run_engine(self, clip: Artifact) -> Artifact:
        job = InterpolateJob(source=clip, target_fps=self.target_fps)
        result = self.engine.interpolate(
            self.instance, job, self.cfg, cancel_token=self.cancel_token
        )
        return result.artifact
