"""Tests for kinoforge.core.batch.batch_generate (Layer L Task 3).

batch_generate wraps deploy_session, fans entries out via a
ThreadPoolExecutor, collects via as_completed, swallows per-entry
exceptions, re-raises batch-fatal ones (BudgetExceeded /
CapabilityMismatch / TeardownError), and writes _batch_summary.json in
a finally block so every exit path leaves a parseable record.

The spy classes used here (``_BatchSpyEngine``,
``_VerifyCountingProfileCache``, ``_DiscoverCountingProfileCache``) are
test-local rather than living in test_orchestrator.py — they add a
handful of fields specific to batch behaviour (peak-in-flight,
per-entry param capture, prompt-targeted failure injection) and would
clutter the shared orchestrator fixtures.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# Import providers/engines/sources so they self-register.
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401 — registers https:// source
from kinoforge.core.batch import (
    BatchEntry,
    BatchManifest,
    BatchResult,
    batch_generate,
)
from kinoforge.core.errors import AssetFetchError, BudgetExceeded
from kinoforge.core.interfaces import (
    CapabilityKey,
    GenerationBackend,
    GenerationJob,
    Instance,
    ModelProfile,
)
from kinoforge.core.profiles import JsonProfileCache
from kinoforge.engines.fake import FakeBackend, FakeEngine
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

# Reuse compute-cfg helper from existing orchestrator tests.
from tests.core.test_orchestrator import _compute_cfg, _probe_profile

# ---------------------------------------------------------------------------
# Test-local spy backend + engine
# ---------------------------------------------------------------------------


class _BatchSpyBackend(FakeBackend):
    """FakeBackend variant that records jobs and supports failure injection.

    Tracks:
      * Every submitted job (so the engine can post-hoc compute
        per-entry observed params keyed by ``segments[0].prompt``).
      * Optional barrier-based peak in-flight counter for the
        concurrency-cap test.
      * Optional ``fail_on_prompt`` / ``fail_with`` hooks so an entry
        whose first-segment prompt matches a target string raises a
        chosen exception inside ``submit``.
      * Optional ``mutate_base_params`` flag for the cross-entry
        mutation leak test.
    """

    def __init__(
        self,
        probe: ModelProfile,
        *,
        fail_on_prompt: str | None,
        fail_with: Exception | None,
        observe_in_flight: bool,
        mutate_base_params: bool,
        in_flight_state: dict[str, int],
        in_flight_lock: threading.Lock,
        barrier_delay: float,
        observed_params: dict[str, dict[str, Any]],
    ) -> None:
        super().__init__(probe=probe)
        self._fail_on_prompt = fail_on_prompt
        self._fail_with = fail_with
        self._observe_in_flight = observe_in_flight
        self._mutate_base_params = mutate_base_params
        self._in_flight_state = in_flight_state
        self._in_flight_lock = in_flight_lock
        self._barrier_delay = barrier_delay
        self._observed_params = observed_params

    def submit(self, job: GenerationJob) -> str:
        prompt = job.segments[0].prompt if job.segments else ""
        # Record observed params keyed by the segment-0 prompt — tests pick
        # prompts that uniquely identify the entry under test.
        self._observed_params[prompt] = dict(job.params)

        if self._mutate_base_params:
            # Deliberately bad-citizen: poke a value into the dict the
            # engine received.  batch_generate must defend against this
            # leaking back into cfg.params or sibling entries via a
            # fresh-copy invariant per entry.
            if "nested" in job.params and isinstance(job.params["nested"], dict):
                job.params["nested"]["a"] = 99
            else:
                job.params["_mutated"] = True

        if self._observe_in_flight:
            with self._in_flight_lock:
                self._in_flight_state["current"] += 1
                if self._in_flight_state["current"] > self._in_flight_state["peak"]:
                    self._in_flight_state["peak"] = self._in_flight_state["current"]
            try:
                # Sleep with the in-flight counter raised so a second
                # concurrent submit can observe it.  Tiny duration so the
                # whole test finishes in <100ms.
                time.sleep(self._barrier_delay)
            finally:
                with self._in_flight_lock:
                    self._in_flight_state["current"] -= 1

        if (
            self._fail_on_prompt is not None
            and prompt == self._fail_on_prompt
            and self._fail_with is not None
        ):
            raise self._fail_with

        return super().submit(job)


class _BatchSpyEngine(FakeEngine):
    """FakeEngine that constructs spy backends and exposes batch observations.

    Attributes:
        fail_on_prompt: Backend submit raises ``fail_with`` when the
            job's segment-0 prompt matches.  Used by per-entry-fail
            and batch-fatal tests.
        fail_with: The exception instance to raise when the prompt
            matches ``fail_on_prompt``.
        observe_in_flight: When True, the spy backend tracks peak
            concurrent submits via a shared lock-protected counter.
        peak_in_flight: Observed peak (after the batch finishes).
        mutate_base_params: When True, the spy backend mutates
            ``job.params`` mid-submit — used to confirm batch_generate
            isolates per-entry stage state from cfg.
        observed_base_params_per_prompt: Maps segment-0 prompt to the
            ``base_params`` snapshot the spy saw on ``submit``.
    """

    def __init__(
        self,
        *,
        probe_profile: ModelProfile,
        declared_flags_map: dict[str, dict[str, Any]],
        required_spec_keys: set[str],
        fail_on_prompt: str | None = None,
        fail_with: Exception | None = None,
        observe_in_flight: bool = False,
        mutate_base_params: bool = False,
        barrier_delay: float = 0.05,
    ) -> None:
        super().__init__(
            probe_profile=probe_profile,
            declared_flags_map=declared_flags_map,
            required_spec_keys=required_spec_keys,
        )
        self.fail_on_prompt = fail_on_prompt
        self.fail_with = fail_with
        self.observe_in_flight = observe_in_flight
        self.mutate_base_params = mutate_base_params
        self._barrier_delay = barrier_delay
        self._in_flight_state: dict[str, int] = {"current": 0, "peak": 0}
        self._in_flight_lock = threading.Lock()
        self.observed_base_params_per_prompt: dict[str, dict[str, Any]] = {}

    @property
    def peak_in_flight(self) -> int:
        return self._in_flight_state["peak"]

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> _BatchSpyBackend:
        del instance, cfg
        return _BatchSpyBackend(
            probe=self._probe,
            fail_on_prompt=self.fail_on_prompt,
            fail_with=self.fail_with,
            observe_in_flight=self.observe_in_flight,
            mutate_base_params=self.mutate_base_params,
            in_flight_state=self._in_flight_state,
            in_flight_lock=self._in_flight_lock,
            barrier_delay=self._barrier_delay,
            observed_params=self.observed_base_params_per_prompt,
        )


class _DiscoverCountingProfileCache(JsonProfileCache):
    """JsonProfileCache subclass tracking discover() invocations.

    Mirrors the helper in test_deploy_session.py — we duplicate rather
    than import to keep the batch tests independent of the
    deploy_session test module's evolution.
    """

    def __init__(self, store: Any) -> None:
        super().__init__(store)
        self.discover_calls: int = 0

    def discover(
        self,
        key: CapabilityKey,
        engine: Any,
        backend: GenerationBackend,
    ) -> ModelProfile:
        self.discover_calls += 1
        return super().discover(key, engine, backend)


class _VerifyCountingProfileCache(JsonProfileCache):
    """JsonProfileCache subclass tracking verify() invocations."""

    def __init__(self, store: Any) -> None:
        super().__init__(store)
        self.verify_calls: int = 0

    def verify(
        self,
        profile: ModelProfile,
        backend: GenerationBackend,
        *,
        engine: Any = None,
        key: CapabilityKey | None = None,
    ) -> None:
        self.verify_calls += 1
        return super().verify(profile, backend, engine=engine, key=key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spy_engine(**kwargs: Any) -> _BatchSpyEngine:
    return _BatchSpyEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys=set(),
        **kwargs,
    )


def _three_entry_manifest() -> BatchManifest:
    return BatchManifest(
        entries=[
            BatchEntry(prompt="alpha", mode="t2v", run_id="x"),
            BatchEntry(prompt="beta", mode="t2v", run_id="y"),
            BatchEntry(prompt="gamma", mode="t2v", run_id="z"),
        ]
    )


def _seed_profile_cache(
    tmp_path: Path, store: LocalArtifactStore, engine: FakeEngine
) -> None:
    """Run one deploy_session against *engine* to populate the on-disk cache.

    The next batch_generate call against the same store sees a warm
    cache and exercises the verify() branch.
    """
    from kinoforge.core.orchestrator import deploy_session

    cfg = _compute_cfg()
    provider = LocalProvider()
    with deploy_session(
        cfg,
        store=store,
        engine=engine,
        provider=provider,
        run_id="seed",
        state_dir=tmp_path / "_seed_state",
    ):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_three_entries_all_ok_round_trip(tmp_path: Path) -> None:
    """3-entry batch on local-fake cfg → 3 ok outcomes, 3 distinct URIs.

    Bug catch: an as_completed loop that swaps the outcome-to-entry
    mapping when futures finish out of order would scramble user-facing
    BatchResult ordering — silent data corruption.  We assert outcomes
    are returned in submission order and each entry has a distinct URI
    under <root>/<batch_id>/<run_id>/.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _make_spy_engine()
    provider = LocalProvider()

    result: BatchResult = batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        state_dir=tmp_path / "_state",
    )

    assert isinstance(result, BatchResult)
    assert [o.status for o in result.outcomes] == ["ok", "ok", "ok"]
    assert [o.run_id for o in result.outcomes] == ["x", "y", "z"]
    uris = [o.uri for o in result.outcomes]
    assert all(u is not None for u in uris), uris
    assert len(set(uris)) == 3, f"expected three distinct uris, got {uris!r}"
    # Each artifact lives under <root>/b/<run_id>/...
    for run_id in ("x", "y", "z"):
        sub = tmp_path / "b" / run_id
        assert sub.is_dir(), f"missing namespace {sub}"
        assert any(sub.iterdir()), f"no artifacts in {sub}"


