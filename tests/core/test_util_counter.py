"""Table-driven tests for the consecutive-low-util counter (C26 Task 5)."""

from __future__ import annotations

from kinoforge.core.util_counter import _update_counter
from kinoforge.core.util_endpoints import UtilSnapshot


def _snap(
    *, gpu: float | None, cpu: float | None, uptime: int | None = 100
) -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=gpu,
        cpu_percent=cpu,
        memory_percent=None,
        disk_percent=None,
        uptime_seconds=uptime,
    )


def test_returns_prev_counter_when_snap_none() -> None:
    assert (
        _update_counter(
            3,
            prev_uptime_s=100,
            snap=None,
            gpu_threshold=5.0,
            cpu_threshold=20.0,
        )
        == 3
    )


def test_resets_on_uptime_decrease() -> None:
    snap = _snap(gpu=1.0, cpu=10.0, uptime=5)
    assert (
        _update_counter(
            3,
            prev_uptime_s=100,
            snap=snap,
            gpu_threshold=5.0,
            cpu_threshold=20.0,
        )
        == 0
    )


def test_increments_when_gpu_and_cpu_low() -> None:
    snap = _snap(gpu=2.0, cpu=10.0)
    assert (
        _update_counter(
            3,
            prev_uptime_s=50,
            snap=snap,
            gpu_threshold=5.0,
            cpu_threshold=20.0,
        )
        == 4
    )


def test_resets_when_gpu_above_threshold() -> None:
    snap = _snap(gpu=80.0, cpu=10.0)
    assert (
        _update_counter(
            3,
            prev_uptime_s=50,
            snap=snap,
            gpu_threshold=5.0,
            cpu_threshold=20.0,
        )
        == 0
    )


def test_resets_when_cpu_above_threshold() -> None:
    snap = _snap(gpu=2.0, cpu=80.0)
    assert (
        _update_counter(
            3,
            prev_uptime_s=50,
            snap=snap,
            gpu_threshold=5.0,
            cpu_threshold=20.0,
        )
        == 0
    )


def test_resets_when_gpu_is_none() -> None:
    snap = _snap(gpu=None, cpu=5.0)
    assert (
        _update_counter(
            3,
            prev_uptime_s=50,
            snap=snap,
            gpu_threshold=5.0,
            cpu_threshold=20.0,
        )
        == 0
    )


def test_resets_when_cpu_is_none() -> None:
    snap = _snap(gpu=2.0, cpu=None)
    assert (
        _update_counter(
            3,
            prev_uptime_s=50,
            snap=snap,
            gpu_threshold=5.0,
            cpu_threshold=20.0,
        )
        == 0
    )


def test_first_tick_no_prev_uptime_increments_when_low() -> None:
    snap = _snap(gpu=2.0, cpu=10.0)
    assert (
        _update_counter(
            0,
            prev_uptime_s=None,
            snap=snap,
            gpu_threshold=5.0,
            cpu_threshold=20.0,
        )
        == 1
    )


def test_does_not_reset_when_uptime_present_but_prev_none() -> None:
    """prev_uptime_s None never triggers reset; first tick stays in low-counter mode."""
    snap = _snap(gpu=2.0, cpu=10.0, uptime=5)
    assert (
        _update_counter(
            0,
            prev_uptime_s=None,
            snap=snap,
            gpu_threshold=5.0,
            cpu_threshold=20.0,
        )
        == 1
    )


def test_threshold_boundary_strict_less_than() -> None:
    """Threshold compare is strict < — equal value is NOT low."""
    snap = _snap(gpu=5.0, cpu=10.0)
    assert (
        _update_counter(
            3,
            prev_uptime_s=50,
            snap=snap,
            gpu_threshold=5.0,
            cpu_threshold=20.0,
        )
        == 0
    )
