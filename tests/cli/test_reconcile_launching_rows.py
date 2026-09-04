"""Behavior: a launching row is adopted or aged out — never blind-deleted.

The hazard S5 introduces and this closes: the provisional row is keyed by the
CLIENT-side id (run_id). On RunPod that is the pod NAME, so
``provider.get_instance(run_id)`` KeyErrors even when the pod exists — and the
old reconciler forgets a row on KeyError. That would delete the only durable
handle on a pod that is very much billing.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from kinoforge.core.interfaces import Instance


class _FakeLedger:
    """A ledger with BOTH delete doors, so which one is used is observable.

    ``forget_provisional`` is not optional scenery: the production age-out
    prefers it over ``forget`` (a plain ``forget`` matches on id alone and, on
    the same-key SkyPilot shape, would take a live cluster's real row with the
    ghost). A fake carrying only ``forget`` would make a regression to the
    blunt door invisible — the reconciler probes for the scoped one with
    ``getattr`` and silently falls back.

    Attributes:
        forgotten: Ids deleted through EITHER door, in call order.
        scoped_forgets: Ids deleted through the phase-scoped door only.
        recorded: Instances written.
        record_kwargs: The keyword arguments each ``record`` was given.
    """

    def __init__(self, entries: list[dict]) -> None:  # type: ignore[type-arg]
        self.entries_ = entries
        self.forgotten: list[str] = []
        self.scoped_forgets: list[str] = []
        self.recorded: list[Instance] = []
        self.record_kwargs: list[dict[str, object]] = []

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)

    def forget_provisional(
        self, provisional_id: str, *, real_id: str | None = None
    ) -> bool:
        del real_id
        self.scoped_forgets.append(provisional_id)
        self.forgotten.append(provisional_id)
        return True

    def record(self, instance: Instance, **kwargs: object) -> None:
        self.recorded.append(instance)
        self.record_kwargs.append(dict(kwargs))


class _CollapsingLedger:
    """A ledger with the real :class:`Ledger`'s adopt-safely surface.

    Records every mutation in ONE ordered log, because the order is the
    contract: ``forget_provisional(real_id=...)`` refuses unless a
    non-provisional row already exists under *real_id*, so the record must
    land first or the collapse silently no-ops.
    """

    def __init__(self, rows: dict[str, dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._rows = rows or {}

    def record(self, instance: Instance, **kwargs: object) -> None:
        self.calls.append(("record", instance.id))

    def forget(self, instance_id: str) -> None:
        self.calls.append(("forget", instance_id))

    def forget_provisional(
        self, provisional_id: str, *, real_id: str | None = None
    ) -> bool:
        self.calls.append(("forget_provisional", provisional_id, str(real_id)))
        return True

    def read(self, instance_id: str) -> dict[str, Any] | None:
        return self._rows.get(instance_id)


class _ScanningLedger(_CollapsingLedger):
    """A ledger with ``entries()`` — the real :class:`Ledger`'s full surface.

    ``read`` deliberately mirrors the real one: it returns the FIRST row for an
    id, which on the same-key shape is the provisional one.
    """

    def __init__(self, all_rows: list[dict[str, Any]]) -> None:
        super().__init__()
        self._all = all_rows

    def entries(self) -> list[dict[str, Any]]:
        return list(self._all)

    def read(self, instance_id: str) -> dict[str, Any] | None:
        for row in self._all:
            if row.get("id") == instance_id:
                return row
        return None


def _launching_row(*, age_s: float, now: float) -> dict:  # type: ignore[type-arg]
    return {
        "id": "kf-run-42",
        "provider": "runpod",
        "created_at": now - age_s,
        "tags": {"kf_launch_phase": "launching", "kf_run_id": "kf-run-42"},
    }


class _ProviderWithPod:
    def get_instance(self, instance_id: str) -> Instance:
        raise KeyError(instance_id)

    def list_instances(self) -> list[Instance]:
        return [
            Instance(
                id="pod-real-1",
                provider="runpod",
                status="ready",
                created_at=0.0,
                endpoints={},
                tags={"name": "kf-run-42", "mode": "pod"},
                cost_rate_usd_per_hr=1.5,
            )
        ]


class _ProviderWithNothing(_ProviderWithPod):
    def list_instances(self) -> list[Instance]:
        return []


class _ProviderWithSameKeyCluster(_ProviderWithPod):
    """SkyPilot's shape: the cluster name IS the run id, so the ids coincide."""

    def list_instances(self) -> list[Instance]:
        return [
            Instance(
                id="kf-run-42",
                provider="runpod",
                status="ready",
                created_at=0.0,
                endpoints={},
                tags={},
                cost_rate_usd_per_hr=2.0,
            )
        ]


