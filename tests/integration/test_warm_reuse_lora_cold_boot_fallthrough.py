"""Integration: degraded pod skipped → cold-boot fall-through.

Scenario from spec §13.2: pod exists for the target WarmAttachKey but
its ``status == "degraded"`` (a prior swap failed mid-eviction and the
reaper hasn't run yet). Matcher must NOT pick it; the orchestrator
falls through to cold-boot.
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

    def capability_key(self) -> CapabilityKey:
        return self._key


class _SpyBackend:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def set_lora_stack(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        self.calls.append(kwargs)
        raise AssertionError("backend must not run against a degraded pod")


def test_degraded_pod_is_skipped(tmp_path: Path) -> None:
    """Single degraded pod for this WarmAttachKey → matcher returns None.

    Bug: matcher's filter for status="degraded" uses an identity check
    that admits the value "degraded\\n" or "DEGRADED" from a future
    writer; or worse, treats missing status as degraded and skips every
    healthy pod that pre-dates the field.
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    key = CapabilityKey(
        base_model="hf:base",
        loras=("civitai:A@1",),
        engine="diffusers",
        precision="fp16",
    )
    pod = Instance(
        id="pod-degraded-1",
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
        capability_key_hex=key.derive(),
        status="degraded",
        lora_inventory=[
            {
                "ref": "civitai:A@1",
                "filename": "A.s",
                "size_bytes": 1,
                "downloaded_at_local": "2026-06-20T10:00:00-07:00",
                "last_used_at_local": "2026-06-20T10:00:00-07:00",
                "adapter_name": "lora_0",
            }
        ],
        loras_dir_free_bytes=10_000_000,
        loras_dir_free_bytes_observed_at_local="2026-06-20T10:00:00-07:00",
    )

    backend = _SpyBackend()
    registry = PodLockRegistry()

    match = try_warm_attach_with_swap(
        _StubCfg(key),
        ledger,
        build_backend=lambda _pod_id: backend,
        pod_lock_registry=registry,
        download_specs={
            "civitai:A@1": {
                "url": "x",
                "headers": {},
                "filename": "A.s",
                "size_hint": 1,
            }
        },
    )

    assert match is None
    assert backend.calls == []
