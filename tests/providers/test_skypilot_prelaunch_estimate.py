"""Behavior: an over-cap SkyPilot launch is refused while refusing is free.

S4 shipped the readback that makes the cap TRUE; this makes it cheap. On
SkyPilot ``sky.launch`` runs ``Task.setup`` before it returns, so a violation
caught afterwards discards several minutes of provisioning that was already
paid for (design §6, qualified 2026-09-01; §14).

The estimate is a LOCAL CATALOG read, not an optimizer quote, and that is
forced by the pin. At skypilot-0.12.3.post1 the ``/optimize`` route is
scheduled with ``ignore_return_value=True`` (``sky/server/server.py:1422``)
and the executor stores ``None`` in place of the optimized Dag
(``sky/server/requests/executor.py:568``), so ``stream_and_get`` on an
optimize request id can only ever return ``None`` — a ``sky.optimize``-based
estimate is a guaranteed no-op live. ``/list_accelerators``
(``sky/server/server.py:1333``) does NOT ignore its return value, so the
catalog can genuinely answer; ``InstanceTypeInfo.price``
(``sky/catalog/common.py``) is the whole-instance hourly price, which is why
the read is narrowed with ``quantity_filter=1`` to match the ``"<name>:1"``
the provider actually pins.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.errors import RateCapExceeded
from kinoforge.core.interfaces import Instance, InstanceSpec, Lifecycle, Placement
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.orchestrator import _provision_instance_and_build_backend
from kinoforge.providers.skypilot import PreLaunchRateCapExceeded, SkyPilotProvider
from kinoforge.stores.local import LocalArtifactStore

# Reuse the orchestrator tests' scaffolding rather than standing up a parallel
# set that could drift from the real call contract (same reason
# tests/core/test_provisional_launch_row.py imports these).
from tests.core.test_orchestrator_render_provision import (  # noqa: F401
    _make_cfg,
    fake_engine,
    fake_provider,
)

_CLUSTER = "kf-estimate-probe"


class _PricingSky:
    """A sky stand-in whose accelerator catalog carries prices.

    Models ``sky.list_accelerators``' modern return shape — a dict keyed by
    accelerator name whose values are lists of per-instance-type records
    (``sky/client/sdk.py:284``, ``sky/catalog/common.py:InstanceTypeInfo``).

    Args:
        catalog: ``{accelerator_name: [record, ...]}`` returned for a read.
        raises: When set, ``list_accelerators`` raises this instead.
        block: When set, ``list_accelerators`` waits on it before returning —
            used to drive the deadline.
    """

    def __init__(
        self,
        catalog: dict[str, list[dict[str, Any]]] | None = None,
        *,
        raises: BaseException | None = None,
        block: threading.Event | None = None,
    ) -> None:
        self._catalog = catalog if catalog is not None else {}
        self._raises = raises
        self._block = block
        self.launches: list[Any] = []
        self.catalog_calls: list[dict[str, Any]] = []

    class Task:
        """Stand-in for the ``sky.Task`` namespace."""

        @staticmethod
        def from_yaml_config(config: dict[str, Any]) -> Any:  # noqa: ANN401
            """Return an opaque task object carrying the config."""
            task = MagicMock()
            task.config = config
            return task

    def list_accelerators(self, **kwargs: Any) -> dict[str, list[dict[str, Any]]]:  # noqa: ANN401
        """Record the read's arguments and answer from the fake catalog."""
        self.catalog_calls.append(dict(kwargs))
        if self._block is not None:
            self._block.wait(timeout=30.0)
        if self._raises is not None:
            raise self._raises
        name_filter = kwargs.get("name_filter")
        if name_filter is None:
            return dict(self._catalog)
        return {
            name: list(recs)
            for name, recs in self._catalog.items()
            if name == name_filter
        }

    def launch(self, task: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Record the cluster name a launch was attempted for."""
        del task
        self.launches.append(kwargs.get("cluster_name"))
        return (None, None)

    def status(self, **kwargs: Any) -> list[dict[str, Any]]:  # noqa: ANN401
        """No clusters — ``_selection_tags`` then contributes nothing."""
        del kwargs
        return []


def _catalog(name: str, *prices: float, vram_gb: int = 80) -> dict[str, list[Any]]:
    """Build a one-accelerator catalog offering ``prices`` across clouds.

    Args:
        name: The accelerator name.
        prices: One record per price, as sky returns one per instance type.
        vram_gb: Device memory to report.

    Returns:
        A ``list_accelerators``-shaped dict.
    """
    return {
        name: [
            {
                "accelerator_name": name,
                "vram_gb": vram_gb,
                "cuda": "12.8",
                "price": price,
            }
            for price in prices
        ]
    }


def _spec_with_cap(cap: float, **overrides: Any) -> InstanceSpec:
    """Build a spec whose placement carries ``max_usd_per_hr = cap``.

    Args:
        cap: The USD/hr ceiling to put on ``placement.max_usd_per_hr``.
        **overrides: Extra ``Placement`` fields (e.g. ``spot=True``).

    Returns:
        A minimal server-less spec naming A100 (so selection short-circuits
        and no ssh tunnel is opened).
    """
    return InstanceSpec(
        image="pytorch/pytorch:2.3-cuda12.1-cudnn9-devel",
        run_id=_CLUSTER,
        lifecycle=Lifecycle(idle_timeout_s=7200.0),
        placement=Placement(
            accelerators=("A100",), max_usd_per_hr=cap, min_vram_gb=0, **overrides
        ),
    )


def test_an_over_cap_estimate_refuses_before_launch() -> None:
    """No cluster is created at all.

    Bug caught: enforcing only after launch, which on SkyPilot means paying
    for Task.setup and then throwing it away — the accepted-but-expensive
    trade S4 recorded as this follow-up.
    """
    sky = _PricingSky(_catalog("A100", 1.99))
    provider = SkyPilotProvider(sky)
    with pytest.raises(RateCapExceeded, match="1.9900"):
        provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == []


def test_the_estimate_is_read_for_the_accelerator_the_launch_pins() -> None:
    """The catalog read names the accelerator and the count being booked.

    Bug caught: dropping the accelerator on the way into the estimate — the
    read would then pull the whole catalog, price some unrelated card, and
    every other test here would still pass. Also catches pricing an 8-GPU
    record: ``InstanceTypeInfo.price`` is the whole-INSTANCE price, so a
    quantity other than the pinned ``:1`` prices a different launch.
    """
    sky = _PricingSky(_catalog("A100", 1.99))
    provider = SkyPilotProvider(sky)
    with pytest.raises(RateCapExceeded):
        provider.create_instance(_spec_with_cap(1.09))
    pricing_reads = [c for c in sky.catalog_calls if c.get("name_filter") is not None]
    assert pricing_reads == [{"name_filter": "A100", "quantity_filter": 1}]


def test_the_cheapest_catalog_record_is_the_bound() -> None:
    """A lower bound cannot refuse a launch that was in budget.

    Bug caught: taking the max (or the first record) across clouds, which
    would refuse an A100 launch on the strength of the most expensive cloud
    listing it while a cheaper one was pinned and affordable.
    """
    sky = _PricingSky(_catalog("A100", 3.95, 0.93, 2.10))
    provider = SkyPilotProvider(sky)
    provider.create_instance(_spec_with_cap(1.00))
    assert sky.launches == [_CLUSTER]


def test_an_estimate_under_the_cap_launches() -> None:
    """The ordinary path is untouched.

    Bug caught: a units error (per-second vs per-hour) refusing every launch.
    """
    sky = _PricingSky(_catalog("A100", 0.085))
    provider = SkyPilotProvider(sky)
    provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == [_CLUSTER]


def test_an_estimate_exactly_at_the_cap_launches() -> None:
    """The cap is a ceiling, not a strict inequality.

    Bug caught: ``>=`` refusing a placement that is exactly what the operator
    budgeted for.
    """
    sky = _PricingSky(_catalog("A100", 1.09))
    provider = SkyPilotProvider(sky)
    provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == [_CLUSTER]


def test_the_refusal_names_the_cluster_the_cap_and_its_pre_launch_nature() -> None:
    """An operator can act on the message without reading the source.

    Bug caught: a refusal that reads like a billed rate on a live cluster —
    ``realized $1.99/hr ... instance destroyed`` — sending the operator to
    hunt for a resource that was never created.
    """
    sky = _PricingSky(_catalog("A100", 1.99))
    provider = SkyPilotProvider(sky)
    with pytest.raises(RateCapExceeded) as excinfo:
        provider.create_instance(_spec_with_cap(1.09))
    message = str(excinfo.value)
    assert "1.9900" in message
    assert "1.0900" in message
    assert _CLUSTER in message
    assert "PRE-LAUNCH" in message.upper()
    assert "nothing was launched" in message
    assert "A100" in message
    # The S4 rendering claims the instance was destroyed. Nothing was created
    # here, so neither that tail nor the word "realized" may appear.
    assert "destroyed" not in message
    assert "realized" not in message
    # Still a RateCapExceeded to every existing handler.
    assert isinstance(excinfo.value, RateCapExceeded)
    assert excinfo.value.realized == pytest.approx(1.99)
    assert excinfo.value.cap == pytest.approx(1.09)


def test_repr_and_args_agree_with_str() -> None:
    """A durable artifact must not claim a phantom cluster was destroyed.

    Bug caught: overriding only ``__str__``. ``RateCapExceeded.__init__``
    already baked the S4 message into ``Exception.args``, and the S4 live
    smoke writes ``repr(exc)[:600]`` into a checked-in evidence file — so
    ``repr`` would durably record "instance destroyed" for a cluster that was
    never booked.
    """
    exc = PreLaunchRateCapExceeded(
        realized=1.99,
        cap=1.09,
        instance_id=_CLUSTER,
        placement_summary="provider=skypilot",
    )
    assert "destroyed" not in repr(exc)
    assert "destroyed" not in str(exc.args[0])
    assert exc.args == (str(exc),)
    assert "PRE-LAUNCH" in repr(exc)


def test_a_cap_of_zero_takes_no_estimate_at_all() -> None:
    """With no ceiling the estimate is not merely unused — it is not taken.

    Bug caught: paying a catalog round trip (and its hang risk) to produce a
    number nothing will read, then WARNING that it was unreadable.
    """
    sky = _PricingSky(_catalog("A100", 99.0))
    provider = SkyPilotProvider(sky)
    provider.create_instance(_spec_with_cap(0.0))
    assert sky.launches == [_CLUSTER]
    assert [c for c in sky.catalog_calls if c.get("name_filter") is not None] == []


def test_no_warning_is_logged_when_there_is_no_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Skipping the estimate is not an "unreadable estimate".

    Bug caught: a misleading WARNING on every uncapped launch, training
    operators to ignore the line that matters.
    """
    sky = _PricingSky(_catalog("A100", 99.0))
    provider = SkyPilotProvider(sky)
    with caplog.at_level("WARNING"):
        provider.create_instance(_spec_with_cap(0.0))
    assert "estimate" not in caplog.text.lower()


def test_an_unreadable_estimate_warns_and_launches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A best-effort estimate never blocks a launch on its own failure.

    Bug caught: a wedged or unreachable sky API server turning every launch
    into a hard failure.
    """
    sky = _PricingSky(_catalog("A100", 1.99), raises=RuntimeError("catalog down"))
    provider = SkyPilotProvider(sky)
    with caplog.at_level("WARNING"):
        provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == [_CLUSTER]
    assert "estimate" in caplog.text.lower()
    assert _CLUSTER in caplog.text


def test_an_accelerator_the_catalog_does_not_price_warns_and_launches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A catalog that answers but prices nothing is unreadable, not free.

    Bug caught: an empty price list collapsing to 0.0, which reads as "free"
    and silently vouches for any cap.
    """
    sky = _PricingSky(_catalog("A100", 0.0))
    provider = SkyPilotProvider(sky)
    with caplog.at_level("WARNING"):
        provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == [_CLUSTER]
    assert "estimate" in caplog.text.lower()


def test_a_non_finite_price_is_unreadable_not_under_the_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """NaN compares False in both directions, so it must not be waved through.

    Bug caught: a NaN price silently passing the ``estimate > cap`` test and
    being reported as an in-budget estimate.
    """
    sky = _PricingSky(_catalog("A100", float("nan")))
    provider = SkyPilotProvider(sky)
    with caplog.at_level("WARNING"):
        provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == [_CLUSTER]
    assert "estimate" in caplog.text.lower()
    assert (
        provider._estimate_hourly_rate("A100", _spec_with_cap(1.09).placement) is None
    )  # noqa: SLF001


def test_a_cpu_only_launch_has_no_catalog_price() -> None:
    """The accelerator catalog cannot price a CPU SKU.

    Bug caught: returning 0.0 for a CPU task, which reads as "free" and
    vouches for any cap; or crashing on ``name_filter=None``.
    """
    sky = _PricingSky(_catalog("A100", 1.99))
    provider = SkyPilotProvider(sky)
    placement = Placement(min_vram_gb=0, max_usd_per_hr=0.01)
    assert provider._estimate_hourly_rate(None, placement) is None  # noqa: SLF001
    assert sky.catalog_calls == []


def test_a_spot_launch_is_not_priced_off_the_on_demand_column() -> None:
    """Spot has its own price column; on-demand is the wrong direction.

    Bug caught: refusing a spot launch the operator can afford, because the
    on-demand price used as the bound exceeds a cap set for spot rates.
    """
    sky = _PricingSky(_catalog("A100", 1.99))
    provider = SkyPilotProvider(sky)
    provider.create_instance(_spec_with_cap(1.09, spot=True))
    assert sky.launches == [_CLUSTER]


def test_the_estimate_is_bounded_by_a_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged catalog read cannot hang a launch.

    The estimate runs before anything is booked AND before the instance-side
    watchdog is armed, so there is no guardrail to catch a stall here.

    Bug caught: a synchronous catalog read against an unresponsive sky API
    server blocking create_instance indefinitely.
    """
    monkeypatch.setattr(
        "kinoforge.providers.skypilot._ESTIMATE_TIMEOUT_S", 0.05, raising=True
    )
    release = threading.Event()
    sky = _PricingSky(_catalog("A100", 1.99), block=release)
    provider = SkyPilotProvider(sky)
    try:
        # Would refuse at $1.99 vs $1.09 if the read ever answered; the
        # deadline makes it unreadable, so the launch proceeds.
        provider.create_instance(_spec_with_cap(1.09))
        assert sky.launches == [_CLUSTER]
    finally:
        release.set()


def test_estimate_hourly_rate_never_raises_on_a_sky_without_a_catalog() -> None:
    """A sky stand-in with no ``list_accelerators`` at all yields None.

    Bug caught: a legacy or stubbed sky client (the golden capture harness is
    one) turning every launch into an AttributeError.
    """
    provider = SkyPilotProvider(MagicMock(spec=[]))
    placement = Placement(accelerators=("A100",), max_usd_per_hr=1.09)
    assert provider._estimate_hourly_rate("A100", placement) is None  # noqa: SLF001


def test_the_provisional_ledger_row_is_forgotten_when_the_estimate_refuses(
    tmp_path: Path,
    fake_engine: MagicMock,  # noqa: F811 — imported fixture, not a redefinition
    fake_provider: MagicMock,  # noqa: F811 — imported fixture
) -> None:
    """A pre-launch refusal leaves no ghost row behind.

    The provisional row is written by the orchestrator BEFORE create_instance
    (Task 4/5) and removed by its ``except BaseException`` branch. This proves
    ``RateCapExceeded`` — raised out of the real provider, before any launch —
    goes through that branch rather than needing cleanup of its own.

    Bug caught: a permanent ``launching`` row whose est_spend inflates forever
    for a cluster that was never created (the "$210 phantom pod" failure mode).
    """
    sky = _PricingSky(_catalog("A100", 99.0))
    real_provider = SkyPilotProvider(sky)
    ledger = Ledger(store=LocalArtifactStore(tmp_path / "ledger-root"))
    seen_mid_create: list[list[str]] = []

    def _create(spec: InstanceSpec) -> Instance:
        del spec
        seen_mid_create.append([str(e["id"]) for e in ledger.entries()])
        # Delegate to the REAL provider so the exception under test is the one
        # the pre-launch estimate actually raises, not a hand-rolled stand-in.
        return real_provider.create_instance(_spec_with_cap(1.09))

    fake_provider.create_instance.side_effect = _create
    creds = MagicMock()
    creds.get = MagicMock(return_value="hf_REAL")
    key = MagicMock()
    key.derive.return_value = "deadbeef"

    with pytest.raises(RateCapExceeded):
        _provision_instance_and_build_backend(
            resolved_engine=fake_engine,
            resolved_provider=fake_provider,
            cfg=_make_cfg(),
            run_id="kf-run-estimate",
            key=key,
            creds=creds,
            store=MagicMock(),
            state_dir=tmp_path,
            for_discovery=False,
            on_instance_created=None,
            provisional_ledger=ledger,
            capacity_wait_s=0.0,
        )

    # The row was there while the create ran...
    assert seen_mid_create == [["kf-run-estimate"]]
    # ...and is gone now, with nothing ever launched.
    assert ledger.entries() == []
    assert sky.launches == []
