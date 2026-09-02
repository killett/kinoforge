"""Behavior: an instance that bills above the cap is destroyed, loudly.

F4 in one sentence: a Lambda A100 billed $1.99/hr under a $1.09 cap and every
kinoforge surface reported $1.09, because the cap was a filter over a catalog
the optimizer never consulted. The filter cannot see the optimizer's choice;
only a readback can, and a readback is worthless unless something acts on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest

from kinoforge.core import orchestrator
from kinoforge.core.capabilities import Capability
from kinoforge.core.errors import RateCapExceeded
from kinoforge.core.interfaces import (
    Instance,
    Launch,
    Lifecycle,
    Offer,
    Placement,
    RenderedProvision,
    SetupStep,
)
from kinoforge.core.orchestrator import _provision_instance_and_build_backend

if TYPE_CHECKING:
    from collections.abc import Callable

    from kinoforge.core.interfaces import ComputeProvider, InstanceSpec


class _FakeProvider:
    """Minimal ComputeProvider surface the provision path actually touches."""

    name = "fakeprovider"

    def __init__(
        self,
        *,
        realized: float | None,
        caps: frozenset[Capability],
        catalog_rate: float,
        destroy_raises: Exception | None,
    ) -> None:
        self._realized = realized
        self._caps = caps
        self._catalog_rate = catalog_rate
        self._destroy_raises = destroy_raises
        self.destroyed: list[str] = []
        self.realized_calls = 0
        self.instance = Instance(
            id="inst-1",
            provider=self.name,
            status="ready",
            created_at=0.0,
            endpoints={"8000": "https://inst-1-8000"},
            tags={"sku": "A100:1", "cloud": "lambda"},
            cost_rate_usd_per_hr=catalog_rate,
        )

    def capabilities(self, shape: Any = None) -> frozenset[Capability]:  # noqa: ANN401
        return self._caps

    def find_offers(self, reqs: Any) -> list[Offer]:  # noqa: ANN401
        return [
            Offer(
                id="X1",
                gpu_type="X1",
                vram_gb=80,
                cuda="12.4",
                cost_rate_usd_per_hr=self._catalog_rate,
            )
        ]

    def create_instance(self, spec: InstanceSpec) -> Instance:
        return self.instance

    def get_instance(self, instance_id: str) -> Instance:
        return self.instance

    def destroy_instance(self, instance_id: str) -> None:
        if self._destroy_raises is not None:
            raise self._destroy_raises
        self.destroyed.append(instance_id)

    def realized_rate(self, instance: Instance) -> float | None:
        self.realized_calls += 1
        return self._realized


@dataclass
class _Harness:
    """One drive of the provision path, with everything the tests assert on."""

    provider: _FakeProvider
    cap: float
    run: Callable[[], None]
    recorded: list[str] = field(default_factory=list)
    rate_corrections: list[tuple[str, float]] = field(default_factory=list)
    provisioned_instances: list[Instance] = field(default_factory=list)

    @property
    def instance_id(self) -> str:
        return self.provider.instance.id

    @property
    def destroyed(self) -> list[str]:
        return self.provider.destroyed

    @property
    def realized_calls(self) -> int:
        return self.provider.realized_calls

    @property
    def provisioned(self) -> bool:
        return bool(self.provisioned_instances)

    @property
    def final_instance(self) -> Instance:
        return self.provisioned_instances[-1]


@pytest.fixture
def rate_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[..., _Harness]:
    """Return a factory that drives ``_provision_instance_and_build_backend``.

    ``_provision_compute_once`` is replaced by a recorder: the tests ask "did
    the expensive part of the boot happen, and against which instance", which
    is exactly what that call answers, and driving the real one would drag in
    the marker/lock machinery this behaviour has nothing to do with.
    """

    def _make(
        *,
        realized: float | None,
        cap: float,
        caps: set[Capability],
        catalog_rate: float = 1.0,
        destroy_raises: Exception | None = None,
    ) -> _Harness:
        provider = _FakeProvider(
            realized=realized,
            caps=frozenset(caps),
            catalog_rate=catalog_rate,
            destroy_raises=destroy_raises,
        )
        rendered = RenderedProvision(
            script="COMBINED",
            setup_steps=(SetupStep("export A=1"),),
            launch=Launch(("python", "-m", "x")),
            image="fake:latest",
            ports=["8000"],
            env_required=["HF_TOKEN"],
        )
        engine = MagicMock()
        engine.name = "fakeengine"
        engine.render_provision.return_value = rendered

        cfg = MagicMock()
        cfg.lifecycle.return_value = Lifecycle(boot_timeout_s=900.0)
        cfg.placement.return_value = Placement(max_usd_per_hr=cap)
        cfg.hardware_requirements.return_value = MagicMock()
        cfg.compute = MagicMock(image="should-be-overridden")
        cfg.diagnostic_mode = False
        cfg.model_dump.return_value = {"engine": {}, "models": []}

        harness = _Harness(provider=provider, cap=cap, run=lambda: None)

        def _fake_provision_once(**kwargs: Any) -> None:  # noqa: ANN401
            harness.provisioned_instances.append(kwargs["instance"])

        monkeypatch.setattr(
            orchestrator, "_provision_compute_once", _fake_provision_once
        )

        creds = MagicMock()
        creds.get = MagicMock(return_value="hf_REAL")
        key = MagicMock()
        key.derive.return_value = "deadbeef"

        def _run() -> None:
            _provision_instance_and_build_backend(
                resolved_engine=engine,
                # A structural fake, not a ComputeProvider subclass: the point
                # is to control capabilities() and realized_rate() together,
                # which is what the enforcement point branches on.
                resolved_provider=cast("ComputeProvider", provider),
                cfg=cfg,
                run_id="run-1",
                key=key,
                creds=creds,
                store=MagicMock(),
                state_dir=tmp_path,
                for_discovery=False,
                on_instance_created=lambda inst: harness.recorded.append(inst.id),
                on_rate_verified=lambda inst: harness.rate_corrections.append(
                    (inst.id, inst.cost_rate_usd_per_hr)
                ),
            )

        harness.run = _run
        return harness

    return _make


def test_over_cap_destroys_the_instance_and_raises(
    rate_harness: Callable[..., _Harness],
) -> None:
    """Bug caught: logging the violation and continuing bills the operator at
    a rate they capped, for the whole run."""
    h = rate_harness(realized=1.99, cap=1.09, caps={Capability.RATE_READBACK})
    with pytest.raises(RateCapExceeded) as ei:
        h.run()
    assert h.destroyed == [h.instance_id]
    assert ei.value.realized == pytest.approx(1.99)
    assert ei.value.cap == pytest.approx(1.09)
    assert h.provisioned is False


def test_unreadable_rate_on_a_readback_provider_destroys(
    rate_harness: Callable[..., _Harness],
) -> None:
    """Bug caught: treating unreadable as OK on the ONE provider that chooses
    its own SKU reinstates F4 exactly — the number nobody could read is the
    number that was wrong."""
    h = rate_harness(realized=None, cap=1.09, caps={Capability.RATE_READBACK})
    with pytest.raises(RateCapExceeded) as ei:
        h.run()
    assert h.destroyed == [h.instance_id]
    assert ei.value.realized is None
    assert "unreadable" in str(ei.value)


def test_unreadable_rate_on_a_deterministic_provider_proceeds(
    rate_harness: Callable[..., _Harness],
) -> None:
    """Bug caught: destroying here would tear down healthy RunPod pods over a
    missing nicety — the catalog already bounded the price before booking."""
    h = rate_harness(realized=None, cap=1.09, caps={Capability.RATE_DETERMINISTIC})
    h.run()
    assert h.destroyed == []
    assert h.provisioned is True


def test_under_cap_proceeds_without_extra_provider_calls(
    rate_harness: Callable[..., _Harness],
) -> None:
    """Bug caught: a check that re-reads the rate per retry turns one launch
    into N provider calls on the happy path."""
    h = rate_harness(realized=0.85, cap=1.09, caps={Capability.RATE_READBACK})
    h.run()
    assert h.destroyed == []
    assert h.realized_calls == 1
    assert h.provisioned is True


def test_a_rate_exactly_at_the_cap_is_allowed(
    rate_harness: Callable[..., _Harness],
) -> None:
    """Boundary. Bug caught: a strict `<` refuses the launch an operator who
    wrote the exact published price of the SKU they want explicitly asked
    for — and it fails AFTER paying for the boot."""
    h = rate_harness(realized=1.09, cap=1.09, caps={Capability.RATE_READBACK})
    h.run()
    assert h.destroyed == []
    assert h.provisioned is True


def test_over_cap_on_a_deterministic_provider_also_destroys(
    rate_harness: Callable[..., _Harness],
) -> None:
    """The deterministic leniency is for an UNREADABLE rate only.

    Bug caught: extending it to a rate that was read and IS over cap would let
    Modal launch above the ceiling forever — its catalog is mode="serverless",
    so filter_offers never applied the pre-book price filter there at all.
    """
    h = rate_harness(realized=2.50, cap=1.09, caps={Capability.RATE_DETERMINISTIC})
    with pytest.raises(RateCapExceeded):
        h.run()
    assert h.destroyed == [h.instance_id]
    assert h.provisioned is False


def test_teardown_failure_is_reported_with_the_violation(
    rate_harness: Callable[..., _Harness],
) -> None:
    """Bug caught: swallowing the teardown error reports 'instance destroyed'
    about an instance that is still billing, and the operator has no id to
    chase."""
    h = rate_harness(
        realized=1.99,
        cap=1.09,
        caps={Capability.RATE_READBACK},
        destroy_raises=RuntimeError("provider refused"),
    )
    with pytest.raises(RateCapExceeded) as ei:
        h.run()
    text = str(ei.value)
    assert "provider refused" in text
    assert h.instance_id in text


def test_the_ledger_row_is_written_before_the_check(
    rate_harness: Callable[..., _Harness],
) -> None:
    """Bug caught: checking before on_instance_created means a failed teardown
    leaves an instance with no ledger row — invisible to `kinoforge list` and
    to the reaper, which is the orphan class this project has paid for twice."""
    h = rate_harness(realized=1.99, cap=1.09, caps={Capability.RATE_READBACK})
    with pytest.raises(RateCapExceeded):
        h.run()
    assert h.recorded == [h.instance_id]


def test_the_instance_carries_the_realized_rate_after_the_check(
    rate_harness: Callable[..., _Harness],
) -> None:
    """Bug caught, and it is F4 itself: reporting the asked-for rate makes the
    ledger, est_spend, kinoforge list and every budget computation agree with
    each other and disagree with the invoice."""
    h = rate_harness(
        realized=0.85, cap=1.09, caps={Capability.RATE_READBACK}, catalog_rate=0.40
    )
    h.run()
    assert h.final_instance.cost_rate_usd_per_hr == pytest.approx(0.85)


def test_an_unreadable_deterministic_rate_keeps_the_catalog_number(
    rate_harness: Callable[..., _Harness],
) -> None:
    """Bug caught: overwriting with None zeroes RunPod's cost tracking, so
    budget guardrails silently stop firing — a worse failure than the one this
    task fixes, because it is invisible."""
    h = rate_harness(
        realized=None,
        cap=1.09,
        caps={Capability.RATE_DETERMINISTIC},
        catalog_rate=0.40,
    )
    h.run()
    assert h.final_instance.cost_rate_usd_per_hr == pytest.approx(0.40)


def test_the_recorded_rate_is_corrected_exactly_once(
    rate_harness: Callable[..., _Harness],
) -> None:
    """Bug caught: re-firing on_instance_created to carry the new number would
    APPEND a second ledger row (Ledger.record appends), leaving two rows for
    one instance and `kinoforge list` double-counting the spend."""
    h = rate_harness(
        realized=0.85, cap=1.09, caps={Capability.RATE_READBACK}, catalog_rate=0.40
    )
    h.run()
    assert h.recorded == [h.instance_id]
    assert len(h.rate_corrections) == 1
    corrected_id, corrected_rate = h.rate_corrections[0]
    assert corrected_id == h.instance_id
    assert corrected_rate == pytest.approx(0.85)


def test_no_rate_correction_fires_when_the_rate_was_unreadable(
    rate_harness: Callable[..., _Harness],
) -> None:
    """Bug caught: correcting the row with None (or with 0.0 coerced from it)
    zeroes the cost of a pod that is very much billing."""
    h = rate_harness(
        realized=None,
        cap=1.09,
        caps={Capability.RATE_DETERMINISTIC},
        catalog_rate=0.40,
    )
    h.run()
    assert h.rate_corrections == []


def test_ledger_set_cost_rate_rewrites_the_row_in_place(tmp_path: Path) -> None:
    """The mechanism behind the correction, tested against a real ledger.

    Bug caught: an implementation that appends (which is what Ledger.record
    does) leaves two rows for one instance, and every reader picks whichever
    it finds first.
    """
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.stores.local import LocalArtifactStore

    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test")
    inst = Instance(
        id="inst-9",
        provider="skypilot",
        status="ready",
        created_at=0.0,
        cost_rate_usd_per_hr=0.40,
    )
    ledger.record(inst)
    ledger.set_cost_rate("inst-9", 1.99)

    rows = [e for e in ledger.entries() if e["id"] == "inst-9"]
    assert len(rows) == 1
    assert rows[0]["cost_rate_usd_per_hr"] == pytest.approx(1.99)


def test_ledger_set_cost_rate_on_a_missing_row_is_a_no_op(tmp_path: Path) -> None:
    """Boundary. Bug caught: inventing a row for an id the ledger never had
    would resurrect an instance the reaper had already forgotten."""
    from kinoforge.core.lifecycle import Ledger
    from kinoforge.stores.local import LocalArtifactStore

    ledger = Ledger(store=LocalArtifactStore(root=tmp_path), run_id="test")
    ledger.set_cost_rate("never-existed", 1.99)
    assert ledger.entries() == []
