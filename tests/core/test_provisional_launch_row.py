"""Behavior: no launch is invisible, on any provider.

Finding F12: every durable ledger write happened in an ``on_instance_created``
callback, so a kill during the multi-minute create left a billing resource no
kinoforge command could see. Brief 1 fixed it inside the SkyPilot provider;
S5 makes it a property of the orchestrator, which is the only place that knows
a create is about to happen on ANY provider.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

# Import providers/engines/sources so they self-register for deploy_session.
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401 — registers https:// source
from kinoforge.core import orchestrator
from kinoforge.core.errors import CapacityError, ProvisionFailed
from kinoforge.core.interfaces import Instance, InstanceSpec
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.orchestrator import (
    _provision_instance_and_build_backend,
    deploy_session,
)
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore

# Reuse the scaffolding the orchestrator tests already drive these seams with,
# rather than standing up a parallel set that could drift from the real call
# contract. ``fake_engine`` / ``fake_provider`` are imported as fixtures, so
# pytest resolves them here too.
from tests.core.test_orchestrator import _compute_cfg, _make_engine
from tests.core.test_orchestrator_render_provision import (  # noqa: F401
    _make_cfg,
    fake_engine,
    fake_provider,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _ledger_ids(ledger: Ledger) -> list[str]:
    """Return the ids of every row currently in *ledger*, in on-disk order.

    Args:
        ledger: The ledger to read.

    Returns:
        The ``id`` of each entry. Order is load-bearing: ``Ledger.record``
        appends, so a duplicated write shows up as a repeated id.
    """
    return [str(entry["id"]) for entry in ledger.entries()]


def _provision_kwargs(
    *,
    engine: MagicMock,
    provider: MagicMock,
    run_id: str,
    ledger: Ledger,
    on_instance_created: Callable[[Instance], None] | None,
    tmp_path: Path,
) -> dict[str, Any]:
    """Build the keyword bundle ``_provision_instance_and_build_backend`` needs.

    Args:
        engine: The fake generation engine.
        provider: The fake compute provider.
        run_id: The client-side run id, which is also the provisional row key.
        ledger: The ledger the orchestrator writes the provisional row into.
        on_instance_created: The post-create callback, or None.
        tmp_path: Operator state root for this test.

    Returns:
        A kwargs dict ready to splat into the orchestrator helper.
    """
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    key = MagicMock()
    key.derive.return_value = "deadbeef"
    return {
        "resolved_engine": engine,
        "resolved_provider": provider,
        "cfg": _make_cfg(),
        "run_id": run_id,
        "key": key,
        "creds": creds,
        "store": MagicMock(),
        "state_dir": tmp_path,
        "for_discovery": False,
        "on_instance_created": on_instance_created,
        "provisional_ledger": ledger,
        "capacity_wait_s": 0.0,
    }


def test_row_exists_while_create_is_in_flight(tmp_path: Path) -> None:
    """The row is visible from INSIDE create_instance.

    Bug caught: writing the row after create returns — which is exactly the
    hole F12 names, and which a test asserting only the end state cannot see.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)

    row_id = orchestrator._record_provisional_row(
        ledger=ledger,
        run_id="kf-run-1",
        provider_name="runpod",
        tags={"kinoforge_engine": "diffusers"},
        max_age_s=3600,
        now=1_700_000_000.0,
    )

    assert row_id == "kf-run-1"
    entry = ledger.read("kf-run-1")
    assert entry is not None
    assert entry["provider"] == "runpod"
    assert entry["tags"]["kf_launch_phase"] == "launching"
    assert entry["tags"]["kf_run_id"] == "kf-run-1"
    assert entry["tags"]["kinoforge_engine"] == "diffusers"
    assert float(entry["tags"]["kf_launched_at"]) == 1_700_000_000.0


def test_empty_run_id_writes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No key, no row — an unkeyed row is unfindable and unforgettable.

    Bug caught: writing ``id=""`` produces a row every reconcile pass trips
    over and no destroy can act on.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path))
    with caplog.at_level("WARNING"):
        assert (
            orchestrator._record_provisional_row(
                ledger=ledger,
                run_id="",
                provider_name="runpod",
                tags={},
                max_age_s=60,
                now=0.0,
            )
            is None
        )
    assert ledger.entries() == []


