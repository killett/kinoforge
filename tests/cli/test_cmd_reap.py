"""Layer V T6: CLI `kinoforge reap` integration tests.

Covers AC13–AC16 of spec §4.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.cli._commands import _cmd_reap

if TYPE_CHECKING:
    from kinoforge.cli.context import SessionContext


def _args(**overrides: Any) -> argparse.Namespace:
    """Default flags = dry-run, no opts."""
    base = dict(
        apply=False,
        include_orphans=False,
        force_forget=False,
        strict=False,
        id=None,
        format="human",
        config=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class _FakeCtx:
    """Minimal SessionContext stand-in for CLI tests."""

    def __init__(
        self,
        entries: list[dict[str, Any]],
        cfg: Any = None,
        store: Any = None,
    ) -> None:
        self._entries = entries
        self.cfg = cfg
        self._ledger = MagicMock()
        self._ledger.entries.return_value = entries
        self._ledger.forget = MagicMock()

        if store is not None:
            # Caller supplied a real store (e.g. LocalArtifactStore) — used
            # when a test needs EphemeralIndex reads/writes to actually work.
            self._store = store
        else:
            self._store = MagicMock()
            # No ephemeral-index.json has ever been written in this fake —
            # match a real ArtifactStore's FileNotFoundError-on-missing-file
            # contract so EphemeralIndex.rows() sees "no rows" instead of
            # choking trying to iterate an unconfigured MagicMock return
            # value.
            self._store.get_json = MagicMock(side_effect=FileNotFoundError)

            # acquire_lock returns a context manager
            class _L:
                def __enter__(self) -> _L:
                    return self

                def __exit__(self, *_: object) -> None:
                    return None

            self._store.acquire_lock = MagicMock(return_value=_L())

    def ledger(self) -> MagicMock:
        """Return the fake ledger."""
        return self._ledger

    def store(self) -> MagicMock:
        """Return the fake store."""
        return self._store


def _ctx(
    entries: list[dict[str, Any]], cfg: Any = None, store: Any = None
) -> SessionContext:
    """Build a typed _FakeCtx cast to SessionContext."""
    return cast("SessionContext", _FakeCtx(entries, cfg, store))


# ---------------------------------------------------------------------------
# Dry-run default
# ---------------------------------------------------------------------------


def test_reap_dry_run_default_does_not_destroy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No --apply → no destructive calls; sweep called with policy=None."""
    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        from kinoforge.core.reaper_actor import SweepReport

        mock_sweep.return_value = SweepReport(snapshot={}, actions=[])
        code = _cmd_reap(_args(), ctx)
    assert code == 0
    # sweep called with policy=None for dry-run
    assert mock_sweep.call_args.kwargs["policy"] is None