class _ProviderThatCannotList(_ProviderWithPod):
    def list_instances(self) -> list[Instance]:
        raise RuntimeError("runpod graphql 500")


def test_a_launching_row_is_adopted_onto_the_real_id() -> None:
    """An ABANDONED launch's pod exists under the run_id NAME → row is usable.

    Bug caught: forgetting the row and leaving a live RunPod pod with no
    ledger entry — F12 reopened one layer down.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    entries = [_launching_row(age_s=7200.0, now=now)]

    _reconcile_dead_ledger_entries(
        ledger, entries, get_provider=lambda _n: _ProviderWithPod, now=now
    )

    assert ledger.forgotten == ["kf-run-42"]
    assert [i.id for i in ledger.recorded] == ["pod-real-1"]


def test_a_young_launching_row_with_no_pod_is_left_alone() -> None:
    """A create still in flight is not reconciled away.

    Bug caught: a concurrent ``kinoforge list`` during a 10-minute boot
    deleting the row that protects that very boot.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=30.0, now=now)],
        get_provider=lambda _n: _ProviderWithNothing,
        now=now,
    )
    assert ledger.forgotten == []


def test_a_young_launching_row_with_a_name_match_is_left_untouched() -> None:
    """A LIVE launch's own row is not adopted out from under it.

    The provisional row is written above the capacity-wait loop and collapsed
    only after ``create_instance`` returns. On SkyPilot that call blocks
    through all of ``sky.launch`` while ``sky.status()`` already lists the
    cluster in INIT, so a concurrent ``kinoforge list`` CAN match the row.

    Bug caught: adopting there records a listing-derived row and collapses
    the provisional one; the orchestrator's own ``record`` then APPENDS a
    second row under that id and its collapse finds nothing. Since the
    reconciler's row landed first and ``Ledger.read`` returns the first
    match, warm-attach and est_spend go on reading a mid-INIT row with empty
    endpoints, no warm_attach_key and no lifecycle snapshot — and the
    overview double-counts the spend.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _CollapsingLedger()
    entries = [_launching_row(age_s=30.0, now=now)]

    gone = _reconcile_dead_ledger_entries(
        ledger, entries, get_provider=lambda _n: _ProviderWithPod, now=now
    )

    assert gone == []
    assert ledger.calls == []
    assert entries[0]["id"] == "kf-run-42"


def test_an_aged_launching_row_with_no_pod_is_forgotten() -> None:
    """A launch that never produced anything eventually stops haunting list.

    Bug caught: a permanent ghost row whose est_spend inflates forever.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithNothing,
        now=now,
    )
    assert ledger.forgotten == ["kf-run-42"]


