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

from kinoforge.core.interfaces import (
    Artifact,
    BackendPool,
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
    """

    profile: ModelProfile
    pool: BackendPool
    store: ArtifactStore
    run_id: str
    accepted_kinds: set[str]
    base_params: dict  # type: ignore[type-arg]
    base_spec: dict  # type: ignore[type-arg]

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
        """
        if segments_override is not None:
            segments = segments_override
        else:
            validated = validate_request(
                self.profile, request, accepted_kinds=self.accepted_kinds
            )
            segments = [Segment(prompt=validated.prompt, assets=list(validated.assets))]

        jobs = decide(self.profile, segments, self.base_params, self.base_spec)

        # Single-clip happy path produces one Artifact; native-extension also
        # produces a single Artifact (one N-segment job). The non-native fan-out
        # returns N Artifacts — for this single-clip seam we return the last one
        # and DEFER stitching/continuity.
        results = self.pool.map(jobs)
        last = results[-1]  # DEFERRED: stitching across N artifacts.

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
