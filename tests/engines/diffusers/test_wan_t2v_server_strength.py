"""Server-side P1 schema tests: SetStackRequest migration + LoraTarget
bounds.

These tests exercise the Pydantic surface only — they do NOT touch the
HTTP app or import diffusers. The server runs in a slim pod env; test
coverage at the schema level catches contract drift without paying the
diffusers import cost.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from kinoforge.engines.diffusers.servers.wan_t2v_server import (
    LoraTarget,
    SetStackRequest,
)


def test_set_stack_request_accepts_new_shape() -> None:
    """Bug: a future edit reverts target back to target_refs only,
    breaking forward callers."""
    req = SetStackRequest.model_validate(
        {
            "target": [{"ref": "civitai:1@2", "strength": 0.5}],
            "download_specs": {},
        }
    )
    assert len(req.target) == 1
    assert req.target[0].ref == "civitai:1@2"
    assert req.target[0].strength == 0.5


def test_set_stack_request_legacy_target_refs_promotes_strength_1_0() -> None:
    """Bug: legacy callers (orchestrator running an older release) post
    target_refs: [...] — the migrator must accept and assign
    strength=1.0 so warm-pool clients survive the rolling deploy."""
    req = SetStackRequest.model_validate(
        {
            "target_refs": ["civitai:1@2", "hf:org/y:foo.safetensors"],
            "download_specs": {},
        }
    )
    assert [t.ref for t in req.target] == [
        "civitai:1@2",
        "hf:org/y:foo.safetensors",
    ]
    assert all(t.strength == 1.0 for t in req.target)


def test_set_stack_request_rejects_both_keys() -> None:
    """Bug: defense-in-depth — a client carrying BOTH legacy and new
    keys is a programming error; refuse rather than guess intent."""
    with pytest.raises((ValidationError, ValueError)) as exc:
        SetStackRequest.model_validate(
            {
                "target": [{"ref": "civitai:1@2", "strength": 1.0}],
                "target_refs": ["civitai:1@2"],
                "download_specs": {},
            }
        )
    msg = str(exc.value)
    assert "target_refs" in msg and "target" in msg


def test_lora_target_strength_out_of_range_rejected() -> None:
    """Bug: server-side bound enforcement matters even when the client
    validates — defense-in-depth against a tool bypassing the
    kinoforge CLI and posting raw to /lora/set_stack."""
    with pytest.raises(ValidationError) as exc:
        SetStackRequest.model_validate(
            {
                "target": [{"ref": "x", "strength": 3.0}],
                "download_specs": {},
            }
        )
    assert "strength" in str(exc.value)


def test_lora_target_construct_with_defaults() -> None:
    """Bug: LoraTarget loses its strength default → callers building
    targets manually must supply strength = 1.0 every time, easy to
    miss."""
    t = LoraTarget(ref="x")
    assert t.strength == 1.0


# -- Task 4 ------------------------------------------------------------------


def test_set_stack_passes_adapter_weights_to_set_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: a future edit drops the adapter_weights kwarg → every LoRA
    silently loads at strength=1.0 regardless of the request."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    calls: list[dict[str, Any]] = []

    class _FakePipe:
        def __init__(self) -> None:
            self._loaded: list[tuple[str, str]] = []

        def unload_lora_weights(self) -> None:
            self._loaded.clear()

        def load_lora_weights(self, path: str, *, adapter_name: str) -> None:
            self._loaded.append((path, adapter_name))

        def set_adapters(
            self,
            names: list[str],
            adapter_weights: list[float] | None = None,
        ) -> None:
            calls.append({"names": list(names), "weights": list(adapter_weights or [])})

    monkeypatch.setattr(srv, "pipe", _FakePipe())
    monkeypatch.setitem(
        srv._inventory,
        ("civitai:1@2", "auto"),
        {
            "ref": "civitai:1@2",
            "filename": "a.safetensors",
            "size_bytes": 1,
            "loras_dir_path": "/tmp/a",
            "downloaded_at_local": "x",
            "last_used_at_local": "x",
            "adapter_name": "lora_0_a",
            "branch": "auto",
        },
    )
    monkeypatch.setitem(
        srv._inventory,
        ("civitai:3@4", "auto"),
        {
            "ref": "civitai:3@4",
            "filename": "b.safetensors",
            "size_bytes": 1,
            "loras_dir_path": "/tmp/b",
            "downloaded_at_local": "x",
            "last_used_at_local": "x",
            "adapter_name": "lora_1_a",
            "branch": "auto",
        },
    )

    target = [
        srv.LoraTarget(ref="civitai:1@2", strength=0.5),
        srv.LoraTarget(ref="civitai:3@4", strength=1.2),
    ]
    srv._replace_adapter_stack(target)

    assert len(calls) == 1
    assert calls[0]["weights"] == [0.5, 1.2]
    assert calls[0]["names"] == ["lora_0_a", "lora_1_a"]


def test_set_stack_persists_last_strength_on_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: a future edit forgets to write last_strength → matcher's
    same-refs / different-strength path always sees None → constant
    set_stack re-issues even when nothing changed."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    class _NoopPipe:
        def unload_lora_weights(self) -> None:
            pass

        def load_lora_weights(self, *a: Any, **kw: Any) -> None:
            pass

        def set_adapters(self, *a: Any, **kw: Any) -> None:
            pass

    monkeypatch.setattr(srv, "pipe", _NoopPipe())
    monkeypatch.setitem(
        srv._inventory,
        ("civitai:1@2", "auto"),
        {
            "ref": "civitai:1@2",
            "filename": "a.safetensors",
            "size_bytes": 1,
            "loras_dir_path": "/tmp/a",
            "downloaded_at_local": "x",
            "last_used_at_local": "x",
            "adapter_name": "lora_0_a",
            "branch": "auto",
        },
    )
    srv._replace_adapter_stack([srv.LoraTarget(ref="civitai:1@2", strength=0.7)])
    assert srv._inventory[("civitai:1@2", "auto")]["last_strength"] == 0.7


def test_inventory_snapshot_surfaces_last_strength(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: a future edit forgets to expose last_strength on the
    LoraInventoryEntry → /lora/inventory still returns ref+filename
    but drops the strength dimension."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    monkeypatch.setitem(
        srv._inventory,
        ("civitai:1@2", "auto"),
        {
            "ref": "civitai:1@2",
            "filename": "a.safetensors",
            "size_bytes": 1,
            "loras_dir_path": "/tmp/a",
            "downloaded_at_local": "x",
            "last_used_at_local": "x",
            "adapter_name": "lora_0_a",
            "branch": "auto",
            "last_strength": 1.2,
        },
    )
    snap = srv._inventory_snapshot()
    matches = [e for e in snap if e.ref == "civitai:1@2"]
    assert matches
    assert matches[0].last_strength == 1.2


def test_inventory_entry_without_last_strength_renders_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: a pre-P1 inventory entry has no ``last_strength`` key. The
    snapshot MUST default the field to None rather than raise — pods
    rolling P0 → P1 must not crash /lora/inventory."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    monkeypatch.setitem(
        srv._inventory,
        ("civitai:9@9", "auto"),
        {
            "ref": "civitai:9@9",
            "filename": "z.safetensors",
            "size_bytes": 1,
            "loras_dir_path": "/tmp/z",
            "downloaded_at_local": "x",
            "last_used_at_local": "x",
            "adapter_name": "lora_pending_civitai:9@9",
            "branch": "auto",
        },
    )
    snap = srv._inventory_snapshot()
    matches = [e for e in snap if e.ref == "civitai:9@9"]
    assert len(matches) == 1
    assert matches[0].last_strength is None


# -- Task 5 ------------------------------------------------------------------


def test_snapshot_inventory_as_targets_defaults_missing_last_strength_to_1_0(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: a pre-P1 entry with no last_strength field crashes the
    snapshot → server can't roll back at all because it can't capture
    state."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    monkeypatch.setitem(
        srv._inventory,
        ("civitai:9@9", "auto"),
        {
            "ref": "civitai:9@9",
            "filename": "z.safetensors",
            "size_bytes": 1,
            "loras_dir_path": "/tmp/z",
            "downloaded_at_local": "x",
            "last_used_at_local": "x",
            "adapter_name": "lora_pending_9",
            "branch": "auto",
            # NOTE: no last_strength key — pre-P1 entry shape.
        },
    )
    snap = srv._snapshot_inventory_as_targets()
    matches = [t for t in snap if t.ref == "civitai:9@9"]
    assert matches
    assert matches[0].strength == 1.0


def test_snapshot_inventory_as_targets_preserves_strength(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: snapshot reads only the ref → strength is silently lost on
    rollback even when the entry carried last_strength."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    monkeypatch.setitem(
        srv._inventory,
        ("civitai:prev@1", "auto"),
        {
            "ref": "civitai:prev@1",
            "filename": "p.safetensors",
            "size_bytes": 1,
            "loras_dir_path": "/tmp/p",
            "downloaded_at_local": "x",
            "last_used_at_local": "x",
            "adapter_name": "lora_0_a",
            "branch": "auto",
            "last_strength": 0.7,
        },
    )
    snap = srv._snapshot_inventory_as_targets()
    matches = [t for t in snap if t.ref == "civitai:prev@1"]
    assert matches
    assert matches[0].strength == 0.7


def test_value_error_from_set_adapters_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: a future edit catches ValueError silently inside
    _replace_adapter_stack rather than propagating — the handler's
    rollback path never fires because the helper swallows the failure."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    class _AlwaysFailPipe:
        def unload_lora_weights(self) -> None:
            pass

        def load_lora_weights(self, *a: Any, **kw: Any) -> None:
            pass

        def set_adapters(self, *a: Any, **kw: Any) -> None:
            raise ValueError("unknown adapter name lora_0")

    monkeypatch.setattr(srv, "pipe", _AlwaysFailPipe())
    monkeypatch.setitem(
        srv._inventory,
        ("civitai:x@1", "auto"),
        {
            "ref": "civitai:x@1",
            "filename": "x.safetensors",
            "size_bytes": 1,
            "loras_dir_path": "/tmp/x",
            "downloaded_at_local": "x",
            "last_used_at_local": "x",
            "adapter_name": "lora_pending_x",
            "branch": "auto",
        },
    )
    with pytest.raises(ValueError):
        srv._replace_adapter_stack([srv.LoraTarget(ref="civitai:x@1", strength=0.5)])
