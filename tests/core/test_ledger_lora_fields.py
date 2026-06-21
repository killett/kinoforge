"""Ledger.touch type widening for LoRA-flexible warm-reuse fields.

Pins:
- touch accepts lora_inventory=list[dict] (the pod's reported inventory).
- touch accepts warm_attach_key=str (the WAK derivation hex).
- touch accepts loras_dir_free_bytes=int (rolled with each swap).
- touch accepts status="healthy" or "degraded" (consumed by reaper).
- All four fields make it through to the on-disk entry verbatim.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.local import LocalArtifactStore


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    """Fresh ledger with one recorded instance."""
    store = LocalArtifactStore(tmp_path)
    led = Ledger(store=store, run_id="r1")
    led.record(
        Instance(
            id="pod-a",
            provider="runpod",
            status="ready",
            created_at=time.time(),
            cost_rate_usd_per_hr=0.0,
            tags={"role": "wan-t2v"},
        )
    )
    return led


def test_touch_accepts_lora_inventory_list_of_dicts(ledger: Ledger) -> None:
    """Bug: passing a list[dict] is rejected because the type hint omits
    list, forcing callers to JSON-serialize the inventory by hand and
    defeating the structured on-disk format."""
    inventory: list[dict[str, Any]] = [
        {"ref": "civitai:A@1", "size_bytes": 1024, "adapter_name": "lora_0"},
        {"ref": "civitai:B@2", "size_bytes": 2048, "adapter_name": "lora_1"},
    ]
    wrote = ledger.touch("pod-a", lora_inventory=inventory)
    assert wrote is True
    entries = ledger.entries()
    pod = next(e for e in entries if e["id"] == "pod-a")
    assert pod["lora_inventory"] == inventory


def test_touch_accepts_warm_attach_key_str(ledger: Ledger) -> None:
    """Bug: WAK hex is rejected as str — already covered by the legacy
    signature, but the regression test pins it so the type widening
    refactor doesn't accidentally drop str."""
    wak = "abc123" * 10
    assert ledger.touch("pod-a", warm_attach_key=wak) is True
    pod = next(e for e in ledger.entries() if e["id"] == "pod-a")
    assert pod["warm_attach_key"] == wak


def test_touch_accepts_loras_dir_free_bytes_int(ledger: Ledger) -> None:
    """Bug: int field is silently coerced to float by the touch body,
    defeating the bytes-precision contract the matcher consults."""
    assert ledger.touch("pod-a", loras_dir_free_bytes=42_949_672_960) is True
    pod = next(e for e in ledger.entries() if e["id"] == "pod-a")
    assert pod["loras_dir_free_bytes"] == 42_949_672_960
    assert isinstance(pod["loras_dir_free_bytes"], int)


def test_touch_accepts_status_string(ledger: Ledger) -> None:
    """Bug: the reaper's status='degraded' write is dropped because str
    is filtered out, leaving the pod un-reapable."""
    assert ledger.touch("pod-a", status="degraded") is True
    pod = next(e for e in ledger.entries() if e["id"] == "pod-a")
    assert pod["status"] == "degraded"


def test_touch_skip_unchanged_inventory(ledger: Ledger) -> None:
    """Bug: list comparison shortcuts to identity instead of value, so
    the skip-unchanged guard never fires for inventory writes and the
    ledger is rewritten on every heartbeat."""
    inv = [{"ref": "civitai:A@1", "size_bytes": 1024}]
    assert ledger.touch("pod-a", lora_inventory=inv) is True
    # Same shape, different list identity: must still skip.
    assert ledger.touch("pod-a", lora_inventory=list(inv)) is False
