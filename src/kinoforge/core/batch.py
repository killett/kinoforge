"""Batch generation: manifest dispatch (Layer L).

This module owns:
  * load_manifest() — reads YAML, resolves prompt_file paths, auto-indexes
    run_ids, returns a fully validated BatchManifest.
  * batch_generate() — the orchestration entry point that wraps
    deploy_session, fans entries out via ThreadPoolExecutor, and writes
    _batch_summary.json on every exit path.

The four batch dataclasses (BatchEntry, BatchManifest, BatchOutcome,
BatchResult) now live in core/batch_models.py and are re-exported from
this module for backward compatibility with existing import sites.

Core-import-ban: this module imports ONLY from kinoforge.core.* +
kinoforge.pipeline.* + kinoforge.stores.base + kinoforge.outputs.base +
stdlib + pydantic + PyYAML.  No kinoforge.providers / engines / sources.
The invariant test in tests/test_core_invariant.py enforces this via
subprocess isolation.
"""

from __future__ import annotations

import copy
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

import yaml

from kinoforge.core import registry
from kinoforge.core.batch_events import (
    BatchEvent,
    BatchEventCallback,
    _LockedEmitter,
)
from kinoforge.core.batch_models import (
    BatchEntry,
    BatchManifest,
    BatchOutcome,
    BatchResult,
)
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.errors import (
    BudgetExceeded,
    CapabilityMismatch,
    ConfigError,
    ProfileNotCached,
    TeardownError,
)
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationRequest,
    ImageBackend,
    ImageEngine,
    ImageProfile,
    ImageProfileProvider,
    Instance,
    PipelineState,
    Segment,
)
from kinoforge.core.logging import get_logger
from kinoforge.core.orchestrator import deploy_session
from kinoforge.core.profiles import JsonImageProfileCache
from kinoforge.core.validation import validate_request
from kinoforge.outputs.base import OutputSink
from kinoforge.pipeline.generate_clip import GenerateClipStage
from kinoforge.pipeline.keyframe import KeyframeStage
from kinoforge.stores.base import ArtifactStore

if TYPE_CHECKING:
    from kinoforge.core.cancel import CancelToken
    from kinoforge.core.config import Config, KeyframeConfig
    from kinoforge.core.interfaces import (
        ComputeProvider,
        CredentialProvider,
        GenerationEngine,
        ModelProfileProvider,
    )
    from kinoforge.core.orchestrator import DeploySession

_log = get_logger(__name__)

__all__ = [
    "BatchEntry",
    "BatchManifest",
    "BatchOutcome",
    "BatchResult",
    "batch_generate",
    "load_manifest",
]


def load_manifest(path: Path) -> BatchManifest:
    """Load and fully validate a batch manifest YAML.

    Performs:
      1. YAML parse (top-level must be a list).
      2. pydantic validation (extra="forbid", per-entry exactly-one
         prompt source, manifest-level run_id uniqueness).
      3. prompt_file resolution against ``path.parent`` + content read
         + ``.strip()`` of trailing whitespace.
      4. Auto-indexing of any entry that didn't set run_id.

    After this returns, every entry has ``prompt is not None``,
    ``prompt_file is None``, ``run_id is not None``.

    Args:
        path: Filesystem path to the manifest YAML.

    Returns:
        A fully validated BatchManifest ready for batch_generate().

    Raises:
        ConfigError: Manifest isn't a YAML list, or a prompt_file is missing.
        pydantic.ValidationError: Schema / per-entry / manifest-level rules.
    """
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"manifest YAML parse error: {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise ConfigError("manifest top-level must be a YAML list of entries")
    manifest = BatchManifest(entries=raw)

    base = path.parent
    for entry in manifest.entries:
        if entry.prompt_file is not None:
            resolved = (base / entry.prompt_file).resolve()
            if not resolved.is_file():
                raise ConfigError(
                    f"prompt_file not found: {resolved} (entry mode={entry.mode!r})"
                )
            entry.prompt = resolved.read_text().strip()
            entry.prompt_file = None

    for idx, entry in enumerate(manifest.entries):
        if entry.run_id is None:
            entry.run_id = str(idx)

    return manifest