def test_a_ledger_fault_never_fails_the_launch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bookkeeping cannot break a launch that would have succeeded.

    Bug caught: a store 5xx or lock-lease timeout turning a healthy launch
    into a failure — the discipline carried over from
    providers/skypilot/__init__.py:1051-1070.
    """

    class _AngryLedger:
        def record(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("store unavailable")

    with caplog.at_level("WARNING"):
        assert (
            orchestrator._record_provisional_row(
                ledger=_AngryLedger(),
                run_id="kf-run-2",
                provider_name="modal",
                tags={},
                max_age_s=60,
                now=0.0,
            )
            is None
        )
    assert "kf-run-2" in caplog.text
    # The diagnosis is the point of the log line: a WARNING carrying the
    # traceback. Dropping either — logging at DEBUG, or losing exc_info — turns
    # a silent forfeiture of F12 protection into something unattributable.
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None


def test_forget_is_best_effort(caplog: pytest.LogCaptureFixture) -> None:
    """A failing forget logs and returns.

    Bug caught: the success path raising AFTER the instance is live, so the
    caller never reaches the orchestrator's post-create record — the exact
    hazard documented at providers/skypilot/__init__.py:1100-1112.
    """

    class _AngryLedger:
        def forget(self, instance_id: str) -> None:
            raise RuntimeError("store unavailable")

    with caplog.at_level("WARNING"):
        orchestrator._forget_provisional_row(_AngryLedger(), "kf-run-3")
    assert "kf-run-3" in caplog.text
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None


def test_success_records_the_real_row_before_forgetting_the_provisional(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
) -> None:
    """Order matters: real row first, then forget.

    Bug caught: forgetting first leaves a window in which a kill loses BOTH
    rows, which is the state F12 exists to make impossible.

    Drives the real orchestrator with a provider whose ``create_instance``
    hands back a SERVER-assigned id (``pod-abc`` != ``kf-run-9`` — runpod /
    modal semantics), so the provisional row and the real row are two
    distinct keys and their write order is observable.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path / "ledger-root"))
    observed: list[list[str]] = []
    created = Instance(
        id="pod-abc",
        provider="fakeprovider",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://pod-abc-8000"},
    )
    in_flight_row: dict[str, Any] = {}

    def _create(spec: InstanceSpec) -> Instance:
        del spec
        observed.append(_ledger_ids(ledger))
        in_flight_row.update(ledger.read("kf-run-9") or {})
        return created

    fake_provider.create_instance.side_effect = _create

    def _record_real_row(inst: Instance) -> None:
        # Stand-in for deploy_session's ``_record_then_install``.
        ledger.record(inst)
        observed.append(_ledger_ids(ledger))

    _provision_instance_and_build_backend(
        **_provision_kwargs(
            engine=fake_engine,
            provider=fake_provider,
            run_id="kf-run-9",
            ledger=ledger,
            on_instance_created=_record_real_row,
            tmp_path=tmp_path,
        )
    )
    observed.append(_ledger_ids(ledger))

    assert observed == [["kf-run-9"], ["kf-run-9", "pod-abc"], ["pod-abc"]]
    # The row visible mid-create is a full, actionable row — a reconciler that
    # finds it can name the provider it has to ask about.
    assert in_flight_row["provider"] == "fakeprovider"
    assert in_flight_row["tags"]["kf_launch_phase"] == "launching"
    assert in_flight_row["tags"]["kf_run_id"] == "kf-run-9"