def test_reap_empty_ledger_prints_message_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty ledger → exit 0 with informational message.

    Bug caught: ``"no" in combined.lower()`` is satisfied by "nothing",
    "no entries", or nearly any plausible sentence — it passed unchanged
    across the U18 wording change (old: "reap: ledger empty (nothing to
    do)"; new: "reap: ledger and ephemeral index both empty (nothing to
    do)"), so it never actually pinned the sentence. Assert the exact
    current sentence instead.
    """
    ctx = _ctx([])
    code = _cmd_reap(_args(), ctx)
    captured = capsys.readouterr()
    assert code == 0
    assert (
        captured.out == "reap: ledger and ephemeral index both empty (nothing to do)\n"
    )
    assert captured.err == ""


def test_reap_empty_ledger_with_ephemeral_index_row_reaches_orphan(
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty ledger + one EphemeralIndex row must reach sweep(), not short-circuit.

    Bug caught (U18): the original guard was ``if not ledger.entries():`` —
    an empty ledger returned "nothing to do" before ``sweep()`` was ever
    called. But an ``--ephemeral`` run writes no ledger row by design; its
    only durable trace is a row in the ``EphemeralIndex``, which is unioned
    in *inside* ``sweep()``, on the far side of that guard. So a run whose
    only trace is an index row got "nothing to do" printed over a billing
    pod. This test writes ONLY an index row (the ledger stays empty) and
    asserts the orphan's id and verdict actually appear in the reap
    output — not merely that the exit code is 0, which the broken
    short-circuit path also returns.

    ``sweep()`` is not mocked: the real registry-provider/probe/classify
    path must run so the assertion proves the orphan was truly classified,
    not just that sweep() was invoked with the right arguments.
    """
    from datetime import datetime

    from kinoforge.core.reaper import Verdict
    from kinoforge.core.runtime_probe import RuntimeProbe
    from kinoforge.core.warm_reuse.ephemeral_index import (
        EphemeralIndex,
        EphemeralIndexRow,
    )
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    EphemeralIndex(store=store).add(
        EphemeralIndexRow(
            id="orphan-pod-1",
            warm_attach_key="w",
            kinoforge_key="k-12345678901",
            endpoints={},
            provider="fake-provider",
            # "now" (local TZ, per project convention) — keeps age_s ~0 so
            # neither OVERAGE_REAP nor ORPHAN_REAP can fire regardless of
            # wall-clock time-of-day the suite happens to run at.
            created_at_local=datetime.now().isoformat(),
        )
    )

    class _LiveProbeProvider:
        """Minimal ComputeProvider stand-in: only probe_runtime is used."""

        def probe_runtime(self, pod_id: str) -> RuntimeProbe:
            return RuntimeProbe(
                pod_id=pod_id,
                found=True,
                container_uptime_s=60.0,
                gpu_util_pct=0.0,
                cpu_pct=0.0,
                cost_per_hr=None,
                probed_at_local=datetime.now().isoformat(),
            )

    provider = _LiveProbeProvider()
    ctx = _ctx([], store=store)

    with patch(
        "kinoforge.core.registry.get_provider",
        side_effect=lambda name: (
            (lambda: provider) if name == "fake-provider" else None
        ),
    ):
        code = _cmd_reap(_args(), ctx)

    out = capsys.readouterr().out
    assert code == 0
    assert "nothing to do" not in out, out
    assert "orphan-pod-1" in out, out
    row = next(line for line in out.splitlines() if "orphan-pod-1" in line)
    assert row.startswith(Verdict.LIVE.value), f"expected LIVE verdict row: {row!r}"


def test_reap_apply_include_orphans_destroys_index_only_orphan(tmp_path: Any) -> None:
    """--apply --include-orphans actually destroys an aged, idle index-only row.

    Bug this closes the remaining gap on: U18's stated cost was that
    `--include-orphans` is unreachable for the `--ephemeral` case, but the
    sibling test above only proves the CLI reaches `sweep()` and classifies
    a fresh row `LIVE` — it never exercises the *act* path. This writes a
    row old enough and idle enough to classify `ORPHAN_REAP` under a
    threshold that enables the age gate, and asserts the fake provider's
    `destroy_instance` was actually called with that pod's id — the act
    path, not just the dry-run classification.
    """
    from datetime import datetime, timedelta

    from kinoforge.core.interfaces import Lifecycle
    from kinoforge.core.runtime_probe import RuntimeProbe
    from kinoforge.core.warm_reuse.ephemeral_index import (
        EphemeralIndex,
        EphemeralIndexRow,
    )
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    old_created_at = (datetime.now() - timedelta(seconds=600)).isoformat()
    EphemeralIndex(store=store).add(
        EphemeralIndexRow(
            id="orphan-pod-old",
            warm_attach_key="w",
            kinoforge_key="k-12345678901",
            endpoints={},
            provider="fake-provider",
            created_at_local=old_created_at,
        )
    )

    class _IdleOldProvider:
        """Minimal ComputeProvider stand-in for the ORPHAN_REAP act path."""

        def __init__(self) -> None:
            self.destroyed: list[str] = []

        def probe_runtime(self, pod_id: str) -> RuntimeProbe:
            return RuntimeProbe(
                pod_id=pod_id,
                found=True,
                container_uptime_s=600.0,
                gpu_util_pct=0.0,
                cpu_pct=0.0,
                cost_per_hr=None,
                probed_at_local=datetime.now().isoformat(),
            )

        def list_instances(self) -> list[Any]:
            # Always "gone" — lets destroy_confirmed's poll succeed on the
            # first attempt without needing a real destroy side effect.
            return []

        def destroy_instance(self, instance_id: str) -> None:
            self.destroyed.append(instance_id)

    provider = _IdleOldProvider()

    class _FakeCfgWithOrphanAgeGate:
        def lifecycle(self) -> Lifecycle:
            # 60s age gate; the row above is 600s old and idle (gpu=cpu=0.0)
            # — both halves _ephemeral_orphan_predicate requires.
            return Lifecycle(ephemeral_orphan_age_s=60.0)

    ctx = _ctx([], cfg=_FakeCfgWithOrphanAgeGate(), store=store)

    with patch(
        "kinoforge.core.registry.get_provider",
        side_effect=lambda name: (
            (lambda: provider) if name == "fake-provider" else None
        ),
    ):
        code = _cmd_reap(_args(apply=True, include_orphans=True), ctx)

    assert code == 0
    assert provider.destroyed == ["orphan-pod-old"]