def _build_stage_for_entry(
    cfg: Config,
    entry: BatchEntry,
    session: DeploySession,
    accepted_kinds: set[str],
    store: ArtifactStore,
    batch_id: str,
    sink: OutputSink | None = None,
    keyframe_state: PipelineState | None = None,
    cancel_token: CancelToken | None = None,
) -> tuple[GenerateClipStage, PipelineState]:
    """Build a stage + initial PipelineState pair for one batch entry.

    Deep-copies cfg.params / cfg.spec so neither cfg nor any sibling
    entry's stage shares a mutable reference with this entry's
    base_params / base_spec.  A shallow ``dict(cfg.params)`` would
    keep nested-dict identity, letting a deliberately-bad-citizen
    engine that does ``job.params["nested"]["a"] = 99`` corrupt
    ``cfg.params`` in place.  Entry-side overrides are deep-copied too
    so the same protection applies to per-entry nested dicts.

    When ``keyframe_state`` is supplied, its ``request`` (which carries
    the keyframe-filled assets) is used as the base for validation
    instead of the raw entry request.  This is the Layer R T11 shape:
    KeyframeStage runs BEFORE ``_build_stage_for_entry`` so that
    ``validate_request`` sees the enriched (filled) request.

    Args:
        cfg: The loaded kinoforge configuration.
        entry: The batch entry being scheduled.
        session: The active deploy_session yielding profile / pool /
            engine.
        accepted_kinds: Asset kinds the underlying engine accepts.
        store: Destination ArtifactStore for the entry's outputs.
        batch_id: Top-level namespace for this batch's artifacts; used
            to build the entry-scoped ``run_id`` and as the
            ``namespace`` forwarded to the stage's sink.
        sink: Optional user-facing publish target forwarded to each
            per-entry :class:`~kinoforge.pipeline.generate_clip.GenerateClipStage`.
            When ``None`` (the default), no publish side-effect occurs.
        keyframe_state: Optional pre-run KeyframeStage output.  When
            non-None, its ``request`` (assets already filled) is
            validated instead of the bare entry request.  Its
            ``artifacts`` are carried forward into the initial
            PipelineState so keyframe artifacts survive downstream.
        cancel_token: Phase 50 cooperative-cancellation token forwarded
            verbatim into the built :class:`GenerateClipStage` so its
            ``pool.submit`` calls observe the operator's interrupt.
            ``None`` (the default) preserves library-caller behavior.

    Returns:
        A ``(stage, initial_state)`` tuple ready for
        ``executor.submit(_run_with_clock, stage, initial_state, ...)``.
        The state carries the validated request; the stage carries the
        ordered segment list produced by the splitter.
    """
    merged_params = {
        **copy.deepcopy(dict(cfg.params)),
        **copy.deepcopy(entry.params or {}),
    }
    merged_spec = {
        **copy.deepcopy(dict(cfg.spec)),
        **copy.deepcopy(entry.spec or {}),
    }

    if keyframe_state is not None:
        # KeyframeStage already ran; use its enriched request directly.
        request_to_validate = keyframe_state.request
        prior_artifacts = dict(keyframe_state.artifacts)
    else:
        request_to_validate = GenerationRequest(
            prompt=entry.prompt or "",
            mode=entry.mode,
            assets=[ConditioningAsset(**a) for a in (entry.assets or [])],
        )
        prior_artifacts = {}

    validated = validate_request(
        session.profile, request_to_validate, accepted_kinds=accepted_kinds
    )

    splitter = registry.get_splitter(cfg.splitter.kind)()
    prompt_segments: list[Segment] = splitter.split(
        validated.prompt, session.profile, {}
    )
    # Attach assets to segment 0 only.  Continuity fills 1..N-1.
    if prompt_segments and validated.assets:
        prompt_segments[0] = replace(prompt_segments[0], assets=list(validated.assets))

    entry_run_id = f"{batch_id}/{entry.run_id}"
    # Layer 4: provider + model are read off the active engine + merged spec
    # for the OutputSink filename schema. Each entry can override spec.model
    # so we read the merged value, not cfg.spec.
    _provider = getattr(session.engine, "name", None) or None
    _model = str(merged_spec.get("model", "") or "") or None
    stage = GenerateClipStage(
        profile=session.profile,
        pool=session.pool,
        store=store,
        run_id=entry_run_id,
        accepted_kinds=accepted_kinds,
        base_params=merged_params,
        base_spec=merged_spec,
        engine=session.engine,
        segments=prompt_segments,
        sink=sink,
        namespace=batch_id,
        provider=_provider,
        model=_model,
        cancel_token=cancel_token,
    )
    initial_state = PipelineState(request=validated, artifacts=prior_artifacts)
    return stage, initial_state


