"""GET /lora/inventory — read-only snapshot."""

from __future__ import annotations

import asyncio

import pytest

import kinoforge.engines.diffusers.servers.wan_t2v_server as s


def test_inventory_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty inventory returns [] + current free bytes.

    Bug: endpoint returns 404 or omits free_bytes when inventory is empty,
    forcing every caller to special-case the cold pod.
    """
    s._inventory.clear()
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 999)
    resp = asyncio.run(s.inventory())
    assert resp.inventory == []
    assert resp.free_bytes == 999


def test_inventory_with_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Populated inventory returns shaped LoraInventoryEntry rows + free bytes.

    Bug: endpoint serializes the raw dict (leaking loras_dir_path, which
    contains the on-disk path) instead of the BaseModel projection.
    """
    s._inventory.clear()
    s._inventory[("A", "auto")] = {
        "ref": "A",
        "filename": "a.s",
        "size_bytes": 100,
        "loras_dir_path": "/loras/a.s",
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_0_a",
        "branch": "auto",
    }
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 5000)
    resp = asyncio.run(s.inventory())
    assert len(resp.inventory) == 1
    assert resp.inventory[0].ref == "A"
    assert resp.inventory[0].adapter_name == "lora_0_a"
    assert resp.free_bytes == 5000
    # loras_dir_path must NOT leak — LoraInventoryEntry does not declare it.
    assert not hasattr(resp.inventory[0], "loras_dir_path")


def test_inventory_response_shape_matches_set_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inventory and set_stack surfaces must share the LoraInventoryEntry shape.

    Bug: callers parse both surfaces via the same model; if either drifts,
    the orchestrator's response parsing breaks for half the warm pods.

    Post-8d88e0b the set_stack side is the job record: _run_swap_job dumps
    ``_inventory_snapshot()`` rows into the terminal ``done`` payload (the
    retired SetStackResponse model asserted here previously is deleted).
    """
    s._inventory.clear()
    s._inventory[("A", "auto")] = {
        "ref": "A",
        "filename": "a.s",
        "size_bytes": 1,
        "loras_dir_path": "/loras/a.s",
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_0_a",
        "branch": "auto",
    }
    monkeypatch.setattr(s, "_disk_free_bytes", lambda _: 1)
    inv_resp = asyncio.run(s.inventory())
    # Both surfaces must use the LoraInventoryEntry shape — caller code
    # parses the rows through one model on both paths.
    assert isinstance(inv_resp.inventory[0], s.LoraInventoryEntry)
    snapshot = s._inventory_snapshot()
    assert [type(e) for e in snapshot] == [s.LoraInventoryEntry]
    inv_field_type = s.InventoryResponse.model_fields["inventory"].annotation
    assert inv_field_type == list[s.LoraInventoryEntry]
    # The job record serializes exactly the model's fields (no extra keys
    # like loras_dir_path leaking to the client).
    assert set(snapshot[0].model_dump()) == set(s.LoraInventoryEntry.model_fields)