def test_create_failure_removes_the_provisional_row(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
) -> None:
    """A failed launch leaves no ghost.

    Bug caught: a permanent row whose est_spend (age×rate) inflates forever —
    the "$210 phantom pod" failure mode cli/_reconcile.py documents.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path / "ledger-root"))
    boom = RuntimeError("provider create exploded")
    seen_mid_create: list[list[str]] = []

    def _create(spec: InstanceSpec) -> Instance:
        del spec
        seen_mid_create.append(_ledger_ids(ledger))
        raise boom

    fake_provider.create_instance.side_effect = _create
    created_rows: list[Instance] = []

    with pytest.raises(RuntimeError) as excinfo:
        _provision_instance_and_build_backend(
            **_provision_kwargs(
                engine=fake_engine,
                provider=fake_provider,
                run_id="kf-run-8",
                ledger=ledger,
                on_instance_created=created_rows.append,
                tmp_path=tmp_path,
            )
        )

    # The exception propagates unchanged — the cleanup must not swallow or
    # rewrap the reason the launch failed.
    assert excinfo.value is boom
    # The row WAS there while the create was running...
    assert seen_mid_create == [["kf-run-8"]]
    # ...and is gone once it failed, with no real row ever written.
    assert ledger.entries() == []
    assert created_rows == []


def test_capacity_retry_writes_the_row_once(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retries do not append duplicate rows.

    Bug caught: Ledger.record APPENDS (core/lifecycle.py:592), so a row
    written inside the retry loop yields N rows for one launch and every
    reader picks whichever it finds first.
    """
    real_wait = orchestrator._create_with_capacity_wait

    def _instant_wait(**kwargs: Any) -> Any:
        # The real retry loop, minus the 25 s sleep between attempts.
        return real_wait(**kwargs, retry_interval_s=0.0, sleep=lambda _s: None)

    monkeypatch.setattr(orchestrator, "_create_with_capacity_wait", _instant_wait)

    ledger = Ledger(store=LocalArtifactStore(tmp_path / "ledger-root"))
    observed: list[list[str]] = []
    attempts = {"n": 0}
    created = Instance(
        id="pod-xyz",
        provider="fakeprovider",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://pod-xyz-8000"},
    )

    def _create(spec: InstanceSpec) -> Instance:
        del spec
        attempts["n"] += 1
        observed.append(_ledger_ids(ledger))
        if attempts["n"] < 3:
            raise CapacityError("no capacity yet")
        return created

    fake_provider.create_instance.side_effect = _create

    kwargs = _provision_kwargs(
        engine=fake_engine,
        provider=fake_provider,
        run_id="kf-run-7",
        ledger=ledger,
        on_instance_created=ledger.record,
        tmp_path=tmp_path,
    )
    kwargs["capacity_wait_s"] = 60.0
    _provision_instance_and_build_backend(**kwargs)

    assert attempts["n"] == 3
    # One row, unchanged, across all three attempts — not two by attempt 2.
    assert observed == [["kf-run-7"], ["kf-run-7"], ["kf-run-7"]]
    # And the successful launch still ends with exactly the real row.
    assert _ledger_ids(ledger) == ["pod-xyz"]


