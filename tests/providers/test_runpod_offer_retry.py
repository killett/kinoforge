"""Behavior: RunPod rides a capacity drought by trying the next offer itself.

compute-seam S4 moved this loop out of the orchestrator. It lived there because
RunPod is the provider it was written for; every other provider either schedules
(Modal) or hands the choice to an optimizer (SkyPilot), so the orchestrator was
iterating a catalog on behalf of providers that do not have one.

These tests are the orchestrator's own offer-retry tests, MOVED and re-pointed
at ``RunPodProvider.create_instance``. The assertions are the originals — design
§14 names this loop as the behaviour most at risk in S4, so they are carried
over rather than rewritten from memory. The originals lived in
``tests/core/test_orchestrator.py`` as:

* ``test_deploy_retries_next_offer_on_capacity_error``
* ``test_deploy_iterates_offers_in_input_order``
* ``test_deploy_raises_capacity_error_when_all_offers_exhausted``
* ``test_deploy_does_not_retry_on_non_capacity_error``
* ``test_provision_instance_helper_retries_next_offer_on_capacity_error``

The remaining CapacityError coverage stays where it is:
``tests/core/test_capacity_wait_retry.py`` (the WAIT window, which is a
different mechanism and still orchestrator-owned) and
``tests/providers/test_runpod_capacity_error.py`` (what makes a RunPod response
a CapacityError in the first place).
"""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import CapacityError
from kinoforge.core.interfaces import InstanceSpec, Lifecycle, Offer, Placement
from kinoforge.providers.runpod import RunPodProvider


def _offer(i: int) -> Offer:
    """Return the i-th catalog offer, ranked as filter_offers would leave it.

    The id matches the RunPod catalog's own gpuTypeId, because a caller's
    pre-selected offer always came FROM that catalog. An id that could never
    collide would make the provider's already-attempted check untestable.
    """
    return Offer(
        id=f"GPU_{i}",
        gpu_type=f"GPU_{i}",
        vram_gb=80,
        cuda="12.8",
        cost_rate_usd_per_hr=0.10 * (i + 1),
        mode="pod",
    )


_GPU_TYPES_RESPONSE: dict[str, Any] = {
    "data": {
        "gpuTypes": [
            {
                "id": f"GPU_{i}",
                "displayName": f"GPU_{i}",
                "memoryInGb": 80,
                "secureCloud": True,
                "lowestPrice": {
                    "minimumBidPrice": 0.10 * (i + 1),
                    "uninterruptablePrice": 0.10 * (i + 1),
                },
            }
            for i in range(3)
        ]
    }
}


