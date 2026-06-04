"""Ring-2 unit tests for the SkyPilot recording proxy + serializer.

Runs in the default pixi env (no skypilot installed) — the proxy is fed a
DummySky stand-in. See docs/superpowers/specs/2026-06-03-skypilot-real-cloud-design.md
section 9 for AC list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from tests.live._skypilot_recorder import _RecordingProxy, _to_jsonable


@dataclass
class _SampleHandle:
    cluster_name_on_cloud: str
    region: str


class _SampleStatus(StrEnum):
    UP = "UP"
    INIT = "INIT"


class _DummySky:
    """A minimal sky-shaped object for proxy tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def gpu_list(self) -> list[dict[str, Any]]:
        self.calls.append(("gpu_list", (), {}))
        return [{"name": "T4", "vram_gb": 16, "cost_rate_usd_per_hr": 0.35}]

    def launch(self, task_config: dict[str, Any], autostop: float) -> dict[str, Any]:
        self.calls.append(("launch", (task_config,), {"autostop": autostop}))
        return {
            "cluster_name": "cluster-abc",
            "handle": _SampleHandle(
                cluster_name_on_cloud="gcp-12345", region="us-central1"
            ),
            "launched_at": datetime(2026, 6, 3, 12, 0, 0),
            "status": _SampleStatus.UP,
        }


def test_proxy_delegates_call_and_returns_real_value(tmp_path: Path) -> None:
    """AC1: every method call passes through unchanged."""
    real = _DummySky()
    proxy = _RecordingProxy(real, tmp_path)

    result = proxy.gpu_list()

    assert result == [{"name": "T4", "vram_gb": 16, "cost_rate_usd_per_hr": 0.35}]
    assert real.calls == [("gpu_list", (), {})]


def test_proxy_forwards_args_and_kwargs(tmp_path: Path) -> None:
    """AC1 reinforced: positional + keyword args flow through unchanged."""
    real = _DummySky()
    proxy = _RecordingProxy(real, tmp_path)

    proxy.launch({"image": "alpine"}, autostop=1.0)

    assert real.calls == [("launch", ({"image": "alpine"},), {"autostop": 1.0})]


def test_proxy_writes_fixture_file(tmp_path: Path) -> None:
    """AC7: fixture file written to <method_name>.json with sort_keys + indent."""
    real = _DummySky()
    proxy = _RecordingProxy(real, tmp_path)

    proxy.gpu_list()

    fixture = tmp_path / "gpu_list.json"
    assert fixture.exists()
    payload = json.loads(fixture.read_text())
    assert payload == [{"cost_rate_usd_per_hr": 0.35, "name": "T4", "vram_gb": 16}]


def test_to_jsonable_handles_dataclass() -> None:
    """AC2: dataclass → asdict()."""
    handle = _SampleHandle(cluster_name_on_cloud="x", region="us")
    assert _to_jsonable(handle) == {
        "cluster_name_on_cloud": "<volatile>",
        "region": "us",
    }


def test_to_jsonable_handles_enum() -> None:
    """AC3: enum → .value."""
    assert _to_jsonable(_SampleStatus.UP) == "UP"


def test_to_jsonable_handles_path_and_datetime() -> None:
    """AC4 + AC5: pathlib + datetime serialise to str/isoformat."""
    assert _to_jsonable(Path("/tmp/x")) == "/tmp/x"
    assert _to_jsonable(datetime(2026, 6, 3, 12, 0, 0)) == "2026-06-03T12:00:00"


def test_to_jsonable_strips_volatile_keys_recursively() -> None:
    """AC6: volatile keys replaced at any nesting depth."""
    payload = {
        "outer_id": "abc",
        "launched_at": datetime(2026, 6, 3),
        "nested": {"internal_ip": "10.0.0.1", "region": "us"},
        "list_of_dicts": [{"head_ip": "10.0.0.2", "name": "n"}],
    }
    result = _to_jsonable(payload)
    assert result == {
        "outer_id": "abc",
        "launched_at": "<volatile>",
        "nested": {"internal_ip": "<volatile>", "region": "us"},
        "list_of_dicts": [{"head_ip": "<volatile>", "name": "n"}],
    }


def test_two_successive_calls_produce_byte_identical_files(tmp_path: Path) -> None:
    """AC8: stability — same input → same bytes."""
    real = _DummySky()
    proxy = _RecordingProxy(real, tmp_path)

    proxy.launch({"image": "alpine"}, autostop=1.0)
    first = (tmp_path / "launch.json").read_bytes()

    proxy.launch({"image": "alpine"}, autostop=1.0)
    second = (tmp_path / "launch.json").read_bytes()

    assert first == second


def test_proxy_passes_through_non_callable_attributes(tmp_path: Path) -> None:
    """Non-callable attributes on the wrapped object pass through unchanged
    and do NOT generate a fixture file."""
    real = _DummySky()
    real.version = "0.12.3"  # type: ignore[attr-defined]
    proxy = _RecordingProxy(real, tmp_path)

    assert proxy.version == "0.12.3"
    # The presence/absence of fixture files is the load-bearing observable —
    # the non-callable branch must not write anything.
    assert list(tmp_path.iterdir()) == []


def test_to_jsonable_converts_tuple_to_list() -> None:
    """`tuple` input is normalized to `list` so JSON output is consistent
    regardless of whether the SDK returned a tuple or a list."""
    assert _to_jsonable((1, 2, "x")) == [1, 2, "x"]
    # Nested + mixed with volatile-key dicts inside the tuple
    nested = ({"head_ip": "10.0.0.1", "name": "a"}, {"name": "b"})
    assert _to_jsonable(nested) == [
        {"head_ip": "<volatile>", "name": "a"},
        {"name": "b"},
    ]