# ---------------------------------------------------------------------------
# Flag validation
# ---------------------------------------------------------------------------


def test_reap_include_orphans_without_apply_exit_4(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--include-orphans without --apply → exit 4 + stderr."""
    ctx = _ctx([])
    code = _cmd_reap(_args(include_orphans=True), ctx)
    err = capsys.readouterr().err
    assert code == 4
    assert "--apply" in err


def test_reap_force_forget_without_apply_exit_4() -> None:
    """--force-forget without --apply → exit 4."""
    ctx = _ctx([])
    code = _cmd_reap(_args(force_forget=True), ctx)
    assert code == 4


# ---------------------------------------------------------------------------
# --apply path
# ---------------------------------------------------------------------------


def test_reap_apply_routes_default_policy_to_sweep() -> None:
    """sweep is called with DEFAULT_APPLY_POLICY when --apply set."""
    from kinoforge.core.reaper import DEFAULT_APPLY_POLICY
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot={}, actions=[])
        _cmd_reap(_args(apply=True), ctx)
    assert (
        mock_sweep.call_args.kwargs["policy"].act_verdicts
        == DEFAULT_APPLY_POLICY.act_verdicts
    )


def test_reap_apply_include_orphans_extends_policy() -> None:
    """--include-orphans with --apply adds ORPHAN_REAP to policy."""
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot={}, actions=[])
        _cmd_reap(_args(apply=True, include_orphans=True), ctx)
    assert Verdict.ORPHAN_REAP in mock_sweep.call_args.kwargs["policy"].act_verdicts


def test_reap_apply_force_forget_extends_policy() -> None:
    """--force-forget with --apply adds UNROUTABLE to policy."""
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot={}, actions=[])
        _cmd_reap(_args(apply=True, force_forget=True), ctx)
    assert Verdict.UNROUTABLE in mock_sweep.call_args.kwargs["policy"].act_verdicts


# ---------------------------------------------------------------------------
# --strict
# ---------------------------------------------------------------------------


def test_reap_strict_with_unroutable_present_exits_3() -> None:
    """--strict with UNROUTABLE verdict present → exit 3."""
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "broken"}])
    snapshot = {"i-1": ({"id": "i-1", "provider": "broken"}, Verdict.UNROUTABLE)}
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=[])
        code = _cmd_reap(_args(strict=True), ctx)
    assert code == 3


def test_reap_strict_no_uncertainty_exits_0() -> None:
    """--strict with only LIVE verdicts → exit 0."""
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    snapshot = {"i-1": ({"id": "i-1", "provider": "fake"}, Verdict.LIVE)}
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=[])
        code = _cmd_reap(_args(strict=True), ctx)
    assert code == 0


def _capability_less_row(*, session_end_age: float) -> dict[str, Any]:
    """A skypilot ledger row with no heartbeat fields, aged past/inside grace.

    SkyPilot declares no ``Capability.HEARTBEAT_READ`` and
    ``_adapters.py:185-189`` forces ``heartbeat_mode: none`` for it, so a
    real row of this shape carries neither sentinel field at any age.

    Args:
        session_end_age: Seconds between the last driver detach and ``now``.

    Returns:
        A ledger-shaped dict for a provider that declares no
        ``Capability.HEARTBEAT_READ``.
    """
    now = 1_800_000_000.0
    return {
        "id": "i-1",
        "provider": "skypilot",
        "created_at": now - (session_end_age + 60.0),
        "session_end": now - session_end_age,
        "grace_after_session_s": 1800.0,
    }


def _real_verdict(entry: dict[str, Any]) -> Any:
    """Classify ``entry`` with the real classifier, not a hand-picked value.

    Args:
        entry: Ledger-shaped dict.

    Returns:
        The Verdict ``kinoforge.core.reaper.classify`` assigns.
    """
    from kinoforge.core.reaper import classify

    return classify(
        entry,
        live_pod_ids={"i-1"},
        now=1_800_000_000.0,
        idle_timeout_s=600.0,
        max_lifetime_s=18_000.0,
        heartbeat_interval_s=30.0,
        grace_after_session_s=1800.0,
    )


def _strict_exit_code(entry: dict[str, Any], verdict: Any) -> int:
    """Run ``reap --strict`` over a one-row snapshot and return its exit code.

    Only ``sweep`` is stubbed — that is the provider I/O. The verdict is
    supplied by the caller from the real classifier, so the exit-code logic
    under test is the production one.

    Args:
        entry: Ledger-shaped dict placed in the sweep snapshot.
        verdict: The Verdict the real classifier assigned to ``entry``.

    Returns:
        The process exit code ``_cmd_reap`` returns.
    """
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([entry])
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(
            snapshot={"i-1": (entry, verdict)}, actions=[]
        )
        return _cmd_reap(_args(strict=True), ctx)


def test_reap_strict_expected_absence_within_grace_exits_3() -> None:
    """--strict on a capability-less row still inside grace → exit 3.

    A provider that declares no ``Capability.HEARTBEAT_READ`` can never
    produce heartbeat data, so its rows classify
    HEARTBEAT_SUBSTRATE_MISSING, which IS in DEFAULT_STRICT_VERDICTS.
    ``--strict`` therefore reports the uncertainty. This path had no
    coverage in either direction before; the verdict comes from the real
    classifier so the test breaks loudly if the gate stops producing it.
    """
    from kinoforge.core.reaper import Verdict

    entry = _capability_less_row(session_end_age=1799.0)
    verdict = _real_verdict(entry)
    assert verdict == Verdict.HEARTBEAT_SUBSTRATE_MISSING  # precondition

    assert _strict_exit_code(entry, verdict) == 3


def test_reap_strict_expected_absence_past_grace_also_exits_3() -> None:
    """--strict on a capability-less row PAST grace → exit 3, same as inside.

    The grace boundary deliberately does NOT move this exit code. The
    reaper fails open on an expected heartbeat absence: it cannot tell a
    stranded row from a warm-re-attached row that is actively rendering,
    because warm re-attach writes nothing to the ledger and the two are
    byte-identical (design §8, 2026-08-16). So age never converts the
    verdict into an actionable one, and ``--strict`` keeps signalling
    "uncertain" at every age.

    Pinning both sides of the boundary is the point: a future change that
    makes an aged row actionable (ORPHAN_REAP, say) silently drops this
    exit code to 0 and a CI gate watching for 3 goes quiet. It fails here
    first, with a reason.
    """
    from kinoforge.core.reaper import Verdict

    entry = _capability_less_row(session_end_age=1801.0)
    verdict = _real_verdict(entry)
    assert verdict == Verdict.HEARTBEAT_SUBSTRATE_MISSING  # precondition

    assert _strict_exit_code(entry, verdict) == 3


def test_reap_id_flag_restricts_sweep_to_one_entry() -> None:
    """--id X passes a ledger that surfaces only one entry to sweep."""
    from kinoforge.core.reaper_actor import SweepReport

    entries = [
        {"id": "i-1", "provider": "fake"},
        {"id": "i-2", "provider": "fake"},
    ]
    ctx = _ctx(entries)

    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot={}, actions=[])
        _cmd_reap(_args(id="i-1"), ctx)

    # sweep is called with a ledger whose entries() returns only the
    # matching entry — verifies the --id filter took effect at the
    # boundary the implementation chose.
    passed_ledger = mock_sweep.call_args.kwargs["ledger"]
    filtered = list(passed_ledger.entries())
    assert len(filtered) == 1
    assert filtered[0]["id"] == "i-1"


def test_reap_id_scopes_the_index_check_to_the_named_id(
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--id naming an id absent from BOTH ledger and index must still no-op.

    Bug caught (review round 2, Task 3): the union guard consulted
    ``EphemeralIndex().rows()`` globally, unscoped by ``--id``. So an
    UNRELATED ephemeral row — belonging to a pod the operator never
    named — was enough to defeat the short-circuit and let
    `reap --id <typo'd-id>` reach `sweep()`. Under `--apply
    --include-orphans` that turns a formerly-safe no-op into a command that
    can classify and destroy a pod the operator did not ask about. This
    test writes exactly one index row for a DIFFERENT id and asks for an id
    that is in neither the ledger nor the index: `sweep()` must never be
    called, and the printed message must name the id that was searched for.
    """
    from datetime import datetime

    from kinoforge.core.warm_reuse.ephemeral_index import (
        EphemeralIndex,
        EphemeralIndexRow,
    )
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    EphemeralIndex(store=store).add(
        EphemeralIndexRow(
            id="unrelated-pod",
            warm_attach_key="w",
            kinoforge_key="k-12345678901",
            endpoints={},
            provider="fake-provider",
            created_at_local=datetime.now().isoformat(),
        )
    )
    ctx = _ctx([], store=store)

    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        code = _cmd_reap(_args(id="typo-d-id"), ctx)

    assert code == 0
    mock_sweep.assert_not_called()
    out = capsys.readouterr().out
    assert "typo-d-id" in out, out


def test_reap_id_scopes_sweep_apply_to_the_named_pod_only(tmp_path: Any) -> None:
    """--id X --apply --include-orphans must destroy only X.

    Bug caught (U26 / whole-branch review Finding 1): the short-circuit
    guard above is scoped by ``--id`` (``index_rows`` filtered to
    ``single_id``), but once past it, `_cmd_reap` handed `sweep()` the
    FULL, unscoped `EphemeralIndex` — `sweep()` unions in every row in
    the index (reaper_actor.py) regardless of the id-scoped ledger view.
    So `--id target-pod` (an id that DOES exist in the index) would reach
    `sweep()`, which would then ALSO classify and, under `--apply
    --include-orphans`, destroy an unrelated orphan-eligible row the
    operator never named. `--id` is precisely the flag an operator uses
    to *restrict* scope, so this is a live-pod-safety bug, not cosmetic.

    Fixture mirrors the proven `test_reap_apply_include_orphans_destroys_
    index_only_orphan` shape: two rows, both 600s old under a 60s
    `ephemeral_orphan_age_s` gate (both independently orphan-eligible).
    Only "target-pod" is named by `--id`; "unrelated-pod" must survive.
    """
    from datetime import datetime, timedelta

    from kinoforge.core.interfaces import Lifecycle
    from kinoforge.core.runtime_probe import RuntimeProbe
    from kinoforge.core.warm_reuse.ephemeral_index import (
        EphemeralIndex,
        EphemeralIndexRow,
    )
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    old_created_at = (datetime.now() - timedelta(seconds=600)).isoformat()
    index = EphemeralIndex(store=store)
    for pod_id in ("target-pod", "unrelated-pod"):
        index.add(
            EphemeralIndexRow(
                id=pod_id,
                warm_attach_key="w",
                kinoforge_key="k-12345678901",
                endpoints={},
                provider="fake-provider",
                created_at_local=old_created_at,
            )
        )

    class _IdleOldProvider:
        """Minimal ComputeProvider stand-in for the ORPHAN_REAP act path."""

        def __init__(self) -> None:
            self.destroyed: list[str] = []

        def probe_runtime(self, pod_id: str) -> RuntimeProbe:
            return RuntimeProbe(
                pod_id=pod_id,
                found=True,
                container_uptime_s=600.0,
                gpu_util_pct=0.0,
                cpu_pct=0.0,
                cost_per_hr=None,
                probed_at_local=datetime.now().isoformat(),
            )

        def list_instances(self) -> list[Any]:
            return []

        def destroy_instance(self, instance_id: str) -> None:
            self.destroyed.append(instance_id)

    provider = _IdleOldProvider()

    class _FakeCfgWithOrphanAgeGate:
        def lifecycle(self) -> Lifecycle:
            return Lifecycle(ephemeral_orphan_age_s=60.0)

    ctx = _ctx([], cfg=_FakeCfgWithOrphanAgeGate(), store=store)

    with patch(
        "kinoforge.core.registry.get_provider",
        side_effect=lambda name: (
            (lambda: provider) if name == "fake-provider" else None
        ),
    ):
        code = _cmd_reap(_args(apply=True, include_orphans=True, id="target-pod"), ctx)

    assert code == 0
    assert provider.destroyed == ["target-pod"], provider.destroyed


# ---------------------------------------------------------------------------
# --format json
# ---------------------------------------------------------------------------


def test_reap_format_json_emits_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    """--format json emits valid JSONL (one record per line)."""
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    snapshot = {
        "i-1": ({"id": "i-1", "provider": "fake", "created_at": 0.0}, Verdict.LIVE)
    }
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=[])
        code = _cmd_reap(_args(format="json"), ctx)

    out = capsys.readouterr().out.strip().splitlines()
    # Every line must be parseable JSON.
    for line in out:
        json.loads(line)
    assert code == 0


