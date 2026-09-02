"""Behavior: what SkyPilot actually booked is on the Instance.

S4 shipped a cap violation whose message read ``provider=skypilot`` and
nothing else, because a SkyPilotProvider Instance carried no selection tags
and _placement_summary reports only what an Instance really holds. An operator
reading that cannot tell "my cap is too low" from "this went somewhere I did
not intend", which is the whole stated purpose of the summary.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import RateCapExceeded
from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Placement
from kinoforge.core.orchestrator import _enforce_rate_cap
from kinoforge.providers.skypilot import SkyPilotProvider


class _Resources:
    """Stand-in for ``sky.Resources`` — exposes exactly what sky's real
    ``Resources`` object exposes for a booked cluster: ``instance_type``,
    ``cloud``, ``region`` and ``accelerators``, plus ``get_cost`` (the same
    method ``realized_rate`` already reads).
    """

    def __init__(
        self,
        *,
        instance_type: str = "c6i.large",
        cloud: str = "AWS",
        region: str = "us-west-2",
        accelerators: Any = "",
        cost_per_hr: float = 0.085,
    ) -> None:
        self.instance_type = instance_type
        self.cloud = cloud
        self.region = region
        self.accelerators = accelerators
        self._cost_per_hr = cost_per_hr

    def get_cost(self, seconds: float) -> float:
        return self._cost_per_hr * (seconds / 3600.0)


class _Handle:
    """Stand-in for sky's cluster handle — the only field readers use."""

    def __init__(self, launched_resources: Any) -> None:
        self.launched_resources = launched_resources


class _FakeSky:
    """Minimal injectable sky client: only ``status`` and ``launch`` are
    exercised, since ``_make_spec`` below sets no ``launch`` (no ssh tunnel
    is opened, so no other sky surface is touched).
    """

    def __init__(self, status_result: list[dict[str, Any]]) -> None:
        self._status_result = status_result
        self.down_calls: list[str] = []

        class _TaskNamespace:
            @staticmethod
            def from_yaml_config(config: dict[str, Any]) -> Any:
                class _Task:
                    pass

                task = _Task()
                task.config = config  # type: ignore[attr-defined]
                return task

        self.Task = _TaskNamespace()

    def status(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return self._status_result

    def launch(self, task: Any, **kwargs: Any) -> Any:
        return (None, None)

    def down(self, cluster_id: str) -> None:
        self.down_calls.append(cluster_id)


def _make_spec(**overrides: Any) -> InstanceSpec:
    """Minimal InstanceSpec naming an accelerator (S4 made selection
    provider-side, so a spec naming none would route to the catalog)."""
    base: dict[str, Any] = {
        "image": "pytorch/pytorch:2.3-cuda12.1-cudnn9-devel",
        "run_id": "kf-selection-probe",
        "lifecycle": Lifecycle(idle_timeout_s=7200.0),
        "placement": Placement(accelerators=("A100",)),
    }
    base.update(overrides)
    return InstanceSpec(**base)


def test_launch_records_the_chosen_sku_cloud_and_region() -> None:
    """The optimizer's choice lands on the Instance.

    Bug caught: a cap violation, a ledger row and a status line that all
    describe a cluster without saying what it is.
    """
    resources = _Resources(
        instance_type="c6i.large",
        cloud="AWS",
        region="us-west-2",
        accelerators="",
    )
    fake = _FakeSky(
        status_result=[
            {
                "name": "kf-selection-probe",
                "handle": _Handle(resources),
                "status": "UP",
            }
        ]
    )
    provider = SkyPilotProvider(sky_client=fake, sleep=lambda _s: None)

    instance = provider.create_instance(_make_spec())

    assert instance.tags["sku"] == "c6i.large"
    assert instance.tags["cloud"] == "AWS"
    assert instance.tags["region"] == "us-west-2"
    assert instance.tags["accelerators"] == ""
    # The pre-existing ports merge (Task 0/1) must survive alongside the new
    # selection tags — this is not a replacement, it is an addition.
    assert instance.tags["ports"] == ""


def test_an_unreadable_handle_writes_no_empty_tags() -> None:
    """Silence beats ``sku=``.

    Bug caught: writing empty strings makes _placement_summary emit
    "sku=, cloud=, provider=skypilot", which reads as a broken tool rather
    than as missing information.
    """
    fake = _FakeSky(
        status_result=[{"name": "kf-selection-probe", "handle": None, "status": "UP"}]
    )
    provider = SkyPilotProvider(sky_client=fake, sleep=lambda _s: None)

    instance = provider.create_instance(_make_spec())

    for key in ("sku", "cloud", "region", "accelerators"):
        assert key not in instance.tags, (
            f"an unreadable handle must OMIT {key!r}, not write it as an empty string"
        )
    # The rest of the merge (spec tags + ports) must still be intact.
    assert instance.tags["ports"] == ""


def test_the_rate_cap_message_names_the_sku() -> None:
    """The S4 follow-up, end to end.

    Bug caught: the enrichment landing on the Instance but never reaching the
    error, e.g. because the tags are applied after the orchestrator's copy.
    """
    resources = _Resources(
        instance_type="c6i.large",
        cloud="AWS",
        region="us-west-2",
        accelerators="",
        # Above whatever cap the test sets below, so realized > cap.
        cost_per_hr=5.00,
    )
    fake = _FakeSky(
        status_result=[
            {
                "name": "kf-selection-probe",
                "handle": _Handle(resources),
                "status": "UP",
            }
        ]
    )
    provider = SkyPilotProvider(sky_client=fake, sleep=lambda _s: None)
    instance = provider.create_instance(_make_spec())

    with pytest.raises(RateCapExceeded) as exc_info:
        _enforce_rate_cap(provider=provider, instance=instance, cap=1.09)

    message = str(exc_info.value)
    assert "sku=c6i.large" in message
    assert "cloud=AWS" in message
    # The teardown this violation triggers must also have actually run.
    assert fake.down_calls == ["kf-selection-probe"]
