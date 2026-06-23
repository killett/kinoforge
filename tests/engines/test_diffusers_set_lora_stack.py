"""DiffusersBackend.set_lora_stack — error mapping."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kinoforge.core.errors import (
    LoraSwapDegradedPodError,
    LoraSwapDiskFullError,
    LoraSwapDownloadError,
    LoraSwapPodUnreachableError,
    LoraSwapVramOomError,
)
from kinoforge.core.interfaces import ModelProfile
from kinoforge.core.lora import LoraEntry
from kinoforge.engines.diffusers import DiffusersBackend


class _HTTPError(Exception):
    """Pod-side error: HTTPError-like exception carrying status + body."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


def _backend(
    http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> DiffusersBackend:
    profile = ModelProfile(
        name="wan-2.2",
        max_frames=81,
        fps=24,
        supported_modes={"t2v"},
        max_resolution=(1024, 1024),
        supports_native_extension=False,
        supports_joint_audio=False,
    )
    return DiffusersBackend(
        http_post=http_post,
        http_get=lambda url: {},
        base_url="http://pod",
        probe_profile=profile,
    )


def test_set_lora_stack_threads_explicit_branch_to_wire() -> None:
    """Bug: orchestrator strips ``branch`` from the wire body, defeating
    the pod-side per-transformer routing. The Arcane Style Wan-2.2 pair
    in canonical cfg specifies ``branch=high_noise`` / ``branch=low_noise``;
    a missing field on the wire would silently route both into
    ``transformer`` only."""
    captured: dict[str, Any] = {}

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured["body"] = body
        return {"inventory": [], "free_bytes": 0, "swap_rejected": None}

    backend = _backend(_post)
    backend.set_lora_stack(
        pod_id="pod-7b2",
        active_stack=[
            LoraEntry(ref="civitai:1@1", strength=1.0, branch="high_noise"),
            LoraEntry(ref="civitai:2@2", strength=0.8, branch="low_noise"),
        ],
        download_specs={},
    )
    assert captured["body"]["target"] == [
        {"ref": "civitai:1@1", "strength": 1.0, "branch": "high_noise"},
        {"ref": "civitai:2@2", "strength": 0.8, "branch": "low_noise"},
    ]


def test_set_lora_stack_happy_path_returns_response() -> None:
    """Happy 200 returns the parsed response body intact.

    Bug: backend swallows the response, returns None → caller cannot update
    the ledger with the post-swap inventory.
    """
    captured: dict[str, Any] = {}

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured["url"] = url
        captured["body"] = body
        return {
            "inventory": [
                {
                    "ref": "civitai:A@1",
                    "filename": "a.s",
                    "size_bytes": 100,
                    "downloaded_at_local": "x",
                    "last_used_at_local": "x",
                    "adapter_name": "lora_0",
                }
            ],
            "free_bytes": 5000,
            "swap_rejected": None,
        }

    backend = _backend(_post)
    resp = backend.set_lora_stack(
        pod_id="pod-7b2",
        active_stack=[LoraEntry(ref="civitai:A@1", strength=0.8)],
        download_specs={
            "civitai:A@1": {
                "url": "https://x/a",
                "headers": {},
                "filename": "a.s",
                "size_hint": 100,
            }
        },
    )
    assert captured["url"] == "http://pod/lora/set_stack"
    # P1 (2026-06-21): wire shape is `target: [{ref, strength}, ...]`,
    # not the legacy `target_refs: [...]`.
    # P2 (2026-06-22): each entry carries ``branch`` (default ``"auto"``
    # — server-side LoraTarget reads it for per-transformer routing).
    assert captured["body"]["target"] == [
        {"ref": "civitai:A@1", "strength": 0.8, "branch": "auto"}
    ]
    assert "target_refs" not in captured["body"]
    assert resp["free_bytes"] == 5000
    assert resp["inventory"][0]["ref"] == "civitai:A@1"


def test_set_lora_stack_502_no_eviction_raises_download_error() -> None:
    """502 + empty evict_completed → LoraSwapDownloadError carrying ref + cause.

    Bug: backend maps every 502 to LoraSwapDegradedPodError regardless of
    eviction state, falsely marking healthy pods as degraded.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        raise _HTTPError(
            502,
            {
                "error": "lora_download_failed",
                "evict_completed": [],
                "download_failed": "civitai:B@2",
                "underlying": "504",
            },
        )

    backend = _backend(_post)
    with pytest.raises(LoraSwapDownloadError) as ei:
        backend.set_lora_stack(
            pod_id="pod-7b2",
            active_stack=[LoraEntry(ref="civitai:B@2")],
            download_specs={
                "civitai:B@2": {"url": "x", "headers": {}, "filename": "b.s"}
            },
        )
    assert ei.value.pod_id == "pod-7b2"
    assert ei.value.ref == "civitai:B@2"


def test_set_lora_stack_502_with_eviction_raises_degraded_error() -> None:
    """502 + non-empty evict_completed → LoraSwapDegradedPodError carries list.

    Bug: degraded-state error drops evict_completed, leaving the operator
    no way to tell which LoRAs were lost.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        raise _HTTPError(
            502,
            {
                "error": "lora_download_failed",
                "evict_completed": ["civitai:X@1"],
                "download_failed": "civitai:B@2",
                "underlying": "504",
            },
        )

    backend = _backend(_post)
    with pytest.raises(LoraSwapDegradedPodError) as ei:
        backend.set_lora_stack(
            pod_id="pod-7b2",
            active_stack=[LoraEntry(ref="civitai:B@2")],
            download_specs={},
        )
    assert ei.value.evict_completed == ["civitai:X@1"]


def test_set_lora_stack_507_raises_disk_full() -> None:
    """507 maps to LoraSwapDiskFullError.

    Bug: 507 falls into the generic 502 branch, leaving the matcher unable
    to distinguish 'this pod is full' from 'this LoRA hosting is down'.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        raise _HTTPError(
            507,
            {
                "error": "disk_full",
                "evict_completed": ["civitai:X@1"],
                "download_failed": "civitai:B@2",
            },
        )

    backend = _backend(_post)
    with pytest.raises(LoraSwapDiskFullError):
        backend.set_lora_stack(pod_id="pod-7b2", active_stack=[], download_specs={})


def test_set_lora_stack_200_swap_rejected_raises_vram_oom() -> None:
    """200 with swap_rejected.reason=vram_oom → LoraSwapVramOomError.

    Bug: VRAM rollback response is treated as success; caller proceeds to
    generate against an adapter set that doesn't actually exist on the pod.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "inventory": [],
            "free_bytes": 5000,
            "swap_rejected": {
                "reason": "vram_oom",
                "target_refs_dropped": ["civitai:big@1"],
            },
        }

    backend = _backend(_post)
    with pytest.raises(LoraSwapVramOomError) as ei:
        backend.set_lora_stack(pod_id="pod-7b2", active_stack=[], download_specs={})
    assert ei.value.dropped_refs == ["civitai:big@1"]


def test_set_lora_stack_transport_error_raises_pod_unreachable() -> None:
    """Bare transport error → LoraSwapPodUnreachableError.

    Bug: transport errors leak as raw ConnectionError, bypassing the LoraSwap
    hierarchy entirely so callers' except blocks miss them.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        raise ConnectionError("ConnectionResetError")

    backend = _backend(_post)
    with pytest.raises(LoraSwapPodUnreachableError) as ei:
        backend.set_lora_stack(pod_id="pod-7b2", active_stack=[], download_specs={})
    assert "ConnectionResetError" in ei.value.underlying
