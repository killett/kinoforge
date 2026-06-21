"""_FaithfulStubPipe contract."""

from __future__ import annotations

import pytest

from tests.smoke.local_cpu.stub_pipe import (
    _FaithfulStubPipe,
    _stub_diffusers_load,
)


def test_load_lora_weights_appends() -> None:
    """Bug: append silently dropped → adapter list always empty."""
    p = _FaithfulStubPipe()
    p.load_lora_weights("/x/a.s", adapter_name="lora_0")
    p.load_lora_weights("/x/b.s", adapter_name="lora_1")
    assert [n for n, _ in p._loaded_adapters] == ["lora_0", "lora_1"]


def test_set_adapters_under_budget_updates_active() -> None:
    """Bug: set_adapters never updates _active → server can't tell
    which adapters are live."""
    p = _FaithfulStubPipe()
    p._vram_budget_mb = 100_000
    p.load_lora_weights("/x/a", adapter_name="a")
    p.set_adapters(["a"])
    assert p._active == ["a"]


def test_set_adapters_over_budget_raises_cuda_oom() -> None:
    """Bug: stub raises wrong exception/substring → server's VramOom
    mapping (T8) doesn't recognise it."""
    p = _FaithfulStubPipe()
    p._vram_budget_mb = 1
    p.load_lora_weights("/x/big", adapter_name="big")
    with pytest.raises(RuntimeError, match="CUDA out of memory"):
        p.set_adapters(["big"])


def test_unload_clears_state() -> None:
    p = _FaithfulStubPipe()
    p.load_lora_weights("/x/a", adapter_name="a")
    p.set_adapters(["a"])
    p.unload_lora_weights()
    assert p._loaded_adapters == []
    assert p._active == []


def test_delete_adapters_removes_named() -> None:
    p = _FaithfulStubPipe()
    p.load_lora_weights("/x/a", adapter_name="a")
    p.load_lora_weights("/x/b", adapter_name="b")
    p.delete_adapters(["a"])
    assert [n for n, _ in p._loaded_adapters] == ["b"]


def test_stub_factory_returns_fresh_instance() -> None:
    """Bug: factory returns a process-wide singleton → state leaks."""
    p1 = _stub_diffusers_load()
    p2 = _stub_diffusers_load()
    assert p1 is not p2
    assert isinstance(p1, _FaithfulStubPipe)
