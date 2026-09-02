"""Behavior: an over-cap SkyPilot launch is refused while refusing is free.

S4 shipped the readback that makes the cap TRUE; this makes it cheap. On
SkyPilot ``sky.launch`` runs ``Task.setup`` before it returns, so a violation
caught afterwards discards several minutes of provisioning that was already
paid for (design §6, qualified 2026-09-01; §14). ``sky.optimize`` answers the
same question before anything is booked.

The fakes here model the shapes of the pinned ``skypilot-0.12.3.post1``:

* ``sky.optimize(dag, ...)`` (``sky/client/sdk.py:411``) takes a ``sky.Dag``
  — not a ``Task`` — and returns a ``RequestId['sky.Dag']``, a ``str``
  subclass resolved through ``sky.stream_and_get``. Both the resolved-Dag
  and the RequestId shape are exercised below.
* ``sky.Dag`` (``sky/dag.py:26``) is built empty and populated via
  ``dag.add(task)``; its ``tasks`` list is what the optimized Dag exposes.
* The price comes off ``best_resources.get_cost(3600.0)``
  (``sky/resources.py:1704``), which asserts on ``cloud`` and
  ``_instance_type`` and so can raise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kinoforge.core.errors import RateCapExceeded
from kinoforge.core.interfaces import Instance, InstanceSpec, Lifecycle, Placement
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.orchestrator import _provision_instance_and_build_backend
from kinoforge.providers.skypilot import SkyPilotProvider
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


class _FakeTask:
    """Stand-in for ``sky.Task`` — the provider only ever passes it around."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config


class _FakeInputDag:
    """Stand-in for ``sky.Dag``: built empty, populated with ``add``."""

    def __init__(self) -> None:
        self.tasks: list[Any] = []

    def add(self, task: Any) -> None:  # noqa: ANN401
        """Append ``task``, mirroring ``sky.Dag.add`` (``sky/dag.py:61``)."""
        self.tasks.append(task)


class _OptimizedResources:
    """Stand-in for ``sky.Resources`` as the optimizer returns it."""

    def __init__(self, hourly: float, *, raises: bool = False) -> None:
        self._hourly = hourly
        self._raises = raises

    def get_cost(self, seconds: float) -> float:
        """Return cost for ``seconds``, priced per hour like the real method.

        The real ``get_cost`` asserts on ``cloud`` / ``_instance_type``, so
        ``raises=True`` models that assertion firing.
        """
        if self._raises:
            raise AssertionError("Cloud must be specified")
        return self._hourly * (seconds / 3600.0)


class _OptimizedTask:
    """A task in the optimized Dag: carries the chosen ``best_resources``."""

    def __init__(self, best_resources: Any) -> None:  # noqa: ANN401
        self.best_resources = best_resources


class _FakeDag:
    """The optimized Dag ``sky.optimize`` yields, exposing ``tasks``."""

    def __init__(self, hourly: float, *, cost_raises: bool = False) -> None:
        self.tasks = [_OptimizedTask(_OptimizedResources(hourly, raises=cost_raises))]


