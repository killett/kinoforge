"""Auto-register observed LoRA refs with RedactionRegistry.

Pod-side inventory snapshots contain LoRA refs that may have been
loaded by previous sessions (not by the current session's vault).
This helper registers them with the redaction registry so any
log line / json sink that mentions them gets redacted at source.

Called from the matcher after every /lora/inventory + /lora/set_stack
response. Idempotent.
"""

from __future__ import annotations

from typing import Any

from kinoforge.core.redaction import RedactionRegistry


def _extract_inventory(snapshot: Any) -> list[Any]:  # noqa: ANN401 — dual-shape snapshot
    """Return the inventory list from snapshot whether it is a dict or object."""
    if hasattr(snapshot, "inventory"):
        return list(snapshot.inventory or [])
    if isinstance(snapshot, dict):
        return list(snapshot.get("inventory") or [])
    return []


def _extract_ref(entry: Any) -> str | None:  # noqa: ANN401 — dual-shape entry
    """Return the ``ref`` field from an inventory entry, dict or object."""
    if hasattr(entry, "ref"):
        return entry.ref or None
    if isinstance(entry, dict):
        ref = entry.get("ref")
        return ref if isinstance(ref, str) else None
    return None


def _register_observed_lora_refs(snapshot: Any) -> None:  # noqa: ANN401 — dual-shape snapshot
    """Register every observed LoRA ref under the ``lora:ref`` token kind.

    Accepts either a dict (``snapshot["inventory"][i]["ref"]``) or an
    object (``snapshot.inventory[i].ref``). Empty / missing inventory
    is a silent no-op. Idempotent — re-registering the same ref drops
    through ``RedactionRegistry.add``'s built-in dedupe.
    """
    inventory = _extract_inventory(snapshot)
    if not inventory:
        return
    pairs: list[tuple[str, str]] = []
    for entry in inventory:
        ref = _extract_ref(entry)
        # Skip refs shorter than the registry's MIN_TOKEN_LEN — a 1-3 char
        # ref is either fixture data or an upstream malformation; either
        # way it does not need (and cannot get) a redaction placeholder.
        if ref and len(ref) >= 4:
            pairs.append((ref, "lora:ref"))
    if pairs:
        RedactionRegistry.instance().add_many(pairs)
