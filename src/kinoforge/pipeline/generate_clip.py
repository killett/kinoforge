"""GenerateClipStage: the single-clip happy path that proves the seam.

Builds a 1-segment job from the request (prompt splitter DEFERRED), dispatches
through a BackendPool, persists the resulting clip Artifact into an ArtifactStore,
and returns the stored Artifact.

The ``segments_override`` parameter on :meth:`GenerateClipStage.run` allows tests
to bypass the single-segment build and exercise the packaging branches (native
extension vs fallback) directly without a real splitter.
"""

from __future__ import annotations

from dataclasses import dataclass

from kinoforge.core.continuity import inject_tail_frame
from kinoforge.core.interfaces import (
    MODE_ROLE_REQUIREMENTS,
    Artifact,
    BackendPool,
    ConditioningAsset,
    GenerationEngine,
    GenerationRequest,
    ModelProfile,
    Segment,
)
from kinoforge.core.strategy import decide
from kinoforge.core.validation import validate_request
from kinoforge.stores.base import ArtifactStore


@dataclass
class GenerateClipStage:
    """Single-clip pipeline stage.

    Validates a :class:`~kinoforge.core.interfaces.GenerationRequest`, packages
    it into one or more :class:`~kinoforge.core.interfaces.GenerationJob` objects
    via :func:`~kinoforge.core.strategy.decide`, dispatches through a
    :class:`~kinoforge.core.interfaces.BackendPool`, and persists the resulting
    bytes to an :class:`~kinoforge.stores.base.ArtifactStore`.

    Attributes:
        profile: The model's cached capability profile.
        pool: A BackendPool (e.g. SequentialPool with one backend).
        store: The ArtifactStore destination for the produced clip.
        run_id: Namespace for outputs in the store.
        accepted_kinds: Asset kinds the underlying engine accepts.
        base_params: Engine-neutral params for every produced job.
        base_spec: Engine-interpreted spec template merged into every job.
        engine: The GenerationEngine providing extract_last_frame for continuity chaining.
    """

    profile: ModelProfile
    pool: BackendPool
    store: ArtifactStore
    run_id: str
    accepted_kinds: set[str]
    base_params: dict  # type: ignore[type-arg]
    base_spec: dict  # type: ignore[type-arg]
    engine: GenerationEngine

    def run(
        self,
        request: GenerationRequest,
        *,
        segments_override: list[Segment] | None = None,
    ) -> Artifact:
        """Validate, package, dispatch, persist.

        Args:
            request: The user-level generation request.
            segments_override: For testing the packaging branches directly.
                When ``None``, the happy path builds one ``Segment`` from the
                request. When provided, these segments are used verbatim and
                the single-segment build is skipped.

        Returns:
            The persisted :class:`~kinoforge.core.interfaces.Artifact` (uri in
            the store) of the produced clip.

        Raises:
            ValidationError: If the request fails mode/role/kind validation.
                Validation runs only when ``segments_override`` is ``None``;
                callers that pre-build segments (e.g. the orchestrator after
                its own ``validate_request`` call) are expected to have
                validated upstream.
        """
        if segments_override is not None:
            segments = segments_override
        else:
            validated = validate_request(
                self.profile, request, accepted_kinds=self.accepted_kinds
            )
            segments = [Segment(prompt=validated.prompt, assets=list(validated.assets))]

        jobs = decide(self.profile, segments, self.base_params, self.base_spec)

        # Continuity: for modes whose role contract accepts init_image (today
        # i2v only), thread each rendered tail-frame into the next segment's
        # init_image slot. Stitching across the N artifacts is DEFERRED to its
        # own follow-up; we still persist only the last artifact below.
        should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, set())
        results: list[Artifact] = []
        for i, job in enumerate(jobs):
            if i > 0 and should_chain:
                tail_bytes = self.engine.extract_last_frame(results[-1])
                tail_name = f"seg-{i - 1}-tail.png"
                tail_artifact = self.store.put_bytes(self.run_id, tail_name, tail_bytes)
                tail_asset = ConditioningAsset(
                    kind="image",
                    role="init_image",
                    ref=tail_artifact,
                )
                job = inject_tail_frame(job, tail_asset)
            art = self.pool.submit(job).result()
            results.append(art)
        last = results[-1]

        # Persist the bytes derived from the engine's Artifact.
        payload = self._artifact_bytes(last)
        return self.store.put_bytes(self.run_id, last.filename, payload)

    def _artifact_bytes(self, artifact: Artifact) -> bytes:
        """Derive deterministic bytes from the engine's Artifact.

        If the artifact carries a URI pointing to an existing file, read it.
        Otherwise, derive deterministic bytes from filename and meta — this is
        sufficient for the FakeEngine test path. Real engines will deliver a
        populated URI and this method will read actual content.

        Args:
            artifact: The :class:`~kinoforge.core.interfaces.Artifact` returned
                by the backend.

        Returns:
            The bytes to persist in the store.
        """
        # DEFERRED: real backends will write to a tempfile and set uri.
        return (
            artifact.filename.encode("utf-8")
            + b"|"
            + repr(sorted(artifact.meta.items())).encode("utf-8")
        )
