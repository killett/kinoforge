"""find_warm_attach_candidate ∪ EphemeralIndex — union, dedupe, re-probe.

Existing matcher coverage in tests/core/test_warm_reuse_matcher.py
locks down the ledger-only paths; this file isolates the new
ephemeral_index kwarg's contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex, EphemeralIndexRow
from kinoforge.core.warm_reuse.matcher import find_warm_attach_candidate
from kinoforge.stores.local import LocalArtifactStore


@dataclass
class _FakeLoraStack:
    refs: list[str] = field(default_factory=list)


@dataclass
class _FakeCapKey:
    hex: str
    wak_hex: str
    refs: list[str] = field(default_factory=list)

    def derive(self) -> str:
        return self.hex

    def warm_attach_key(self) -> _FakeCapKey:
        return _FakeCapKey(hex=self.wak_hex, wak_hex=self.wak_hex)

    def lora_stack(self) -> _FakeLoraStack:
        return _FakeLoraStack(refs=self.refs)


@dataclass
class _FakeCfg:
    _cap: _FakeCapKey

    def capability_key(self) -> _FakeCapKey:
        return self._cap


@dataclass
class _FakeLedger:
    _entries: list[dict[str, Any]]

    def find_pods_by_warm_attach_key(self, wak_hex: str) -> list[dict[str, Any]]:
        return [e for e in self._entries if e.get("warm_attach_key") == wak_hex]


class _FakeLockRegistry:
    def __init__(self) -> None:
        self._held: set[str] = set()

    def acquire(self, pod_id: str, *, blocking: bool = False) -> bool:
        if pod_id in self._held:
            return False
        self._held.add(pod_id)
        return True

    def release(self, pod_id: str) -> None:
        self._held.discard(pod_id)

    def __contains__(self, pod_id: str) -> bool:
        return pod_id in self._held


@dataclass
class _FakeSnapshot:
    inventory: list[Any] = field(default_factory=list)
    free_bytes: int = 10**12


def _cfg_with(wak: str = "wak-X", cap: str = "cap-X") -> _FakeCfg:
    return _FakeCfg(_cap=_FakeCapKey(hex=cap, wak_hex=wak))


def test_default_kwarg_preserves_current_behavior(tmp_path: Path) -> None:
    """Bug: adding ephemeral_index changes the no-kwarg path = regression."""
    ledger = _FakeLedger(
        _entries=[
            {
                "id": "pod-from-ledger",
                "warm_attach_key": "wak-X",
                "capability_key_hex": "cap-X",
                "status": "live",
            }
        ]
    )
    match = find_warm_attach_candidate(
        cfg=_cfg_with(),
        ledger=ledger,
        pod_lock_registry=_FakeLockRegistry(),
    )
    assert match is not None
    assert match.pod_id == "pod-from-ledger"


def test_union_includes_index_when_ledger_empty(tmp_path: Path) -> None:
    """Bug: cross-session ephemeral warm-reuse silently broken."""
    store = LocalArtifactStore(tmp_path)
    idx = EphemeralIndex(store=store)
    idx.add(
        EphemeralIndexRow(
            id="pod-from-index",
            warm_attach_key="wak-X",
            kinoforge_key="cap-X",
            endpoints={"8188": "https://pod.example.invalid"},
            provider="runpod",
            created_at_local="2026-06-27T14:18:09",
        )
    )
    ledger = _FakeLedger(_entries=[])

    def fake_probe(pod_id: str) -> _FakeSnapshot:
        return _FakeSnapshot(inventory=[], free_bytes=10**12)

    match = find_warm_attach_candidate(
        cfg=_cfg_with(),
        ledger=ledger,
        pod_lock_registry=_FakeLockRegistry(),
        ephemeral_index=idx,
        re_probe=fake_probe,
    )
    assert match is not None
    assert match.pod_id == "pod-from-index"


def test_ledger_wins_on_id_collision(tmp_path: Path) -> None:
    """Bug: sparse index row clobbers richer ledger entry → matcher loses status."""
    store = LocalArtifactStore(tmp_path)
    idx = EphemeralIndex(store=store)
    idx.add(
        EphemeralIndexRow(
            id="pod-shared",
            warm_attach_key="wak-X",
            kinoforge_key="cap-X",
            endpoints={"8188": "https://pod.example.invalid"},
            provider="runpod",
            created_at_local="2026-06-27T14:18:09",
        )
    )
    # Ledger entry marks the same pod degraded — filter must keep that signal.
    ledger = _FakeLedger(
        _entries=[
            {
                "id": "pod-shared",
                "warm_attach_key": "wak-X",
                "capability_key_hex": "cap-X",
                "status": "degraded",
            }
        ]
    )
    match = find_warm_attach_candidate(
        cfg=_cfg_with(),
        ledger=ledger,
        pod_lock_registry=_FakeLockRegistry(),
        ephemeral_index=idx,
    )
    assert match is None, (
        "ledger entry marked degraded must win over sparse index row; "
        "got a match suggesting the sparse row resurrected a dead pod"
    )


def test_sparse_row_triggers_reprobe(tmp_path: Path) -> None:
    """Bug: matcher attaches to ghost without verifying liveness."""
    store = LocalArtifactStore(tmp_path)
    idx = EphemeralIndex(store=store)
    idx.add(
        EphemeralIndexRow(
            id="pod-X",
            warm_attach_key="wak-X",
            kinoforge_key="cap-X",
            endpoints={"8188": "https://pod.example.invalid"},
            provider="runpod",
            created_at_local="2026-06-27T14:18:09",
        )
    )
    ledger = _FakeLedger(_entries=[])

    probe_calls: list[str] = []

    def tracking_probe(pod_id: str) -> _FakeSnapshot:
        probe_calls.append(pod_id)
        return _FakeSnapshot()

    find_warm_attach_candidate(
        cfg=_cfg_with(),
        ledger=ledger,
        pod_lock_registry=_FakeLockRegistry(),
        ephemeral_index=idx,
        re_probe=tracking_probe,
    )
    assert probe_calls == ["pod-X"], (
        f"expected exactly one re-probe of pod-X; got {probe_calls!r}"
    )