def test_the_adopted_row_keeps_the_launch_time_not_the_listings_zero() -> None:
    """A durable adopted row must not be born 56 years old.

    Both listing converters hard-code ``created_at=0.0`` — RunPod's list API
    returns no creation time (``providers/runpod/__init__.py``) and SkyPilot's
    record does the same. Recording that verbatim gives the adopted row an age
    of ~56 years, so ``Lifecycle`` max-age reaping destroys EVERY adopted
    instance unconditionally (``core/lifecycle.py``) and the overview renders a
    six-figure ``est≤$`` for RunPod, whose listing DOES carry a real
    ``costPerHr``.

    Bug caught: ``record(instance)`` straight off ``list_instances()``. The
    caller-side ``_rewrite_adopted_entry`` fixes only the in-memory copy, so
    nothing about the durable row's age was pinned and this shipped silently.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    row = _launching_row(age_s=7200.0, now=now)
    row["max_age_s"] = 14400

    _reconcile_dead_ledger_entries(
        ledger, [row], get_provider=lambda _n: _ProviderWithPod, now=now
    )

    assert [i.id for i in ledger.recorded] == ["pod-real-1"]
    adopted = ledger.recorded[0]
    assert adopted.created_at == now - 7200.0, (
        "the adopted row took the listing's fabricated 0.0 and is now ~56y old"
    )
    # The lifecycle ceiling the launch was configured with rides along, so the
    # reaper ages the adopted instance out on its own config's terms.
    assert ledger.record_kwargs == [{"max_age_s": 14400}]


def test_the_adopted_row_keeps_row_tags_the_listing_cannot_reproduce() -> None:
    """``tags["ports"]`` must survive adoption, or warm attach can never reach it.

    SkyPilot's listing converter returns ``tags={}``. ``ensure_endpoints``
    rebuilds its ssh forwards from ``tags["ports"]`` and from nothing else
    (``providers/skypilot/__init__.py::_ports_for``), so an adopted cluster
    recorded off the listing is permanently unreachable by warm attach.

    Bug caught: recording the listing's tags verbatim. Also pins the merge
    DIRECTION — a key the provider actually read wins over the row's copy,
    because the provider's is the live reading — and that the launch-phase tag
    is dropped, since a real row still carrying it makes every later collapse
    refuse.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    row = _launching_row(age_s=7200.0, now=now)
    row["tags"] = {
        "kf_launch_phase": "launching",
        "kf_run_id": "kf-run-42",
        "ports": "8000,8001",
        "mode": "stale-guess",
    }

    _reconcile_dead_ledger_entries(
        ledger, [row], get_provider=lambda _n: _ProviderWithPod, now=now
    )

    adopted = ledger.recorded[0]
    assert adopted.tags["ports"] == "8000,8001", (
        "the row's ports tag was lost, so ensure_endpoints can never rebuild"
    )
    assert adopted.tags["mode"] == "pod", (
        "the row's stale guess beat the provider's live reading"
    )
    assert "kf_launch_phase" not in adopted.tags


def test_adoption_records_the_real_row_before_the_phase_scoped_collapse() -> None:
    """A ledger that CAN scope the delete gets the collapse, in that order.

    Bug caught: a bare ``forget(run_id)`` — which matches on id alone — takes
    the real row with it wherever the two rows share a key, and collapsing
    BEFORE the record opens a window in which zero rows exist for a resource
    that is already created. ``forget_provisional(real_id=...)`` refuses
    unless the real row is already there, so the wrong order silently
    no-ops the collapse and ships two rows for one pod.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _CollapsingLedger()

    _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithPod,
        now=now,
    )

    assert ledger.calls == [
        ("record", "pod-real-1"),
        ("forget_provisional", "kf-run-42", "pod-real-1"),
    ]


def test_adoption_does_not_re_record_a_real_row_that_already_exists() -> None:
    """The orchestrator's collapse having failed must not double the row.

    Bug caught: ``record`` APPENDS, so re-recording a pod the ledger already
    holds leaves two rows under one id and every per-id reader picks
    whichever it finds first — the exact hazard ``forget_provisional``
    exists to avoid.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _CollapsingLedger(
        rows={"pod-real-1": {"id": "pod-real-1", "provider": "runpod", "tags": {}}}
    )

    _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithPod,
        now=now,
    )

    assert ledger.calls == [("forget_provisional", "kf-run-42", "pod-real-1")]


