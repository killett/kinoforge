"""Warm-attach matcher — two-tier lookup + LRU eviction + disk arithmetic.

See ``docs/superpowers/specs/2026-06-20-lora-flexible-warm-reuse-design.md``
§9 for the full algorithm. Inputs: cfg + ledger + PodLockRegistry.
Output: ``WarmAttachMatch | None``.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kinoforge.core.ephemeral import EphemeralSession
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex
from kinoforge.core.warm_reuse.redaction import _register_observed_lora_refs

# Module-level for tests to override (in MB/s) — tunes the cost ranking only.
_BYTES_PER_SECOND_ESTIMATE: int = 100 * 1024 * 1024  # 100 MB/s


@dataclass(frozen=True)
class SwapPlan:
    """Concrete plan the matcher will hand to ``POST /lora/set_stack``."""

    evict: list[str] = field(default_factory=list)
    download: list[str] = field(default_factory=list)
    estimated_cost_seconds: float = 0.0


@dataclass(frozen=True)
class WarmAttachMatch:
    """Result of a successful matcher decision."""

    pod_id: str
    pod_entry: dict[str, Any]
    swap_plan: SwapPlan


def is_stack_match(
    active: list[Any],
    target: list[Any],
) -> bool:
    """Return ``True`` iff the pod's active stack matches the run's target.

    P1 (2026-06-21): equality requires BOTH the ref-order list AND the
    per-LoRA strength to agree. ``math.isclose(rel_tol=1e-6)`` swallows
    JSON round-trip float drift. Pre-P1 inventory entries with
    ``last_strength=None`` are compared as 1.0.

    P2 (2026-06-22): comparison now extends to ``branch`` per entry, so
    the same ``(ref, strength)`` shipped to two different transformer
    branches no longer counts as a match. Pre-P2 inventory entries with
    no ``branch`` attribute are compared as ``"auto"``.

    Args:
        active: Pod's current inventory snapshot — each item must
            expose ``.ref`` (str), ``.last_strength`` (float | None),
            and ``.branch`` (str — pre-P2 entries: defaults to
            ``"auto"``). Duck-typed via ``Any`` because the concrete
            type (``LoraInventoryEntry``) lives in
            ``kinoforge.engines.diffusers.servers.wan_t2v_server`` and
            ``kinoforge.core.*`` is forbidden from importing
            ``kinoforge.engines.*`` per the core-import-ban invariant.
            The ac8 redaction-scan still flags any future module
            taking ``LoraInventoryEntry`` directly as a parameter
            annotation — see ``tests/test_no_unredacted_writes.py``.
        target: Run's resolved LoRA stack — each item must expose
            ``.ref`` (str), ``.strength`` (float), and ``.branch``
            (str). Typically the output of
            :func:`kinoforge.core.lora.resolve_active_lora_stack`.

    Returns:
        ``True`` iff ``(ref, strength, branch)`` tuples match in order,
        with strengths compared via ``math.isclose(rel_tol=1e-6)``.
    """
    if len(active) != len(target):
        return False
    if [a.ref for a in active] != [t.ref for t in target]:
        return False
    if [getattr(a, "branch", "auto") for a in active] != [t.branch for t in target]:
        return False
    return all(
        math.isclose(
            a.last_strength if a.last_strength is not None else 1.0,
            t.strength,
            rel_tol=1e-6,
        )
        for a, t in zip(active, target, strict=True)
    )


def _estimate_seconds(
    download_specs: dict[str, dict[str, Any]], download_refs: list[str]
) -> float:
    total = sum(
        int(download_specs.get(r, {}).get("size_hint", 0) or 0) for r in download_refs
    )
    return total / _BYTES_PER_SECOND_ESTIMATE


def _snapshot_stale(observed_at_local: str | None, threshold_s: float) -> bool:
    if observed_at_local is None:
        return True
    try:
        observed = datetime.fromisoformat(observed_at_local)
        return (datetime.now(observed.tzinfo) - observed).total_seconds() > threshold_s
    except (ValueError, TypeError):
        return True


def find_warm_attach_candidate(
    cfg: Any,  # noqa: ANN401 — duck-typed Config; structural protocol
    ledger: Any,  # noqa: ANN401 — duck-typed Ledger; structural protocol
    *,
    pod_lock_registry: Any,  # noqa: ANN401 — duck-typed PodLockRegistry
    re_probe: Callable[[str], Any] | None = None,
    re_probe_threshold_s: float = 300.0,
    download_specs: dict[str, dict[str, Any]] | None = None,
    ephemeral_index: EphemeralIndex | None = None,
) -> WarmAttachMatch | None:
    """Find the cheapest warm pod to attach for ``cfg``, or ``None`` to cold-boot.

    Args:
        cfg: The Config whose ``capability_key()`` + ``lora_stack()`` drive
            the match.
        ledger: Ledger to query for candidate pods.
        pod_lock_registry: ``PodLockRegistry``; acquired non-blockingly on
            the chosen match. Returned ``None`` when no candidate can be
            locked.
        re_probe: Optional callable ``(pod_id) -> InventorySnapshot`` used
            when the ledger's snapshot of free-disk is stale or under
            ``--ephemeral``. Snapshot may be a dict or pydantic model — the
            helper accepts both shapes.
        re_probe_threshold_s: Seconds before a free-bytes snapshot is
            considered stale and a re-probe is forced.
        download_specs: ``ref -> {size_hint?, url, headers, filename}`` for
            the target LoRA stack. Required for tight-disk + cost
            estimation; may be empty for the exact-byte fast path.
        ephemeral_index: Optional store-backed pod-discovery index. When
            non-None, rows whose WAK matches are unioned into the
            ledger candidate list (ledger wins on ``id`` collision); the
            mechanism the ``--ephemeral`` cross-session warm-reuse path
            relies on to discover pods provisioned by a prior CLI process.

    Returns:
        ``WarmAttachMatch`` with the lock acquired, or ``None``.
    """
    specs = download_specs or {}
    cap_key = cfg.capability_key()
    cap_hex = cap_key.derive()
    wak_hex = cap_key.warm_attach_key().derive()
    new_lora_refs = list(cap_key.lora_stack().refs)

    candidates = ledger.find_pods_by_warm_attach_key(wak_hex)
    if ephemeral_index is not None:
        ledger_ids = {e["id"] for e in candidates}
        for row in ephemeral_index.rows_by_wak(wak_hex):
            if row.id not in ledger_ids:  # ledger wins on overlap
                candidates.append(row.to_entry_dict())

    eligible: list[dict[str, Any]] = []
    for entry in candidates:
        if entry.get("status") == "degraded":
            continue
        if entry["id"] in pod_lock_registry:
            continue
        eligible.append(entry)

    session = EphemeralSession.current()
    always_reprobe = session is not None and not session.policy.ledger_record

    evaluations: list[tuple[WarmAttachMatch, float]] = []
    for entry in eligible:
        pod_id = entry["id"]
        pod_cap_hex = entry.get("capability_key_hex")

        if pod_cap_hex == cap_hex:
            evaluations.append(
                (
                    WarmAttachMatch(
                        pod_id=pod_id,
                        pod_entry=entry,
                        swap_plan=SwapPlan(
                            evict=[], download=[], estimated_cost_seconds=0.0
                        ),
                    ),
                    0.0,
                )
            )
            continue

        observed_at = entry.get("loras_dir_free_bytes_observed_at_local")
        free_bytes = entry.get("loras_dir_free_bytes")
        inventory_entries: list[Any] = list(entry.get("lora_inventory", []) or [])

        needs_reprobe = (
            always_reprobe
            or free_bytes is None
            or _snapshot_stale(observed_at, re_probe_threshold_s)
        )
        if needs_reprobe and re_probe is not None:
            snapshot = re_probe(pod_id)
            _register_observed_lora_refs(snapshot)
            inventory_entries = _coerce_inventory(snapshot)
            free_bytes = _coerce_free_bytes(snapshot)

        inventory_refs = [_entry_field(e, "ref") for e in inventory_entries]
        current_set = {r for r in inventory_refs if r}
        target_set = set(new_lora_refs)
        to_download = [r for r in new_lora_refs if r not in current_set]
        to_evict_candidates = current_set - target_set

        download_bytes = sum(
            int(specs.get(r, {}).get("size_hint", 0) or 0) for r in to_download
        )
        if free_bytes is None:
            free_bytes = 0
        if download_bytes <= free_bytes:
            evict_plan: list[str] = []
        else:
            ordered = sorted(
                (
                    e
                    for e in inventory_entries
                    if _entry_field(e, "ref") in to_evict_candidates
                ),
                key=lambda e: _entry_field(e, "last_used_at_local") or "",
            )
            evict_plan = []
            freed = 0
            need = download_bytes - free_bytes
            for inv_entry in ordered:
                ref = _entry_field(inv_entry, "ref")
                if ref is None:
                    continue
                evict_plan.append(ref)
                size = _entry_field(inv_entry, "size_bytes") or 0
                freed += int(size)
                if freed >= need:
                    break
            if freed < need:
                continue

        cost = _estimate_seconds(specs, to_download)
        evaluations.append(
            (
                WarmAttachMatch(
                    pod_id=pod_id,
                    pod_entry=entry,
                    swap_plan=SwapPlan(
                        evict=evict_plan,
                        download=to_download,
                        estimated_cost_seconds=cost,
                    ),
                ),
                cost,
            )
        )

    evaluations.sort(key=lambda mc: mc[1])
    for match, _cost in evaluations:
        if pod_lock_registry.acquire(match.pod_id, blocking=False):
            return match
    return None


def _coerce_inventory(snapshot: Any) -> list[Any]:  # noqa: ANN401 — dual-shape snapshot
    if hasattr(snapshot, "inventory"):
        return list(snapshot.inventory or [])
    if isinstance(snapshot, dict):
        return list(snapshot.get("inventory") or [])
    return []


def _coerce_free_bytes(snapshot: Any) -> int | None:  # noqa: ANN401 — dual-shape snapshot
    if hasattr(snapshot, "free_bytes"):
        return int(snapshot.free_bytes)
    if isinstance(snapshot, dict):
        fb = snapshot.get("free_bytes")
        return int(fb) if fb is not None else None
    return None


def _entry_field(entry: Any, name: str) -> Any:  # noqa: ANN401 — dual-shape entry
    if hasattr(entry, name):
        return getattr(entry, name)
    if isinstance(entry, dict):
        return entry.get(name)
    return None
