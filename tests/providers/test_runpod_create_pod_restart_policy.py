"""C28 A3 — ``InstanceSpec.restart_policy`` field + ``_create_pod`` wire branch.

The wire branch consults the A0 schema sidecar before emitting
``restartPolicy`` to RunPod. The plan anticipates the field may not be
exposed; the A0 probe (2026-06-13) confirmed it is NOT — so this code
path always logs + skips on production today. The field still ships so
the orchestrator + CLI surface land in one place; if RunPod ever exposes
the field, re-running the A0 probe flips the sidecar and the wire branch
activates with zero further code change.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
        id="NVIDIA RTX 4090",
        gpu_type="NVIDIA RTX 4090",
        vram_gb=24,
        cuda="12.8",
        cost_rate_usd_per_hr=0.30,
    )


def _input(body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = body["variables"]["input"]
    return payload


def test_default_restart_policy_does_not_emit_field() -> None:
    """Backward compat: default spec wire-shape unchanged."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(image="runpod/pytorch:latest", offer=_offer())
    p.create_instance(spec)
    assert "restartPolicy" not in _input(captured[0][1])


def test_never_with_schema_supported_emits_field_on_wire(tmp_path: Path) -> None:
    sidecar = tmp_path / "schema.json"
    sidecar.write_text(json.dumps({"restart_policy_supported": True}))
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        restart_policy="never",
    )
    with patch(
        "kinoforge.providers.runpod._RUNPOD_SCHEMA_SIDECAR",
        sidecar,
    ):
        p.create_instance(spec)
    assert _input(captured[0][1]).get("restartPolicy") == "NEVER"


def test_never_with_schema_unsupported_skips_field(tmp_path: Path) -> None:
    sidecar = tmp_path / "schema.json"
    sidecar.write_text(json.dumps({"restart_policy_supported": False}))
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        restart_policy="never",
    )
    with patch(
        "kinoforge.providers.runpod._RUNPOD_SCHEMA_SIDECAR",
        sidecar,
    ):
        p.create_instance(spec)
    assert "restartPolicy" not in _input(captured[0][1])


def test_never_with_sidecar_missing_skips_field(tmp_path: Path) -> None:
    """Conservative skip when the sidecar hasn't been captured yet."""
    captured, post = _capture_post()
    p = RunPodProvider(creds=None, http_post=post, http_get=lambda _: {})
    spec = InstanceSpec(
        image="runpod/pytorch:latest",
        offer=_offer(),
        restart_policy="never",
    )
    with patch(
        "kinoforge.providers.runpod._RUNPOD_SCHEMA_SIDECAR",
        tmp_path / "absent.json",
    ):
        p.create_instance(spec)
    assert "restartPolicy" not in _input(captured[0][1])
