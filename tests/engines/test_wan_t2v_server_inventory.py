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
    """Inventory and set_stack responses must share the LoraInventoryEntry shape.

    Bug: callers parse both endpoints via the same model; if either drifts,
    the orchestrator's response parsing breaks for half the warm pods.
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
    # Both responses must use the LoraInventoryEntry shape — caller code
    # parses the rows through one model in both endpoints.
    assert isinstance(inv_resp.inventory[0], s.LoraInventoryEntry)
    set_stack_field_type = s.SetStackResponse.model_fields["inventory"].annotation
    inv_field_type = s.InventoryResponse.model_fields["inventory"].annotation
    assert set_stack_field_type == inv_field_type == list[s.LoraInventoryEntry]
