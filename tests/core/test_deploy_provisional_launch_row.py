"""Behavior: ``kinoforge deploy`` is not the one launch path without a row.

compute-seam S5 closed finding F12 for ``deploy_session``: the orchestrator
writes a durable ``kf_launch_phase=launching`` row BEFORE ``create_instance``,
so a kill inside the multi-minute create still leaves a handle on whatever the
provider may already have booked. ``deploy()`` — the one-shot
``kinoforge deploy`` entry point, and the launch most likely to be interrupted —
took no store and so installed no such row.

These tests pin the deploy() path to the same contract the sibling file
(``test_provisional_launch_row.py``) pins deploy_session to: write before
create, keep the row on an undeclared raise (ruling C1), forget it only for the
errors that prove nothing was booked, collapse onto the real row on success.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import TYPE_CHECKING, Any

import pytest

# Import providers/engines/sources so they self-register for deploy().
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401 — registers https:// source
from kinoforge.cli._reconcile import _reconcile_dead_ledger_entries
from kinoforge.core.config import load_config
from kinoforge.core.errors import CapacityError
from kinoforge.core.interfaces import Instance, InstanceSpec
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.orchestrator import deploy
from kinoforge.providers.local import LocalProvider
from kinoforge.stores.local import LocalArtifactStore
from tests.core.test_orchestrator import _COMPUTE_YAML, _compute_cfg, _make_engine

if TYPE_CHECKING:
    from pathlib import Path


def _ledger_ids(ledger: Ledger) -> list[str]:
    """Return the ids of every row in *ledger*, in on-disk order.

    Args:
        ledger: The ledger to read.

    Returns:
        The ``id`` of each entry. Order is load-bearing: ``Ledger.record``
        appends, so a duplicated write shows up as a repeated id.
    """
    return [str(entry["id"]) for entry in ledger.entries()]


class _LedgerPeekProvider(LocalProvider):
    """A local provider that reads the ledger from INSIDE ``create_instance``.

    The whole F12 claim is about a window that only exists mid-create, so the
    observation has to be made there. A ledger built over the same store — not a
    handed-in row list — is used so the read goes through the same persistence
    the CLI reads.
    """

    def __init__(self, ledger: Ledger) -> None:
        """Initialise the peeking provider.

        Args:
            ledger: A ledger over the store under test.
        """
        super().__init__()
        self._peek_ledger = ledger
        self.seen: dict[str, Any] | None = None
        self.rows_mid_create: list[str] = []
        self.specs: list[InstanceSpec] = []

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Snapshot the ledger, then create as the real local provider does.

        Args:
            spec: The instance spec the orchestrator built.

        Returns:
            The created instance.
        """
        self.specs.append(spec)
        entries = self._peek_ledger.entries()
        self.rows_mid_create = [str(entry["id"]) for entry in entries]
        self.seen = entries[0] if entries else None
        return super().create_instance(spec)


class _SameKeyPeekProvider(_LedgerPeekProvider):
    """SkyPilot's shape: the provider's id IS the client-side ``run_id``.

    One key then holds both rows, which is what makes the collapse a
    ``forget_provisional`` and not a ``forget``.
    """

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Create, then re-key the instance onto the run id.

        Args:
            spec: The instance spec the orchestrator built.

        Returns:
            The created instance, keyed by ``spec.run_id``.
        """
        instance = super().create_instance(spec)
        return dataclasses.replace(instance, id=spec.run_id or instance.id)


class _RaisingProvider(LocalProvider):
    """A provider whose ``create_instance`` always raises *exc*."""

    def __init__(self, exc: BaseException) -> None:
        """Initialise the raising provider.

        Args:
            exc: The exception ``create_instance`` raises.
        """
        super().__init__()
        self._exc = exc

    def create_instance(self, spec: InstanceSpec) -> Instance:
        """Raise the configured exception.

        Args:
            spec: Ignored.

        Raises:
            BaseException: Always — the exception this provider was built with.
        """
        del spec
        raise self._exc


def test_the_row_is_recorded_before_create_instance_on_the_deploy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record strictly precedes create, observed on one shared sequence.

    Both halves are needed. The sequence pins the ORDER of the two calls; the
    mid-create read pins that the row was already DURABLE at that moment, which
    a call-order list alone cannot show.

    Bug caught: writing the row after ``create_instance`` returns. Every
    end-state assertion in this file would still pass while a SIGKILL inside the
    multi-minute create left a billing pod that no kinoforge command can see —
    which is the entire point of the row.
    """
    sequence: list[str] = []
    store = LocalArtifactStore(tmp_path)

    class _SequencingLedger(Ledger):
        def record(self, instance: Instance, **kwargs: Any) -> None:
            sequence.append("record")
            super().record(instance, **kwargs)

    class _SequencingProvider(_LedgerPeekProvider):
        def create_instance(self, spec: InstanceSpec) -> Instance:
            sequence.append("create")
            return super().create_instance(spec)

    # deploy() builds its own ledger over the store it is handed, so the
    # sequencing subclass has to be installed at the construction seam rather
    # than passed in.
    monkeypatch.setattr("kinoforge.core.orchestrator.Ledger", _SequencingLedger)
    provider = _SequencingProvider(Ledger(store=store))

    deploy(
        _compute_cfg(),
        provider=provider,
        engine=_make_engine(),
        store=store,
        run_id="kf-deploy-order",
    )

    assert sequence[:2] == ["record", "create"]
    assert provider.seen is not None, (
        "no provisional row was visible from inside create_instance — "
        "deploy() did not write one"
    )
    assert provider.seen["id"] == "kf-deploy-order"
    assert provider.seen["tags"]["kf_launch_phase"] == "launching"
    assert provider.seen["tags"]["kf_run_id"] == "kf-deploy-order"
    assert provider.seen["provider"] == "local"