def test_the_row_survives_when_the_real_row_never_landed(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
) -> None:
    """No real row, no forget — the live instance stays visible.

    Bug caught: forgetting unconditionally after ``on_instance_created``
    RETURNS. The callback is optional (defaults to None), and
    deploy_session's ``_record_then_install`` catches its own
    ``ledger.record`` failure, logs a warning and returns normally. In either
    case an unconditional forget deletes the ONLY durable handle on an
    instance that is already live and billing — which is the exact state F12
    exists to make impossible, reintroduced on the success path.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path / "ledger-root"))
    fake_provider.create_instance.return_value = Instance(
        id="pod-abc",
        provider="fakeprovider",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://pod-abc-8000"},
    )

    _provision_instance_and_build_backend(
        **_provision_kwargs(
            engine=fake_engine,
            provider=fake_provider,
            run_id="kf-run-6",
            ledger=ledger,
            # Stands in for both real cases: no callback at all, and a
            # callback that swallowed its own ledger.record failure.
            on_instance_created=None,
            tmp_path=tmp_path,
        )
    )

    assert _ledger_ids(ledger) == ["kf-run-6"]
    entry = ledger.read("kf-run-6")
    assert entry is not None
    assert entry["tags"]["kf_launch_phase"] == "launching"


# ---------------------------------------------------------------------------
# The same-key shape (SkyPilot: cluster name == run_id). ``Ledger.forget``
# drops EVERY row whose id matches and ``Ledger.record`` appends, so this shape
# has exactly two ways to go wrong and one correct outcome. Both failure
# directions are pinned separately, because a fix for one is a plausible cause
# of the other.
# ---------------------------------------------------------------------------


def _same_key_instance(run_id: str) -> Instance:
    """Build the real Instance a same-key provider hands back.

    Carries fields the provisional row provably does not — live endpoints and
    a non-zero rate — so a test can tell WHICH row survived rather than only
    how many did.

    Args:
        run_id: The client-side run id, which is also the provider's id here.

    Returns:
        The instance a SkyPilot-shaped ``create_instance`` would return.
    """
    return Instance(
        id=run_id,
        provider="fakeprovider",
        status="ready",
        created_at=0.0,
        endpoints={"8000": f"http://127.0.0.1:8000/{run_id}"},
        tags={"ports": "8000"},
        cost_rate_usd_per_hr=2.5,
    )


def test_the_same_key_collapse_leaves_exactly_one_row(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
) -> None:
    """One launch, one row — even when both rows share a key.

    Bug caught: skipping the collapse (Task 4's stopgap did exactly that, to
    avoid deleting both rows) leaves the provisional and the real row stacked
    under one cluster name. ``kinoforge list`` then shows one cluster twice,
    ``est_spend`` is double-counted, and every per-id reader — ``Ledger.read``,
    ``cli/_reconcile`` — silently picks whichever it finds first, which is the
    stale ``launching`` row.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path / "ledger-root"))
    fake_provider.create_instance.return_value = _same_key_instance("kf-run-5")

    _provision_instance_and_build_backend(
        **_provision_kwargs(
            engine=fake_engine,
            provider=fake_provider,
            run_id="kf-run-5",
            ledger=ledger,
            on_instance_created=ledger.record,
            tmp_path=tmp_path,
        )
    )

    assert _ledger_ids(ledger) == ["kf-run-5"]


def test_the_same_key_collapse_keeps_the_real_row_not_the_provisional(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
) -> None:
    """The survivor is the REAL row, and there is always at least one.

    Bug caught (zero rows): collapsing with a plain ``Ledger.forget(run_id)``.
    It drops every row with that id, so on the same-key shape it takes the real
    row with it and a live, billing cluster becomes invisible to every
    kinoforge command — the exact state F12 exists to make impossible.

    Bug caught (wrong row): collapsing by forgetting the provisional row BEFORE
    the real one is recorded, then having ``on_instance_created`` swallow its
    own write. The count would look right at one, but the surviving row would
    be the ``launching`` stub with no endpoints and a 0.0 rate.

    The expected values come from the Instance the provider returns, not from
    re-running the writer: endpoints and a 2.5/hr rate exist only on the real
    row, and ``kf_launch_phase`` exists only on the provisional one.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path / "ledger-root"))
    fake_provider.create_instance.return_value = _same_key_instance("kf-run-5")

    _provision_instance_and_build_backend(
        **_provision_kwargs(
            engine=fake_engine,
            provider=fake_provider,
            run_id="kf-run-5",
            ledger=ledger,
            on_instance_created=ledger.record,
            tmp_path=tmp_path,
        )
    )

    entry = ledger.read("kf-run-5")
    assert entry is not None, "zero rows for a live instance"
    assert entry["endpoints"] == {"8000": "http://127.0.0.1:8000/kf-run-5"}
    assert entry["cost_rate_usd_per_hr"] == 2.5
    assert "kf_launch_phase" not in entry["tags"]


def test_the_same_key_row_survives_when_the_real_row_never_landed(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
) -> None:
    """No real row under the shared key means no collapse.

    Bug caught: collapsing unconditionally on the same-key shape. The real
    write is not guaranteed — ``on_instance_created`` is optional and
    deploy_session's ``_record_then_install`` swallows its own ``ledger.record``
    failure — so an unconditional drop of the ``launching`` row deletes the only
    durable handle on a cluster that is already up and billing.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path / "ledger-root"))
    fake_provider.create_instance.return_value = _same_key_instance("kf-run-4")

    _provision_instance_and_build_backend(
        **_provision_kwargs(
            engine=fake_engine,
            provider=fake_provider,
            run_id="kf-run-4",
            ledger=ledger,
            # Stands in for both real cases: no callback at all, and a callback
            # that swallowed its own ledger.record failure.
            on_instance_created=None,
            tmp_path=tmp_path,
        )
    )

    assert _ledger_ids(ledger) == ["kf-run-4"]
    entry = ledger.read("kf-run-4")
    assert entry is not None
    assert entry["tags"]["kf_launch_phase"] == "launching"


