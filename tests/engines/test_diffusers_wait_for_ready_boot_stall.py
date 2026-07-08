"""wait_for_ready consults the boot-liveness probe and aborts dead boots."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kinoforge.core.boot_liveness import BootVerdict
from kinoforge.core.errors import ProvisionFailed, ProvisionTimeout
from kinoforge.core.interfaces import Instance
from kinoforge.engines.diffusers import DiffusersEngine


def _inst() -> Instance:
    return Instance(
        id="pod-abc",
        provider="runpod",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "http://pod-abc.runpod.io"},
    )


class _Probe:
    def __init__(self, verdict: BootVerdict) -> None:
        self._v = verdict
        self.calls = 0

    def check(self, _iid: str) -> BootVerdict:
        self.calls += 1
        return self._v


def _wait(
    engine: DiffusersEngine,
    *,
    probe: _Probe | None,
    timeout_s: float = 900.0,
    get_instance: Callable[[str], Instance] | None = None,
) -> None:
    def http_get(_url: str) -> dict[str, object]:
        raise RuntimeError("health not up")

    def _get_instance(_iid: str) -> Instance:
        return _inst()

    engine.attach_boot_liveness_probe(probe)
    engine.wait_for_ready(
        _inst(),
        http_get=http_get,
        sleep=lambda _s: None,
        get_instance=get_instance or _get_instance,
        timeout_s=timeout_s,
    )


def test_stalled_probe_aborts_fast() -> None:
    # Bug caught: a dead boot burns the full boot_timeout (900s) instead of
    # bailing when the probe says STALLED.
    engine = DiffusersEngine()
    with pytest.raises(ProvisionFailed):
        _wait(engine, probe=_Probe(BootVerdict.STALLED))


def test_gone_probe_aborts() -> None:
    engine = DiffusersEngine()
    with pytest.raises(ProvisionFailed):
        _wait(engine, probe=_Probe(BootVerdict.GONE))


def test_get_instance_keyerror_maps_to_provisionfailed() -> None:
    # Bug caught: a reclaimed pod makes get_instance KeyError, which today
    # escapes as an unhandled KeyError instead of a clean ProvisionFailed.
    engine = DiffusersEngine()

    def gone(_iid):
        raise KeyError("pod gone")

    with pytest.raises(ProvisionFailed):
        _wait(engine, probe=_Probe(BootVerdict.ALIVE), get_instance=gone)


def test_none_probe_preserves_timeout() -> None:
    # Bug caught: adding the probe changes the no-probe path (must still time out).
    engine = DiffusersEngine()
    with pytest.raises(ProvisionTimeout):
        _wait(engine, probe=None, timeout_s=0.0)
