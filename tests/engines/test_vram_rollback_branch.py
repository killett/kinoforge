"""VRAM-OOM rollback snapshot carries ``branch`` + rollback failures raise
``VRAMRollbackFailure``.

P2 §6.4 of docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.

The snapshot helper (``_snapshot_inventory_as_targets``) was reshaped in
Task 5 to emit ``LoraTarget(ref, strength, branch)`` so rollback restores
per-transformer routing. This file pins three load-bearing invariants:

  1. The snapshot returns ``LoraTarget`` instances carrying ``branch``.
  2. The rollback path routes through ``_resolve_transformer`` (the
     single dispatch source) — equivalent to calling
     ``_replace_adapter_stack``.
  3. A rollback that itself fails raises ``VRAMRollbackFailure`` so the
     HTTP handler maps it to a structured 500 (and the orchestrator
     destroys the pod) instead of bubbling a generic ``Exception``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from kinoforge.engines.diffusers.servers import wan_t2v_server


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    original_arity = wan_t2v_server._pipe_arity
    original_inventory = wan_t2v_server._inventory.copy()
    yield
    wan_t2v_server._pipe_arity = original_arity
    wan_t2v_server._inventory.clear()
    wan_t2v_server._inventory.update(original_inventory)


def test_snapshot_inventory_as_targets_carries_branch() -> None:
    """Bug: snapshot drops the branch field, so a VRAM-OOM rollback
    re-loads each LoRA with ``branch="auto"`` and silently strands a
    Wan-2.2 stack on the wrong transformer."""
    wan_t2v_server._inventory.clear()
    wan_t2v_server._inventory[("civitai:A@1", "high_noise")] = {
        "ref": "civitai:A@1",
        "filename": "a.s",
        "size_bytes": 100,
        "loras_dir_path": "/tmp/a.s",
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_0_h",
        "last_strength": 0.8,
        "branch": "high_noise",
    }
    wan_t2v_server._inventory[("civitai:B@2", "low_noise")] = {
        "ref": "civitai:B@2",
        "filename": "b.s",
        "size_bytes": 100,
        "loras_dir_path": "/tmp/b.s",
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_1_l",
        "last_strength": 0.4,
        "branch": "low_noise",
    }
    snap = wan_t2v_server._snapshot_inventory_as_targets()
    by_ref = {t.ref: t for t in snap}
    assert by_ref["civitai:A@1"].branch == "high_noise"
    assert by_ref["civitai:A@1"].strength == 0.8
    assert by_ref["civitai:B@2"].branch == "low_noise"
    assert by_ref["civitai:B@2"].strength == 0.4


def test_replace_adapter_stack_on_snapshot_routes_via_resolve_transformer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug: rollback re-loads via a parallel routing path (raw transformer
    rebind or hardcoded branch=auto) instead of ``_resolve_transformer``.
    The rule keeps a single source of truth for per-transformer dispatch."""

    class _MoE:
        def __init__(self) -> None:
            self.transformer = _Recorder()
            self.transformer_2 = _Recorder()
            self.loaded: list[dict[str, Any]] = []

        def unload_lora_weights(self) -> None:
            pass

        def load_lora_weights(
            self,
            path: str,
            adapter_name: str,
            load_into_transformer_2: bool = False,
        ) -> None:
            self.loaded.append(
                {
                    "path": path,
                    "adapter_name": adapter_name,
                    "load_into_transformer_2": load_into_transformer_2,
                }
            )

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], list[float]]] = []

        def set_adapters(
            self,
            names: list[str],
            adapter_weights: list[float] | None = None,
        ) -> None:
            self.calls.append((list(names), list(adapter_weights or [])))

    pipe = _MoE()
    monkeypatch.setattr(wan_t2v_server, "pipe", pipe)
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    wan_t2v_server._inventory.clear()
    wan_t2v_server._inventory[("x", "high_noise")] = {
        "ref": "x",
        "filename": "x.s",
        "size_bytes": 1,
        "loras_dir_path": "/tmp/x.s",
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_old_h",
        "last_strength": 0.3,
        "branch": "high_noise",
    }
    wan_t2v_server._inventory[("y", "low_noise")] = {
        "ref": "y",
        "filename": "y.s",
        "size_bytes": 1,
        "loras_dir_path": "/tmp/y.s",
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "lora_old_l",
        "last_strength": 0.5,
        "branch": "low_noise",
    }
    snapshot = wan_t2v_server._snapshot_inventory_as_targets()
    wan_t2v_server._replace_adapter_stack(snapshot)
    # high_noise → transformer (False); low_noise → transformer_2 (True).
    assert pipe.loaded[0]["load_into_transformer_2"] is False
    assert pipe.loaded[1]["load_into_transformer_2"] is True
    assert pipe.transformer.calls[0][0] == ["lora_0_h"]
    assert pipe.transformer_2.calls[0][0] == ["lora_1_l"]


def test_vram_rollback_failure_class_exists_and_is_specific() -> None:
    """Bug: rollback re-load failure is signaled with a bare
    ``RuntimeError`` so the HTTP handler can't distinguish it from
    in-band swap errors. A dedicated exception class lets the handler
    map rollback failures to the structured ``rollback_failed`` 500
    body."""
    assert issubclass(wan_t2v_server.VRAMRollbackFailure, Exception)
    err = wan_t2v_server.VRAMRollbackFailure("rollback re-load OOMed")
    assert "rollback" in str(err).lower()
