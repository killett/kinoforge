"""Batch generation: manifest schema + dispatch (Layer L).

This module owns:
  * BatchEntry / BatchManifest pydantic models with strict validation.
  * load_manifest() — reads YAML, resolves prompt_file paths, auto-indexes
    run_ids, returns a fully validated BatchManifest.
  * BatchOutcome / BatchResult dataclasses.
  * batch_generate() — the orchestration entry point that wraps
    deploy_session, fans entries out via ThreadPoolExecutor, and writes
    _batch_summary.json on every exit path.

Core-import-ban: this module imports ONLY from kinoforge.core.* +
kinoforge.pipeline.generate_clip + kinoforge.stores.base + stdlib +
pydantic + PyYAML.  No kinoforge.providers / engines / sources.  The
invariant test in tests/test_core_invariant.py enforces this via
subprocess isolation.
"""

from __future__ import annotations

import copy
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kinoforge.core.errors import (
    BudgetExceeded,
    CapabilityMismatch,
    ConfigError,
    TeardownError,
)
from kinoforge.core.interfaces import Artifact, ConditioningAsset, GenerationRequest
from kinoforge.core.logging import get_logger
from kinoforge.core.orchestrator import deploy_session
from kinoforge.outputs.base import OutputSink
from kinoforge.pipeline.generate_clip import GenerateClipStage
from kinoforge.stores.base import ArtifactStore

if TYPE_CHECKING:
    from kinoforge.core.config import Config
    from kinoforge.core.interfaces import (
        ComputeProvider,
        CredentialProvider,
        GenerationEngine,
        ModelProfileProvider,
    )
    from kinoforge.core.orchestrator import DeploySession

_log = get_logger(__name__)