def test_per_entry_failure_continues_batch(tmp_path: Path) -> None:
    """One entry raising AssetFetchError must not abort the others.

    Bug catch: a per-entry exception that aborts the whole batch
    defeats the continue-on-error contract — overnight runs die on
    the first bad prompt.  We pin the contract by failing only the
    "beta" entry and asserting "alpha"/"gamma" still produce ok
    outcomes, and that batch_generate returns normally instead of
    re-raising.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _make_spy_engine(
        fail_on_prompt="beta",
        fail_with=AssetFetchError("forced for test"),
    )
    provider = LocalProvider()

    result = batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        state_dir=tmp_path / "_state",
    )

    statuses = {o.run_id: o.status for o in result.outcomes}
    assert statuses == {"x": "ok", "y": "fail", "z": "ok"}, statuses
    fail_outcome = next(o for o in result.outcomes if o.run_id == "y")
    assert fail_outcome.error is not None
    assert "AssetFetchError" in fail_outcome.error or "forced" in fail_outcome.error


def test_budget_exceeded_re_raises_after_writing_summary(tmp_path: Path) -> None:
    """BudgetExceeded mid-batch must re-raise AND persist a summary.

    Bug catch: a batch-fatal exception that aborts without persisting
    the summary leaves users with no record of what completed before
    the crash.  We pin that contract by failing the middle entry with
    BudgetExceeded, catching the re-raise, and asserting both that
    _batch_summary.json exists on disk and that the failing entry's
    status is "interrupted".
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _make_spy_engine(
        fail_on_prompt="beta",
        fail_with=BudgetExceeded("forced for test"),
    )
    provider = LocalProvider()

    with pytest.raises(BudgetExceeded):
        batch_generate(
            cfg,
            _three_entry_manifest(),
            store=store,
            batch_id="b",
            engine=engine,
            provider=provider,
            state_dir=tmp_path / "_state",
        )

    summary_path = tmp_path / "b" / "_batch_summary.json"
    assert summary_path.is_file(), f"expected summary at {summary_path}"
    summary = json.loads(summary_path.read_text())
    assert summary["batch_id"] == "b"
    entries = summary["entries"]
    statuses = {e["run_id"]: e["status"] for e in entries}
    assert "y" in statuses, statuses
    assert statuses["y"] == "interrupted", statuses