def _run_with_clock(
    stage: GenerateClipStage,
    initial_state: PipelineState,
    start_times: dict[int, float],
    idx: int,
    *,
    emit: _LockedEmitter,
    entry: BatchEntry,
    batch_id: str,
) -> Artifact:
    """Stamp the real stage-run start time, fire ``entry_start``, then run the stage.

    Recording ``monotonic()`` before ``executor.submit`` would
    conflate queue-wait time with the stage's real wall-clock cost —
    a 5-entry batch with ``concurrent=1`` would report 5x inflated
    durations for the last entries.  Stamping here, inside the worker
    thread, gives ``BatchOutcome.duration_s`` the actual stage cost.

    The ``entry_start`` event fires after the timestamp stamp and
    before ``stage.run`` so that ``_LockedEmitter._started_idxs``
    correctly reflects which entries have begun execution by the time
    ``_mark_remaining_after_fatal`` runs (it relies on the populated
    set to distinguish in-flight from never-started cancellations).

    Args:
        stage: The pre-built GenerateClipStage for this entry.
        initial_state: The initial PipelineState (validated request,
            empty artifacts) for this entry.
        start_times: Shared dict keyed by entry index; this worker
            writes its slot before doing real work.
        idx: The entry's position in ``manifest.entries``.
        emit: The locked emitter used to fire streaming events.
        entry: The batch entry being processed (for the entry_start
            event payload).
        batch_id: The batch's top-level namespace ID.

    Returns:
        The persisted :class:`~kinoforge.core.interfaces.Artifact`
        extracted from ``state.artifacts["clip"]`` after the stage runs.
        Side-effect: fires one ``entry_start`` event via ``emit`` before
        delegating to ``stage.run``.
    """
    start_times[idx] = monotonic()
    emit(
        BatchEvent(
            kind="entry_start",
            batch_id=batch_id,
            idx=idx,
            run_id=entry.run_id or str(idx),
            ts=datetime.now(),
            entry=entry,
        )
    )
    out_state = stage.run(initial_state)
    return out_state.artifacts["clip"]


