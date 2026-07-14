"""DiffusersBackend.set_lora_stack — error mapping."""

from __future__ import annotations

import io
import json
import urllib.error
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


def _http_error(status: int, detail: dict[str, Any]) -> urllib.error.HTTPError:
    """Return a real urllib.error.HTTPError whose .read() yields FastAPI's JSON body.

    # Bug caught: a real urllib HTTPError(507) has no .body attribute — the
    # old getattr(e,'body') path silently degraded disk-full to PodUnreachable;
    # decode e.read()['detail'] instead.

    FastAPI serialises HTTPException(status_code=N, detail=<dict>) as the
    JSON body {"detail": <dict>}.  This helper builds the exact wire shape
    so tests exercise the real decode path rather than a fabricated shortcut.
    """
    body = json.dumps({"detail": detail}).encode("utf-8")
    return urllib.error.HTTPError(
        url="http://pod:8000/lora/set_stack",
        code=status,
        msg="HTTP Error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


def _profile() -> ModelProfile:
    """Return a minimal ModelProfile for test backends."""
    return ModelProfile(
        name="wan-2.2",
        max_frames=81,
        fps=24,
        supported_modes={"t2v"},
        max_resolution=(1024, 1024),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


def _backend(
    http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> DiffusersBackend:
    return DiffusersBackend(
        http_post=http_post,
        http_get=lambda url: {},
        base_url="http://pod",
        probe_profile=_profile(),
    )


def _backend_with_get(
    http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
    http_get: Callable[[str], dict[str, Any]],
    *,
    poll_timeout_s: float = 10.0,
) -> DiffusersBackend:
    """Construct a DiffusersBackend with both http_post and http_get stubs.

    Args:
        http_post: Callable for POST requests (submit path).
        http_get: Callable for GET requests (status poll path).
        poll_timeout_s: Wall-clock timeout cap for polling; default 10s for tests.

    Returns:
        A configured DiffusersBackend instance.
    """
    return DiffusersBackend(
        http_post=http_post,
        http_get=http_get,
        base_url="http://pod",
        probe_profile=_profile(),
        sleep=lambda s: None,
        poll_timeout_s=poll_timeout_s,
        poll_interval_s=0.0,
    )


def _entry(ref: str, strength: float = 1.0) -> LoraEntry:
    """Return a minimal LoraEntry for test stacks.

    Args:
        ref: LoRA ref string.
        strength: Adapter weight. Defaults to 1.0.

    Returns:
        A LoraEntry with branch="auto".
    """
    return LoraEntry(ref=ref, strength=strength)


def test_set_lora_stack_threads_explicit_branch_to_wire() -> None:
    """Bug: orchestrator strips ``branch`` from the wire body, defeating
    the pod-side per-transformer routing. The Arcane Style Wan-2.2 pair
    in canonical cfg specifies ``branch=high_noise`` / ``branch=low_noise``;
    a missing field on the wire would silently route both into
    ``transformer`` only."""
    captured: dict[str, Any] = {}

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured["body"] = body
        return {"job_id": "s-branch-test"}

    def _get(url: str) -> dict[str, Any]:
        return {
            "state": "done",
            "inventory": [],
            "free_bytes": 0,
            "swap_rejected": None,
            "error": None,
        }

    backend = _backend_with_get(_post, _get)
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
    """Submit POST → job_id, poll to done, return inventory intact.

    Bug: backend swallows the response or reads inventory from the submit
    body instead of the status poll, returning None or raising KeyError.
    """
    captured: dict[str, Any] = {}

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        captured["url"] = url
        captured["body"] = body
        return {"job_id": "s-happy"}

    def _get(url: str) -> dict[str, Any]:
        return {
            "state": "done",
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
            "error": None,
        }

    backend = _backend_with_get(_post, _get)
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
        raise _http_error(
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
        raise _http_error(
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
        raise _http_error(
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
    """Job done + swap_rejected.reason=vram_oom → LoraSwapVramOomError.

    Bug: VRAM rollback response is treated as success; caller proceeds to
    generate against an adapter set that doesn't actually exist on the pod.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"job_id": "s-vram"}

    def _get(url: str) -> dict[str, Any]:
        return {
            "state": "done",
            "inventory": [],
            "free_bytes": 5000,
            "swap_rejected": {
                "reason": "vram_oom",
                "target_refs_dropped": ["civitai:big@1"],
            },
            "error": None,
        }

    backend = _backend_with_get(_post, _get)
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


# ---------------------------------------------------------------------------
# Job-based (async submit + poll) tests — Task 2
# ---------------------------------------------------------------------------


def test_set_lora_stack_job_done_returns_inventory() -> None:
    """set_lora_stack reads job_id from submit, polls to done, returns result.

    Bug caught: client still reads the synchronous inventory body from the
    submit POST response instead of polling /lora/set_stack/status/{job_id},
    so it returns None or raises KeyError when the server returns {job_id}.
    """
    get_calls: list[str] = []
    gets: list[dict[str, Any]] = [
        {
            "state": "running",
            "inventory": None,
            "free_bytes": None,
            "swap_rejected": None,
            "error": None,
        },
        {
            "state": "done",
            "inventory": [{"ref": "civitai:A@1"}],
            "free_bytes": 5000,
            "swap_rejected": None,
            "error": None,
        },
    ]

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"job_id": "s-abc1"}

    def _get(url: str) -> dict[str, Any]:
        get_calls.append(url)
        return gets.pop(0)

    backend = _backend_with_get(_post, _get)
    out = backend.set_lora_stack(
        pod_id="pod-1",
        active_stack=[_entry("civitai:A@1")],
        download_specs={"civitai:A@1": {"url": "x", "headers": {}, "filename": "a.s"}},
    )
    assert out["inventory"] == [{"ref": "civitai:A@1"}]
    assert out["free_bytes"] == 5000
    assert out["swap_rejected"] is None
    # Both status polls must have hit the correct URL
    assert all("s-abc1" in u for u in get_calls)


def test_set_lora_stack_job_done_with_swap_rejected_raises_vram_oom() -> None:
    """Job done + swap_rejected.reason=vram_oom raises LoraSwapVramOomError.

    Bug caught: client returns the done payload verbatim instead of inspecting
    swap_rejected, so vram_oom goes undetected and the caller proceeds as
    if the adapter set actually loaded.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"job_id": "s-abc2"}

    def _get(url: str) -> dict[str, Any]:
        return {
            "state": "done",
            "inventory": [],
            "free_bytes": 5000,
            "swap_rejected": {"reason": "vram_oom", "target_refs_dropped": ["r1"]},
            "error": None,
        }

    backend = _backend_with_get(_post, _get)
    with pytest.raises(LoraSwapVramOomError) as ei:
        backend.set_lora_stack(
            pod_id="pod-2", active_stack=[_entry("r1")], download_specs={}
        )
    assert ei.value.dropped_refs == ["r1"]
    assert ei.value.pod_id == "pod-2"


def test_set_lora_stack_job_error_502_empty_evict_raises_download_error() -> None:
    """Job error with status=502 + empty evict_completed → LoraSwapDownloadError.

    Bug caught: client treats all job errors identically instead of routing
    through _raise_lora_swap_error, losing the eviction-state distinction.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"job_id": "s-abc3"}

    def _get(url: str) -> dict[str, Any]:
        return {
            "state": "error",
            "inventory": None,
            "free_bytes": None,
            "swap_rejected": None,
            "error": {
                "error": "lora_download_failed",
                "status": 502,
                "evict_completed": [],
                "download_failed": "civitai:B@2",
                "underlying": "connection reset",
            },
        }

    backend = _backend_with_get(_post, _get)
    with pytest.raises(LoraSwapDownloadError) as ei:
        backend.set_lora_stack(
            pod_id="pod-3", active_stack=[_entry("civitai:B@2")], download_specs={}
        )
    assert ei.value.pod_id == "pod-3"
    assert ei.value.ref == "civitai:B@2"


def test_set_lora_stack_job_error_502_nonempty_evict_raises_degraded() -> None:
    """Job error with status=502 + non-empty evict_completed → LoraSwapDegradedPodError.

    Bug caught: client collapses both 502 variants into LoraSwapDownloadError,
    hiding the half-state that means the pod must not be reused.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"job_id": "s-abc4"}

    def _get(url: str) -> dict[str, Any]:
        return {
            "state": "error",
            "inventory": None,
            "free_bytes": None,
            "swap_rejected": None,
            "error": {
                "error": "lora_download_failed",
                "status": 502,
                "evict_completed": ["civitai:X@1"],
                "download_failed": "civitai:B@2",
                "underlying": "timeout",
            },
        }

    backend = _backend_with_get(_post, _get)
    with pytest.raises(LoraSwapDegradedPodError) as ei:
        backend.set_lora_stack(
            pod_id="pod-4", active_stack=[_entry("civitai:B@2")], download_specs={}
        )
    assert ei.value.evict_completed == ["civitai:X@1"]
    assert ei.value.pod_id == "pod-4"


def test_set_lora_stack_job_error_507_raises_disk_full() -> None:
    """Job error with status=507 → LoraSwapDiskFullError.

    Bug caught: client raises a generic error instead of routing 507 through
    _raise_lora_swap_error, so the matcher cannot distinguish disk-full from
    download failures.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"job_id": "s-abc5"}

    def _get(url: str) -> dict[str, Any]:
        return {
            "state": "error",
            "inventory": None,
            "free_bytes": None,
            "swap_rejected": None,
            "error": {
                "error": "disk_full",
                "status": 507,
                "phase": "download",
                "evict_completed": [],
                "download_failed": "civitai:C@3",
            },
        }

    backend = _backend_with_get(_post, _get)
    with pytest.raises(LoraSwapDiskFullError):
        backend.set_lora_stack(
            pod_id="pod-5", active_stack=[_entry("civitai:C@3")], download_specs={}
        )


def test_set_lora_stack_submit_time_507_raises_disk_full() -> None:
    """Synchronous 507 from submit POST raises LoraSwapDiskFullError.

    Bug caught: submit-time structured errors (507) are treated as transport
    failures and raise LoraSwapPodUnreachableError instead of the specific
    LoraSwapDiskFullError that lets the matcher distinguish disk-full from
    unreachability.  A real urllib.error.HTTPError has no .body attribute —
    the old getattr(e,'body') path silently degraded disk-full to PodUnreachable;
    the fix decodes e.read()['detail'] to get the structured error body.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        raise _http_error(
            507,
            {
                "error": "disk_full",
                "phase": "plan",
                "evict_completed": [],
                "download_failed": "civitai:D@4",
            },
        )

    backend = _backend_with_get(_post, lambda url: {})
    with pytest.raises(LoraSwapDiskFullError):
        backend.set_lora_stack(
            pod_id="pod-6", active_stack=[_entry("civitai:D@4")], download_specs={}
        )


def test_set_lora_stack_poll_timeout_raises_pod_unreachable() -> None:
    """Poll wall-clock timeout raises LoraSwapPodUnreachableError.

    Bug caught: poll loop runs forever on a stuck "running" state, never
    surfacing a timeout to the caller.
    """

    def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"job_id": "s-abc6"}

    def _get(url: str) -> dict[str, Any]:
        return {
            "state": "running",
            "inventory": None,
            "free_bytes": None,
            "swap_rejected": None,
            "error": None,
        }

    backend = _backend_with_get(_post, _get, poll_timeout_s=0.0)
    with pytest.raises(LoraSwapPodUnreachableError) as ei:
        backend.set_lora_stack(pod_id="pod-7", active_stack=[], download_specs={})
    assert "s-abc6" in ei.value.underlying