def test_forget_provisional_drops_only_the_launching_row(tmp_path: Path) -> None:
    """``Ledger.forget_provisional`` is id-AND-phase scoped, not id scoped.

    Bug caught: implementing the collapse as ``forget(provisional_id)``, which
    matches on id alone and therefore removes the real row too whenever the two
    share a key. Also catches a "drop the first match" implementation: the rows
    are recorded provisional-first here, so dropping by position would look
    correct, and the assertion on the SURVIVING row's rate is what tells them
    apart.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path))
    ledger.record(
        Instance(
            id="kf-same",
            provider="skypilot",
            status="starting",
            created_at=0.0,
            tags={"kf_launch_phase": "launching", "kf_run_id": "kf-same"},
        )
    )
    ledger.record(_same_key_instance("kf-same"))

    assert ledger.forget_provisional("kf-same", real_id="kf-same") is True
    remaining = ledger.entries()
    assert len(remaining) == 1
    assert remaining[0]["cost_rate_usd_per_hr"] == 2.5


def test_forget_provisional_refuses_when_no_real_row_exists(tmp_path: Path) -> None:
    """With nothing to fall back on, the provisional row stays.

    Bug caught: dropping the row whenever the phase tag matches, regardless of
    whether a real row exists. That is the zero-row hole in miniature — the
    method is the single place the precondition and the delete happen under one
    lock, so a concurrent writer cannot slip between them.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path))
    ledger.record(
        Instance(
            id="kf-lonely",
            provider="skypilot",
            status="starting",
            created_at=0.0,
            tags={"kf_launch_phase": "launching"},
        )
    )

    assert ledger.forget_provisional("kf-lonely", real_id="kf-lonely") is False
    assert _ledger_ids(ledger) == ["kf-lonely"]