# ---------------------------------------------------------------------------
# action="failed" → exit 2
# ---------------------------------------------------------------------------


def test_reap_apply_with_failed_action_exits_2() -> None:
    """One action=failed under --apply → exit 2."""
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import ActionResult, SweepReport

    ctx = _ctx([{"id": "i-1", "provider": "fake"}])
    snapshot = {"i-1": ({"id": "i-1", "provider": "fake"}, Verdict.IDLE_REAP)}
    actions = [
        ActionResult(
            instance_id="i-1",
            snapshot_verdict=Verdict.IDLE_REAP,
            applied_verdict=Verdict.IDLE_REAP,
            action="failed",
            reason="simulated",
        )
    ]
    with patch("kinoforge.cli._commands.sweep") as mock_sweep:
        mock_sweep.return_value = SweepReport(snapshot=snapshot, actions=actions)
        code = _cmd_reap(_args(apply=True), ctx)
    assert code == 2


# ---------------------------------------------------------------------------
# B7: deferred-session-claim render coverage
# ---------------------------------------------------------------------------


def test_emit_reap_human_includes_deferred_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_emit_reap_human summary line counts <N> deferred for B7.

    Bug catch: a forgotten `deferred` accumulator would hide the B7
    action from operators reading the human-readable output.
    """
    from kinoforge.cli._commands import _emit_reap_human
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import ActionResult, SweepReport

    actions = [
        ActionResult(
            instance_id="i-a",
            snapshot_verdict=Verdict.IDLE_REAP,
            applied_verdict=Verdict.IDLE_REAP,
            action="destroyed_and_forgot",
        ),
        ActionResult(
            instance_id="i-b",
            snapshot_verdict=Verdict.IDLE_REAP,
            applied_verdict=Verdict.IDLE_REAP,
            action="deferred-session-claim",
            reason="held by pid 4242; orchestrator mid-session-claim",
        ),
        ActionResult(
            instance_id="i-c",
            snapshot_verdict=Verdict.IDLE_REAP,
            applied_verdict=Verdict.IDLE_REAP,
            action="deferred-session-claim",
            reason="held by pid 4243; orchestrator mid-session-claim",
        ),
    ]
    report = SweepReport(
        snapshot={
            "i-a": (
                {"id": "i-a", "provider": "local", "created_at": 0.0},
                Verdict.IDLE_REAP,
            ),
            "i-b": (
                {"id": "i-b", "provider": "local", "created_at": 0.0},
                Verdict.IDLE_REAP,
            ),
            "i-c": (
                {"id": "i-c", "provider": "local", "created_at": 0.0},
                Verdict.IDLE_REAP,
            ),
        },
        actions=actions,
    )

    _emit_reap_human(report, applied=True, include_orphans=False)

    captured = capsys.readouterr()
    assert "2 deferred" in captured.out, captured.out


def test_emit_reap_jsonl_handles_deferred_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_emit_reap_jsonl emits the deferred-session-claim literal in
    per-action records without crashing on the unknown action.

    Discriminating: the JSONL emitter passes `action` through as-is; a
    regression that introduced an action-enum gate would drop the new
    literal silently.
    """
    from kinoforge.cli._commands import _emit_reap_jsonl
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import ActionResult, SweepReport

    actions = [
        ActionResult(
            instance_id="i-d",
            snapshot_verdict=Verdict.IDLE_REAP,
            applied_verdict=Verdict.IDLE_REAP,
            action="deferred-session-claim",
            reason="held by pid 4244; orchestrator mid-session-claim",
        ),
    ]
    report = SweepReport(
        snapshot={
            "i-d": (
                {"id": "i-d", "provider": "local", "created_at": 0.0},
                Verdict.IDLE_REAP,
            ),
        },
        actions=actions,
    )

    _emit_reap_jsonl(report)

    captured = capsys.readouterr()
    found = False
    for line in captured.out.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("action") == "deferred-session-claim":
            found = True
            assert rec.get("reason", "").startswith("held by pid ")
            break
    assert found, captured.out


