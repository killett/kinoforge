"""Table-driven tests for the consecutive-low-util counter (C26 Task 5)."""

from __future__ import annotations

import pytest

from kinoforge.core.util_counter import _update_counter, _update_uptime_counter
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


class TestUpdateUptimeCounter:
    """C27: consecutive-low-uptime counter pure state machine."""

    @pytest.mark.parametrize(
        "name, prev, snap_key, uptime_threshold, expected",
        [
            ("transport hiccup preserves at high counter", 9, None, 90.0, 9),
            ("snap with uptime_seconds=None resets", 5, "SNAP_UPTIME_NONE", 90.0, 0),
            ("uptime strictly < threshold increments", 3, "SNAP_UPTIME_89", 90.0, 4),
            ("uptime == threshold resets (strict <)", 3, "SNAP_UPTIME_90", 90.0, 0),
            ("uptime > threshold resets", 7, "SNAP_UPTIME_200", 90.0, 0),
            ("near-threshold-from-below stays below", 0, "SNAP_UPTIME_89", 90.0, 1),
            ("fresh tick uptime=1 always counts", 0, "SNAP_UPTIME_1", 90.0, 1),
            ("extreme threshold (0) blocks all", 5, "SNAP_UPTIME_1", 0.0, 0),
        ],
    )
    def test_counter_table(
        self,
        name: str,
        prev: int,
        snap_key: str | None,
        uptime_threshold: float,
        expected: int,
    ) -> None:
        """Each row asserts the state machine returns the expected counter."""
        snap_map: dict[str, UtilSnapshot | None] = {
            "SNAP_UPTIME_NONE": UtilSnapshot(None, None, None, None, None),
            "SNAP_UPTIME_89": UtilSnapshot(None, None, None, None, 89),
            "SNAP_UPTIME_90": UtilSnapshot(None, None, None, None, 90),
            "SNAP_UPTIME_200": UtilSnapshot(None, None, None, None, 200),
            "SNAP_UPTIME_1": UtilSnapshot(None, None, None, None, 1),
        }
        resolved_snap = None if snap_key is None else snap_map[snap_key]
        result = _update_uptime_counter(
            prev, snap=resolved_snap, uptime_threshold_s=uptime_threshold
        )
        assert result == expected, f"case={name!r} got {result} want {expected}"
