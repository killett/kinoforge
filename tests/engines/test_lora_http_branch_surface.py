"""HTTP surface around the P2 branch field.

P2 §6.1 + §6.4 of
docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.

Pins:
  - ``LoraInventoryEntry.branch`` exists + defaults to ``"auto"`` so the
    /lora/inventory wire format always carries the field.
  - ``POST /lora/set_stack`` returns 400 with the structured
    ``branch_routing`` body when ``_replace_adapter_stack`` raises a
    branch-routing exception (auto-on-MoE, explicit-on-single).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import HTTPException

from kinoforge.engines.diffusers.servers import wan_t2v_server


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    original_arity = wan_t2v_server._pipe_arity
    original_inventory = wan_t2v_server._inventory.copy()
    yield
    wan_t2v_server._pipe_arity = original_arity
    wan_t2v_server._inventory.clear()
    wan_t2v_server._inventory.update(original_inventory)


def test_lora_inventory_entry_carries_branch_default_auto() -> None:
    """Bug: client deserialization drops the field because the server
    wire schema doesn't declare it. Default ``"auto"`` keeps pre-P2
    inventory entries round-trippable."""
    assert "branch" in wan_t2v_server.LoraInventoryEntry.model_fields
    entry = wan_t2v_server.LoraInventoryEntry(
        ref="x",
        filename="x.s",
        size_bytes=1,
        downloaded_at_local="x",
        last_used_at_local="x",
        adapter_name="lora_0_a",
    )
    assert entry.branch == "auto"
    assert entry.model_dump()["branch"] == "auto"


def test_lora_inventory_endpoint_returns_branch_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bug: ``/lora/inventory`` serializes ``LoraInventoryEntry`` rows but
    silently drops the branch field if the model loses it."""
    wan_t2v_server._inventory.clear()
    wan_t2v_server._inventory[("civitai:A@1", "high_noise")] = {
        "ref": "civitai:A@1",
        "filename": "a.s",
        "size_bytes": 1,
        "loras_dir_path": str(tmp_path / "a.s"),
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_0_h",
        "last_strength": 1.0,
        "branch": "high_noise",
    }
    monkeypatch.setattr(wan_t2v_server, "_disk_free_bytes", lambda _: 1000)
    resp = asyncio.run(wan_t2v_server.inventory())
    assert len(resp.inventory) == 1
    assert resp.inventory[0].branch == "high_noise"


@pytest.fixture
def set_stack_with_pre_existing_high_noise_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> wan_t2v_server.SetStackRequest:
    """Seed inventory with a high-noise LoRA already loaded so set_stack
    has no download/eviction work — the test isolates the branch
    validation path."""
    monkeypatch.setattr(wan_t2v_server, "LORAS_DIR", tmp_path)
    monkeypatch.setattr(wan_t2v_server, "_disk_free_bytes", lambda _: 1_000_000)

    class _Stub:
        def __init__(self) -> None:
            self.transformer = _Recorder()

        def unload_lora_weights(self) -> None:
            pass

        def load_lora_weights(self, *a: Any, **kw: Any) -> None:
            pass

    class _Recorder:
        def set_adapters(self, *a: Any, **kw: Any) -> None:
            pass

    stub = _Stub()
    monkeypatch.setattr(wan_t2v_server, "pipe", stub)
    # Wan-2.1 shape — single transformer, no transformer_2.
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 1)
    wan_t2v_server._inventory.clear()
    wan_t2v_server._inventory[("civitai:X@1", "auto")] = {
        "ref": "civitai:X@1",
        "filename": "x.s",
        "size_bytes": 1,
        "loras_dir_path": str(tmp_path / "x.s"),
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_0_a",
        "last_strength": 1.0,
        "branch": "auto",
    }
    return wan_t2v_server.SetStackRequest(
        target=[
            wan_t2v_server.LoraTarget(ref="civitai:X@1", branch="high_noise"),
        ],
        download_specs={},
    )


def test_set_stack_returns_400_on_explicit_branch_against_single_transformer(
    set_stack_with_pre_existing_high_noise_target: wan_t2v_server.SetStackRequest,
) -> None:
    """Bug: server collapses to single-transformer silently and returns
    200 with a successful inventory — the operator can't tell their
    Wan 2.2 cfg was loaded into a Wan 2.1 pod."""
    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            wan_t2v_server.set_stack(set_stack_with_pre_existing_high_noise_target)
        )
    assert ei.value.status_code == 400
    detail = cast(dict[str, Any], ei.value.detail)
    assert detail["error"] == "branch_routing"
    assert detail["reason"] == "branch_unsupported_single_transformer"
    assert detail["branch"] == "high_noise"
    assert detail["arity"] == 1


def test_set_stack_returns_400_on_auto_branch_against_moe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bug: server accepts ``branch="auto"`` on Wan 2.2 and routes every
    LoRA into ``transformer`` only, silently half-applying the stack."""
    monkeypatch.setattr(wan_t2v_server, "LORAS_DIR", tmp_path)
    monkeypatch.setattr(wan_t2v_server, "_disk_free_bytes", lambda _: 1_000_000)

    class _MoE:
        def __init__(self) -> None:
            self.transformer = _R()
            self.transformer_2 = _R()

        def unload_lora_weights(self) -> None:
            pass

        def load_lora_weights(self, *a: Any, **kw: Any) -> None:
            pass

    class _R:
        def set_adapters(self, *a: Any, **kw: Any) -> None:
            pass

    monkeypatch.setattr(wan_t2v_server, "pipe", _MoE())
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    wan_t2v_server._inventory.clear()
    wan_t2v_server._inventory[("civitai:Y@1", "auto")] = {
        "ref": "civitai:Y@1",
        "filename": "y.s",
        "size_bytes": 1,
        "loras_dir_path": str(tmp_path / "y.s"),
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_0_a",
        "last_strength": 1.0,
        "branch": "auto",
    }
    req = wan_t2v_server.SetStackRequest(
        target=[wan_t2v_server.LoraTarget(ref="civitai:Y@1", branch="auto")],
        download_specs={},
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(wan_t2v_server.set_stack(req))
    assert ei.value.status_code == 400
    detail = cast(dict[str, Any], ei.value.detail)
    assert detail["error"] == "branch_routing"
    assert detail["reason"] == "branch_auto_disallowed_on_moe"
    assert detail["arity"] == 2
