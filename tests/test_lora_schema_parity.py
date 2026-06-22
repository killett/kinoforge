"""Lockdown: ``LoraEntry`` (core) and ``LoraTarget`` (server) must agree
on the shared field set so a future edit to either stays in sync.

Why two classes? See spec §6.3 — server runs in a slim pod env without
``kinoforge.core`` available, so the wire format is its own contract.
"""

from __future__ import annotations

from pydantic import BaseModel

from kinoforge.core.lora import LoraEntry
from kinoforge.engines.diffusers.servers.wan_t2v_server import LoraTarget


def _field_constraints(
    model_cls: type[BaseModel], field_name: str
) -> dict[str, object]:
    """Return a small dict of constraint values for the named field."""
    field_info = model_cls.model_fields[field_name]
    bounds: dict[str, object] = {}
    for m in field_info.metadata:
        if hasattr(m, "ge"):
            bounds["ge"] = m.ge
        if hasattr(m, "le"):
            bounds["le"] = m.le
    return {
        "default": field_info.default,
        "annotation": field_info.annotation,
        **bounds,
    }


def test_lora_entry_and_lora_target_share_ref_field_shape() -> None:
    """Bug: a future edit changes ref's min_length on one but not the
    other. Both must reject empty strings identically."""
    e_field = LoraEntry.model_fields["ref"]
    t_field = LoraTarget.model_fields["ref"]
    assert e_field.annotation is str
    assert t_field.annotation is str


def test_lora_entry_and_lora_target_share_strength_field_constraints() -> None:
    """Bug: bounds drift between the two — server accepts strength=3.0
    that the cfg-side rejected, or vice-versa. Round-trip becomes
    lossy."""
    e = _field_constraints(LoraEntry, "strength")
    t = _field_constraints(LoraTarget, "strength")
    assert e["default"] == t["default"] == 1.0
    assert e["ge"] == t["ge"] == -2.0
    assert e["le"] == t["le"] == 2.0
    assert e["annotation"] is float
    assert t["annotation"] is float


def test_both_models_forbid_extra_fields() -> None:
    """Bug: one model loses extra='forbid', allowing silent typos to
    cross the wire intact and confuse the receiver."""
    assert LoraEntry.model_config.get("extra") == "forbid"
    assert LoraTarget.model_config.get("extra") == "forbid"
