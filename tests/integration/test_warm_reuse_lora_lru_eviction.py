"""Integration: tight-disk forces LRU eviction before download.

Scenario from spec §13.2: pod has [A, B, C] cached; target stack is
[D]; free disk is less than D's size_hint. Matcher selects the
LRU-oldest evictable refs in order until enough disk is freed. Backend
gets called with target=[D] and the matcher's evict plan should appear
on disk after the swap (real pod would honor it; here we simulate).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kinoforge.core.interfaces import CapabilityKey, Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.warm_reuse.integration import try_warm_attach_with_swap
from kinoforge.core.warm_reuse.pod_lock import PodLockRegistry
from kinoforge.stores.local import LocalArtifactStore


class _StubCfg:
    def __init__(self, key: CapabilityKey) -> None:
        self._key = key
        # P1 (2026-06-21): integration.py reads cfg.loras for the active
        # stack; mirror the capability_key's refs into cfg.loras so the
        # stub stays consistent.
        from kinoforge.core.lora import LoraEntry

        self.loras = [LoraEntry(ref=r) for r in key.loras]

    def capability_key(self) -> CapabilityKey:
        return self._key


class _RecordingBackend:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def set_lora_stack(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _entry(ref: str, last_used: str, size: int) -> dict[str, Any]:
    return {
        "ref": ref,
        "filename": f"{ref}.s",
        "size_bytes": size,
        "downloaded_at_local": last_used,
        "last_used_at_local": last_used,
        "adapter_name": f"adapter-{ref}",
    }


def test_tight_disk_picks_lru_oldest_first(tmp_path: Path) -> None:
    """Pod full; target [D] needs 800B; A=500B oldest, B=500B mid, C=500B newest.

    Bug: matcher selects by largest-size-first (or newest-first),
    evicting C — the operator's most-recent LoRA — instead of the LRU
    A. The user re-pays the download cost on every subsequent swap
    that targets C, defeating warm-reuse for the hot ref.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    key = CapabilityKey(
        base_model="hf:base",
        loras=("civitai:D@4",),
        engine="diffusers",
        precision="fp16",
    )
    pod = Instance(
        id="pod-tight-1",
        provider="local",
        status="ready",
        created_at=0.0,
        cost_rate_usd_per_hr=0.0,
        tags={},
    )
    ledger.record(pod)
    ledger.touch(
        pod.id,
        warm_attach_key=key.warm_attach_key().derive(),
        capability_key_hex=CapabilityKey(
            base_model="hf:base",
            loras=("civitai:A@1", "civitai:B@2", "civitai:C@3"),
            engine="diffusers",
            precision="fp16",
        ).derive(),
        lora_inventory=[
            _entry("civitai:A@1", "2026-06-20T09:00:00-07:00", 500),  # oldest
            _entry("civitai:B@2", "2026-06-20T10:00:00-07:00", 500),
            _entry("civitai:C@3", "2026-06-20T11:00:00-07:00", 500),  # newest
        ],
        loras_dir_free_bytes=100,  # tight
        loras_dir_free_bytes_observed_at_local="2026-06-20T11:00:00-07:00",
    )

    backend = _RecordingBackend(
        {
            "inventory": [
                _entry("civitai:B@2", "2026-06-20T10:00:00-07:00", 500),
                _entry("civitai:C@3", "2026-06-20T11:00:00-07:00", 500),
                _entry("civitai:D@4", "2026-06-20T12:00:00-07:00", 800),
            ],
            "free_bytes": 200,
        }
    )
    registry = PodLockRegistry()
    specs = {
        "civitai:D@4": {"url": "x", "headers": {}, "filename": "D.s", "size_hint": 800}
    }

    match = try_warm_attach_with_swap(
        _StubCfg(key),
        ledger,
        build_backend=lambda pod_id: backend,
        pod_lock_registry=registry,
        download_specs=specs,
    )

    assert match is not None
    # Need = 800 - 100 = 700 bytes; evict A (500) then B (500) = 1000 freed.
    assert match.swap_plan.evict == ["civitai:A@1", "civitai:B@2"]
    assert match.swap_plan.download == ["civitai:D@4"]

    assert [e.ref for e in backend.calls[0]["active_stack"]] == ["civitai:D@4"]
    assert list(backend.calls[0]["download_specs"].keys()) == ["civitai:D@4"]
