"""Ledger.find_pods_by_warm_attach_key — index lookup + lazy backfill."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kinoforge.core.interfaces import Instance
from kinoforge.core.lifecycle import Ledger
from kinoforge.stores.local import LocalArtifactStore


def _ledger_with_pods(tmp_path: Path, pods: list[dict[str, Any]]) -> Ledger:
    store = LocalArtifactStore(root=tmp_path)
    ledger = Ledger(store=store, run_id="test-run")
    for p in pods:
        inst = Instance(
            id=p["id"],
            provider="runpod",
            status="ready",
            tags={},
            created_at=0.0,
            cost_rate_usd_per_hr=1.0,
        )
        ledger.record(inst)
        if "warm_attach_key" in p:
            ledger.touch(p["id"], warm_attach_key=p["warm_attach_key"])
    return ledger


def test_find_pods_by_warm_attach_key_returns_matching(tmp_path: Path) -> None:
    """Filtered lookup returns only entries with the requested wak_hex.

    Bug: helper returns ALL entries instead of filtering by warm_attach_key,
    so the matcher routes work to pods with the wrong base model.
    """
    ledger = _ledger_with_pods(
        tmp_path,
        [
            {"id": "pod-a", "warm_attach_key": "wak-1"},
            {"id": "pod-b", "warm_attach_key": "wak-2"},
            {"id": "pod-c", "warm_attach_key": "wak-1"},
        ],
    )
    result = ledger.find_pods_by_warm_attach_key("wak-1")
    ids = {e["id"] for e in result}
    assert ids == {"pod-a", "pod-c"}


def test_find_pods_by_warm_attach_key_empty_returns_list(tmp_path: Path) -> None:
    """Empty result is [], not None.

    Bug: helper returns None instead of [] for empty result, crashing callers
    that iterate without a None-check.
    """
    ledger = _ledger_with_pods(tmp_path, [])
    assert ledger.find_pods_by_warm_attach_key("wak-1") == []


def test_find_pods_by_warm_attach_key_skips_unrecoverable_pre_feature(
    tmp_path: Path,
) -> None:
    """Pre-feature entry without warm_attach_key is silently skipped.

    Bug: helper crashes on entries that predate the warm_attach_key field,
    breaking the matcher for any ledger that has historical entries.
    """
    ledger = _ledger_with_pods(tmp_path, [{"id": "pod-pre-feature"}])
    result = ledger.find_pods_by_warm_attach_key("wak-1")
    assert result == []


def test_find_pods_by_warm_attach_key_does_not_acquire_mutate_lock(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Read-only path must NOT acquire the cross-process mutate lock.

    Bug: helper accidentally takes acquire_lock, contending with active
    heartbeat touch() calls and deadlocking the matcher under load.
    """
    ledger = _ledger_with_pods(tmp_path, [{"id": "pod-a", "warm_attach_key": "wak-1"}])
    acquire_calls: list[str] = []
    original = ledger._store.acquire_lock

    def _spy(name: str, *a: Any, **k: Any) -> Any:
        acquire_calls.append(name)
        return original(name, *a, **k)

    monkeypatch.setattr(ledger._store, "acquire_lock", _spy)
    ledger.find_pods_by_warm_attach_key("wak-1")
    assert acquire_calls == []
