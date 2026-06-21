"""Pure-ish helpers used by /lora/set_stack."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest


def _inv_entry(ref: str, size: int, last_used: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "filename": f"{ref}.s",
        "size_bytes": size,
        "loras_dir_path": f"/loras/{ref}.s",
        "downloaded_at_local": last_used,
        "last_used_at_local": last_used,
        "adapter_name": "lora_x",
    }


def test_pick_lru_evict_chooses_oldest_first() -> None:
    """Bug: helper sorts by newest-first instead of oldest-first → evicts
    the entries the operator most recently used, defeating LRU."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    inventory = {
        "A": _inv_entry("A", size=100, last_used="2026-06-20T10:00:00-07:00"),
        "B": _inv_entry("B", size=100, last_used="2026-06-20T09:00:00-07:00"),
        "C": _inv_entry("C", size=100, last_used="2026-06-20T11:00:00-07:00"),
    }
    candidates = {"A", "B", "C"}
    plan = s._pick_lru_evict(candidates, inventory, need=100)
    assert plan == ["B"]


def test_pick_lru_evict_pops_until_enough_room() -> None:
    """Bug: helper returns the single LRU entry even when one isn't enough,
    leaving the swap to fail mid-download from disk-pressure."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    inventory = {
        "A": _inv_entry("A", size=50, last_used="2026-06-20T09:00:00-07:00"),
        "B": _inv_entry("B", size=50, last_used="2026-06-20T10:00:00-07:00"),
        "C": _inv_entry("C", size=50, last_used="2026-06-20T11:00:00-07:00"),
    }
    plan = s._pick_lru_evict({"A", "B", "C"}, inventory, need=120)
    assert plan == ["A", "B", "C"]


def test_pick_lru_evict_returns_none_if_insufficient() -> None:
    """Bug: helper returns the partial list anyway, lying about feasibility
    → matcher commits to a doomed swap plan."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    inventory = {
        "A": _inv_entry("A", size=50, last_used="2026-06-20T09:00:00-07:00"),
    }
    assert s._pick_lru_evict({"A"}, inventory, need=999) is None


def test_pick_lru_evict_empty_plan_when_need_zero() -> None:
    """Bug: matcher passes need=0 (everything fits) but helper returns
    a non-empty plan, evicting LoRAs we wanted to keep."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    inventory = {
        "A": _inv_entry("A", size=50, last_used="2026-06-20T09:00:00-07:00"),
    }
    assert s._pick_lru_evict({"A"}, inventory, need=0) == []
    assert s._pick_lru_evict({"A"}, inventory, need=-5) == []


def test_reload_pipeline_loras_unloads_then_reloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: helper forgets unload_lora_weights() before reloading, leaving
    stale adapters resident → set_adapters silently ignores the new ones."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    calls: list[tuple[Any, ...]] = []

    class _Stub:
        def unload_lora_weights(self) -> None:
            calls.append(("unload",))

        def load_lora_weights(self, path: str, adapter_name: str) -> None:
            calls.append(("load", path, adapter_name))

        def set_adapters(self, names: list[str]) -> None:
            calls.append(("set_adapters", list(names)))

    monkeypatch.setattr(s, "pipe", _Stub())
    s._inventory.clear()
    s._inventory["A"] = _inv_entry("A", 1, "x")
    s._inventory["A"]["loras_dir_path"] = "/loras/A"
    s._inventory["B"] = _inv_entry("B", 1, "x")
    s._inventory["B"]["loras_dir_path"] = "/loras/B"

    s._reload_pipeline_loras(["A", "B"])

    assert calls[0] == ("unload",)
    assert calls[1] == ("load", "/loras/A", "lora_0")
    assert calls[2] == ("load", "/loras/B", "lora_1")
    assert calls[3] == ("set_adapters", ["lora_0", "lora_1"])


def test_reload_pipeline_loras_empty_unloads_no_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: empty target stack accidentally calls set_adapters([]) and
    triggers a pipeline error."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    calls: list[tuple[str, ...]] = []

    class _Stub:
        def unload_lora_weights(self) -> None:
            calls.append(("unload",))

        def load_lora_weights(self, *a: Any, **k: Any) -> None:
            calls.append(("load",))

        def set_adapters(self, *a: Any, **k: Any) -> None:
            calls.append(("set_adapters",))

    monkeypatch.setattr(s, "pipe", _Stub())
    s._reload_pipeline_loras([])

    assert calls == [("unload",)]


def test_evict_one_removes_inventory_and_unloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bug: helper deletes from _inventory but forgets to delete the file,
    leaking disk; or vice-versa."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as s

    lora_file = tmp_path / "secret.safetensors"
    lora_file.write_bytes(b"x" * 100)

    class _Stub:
        def __init__(self) -> None:
            self.unloaded: list[str] = []

        def delete_adapters(self, names: list[str]) -> None:
            self.unloaded.extend(names)

    stub = _Stub()
    monkeypatch.setattr(s, "pipe", stub)
    s._inventory.clear()
    s._inventory["A"] = {
        "ref": "A",
        "filename": "secret.safetensors",
        "size_bytes": 100,
        "loras_dir_path": str(lora_file),
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_3",
    }

    asyncio.run(s._evict_one("A"))

    assert "A" not in s._inventory
    assert not lora_file.exists()
    assert stub.unloaded == ["lora_3"]