def test_the_deploy_row_is_keyed_by_the_name_the_provider_will_use(
    tmp_path: Path,
) -> None:
    """The row key and ``spec.run_id`` are the same string.

    ``cli/_reconcile._adopt_or_age_out`` resolves a launching row by matching
    its id against the NAME on the provider's listing, and that name is
    ``spec.run_id`` (``spec.run_id or "kinoforge-pod"`` on RunPod,
    ``or "skypilot-cluster"`` on SkyPilot).

    Bug caught: minting a row key that the spec never carries — e.g. keying the
    row by a fresh uuid while the pod is still named ``kinoforge-pod``. Adoption
    then never matches, and the row protecting a live, billing pod is aged out
    instead of adopted.
    """
    store = LocalArtifactStore(tmp_path)
    provider = _LedgerPeekProvider(Ledger(store=store))

    deploy(_compute_cfg(), provider=provider, engine=_make_engine(), store=store)

    assert provider.seen is not None
    assert provider.specs[0].run_id == provider.seen["id"], (
        "the provisional row is keyed by something the provider will not name "
        "the resource — reconcile can never adopt it"
    )
    assert provider.specs[0].run_id != "", (
        "an empty run_id makes the provider fall back to a shared default name "
        "and _record_provisional_row skip the row entirely"
    )


def test_a_deploy_create_that_raises_keeps_its_provisional_row(
    tmp_path: Path,
) -> None:
    """Ruling C1, on the deploy path: a raise is not proof nothing was booked.

    Bug caught: restoring the unconditional forget. ``sky.launch`` raising out
    of a failed setup script leaves an UP cluster, and a Ctrl-C arrives as a
    BaseException while the API server goes on creating one — deleting the row
    in either case leaves a live resource with zero ledger rows, the F12 hole in
    reverse.
    """
    store = LocalArtifactStore(tmp_path)

    with pytest.raises(RuntimeError, match="boom"):
        deploy(
            _compute_cfg(),
            provider=_RaisingProvider(RuntimeError("boom")),
            engine=_make_engine(),
            store=store,
            run_id="kf-deploy-raise",
        )

    ledger = Ledger(store=store)
    assert _ledger_ids(ledger) == ["kf-deploy-raise"]
    entry = ledger.read("kf-deploy-raise")
    assert entry is not None
    assert entry["tags"]["kf_launch_phase"] == "launching"


def test_a_deploy_capacity_error_forgets_the_provisional_row(
    tmp_path: Path,
) -> None:
    """``CapacityError`` is the portable "nothing was booked" proof.

    Bug caught: keeping the row for a create that provably created nothing. Its
    ``est_spend`` (age × rate) then inflates forever and shows up in every
    ``kinoforge list`` — cli/_reconcile's "$210 phantom pod".
    """
    store = LocalArtifactStore(tmp_path)

    with pytest.raises(CapacityError):
        deploy(
            _compute_cfg(),
            provider=_RaisingProvider(CapacityError("no offers")),
            engine=_make_engine(),
            store=store,
            run_id="kf-deploy-capacity",
        )

    assert _ledger_ids(Ledger(store=store)) == []


