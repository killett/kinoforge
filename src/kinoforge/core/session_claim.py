"""B7: cooperative session-claim lock helper.

Closes the deploy_session-vs-sweep race (Layer V spec §5 Risk 3) by
extending the existing ``provision:<id>`` lock's scope from
"engine.provision only" to "instance-id committed through first
heartbeat tick lands". The reaper non-blocking-probes the same key
before destroying — see ``core/reaper_actor.py:act_on_verdict``.

Sentinel-gate honesty: this module reads ``heartbeat_thread_tick`` for
a RELEASE-decision, not a destructive decision. ``classify`` remains
the single place gating destructive verdicts (Layer U §3.4 forward-
compat contract).
"""

from __future__ import annotations

import logging
import time as _time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from kinoforge.core.clock import Clock, RealClock
from kinoforge.core.errors import KinoforgeError
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.base import ArtifactStore

_log = logging.getLogger(__name__)


class FirstTickTimeout(KinoforgeError):
    """Raised when the HeartbeatLoop did not record a tick within ``timeout_s``.

    The orchestrator's cold-path teardown surface should catch this and
    destroy the orphaned instance before re-raising — same shape as the
    ``CapabilityMismatch`` teardown branch already present in
    ``deploy_session.__enter__``.
    """


@contextmanager
def hold_until_first_tick(
    *,
    store: ArtifactStore,
    instance_id: str,
    ledger: Ledger,
    ttl_s: float,
    timeout_s: float,
    poll_interval_s: float = 0.05,
    clock: Clock | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Iterator[None]:
    """Hold ``provision:<instance_id>`` until first heartbeat_thread_tick lands.

    Contract:

      1. Acquires ``store.acquire_lock(f"provision:{instance_id}", ttl_s=ttl_s)``
         blocking. Lock release happens in the outer ``with`` regardless of
         which exit path the body takes.
      2. Records ``start = clock.now()`` (Clock seam — NOT ``time.time()``).
      3. Yields to the caller — caller runs ``engine.provision``, builds
         backend, starts HeartbeatLoop, etc.
      4. After the yielded block exits cleanly: polls
         ``ledger.read(instance_id)`` and reads
         ``entry.get("heartbeat_thread_tick", 0.0)`` (with ``entry=None``
         treated as ``0.0``) at ``poll_interval_s`` cadence. Returns when
         the tick value is ``>= start``. Raises ``FirstTickTimeout`` when
         ``timeout_s`` elapses without a fresh tick.
      5. If the yielded block raises, propagate unchanged — the lock
         releases via the outer ``with``, the polling step is skipped.

    Args:
        store: Artifact store providing the cross-process lock.
        instance_id: Instance id to claim — used both as the lock-key
            suffix and as the ledger lookup key.
        ledger: Ledger whose ``read`` is polled for ``heartbeat_thread_tick``.
        ttl_s: Lock TTL recorded in the sidecar JSON. Callers MUST size
            this larger than the worst-case held duration (cf. spec D2:
            ``cfg.lifecycle().boot_timeout_s + 2*heartbeat_interval_s``).
        timeout_s: Polling budget — when exhausted without a fresh tick,
            raises ``FirstTickTimeout``.
        poll_interval_s: Sleep cadence between polls. Default 0.05s gives
            ~20 reads/sec — local-store overhead is negligible.
        clock: Wall-clock source. Defaults to :class:`RealClock`. Tests
            inject :class:`FakeClock` for determinism.
        sleep: Test seam — defaults to ``time.sleep``. Tests inject a
            spy/no-op to bypass real wall-clock waits.

    Yields:
        ``None`` — the body of the ``with`` block is the caller's
        engine.provision + HeartbeatLoop.start critical section.

    Raises:
        FirstTickTimeout: ``timeout_s`` elapsed before the ledger's
            ``heartbeat_thread_tick`` for ``instance_id`` exceeded ``start``.

    Hosted-edge: ``ledger.read`` returning ``None`` indefinitely means
    the caller never recorded the instance (test-substrate edge). The
    helper raises ``FirstTickTimeout`` at ``timeout_s`` — same loud
    failure shape as a crashed HeartbeatLoop. Production callers route
    the hosted-path branch through ``contextlib.nullcontext`` and never
    enter this helper.
    """
    _clock: Clock = clock if clock is not None else RealClock()
    _sleep = sleep if sleep is not None else _time.sleep
    with store.acquire_lock(f"provision:{instance_id}", ttl_s=ttl_s):
        start = _clock.now()
        try:
            yield
        except BaseException:
            raise
        deadline = start + timeout_s
        while True:
            entry = ledger.read(instance_id)
            tick = 0.0
            if entry is not None:
                raw_tick = entry.get("heartbeat_thread_tick", 0.0)
                try:
                    tick = float(raw_tick)
                except (TypeError, ValueError):
                    tick = 0.0
            if tick >= start:
                _log.debug(
                    "session-claim released for %s (tick=%.3f >= start=%.3f)",
                    instance_id,
                    tick,
                    start,
                )
                return
            if _clock.now() >= deadline:
                raise FirstTickTimeout(
                    f"no heartbeat tick for {instance_id!r} within {timeout_s}s "
                    f"(start={start:.3f}, last_tick={tick:.3f})"
                )
            _sleep(poll_interval_s)
