"""Shared test fakes/spies for Layer L batch tests.

Lives here so test_batch_generate.py (Task 3) and test_batch_cli.py
(Task 4) can both reuse the same spies without copy-paste.

Provides:
    * :class:`_BatchSpyBackend` — FakeBackend variant that records
      every job, supports prompt-targeted failure injection, optional
      barrier-based peak-in-flight tracking, and an optional
      bad-citizen ``mutate_base_params`` flag for the cross-entry
      mutation-leak test.
    * :class:`_BatchSpyEngine` — FakeEngine variant that constructs
      the spy backend and exposes the batch-level observations
      (``peak_in_flight``, ``observed_base_params_per_prompt``,
      ``observed_requests``).
    * :class:`_ProfileCacheCallCounter` — a single
      :class:`~kinoforge.core.profiles.JsonProfileCache` subclass that
      counts both ``discover()`` and ``verify()`` invocations.  Used
      for cold-cache + warm-cache invariants.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from kinoforge.core.cancel import CancelToken
from kinoforge.core.interfaces import (
    CapabilityKey,
    ConditioningAsset,
    GenerationBackend,
    GenerationJob,
    Instance,
    ModelProfile,
)
from kinoforge.core.profiles import JsonProfileCache
from kinoforge.engines.fake import FakeBackend, FakeEngine

__all__ = [
    "_BatchSpyBackend",
    "_BatchSpyEngine",
    "_ProfileCacheCallCounter",
]


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
        observed_assets_per_prompt: dict[str, list[ConditioningAsset]],
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
        self._observed_assets_per_prompt = observed_assets_per_prompt

    def submit(
        self,
        job: GenerationJob,
        *,
        cancel_token: CancelToken | None = None,
    ) -> str:
        prompt = job.segments[0].prompt if job.segments else ""
        # Record observed params keyed by the segment-0 prompt — tests pick
        # prompts that uniquely identify the entry under test.
        self._observed_params[prompt] = dict(job.params)
        # Record observed segment-0 assets list (a shallow copy so any
        # later mutation by downstream code doesn't pollute the spy).
        self._observed_assets_per_prompt[prompt] = list(
            job.segments[0].assets if job.segments else []
        )

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

        return super().submit(job, cancel_token=cancel_token)


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
        observed_assets_per_prompt: Maps segment-0 prompt to the
            list of segment-0 assets the spy saw on ``submit``.
            These are the :class:`ConditioningAsset` instances the
            stage built from the inbound
            :class:`GenerationRequest.assets`, so a non-empty list
            here proves the entry's manifest-side assets made it
            all the way through the seam without being dropped.
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
        self.observed_assets_per_prompt: dict[str, list[ConditioningAsset]] = {}

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
            observed_assets_per_prompt=self.observed_assets_per_prompt,
        )


class _ProfileCacheCallCounter(JsonProfileCache):
    """JsonProfileCache subclass tracking both discover() and verify() calls.

    Fuses what used to be two separate spy classes
    (``_DiscoverCountingProfileCache`` and
    ``_VerifyCountingProfileCache``) into one: cold-cache tests assert
    ``discover_calls == 1`` and warm-cache tests assert
    ``verify_calls == 1``, both off the same instance.
    """

    def __init__(self, store: Any) -> None:
        super().__init__(store)
        self.discover_calls: int = 0
        self.verify_calls: int = 0

    def discover(
        self,
        key: CapabilityKey,
        engine: Any,
        backend: GenerationBackend,
    ) -> ModelProfile:
        self.discover_calls += 1
        return super().discover(key, engine, backend)

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
