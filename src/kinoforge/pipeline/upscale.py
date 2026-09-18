"""UpscaleStage — PipelineState in, PipelineState out.

Reads ``state.artifacts["clip"]``, invokes the configured ``UpscalerEngine``,
writes ``state.artifacts["upscaled"]``. A height target (``ScaleTarget(kind=
"height")``) is resolved here to a concrete factor plus an optional
``downscale_to`` stashed on the upscaled artifact's ``.meta`` for the orchestrator
materialize boundary to apply. Engines only ever receive ``kind="factor"``.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import ScaleUnsatisfiableError
from kinoforge.core.frames import _default_run, ffprobe_dims
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
from kinoforge.pipeline.chunk import (
    concat_argv,
    ffprobe_frames,
    plan_chunks,
    split_argv,
)

_log = logging.getLogger("kinoforge.pipeline.upscale")


def _default_fetch(url: str) -> bytes:
    """GET a pod-served upscaled chunk (``{base}/artifacts/<name>``)."""
    req = urllib.request.Request(  # noqa: S310 — pod proxy URL only
        url, headers={"User-Agent": "kinoforge-upscale-stage/0.1"}
    )
    with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310
        return bytes(resp.read())


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
        chunk_frames: Frames each temporal chunk keeps; ``None`` disables
            chunking (the single-call path). See :mod:`kinoforge.pipeline.chunk`.
        chunk_overlap: Warm-up frames rendered before each chunk and discarded.
        ffmpeg_run: Injectable ffmpeg seam ``(argv, stdin) -> stdout`` for the split
            and join steps.
        fetch: Injectable ``(url) -> bytes`` seam for pulling each upscaled
            chunk off the pod.
        probe_frames: Injectable ``(path) -> frame count`` seam.
        work_dir: Directory for chunk intermediates; a fresh temp dir when None.
    """

    engine: UpscalerEngine
    scale: ScaleTarget
    instance: Instance | None
    cfg: dict[str, Any]
    cancel_token: CancelToken | None = None
    probe_dims: Callable[[str | Path], tuple[int, int]] = ffprobe_dims
    chunk_frames: int | None = None
    chunk_overlap: int = 8
    ffmpeg_run: Callable[[list[str], bytes], bytes] = _default_run
    fetch: Callable[[str], bytes] = _default_fetch
    probe_frames: Callable[[str | Path], int] = ffprobe_frames
    work_dir: Path | None = None

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
        """Invoke the engine at a concrete factor scale, chunking when configured.

        Raises:
            ValueError: Chunking is on but the source is not a local file.
        """
        if self.chunk_frames is None:
            return self._engine_call(clip, scale)
        local = self._local_path(clip)
        if local is None:
            raise ValueError(
                f"chunked upscale needs a local source clip; got {clip.uri!r}"
            )
        total = self.probe_frames(local)
        specs = plan_chunks(
            total, chunk_frames=self.chunk_frames, overlap=self.chunk_overlap
        )
        if len(specs) == 1:
            return self._engine_call(clip, scale)
        return self._run_chunked(local, specs, scale)

    def _engine_call(self, clip: Artifact, scale: ScaleTarget) -> UpscaleResult:
        job = UpscaleJob(source=clip, scale=scale)
        return self.engine.upscale(
            self.instance, job, self.cfg, cancel_token=self.cancel_token
        )

    def _run_chunked(
        self, local: Path, specs: list[Any], scale: ScaleTarget
    ) -> UpscaleResult:
        """Split → upscale each chunk on the same pod → fetch → trim+join."""
        work = self.work_dir or Path(tempfile.mkdtemp(prefix="kinoforge-chunks-"))
        work.mkdir(parents=True, exist_ok=True)
        parts: list[tuple[str, int]] = []
        elapsed = 0.0
        first: UpscaleResult | None = None
        chunk_meta: list[dict[str, Any]] = []
        for i, spec in enumerate(specs):
            _log.info(
                "upscale chunk %d/%d: frames %d-%d (trim %d)",
                i + 1,
                len(specs),
                spec.start,
                spec.start + spec.count,
                spec.trim_head,
            )
            chunk_path = work / f"chunk{i:03d}.mp4"
            self.ffmpeg_run(split_argv(str(local), spec, str(chunk_path)), b"")
            result = self._engine_call(Artifact(uri=f"file://{chunk_path}"), scale)
            # Same seekable-temp-file pattern as pipeline/downscale.py: an
            # mp4's moov atom lives at the tail, so ffmpeg needs a real file.
            with tempfile.NamedTemporaryFile(
                dir=work, prefix=f"up{i:03d}-", suffix=".mp4", delete=False
            ) as tf:
                tf.write(self.fetch(result.artifact.uri))
                up_path = tf.name
            parts.append((up_path, spec.trim_head))
            elapsed += result.elapsed_s
            chunk_meta.append(dict(result.engine_meta))
            first = first or result
        assert first is not None  # noqa: S101 — len(specs) >= 2 here
        joined = work / "joined.mp4"
        _log.info("joining %d upscaled chunks -> %s", len(parts), joined)
        self.ffmpeg_run(concat_argv(parts, str(joined)), b"")
        body = joined.read_bytes()
        return UpscaleResult(
            artifact=Artifact(
                uri=f"file://{joined}",
                sha256=hashlib.sha256(body).hexdigest(),
                size=len(body),
                meta={"chunks": len(specs), "materialize": True},
            ),
            input_resolution=first.input_resolution,
            output_resolution=first.output_resolution,
            elapsed_s=elapsed,
            engine_meta={"chunks": len(specs), "chunk_meta": chunk_meta},
        )

    @staticmethod
    def _local_path(clip: Artifact) -> Path | None:
        """Local filesystem path of *clip*, or None for a remote source."""
        uri = clip.uri
        if uri.startswith("file://"):
            return Path(uri.removeprefix("file://"))
        if uri.startswith("/"):
            return Path(uri)
        return None

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
