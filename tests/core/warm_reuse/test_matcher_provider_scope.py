"""find_warm_attach_candidate must not cross providers (U1).

``WarmAttachKey`` carries base_model / engine / precision / stages /
upscaler and NO provider, so a Modal cfg and a RunPod pod running the same
model hash to the same key. Every candidate row — ledger or ephemeral-index
— already records the provider it belongs to, and the explicit
``--instance-id`` path has refused a mismatch since D1
(``_resolve_warm_instance`` step 2). These tests hold the automatic matcher
to the same rule.

The strictness is deliberate and matches that precedent: a row that does
not positively declare this cfg's provider is not eligible, because a
missing provider is not evidence of the right one. The one case where no
rule can be applied is a duck-typed cfg with no ``compute`` at all — the
matcher's whole signature is structural, so it must keep working there.
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
class _FakeCompute:
    provider: str


@dataclass
class _FakeCfg:
    """Duck-typed Config. ``compute=None`` models a cfg with no provider."""

    _cap: _FakeCapKey
    compute: _FakeCompute | None = None

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


def _cfg_on(provider: str | None) -> _FakeCfg:
    cap = _FakeCapKey(hex="cap-X", wak_hex="wak-X")
    return _FakeCfg(_cap=cap, compute=_FakeCompute(provider) if provider else None)


def _entry(pod_id: str, provider: str | None) -> dict[str, Any]:
    """A candidate that matches on every axis EXCEPT possibly provider."""
    entry: dict[str, Any] = {
        "id": pod_id,
        "warm_attach_key": "wak-X",
        "capability_key_hex": "cap-X",
        "status": "live",
    }
    if provider is not None:
        entry["provider"] = provider
    return entry


def test_ledger_candidate_on_another_provider_is_not_matched() -> None:
    """U1: a Modal cfg must not select a RunPod pod with the same key.

    Bug caught: the matcher never reads ``provider`` at all, so the two
    hash to the same WarmAttachKey and the RunPod row wins outright. That
    is the T0-07 observation — ``--dry-run-swap`` on a Modal cfg naming a
    RunPod pod — and, once the swap integration is wired to a caller, an
    attach against a pod on another provider entirely.
    """
    ledger = _FakeLedger(_entries=[_entry("runpod-pod-1", "runpod")])

    match = find_warm_attach_candidate(
        cfg=_cfg_on("modal"),
        ledger=ledger,
        pod_lock_registry=_FakeLockRegistry(),
    )

    assert match is None


def test_ledger_candidate_on_the_same_provider_is_still_matched() -> None:
    """The scope narrows to one provider; it does not close entirely.

    Bug caught: a filter comparing the wrong pair — cfg provider against
    the pod id, or against an always-absent key — refuses every candidate
    and turns warm reuse into a permanent cold boot, which costs a full
    cold-boot per run and would read as "no warm pod available".
    """
    ledger = _FakeLedger(_entries=[_entry("modal-pod-1", "modal")])

    match = find_warm_attach_candidate(
        cfg=_cfg_on("modal"),
        ledger=ledger,
        pod_lock_registry=_FakeLockRegistry(),
    )

    assert match is not None
    assert match.pod_id == "modal-pod-1"


def test_index_row_on_another_provider_is_skipped_for_its_sibling(
    tmp_path: Path,
) -> None:
    """The ephemeral-index union is filtered too, not just the ledger.

    Bug caught: filtering only ``ledger.find_pods_by_warm_attach_key``'s
    result and leaving the index rows appended afterwards unfiltered. That
    is where U1 was actually observed — the three stale 2026-07-13 RunPod
    rows that had to be cleared by hand on 2026-09-06 lived in
    ``ephemeral-index.json``, not in the ledger.

    Asserting the Modal sibling is chosen makes the pass specific: a fix
    that drops the whole index union would satisfy "not the RunPod pod"
    and fail here.
    """
    idx = EphemeralIndex(store=LocalArtifactStore(tmp_path))
    for pod_id, provider in (("runpod-pod-1", "runpod"), ("modal-pod-1", "modal")):
        idx.add(
            EphemeralIndexRow(
                id=pod_id,
                warm_attach_key="wak-X",
                kinoforge_key="cap-X",
                endpoints={"8000": f"https://{pod_id}.example.invalid"},
                provider=provider,
                created_at_local="2026-09-10T12:00:00",
            )
        )

    match = find_warm_attach_candidate(
        cfg=_cfg_on("modal"),
        ledger=_FakeLedger(_entries=[]),
        pod_lock_registry=_FakeLockRegistry(),
        ephemeral_index=idx,
        re_probe=lambda pod_id: _FakeSnapshot(),
    )

    assert match is not None
    assert match.pod_id == "modal-pod-1"


def test_candidate_with_no_provider_recorded_is_not_matched() -> None:
    """A row that does not declare a provider is not evidence of this one.

    Bug caught: a permissive filter (``if p and p != want: skip``) that
    waves through rows whose provider is missing — precisely the rows with
    unknown provenance. ``_resolve_warm_instance`` already refuses this
    case on the explicit ``--instance-id`` path by comparing
    ``entry.get("provider", "")``; the automatic path must not be laxer
    than the one the operator drives by hand.
    """
    ledger = _FakeLedger(_entries=[_entry("mystery-pod-1", None)])

    match = find_warm_attach_candidate(
        cfg=_cfg_on("modal"),
        ledger=ledger,
        pod_lock_registry=_FakeLockRegistry(),
    )

    assert match is None


def test_cfg_without_compute_block_still_matches() -> None:
    """No cfg provider means no rule to apply — not "refuse everything".

    Bug caught: reading ``cfg.compute.provider`` unguarded. The matcher's
    signature is structurally typed (``cfg: Any``, "duck-typed Config"),
    and ``Config.compute`` is itself optional, so an unguarded dereference
    raises ``AttributeError`` out of the matcher for every such caller
    instead of returning a decision.
    """
    ledger = _FakeLedger(_entries=[_entry("pod-1", "runpod")])

    match = find_warm_attach_candidate(
        cfg=_cfg_on(None),
        ledger=ledger,
        pod_lock_registry=_FakeLockRegistry(),
    )

    assert match is not None
    assert match.pod_id == "pod-1"
