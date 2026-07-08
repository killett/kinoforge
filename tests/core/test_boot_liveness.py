"""Pure boot-liveness verdict logic — no network, fakes only."""

from __future__ import annotations

from kinoforge.core.boot_liveness import (
    BootLivenessResult,
    BootVerdict,
    classify_boot_liveness,
)
from kinoforge.core.util_endpoints import UtilSnapshot


def _snap(cpu: float, mem: float, disk: float | None = None) -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=0.0,
        cpu_percent=cpu,
        memory_percent=mem,
        disk_percent=disk,
        uptime_seconds=120,
    )


def _classify(**kw: object) -> BootLivenessResult:
    base: dict[str, object] = dict(
        exists=True,
        log_tail=None,
        snap=_snap(0.0, 5.0),
        prev_snap=_snap(0.0, 5.0),
        consecutive_flat=0,
        elapsed_s=300.0,
        grace_s=90.0,
        consecutive_needed=3,
    )
    base.update(kw)
    return classify_boot_liveness(**base)  # type: ignore[arg-type]


def test_gone_when_not_exists() -> None:
    # Bug caught: a reclaimed pod is not detected during boot and waits 900s.
    r = _classify(exists=False, consecutive_flat=2)
    assert r.verdict is BootVerdict.GONE
    assert r.consecutive_flat == 0


def test_trap_nonzero_is_stalled() -> None:
    # Bug caught: provision script crashed under the trap (rc!=0) but wait_for_ready
    # keeps polling /health for the full boot_timeout.
    r = _classify(log_tail="... \n[bootstrap-trap] rc=1 at 2026-07-07T00:00:00Z\n")
    assert r.verdict is BootVerdict.STALLED


def test_trap_zero_is_not_stalled() -> None:
    # Bug caught: rc=0 (provision succeeded, server coming up) misread as dead.
    r = _classify(log_tail="[bootstrap-trap] rc=0 at 2026-07-07T00:00:00Z\n")
    assert r.verdict is not BootVerdict.STALLED


def test_grace_window_suppresses_flatline() -> None:
    # Bug caught: early-boot quiet trips a false STALLED before the pod has had
    # time to start downloading.
    r = _classify(
        elapsed_s=30.0,
        snap=_snap(0.0, 5.0),
        prev_snap=_snap(0.0, 5.0),
        consecutive_flat=2,
    )
    assert r.verdict is not BootVerdict.STALLED
    assert r.consecutive_flat == 0


def test_flatline_reaches_threshold_stalls() -> None:
    # Bug caught: a hung boot (CPU 0, mem flat, disk flat) is never declared dead.
    r = _classify(
        snap=_snap(0.0, 5.0, disk=40.0),
        prev_snap=_snap(0.0, 5.0, disk=40.0),
        consecutive_flat=2,
    )
    assert r.verdict is BootVerdict.STALLED
    assert r.consecutive_flat == 3


def test_progress_resets_counter() -> None:
    # Bug caught: a slow-but-healthy download (disk growing) is killed as stalled.
    r = _classify(
        snap=_snap(0.0, 5.0, disk=42.0),
        prev_snap=_snap(0.0, 5.0, disk=40.0),
        consecutive_flat=2,
    )
    assert r.verdict is BootVerdict.ALIVE
    assert r.consecutive_flat == 0


def test_cpu_active_is_progress() -> None:
    r = _classify(snap=_snap(13.0, 5.0), prev_snap=_snap(0.0, 5.0), consecutive_flat=2)
    assert r.verdict is BootVerdict.ALIVE
    assert r.consecutive_flat == 0


def test_snap_none_is_unknown() -> None:
    # Bug caught: a transient util-probe error is treated as flatline → false kill.
    r = _classify(snap=None, consecutive_flat=1)
    assert r.verdict is BootVerdict.UNKNOWN
    assert r.consecutive_flat == 1
