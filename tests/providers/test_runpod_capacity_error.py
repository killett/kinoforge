"""RunPod _create_pod classifies capacity-exhaustion messages as CapacityError."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import CapacityError
from kinoforge.core.interfaces import InstanceSpec, Launch, SetupStep
from kinoforge.providers.runpod import RunPodProvider

#: compute-seam S4: RunPod selects its own SKU inside create_instance, so every
#: transport a create test drives must answer the catalog query first.
_S4_GPU_TYPES: dict[str, object] = {
    "data": {
        "gpuTypes": [
            {
                "id": name,
                "displayName": name,
                "memoryInGb": vram,
                "secureCloud": True,
                "lowestPrice": {
                    "minimumBidPrice": price,
                    "uninterruptablePrice": price,
                },
            }
            # Wide enough that both shapes of shipped config find something: a
            # DEFAULT Placement (48 GB floor, $2.20 cap) and the cheap
            # interpolate configs (16 GB floor, $1.00 cap, named 24 GB SKUs).
            for name, vram, price in (
                ("NVIDIA RTX A4000", 16, 0.32),
                ("NVIDIA RTX A5000", 24, 0.44),
                ("NVIDIA GeForce RTX 4090", 24, 0.69),
                ("NVIDIA A100 80GB PCIe", 80, 1.64),
            )
        ]
    }
}


def _spec() -> InstanceSpec:
    return InstanceSpec(
        image="img",
        ports=("8000",),
        env={},
        run_id="r",
        setup_steps=(SetupStep("#!/bin/sh\ntrue\n"),),
        launch=Launch(("sleep", "infinity")),
    )


def _provider_returning(error_message: str) -> RunPodProvider:
    """A provider whose CREATE fails with *error_message*.

    The catalog read S4 added is answered normally: this module is about how a
    create-mutation error is CLASSIFIED, and a transport that failed the
    enumeration instead would never reach the classification.
    """

    def fake_post(_url: str, body: dict[str, Any]) -> dict[str, Any]:
        if "gpuTypes" in str(body.get("query", "")):
            return _S4_GPU_TYPES
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
