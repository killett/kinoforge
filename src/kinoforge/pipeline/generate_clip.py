"""GenerateClipStage: the single-clip happy path that proves the seam.

Builds a 1-segment job from the request (prompt splitter DEFERRED), dispatches
through a BackendPool, persists the resulting clip Artifact into an ArtifactStore,
and returns the stored Artifact.

The ``segments_override`` parameter on :meth:`GenerateClipStage.run` allows tests
to bypass the single-segment build and exercise the packaging branches (native
extension vs fallback) directly without a real splitter.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

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
        should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, set())
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
                    stored = self.store.put_bytes(self.run_id, tail_name, tail_bytes)
                    # Stores return Artifact(uri=...) with filename=""; pre-populate
                    # filename so downstream consumers don't have to derive it from
                    # Path(ref.uri).name.
                    tail_artifact = replace(stored, filename=tail_name)
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
        payload = self._artifact_bytes(last)
        return self.store.put_bytes(self.run_id, last.filename, payload)

    def _artifact_bytes(self, artifact: Artifact) -> bytes:
        """Derive bytes from the engine's Artifact.

        Resolution order:

        1. ``artifact.uri`` — a ``file://`` URI or local path written by a
           local engine.  Read the file directly.
        2. ``artifact.url`` — an http(s) URL returned by a hosted/queue engine
           (e.g. fal.ai's signed media URL).  Download it.
        3. Fallback: synthesize deterministic bytes from ``filename + meta``
           so FakeEngine-driven unit tests keep working without HTTP.

        Args:
            artifact: The :class:`~kinoforge.core.interfaces.Artifact` returned
                by the backend.

        Returns:
            The bytes to persist in the store.
        """
        import urllib.parse
        import urllib.request
        from pathlib import Path

        uri = (artifact.uri or "").strip()
        if uri:
            parsed = urllib.parse.urlparse(uri)
            local_path: str | None = None
            if parsed.scheme == "file":
                local_path = urllib.request.url2pathname(parsed.path)
            elif parsed.scheme == "" and uri:
                local_path = uri
            if local_path is not None:
                candidate = Path(local_path)
                if candidate.exists():
                    return candidate.read_bytes()

        url = (artifact.url or "").strip()
        if url.startswith(("http://", "https://")):
            with urllib.request.urlopen(url) as resp:  # noqa: S310
                downloaded: bytes = resp.read()
            return downloaded

        # Synthetic fallback retained for FakeEngine-driven tests that
        # exercise the pipeline without a real backend.
        return (
            artifact.filename.encode("utf-8")
            + b"|"
            + repr(sorted(artifact.meta.items())).encode("utf-8")
        )