def test_entry_param_override_isolated_to_that_entry(tmp_path: Path) -> None:
    """params override on one entry must not leak to sibling entries.

    Bug catch: a shared-dict bug where every entry's stage references
    the same cfg.params dict means one user's seed silently propagates
    to every other clip in the batch.  We assert the overriding
    entry's stage sees the merged dict ({"seed": 42}), the other
    entries see the cfg-only dict ({"seed": 1}), and that cfg.params
    itself is unmodified.
    """
    cfg = _compute_cfg()
    cfg.params = {"seed": 1}
    store = LocalArtifactStore(tmp_path)
    engine = _make_spy_engine()
    provider = LocalProvider()

    manifest = BatchManifest(
        entries=[
            BatchEntry(prompt="alpha", mode="t2v", run_id="x"),
            BatchEntry(prompt="beta", mode="t2v", run_id="y", params={"seed": 42}),
            BatchEntry(prompt="gamma", mode="t2v", run_id="z"),
        ]
    )

    batch_generate(
        cfg,
        manifest,
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        state_dir=tmp_path / "_state",
    )

    observed = engine.observed_base_params_per_prompt
    assert observed["alpha"] == {"seed": 1}, observed
    assert observed["beta"] == {"seed": 42}, observed
    assert observed["gamma"] == {"seed": 1}, observed
    # cfg.params is untouched at the outer level.
    assert cfg.params == {"seed": 1}, cfg.params


