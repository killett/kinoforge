"""A growing bootstrap log is proof of life, and must outrank a util flatline.

Found live 2026-09-12 on pod ``rohjrsmre9obsp``, running the shipped 1.3B swap
grid. The boot-liveness probe killed a HEALTHY pod with ``boot stalled
(provision crashed or util flatline)`` while its bootstrap log was visibly
downloading the model — live ``huggingface.co`` requests, ``Fetching 19 files``,
and the log grew to 9233 B between two fetches taken 30 s apart.

The predicate cannot tell those apart on RunPod, and it is structural rather
than a tuning problem. ``_is_flat`` is "CPU 0 AND memory flat AND disk
flat-or-unknown", and an HF download is precisely a CPU-idle, network-bound,
disk-filling operation: it streams to disk, so memory stays flat, and
``runpod/util.py`` states in its own module docstring that "RunPod's GraphQL has
no disk-percent field, so ``disk_percent`` is always ``None``". The one signal
that would distinguish a download from a corpse is unavailable on this provider
by design, so the predicate degenerates to "CPU 0 and memory flat" — which a
slow download satisfies perfectly.

``classify_boot_liveness`` already RECEIVES the log tail. It reads it only for a
failed trap rc, never as evidence of progress.
"""

from __future__ import annotations

from kinoforge.core.boot_liveness import (
    BootLivenessResult,
    BootVerdict,
    classify_boot_liveness,
)
from kinoforge.core.util_endpoints import UtilSnapshot


def _flat_snap() -> UtilSnapshot:
    """Return the exact util shape a RunPod HF download produces.

    CPU idle (the work is network-bound), memory flat (it streams to disk), and
    disk unknown because RunPod never reports it.

    Returns:
        A ``UtilSnapshot`` indistinguishable from a dead container's.
    """
    return UtilSnapshot(
        gpu_util_percent=0.0,
        cpu_percent=0.0,
        memory_percent=5.0,
        disk_percent=None,
        uptime_seconds=500,
    )


def _classify(**kw: object) -> BootLivenessResult:
    """Classify with defaults matching a past-grace RunPod boot.

    Args:
        **kw: Overrides for the classifier's arguments.

    Returns:
        The classifier's result.
    """
    base: dict[str, object] = dict(
        exists=True,
        log_tail=None,
        prev_log_tail=None,
        snap=_flat_snap(),
        prev_snap=_flat_snap(),
        consecutive_flat=2,  # one short of the default threshold
        elapsed_s=500.0,
        grace_s=90.0,
        consecutive_needed=3,
    )
    base.update(kw)
    return classify_boot_liveness(**base)  # type: ignore[arg-type]


def test_growing_log_keeps_a_downloading_pod_alive() -> None:
    """A log that gained bytes resets the flatline counter.

    Bug caught: the shipped behaviour, which killed pod rohjrsmre9obsp mid
    model-download. Without this the counter reaches ``consecutive_needed`` and
    the provision is aborted while HF transfers are still in flight — the util
    snapshot alone genuinely cannot distinguish that from a crash on RunPod.
    """
    r = _classify(
        prev_log_tail="Fetching 19 files:   0%|          | 0/19",
        log_tail="Fetching 19 files:  21%|██        | 4/19",
    )

    assert r.verdict is BootVerdict.ALIVE, r
    assert r.consecutive_flat == 0, r


def test_unchanged_log_still_counts_toward_stalled() -> None:
    """A frozen log does not rescue a flatlined pod.

    Bug caught: a fix that returns ALIVE whenever a log tail merely EXISTS,
    which would disarm stall detection completely — every dead pod has a log.
    The guard must still fire when nothing is moving.
    """
    frozen = "Fetching 19 files:   0%|          | 0/19"

    r = _classify(prev_log_tail=frozen, log_tail=frozen)

    assert r.verdict is BootVerdict.STALLED, r


def test_failed_trap_rc_still_wins_over_a_growing_log() -> None:
    """A crashed provision is STALLED even though its log just grew.

    This is the sharp one. When the bootstrap trap fires it APPENDS
    ``[bootstrap-trap] rc=1`` — so the log grows at the exact moment the
    provision dies. A naive "log grew, therefore alive" check would read a
    crash as progress and hang until boot_timeout, turning a fast-fail into
    the long wait this probe exists to avoid.
    """
    r = _classify(
        prev_log_tail="installing wheels...",
        log_tail="installing wheels...\n[bootstrap-trap] rc=1 at 2026-09-12T14:19:00Z",
    )

    assert r.verdict is BootVerdict.STALLED, r


def test_absent_log_preserves_the_util_only_behaviour() -> None:
    """With no log available, the util flatline still decides.

    Bug caught: making log progress REQUIRED for a stall verdict, which would
    disable the probe on any provider or moment where the sidecar is
    unreachable — exactly when a boot is most likely to be broken.
    """
    r = _classify(prev_log_tail=None, log_tail=None)

    assert r.verdict is BootVerdict.STALLED, r


def test_first_probe_with_a_log_does_not_count_as_progress() -> None:
    """No previous tail means no progress claim, only the util evidence.

    Bug caught: treating ``prev_log_tail is None`` as "changed", which would
    silently grant every first post-grace probe a free reset and delay a real
    stall verdict by one full interval.
    """
    r = _classify(prev_log_tail=None, log_tail="some output")

    assert r.verdict is BootVerdict.STALLED, r
