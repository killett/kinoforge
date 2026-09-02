"""Behavior: no launch is invisible, on any provider.

Finding F12: every durable ledger write happened in an ``on_instance_created``
callback, so a kill during the multi-minute create left a billing resource no
kinoforge command could see. Brief 1 fixed it inside the SkyPilot provider;
S5 makes it a property of the orchestrator, which is the only place that knows
a create is about to happen on ANY provider.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core import orchestrator
from kinoforge.core.errors import CapacityError
from kinoforge.core.interfaces import Instance, InstanceSpec
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.orchestrator import _provision_instance_and_build_backend
from kinoforge.stores.local import LocalArtifactStore

# Reuse the orchestrator fakes the provision tests already drive
# ``_provision_instance_and_build_backend`` with, rather than standing up a
# parallel set that could drift from the real call contract. Imported as
# fixtures, so pytest resolves ``fake_engine`` / ``fake_provider`` here too.
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
