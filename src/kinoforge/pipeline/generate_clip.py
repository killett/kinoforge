"""GenerateClipStage: the single-clip happy path that proves the seam.

Dispatches through a BackendPool, persists the resulting clip Artifact into an
ArtifactStore, and returns an updated PipelineState.

``segments`` is now a constructor field — the orchestrator (and batch) populate
it from the splitter output before constructing the stage. Validation runs
upstream (orchestrator.validate_request); the stage body assumes
``state.request`` is already validated.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from kinoforge.core.continuity import inject_tail_frame
from kinoforge.core.interfaces import (
    MODE_ROLE_REQUIREMENTS,
    BackendPool,
    ConditioningAsset,
    GenerationEngine,
    ModelProfile,
    PipelineState,
    Segment,
)
from kinoforge.core.strategy import decide
from kinoforge.outputs.base import OutputSink
from kinoforge.pipeline.artifact_bytes import artifact_bytes
from kinoforge.stores.base import ArtifactStore


@dataclass
class GenerateClipStage:
    """Single-clip pipeline stage (Layer R: PipelineState in, PipelineState out).

    Attributes:
        profile: The model's cached capability profile.
        pool: A BackendPool (e.g. SequentialPool with one backend).
        store: The ArtifactStore destination for the produced clip.
        run_id: Namespace for outputs in the store.
        accepted_kinds: Asset kinds the underlying engine accepts.
        base_params: Engine-neutral params for every produced job.
        base_spec: Engine-interpreted spec template merged into every job.
        engine: The GenerationEngine providing extract_last_frame for continuity chaining.
        segments: Ordered segment list produced by the splitter — always
            populated by the orchestrator before construction.
        http_get_bytes: Optional injectable seam for http(s) artifact downloads.
            When ``None`` (the default), :func:`_default_http_get_bytes` is used,
            which builds a :class:`urllib.request.Request` with the artifact's
            headers and reads via ``urlopen``.  Override in tests to avoid real
            network I/O while still exercising the full pipeline path.
        sink: Optional user-facing publish target.  When ``None`` (the
            default) the stage behaves identically to pre-Layer-O —
            ``store.put_bytes`` is the only persistence side effect.
            When non-None, the stage calls ``sink.publish(payload,
            prompt=segments[-1].prompt, extension=ext,
            namespace=self.namespace)`` after ``store.put_bytes`` returns.
        namespace: Optional sub-directory grouping for the sink, used by
            ``batch_generate`` to namespace per-batch publishes under
            ``<output_dir>/<batch_id>/``.
    """

    profile: ModelProfile
    pool: BackendPool
    store: ArtifactStore
    run_id: str
    accepted_kinds: set[str]
    base_params: dict  # type: ignore[type-arg]
    base_spec: dict  # type: ignore[type-arg]
    engine: GenerationEngine
    segments: list[Segment]  # NEW — always populated by orchestrator
    http_get_bytes: Callable[[str, dict[str, str]], bytes] | None = None
    sink: OutputSink | None = None
    namespace: str | None = None

    def run(self, state: PipelineState) -> PipelineState:
        """Dispatch jobs through pool, persist clip artifact, return updated state.

        Validation runs upstream (orchestrator.validate_request); body assumes
        ``state.request`` is already validated and ``self.segments`` is the
        ordered segment list produced by the splitter.

        Args:
            state: The current pipeline state carrying the validated request.

        Returns:
            Updated :class:`~kinoforge.core.interfaces.PipelineState` with
            ``state.artifacts["clip"]`` set to the persisted Artifact.
        """
        request = state.request
        jobs = decide(self.profile, self.segments, self.base_params, self.base_spec)

        # Validate every job's spec ONCE, before any dispatch.  Previously
        # this only ran inside the chained branch (i > 0) and the fan-out
        # branch skipped it entirely, so the first job and any t2v
        # non-chained fan-out job dispatched without spec validation.
        # Layer K Task 2 fix: every real job is validated up front so the
        # orchestrator's try/except ValidationError wrapper can tear down
        # compute before any backend.submit() wire I/O.
        for job in jobs:
            self.engine.validate_spec(job)

        # Continuity: for modes whose role contract accepts init_image (today
        # i2v only), thread each rendered tail-frame into the next segment's
        # init_image slot. Stitching across the N artifacts is DEFERRED to its
        # own follow-up; we still persist only the last artifact below.
        should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, {})
        if not should_chain and len(jobs) > 1:
            # Layer G: t2v non-chained fallback fans out via pool.map.
            # Chained continuity (i2v) and trivial 1-job paths take the
            # serial loop below.
            results = list(self.pool.map(jobs))
        else:
            results = []
            for i, job in enumerate(jobs):
                if i > 0 and should_chain:
                    tail_bytes = self.engine.extract_last_frame(results[-1])
                    tail_name = f"seg-{i - 1}-tail.png"
                    stored_tail = self.store.put_bytes(
                        self.run_id, tail_name, tail_bytes
                    )
                    # Stores return Artifact(uri=...) with filename=""; pre-populate
                    # filename so downstream consumers don't have to derive it from
                    # Path(ref.uri).name.
                    tail_artifact = replace(stored_tail, filename=tail_name)
                    tail_asset = ConditioningAsset(
                        kind="image",
                        role="init_image",
                        ref=tail_artifact,
                    )
                    job = inject_tail_frame(job, tail_asset)
                    # Layer F: validate the now-asset-bearing job against the
                    # engine's spec contract (e.g. asset_node_ids / asset_paths)
                    # before the engine HTTP round-trip. The orchestrator's
                    # pre-dispatch validate_request saw seg-0's spec only; it
                    # didn't know about the tail-frame asset injected here.
                    self.engine.validate_spec(job)
                art = self.pool.submit(job).result()
                results.append(art)
        last = results[-1]

        # Persist the bytes derived from the engine's Artifact.
        payload = artifact_bytes(last, self.http_get_bytes)
        stored = self.store.put_bytes(self.run_id, last.filename, payload)

        # Layer O — also publish to the user-facing sink if one is wired.
        # Read prompt from the LAST segment so chained continuity (i2v)
        # uses the final segment's prompt when it eventually grows past
        # the seg-0-only case; today single-segment is the only path
        # that publishes anything meaningful, so this is also correct
        # for it.
        if self.sink is not None:
            ext = Path(last.filename).suffix or ".bin"
            self.sink.publish(
                payload,
                prompt=self.segments[-1].prompt,
                extension=ext,
                namespace=self.namespace,
            )

        return replace(
            state,
            artifacts={**state.artifacts, "clip": stored},
        )