def test_the_duplicate_guard_scans_every_row_not_just_the_first() -> None:
    """On the same-key shape the real row is BEHIND the provisional one.

    Bug caught: asking ``read`` (which returns the FIRST row for an id)
    answers "provisional" for a cluster that already has a real row, so the
    guard reports "no real row", ``record`` appends a THIRD row, and the
    per-id readers pick whichever they find first. ``entries()`` is the only
    question with a correct answer on this shape.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    provisional = _launching_row(age_s=7200.0, now=now)
    real = {"id": "kf-run-42", "provider": "runpod", "tags": {"mode": "pod"}}
    ledger = _ScanningLedger([provisional, real])

    _reconcile_dead_ledger_entries(
        ledger,
        [provisional],
        get_provider=lambda _n: _ProviderWithSameKeyCluster,
        now=now,
    )

    assert ledger.calls == [("forget_provisional", "kf-run-42", "kf-run-42")]


def test_a_failing_entries_scan_falls_back_to_the_per_id_read() -> None:
    """The scan is preferred, not required.

    Bug caught: an ``entries()`` that raises (a locked or half-written
    ledger) short-circuiting the guard to "no real row", so the adoption
    appends a duplicate for a pod the ledger already holds — the very
    duplication the guard exists to prevent, reintroduced by its own error
    path.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    class _ScanFailsLedger(_ScanningLedger):
        def entries(self) -> list[dict[str, Any]]:
            raise OSError("ledger locked")

    now = 1_700_000_000.0
    ledger = _ScanFailsLedger(
        [{"id": "pod-real-1", "provider": "runpod", "tags": {"mode": "pod"}}]
    )

    _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithPod,
        now=now,
    )

    assert ledger.calls == [("forget_provisional", "kf-run-42", "pod-real-1")]


def test_an_adopted_entry_is_rewritten_in_place_for_the_caller() -> None:
    """The caller's snapshot must name the id the ledger now holds.

    Bug caught: ``kinoforge list`` printing the run_id for a row the ledger
    keys under the pod id, so ``kinoforge destroy --id <printed>`` fails
    against a pod that is live and billing. The launch-phase tag has to go
    too, or the row still reads as provisional to everything downstream.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    entries = [_launching_row(age_s=7200.0, now=now)]

    _reconcile_dead_ledger_entries(
        _FakeLedger([]), entries, get_provider=lambda _n: _ProviderWithPod, now=now
    )

    assert entries[0]["id"] == "pod-real-1"
    assert "kf_launch_phase" not in entries[0]["tags"]
    assert entries[0]["tags"]["kf_run_id"] == "kf-run-42"


def test_a_refused_collapse_is_logged_and_the_entry_is_not_rewritten(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refusal leaves the row provisional — say so, and do not claim it.

    Bug caught: discarding ``forget_provisional``'s return value, so a
    permanently stale ``launching`` row on a live instance reaches production
    with nothing to grep for, while the caller's snapshot has already been
    rewritten to an id whose row was never collapsed.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    class _RefusingLedger(_CollapsingLedger):
        def forget_provisional(
            self, provisional_id: str, *, real_id: str | None = None
        ) -> bool:
            super().forget_provisional(provisional_id, real_id=real_id)
            return False

    now = 1_700_000_000.0
    entries = [_launching_row(age_s=7200.0, now=now)]

    with caplog.at_level("WARNING", logger="kinoforge.cli._reconcile"):
        gone = _reconcile_dead_ledger_entries(
            _RefusingLedger(),
            entries,
            get_provider=lambda _n: _ProviderWithPod,
            now=now,
        )

    assert gone == []
    assert entries[0]["id"] == "kf-run-42"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("kf-run-42" in m and "pod-real-1" in m for m in warnings), warnings


def test_a_same_key_row_is_collapsed_onto_its_own_id() -> None:
    """SkyPilot's shape — cluster name IS the run id — still adopts.

    Bug caught: treating "the live instance id equals the row id" as
    "nothing to do" leaves the row tagged ``launching`` forever, so the
    sweeper and ``kinoforge status`` keep reading a provisional row for a
    cluster that has been up for hours.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _CollapsingLedger()

    _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithSameKeyCluster,
        now=now,
    )

    assert ledger.calls == [
        ("record", "kf-run-42"),
        ("forget_provisional", "kf-run-42", "kf-run-42"),
    ]