def test_a_collapse_fault_never_fails_the_launch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing collapse logs and returns — the instance is already live.

    Bug caught: letting a store 5xx or lock-lease timeout from the collapse
    propagate. At that point the instance is created, ready and billing, so
    raising would turn a successful launch into a failure and the caller would
    never receive the handle it needs to destroy it.
    """

    class _AngryLedger:
        def forget_provisional(self, provisional_id: str, *, real_id: str) -> bool:
            raise RuntimeError("store unavailable")

    with caplog.at_level("WARNING"):
        orchestrator._collapse_provisional_row(_AngryLedger(), "kf-run-c", "kf-run-c")
    assert "kf-run-c" in caplog.text
    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert record.exc_info is not None


# ---------------------------------------------------------------------------
# Moved from tests/providers/test_skypilot.py (S5 Task 5). These claims were
# written against the provider's private writer; the writer is gone, so each
# one is re-stated against the orchestrator. They are kept rather than deleted
# because the hazards they name — write-before-create, a row rich enough to act
# on, a create that raises, no ledger at all, and the real Ledger satisfying the
# duck-typed surface — are properties of the seam, not of SkyPilot.
# ---------------------------------------------------------------------------


def test_the_row_is_recorded_before_create_instance_is_entered(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
) -> None:
    """Record strictly precedes create, observed on one shared sequence.

    Moved from ``test_ledger_row_is_written_before_launch``. A shared list
    across both seams is used rather than mock call counts because the ORDER is
    the whole claim.

    Bug caught: moving the write below ``_create_with_capacity_wait`` — every
    end-state assertion in this file would still pass, while a SIGKILL inside
    the multi-minute create would again leave a billing resource nothing can
    see.
    """
    sequence: list[str] = []

    class _SequencingLedger(Ledger):
        def record(self, instance: Instance, **kwargs: Any) -> None:
            sequence.append("record")
            super().record(instance, **kwargs)

    ledger = _SequencingLedger(store=LocalArtifactStore(tmp_path / "ledger-root"))

    def _create(spec: InstanceSpec) -> Instance:
        del spec
        sequence.append("create")
        return _same_key_instance("kf-run-order")

    fake_provider.create_instance.side_effect = _create

    _provision_instance_and_build_backend(
        **_provision_kwargs(
            engine=fake_engine,
            provider=fake_provider,
            run_id="kf-run-order",
            ledger=ledger,
            on_instance_created=None,
            tmp_path=tmp_path,
        )
    )

    assert sequence == ["record", "create"]


def test_the_row_carries_enough_to_find_and_destroy_the_resource(
    tmp_path: Path,
) -> None:
    """The row names the resource, its provider, its run and its age limit.

    Moved from ``test_provisional_row_carries_enough_to_find_and_destroy``. The
    orchestrator cannot know the CLOUD (``kf_cloud``) or the watchdog deadline
    (``kf_deadline_epoch``) the SkyPilot writer used to add — those are provider
    facts — so the portable row carries the provider NAME instead, which is what
    a reconciler actually needs to know who to ask.

    Bug caught: a row with only an id. A later sweep could not tell which
    provider to reach, which run it belonged to, or when the reaper is allowed
    to age it out — so it could neither destroy it nor safely ignore it.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path))

    orchestrator._record_provisional_row(
        ledger=ledger,
        run_id="kf-provisional",
        provider_name="skypilot",
        tags={"kinoforge_engine": "wan"},
        max_age_s=3600,
        now=1_700_000_000.0,
    )

    entry = ledger.read("kf-provisional")
    assert entry is not None
    assert entry["provider"] == "skypilot"
    assert entry["max_age_s"] == 3600
    assert entry["tags"]["kf_launch_phase"] == "launching"
    assert entry["tags"]["kf_run_id"] == "kf-provisional"
    assert entry["tags"]["kinoforge_engine"] == "wan"
    assert float(entry["tags"]["kf_launched_at"]) == 1_700_000_000.0


def test_a_create_that_raises_after_the_resource_exists_keeps_nothing(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
) -> None:
    """A create that raises leaves no row — and that is now safe.

    The SkyPilot version of this test (test_tunnel_failure_keeps_the_
    provisional_row) asserted the OPPOSITE, because the provider raised
    ProvisionFailed *after* the cluster was up and only the provisional row
    could surface it. That is still true, so the provider's failure path must
    tear the cluster down itself before raising — asserted in
    tests/providers/test_skypilot_endpoints.py::
    test_second_spawn_failure_kills_the_first_tunnel_and_the_cluster.

    Bug caught: dropping BOTH the row and the teardown, which would restore
    the invisible-billing-cluster hole in a new place.
    """
    ledger = Ledger(store=LocalArtifactStore(tmp_path / "ledger-root"))
    boom = ProvisionFailed("failed to open ssh tunnel to 'kf-run-t' for port 8000")

    def _create(spec: InstanceSpec) -> Instance:
        del spec
        # Same shape as SkyPilot's tunnel failure: the cluster IS up by now and
        # create_instance has already best-effort torn it down.
        raise boom

    fake_provider.create_instance.side_effect = _create

    with pytest.raises(ProvisionFailed) as excinfo:
        _provision_instance_and_build_backend(
            **_provision_kwargs(
                engine=fake_engine,
                provider=fake_provider,
                run_id="kf-run-t",
                ledger=ledger,
                on_instance_created=None,
                tmp_path=tmp_path,
            )
        )

    assert excinfo.value is boom
    assert ledger.entries() == []


