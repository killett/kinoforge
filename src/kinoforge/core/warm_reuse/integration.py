"""Orchestrator-facing warm-attach helper.

Wraps :func:`find_warm_attach_candidate` with the full error-handling
contract from spec §11.2:

- Acquire pod lock BEFORE the swap call (the matcher does this on its
  way out, so the wrapper only needs to release it on every failure
  path).
- Issue ``backend.set_lora_stack`` with the matcher's swap plan.
- On success, ``Ledger.touch`` the pod with the post-swap inventory
  snapshot + free disk + observation timestamp.
- On every ``LoraSwapError`` subclass, route the per-class side effect
  (mark degraded vs leave-healthy) + release the pod lock + re-raise.

The wrapper is intentionally callable in isolation so the CLI / deep
orchestrator can adopt it incrementally without touching
``deploy_session`` end-to-end. ``deploy_session`` integration is a
follow-up (depends on a CLI-level "attach to existing pod" path that
sidesteps cold-boot provisioning).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from kinoforge.core.errors import (
    LoraSwapDegradedPodError,
    LoraSwapDiskFullError,
    LoraSwapDownloadError,
    LoraSwapPodUnreachableError,
    LoraSwapVramOomError,
)
from kinoforge.core.warm_reuse.ephemeral_index import EphemeralIndex
from kinoforge.core.warm_reuse.matcher import (
    WarmAttachMatch,
    find_warm_attach_candidate,
)
from kinoforge.core.warm_reuse.redaction import _register_observed_lora_refs


def try_warm_attach_with_swap(
    cfg: Any,  # noqa: ANN401 — duck-typed Config
    ledger: Any,  # noqa: ANN401 — duck-typed Ledger
    build_backend: Callable[[str], Any],
    *,
    pod_lock_registry: Any,  # noqa: ANN401 — duck-typed PodLockRegistry
    download_specs: dict[str, dict[str, Any]] | None = None,
    re_probe: Callable[[str], Any] | None = None,
    re_probe_threshold_s: float = 300.0,
    ephemeral_index: EphemeralIndex | None = None,
) -> WarmAttachMatch | None:
    """Find a warm pod + apply the LoRA swap; return the locked match or None.

    On a successful return the caller is the owner of
    ``pod_lock_registry[match.pod_id]`` and MUST release it in a
    ``finally`` block once it has finished using the pod.

    Args:
        cfg: Config providing ``capability_key()``.
        ledger: Ledger used for both candidate lookup and post-swap update.
        build_backend: ``pod_id -> DiffusersBackend`` factory; used to
            issue the actual ``set_lora_stack`` call.
        pod_lock_registry: ``PodLockRegistry``; the matcher acquires
            non-blockingly on success.
        download_specs: ``ref -> {url, headers, filename, size_hint?}`` for
            every LoRA in the target stack; required for any path that
            needs to download new refs.
        re_probe: Optional ``pod_id -> InventorySnapshot`` re-probe.
        re_probe_threshold_s: Forwarded to the matcher.
        ephemeral_index: Optional pod-discovery index; forwarded to
            :func:`find_warm_attach_candidate` so the matcher can union
            ``--ephemeral``-session rows into the candidate list.

    Returns:
        Locked ``WarmAttachMatch`` on success, ``None`` when no candidate
        was viable (caller falls through to cold-boot).

    Raises:
        LoraSwapDegradedPodError: Pod was in half-state after partial
            eviction; ledger marked degraded; lock released.
        LoraSwapPodUnreachableError: Pod proxy past retry budget; ledger
            marked degraded; lock released.
        LoraSwapDiskFullError: Pod ran out of disk mid-swap; ledger
            marked degraded; lock released.
        LoraSwapDownloadError: Clean download failure with no inventory
            change; ledger NOT marked degraded (pod healthy); lock
            released.
        LoraSwapVramOomError: VRAM OOM rolled back to prior adapter set;
            ledger NOT marked degraded (pod healthy); lock released.
    """
    specs = download_specs or {}
    match = find_warm_attach_candidate(
        cfg,
        ledger,
        pod_lock_registry=pod_lock_registry,
        re_probe=re_probe,
        re_probe_threshold_s=re_probe_threshold_s,
        download_specs=specs,
        ephemeral_index=ephemeral_index,
    )
    if match is None:
        return None

    backend = build_backend(match.pod_id)
    plan = match.swap_plan
    swap_needed = bool(plan.evict or plan.download)
    try:
        if swap_needed:
            # P1 (2026-06-21): resolve cfg.loras vs vault.loras precedence
            # so vault-owned LoRAs (with their strengths) drive the wire
            # payload — same precedence rules as the cold-boot path.
            from kinoforge.core.ephemeral import EphemeralSession
            from kinoforge.core.lora import resolve_active_lora_stack

            _session = EphemeralSession.current()
            _vault = _session.vault if _session is not None else None
            _cli_loras = getattr(_session, "cli_loras", None) if _session else None
            active_stack = resolve_active_lora_stack(cfg, _vault, cli_loras=_cli_loras)
            resp = backend.set_lora_stack(
                pod_id=match.pod_id,
                active_stack=active_stack,
                download_specs={ref: specs[ref] for ref in plan.download},
            )
            inventory = resp.get("inventory", []) if isinstance(resp, dict) else []
            _register_observed_lora_refs({"inventory": inventory})
            inventory_dicts = [_entry_to_dict(e) for e in inventory]
            free_bytes = resp.get("free_bytes") if isinstance(resp, dict) else None
            ledger.touch(
                match.pod_id,
                lora_inventory=inventory_dicts,
                loras_dir_free_bytes=int(free_bytes) if free_bytes is not None else 0,
                loras_dir_free_bytes_observed_at_local=datetime.now().isoformat(),
            )
        return match
    except (
        LoraSwapDegradedPodError,
        LoraSwapPodUnreachableError,
        LoraSwapDiskFullError,
    ):
        ledger.touch(match.pod_id, status="degraded")
        pod_lock_registry.release(match.pod_id)
        raise
    except (LoraSwapDownloadError, LoraSwapVramOomError):
        pod_lock_registry.release(match.pod_id)
        raise
    except Exception:
        pod_lock_registry.release(match.pod_id)
        raise


def _entry_to_dict(entry: Any) -> dict[str, Any]:  # noqa: ANN401 — dual-shape entry
    if isinstance(entry, dict):
        return dict(entry)
    if hasattr(entry, "model_dump"):
        return dict(entry.model_dump())
    if hasattr(entry, "dict"):
        return dict(entry.dict())
    return {}