def _mark_remaining_after_fatal(
    future_to_idx: dict[Future[Any], int],
    outcomes_by_idx: dict[int, BatchOutcome],
    manifest: BatchManifest,
    start_times: dict[int, float],
    batch_start: float,
    *,
    emit: _LockedEmitter,
    batch_id: str,
    fatal_type: str,
) -> None:
    """Cancel queued futures and label them aborted/interrupted in-place.

    Called after a batch-fatal exception is observed on one future;
    drains the rest into ``outcomes_by_idx`` so the summary is
    complete before the fatal re-raises.

    Args:
        future_to_idx: Map of submitted futures to entry index.
        outcomes_by_idx: Per-entry outcomes being assembled.  This
            function mutates it in place.
        manifest: The original manifest, used to recover ``run_id`` for
            any entry that didn't get a ``BatchOutcome`` yet.
        start_times: Per-index actual stage-start monotonic stamps;
            entries that never started fall back to ``batch_start``
            so ``duration_s`` is 0 instead of negative or unbounded.
        batch_start: The monotonic timestamp captured at batch entry
            into the dispatch loop.
        emit: The locked emitter used to fire streaming events.
        batch_id: The batch's top-level namespace ID.
        fatal_type: The class name of the fatal exception, used in
            the error message of aborted/interrupted events.
    """
    abort_error = f"batch aborted by {fatal_type}"
    for other_fut, other_idx in future_to_idx.items():
        if other_idx in outcomes_by_idx:
            continue
        other_entry = manifest.entries[other_idx]
        if other_fut.cancel():
            # Never-started: emit synthetic start + finish(aborted).
            if not emit.has_started(other_idx):
                emit(
                    BatchEvent(
                        kind="entry_start",
                        batch_id=batch_id,
                        idx=other_idx,
                        run_id=other_entry.run_id or str(other_idx),
                        ts=datetime.now(),
                        entry=other_entry,
                    )
                )
            emit(
                BatchEvent(
                    kind="entry_finish",
                    batch_id=batch_id,
                    idx=other_idx,
                    run_id=other_entry.run_id or str(other_idx),
                    ts=datetime.now(),
                    status="aborted",
                    duration_s=0.0,
                    error=abort_error,
                )
            )
            outcomes_by_idx[other_idx] = BatchOutcome(
                run_id=other_entry.run_id or str(other_idx),
                status="aborted",
                duration_s=0.0,
                error=abort_error,
            )
        else:
            # An in-flight future cannot be cancelled; record how long
            # it has been running.  Fall back to batch_start when the
            # worker hadn't yet hit _run_with_clock — that means the
            # future was scheduled but never woke up before the fatal
            # took over, so duration is effectively 0.
            other_duration = monotonic() - start_times.get(other_idx, batch_start)
            # Only emit entry_finish; entry_start was already emitted by
            # the worker thread when it started running.
            emit(
                BatchEvent(
                    kind="entry_finish",
                    batch_id=batch_id,
                    idx=other_idx,
                    run_id=other_entry.run_id or str(other_idx),
                    ts=datetime.now(),
                    status="interrupted",
                    duration_s=other_duration,
                    error=abort_error,
                )
            )
            outcomes_by_idx[other_idx] = BatchOutcome(
                run_id=other_entry.run_id or str(other_idx),
                status="interrupted",
                duration_s=other_duration,
                error=abort_error,
            )


def _finalize_summary(
    manifest: BatchManifest,
    outcomes_by_idx: dict[int, BatchOutcome],
    batch_id: str,
    started_at: str,
) -> BatchResult:
    """Pad missing outcomes as ``"aborted"``, timestamp, and assemble.

    Any entry index missing from ``outcomes_by_idx`` (e.g. a future
    that was cancelled before any worker observed it, or one we never
    got to submit because deploy_session raised mid-loop) is recorded
    as ``"aborted"`` with no duration.

    Args:
        manifest: The original manifest, used to recover ``run_id``.
        outcomes_by_idx: Per-entry outcomes collected so far.
        batch_id: The batch's top-level namespace id.
        started_at: ISO timestamp captured before any per-entry work.

    Returns:
        A :class:`BatchResult` with outcomes in submission order and
        a freshly captured ``finished_at`` ISO timestamp.
    """
    ordered = [
        outcomes_by_idx.get(
            i,
            BatchOutcome(
                run_id=manifest.entries[i].run_id or str(i),
                status="aborted",
            ),
        )
        for i in range(len(manifest.entries))
    ]
    finished_at = datetime.now().isoformat(timespec="seconds")
    return BatchResult(
        batch_id=batch_id,
        started_at=started_at,
        finished_at=finished_at,
        outcomes=ordered,
    )


