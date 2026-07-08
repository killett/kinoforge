"""RunPodBootLivenessProbe: util + bootstrap.log → BootVerdict."""

from __future__ import annotations

from kinoforge.core.boot_liveness import BootVerdict
from kinoforge.core.util_endpoints import UtilSnapshot
from kinoforge.providers.runpod import RunPodBootLivenessProbe


class _Clock:
    def __init__(self, times: list[float]) -> None:
        self._t = times
        self._i = 0

    def now(self) -> float:
        v = self._t[min(self._i, len(self._t) - 1)]
        self._i += 1
        return v


class _UtilEP:
    """Stub util endpoint: probe()->(exists, snap)."""

    def __init__(self, exists: bool, snap: UtilSnapshot | None) -> None:
        self._exists = exists
        self._snap = snap

    def probe(self, _iid: str) -> tuple[bool, UtilSnapshot | None]:
        return (self._exists, self._snap)


def _snap(cpu: float, mem: float, disk: float | None = None) -> UtilSnapshot:
    return UtilSnapshot(
        gpu_util_percent=0.0,
        cpu_percent=cpu,
        memory_percent=mem,
        disk_percent=disk,
        uptime_seconds=120,
    )


def _probe(*, exists=True, snap=None, log_tail="", clock_times=None):
    return RunPodBootLivenessProbe(
        instance_id="pod1",
        util_endpoint=_UtilEP(exists, snap),
        fetch_bootstrap_log=lambda _iid: log_tail,
        grace_s=90.0,
        consecutive_needed=3,
        clock=_Clock(clock_times or [0.0, 100.0, 200.0, 300.0]),
    )


def test_gone_when_probe_absent() -> None:
    # Bug caught: reclaimed-during-boot pod not detected → 900s wait.
    p = _probe(exists=False, snap=_snap(0.0, 5.0))
    assert p.check("pod1") is BootVerdict.GONE


def test_trap_nonzero_stalled() -> None:
    # Bug caught: crashed provision script (rc!=0) not detected.
    p = _probe(snap=_snap(0.0, 5.0), log_tail="[bootstrap-trap] rc=1 at T\n")
    assert p.check("pod1") is BootVerdict.STALLED


def test_flatline_across_calls_stalls() -> None:
    # Bug caught: probe is stateless → never accumulates flatline → never stalls.
    p = _probe(
        snap=_snap(0.0, 5.0, disk=40.0),
        log_tail="",
        clock_times=[0.0, 100.0, 130.0, 160.0],  # all past 90s grace
    )
    # First post-grace call establishes prev_snap; subsequent flat calls count up.
    verdicts = [p.check("pod1") for _ in range(4)]
    assert verdicts[-1] is BootVerdict.STALLED


def test_log_fetch_error_never_raises() -> None:
    # Bug caught: a transient 502 on the log fetch kills a healthy boot.
    def boom(_iid: str) -> str:
        raise RuntimeError("proxy 502")

    p = RunPodBootLivenessProbe(
        instance_id="pod1",
        util_endpoint=_UtilEP(True, _snap(13.0, 5.0)),
        fetch_bootstrap_log=boom,
        grace_s=90.0,
        consecutive_needed=3,
        clock=_Clock([0.0, 100.0]),
    )
    assert p.check("pod1") in (BootVerdict.ALIVE, BootVerdict.UNKNOWN)