def test_no_ledger_is_a_no_op(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
) -> None:
    """``provisional_ledger=None`` provisions exactly as before.

    Moved from ``test_no_ledger_installed_is_a_no_op``. ``None`` is the state
    every hosted engine and every direct caller of this helper is in.

    Bug caught: an unconditional ledger call — ``None.record(...)`` would
    AttributeError and break every launch that does not supply a ledger.
    """
    kwargs = _provision_kwargs(
        engine=fake_engine,
        provider=fake_provider,
        run_id="kf-no-ledger",
        ledger=Ledger(store=LocalArtifactStore(tmp_path / "unused")),
        on_instance_created=None,
        tmp_path=tmp_path,
    )
    kwargs["provisional_ledger"] = None
    fake_provider.create_instance.return_value = _same_key_instance("kf-no-ledger")

    result = _provision_instance_and_build_backend(**kwargs)

    assert result.instance.id == "kf-no-ledger"


def test_the_real_ledger_satisfies_the_duck_typed_surface(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
) -> None:
    """A real ``Ledger`` — not a fake — round-trips the whole same-key path.

    Moved from ``test_ledger_protocol_matches_the_real_ledger``. The
    ``provisional_ledger`` parameter is typed ``Any`` and every call through it
    is wrapped in a best-effort ``except``, so a method name the real class does
    not have (``forget_provisional_row`` vs ``forget_provisional``) would be
    swallowed as a WARNING and leave a duplicate row. A fake ledger cannot catch
    that; only the real class can.

    Bug caught: drift between the orchestrator's call and ``Ledger``'s actual
    signature, which is invisible to mypy across an ``Any`` boundary.
    """
    store = LocalArtifactStore(tmp_path / "ledger-root")
    ledger = Ledger(store=store)
    fake_provider.create_instance.return_value = _same_key_instance("kf-real-ledger")

    _provision_instance_and_build_backend(
        **_provision_kwargs(
            engine=fake_engine,
            provider=fake_provider,
            run_id="kf-real-ledger",
            ledger=ledger,
            on_instance_created=ledger.record,
            tmp_path=tmp_path,
        )
    )

    # A FRESH Ledger over the same store: the collapse has to have been durable,
    # not just visible to the in-memory object that performed it.
    assert _ledger_ids(Ledger(store=store)) == ["kf-real-ledger"]


# ---------------------------------------------------------------------------
# deploy_session wiring — AC7 ("every provider gets this") rests on the two
# ``provisional_ledger=_provisional_ledger`` call sites. Deleting either one
# leaves every test above green while the feature is OFF in production.
# ---------------------------------------------------------------------------


class _LedgerPeekProvider(LocalProvider):
    """LocalProvider that reads the ledger from INSIDE ``create_instance``.

    Stands in for the SERVER-assigned-id providers (local / runpod / modal):
    its ``create_instance`` returns ``local-<uuid>``, which is not the
    ``run_id``, so the provisional row and the real row occupy two distinct
    ledger keys. ``_SameKeyProvider`` below covers the other shape.

    Attributes:
        seen: The row the orchestrator wrote for ``run_id``, as visible mid-
            create, or None when no row was there.
    """

    def __init__(self, ledger: Ledger, run_id: str) -> None:
        """Initialise the peeking provider.

        Args:
            ledger: A ledger over the same store the orchestrator writes to.
            run_id: The key the provisional row is expected under.
        """
        super().__init__()
        self._peek_ledger = ledger
        self._peek_run_id = run_id
        self.seen: dict[str, Any] | None = None

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Snapshot the provisional row, then create as usual.

        Args:
            spec: The instance specification.

        Returns:
            The instance LocalProvider would have created anyway.
        """
        self.seen = self._peek_ledger.read(self._peek_run_id)
        return super().create_instance(spec)


class _SameKeyProvider(LocalProvider):
    """LocalProvider that returns the run_id as its instance id, as SkyPilot does.

    A SkyPilot cluster name IS ``spec.run_id``, so the provisional row and the
    real row land on the same ledger key. Before S5 Task 5 this provider shape
    could not reach the orchestrator's writer at all: SkyPilot wrote its own row
    and ``deploy_session`` gated the orchestrator's writer off for it. With the
    gate and the private writer gone, this is the production shape.

    Attributes:
        rows_mid_create: Ledger ids visible from inside ``create_instance``.
    """

    def __init__(self, ledger: Ledger) -> None:
        """Initialise with a ledger over the session store.

        Args:
            ledger: A ledger reading the same store deploy_session writes to.
        """
        super().__init__()
        self._peek_ledger = ledger
        self.rows_mid_create: list[str] = []

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Snapshot the ledger, then return an instance keyed by the run id.

        Args:
            spec: The instance specification.

        Returns:
            The instance LocalProvider would have created, re-keyed onto
            ``spec.run_id`` so it collides with the provisional row.
        """
        self.rows_mid_create = [
            str(entry["id"]) for entry in self._peek_ledger.entries()
        ]
        instance = super().create_instance(spec)
        return dataclasses.replace(instance, id=spec.run_id or instance.id)