def test_entry_override_does_not_mutate_cfg_or_siblings(tmp_path: Path) -> None:
    """Engine-side mutation of base_params must not leak into cfg.params.

    Bug catch: a shallow-copy bug where ``dict(cfg.params)`` shares
    nested-dict references means an engine that does
    ``job.params["nested"]["a"] = 99`` corrupts the user's cfg.params
    in place — every subsequent batch entry sees the mutated value.
    We pin the contract by enabling the spy's deliberate-bad-citizen
    mutation and asserting cfg.params is unchanged afterwards.
    """
    cfg = _compute_cfg()
    cfg.params = {"nested": {"a": 1}}
    store = LocalArtifactStore(tmp_path)
    engine = _make_spy_engine(mutate_base_params=True)
    provider = LocalProvider()

    manifest = BatchManifest(
        entries=[
            BatchEntry(prompt="alpha", mode="t2v", run_id="x"),
            BatchEntry(prompt="beta", mode="t2v", run_id="y"),
        ]
    )

    batch_generate(
        cfg,
        manifest,
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        state_dir=tmp_path / "_state",
    )

    assert cfg.params == {"nested": {"a": 1}}, (
        f"batch_generate must defend cfg.params against engine mutation; "
        f"saw cfg.params={cfg.params!r}"
    )


def test_concurrent_caps_in_flight_stages(tmp_path: Path) -> None:
    """concurrent=2 limits in-flight stage runs to <= 2 at a time.

    Bug catch: an unbounded ThreadPoolExecutor floods the backend with
    concurrent requests, blowing past the engine's documented cap.  We
    pin the contract by having the spy backend hold a tiny barrier in
    every submit so multiple in-flight calls overlap if the executor
    permits it, then assert the observed peak is <= 2.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _make_spy_engine(observe_in_flight=True, barrier_delay=0.05)
    provider = LocalProvider()

    manifest = BatchManifest(
        entries=[
            BatchEntry(prompt=f"prompt-{i}", mode="t2v", run_id=str(i))
            for i in range(3)
        ]
    )

    batch_generate(
        cfg,
        manifest,
        store=store,
        batch_id="b",
        concurrent=2,
        engine=engine,
        provider=provider,
        state_dir=tmp_path / "_state",
    )

    assert engine.peak_in_flight <= 2, (
        f"--concurrent=2 must cap peak in-flight at 2; "
        f"observed peak={engine.peak_in_flight}"
    )


def test_cold_cache_discover_runs_once(tmp_path: Path) -> None:
    """Cold profile cache → one discover() call for the whole batch.

    Bug catch: per-entry rediscovery would burn one inspect_capabilities
    probe per entry instead of amortizing it across the batch.  We pin
    the contract by running batch_generate against a fresh
    _DiscoverCountingProfileCache and asserting exactly one discover().
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _make_spy_engine()
    provider = LocalProvider()
    counting_cache = _DiscoverCountingProfileCache(store)

    batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        profile_provider=counting_cache,
        state_dir=tmp_path / "_state",
    )

    assert counting_cache.discover_calls == 1, (
        f"cold cache must call discover exactly once for the batch; "
        f"got discover_calls={counting_cache.discover_calls}"
    )