def test_a_same_key_row_is_never_dropped_by_a_forget_only_ledger() -> None:
    """Without a phase-scoped delete, the same-key row is left untouched.

    Bug caught: falling back to ``forget(row_id)`` when the live instance id
    EQUALS the row id deletes the real row that was just recorded under that
    same id — zero rows for a live, billing cluster, which is precisely the
    F12 hole this task closes.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    class _ForgetOnlyLedger(_FakeLedger):
        """No phase-scoped delete at all — the shape the fallback is for.

        ``_FakeLedger`` deliberately HAS ``forget_provisional`` (so a
        regression to the blunt door is visible elsewhere), so this test has to
        take it away again to be about the forget-only case at all.
        """

        forget_provisional = None  # type: ignore[assignment]

    now = 1_700_000_000.0
    ledger = _ForgetOnlyLedger([])

    _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithSameKeyCluster,
        now=now,
    )

    assert ledger.forgotten == []
    assert ledger.recorded == []


def test_an_unreadable_provider_leaves_the_launching_row_alone() -> None:
    """A failed listing is uncertainty, not evidence of absence.

    Bug caught: a transient RunPod GraphQL 500 during ``kinoforge list``
    ageing out every in-flight launch row on the box.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])

    gone = _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderThatCannotList,
        now=now,
    )

    assert gone == []
    assert ledger.forgotten == []
    assert ledger.recorded == []


def test_only_the_aged_out_row_is_reported_gone_never_the_adopted_one() -> None:
    """The report drives what ``list`` prints and hides.

    Bug caught: reporting an ADOPTED row as gone makes ``kinoforge list``
    print "pod gone provider-side" for a pod that is live and billing, and
    filter it out of the very listing the operator is reading — the money
    leak F12 exists to prevent, dressed as a reassuring log line.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0

    adopted = _reconcile_dead_ledger_entries(
        _FakeLedger([]),
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithPod,
        now=now,
    )
    aged = _reconcile_dead_ledger_entries(
        _FakeLedger([]),
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithNothing,
        now=now,
    )

    assert adopted == []
    assert aged == ["kf-run-42"]


def test_a_ledger_that_cannot_record_keeps_its_launching_row() -> None:
    """No ``record`` means no row to adopt onto — so nothing is deleted.

    Bug caught: probing for the adoption capability but deleting the
    provisional row regardless, which turns a ledger that merely lacks
    ``record`` (``tests/cli/test_reconcile_ledger.py`` passes one) into a
    live pod with no ledger entry at all.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    class _ForgetOnlyLedger:
        def __init__(self) -> None:
            self.forgotten: list[str] = []

        def forget(self, instance_id: str) -> None:
            self.forgotten.append(instance_id)

    now = 1_700_000_000.0
    ledger = _ForgetOnlyLedger()

    gone = _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithPod,
        now=now,
    )

    assert gone == []
    assert ledger.forgotten == []


def test_a_failing_record_aborts_the_adoption_without_deleting() -> None:
    """A write that fails must not be followed by the delete.

    Bug caught: swallowing the ``record`` exception and carrying on to the
    collapse, which removes the provisional row after its replacement
    failed to land — zero rows for a live instance.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    class _RecordFailsLedger(_FakeLedger):
        def record(self, instance: Instance, **kwargs: object) -> None:
            raise OSError("ledger full")

    now = 1_700_000_000.0
    ledger = _RecordFailsLedger([])

    gone = _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithPod,
        now=now,
    )

    assert gone == []
    assert ledger.forgotten == []


def test_an_unreadable_ledger_still_adopts() -> None:
    """A failed duplicate-check must not block the adoption.

    Bug caught: treating an exception from ``read`` as "a real row already
    exists" and skipping the ``record``, so the collapse is refused and the
    row stays provisional forever. A spurious duplicate row is recoverable;
    a missing row is not.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    class _ReadFailsLedger(_CollapsingLedger):
        def read(self, instance_id: str) -> dict[str, Any] | None:
            raise OSError("ledger unreadable")

    now = 1_700_000_000.0
    ledger = _ReadFailsLedger()

    _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithPod,
        now=now,
    )

    assert ledger.calls == [
        ("record", "pod-real-1"),
        ("forget_provisional", "kf-run-42", "pod-real-1"),
    ]