def test_a_successful_deploy_leaves_exactly_one_real_row(tmp_path: Path) -> None:
    """Two distinct keys (RunPod/Modal shape) end as one real row.

    Bug caught: never collapsing — the launching stub then outlives the launch
    and ``kinoforge list`` shows two rows per pod forever, one of them a stub
    the reconciler will keep trying to resolve.
    """
    store = LocalArtifactStore(tmp_path)
    provider = _LedgerPeekProvider(Ledger(store=store))

    result = deploy(
        _compute_cfg(),
        provider=provider,
        engine=_make_engine(),
        store=store,
        run_id="kf-deploy-collapse",
    )

    assert result.instance is not None
    assert result.instance.id != "kf-deploy-collapse", (
        "this test is only meaningful on the two-key shape"
    )
    ledger = Ledger(store=store)
    assert _ledger_ids(ledger) == [result.instance.id]
    entry = ledger.read(result.instance.id)
    assert entry is not None
    assert "kf_launch_phase" not in entry["tags"]


def test_a_successful_same_key_deploy_keeps_the_real_row(tmp_path: Path) -> None:
    """One key holding two rows collapses onto the REAL one.

    Bug caught: writing the collapse as a plain ``Ledger.forget``, which matches
    on id alone. On SkyPilot the cluster name IS the run id, so a forget deletes
    both rows and a live, billing cluster becomes invisible.
    """
    store = LocalArtifactStore(tmp_path)
    provider = _SameKeyPeekProvider(Ledger(store=store))

    result = deploy(
        _compute_cfg(),
        provider=provider,
        engine=_make_engine(),
        store=store,
        run_id="kf-deploy-same-key",
    )

    assert result.instance is not None
    assert result.instance.id == "kf-deploy-same-key"
    ledger = Ledger(store=store)
    assert _ledger_ids(ledger) == ["kf-deploy-same-key"]
    entry = ledger.read("kf-deploy-same-key")
    assert entry is not None
    assert "kf_launch_phase" not in entry["tags"], (
        "the provisional stub outlived the real row — the collapse kept the wrong one"
    )


def test_a_dry_run_writes_no_row(tmp_path: Path) -> None:
    """``--dry-run`` never calls create, so it must never write the row.

    Bug caught: hoisting the write above the dry-run guard. Every
    ``kinoforge deploy --dry-run`` would then leave a launching row for a launch
    that never happened, and the reconciler would spend its grace window on it.
    """
    store = LocalArtifactStore(tmp_path)
    provider = _LedgerPeekProvider(Ledger(store=store))

    result = deploy(
        _compute_cfg(),
        dry_run=True,
        provider=provider,
        engine=_make_engine(),
        store=store,
        run_id="kf-deploy-dry",
    )

    assert result.instance is None
    assert _ledger_ids(Ledger(store=store)) == []


def test_deploy_without_a_store_still_launches_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """The parameter is additive: every existing caller keeps working.

    Bug caught: making ``store`` required, or dereferencing ``None`` on the
    default path — either turns every non-CLI caller of ``deploy()`` (tests, the
    live smokes, the golden harness) into an AttributeError.
    """
    store = LocalArtifactStore(tmp_path)
    provider = _LedgerPeekProvider(Ledger(store=store))

    result = deploy(_compute_cfg(), provider=provider, engine=_make_engine())

    assert result.instance is not None
    assert _ledger_ids(Ledger(store=store)) == []