@pytest.mark.parametrize("warm_profile_cache", [False, True])
def test_deploy_session_writes_the_row_before_create(
    tmp_path: Path, warm_profile_cache: bool
) -> None:
    """deploy_session hands the ledger down on BOTH provision call sites.

    Bug caught: dropping ``provisional_ledger=_provisional_ledger`` at either
    ``_provision_instance_and_build_backend`` call site — the cache-miss
    (discovery) branch or the cache-hit branch — leaves every unit test above
    green while no production launch writes a row at all.

    Args:
        tmp_path: Per-test store root.
        warm_profile_cache: When True, a seed session populates the profile
            cache first, so the launch under test goes through the cache-HIT
            call site instead of the discovery one.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)

    if warm_profile_cache:
        with deploy_session(
            cfg,
            store=store,
            engine=_make_engine(),
            provider=LocalProvider(),
            run_id="seed",
        ):
            pass

    provider = _LedgerPeekProvider(Ledger(store=store), run_id="kf-deploy-1")
    with deploy_session(
        cfg,
        store=store,
        engine=_make_engine(),
        provider=provider,
        run_id="kf-deploy-1",
    ) as session:
        assert session.instance is not None

    assert provider.seen is not None, (
        "no provisional row was visible from inside create_instance — "
        "deploy_session did not pass provisional_ledger down"
    )
    assert provider.seen["tags"]["kf_launch_phase"] == "launching"
    assert provider.seen["tags"]["kf_run_id"] == "kf-deploy-1"
    assert provider.seen["provider"] == "local"


def test_deploy_session_leaves_one_real_row_for_a_same_key_provider(
    tmp_path: Path,
) -> None:
    """End to end, a SkyPilot-shaped launch ends with one row: the real one.

    Bug caught: deleting the Task-5 gate (which withheld the orchestrator's
    writer from providers that wrote their own row) without also collapsing the
    duplicate the same-key shape then produces. Every helper-level test can be
    green while a real ``kinoforge generate`` on SkyPilot leaves two rows per
    cluster — or, if the collapse is written as a plain forget, none at all.
    """
    cfg = _compute_cfg()
    store = LocalArtifactStore(tmp_path)
    provider = _SameKeyProvider(Ledger(store=store))

    with deploy_session(
        cfg,
        store=store,
        engine=_make_engine(),
        provider=provider,
        run_id="kf-deploy-2",
    ) as session:
        assert session.instance is not None
        assert session.instance.id == "kf-deploy-2"

    assert provider.rows_mid_create == ["kf-deploy-2"], (
        "exactly one provisional row must exist mid-create; the orchestrator "
        "is the only writer now"
    )
    ledger = Ledger(store=store)
    assert _ledger_ids(ledger) == ["kf-deploy-2"]
    entry = ledger.read("kf-deploy-2")
    assert entry is not None
    assert "kf_launch_phase" not in entry["tags"], (
        "the provisional stub outlived the real row — the collapse kept the wrong one"
    )