def test_a_provider_returning_a_non_listing_is_survivable() -> None:
    """A malformed listing is uncertainty, not a crash.

    Bug caught: a provider whose ``list_instances`` returns ``None`` (a
    stubbed or half-migrated provider) raising TypeError out of the loop and
    taking down every ``kinoforge`` command's instance overview.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    class _ProviderReturningNone(_ProviderWithPod):
        def list_instances(self) -> list[Instance]:
            return None  # type: ignore[return-value]

    now = 1_700_000_000.0
    ledger = _FakeLedger([])

    gone = _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderReturningNone,
        now=now,
    )

    assert gone == []
    assert ledger.forgotten == []


def test_a_row_exactly_at_the_grace_window_is_kept() -> None:
    """The boundary itself is not yet "too old".

    Bug caught: a ``>=`` comparison reaping a launch on the exact tick its
    grace window runs out, one poll before the pod would have registered.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])

    gone = _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=600.0, now=now)],
        get_provider=lambda _n: _ProviderWithNothing,
        now=now,
        launching_grace_s=600.0,
    )

    assert gone == []
    assert ledger.forgotten == []


def test_a_malformed_created_at_leaves_the_row_alone() -> None:
    """An unparseable timestamp is uncertainty, and must not raise.

    Bug caught: ``float("soon")`` raising ValueError out of a best-effort
    reconcile, which runs inline on every ``kinoforge list`` — one bad row
    would take the whole command down.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    row = _launching_row(age_s=7200.0, now=now)
    row["created_at"] = "soon"

    gone = _reconcile_dead_ledger_entries(
        ledger, [row], get_provider=lambda _n: _ProviderWithNothing, now=now
    )

    assert gone == []
    assert ledger.forgotten == []


def test_a_failing_forget_is_swallowed_and_not_reported() -> None:
    """A ledger that refuses the delete must not crash or lie.

    Bug caught: a locked/corrupt ledger propagating out of ``list``, or the
    id being reported gone so the caller stops displaying a row that is
    still on disk.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    class _BrokenLedger(_FakeLedger):
        # BOTH doors must refuse. Overriding only ``forget`` would leave the
        # phase-scoped one working, the row would be deleted happily, and this
        # test would pass while proving nothing about the failure path.
        def forget(self, instance_id: str) -> None:
            raise OSError("ledger locked")

        def forget_provisional(
            self, provisional_id: str, *, real_id: str | None = None
        ) -> bool:
            raise OSError("ledger locked")

    now = 1_700_000_000.0
    ledger = _BrokenLedger([])

    gone = _reconcile_dead_ledger_entries(
        ledger,
        [_launching_row(age_s=7200.0, now=now)],
        get_provider=lambda _n: _ProviderWithNothing,
        now=now,
    )

    assert gone == []


@pytest.mark.parametrize("provider_name", ["modal", "local", "not-a-provider"])
def test_an_aged_launching_row_is_forgotten_even_when_it_cannot_be_adopted(
    provider_name: str,
) -> None:
    """The provider-agnostic age-out, added with ruling C1 (2026-09-03).

    Since C1 the orchestrator KEEPS the provisional row when ``create_instance``
    raises, because a raise is not proof the provider booked nothing. On a
    provider outside ``_RECONCILABLE_PROVIDERS`` — Modal's listing exposes no
    name matchable against a ``run_id``, ``local``'s table is in-process, an
    unregistered name resolves to nothing at all — the adoption branch can
    never run, so without this fallback that row is PERMANENT: it shows in
    every ``kinoforge list`` forever and no branch anywhere can clear it.

    Bug caught: leaving these rows alone (the pre-C1 shape), which turns every
    dead Modal launch into an immortal ghost row.

    Args:
        provider_name: A provider the reconciler cannot adopt against.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    resolved: list[str] = []

    def _resolve(name: str) -> Any:  # noqa: ANN401
        resolved.append(name)
        return _ProviderWithNothing

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    row = _launching_row(age_s=7200.0, now=now)
    row["provider"] = provider_name

    gone = _reconcile_dead_ledger_entries(ledger, [row], get_provider=_resolve, now=now)

    assert gone == ["kf-run-42"]
    # Forgotten through the PHASE-SCOPED door: a plain forget matches on id
    # alone and would take a real row sharing that id with it.
    assert ledger.scoped_forgets == ["kf-run-42"]
    # ...and never ADOPTED. No provider is constructed and nothing is recorded,
    # which is the half of the old gate that must survive.
    assert resolved == []
    assert ledger.recorded == []


@pytest.mark.parametrize("provider_name", ["modal", "local", "not-a-provider"])
def test_a_young_launching_row_is_kept_on_an_unadoptable_provider(
    provider_name: str,
) -> None:
    """The age gate applies to the fallback too — a launch may be in flight.

    Bug caught: an age-out that fires on provider name alone. A Modal deploy
    takes minutes, and reaping the row protecting it from a concurrent
    ``kinoforge list`` (which this project's own live-smoke polling rule
    actively encourages) is the F12 orphan hole reopened.

    Args:
        provider_name: A provider the reconciler cannot adopt against.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    row = _launching_row(age_s=60.0, now=now)
    row["provider"] = provider_name

    gone = _reconcile_dead_ledger_entries(
        ledger, [row], get_provider=lambda _n: _ProviderWithNothing, now=now
    )

    assert gone == []
    assert ledger.forgotten == []


