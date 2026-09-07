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
from kinoforge.core.interfaces import Instance
from kinoforge.core.reaper import (
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


def _row(pod_id: str = "eph-61ee7764") -> EphemeralIndexRow:
    """An index row born at :data:`_BIRTH`."""
    return EphemeralIndexRow(
        id=pod_id,
        warm_attach_key="wak-deadbeef",
        kinoforge_key="kfkey-dead0",
        endpoints={"8000": "https://example.invalid/8000"},
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
