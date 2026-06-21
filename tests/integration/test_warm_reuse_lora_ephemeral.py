"""Integration: ephemeral session — refs registered, ledger stays in-memory.

Scenario from spec §13.2: ``EphemeralSession(enabled=True)`` is active.
The matcher + integration helper still operate against the live
ledger, observed LoRA refs are auto-registered with
``RedactionRegistry``, the post-swap update lands on the session's
``in_memory_ledger`` — but ``_ledger.json`` never hits the disk
(``policy.ledger_record=False`` gate at ``Ledger._write_entries``).
The pod-side inventory snapshot we send back from the stub is NOT
scrubbed: spec §11.4 says pod-side disk survives ephemeral exits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.interfaces import CapabilityKey, Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.core.redaction import RedactionRegistry
from kinoforge.core.warm_reuse.integration import try_warm_attach_with_swap
from kinoforge.core.warm_reuse.pod_lock import PodLockRegistry
from kinoforge.stores.local import LocalArtifactStore


class _StubCfg:
    def __init__(self, key: CapabilityKey) -> None:
        self._key = key

    def capability_key(self) -> CapabilityKey:
        return self._key


class _RecordingBackend:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def set_lora_stack(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.response


def _entry(ref: str, last_used: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "filename": f"{ref}.s",
        "size_bytes": 1,
        "downloaded_at_local": last_used,
        "last_used_at_local": last_used,
        "adapter_name": "lora_0",
    }


def test_ephemeral_run_registers_refs_and_skips_on_disk_ledger(
    tmp_path: Path,
) -> None:
    """Under EphemeralSession + STRICT_POLICY: ledger writes go in-memory.

    Asserts:
    1. The observed LoRA refs from the stub backend response are tokenised
       by RedactionRegistry (would-fail bug: helper not invoked → refs
       pass through unchanged in any subsequent log line).
    2. ``_ledger.json`` does NOT appear in the artifact store on disk
       (would-fail bug: integration.py wrote via store.put_json or
       Ledger._write_entries lost its policy gate).
    3. The pod-side inventory snapshot we modelled is unchanged by the
       session exit (the spec deliberately leaves pod-side disk alone).
    """
    store = LocalArtifactStore(tmp_path)
    ledger = Ledger(store=store)
    key = CapabilityKey(
        base_model="hf:base-eph",
        loras=("civitai:eph-A@1",),
        engine="diffusers",
        precision="fp16",
    )
    pod = Instance(
        id="pod-eph-1",
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
            base_model="hf:base-eph", loras=(), engine="diffusers", precision="fp16"
        ).derive(),
        lora_inventory=[],
        loras_dir_free_bytes=10_000_000,
        loras_dir_free_bytes_observed_at_local="2026-06-20T10:00:00-07:00",
    )

    pod_disk_inventory = [_entry("civitai:eph-A@1", "2026-06-20T12:00:00-07:00")]
    backend = _RecordingBackend(
        {"inventory": pod_disk_inventory, "free_bytes": 9_999_999}
    )
    registry = PodLockRegistry()
    RedactionRegistry.instance().clear_session()
    specs = {
        "civitai:eph-A@1": {
            "url": "x",
            "headers": {},
            "filename": "A.s",
            "size_hint": 1,
        }
    }

    state_files_before = {p.name for p in tmp_path.rglob("*") if p.is_file()}

    with EphemeralSession(enabled=True):
        match = try_warm_attach_with_swap(
            _StubCfg(key),
            ledger,
            build_backend=lambda _pid: backend,
            pod_lock_registry=registry,
            download_specs=specs,
        )
        assert match is not None
        # Inside the session, ref should already be registered.
        out = RedactionRegistry.instance().redact("hello civitai:eph-A@1 world")
        assert "civitai:eph-A@1" not in out, (
            "observed LoRA ref was not registered with RedactionRegistry "
            "inside the ephemeral session"
        )

    # After exit: no _ledger.json was ever written to the store dir.
    state_files_after = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    new_files = state_files_after - state_files_before
    assert not any(name == "_ledger.json" for name in new_files), (
        f"ephemeral policy.ledger_record=False gate failed — _ledger.json on disk: {new_files}"
    )

    # Pod-side disk model unchanged: we never invoked anything that
    # would scrub it. The stub's reported inventory is still intact.
    assert pod_disk_inventory == [
        _entry("civitai:eph-A@1", "2026-06-20T12:00:00-07:00")
    ]
