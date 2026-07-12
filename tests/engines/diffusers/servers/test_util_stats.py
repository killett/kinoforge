"""Offline: the server-side GPU/CPU/mem stats reader (Modal util probe).

Bugs caught: a reader that raises when pynvml/nvidia-smi/psutil are absent
would crash the /util route (and, if that route were async, stall /health).
"""

from __future__ import annotations

import pytest

import kinoforge.engines.diffusers.servers._util_stats as us


def test_read_gpu_stats_has_all_five_keys() -> None:
    d = us.read_gpu_stats()
    assert set(d) == {
        "gpu_util_percent",
        "cpu_percent",
        "memory_percent",
        "disk_percent",
        "uptime_seconds",
    }


def test_reader_never_raises_when_everything_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force every source to fail; the reader must degrade to None, not raise.
    monkeypatch.setattr(us, "_read_gpu_via_pynvml", lambda: None)
    monkeypatch.setattr(us, "_read_gpu_via_smi", lambda: None)
    monkeypatch.setattr(us, "_read_host_via_psutil", lambda: (None, None, None))
    d = us.read_gpu_stats()
    assert d["gpu_util_percent"] is None
    assert d["cpu_percent"] is None
    assert d["memory_percent"] is None
    assert d["disk_percent"] is None
    assert isinstance(d["uptime_seconds"], int)  # uptime always computable


def test_pynvml_preferred_over_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us, "_read_gpu_via_pynvml", lambda: (42.0, 55.0))
    monkeypatch.setattr(
        us, "_read_gpu_via_smi", lambda: (99.0, 99.0)
    )  # must be ignored
    d = us.read_gpu_stats()
    assert d["gpu_util_percent"] == 42.0


def test_smi_fallback_when_pynvml_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(us, "_read_gpu_via_pynvml", lambda: None)
    monkeypatch.setattr(us, "_read_gpu_via_smi", lambda: (17.0, 33.0))
    d = us.read_gpu_stats()
    assert d["gpu_util_percent"] == 17.0