class BatchEntry(BaseModel):
    """One entry in a batch manifest.

    Attributes:
        prompt: Inline prompt text. Mutually exclusive with prompt_file.
        prompt_file: Path to a text file (resolved relative to the
            manifest's parent dir). Mutually exclusive with prompt.
            After load_manifest runs, this is always None — the loader
            collapses prompt_file into prompt.
        mode: Generation mode (t2v / i2v / flf2v). Required per entry —
            no inherited default. An explicit per-entry choice avoids
            silent mode mixups.
        run_id: Sub-namespace under the batch_id for this entry's
            artifacts. None means "let the loader auto-index by position".
            After load_manifest runs, this is always set.
        params: Engine-neutral param overrides shallow-merged onto
            cfg.params (entry wins per key).
        spec: Engine-interpreted spec overrides shallow-merged onto
            cfg.spec (entry wins per key).
        assets: List of asset dicts forwarded into GenerationRequest.assets.
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = None
    prompt_file: str | None = None
    mode: str
    run_id: str | None = None
    params: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None
    assets: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _exactly_one_prompt_source(self) -> BatchEntry:
        """Reject entries that set both or neither of prompt / prompt_file.

        Returns:
            ``self`` unchanged when the rule is satisfied.

        Raises:
            ValueError: When neither or both of prompt / prompt_file are set.
        """
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError("entry must set exactly one of `prompt` / `prompt_file`")
        return self


class BatchManifest(BaseModel):
    """A validated batch manifest.

    Attributes:
        entries: One or more BatchEntry objects, in submission order.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[BatchEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_run_ids(self) -> BatchManifest:
        """Reject manifests whose explicit run_ids collide.

        When ANY entry sets ``run_id`` explicitly, ALL run_ids (including
        the auto-derived ones added by load_manifest later) must be
        unique.  When NONE set ``run_id``, the loader auto-indexes
        ``"0"``, ``"1"``, ... — collision-free by construction.

        Returns:
            ``self`` unchanged when run_ids are unique.

        Raises:
            ValueError: When the explicit run_id set contains duplicates.
        """
        ids = [e.run_id for e in self.entries if e.run_id is not None]
        if ids and len(set(ids)) != len(ids):
            dupes = sorted({x for x in ids if ids.count(x) > 1})
            raise ValueError(f"duplicate run_id in manifest: {dupes}")
        return self


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


@dataclass
class BatchOutcome:
    """The result of one entry in a batch run.

    Attributes:
        run_id: The entry's run_id (always set after load_manifest).
        status: One of "ok" / "fail" / "aborted" / "interrupted".
        duration_s: Seconds the entry was in-flight (None for "aborted").
        uri: Persisted artifact URI on "ok"; None otherwise.
        error: Stringified exception on "fail" / "interrupted"; None otherwise.
    """

    run_id: str
    status: Literal["ok", "fail", "aborted", "interrupted"]
    duration_s: float | None = None
    uri: str | None = None
    error: str | None = None


@dataclass
class BatchResult:
    """Summary of one batch_generate() call.

    Attributes:
        batch_id: The batch namespace ID (e.g. "batch-20260531-093052").
        started_at: ISO local-tz timestamp string.
        finished_at: ISO local-tz timestamp string.
        outcomes: Ordered by entry submission order (NOT completion order).
    """

    batch_id: str
    started_at: str
    finished_at: str
    outcomes: list[BatchOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly shape written to ``_batch_summary.json``.

        Returns:
            A dict with ``batch_id``, ``started_at``, ``finished_at``,
            and ``entries`` (the outcomes with ``None`` fields omitted).
        """
        return {
            "batch_id": self.batch_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "entries": [
                {k: v for k, v in vars(o).items() if v is not None}
                for o in self.outcomes
            ],
        }


def _build_stage_for_entry(
    cfg: Config,
    entry: BatchEntry,
    session: DeploySession,
    accepted_kinds: set[str],
    store: ArtifactStore,
    batch_id: str,
    sink: OutputSink | None = None,
) -> tuple[GenerateClipStage, GenerationRequest]:
    """Build a stage + request pair for one batch entry.

    Deep-copies cfg.params / cfg.spec so neither cfg nor any sibling
    entry's stage shares a mutable reference with this entry's
    base_params / base_spec.  A shallow ``dict(cfg.params)`` would
    keep nested-dict identity, letting a deliberately-bad-citizen
    engine that does ``job.params["nested"]["a"] = 99`` corrupt
    ``cfg.params`` in place.  Entry-side overrides are deep-copied too
    so the same protection applies to per-entry nested dicts.

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

    Returns:
        A ``(stage, request)`` tuple ready for
        ``executor.submit(stage.run, request)``.  The request carries
        the entry's prompt, mode, and any declared
        :class:`ConditioningAsset` objects built from ``entry.assets``.
    """
    merged_params = {
        **copy.deepcopy(dict(cfg.params)),
        **copy.deepcopy(entry.params or {}),
    }
    merged_spec = {
        **copy.deepcopy(dict(cfg.spec)),
        **copy.deepcopy(entry.spec or {}),
    }
    request = GenerationRequest(
        prompt=entry.prompt or "",
        mode=entry.mode,
        assets=[ConditioningAsset(**a) for a in (entry.assets or [])],
    )
    entry_run_id = f"{batch_id}/{entry.run_id}"
    stage = GenerateClipStage(
        profile=session.profile,
        pool=session.pool,
        store=store,
        run_id=entry_run_id,
        accepted_kinds=accepted_kinds,
        base_params=merged_params,
        base_spec=merged_spec,
        engine=session.engine,
        sink=sink,
        namespace=batch_id,
    )
    return stage, request


def _run_with_clock(
    stage: GenerateClipStage,
    request: GenerationRequest,
    start_times: dict[int, float],
    idx: int,
) -> Artifact:
    """Stamp the real stage-run start time, then run the stage.

    Recording ``monotonic()`` before ``executor.submit`` would
    conflate queue-wait time with the stage's real wall-clock cost —
    a 5-entry batch with ``concurrent=1`` would report 5x inflated
    durations for the last entries.  Stamping here, inside the worker
    thread, gives ``BatchOutcome.duration_s`` the actual stage cost.

    Args:
        stage: The pre-built GenerateClipStage for this entry.
        request: The GenerationRequest carrying prompt / mode / assets.
        start_times: Shared dict keyed by entry index; this worker
            writes its slot before doing real work.
        idx: The entry's position in ``manifest.entries``.

    Returns:
        Whatever ``stage.run(request)`` returns (the persisted
        :class:`~kinoforge.core.interfaces.Artifact`).
    """
    start_times[idx] = monotonic()
    return stage.run(request)


def _mark_remaining_after_fatal(
    future_to_idx: dict[Future[Any], int],
    outcomes_by_idx: dict[int, BatchOutcome],
    manifest: BatchManifest,
    start_times: dict[int, float],
    batch_start: float,
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
    """
    for other_fut, other_idx in future_to_idx.items():
        if other_idx in outcomes_by_idx:
            continue
        other_entry = manifest.entries[other_idx]
        if other_fut.cancel():
            outcomes_by_idx[other_idx] = BatchOutcome(
                run_id=other_entry.run_id or str(other_idx),
                status="aborted",
            )
        else:
            # An in-flight future cannot be cancelled; record how long
            # it has been running.  Fall back to batch_start when the
            # worker hadn't yet hit _run_with_clock — that means the
            # future was scheduled but never woke up before the fatal
            # took over, so duration is effectively 0.
            other_duration = monotonic() - start_times.get(other_idx, batch_start)
            outcomes_by_idx[other_idx] = BatchOutcome(
                run_id=other_entry.run_id or str(other_idx),
                status="interrupted",
                duration_s=other_duration,
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
    creds: CredentialProvider | None = None,
    profile_provider: ModelProfileProvider | None = None,
    state_dir: Path = Path(".kinoforge"),
    sink: OutputSink | None = None,
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
        creds: Optional credential provider, forwarded to the
            provisioner via :func:`deploy_session`.
        profile_provider: Optional
            :class:`~kinoforge.core.interfaces.ModelProfileProvider`
            (test injection).  Defaults to
            :class:`~kinoforge.core.profiles.JsonProfileCache`.
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
    cap = concurrent if concurrent is not None else cfg.lifecycle().max_in_flight

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
        ) as session:
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
                    stage, request = _build_stage_for_entry(
                        cfg,
                        entry,
                        session,
                        accepted_kinds,
                        store,
                        batch_id,
                        sink=sink,
                    )
                    fut = executor.submit(
                        _run_with_clock, stage, request, start_times, idx
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
                            # Cancel everything else; mark queued as
                            # "aborted", in-flight as "interrupted".
                            _mark_remaining_after_fatal(
                                future_to_idx,
                                outcomes_by_idx,
                                manifest,
                                start_times,
                                batch_start,
                            )
                            raise
                        except Exception as exc:  # noqa: BLE001 — per-entry catch
                            outcomes_by_idx[idx] = BatchOutcome(
                                run_id=entry.run_id or str(idx),
                                status="fail",
                                duration_s=duration,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                        else:
                            outcomes_by_idx[idx] = BatchOutcome(
                                run_id=entry.run_id or str(idx),
                                status="ok",
                                duration_s=duration,
                                uri=artifact.uri,
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
            store.put_json(batch_id, "_batch_summary.json", summary.to_dict())
        except Exception:  # noqa: BLE001
            _log.exception(
                "failed to write _batch_summary.json for batch_id=%s",
                batch_id,
            )

    return summary