# ---------------------------------------------------------------------------
# Empty ledger — the --format json contract holds on the short-circuit path
# ---------------------------------------------------------------------------


def test_reap_empty_ledger_json_emits_parseable_records(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`reap --format json` on an empty ledger stays machine-readable.

    Bug caught: the empty-ledger early return printed the human sentence
    "reap: ledger empty (nothing to do)" — the historical wording, since
    reworded to "reap: ledger and ephemeral index both empty (nothing to
    do)" by U18 — without consulting the requested format, so `kinoforge
    reap --format json | jq` died on exactly the case a scripted teardown
    check hits most — the one where there is nothing left to reap. Every
    stdout line must parse as JSON, and the header must report zero entries
    so the consumer can distinguish "empty" from "not run".
    """
    ctx = _ctx([])

    code = _cmd_reap(_args(format="json"), ctx)

    assert code == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "empty ledger emitted nothing at all under --format json"
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:  # pragma: no cover - failure path
            pytest.fail(f"non-JSON line under --format json: {line!r} ({exc})")
    headers = [r for r in records if r.get("type") == "header"]
    assert len(headers) == 1, f"expected exactly one header record, got {records}"
    assert headers[0]["entries"] == 0
    assert "nothing to do" not in out


def test_reap_empty_ledger_human_keeps_the_sentence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default human format still says so in words.

    Bug caught: fixing the JSON path by always emitting records would strip the
    operator-facing message that makes an empty `kinoforge reap` legible. The
    sentence names both things that were searched (ledger AND ephemeral
    index, per U18) rather than the pre-fix "ledger empty" wording, which
    was misleading — the ledger being empty was never proof there was
    nothing to reap.
    """
    ctx = _ctx([])

    code = _cmd_reap(_args(), ctx)

    assert code == 0
    assert (
        "reap: ledger and ephemeral index both empty (nothing to do)"
        in capsys.readouterr().out
    )


def test_emit_reap_human_separates_the_longest_verdict_from_the_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every Verdict renders inside its column, so the id stays a separate word.

    Bug caught (matrix T1-07 / T1-22, follow-up F6): the verdict field was
    padded to 18 while ``HEARTBEAT_SUBSTRATE_MISSING`` is 27 characters, so
    the longest verdict overflowed and the row printed
    ``HEARTBEAT_SUBSTRATE_MISSINGrun-20260906-010255`` — no separator, and the
    id unreadable at exactly the moment an operator is deciding whether to
    reap. The required width is derived from the enum, not from the format
    string, so adding a Verdict member longer than the column fails here too.

    Both the header and the data row are checked at the same offset: a fix
    that widened only one of them would leave the table misaligned.
    """
    from kinoforge.cli._commands import _emit_reap_human
    from kinoforge.core.reaper import Verdict
    from kinoforge.core.reaper_actor import SweepReport

    longest = max(Verdict, key=lambda v: len(v.value))
    assert longest.value == "HEARTBEAT_SUBSTRATE_MISSING", (
        "fixture assumption changed: update the expected longest verdict"
    )
    eid = "run-20260906-010255"
    report = SweepReport(
        snapshot={
            eid: ({"id": eid, "provider": "modal", "created_at": 0.0}, longest),
        },
        actions=[],
    )

    _emit_reap_human(report, applied=False, include_orphans=False)

    lines = capsys.readouterr().out.splitlines()
    header = next(ln for ln in lines if ln.startswith("verdict"))
    row = next(ln for ln in lines if ln.startswith(longest.value))

    assert row[len(longest.value)] == " ", (
        f"verdict column too narrow for {longest.value!r}: {row!r}"
    )
    assert row.index(eid) == header.index("id"), (
        f"id column misaligned with its header: row={row!r} header={header!r}"
    )
