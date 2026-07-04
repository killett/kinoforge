"""``InstanceSpec.cloud_type`` → RunPod ``cloudType`` wire field.

Motivation (2026-07-03): three consecutive BSA wheel-build pods were
deleted mid-compile. All three landed on community-cloud hosts because
``_create_pod`` hardcoded ``cloudType: "ALL"`` and RunPod schedules the
cheapest capacity — community hosts, whose interruption terminates
zero-volume pods outright. ``cloud_type="secure"`` lets long-running
one-shot workloads (wheel builds) pin dedicated hosts.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from kinoforge.core.interfaces import InstanceSpec, Offer
from kinoforge.providers.runpod import RunPodProvider


def _capture_post() -> tuple[
    list[tuple[str, dict[str, Any]]],
    Callable[[str, dict[str, Any]], dict[str, Any]],
]:
    captured: list[tuple[str, dict[str, Any]]] = []

    def _http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured.append((url, body))
        return {"data": {"podFindAndDeployOnDemand": {"id": "pod-xyz"}}}

    return captured, _http_post


def _offer() -> Offer:
    return Offer(
        id="NVIDIA A100 80GB PCIe",
        gpu_type="NVIDIA A100 80GB PCIe",
        vram_gb=80,
        cuda="12.4",
        cost_rate_usd_per_hr=1.64,
    )


def _input(body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = body["variables"]["input"]
    return payload


def test_default_cloud_type_stays_all() -> None:
    """Default spec emits ``cloudType: "ALL"``.

    Bug caught: a default flip to SECURE would shrink the capacity pool
    (and raise prices) for every existing cfg in one silent stroke.
    """
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    p.create_instance(InstanceSpec(image="runpod/pytorch:latest", offer=_offer()))
    assert _input(captured[0][1])["cloudType"] == "ALL"


def test_secure_cloud_type_emitted_on_wire() -> None:
    """``cloud_type="secure"`` emits ``cloudType: "SECURE"``.

    Bug caught: the field silently ignored → long builds land on
    community hosts and get deleted mid-run (three-pod incident,
    2026-07-03).
    """
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    p.create_instance(
        InstanceSpec(
            image="runpod/pytorch:latest",
            offer=_offer(),
            cloud_type="secure",
        )
    )
    assert _input(captured[0][1])["cloudType"] == "SECURE"


def test_community_cloud_type_emitted_on_wire() -> None:
    """``cloud_type="community"`` emits ``cloudType: "COMMUNITY"``.

    Bug caught: mapping table typo (e.g. lowercase passthrough) —
    RunPod's enum is uppercase and rejects anything else.
    """
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    p.create_instance(
        InstanceSpec(
            image="runpod/pytorch:latest",
            offer=_offer(),
            cloud_type="community",
        )
    )
    assert _input(captured[0][1])["cloudType"] == "COMMUNITY"
