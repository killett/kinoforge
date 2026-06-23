"""``_replace_adapter_stack`` per-transformer routing + pre-load gate.

P2 §3.3 + §5.4 of
docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md.

Asserts the Task-0-LOCKED Approach-1 routing — boolean
``load_into_transformer_2`` kwarg on the diffusers loader, per-transformer
activation loop on the back end.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from kinoforge.engines.diffusers.servers import wan_t2v_server


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Snapshot/restore module globals so the resolver tests stay
    deterministic in the full engine suite."""
    original_arity = wan_t2v_server._pipe_arity
    original_inventory = wan_t2v_server._inventory.copy()
    yield
    wan_t2v_server._pipe_arity = original_arity
    wan_t2v_server._inventory.clear()
    wan_t2v_server._inventory.update(original_inventory)


@pytest.fixture
def moe_pipe(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Wan-2.2-shaped pipe stub with separate transformer + transformer_2."""
    pipe = MagicMock(name="moe_pipe")
    pipe.transformer = MagicMock(name="transformer")
    pipe.transformer_2 = MagicMock(name="transformer_2")
    monkeypatch.setattr(wan_t2v_server, "pipe", pipe)
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    wan_t2v_server._inventory.clear()
    wan_t2v_server._inventory.update(
        {
            ("x", "high_noise"): {
                "ref": "x",
                "loras_dir_path": "/tmp/x_h.safetensors",
                "filename": "x_h.safetensors",
                "size_bytes": 1,
                "downloaded_at_local": "x",
                "last_used_at_local": "x",
                "adapter_name": "",
                "last_strength": 1.0,
                "branch": "high_noise",
            },
            ("y", "low_noise"): {
                "ref": "y",
                "loras_dir_path": "/tmp/y_l.safetensors",
                "filename": "y_l.safetensors",
                "size_bytes": 1,
                "downloaded_at_local": "x",
                "last_used_at_local": "x",
                "adapter_name": "",
                "last_strength": 1.0,
                "branch": "low_noise",
            },
        }
    )
    return pipe


@pytest.fixture
def single_pipe(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Wan-2.1-shaped pipe stub with ONLY ``transformer`` (no ``_2``)."""
    # Use `del` to remove the auto-vivified ``transformer_2`` attr so the
    # single-transformer pipeline shape matches Wan 2.1 (no MoE).
    pipe = MagicMock(name="single_pipe")
    pipe.transformer = MagicMock(name="transformer")
    del pipe.transformer_2
    monkeypatch.setattr(wan_t2v_server, "pipe", pipe)
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 1)
    wan_t2v_server._inventory.clear()
    wan_t2v_server._inventory[("x", "auto")] = {
        "ref": "x",
        "loras_dir_path": "/tmp/x.safetensors",
        "filename": "x.safetensors",
        "size_bytes": 1,
        "downloaded_at_local": "x",
        "last_used_at_local": "x",
        "adapter_name": "",
        "last_strength": 1.0,
        "branch": "auto",
    }
    return pipe


def test_preload_gate_rejects_auto_on_moe_atomically(moe_pipe: MagicMock) -> None:
    """Bug: validation runs INSIDE the load loop, so unload_lora_weights
    already fired before the auto-on-MoE check rejected — the pod ends
    in a half-stripped state. The pre-load gate must walk every entry
    BEFORE any state mutation."""
    target = [wan_t2v_server.LoraTarget(ref="x", branch="auto")]
    with pytest.raises(wan_t2v_server.BranchAutoNotAllowedOnMoE):
        wan_t2v_server._replace_adapter_stack(target)
    assert ("x", "high_noise") in wan_t2v_server._inventory
    moe_pipe.unload_lora_weights.assert_not_called()
    moe_pipe.load_lora_weights.assert_not_called()


def test_preload_gate_rejects_explicit_branch_on_single_transformer(
    single_pipe: MagicMock,
) -> None:
    """Bug: arity-1 + h/l silently collapses to the single transformer
    on a Wan 2.1 pipeline, dropping the explicit-portability contract
    on the floor."""
    target = [wan_t2v_server.LoraTarget(ref="x", branch="high_noise")]
    with pytest.raises(wan_t2v_server.BranchUnsupportedOnSingleTransformer):
        wan_t2v_server._replace_adapter_stack(target)
    single_pipe.unload_lora_weights.assert_not_called()


def test_valid_moe_pair_routes_to_correct_transformers(moe_pipe: MagicMock) -> None:
    """Bug: routing misdirected — high_noise LoRA ships into
    ``transformer_2``. The Task-0-LOCKED dispatch uses the boolean
    ``load_into_transformer_2`` kwarg on ``WanLoraLoaderMixin``."""
    target = [
        wan_t2v_server.LoraTarget(ref="x", strength=1.0, branch="high_noise"),
        wan_t2v_server.LoraTarget(ref="y", strength=0.8, branch="low_noise"),
    ]
    wan_t2v_server._replace_adapter_stack(target)
    assert moe_pipe.load_lora_weights.call_count == 2
    high_call = moe_pipe.load_lora_weights.call_args_list[0]
    low_call = moe_pipe.load_lora_weights.call_args_list[1]
    assert high_call.kwargs["load_into_transformer_2"] is False
    assert low_call.kwargs["load_into_transformer_2"] is True
    # Per-transformer activation (Task 0 Q2) — pipe-level set_adapters
    # is NOT called; each transformer's set_adapters is called once with
    # its own disjoint adapter name list.
    moe_pipe.transformer.set_adapters.assert_called_once()
    moe_pipe.transformer_2.set_adapters.assert_called_once()
    high_names, high_weights = moe_pipe.transformer.set_adapters.call_args.args
    low_names, low_weights = moe_pipe.transformer_2.set_adapters.call_args.args
    assert high_names == ["lora_0_h"]
    assert high_weights == [1.0]
    assert low_names == ["lora_1_l"]
    assert low_weights == [0.8]


def test_partial_failure_mid_load_does_not_corrupt_inventory(
    moe_pipe: MagicMock,
) -> None:
    """Bug: second entry's branch is invalid, but the first entry already
    loaded — inventory + pipe are inconsistent. The pre-load gate must
    validate ALL entries BEFORE any unload, so a rejected request leaves
    EVERY load-side call un-fired."""
    target = [
        wan_t2v_server.LoraTarget(ref="x", strength=1.0, branch="high_noise"),
        wan_t2v_server.LoraTarget(ref="y", strength=0.8, branch="auto"),
    ]
    with pytest.raises(wan_t2v_server.BranchAutoNotAllowedOnMoE):
        wan_t2v_server._replace_adapter_stack(target)
    moe_pipe.unload_lora_weights.assert_not_called()
    moe_pipe.load_lora_weights.assert_not_called()