def batch_generate(
    cfg: Config,
    manifest: BatchManifest,
    *,
    store: ArtifactStore,
    batch_id: str,
    concurrent: int | None = None,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    image_engine: ImageEngine | None = None,
    creds: CredentialProvider | None = None,
    profile_provider: ModelProfileProvider | None = None,
    image_profile_provider: ImageProfileProvider | None = None,
    state_dir: Path = Path(".kinoforge"),
    sink: OutputSink | None = None,
    instance: Instance | None = None,
    tags: dict[str, str] | None = None,
    on_event: BatchEventCallback | None = None,
    cancel_token: CancelToken | None = None,
) -> BatchResult:
    """Run every entry in *manifest* on one shared deployed instance.

    Lifecycle:

    1. Open ``deploy_session(cfg, ...)`` — sets up backend, profile,
       pool, optional instance.  Discover runs once on cold cache;
       verify runs once on warm cache.
    2. For each entry, build a per-entry
       :class:`~kinoforge.pipeline.generate_clip.GenerateClipStage` with
       shallow-merged params/spec (a *fresh* dict per entry so engine
       mutations cannot leak into ``cfg`` or sibling entries) and
       submit ``stage.run`` to an outer ``ThreadPoolExecutor`` sized by
       ``concurrent`` (falling back to
       ``cfg.lifecycle().max_in_flight``).
    3. Collect via :func:`concurrent.futures.as_completed`.  Per-entry
       exceptions become ``BatchOutcome(status="fail", error=...)`` —
       the batch keeps running.  Batch-fatal exceptions
       (:class:`BudgetExceeded`, :class:`CapabilityMismatch`,
       :class:`TeardownError`) cancel queued futures, mark in-flight
       entries as ``"interrupted"`` / ``"aborted"``, and re-raise.
    4. In a ``finally`` block, write ``_batch_summary.json`` under
       ``<batch_id>/`` on the store so every exit path (success,
       per-entry fail, batch-fatal) leaves a parseable record.

    Args:
        cfg: The loaded kinoforge configuration.
        manifest: A fully validated :class:`BatchManifest`
            (use :func:`load_manifest`).
        store: Destination :class:`~kinoforge.stores.base.ArtifactStore`
            for per-entry outputs + the summary JSON.
        batch_id: Top-level namespace for this batch's artifacts.
        concurrent: Outer-executor size override.  Defaults to
            ``cfg.lifecycle().max_in_flight``.
        provider: Optional pre-constructed
            :class:`~kinoforge.core.interfaces.ComputeProvider` (test
            injection).
        engine: Optional pre-constructed
            :class:`~kinoforge.core.interfaces.GenerationEngine` (test
            injection).
        image_engine: Optional pre-constructed
            :class:`~kinoforge.core.interfaces.ImageEngine` (test
            injection for the keyframe path).  When ``None`` and
            ``cfg.keyframe`` is set, resolved from the registry via
            ``cfg.keyframe.engine``.
        creds: Optional credential provider, forwarded to the
            provisioner via :func:`deploy_session`.
        profile_provider: Optional
            :class:`~kinoforge.core.interfaces.ModelProfileProvider`
            (test injection).  Defaults to
            :class:`~kinoforge.core.profiles.JsonProfileCache`.
        image_profile_provider: Optional
            :class:`~kinoforge.core.interfaces.ImageProfileProvider`
            (test injection for the image-engine profile cache).
            Defaults to ``JsonImageProfileCache(store)`` when
            ``cfg.keyframe`` is set.
        state_dir: Operator state root (provision markers, weights,
            locks).
        sink: Optional user-facing publish target.  When non-None,
            every per-entry
            :class:`~kinoforge.pipeline.generate_clip.GenerateClipStage`
            receives ``sink=sink, namespace=batch_id`` so all finished
            clips from this batch land under
            ``<output_dir>/<batch_id>/``.  When ``None`` (the default),
            no publish side-effect occurs and pre-Layer-O batch
            behaviour is preserved.
        instance: Optional caller-supplied pre-created
            :class:`~kinoforge.core.interfaces.Instance`.  When non-None,
            the inner :func:`deploy_session` skips
            ``provider.create_instance`` and binds the engine to the
            supplied pod for every entry in this batch (warm-pod reuse,
            Layer P Task 7 item #2).  Caller must pre-poll the instance
            to ``status == 'ready'``.  Teardown of the supplied pod
            remains the caller's responsibility — ``deploy_session``
            does not destroy a caller-owned instance.
        tags: Optional dict of caller tags merged onto the
            orchestrator-built :class:`InstanceSpec.tags` on the cold
            path (when ``instance=None``).  Built-ins
            (``kinoforge_engine``, ``kinoforge_key``) always win on key
            collision so cache-key derivation stays deterministic.
            Ignored when ``instance=`` is supplied — the caller already
            owns that pod's tags.
        on_event: Optional streaming callback fired with one
            :class:`BatchEvent` per per-entry milestone.  Two event
            kinds: ``entry_start`` (just before the worker begins the
            stage) and ``entry_finish`` (after the worker records its
            terminal status: ``ok`` / ``fail`` / ``interrupted`` /
            ``aborted``).  Calls are serialized via an internal
            ``threading.Lock`` so multi-line output never interleaves.
            When ``None`` (the default), no events fire and behaviour is
            byte-identical to pre-Layer-L-T4.
        cancel_token: Phase 50 cooperative-cancellation token. Forwarded
            verbatim into ``deploy_session`` (which uses it to bound the
            pool's shutdown) and into every per-entry
            :class:`GenerateClipStage` (which threads it into
            ``pool.submit`` so backend poll loops can observe and
            unwind). The CLI's SIGINT handler sets this token on first
            Ctrl-C. ``None`` (the default) preserves library-caller
            behavior.

    Returns:
        A :class:`BatchResult` with one
        :class:`BatchOutcome` per entry in submission order.

    Raises:
        BudgetExceeded: A per-entry stage breached the budget mid-batch.
            The summary JSON is written before this propagates.
        CapabilityMismatch: A live backend drifted from its cached
            profile mid-batch.  Summary written before re-raise.
        TeardownError: A teardown attempt failed inside
            :func:`deploy_session`.  Summary written before re-raise.

    Notes:
        Setup-time exceptions raised by ``deploy_session.__enter__``
        (``AuthError``, ``CapacityError``, ``UnknownAdapter``,
        ``CapabilityMismatch`` from the verify path) propagate before
        any per-entry work runs.  No summary is written in that case
        because no batch state exists yet — the failure is a pre-flight
        config / capacity problem, not a partial batch.
    """
    # Default-shim: sibling to orchestrator.generate's default. A None
    # creds reaches the provisioner via deploy_session and trips
    # AuthError on the first env_required var even when os.environ
    # holds the value. CLI's _cmd_batch + ad-hoc programmatic callers
    # routinely forget the kwarg; default it here so the public API
    # matches operator expectations. Drift-locked by
    # tests/core/test_batch_creds_default.py.
    if creds is None:
        creds = EnvCredentialProvider()
    cap = concurrent if concurrent is not None else cfg.lifecycle().max_in_flight
    emit = _LockedEmitter(on_event)

    started_at = datetime.now().isoformat(timespec="seconds")
    outcomes_by_idx: dict[int, BatchOutcome] = {}
    # Pre-seed the summary so the outer finally never has to reason
    # about a None.  The inner finally re-assigns this with the real
    # outcomes before deploy_session exits; the placeholder only
    # survives if deploy_session.__enter__ raises (in which case we
    # also skip writing _batch_summary.json — see the docstring's
    # "Notes" section).
    summary = BatchResult(
        batch_id=batch_id,
        started_at=started_at,
        finished_at=started_at,
        outcomes=[],
    )
    # Captured before any per-entry work so _mark_remaining_after_fatal
    # can fall back to it for never-started futures.
    batch_start = monotonic()

    # ------------------------------------------------------------------
    # Pre-resolve image engine + backend + profile ONCE per batch if
    # cfg.keyframe is set.  Amortises construction cost; unknown engine
    # names fail fast here before any compute spend.
    # ------------------------------------------------------------------
    _image_backend: ImageBackend | None = None
    _image_profile: ImageProfile | None = None
    _resolved_image_engine: ImageEngine | None = None
    if cfg.keyframe is not None:
        _resolved_image_engine = (
            image_engine
            if image_engine is not None
            else registry.get_image_engine(cfg.keyframe.engine)()
        )
        kf_cfg_dict = cfg.keyframe.model_dump()
        _resolved_image_engine.provision(None, kf_cfg_dict)
        _image_backend = _resolved_image_engine.backend(None, kf_cfg_dict)
        image_key = cfg.keyframe.capability_key()
        ipp: ImageProfileProvider = (
            image_profile_provider
            if image_profile_provider is not None
            else JsonImageProfileCache(store)  # type: ignore[assignment]
        )
        try:
            _image_profile = ipp.resolve(image_key)
        except ProfileNotCached:
            _image_profile = ipp.discover(
                image_key, _resolved_image_engine, _image_backend
            )

    try:
        with deploy_session(
            cfg,
            store=store,
            provider=provider,
            engine=engine,
            creds=creds,
            profile_provider=profile_provider,
            run_id=batch_id,
            state_dir=state_dir,
            instance=instance,
            tags=tags,
            cancel_token=cancel_token,
        ) as session:
            _eph = EphemeralSession.current()
            if _eph is not None:
                _eph.register_store(store, batch_id)
            accepted_kinds: set[str]
            if hasattr(session.engine, "accepted_kinds"):
                accepted_kinds = session.engine.accepted_kinds
            else:
                accepted_kinds = {"image"}

            executor = ThreadPoolExecutor(
                max_workers=cap,
                thread_name_prefix=f"kinoforge-batch-{batch_id}",
            )
            future_to_idx: dict[Future[Any], int] = {}
            start_times: dict[int, float] = {}

            try:
                for idx, entry in enumerate(manifest.entries):
                    try:
                        # --------------------------------------------------
                        # Per-entry keyframe phase: run BEFORE validate_request
                        # so that missing image-kind roles (e.g. init_image for
                        # i2v) are filled before validation fires.
                        # --------------------------------------------------
                        keyframe_state: PipelineState | None = None
                        if cfg.keyframe is not None:
                            # Merge per-entry keyframe overrides onto cfg-level
                            # defaults (shallow merge; entry wins per key).
                            base_kf_dict = cfg.keyframe.model_dump()
                            if entry.keyframe:
                                base_kf_dict.update(entry.keyframe)
                            # Re-parse to a validated KeyframeConfig so
                            # KeyframeStage receives a well-typed object.

                            entry_kf_cfg: KeyframeConfig = cfg.keyframe.__class__(
                                **base_kf_dict
                            )
                            entry_run_id = f"{batch_id}/{entry.run_id}"
                            raw_request = GenerationRequest(
                                prompt=entry.prompt or "",
                                mode=entry.mode,
                                assets=[
                                    ConditioningAsset(**a) for a in (entry.assets or [])
                                ],
                            )
                            # Layer 4 — publish keyframes alongside videos.
                            _kf_provider = (
                                getattr(_resolved_image_engine, "name", None) or None
                            )
                            _kf_model = (
                                str((entry_kf_cfg.spec or {}).get("model", "") or "")
                                or None
                            )
                            keyframe_state = KeyframeStage(
                                keyframe_cfg=entry_kf_cfg,
                                image_engine=_resolved_image_engine,  # type: ignore[arg-type]
                                image_backend=_image_backend,  # type: ignore[arg-type]
                                image_profile=_image_profile,  # type: ignore[arg-type]
                                store=store,
                                run_id=entry_run_id,
                                sink=sink,
                                namespace=batch_id,
                                provider=_kf_provider,
                                model=_kf_model,
                            ).run(PipelineState(request=raw_request, artifacts={}))

                        stage, initial_state = _build_stage_for_entry(
                            cfg,
                            entry,
                            session,
                            accepted_kinds,
                            store,
                            batch_id,
                            sink=sink,
                            keyframe_state=keyframe_state,
                            cancel_token=cancel_token,
                        )
                    except Exception as build_exc:  # noqa: BLE001 — per-entry catch
                        # Validation errors (e.g. unsupported mode) detected
                        # at build time are recorded as per-entry failures so
                        # the rest of the batch continues.
                        start_times[idx] = monotonic()
                        emit(
                            BatchEvent(
                                kind="entry_start",
                                batch_id=batch_id,
                                idx=idx,
                                run_id=entry.run_id or str(idx),
                                ts=datetime.now(),
                                entry=entry,
                            )
                        )
                        emit(
                            BatchEvent(
                                kind="entry_finish",
                                batch_id=batch_id,
                                idx=idx,
                                run_id=entry.run_id or str(idx),
                                ts=datetime.now(),
                                status="fail",
                                duration_s=0.0,
                                error=f"{type(build_exc).__name__}: {build_exc}",
                            )
                        )
                        outcomes_by_idx[idx] = BatchOutcome(
                            run_id=entry.run_id or str(idx),
                            status="fail",
                            duration_s=0.0,
                            error=f"{type(build_exc).__name__}: {build_exc}",
                        )
                        continue
                    fut = executor.submit(
                        _run_with_clock,
                        stage,
                        initial_state,
                        start_times,
                        idx,
                        emit=emit,
                        entry=entry,
                        batch_id=batch_id,
                    )
                    future_to_idx[fut] = idx

                try:
                    for fut in as_completed(future_to_idx.keys()):
                        idx = future_to_idx[fut]
                        entry = manifest.entries[idx]
                        duration = monotonic() - start_times.get(idx, batch_start)
                        try:
                            artifact = fut.result()
                        except (
                            BudgetExceeded,
                            CapabilityMismatch,
                            TeardownError,
                        ) as exc:
                            outcomes_by_idx[idx] = BatchOutcome(
                                run_id=entry.run_id or str(idx),
                                status="interrupted",
                                duration_s=duration,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                            emit(
                                BatchEvent(
                                    kind="entry_finish",
                                    batch_id=batch_id,
                                    idx=idx,
                                    run_id=entry.run_id or str(idx),
                                    ts=datetime.now(),
                                    status="interrupted",
                                    duration_s=duration,
                                    error=f"{type(exc).__name__}: {exc}",
                                )
                            )
                            # Cancel everything else; mark queued as
                            # "aborted", in-flight as "interrupted".
                            _mark_remaining_after_fatal(
                                future_to_idx,
                                outcomes_by_idx,
                                manifest,
                                start_times,
                                batch_start,
                                emit=emit,
                                batch_id=batch_id,
                                fatal_type=type(exc).__name__,
                            )
                            raise
                        except Exception as exc:  # noqa: BLE001 — per-entry catch
                            outcomes_by_idx[idx] = BatchOutcome(
                                run_id=entry.run_id or str(idx),
                                status="fail",
                                duration_s=duration,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                            emit(
                                BatchEvent(
                                    kind="entry_finish",
                                    batch_id=batch_id,
                                    idx=idx,
                                    run_id=entry.run_id or str(idx),
                                    ts=datetime.now(),
                                    status="fail",
                                    duration_s=duration,
                                    error=f"{type(exc).__name__}: {exc}",
                                )
                            )
                        else:
                            outcomes_by_idx[idx] = BatchOutcome(
                                run_id=entry.run_id or str(idx),
                                status="ok",
                                duration_s=duration,
                                uri=artifact.uri,
                            )
                            emit(
                                BatchEvent(
                                    kind="entry_finish",
                                    batch_id=batch_id,
                                    idx=idx,
                                    run_id=entry.run_id or str(idx),
                                    ts=datetime.now(),
                                    status="ok",
                                    duration_s=duration,
                                    uri=artifact.uri,
                                )
                            )
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
            finally:
                # Build the ordered outcome list inside the
                # deploy_session block so it lands in the summary even
                # if the with-statement raises on exit.
                summary = _finalize_summary(
                    manifest, outcomes_by_idx, batch_id, started_at
                )
    finally:
        # Persist the summary on EVERY exit path: clean batch,
        # per-entry-fail batch, BudgetExceeded re-raise.  Failure to
        # write is logged but never escalates — the in-memory
        # BatchResult is still returned (or the original exception
        # re-raised).
        try:
            from kinoforge.core.redaction import RedactionRegistry

            payload = summary.to_dict()
            _eph = EphemeralSession.current()
            if _eph is not None and not _eph.policy.batch_summary_write:
                # Strict mode: in-memory summary still returned to caller
                # via BatchResult; no _batch_summary.json on disk.
                pass
            else:
                redacted = RedactionRegistry.instance().redact_json(payload)
                if not isinstance(
                    redacted, dict
                ):  # pragma: no cover — redact_json keeps dict shape
                    raise TypeError(
                        f"redact_json must preserve dict shape, got {type(redacted).__name__}"
                    )
                store.put_json(batch_id, "_batch_summary.json", redacted)
        except Exception:  # noqa: BLE001
            _log.exception(
                "failed to write _batch_summary.json for batch_id=%s",
                batch_id,
            )

    return summary
