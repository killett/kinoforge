"""Age + utilisation classification for ``--ephemeral`` orphan rows (spec C1).

An ``--ephemeral`` run writes no ledger row by design, so the only record of
the pod is its :class:`~kinoforge.core.warm_reuse.ephemeral_index.EphemeralIndexRow`.
Those rows carry no heartbeat and no last-used stamp, so before this module's
subject existed a pod that merely answered its probe classified ``LIVE``
forever and billed at the GPU rate until a human typed ``kinoforge destroy``.

The rule under test: an ephemeral row is reapable only when it is old enough
AND idle on the provider's util probe. Age alone must never reap — a long
generation is not a leak — and idleness alone must never reap, because a Wan
A14B cold boot spends ~25 minutes at 0% GPU fetching 70 GB of weights.

Every test drives a fake clock (explicit ``now`` floats / :class:`FakeClock`)
and a fake util probe (a hand-built :class:`RuntimeProbe` fed through the real
``_synthesize_ephemeral_entry``), so none of this needs a live pod.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from kinoforge.core.clock import FakeClock
from kinoforge.core.errors import TeardownError
from kinoforge.core.interfaces import Instance
from kinoforge.core.reaper import (
    _EPHEMERAL_GC_404_GRACE_S,
    DEFAULT_APPLY_POLICY,
    Policy,
    Verdict,
    classify,
    ephemeral_orphan_reason,
)
from kinoforge.core.reaper_actor import (
    SweepReport,
    _synthesize_ephemeral_entry,
    sweep,
)
from kinoforge.core.runtime_probe import RuntimeProbe
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex, EphemeralIndexRow
from kinoforge.stores.local import LocalArtifactStore

# Birth of the pod under test, in local time — the semantics Task 3 (9d34d008)
# gave EphemeralIndexRow.created_at_local: stamped immediately BEFORE
# create_instance, so ``now - created_at_local`` is the real age INCLUDING the
# whole cold boot.
_BIRTH = datetime(2026, 9, 6, 12, 0, 0)
_BIRTH_EPOCH = _BIRTH.timestamp()

_ORPHAN_AGE_S = 3600.0

#: Thresholds as the sweeper daemon forwards them. ``stall_window_s`` is None
#: (STALL_REAP kill-switch) so these tests isolate the age+util rule.
_THRESHOLDS: dict[str, Any] = {
    "idle_timeout_s": 2 * 3600.0,
    "max_lifetime_s": 5 * 3600.0,
    "heartbeat_interval_s": 30.0,
    "grace_after_session_s": 1800.0,
    "stall_window_s": None,
    "stall_gpu_threshold": 5.0,
    "stall_cpu_threshold": 20.0,
    "restart_loop_window_s": None,
    "ephemeral_orphan_age_s": _ORPHAN_AGE_S,
}


def _row(
    pod_id: str = "eph-61ee7764",
    *,
    endpoints: dict[str, str] | None = None,
) -> EphemeralIndexRow:
    """An index row born at :data:`_BIRTH`.

    ``endpoints={}`` is the PRE-CREATE shape: the row the controller reserves
    before ``create_instance``, when no URL is knowable yet.
    """
    return EphemeralIndexRow(
        id=pod_id,
        warm_attach_key="wak-deadbeef",
        kinoforge_key="kfkey-dead0",
        endpoints=(
            {"8000": "https://example.invalid/8000"} if endpoints is None else endpoints
        ),
        provider="runpod",
        created_at_local=_BIRTH.isoformat(),
    )


def _probe(
    *,
    gpu: float | None,
    cpu: float | None,
    found: bool = True,
    pod_id: str = "eph-61ee7764",
) -> RuntimeProbe:
    """A fake util probe result — the only source of idleness evidence."""
    return RuntimeProbe(
        pod_id=pod_id,
        found=found,
        container_uptime_s=900.0,
        gpu_util_pct=gpu,
        cpu_pct=cpu,
        cost_per_hr=2.50,
        probed_at_local=_BIRTH.isoformat(),
    )


def _entry(
    *, gpu: float | None, cpu: float | None, pod_id: str = "eph-61ee7764"
) -> dict[str, Any]:
    """Synthesise the ephemeral entry exactly as ``sweep`` does."""
    return _synthesize_ephemeral_entry(_row(pod_id), _probe(gpu=gpu, cpu=cpu))


def _at(age_s: float) -> float:
    """Wall-clock ``now`` for a pod of ``age_s`` seconds."""
    return _BIRTH_EPOCH + age_s


def _classify(entry: Mapping[str, Any], now: float, **overrides: Any) -> Verdict:
    thresholds = {**_THRESHOLDS, **overrides}
    return classify(entry, set(), now, **thresholds)


# ---------------------------------------------------------------------------
# The three cases the safety net turns on
# ---------------------------------------------------------------------------


def test_old_and_idle_ephemeral_row_is_reapable() -> None:
    """Old AND idle on both GPU and CPU → ORPHAN_REAP.

    This is the leak the sweeper exists to close: the 2026-09-06 T1-24 cell
    watched ``eph-61ee7764`` sit at gpu=0.0 cpu=0.0 for the whole run and the
    daemon returned LIVE on every pass. A regression that drops the age+util
    rule puts that verdict back and the pod bills until a human intervenes.
    """
    verdict = _classify(_entry(gpu=0.0, cpu=0.0), _at(2 * 3600.0))
    assert verdict == Verdict.ORPHAN_REAP


def test_old_but_busy_ephemeral_row_stays_live() -> None:
    """Old but the GPU is working → LIVE. Age alone must never reap.

    Catches an age-only TTL reaper, which would destroy a pod four hours into
    a legitimate long render and take the operator's work with it.
    """
    verdict = _classify(_entry(gpu=97.0, cpu=45.0), _at(4 * 3600.0))
    assert verdict == Verdict.LIVE


def test_young_but_idle_ephemeral_row_stays_live() -> None:
    """Idle but young → LIVE. Idleness alone must never reap.

    Catches a util-only reaper: a Wan A14B cold boot sits at 0% GPU for ~25
    minutes fetching 70 GB of weights, so reaping on idleness at 10 minutes
    kills every large model before it ever renders a frame.
    """
    verdict = _classify(_entry(gpu=0.0, cpu=0.0), _at(600.0))
    assert verdict == Verdict.LIVE


# ---------------------------------------------------------------------------
# Boundaries and conservatism
# ---------------------------------------------------------------------------


def test_orphan_age_boundary_is_strictly_greater_than_threshold() -> None:
    """At exactly the threshold the pod survives; one second past it is reaped.

    Catches a ``>=``/``>`` slip or an off-by-one in the age arithmetic — on a
    $2.50/hr card the boundary is the whole decision.
    """
    idle = _entry(gpu=0.0, cpu=0.0)
    assert _classify(idle, _at(_ORPHAN_AGE_S)) == Verdict.LIVE
    assert _classify(idle, _at(_ORPHAN_AGE_S + 1.0)) == Verdict.ORPHAN_REAP


def test_orphan_age_measured_from_row_birth_not_from_probe() -> None:
    """Age comes from ``created_at_local``, not the probe's uptime field.

    The row is born at :data:`_BIRTH` while ``container_uptime_s`` says 900 s.
    A reaper that measured age from the container uptime would see 15 minutes
    and refuse to reap a pod that has actually been billing for two hours —
    the exact under-count Task 3 fixed at the write side.
    """
    entry = _entry(gpu=0.0, cpu=0.0)
    assert entry["container_uptime_s"] == 900.0
    assert _classify(entry, _at(2 * 3600.0)) == Verdict.ORPHAN_REAP


def test_busy_cpu_with_idle_gpu_stays_live() -> None:
    """GPU idle but CPU pegged → LIVE.

    This is what a weight download looks like: 0% GPU, high CPU, for 25 of the
    30 minutes of a Wan A14B boot. A GPU-only idleness test reaps it.
    """
    verdict = _classify(_entry(gpu=0.0, cpu=90.0), _at(2 * 3600.0))
    assert verdict == Verdict.LIVE


def test_missing_util_readings_stay_live() -> None:
    """A probe that reports no GPU/CPU numbers is not evidence of idleness.

    RunPod returns a null GPU array during early boot. Treating ``None`` as
    0.0 would reap on ignorance — destroying a pod nobody has observed.
    """
    verdict = _classify(_entry(gpu=None, cpu=None), _at(4 * 3600.0))
    assert verdict == Verdict.LIVE


def test_failed_probe_is_not_reaped_however_old() -> None:
    """probe_state != "ok" short-circuits before the age+util rule.

    A failed probe carries no ``gpu_util_pct`` key at all; if the orphan rule
    ran ahead of the probe-state guards it would read the missing field as
    idle and destroy a pod whose state is simply unknown.
    """
    entry = _synthesize_ephemeral_entry(_row(), "failed")
    assert _classify(entry, _at(4 * 3600.0)) == Verdict.PROBE_FAILED


def test_orphan_reap_disabled_when_threshold_is_none() -> None:
    """``ephemeral_orphan_age_s=None`` is the kill switch.

    Catches the classic wiring bug ``float(thresholds.get(...) or 0.0)``,
    which turns "feature off" into "age threshold 0" — i.e. reap every idle
    ephemeral pod the instant it is seen.
    """
    verdict = _classify(
        _entry(gpu=0.0, cpu=0.0), _at(4 * 3600.0), ephemeral_orphan_age_s=None
    )
    assert verdict == Verdict.LIVE


def test_ledger_backed_entry_is_untouched_by_the_orphan_age_threshold() -> None:
    """A heartbeat-bearing ledger entry never sees the ephemeral rule.

    Catches the new threshold leaking into the heartbeat branch, where a fresh
    heartbeat is the authority and an idle-looking pod inside a live session
    must stay LIVE.
    """
    now = _at(4 * 3600.0)
    entry = {
        "id": "i-ledger",
        "provider": "runpod",
        "created_at": _BIRTH_EPOCH,
        "last_heartbeat": now - 5.0,
        "heartbeat_thread_tick": now - 5.0,
        "gpu_util_pct": 0.0,
        "cpu_pct": 0.0,
    }
    assert classify(entry, {"i-ledger"}, now, **_THRESHOLDS) == Verdict.LIVE


# ---------------------------------------------------------------------------
# The decision has to say what it saw
# ---------------------------------------------------------------------------


def test_reason_states_the_age_and_the_utilisation_observed() -> None:
    """The reap decision names the age and both util readings it acted on.

    A destroy that records only "ORPHAN_REAP" is unreviewable: the operator
    whose render vanished cannot tell whether the daemon saw an idle GPU or
    guessed. Catches a reason string that drops either number.
    """
    reason = ephemeral_orphan_reason(_entry(gpu=0.0, cpu=1.5), _at(7200.0))
    assert "7200" in reason
    assert "0.0" in reason
    assert "1.5" in reason
    assert "age" in reason.lower()


# ---------------------------------------------------------------------------
# End to end through sweep(): classification must actually reach a destroy
# ---------------------------------------------------------------------------


class _FakeLedger:
    def __init__(self) -> None:
        self.forgotten: list[str] = []

    def entries(self) -> list[dict[str, Any]]:
        return []

    def forget(self, instance_id: str) -> None:
        self.forgotten.append(instance_id)


class _FakeProvider:
    """Provider with a fake util probe; destroy removes the pod from the list."""

    def __init__(self, probe: RuntimeProbe) -> None:
        self._probe = probe
        self.destroyed: list[str] = []
        self.live: set[str] = {probe.pod_id}

    def probe_runtime(self, pod_id: str) -> RuntimeProbe:
        return self._probe

    def list_instances(self) -> list[Instance]:
        return [
            Instance(id=i, provider="runpod", created_at=_BIRTH_EPOCH, status="ready")
            for i in sorted(self.live)
        ]

    def destroy_instance(self, instance_id: str) -> None:
        self.destroyed.append(instance_id)
        self.live.discard(instance_id)


def _sweep_once(
    tmp_path: Any, provider: _FakeProvider, *, policy: Policy, now: float
) -> tuple[LocalArtifactStore, SweepReport]:
    store = LocalArtifactStore(root=tmp_path)
    EphemeralIndex(store=store).add(_row())
    ledger = _FakeLedger()
    report = sweep(
        store,
        ledger,  # type: ignore[arg-type]
        lambda _name: lambda: provider,  # type: ignore[arg-type,return-value]
        _THRESHOLDS,
        FakeClock(start=now),
        policy=policy,
    )
    return store, report


def test_sweep_destroys_old_idle_ephemeral_pod_and_clears_its_index_row(
    tmp_path: Any,
) -> None:
    """With ORPHAN_REAP opted in, sweep destroys the pod and drops the row.

    Catches the half-wiring where classification is right but nothing acts —
    and the sibling bug where the pod is destroyed but its index row survives,
    so the next tick re-reaps a dead id forever.
    """
    provider = _FakeProvider(_probe(gpu=0.0, cpu=0.0))
    policy = Policy(
        act_verdicts=DEFAULT_APPLY_POLICY.act_verdicts | {Verdict.ORPHAN_REAP}
    )
    store, report = _sweep_once(tmp_path, provider, policy=policy, now=_at(2 * 3600.0))

    assert provider.destroyed == ["eph-61ee7764"]
    assert EphemeralIndex(store=store).rows() == []
    assert [a.action for a in report.actions] == ["destroyed_and_forgot"]
    reason = report.actions[0].reason or ""
    assert "7200" in reason, f"reason must state the observed age: {reason!r}"
    assert "0.0" in reason, f"reason must state the observed utilisation: {reason!r}"


def test_sweep_leaves_the_pod_alone_without_the_orphan_opt_in(tmp_path: Any) -> None:
    """ORPHAN_REAP outside the policy → classified, never destroyed.

    Catches ORPHAN_REAP being folded into DEFAULT_APPLY_POLICY, which would
    make every existing ``--apply`` invocation start destroying ephemeral pods
    without the operator opting in.
    """
    provider = _FakeProvider(_probe(gpu=0.0, cpu=0.0))
    store, report = _sweep_once(
        tmp_path, provider, policy=DEFAULT_APPLY_POLICY, now=_at(2 * 3600.0)
    )

    assert provider.destroyed == []
    assert [r.id for r in EphemeralIndex(store=store).rows()] == ["eph-61ee7764"]
    assert report.snapshot["eph-61ee7764"][1] == Verdict.ORPHAN_REAP
    assert report.actions == []


# ---------------------------------------------------------------------------
# A concurrent sweeper must not GC the pre-create launch row
#
# `_ephemeral_launch_row_reserve` writes an index row keyed by the resource
# NAME before `create_instance` — and on RunPod that name is not the pod id
# `probe_runtime` asks about, so the probe answers ``not_found`` for the whole
# run, not for the ~second it takes the provider to register. A daemon tick in
# that window used to remove the very row the pre-create write exists to
# create, leaving a booting, billing pod with no durable name anywhere.
# ---------------------------------------------------------------------------


def _not_found_entry(
    *, endpoints: dict[str, str] | None = None, pod_id: str = "eph-61ee7764"
) -> dict[str, Any]:
    """Synthesise the entry a ``not_found`` probe produces for such a row."""
    return _synthesize_ephemeral_entry(
        _row(pod_id, endpoints=endpoints),
        _probe(gpu=None, cpu=None, found=False, pod_id=pod_id),
    )


def test_a_pre_create_row_the_probe_cannot_find_is_not_gc_ed() -> None:
    """A young row with no endpoints is a launch in flight, not debris.

    Bug caught: ``probe_state == "not_found"`` returned GC_404 with no grace at
    all, and GC_404 sits inside DEFAULT_APPLY_POLICY — so a sweeper daemon
    ticking during a cold boot deleted the launch row of a pod that was
    already billing. The pod then has no durable name in any state file, which
    is precisely the condition the pre-create row was added to end.
    """
    verdict = _classify(_not_found_entry(endpoints={}), _at(600.0))
    assert verdict == Verdict.LIVE


def test_a_pre_create_row_past_the_grace_window_is_gc_ed() -> None:
    """The grace defers the cleanup; it must not cancel it.

    Bug caught: an unbounded grace turns every abandoned pre-create row into a
    permanent ghost that no sweep can ever clear.
    """
    verdict = _classify(_not_found_entry(endpoints={}), _at(3600.0))
    assert verdict == Verdict.GC_404


def test_a_row_that_once_named_a_live_pod_is_gc_ed_immediately() -> None:
    """Endpoints on the row mean it HAS been confirmed; not_found ends it.

    Bug caught: widening the grace to every ``not_found`` row would delay the
    cleanup of genuinely dead pods by half an hour, and would hide a real
    provider-side disappearance behind a window sized for a boot.
    """
    verdict = _classify(_not_found_entry(), _at(60.0))
    assert verdict == Verdict.GC_404


def test_the_grace_boundary_is_inclusive_and_sits_at_the_documented_value() -> None:
    """The window ends where it says it does — 1800 s, inclusive.

    Bug caught: an off-by-a-window grace (seconds instead of minutes, or a
    value shorter than a Wan A14B cold boot) reads as "fixed" while still
    deleting the launch row of a pod mid-boot.
    """
    entry = _not_found_entry(endpoints={})
    assert _EPHEMERAL_GC_404_GRACE_S == 1800.0
    assert _classify(entry, _at(_EPHEMERAL_GC_404_GRACE_S)) == Verdict.LIVE
    assert _classify(entry, _at(_EPHEMERAL_GC_404_GRACE_S + 0.1)) == Verdict.GC_404


def test_sweep_leaves_a_pre_create_row_on_disk_when_the_probe_404s(
    tmp_path: Any,
) -> None:
    """End to end: the daemon tick must not delete the row it finds mid-launch.

    Catches the half-fix where classification defers but some other branch of
    ``sweep`` removes the row anyway — the only outcome that matters here is
    whether the handle is still on disk when the tick ends.
    """
    provider = _FakeProvider(_probe(gpu=None, cpu=None, found=False))
    store = LocalArtifactStore(root=tmp_path)
    EphemeralIndex(store=store).add(_row(endpoints={}))
    ledger = _FakeLedger()

    report = sweep(
        store,
        ledger,  # type: ignore[arg-type]
        lambda _name: lambda: provider,  # type: ignore[arg-type,return-value]
        _THRESHOLDS,
        FakeClock(start=_at(600.0)),
        policy=DEFAULT_APPLY_POLICY,
    )

    assert [r.id for r in EphemeralIndex(store=store).rows()] == ["eph-61ee7764"]
    assert provider.destroyed == []
    assert [a.action for a in report.actions] == []


# ---------------------------------------------------------------------------
# A failed orphan destroy must keep the evidence it acted on
# ---------------------------------------------------------------------------


class _UndestroyableProvider(_FakeProvider):
    """A provider whose destroy always fails, as a dead API endpoint would."""

    def destroy_instance(self, instance_id: str) -> None:
        raise TeardownError("simulated teardown failure")


def test_a_failed_orphan_destroy_still_records_the_age_and_utilisation(
    tmp_path: Any,
) -> None:
    """`ActionResult.reason` must carry BOTH the failure and the evidence.

    Bug caught: the ``TeardownError`` handler overwrote ``reason`` with the
    exception string, discarding the age + utilisation the decision was made
    on — while the comment beside it claims the record "survives even if the
    destroy fails". An operator reviewing a failed reap of their render gets
    the error and no answer to "why did it try?".
    """
    provider = _UndestroyableProvider(_probe(gpu=0.0, cpu=0.0))
    policy = Policy(
        act_verdicts=DEFAULT_APPLY_POLICY.act_verdicts | {Verdict.ORPHAN_REAP}
    )
    _store, report = _sweep_once(tmp_path, provider, policy=policy, now=_at(2 * 3600.0))

    assert [a.action for a in report.actions] == ["failed"]
    reason = report.actions[0].reason or ""
    assert "simulated teardown failure" in reason, f"failure not reported: {reason!r}"
    assert "7200" in reason, f"observed age lost on the failure path: {reason!r}"
    assert "0.0" in reason, f"observed utilisation lost on the failure path: {reason!r}"
