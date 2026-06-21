"""LoraSwap error class hierarchy + __str__ contracts."""

from __future__ import annotations

from kinoforge.core.errors import (
    KinoforgeError,
    LoraSwapDegradedPodError,
    LoraSwapDiskFullError,
    LoraSwapDownloadError,
    LoraSwapError,
    LoraSwapPodUnreachableError,
    LoraSwapVramOomError,
)


def test_all_subclasses_derive_from_base() -> None:
    """Bug: a subclass accidentally derives directly from Exception, breaking
    `except LoraSwapError` blocks in callers."""
    for cls in (
        LoraSwapDownloadError,
        LoraSwapDegradedPodError,
        LoraSwapPodUnreachableError,
        LoraSwapVramOomError,
        LoraSwapDiskFullError,
    ):
        assert issubclass(cls, LoraSwapError)
        assert issubclass(cls, KinoforgeError)


def test_manual_cleanup_command_names_pod_id() -> None:
    """The cleanup command must include the pod_id so the operator can
    copy-paste it into their shell.

    Bug: the helper returns a generic 'kinoforge destroy' without the id.
    """
    err = LoraSwapDownloadError(pod_id="pod-7b2", ref="civitai:X@Y", underlying="504")
    cmd = err.manual_cleanup_command()
    assert "pod-7b2" in cmd
    assert "destroy" in cmd


def test_download_error_str_names_ref_and_underlying() -> None:
    """Bug: __str__ drops the underlying cause, leaving the operator with
    'download failed' and no actionable detail."""
    err = LoraSwapDownloadError(
        pod_id="pod-7b2",
        ref="civitai:2197303@2474081",
        underlying="504 from CivitAI",
    )
    s = str(err)
    assert "civitai:2197303@2474081" in s
    assert "504 from CivitAI" in s


def test_degraded_pod_error_str_flags_retry_path() -> None:
    """Bug: error message says 'pod broken' without telling the operator
    that the matcher will route the next retry elsewhere — leaving them
    thinking the whole feature is stuck."""
    err = LoraSwapDegradedPodError(
        pod_id="pod-7b2",
        evict_completed=["civitai:X@1"],
        download_failed="civitai:B@2",
        underlying="504",
    )
    s = str(err)
    assert "pod-7b2" in s
    assert "civitai:X@1" in s
    assert "civitai:B@2" in s
    assert "degraded" in s.lower()
    assert "retry" in s.lower()


def test_vram_oom_error_str_clarifies_pod_is_healthy() -> None:
    """Bug: rollback succeeded but error message implies the pod is broken,
    so the operator destroys a perfectly healthy pod."""
    err = LoraSwapVramOomError(pod_id="pod-7b2", dropped_refs=["civitai:big@1"])
    s = str(err)
    assert "civitai:big@1" in s
    assert (
        "previous" in s.lower() or "rolled back" in s.lower() or "healthy" in s.lower()
    )


def test_disk_full_error_str_lists_evicted_and_failed() -> None:
    """Bug: __str__ drops evicted or failed ref, hiding what state the
    pod's disk is in from the operator."""
    err = LoraSwapDiskFullError(
        pod_id="pod-7b2",
        evict_completed=["civitai:X@1"],
        download_failed="civitai:B@2",
    )
    s = str(err)
    assert "civitai:X@1" in s
    assert "civitai:B@2" in s


def test_pod_unreachable_error_str_includes_underlying() -> None:
    """Bug: pod-unreachable error drops the underlying transport cause,
    making it indistinguishable from 'pod just doesn't exist'."""
    err = LoraSwapPodUnreachableError(
        pod_id="pod-7b2", underlying="ConnectionResetError"
    )
    s = str(err)
    assert "pod-7b2" in s
    assert "ConnectionResetError" in s