class _ScriptedTransport:
    """RunPod GraphQL transport scripted per create attempt.

    ``outcomes`` is consumed one entry per create mutation:
        ``"capacity"`` -> a response RunPod reads as CapacityError
        ``"value"``    -> a non-capacity failure raised from the transport
        ``"ok"``       -> a successful pod create
    """

    def __init__(self, outcomes: list[str]) -> None:
        self._outcomes = outcomes
        self._index = 0
        self.created_gpu_ids: list[str] = []
        self.gpu_type_queries = 0

    def __call__(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        del url
        query = str(body.get("query", ""))
        if "gpuTypes" in query:
            self.gpu_type_queries += 1
            return _GPU_TYPES_RESPONSE
        gpu_id = str(body["variables"]["input"]["gpuTypeId"])
        self.created_gpu_ids.append(gpu_id)
        outcome = self._outcomes[self._index]
        self._index += 1
        if outcome == "capacity":
            return {
                "errors": [
                    {
                        "message": f"There are no longer any instances available with {gpu_id}"
                    }
                ]
            }
        if outcome == "value":
            raise ValueError("non-capacity error from provider")
        return {"data": {"podFindAndDeployOnDemand": {"id": f"pod-{gpu_id}"}}}


def _spec(offer: Offer | None) -> InstanceSpec:
    """Return a pod spec carrying *offer* and a three-accelerator placement."""
    return InstanceSpec(
        image="runpod/pytorch:latest",
        placement=Placement(
            accelerators=("GPU_0", "GPU_1", "GPU_2"), min_vram_gb=80, max_usd_per_hr=2.0
        ),
        lifecycle=Lifecycle(),
        tags={"mode": "pod"},
    )


def _provider(transport: _ScriptedTransport) -> RunPodProvider:
    return RunPodProvider(creds=None, http_post=transport, http_get=lambda _u: {})


def test_runpod_tries_the_next_offer_on_capacity_error() -> None:
    """Bug caught: giving up on the first CapacityError fails a run during a
    capacity drought that the previous behaviour rode out — the exact regression
    design §14 flags as S4's most likely invisible break."""
    transport = _ScriptedTransport(["capacity", "ok"])
    inst = _provider(transport).create_instance(_spec(_offer(0)))

    assert inst.id == "pod-GPU_1"
    assert transport.created_gpu_ids == ["GPU_0", "GPU_1"]


def test_runpod_honours_accelerator_order_when_retrying() -> None:
    """Bug caught: retrying in catalog order discards the operator's stated
    preference precisely when capacity is tight and it matters most. A future
    change that uses set() / reversed() iteration also breaks the cost-aware
    sort filter_offers applied, so the cheapest available offer stops being
    tried first."""
    transport = _ScriptedTransport(["capacity", "capacity", "ok"])
    _provider(transport).create_instance(_spec(_offer(0)))

    assert transport.created_gpu_ids == ["GPU_0", "GPU_1", "GPU_2"]


def test_runpod_raises_capacity_error_when_all_offers_are_exhausted() -> None:
    """Bug caught: raising a fresh CapacityError without __cause__ blinds the
    operator to the last real RunPod message. The chain has to reach them."""
    transport = _ScriptedTransport(["capacity", "capacity", "capacity"])
    with pytest.raises(CapacityError) as exc_info:
        _provider(transport).create_instance(_spec(_offer(0)))

    assert "offers exhausted" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, CapacityError)
    assert transport.created_gpu_ids == ["GPU_0", "GPU_1", "GPU_2"]


def test_runpod_does_not_retry_a_non_capacity_error() -> None:
    """Bug caught: retrying an auth failure across every offer turns one clear
    error into N confusing ones and delays the real message. A too-broad
    `except Exception` in the retry loop is exactly how that happens."""
    transport = _ScriptedTransport(["value", "ok", "ok"])
    with pytest.raises(ValueError, match="non-capacity"):
        _provider(transport).create_instance(_spec(_offer(0)))

    assert len(transport.created_gpu_ids) == 1


def test_the_catalog_is_read_once_per_create_not_once_per_attempt() -> None:
    """One enumeration, however many offers it takes.

    Bug caught: re-reading the catalog inside the retry loop turns a capacity
    drought into N round trips against an API that is already rate-limiting,
    and can change the candidate list mid-walk so an offer is tried twice.
    """
    transport = _ScriptedTransport(["capacity", "capacity", "ok"])
    _provider(transport).create_instance(_spec(None))

    assert transport.created_gpu_ids == ["GPU_0", "GPU_1", "GPU_2"]
    assert transport.gpu_type_queries == 1


def test_runpod_selects_for_itself_when_the_caller_passes_no_offer() -> None:
    """S4's inversion: with no offer handed down, RunPod picks from its own
    catalog, ranked by placement.accelerators.

    Bug caught: falling back to "no offer -> no SKU" would send gpuTypeId=None
    on the wire once Task 8 deletes spec.offer, which RunPod rejects with an
    error naming nothing an operator can act on.
    """
    transport = _ScriptedTransport(["ok"])
    inst = _provider(transport).create_instance(_spec(None))

    assert transport.gpu_type_queries == 1
    assert transport.created_gpu_ids == ["GPU_0"]
    assert inst.id == "pod-GPU_0"


def test_an_empty_catalog_is_a_capacity_error_not_an_index_error() -> None:
    """Boundary. Bug caught: indexing offers[0] on an empty enumeration raises
    IndexError from deep inside the provider, which no caller retries and no
    operator can read as 'RunPod has nothing right now'."""

    class _EmptyCatalog(_ScriptedTransport):
        def __call__(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
            if "gpuTypes" in str(body.get("query", "")):
                self.gpu_type_queries += 1
                return {"data": {"gpuTypes": []}}
            return super().__call__(url, body)

    transport = _EmptyCatalog(["ok"])
    with pytest.raises(CapacityError):
        _provider(transport).create_instance(_spec(None))
    assert transport.created_gpu_ids == []