def test_warm_cache_verify_runs_once(tmp_path: Path) -> None:
    """Warm profile cache → one verify() call for the whole batch.

    Bug catch: per-entry verify wastes probe traffic on a warm batch.
    We pin the contract by pre-seeding the cache via one deploy_session
    pass, then running batch_generate against the same store with a
    _VerifyCountingProfileCache and asserting discover never runs and
    verify runs exactly once.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    seed_engine = _make_spy_engine()
    _seed_profile_cache(tmp_path, store, seed_engine)

    # Probe phase: fresh engine + verify-counting cache.
    probe_engine = _make_spy_engine()
    provider = LocalProvider()
    counting_cache = _VerifyCountingProfileCache(store)

    batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=probe_engine,
        provider=provider,
        profile_provider=counting_cache,
        state_dir=tmp_path / "_state",
    )

    assert counting_cache.verify_calls == 1, (
        f"warm cache must verify exactly once for the batch; "
        f"got verify_calls={counting_cache.verify_calls}"
    )


def test_validate_request_runs_once_per_entry(tmp_path: Path) -> None:
    """Stage.validate_request is invoked exactly len(manifest.entries) times.

    Bug catch: skipping per-entry validation lets bad mode/role/asset
    combinations dispatch to the engine, where the failure mode is
    cryptic.  We pin the contract by patching
    ``kinoforge.pipeline.generate_clip.validate_request`` with a wraps=
    spy and asserting call_count equals the manifest size.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _make_spy_engine()
    provider = LocalProvider()

    import kinoforge.core.validation as validation_mod

    with patch(
        "kinoforge.pipeline.generate_clip.validate_request",
        wraps=validation_mod.validate_request,
    ) as spy:
        batch_generate(
            cfg,
            _three_entry_manifest(),
            store=store,
            batch_id="b",
            engine=engine,
            provider=provider,
            state_dir=tmp_path / "_state",
        )

    assert spy.call_count == 3, (
        f"expected exactly 3 validate_request calls (one per entry); "
        f"got {spy.call_count}"
    )


def test_summary_written_on_clean_path(tmp_path: Path) -> None:
    """_batch_summary.json must land under <batch_id>/ on a clean batch.

    Bug catch: writing the summary only on the error branch (or only
    on the clean branch) means downstream tooling can't rely on its
    presence as a marker.  Layer L's contract: summary lands in the
    finally block, period.  We pin the clean-path half here; the
    fatal-path half is pinned by
    ``test_budget_exceeded_re_raises_after_writing_summary``.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    engine = _make_spy_engine()
    provider = LocalProvider()

    batch_generate(
        cfg,
        _three_entry_manifest(),
        store=store,
        batch_id="b",
        engine=engine,
        provider=provider,
        state_dir=tmp_path / "_state",
    )

    summary_path = tmp_path / "b" / "_batch_summary.json"
    assert summary_path.is_file(), f"expected summary at {summary_path}"
    summary = json.loads(summary_path.read_text())
    assert summary["batch_id"] == "b"
    assert len(summary["entries"]) == 3
    assert all(e["status"] == "ok" for e in summary["entries"]), summary
