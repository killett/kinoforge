"""RunPod _create_pod classifies capacity-exhaustion messages as CapacityError."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import CapacityError
from kinoforge.core.interfaces import InstanceSpec, Offer
from kinoforge.providers.runpod import RunPodProvider


def _spec() -> InstanceSpec:
    return InstanceSpec(
        image="img",
        offer=Offer(
            id="NVIDIA A100 80GB PCIe",
            gpu_type="NVIDIA A100 80GB PCIe",
            vram_gb=80,
            cuda="12.8",
            cost_rate_usd_per_hr=1.19,
            mode="pod",
        ),
        ports=("8000",),
        env={},
        run_id="r",
        provision_script="#!/bin/sh\ntrue\n",
    )


def _provider_returning(error_message: str) -> RunPodProvider:
    def fake_post(_url: str, _body: dict[str, Any]) -> dict[str, Any]:
        return {"errors": [{"message": error_message}]}

    return RunPodProvider(http_post=fake_post)


@pytest.mark.parametrize(
    "msg",
    [
        "There are no longer any instances available with the requested specifications. Please refresh and try again.",
        "There are no longer any instances available with enough disk space.",
        "There are no resources to deploy for this request.",
    ],
)
def test_capacity_messages_raise_capacity_error(msg: str) -> None:
    # Bug caught: the "no longer any instances available" variants fell through
    # to a raw ValueError, so _create_with_offer_retry (which catches only
    # CapacityError) never retried and the run died on the first miss.
    provider = _provider_returning(msg)
    with pytest.raises(CapacityError):
        provider.create_instance(_spec())


def test_non_capacity_error_stays_value_error() -> None:
    # Bug caught: over-broad match swallows real create failures (bad schema,
    # auth) as retryable capacity misses, hiding a hard error behind a 5min wait.
    provider = _provider_returning("Field 'bogus' is not defined in the input type.")
    with pytest.raises(ValueError) as exc_info:
        provider.create_instance(_spec())
    assert not isinstance(exc_info.value, CapacityError)