class _OptimizingSky:
    """A sky stand-in whose optimizer returns a priced plan.

    Args:
        hourly: USD/hr the optimizer reports; ``None`` makes ``optimize``
            raise, modelling a sky-server outage.
        cost_raises: When True, ``get_cost`` raises instead of pricing.
        best_resources_missing: When True, the optimized task carries
            ``best_resources = None`` (the optimizer found no plan).
        as_request_id: When True, ``optimize`` returns a ``RequestId``-shaped
            ``str`` that must be resolved through ``stream_and_get`` — the
            shape the pinned 0.12.3 client actually returns.
    """

    def __init__(
        self,
        hourly: float | None,
        *,
        cost_raises: bool = False,
        best_resources_missing: bool = False,
        as_request_id: bool = False,
    ) -> None:
        self._hourly = hourly
        self._cost_raises = cost_raises
        self._best_resources_missing = best_resources_missing
        self._as_request_id = as_request_id
        self.launches: list[Any] = []
        self.optimize_calls: list[Any] = []
        self.resolved: dict[str, Any] = {}

    class Task:
        """Stand-in for the ``sky.Task`` namespace."""

        @staticmethod
        def from_yaml_config(config: dict[str, Any]) -> Any:  # noqa: ANN401
            """Build the opaque task object the provider hands to launch."""
            return _FakeTask(config)

    Dag = _FakeInputDag

    def _plan(self) -> Any:  # noqa: ANN401
        if self._best_resources_missing:
            dag = _FakeDag(0.0)
            dag.tasks[0].best_resources = None
            return dag
        assert self._hourly is not None  # noqa: S101 — guarded by optimize
        return _FakeDag(self._hourly, cost_raises=self._cost_raises)

    def optimize(self, dag: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Record the Dag and answer with a plan, a RequestId, or a fault."""
        del kwargs
        self.optimize_calls.append(dag)
        if self._hourly is None and not self._best_resources_missing:
            raise RuntimeError("optimize unavailable")
        if self._as_request_id:
            self.resolved["req-optimize-1"] = self._plan()
            return "req-optimize-1"
        return self._plan()

    def stream_and_get(self, request_id: str) -> Any:  # noqa: ANN401
        """Resolve a RequestId the way ``sky.stream_and_get`` does."""
        return self.resolved[request_id]

    def launch(self, task: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        """Record the cluster name a launch was attempted for."""
        del task
        self.launches.append(kwargs.get("cluster_name"))
        return (None, None)

    def status(self, **kwargs: Any) -> list[dict[str, Any]]:  # noqa: ANN401
        """No clusters — ``_selection_tags`` then contributes nothing."""
        del kwargs
        return []


def _spec_with_cap(cap: float) -> InstanceSpec:
    """Build a spec whose placement carries ``max_usd_per_hr = cap``.

    Args:
        cap: The USD/hr ceiling to put on ``placement.max_usd_per_hr``.

    Returns:
        A minimal server-less spec (no ``launch``, so no ssh tunnel is
        opened and no further sky surface is touched).
    """
    return InstanceSpec(
        image="pytorch/pytorch:2.3-cuda12.1-cudnn9-devel",
        run_id=_CLUSTER,
        lifecycle=Lifecycle(idle_timeout_s=7200.0),
        placement=Placement(accelerators=("A100",), max_usd_per_hr=cap),
    )


def _provider_with_cap(sky: Any, cap: float) -> SkyPilotProvider:  # noqa: ANN401
    """Build a provider over ``sky``; ``cap`` documents the spec's ceiling.

    Args:
        sky: The injected sky stand-in.
        cap: The ceiling the matching spec carries — recorded for the
            reader's benefit, since the cap reaches the provider through
            ``InstanceSpec.placement``, not through the constructor.

    Returns:
        A provider wired to ``sky``.
    """
    del cap
    return SkyPilotProvider(sky)


def test_an_over_cap_estimate_refuses_before_launch() -> None:
    """No cluster is created at all.

    Bug caught: enforcing only after launch, which on SkyPilot means paying
    for Task.setup and then throwing it away — the accepted-but-expensive
    trade S4 recorded as this follow-up.
    """
    sky = _OptimizingSky(hourly=1.99)
    provider = _provider_with_cap(sky, cap=1.09)
    with pytest.raises(RateCapExceeded, match="1.9900"):
        provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == []
    assert len(sky.optimize_calls) == 1


def test_the_refusal_names_the_cluster_the_cap_and_its_pre_launch_nature() -> None:
    """An operator can act on the message without reading the source.

    Bug caught: a refusal that reads like a billed rate on a live cluster —
    ``realized $1.99/hr ... instance destroyed`` — sending the operator to
    hunt for a resource that was never created.
    """
    sky = _OptimizingSky(hourly=1.99)
    provider = _provider_with_cap(sky, cap=1.09)
    with pytest.raises(RateCapExceeded) as excinfo:
        provider.create_instance(_spec_with_cap(1.09))
    message = str(excinfo.value)
    assert "1.9900" in message
    assert "1.0900" in message
    assert _CLUSTER in message
    assert "PRE-LAUNCH" in message.upper()
    assert "nothing was launched" in message
    # The S4 rendering claims the instance was destroyed. Nothing was created
    # here, so neither that tail nor the word "realized" may appear.
    assert "destroyed" not in message
    assert "realized" not in message
    # Still a RateCapExceeded to every existing handler.
    assert isinstance(excinfo.value, RateCapExceeded)
    assert excinfo.value.realized == pytest.approx(1.99)
    assert excinfo.value.cap == pytest.approx(1.09)


def test_an_estimate_under_the_cap_launches() -> None:
    """The ordinary path is untouched.

    Bug caught: a units error (per-second vs per-hour) refusing every launch.
    """
    sky = _OptimizingSky(hourly=0.085)
    provider = _provider_with_cap(sky, cap=1.09)
    provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == [_CLUSTER]


def test_an_estimate_exactly_at_the_cap_launches() -> None:
    """The cap is a ceiling, not a strict inequality.

    Bug caught: ``>=`` refusing a placement that is exactly what the operator
    budgeted for.
    """
    sky = _OptimizingSky(hourly=1.09)
    provider = _provider_with_cap(sky, cap=1.09)
    provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == [_CLUSTER]


def test_a_request_id_estimate_resolves_and_still_refuses() -> None:
    """The pinned client returns a RequestId, not a Dag.

    Bug caught: reading ``.tasks`` off the RequestId string — which has no
    such attribute, so every real estimate would silently degrade to
    unreadable and the pre-launch refusal would never once fire in
    production.
    """
    sky = _OptimizingSky(hourly=1.99, as_request_id=True)
    provider = _provider_with_cap(sky, cap=1.09)
    with pytest.raises(RateCapExceeded, match="1.9900"):
        provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == []


def test_a_cap_of_zero_means_no_cap() -> None:
    """An unset ceiling must not refuse everything.

    Bug caught: treating the "no cap configured" sentinel as a $0.00 ceiling,
    which would refuse every launch on the pre-launch path.
    """
    sky = _OptimizingSky(hourly=99.0)
    provider = _provider_with_cap(sky, cap=0.0)
    provider.create_instance(_spec_with_cap(0.0))
    assert sky.launches == [_CLUSTER]


def test_an_unreadable_estimate_warns_and_launches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A best-effort estimate never blocks a launch on its own failure.

    Bug caught: a sky server outage or an API shape change (0.12.3 returns a
    RequestId, not a Dag) turning every launch into a hard failure.
    """
    sky = _OptimizingSky(hourly=None)
    provider = _provider_with_cap(sky, cap=1.09)
    with caplog.at_level("WARNING"):
        provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == [_CLUSTER]
    assert "estimate" in caplog.text.lower()
    assert _CLUSTER in caplog.text


def test_a_missing_best_resources_warns_and_launches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An optimizer that found no plan is not a reason to refuse.

    Bug caught: ``None.get_cost(...)`` raising out of create_instance, so an
    optimizer miss becomes a hard launch failure instead of a WARN.
    """
    sky = _OptimizingSky(hourly=None, best_resources_missing=True)
    provider = _provider_with_cap(sky, cap=1.09)
    with caplog.at_level("WARNING"):
        provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == [_CLUSTER]
    assert "estimate" in caplog.text.lower()


def test_a_raising_get_cost_warns_and_launches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``get_cost`` asserts on cloud/instance_type and so can raise.

    Bug caught: the AssertionError from ``sky/resources.py:1704`` escaping
    create_instance and failing a launch that the post-launch readback would
    have policed correctly.
    """
    sky = _OptimizingSky(hourly=1.99, cost_raises=True)
    provider = _provider_with_cap(sky, cap=1.09)
    with caplog.at_level("WARNING"):
        provider.create_instance(_spec_with_cap(1.09))
    assert sky.launches == [_CLUSTER]
    assert "estimate" in caplog.text.lower()


def test_estimate_hourly_rate_never_raises_on_a_sky_without_an_optimizer() -> None:
    """A sky stand-in that has no ``Dag``/``optimize`` at all yields None.

    Bug caught: a legacy or stubbed sky client (the golden capture harness is
    one) turning every launch into an AttributeError.
    """
    provider = SkyPilotProvider(MagicMock(spec=[]))
    assert provider._estimate_hourly_rate(object()) is None  # noqa: SLF001


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
    sky = _OptimizingSky(hourly=99.0)
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