def test_an_abandoned_deploy_launch_is_reconcilable_and_ages_out(
    tmp_path: Path,
) -> None:
    """An interrupted deploy leaves a row the reconciler can resolve.

    Simulates the exposure the brief names: Ctrl-C during a multi-minute
    provision. The row must survive the interruption (C1), must be left ALONE
    while the launch could still be in flight, and must eventually be cleared —
    ``local`` exposes no adoptable listing, so it takes the provider-agnostic
    age-out.

    Bug caught: a deploy-path row that no branch in ``cli/_reconcile`` can ever
    clear, which is a permanent phantom in every ``kinoforge list``; and the
    mirror bug of clearing it inside the grace window, which reaps the row
    protecting a boot that is still running.
    """
    store = LocalArtifactStore(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        deploy(
            _compute_cfg(),
            provider=_RaisingProvider(KeyboardInterrupt()),
            engine=_make_engine(),
            store=store,
            run_id="kf-deploy-abandoned",
        )

    ledger = Ledger(store=store)
    entry = ledger.read("kf-deploy-abandoned")
    assert entry is not None, "the interrupted launch left nothing to reconcile"
    created_at = float(entry["created_at"])

    kept = _reconcile_dead_ledger_entries(
        ledger,
        ledger.entries(),
        now=created_at + 60.0,
        launching_grace_s=1800.0,
    )
    assert kept == []
    assert _ledger_ids(ledger) == ["kf-deploy-abandoned"], (
        "a row younger than the grace window was reaped — that is a launch "
        "still in flight"
    )

    gone = _reconcile_dead_ledger_entries(
        ledger,
        ledger.entries(),
        now=created_at + 1801.0,
        launching_grace_s=1800.0,
    )
    assert gone == ["kf-deploy-abandoned"]
    assert _ledger_ids(ledger) == []


def test_deploy_installs_the_heartbeat_endpoint_it_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deploy path still goes through ``_resolve_provider``.

    That function is the ONLY site that installs the B5a heartbeat substrate
    endpoint, and it only runs when the provider is resolved rather than handed
    in.

    Bug caught: option (c) from the brief — having ``_cmd_deploy`` build the
    provider itself and pass it in. ``_resolve_provider`` would return the
    injected object immediately, ``compute.heartbeat_mode`` would be silently
    dropped, and nothing would raise.
    """
    sentinel = object()
    installed: list[object | None] = []

    monkeypatch.setattr(
        "kinoforge._adapters.build_heartbeat_endpoint_for",
        lambda cfg, creds: sentinel,
    )
    monkeypatch.setattr(
        LocalProvider,
        "set_heartbeat_endpoint",
        lambda self, endpoint: installed.append(endpoint),
        raising=False,
    )

    cfg = load_config(
        _COMPUTE_YAML.replace(
            "  image: fake:latest",
            "  image: fake:latest\n  heartbeat_mode: graphql-tag",
        )
    )

    deploy(cfg, engine=_make_engine())

    assert installed == [sentinel], (
        "deploy() did not resolve its provider through _resolve_provider, so "
        "compute.heartbeat_mode was dropped"
    )


def _orchestrator_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the WARNING-or-higher messages the orchestrator logger emitted.

    Scoped by logger name on purpose: ``kinoforge.validation`` warns about
    field-support gaps on every deploy of the fake/local pair, and that noise
    must not be able to satisfy — or break — an assertion about deploy()'s
    own warning.

    Args:
        caplog: The pytest log-capture fixture.

    Returns:
        The captured messages, in emission order.
    """
    return [
        r.message
        for r in caplog.records
        if r.name == "kinoforge.orchestrator" and r.levelno >= logging.WARNING
    ]


def test_deploy_without_a_store_warns_that_nothing_will_protect_the_launch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The unprotected path says so, and only that path says so.

    ``store=None`` is legitimate for library and test callers, so it must not
    raise — but a safety mechanism that is present in the signature and absent
    in effect must not be SILENT about it either (the Brief 2 principle). The
    warning names the consequence a future caller is opting into: no pre-launch
    record, so a mid-create interruption leaves a billing resource nothing can
    find.

    Bug caught: dropping the warning, logging it below WARNING (invisible at
    the default level), or hoisting it above the dry-run guard / outside the
    ``store is None`` branch — where it becomes noise on every ``--dry-run``
    and on the one production caller that does pass a store.
    """
    store = LocalArtifactStore(tmp_path)
    provider = _LedgerPeekProvider(Ledger(store=store))
    caplog.set_level(logging.WARNING, logger="kinoforge.orchestrator")

    result = deploy(
        _compute_cfg(),
        provider=provider,
        engine=_make_engine(),
        run_id="kf-deploy-unprotected",
    )

    assert result.instance is not None
    warnings = _orchestrator_warnings(caplog)
    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    assert "no pre-launch record" in warnings[0]
    assert "kf-deploy-unprotected" in warnings[0], (
        "the warning does not name the launch it failed to protect"
    )

    caplog.clear()
    deploy(
        _compute_cfg(),
        dry_run=True,
        provider=provider,
        engine=_make_engine(),
        run_id="kf-deploy-unprotected-dry",
    )
    deploy(
        _compute_cfg(),
        provider=provider,
        engine=_make_engine(),
        store=store,
        run_id="kf-deploy-protected",
    )
    assert _orchestrator_warnings(caplog) == [], (
        "the warning fired on a path that never reaches create_instance, or on "
        "one that IS protected"
    )