def test_a_plain_row_on_a_non_reconcilable_provider_is_still_untouched() -> None:
    """The C1 fallback is scoped to ``launching`` rows and nothing else.

    ``local`` is excluded from reconciliation because its instance table is
    in-process, so a fresh CLI invocation KeyErrors on a perfectly valid pod.

    Bug caught: widening the age-out fallback past the ``launching`` phase tag
    — an ordinary ``local`` row would then be probed and forgotten, deleting
    the handle on a live local instance.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    resolved: list[str] = []

    def _resolve(name: str) -> Any:  # noqa: ANN401
        resolved.append(name)
        return _ProviderWithNothing

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    row = _launching_row(age_s=7200.0, now=now)
    row["provider"] = "local"
    row["tags"] = {"mode": "pod"}  # no kf_launch_phase

    gone = _reconcile_dead_ledger_entries(ledger, [row], get_provider=_resolve, now=now)

    assert resolved == []
    assert gone == []
    assert ledger.forgotten == []


def test_a_launching_row_without_a_run_id_tag_matches_on_its_own_id() -> None:
    """Pre-Task-4 rows carry the phase tag but no ``kf_run_id``.

    Bug caught: matching strictly on ``tags["kf_run_id"]`` makes an older
    row match nothing, so it is aged out while its pod is live.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    now = 1_700_000_000.0
    ledger = _FakeLedger([])
    row = _launching_row(age_s=7200.0, now=now)
    row["tags"] = {"kf_launch_phase": "launching"}

    _reconcile_dead_ledger_entries(
        ledger, [row], get_provider=lambda _n: _ProviderWithPod, now=now
    )

    assert [i.id for i in ledger.recorded] == ["pod-real-1"]
    assert ledger.forgotten == ["kf-run-42"]


def test_now_defaults_to_the_wall_clock() -> None:
    """Callers that pass no clock still get the young/aged split.

    Bug caught: a ``now=None`` default falling through as ``0.0``. BOTH
    halves are needed to catch it — under a zero clock every row's age is
    hugely negative, so the young half alone passes while nothing is ever
    aged out again. The aged half is the one that fails.
    """
    from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries

    fresh = {
        "id": "kf-run-99",
        "provider": "runpod",
        "created_at": time.time(),
        "tags": {"kf_launch_phase": "launching", "kf_run_id": "kf-run-99"},
    }
    abandoned = {
        "id": "kf-run-98",
        "provider": "runpod",
        "created_at": time.time() - 7200.0,
        "tags": {"kf_launch_phase": "launching", "kf_run_id": "kf-run-98"},
    }
    young_ledger = _FakeLedger([])
    aged_ledger = _FakeLedger([])

    young = _reconcile_dead_ledger_entries(
        young_ledger, [fresh], get_provider=lambda _n: _ProviderWithNothing
    )
    aged = _reconcile_dead_ledger_entries(
        aged_ledger, [abandoned], get_provider=lambda _n: _ProviderWithNothing
    )

    assert young == []
    assert young_ledger.forgotten == []
    assert aged == ["kf-run-98"]
    assert aged_ledger.forgotten == ["kf-run-98"]
