"""``deploy_session`` lets its caller learn the instance the moment it exists (U28).

The orchestrator has had an ``on_instance_created`` hook since C29 — fired
exactly once inside ``_provision_instance_and_build_backend``, right after
``create_instance`` returns and BEFORE ``engine.provision`` runs, with an
instance that already carries its endpoints (``create_instance`` populates
them; the status poll deliberately preserves them). ``deploy_session`` builds
its own ``_record_then_install`` on top of that hook, and that function's
docstring already says "chain ``on_instance_created`` callbacks".

What was missing is the other half of the chain: no CALLER could supply one.
So the CLI, which owns the ``--ephemeral`` index row, could not learn the pod's
real id or endpoints until the whole orchestrator call returned — and on the
crash path the row exists for (a SIGKILL mid-generation) it never learned them
at all. That is **U28**, and it is why an idle ephemeral pod could not be
reaped: with ``row.endpoints`` empty the reaper never primes ``note_endpoints``,
so the orphan predicate has no utilisation reading and stays
conservative-on-ignorance at ``LIVE``.

These tests pin the caller-facing half. The CLI-facing half — that the row is
actually upgraded mid-run — is in ``tests/cli/test_ephemeral_index_timing.py``,
because a seam that works and a seam nobody passes are indistinguishable from
the orchestrator's side.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import kinoforge.engines.fake  # noqa: F401 — self-registers engine "fake"
import kinoforge.providers.local  # noqa: F401 — self-registers provider "local"
import kinoforge.sources.http  # noqa: F401 — registers the https:// source
from kinoforge.core.interfaces import GenerationJob, Instance
from kinoforge.core.orchestrator import deploy_session
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore
from tests.core.test_orchestrator import _compute_cfg, _make_engine


def test_a_caller_supplied_callback_receives_the_created_instance(
    tmp_path: Path,
) -> None:
    """The callback fires once, with the real instance.

    Bug caught: accepting ``on_instance_created`` and never wiring it. A
    parameter that is present in the signature and absent in effect is the
    failure mode this codebase named in B4 — "an unused param is the next lie"
    — and here it would be worse than cosmetic: every caller would believe it
    had a mid-flight handle on a billing pod and would in fact have nothing.
    """
    seen: list[Instance] = []

    with deploy_session(
        _compute_cfg(),
        store=LocalArtifactStore(tmp_path),
        engine=_make_engine(),
        provider=LocalProvider(),
        run_id="r",
        on_instance_created=seen.append,
    ) as session:
        assert session.instance is not None
        created = session.instance

    assert len(seen) == 1, f"expected exactly one call, got {len(seen)}"
    assert seen[0].id == created.id
    # The endpoints are the whole point: an id alone cannot be probed for
    # utilisation, which is what leaves the reaper conservative-on-ignorance.
    assert seen[0].endpoints == created.endpoints


def test_the_callback_fires_before_the_engine_runs(tmp_path: Path) -> None:
    """Timing, not presence — this is U28's crux.

    Bug caught: chaining the caller's callback at the END of the session (or
    anywhere after the work). That satisfies the test above completely and
    fixes NOTHING: the row is still empty for the whole window a SIGKILL can
    land in, which is the only window it exists for. The pre-fix code is
    exactly this shape — the CLI's settle runs after the orchestrator returns.

    Asserting "fired before the engine dispatched a job" is the strongest
    observable proxy available from inside a session: ``engine.provision`` and
    every generation run strictly after the hook's documented fire point.
    """
    fired_before_dispatch: list[bool] = []
    seen: list[Instance] = []

    with deploy_session(
        _compute_cfg(),
        store=LocalArtifactStore(tmp_path),
        engine=_make_engine(),
        provider=LocalProvider(),
        run_id="r",
        on_instance_created=seen.append,
    ) as session:
        # First observable moment of real work inside the session.
        fired_before_dispatch.append(bool(seen))
        session.pool.submit(GenerationJob(spec={}, params={}, segments=[])).result(
            timeout=2.0
        )

    assert fired_before_dispatch == [True], (
        "the callback must fire before any work runs; firing it afterwards "
        "leaves the caller blind for exactly the window a kill can land in"
    )


def test_a_raising_callback_does_not_abort_the_launch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A caller's bookkeeping failure must not kill a live, billing pod.

    Bug caught: letting the callback's exception propagate. The pod is already
    created and already billing by the time this fires, so an unhandled raise
    turns a recoverable bookkeeping problem (a full disk, a read-only state
    dir) into an aborted run on hardware the operator is paying for — strictly
    worse than the gap being fixed.

    Not an invented rule: the sibling callback ``_record_then_install``
    already swallows and warns on its own ``ledger.record`` failure, for the
    same reason. Silence is not acceptable either, so the warning is asserted.
    """

    def _explode(_: Instance) -> None:
        raise RuntimeError("index write failed")

    with caplog.at_level(logging.WARNING, logger="kinoforge.orchestrator"):
        with deploy_session(
            _compute_cfg(),
            store=LocalArtifactStore(tmp_path),
            engine=_make_engine(),
            provider=LocalProvider(),
            run_id="r",
            on_instance_created=_explode,
        ) as session:
            # The launch survived: there is a usable session on the far side.
            assert session.instance is not None
            session.pool.submit(GenerationJob(spec={}, params={}, segments=[])).result(
                timeout=2.0
            )

    assert any("index write failed" in r.getMessage() for r in caplog.records), (
        "swallowing the failure silently is the other half of the bug: the "
        "operator must be able to find out the row was never upgraded"
    )
