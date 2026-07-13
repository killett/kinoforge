"""ModalProvider.probe_runtime: found/absent/partial mapping + URL priming."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kinoforge.providers.modal import ModalProvider


def _active_lister(name: str) -> Callable[[], list[dict[str, Any]]]:
    return lambda: [{"name": name, "state": "deployed"}]


def test_absent_app_probes_found_false():
    """Bug caught: a dead app never GC'd (row loops PROBE_FAILED/LIVE forever)."""
    provider = ModalProvider(lister=lambda: [])
    probe = provider.probe_runtime("eph-cafef00d")
    assert probe is not None
    assert probe.found is False
    assert probe.gpu_util_pct is None


def test_active_app_with_primed_url_maps_util_snapshot(monkeypatch):
    """Bug caught: field mismatch UtilSnapshot->RuntimeProbe silently breaks
    STALL_REAP thresholds (gpu/cpu None or swapped)."""
    from kinoforge.core.util_endpoints import UtilSnapshot

    provider = ModalProvider(lister=_active_lister("kinoforge-eph-cafef00d"))
    provider.note_endpoints(
        "eph-cafef00d", {"8000": "https://a--kinoforge-eph-cafef00d-f.modal.run"}
    )

    snap = UtilSnapshot(
        gpu_util_percent=87.0,
        cpu_percent=42.0,
        memory_percent=10.0,
        disk_percent=None,
        uptime_seconds=321,
    )
    monkeypatch.setattr(
        "kinoforge.providers.modal.util.ModalUtilEndpoint.read_util",
        lambda self, instance_id: snap,
    )
    probe = provider.probe_runtime("eph-cafef00d")
    assert probe is not None and probe.found is True
    assert probe.gpu_util_pct == 87.0
    assert probe.cpu_pct == 42.0
    assert probe.container_uptime_s == 321.0
    assert probe.cost_per_hr is None


def test_active_app_without_url_is_partial_probe():
    """Bug caught: probe fabricating found=False (or raising) when the URL is
    unknown would GC a LIVE app's row / spam PROBE_FAILED."""
    provider = ModalProvider(lister=_active_lister("kinoforge-eph-cafef00d"))
    probe = provider.probe_runtime("eph-cafef00d")
    assert probe is not None and probe.found is True
    assert probe.gpu_util_pct is None
    assert probe.error  # names the missing-URL condition


def test_util_transport_error_is_partial_probe(monkeypatch):
    """Bug caught: a flaky /util 5xx crashing the probe -> PROBE_FAILED noise
    instead of a conservative partial probe."""
    from kinoforge.core.errors import TransportError

    provider = ModalProvider(lister=_active_lister("kinoforge-eph-cafef00d"))
    provider.note_endpoints(
        "eph-cafef00d", {"8000": "https://a--kinoforge-eph-cafef00d-f.modal.run"}
    )

    def _boom(self, instance_id):
        raise TransportError("modal /util returned HTTP 502")

    monkeypatch.setattr(
        "kinoforge.providers.modal.util.ModalUtilEndpoint.read_util", _boom
    )
    probe = provider.probe_runtime("eph-cafef00d")
    assert probe is not None and probe.found is True
    assert probe.error and "502" in probe.error


def test_lister_failure_propagates():
    """Bug caught: swallowing a lister crash into found=False would GC live
    rows on a transient `modal app list` failure."""

    def _broken():
        raise RuntimeError("modal CLI absent")

    provider = ModalProvider(lister=_broken)
    with pytest.raises(RuntimeError):
        provider.probe_runtime("eph-cafef00d")
