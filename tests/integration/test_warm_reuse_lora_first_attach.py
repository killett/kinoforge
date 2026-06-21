"""Integration: first-attach with no matching warm pod → cold-boot path.

Scenario from spec §13.2: ledger is empty for the target WarmAttachKey
so ``try_warm_attach_with_swap`` returns ``None``, the orchestrator
falls through to cold-boot, the backend's ``set_lora_stack`` is NEVER
called, and the ledger gains nothing during the matcher step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kinoforge.core.interfaces import CapabilityKey
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.warm_reuse.integration import try_warm_attach_with_swap
from kinoforge.core.warm_reuse.pod_lock import PodLockRegistry
from kinoforge.stores.local import LocalArtifactStore


class _StubCfg:
    def __init__(self, key: CapabilityKey) -> None:
        self._key = key

    def capability_key(self) -> CapabilityKey:
        return self._key


class _SpyBackend:
    """Fails loud if called — the test expects cold-boot fall-through."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def set_lora_stack(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        self.calls.append(("set_lora_stack", kwargs))
        raise AssertionError("backend.set_lora_stack must not be called on cold-boot")


def test_no_matching_pod_returns_none_and_backend_untouched(tmp_path: Path) -> None:
    """Empty ledger → matcher returns None, backend not touched.

    Bug: matcher accidentally returns a partially-constructed match
    object with ``pod_id=None``, the orchestrator dispatches against
    it, and the operator sees a confusing AttributeError instead of a
    clean cold-boot fall-through.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    key = CapabilityKey(
        base_model="hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers",
        loras=("civitai:2197303@2474081", "civitai:2197303@2474073"),
        engine="diffusers",
        precision="fp16",
    )
    backend = _SpyBackend()
    registry = PodLockRegistry()

    match = try_warm_attach_with_swap(
        _StubCfg(key),
        ledger,
        build_backend=lambda _pod_id: backend,
        pod_lock_registry=registry,
        download_specs={
            ref: {"url": "x", "headers": {}, "filename": f"{ref}.s", "size_hint": 1}
            for ref in key.lora_stack().refs
        },
    )

    assert match is None
    assert backend.calls == []
    assert ledger.entries() == []
