"""Lockdown: ``LoraEntry`` (core) and every server-side ``LoraTarget`` must
agree on the shared field set so a future edit to one stays in sync with all.

Why more than one class? See spec §6.3 — a server runs in a slim pod env
without ``kinoforge.core`` available, so the wire format is its own contract.
There are now THREE copies: core's ``LoraEntry``, the Wan pod's ``LoraTarget``
and the shared router's (``servers/_lora.py``, which both pods will eventually
mount). Three copies is precisely how a schema drifts, so every assertion below
runs against every server copy, not just the first one written.
"""

from __future__ import annotations

from typing import Any, get_args, get_type_hints

import pytest
from pydantic import BaseModel

from kinoforge.core.lora import LoraEntry
from kinoforge.engines.diffusers.servers._lora import LoraTarget as SharedLoraTarget
from kinoforge.engines.diffusers.servers.wan_t2v_server import (
    LoraTarget as WanLoraTarget,
)

# Every pod-side copy of the wire schema. A new server module that defines its
# own copy belongs here the day it is written.
SERVER_TARGETS = [
    pytest.param(WanLoraTarget, id="wan"),
    pytest.param(SharedLoraTarget, id="shared"),
]


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


@pytest.mark.parametrize("target_cls", SERVER_TARGETS)
def test_lora_entry_and_lora_target_share_ref_field_shape(
    target_cls: type[BaseModel],
) -> None:
    """Bug: a future edit changes ref's min_length on one but not the
    other. Both must reject empty strings identically."""
    e_field = LoraEntry.model_fields["ref"]
    t_field = target_cls.model_fields["ref"]
    assert e_field.annotation is str
    assert t_field.annotation is str
    with pytest.raises(ValueError, match="ref"):
        target_cls.model_validate({"ref": ""})


@pytest.mark.parametrize("target_cls", SERVER_TARGETS)
def test_lora_entry_and_lora_target_share_strength_field_constraints(
    target_cls: type[BaseModel],
) -> None:
    """Bug: bounds drift between the two — server accepts strength=3.0
    that the cfg-side rejected, or vice-versa. Round-trip becomes
    lossy."""
    e = _field_constraints(LoraEntry, "strength")
    t = _field_constraints(target_cls, "strength")
    assert e["default"] == t["default"] == 1.0
    assert e["ge"] == t["ge"] == -2.0
    assert e["le"] == t["le"] == 2.0
    assert e["annotation"] is float
    assert t["annotation"] is float


@pytest.mark.parametrize("target_cls", SERVER_TARGETS)
def test_both_models_forbid_extra_fields(target_cls: type[BaseModel]) -> None:
    """Bug: one model loses extra='forbid', allowing silent typos to
    cross the wire intact and confuse the receiver."""
    assert LoraEntry.model_config.get("extra") == "forbid"
    assert target_cls.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("target_cls", SERVER_TARGETS)
def test_branch_field_present_on_both_classes(target_cls: type[BaseModel]) -> None:
    """Bug: a P2-style edit on LoraEntry without a mirror edit on
    LoraTarget. Field-set diverges and the wire schema silently drops
    the branch routing instruction."""
    assert "branch" in LoraEntry.model_fields
    assert "branch" in target_cls.model_fields


@pytest.mark.parametrize("target_cls", SERVER_TARGETS)
def test_target_field_present_on_every_copy(target_cls: type[BaseModel]) -> None:
    """Bug: the generalised routing token reaches one schema only.

    ``target`` is what lets a pod name a workflow partition (H3's
    ``transformer_ref``) that ``branch``'s Literal cannot express. A copy
    without it rejects the very request the shared router exists to serve —
    at 422, on a booked GPU, after the boot is paid for.
    """
    assert "target" in LoraEntry.model_fields
    assert "target" in target_cls.model_fields
    assert target_cls.model_fields["target"].default is None


@pytest.mark.parametrize("target_cls", SERVER_TARGETS)
def test_branch_literal_args_match_exactly(target_cls: type[BaseModel]) -> None:
    """Bug: one class accepts {high_noise, low_noise, auto} while the
    other accepts {h, l, auto} — wire-vs-cfg representation drift makes
    the canonical form ambiguous and the matcher returns false negatives.
    """
    entry_hints = get_type_hints(LoraEntry)
    target_hints = get_type_hints(target_cls)
    assert get_args(entry_hints["branch"]) == get_args(target_hints["branch"])


@pytest.mark.parametrize("target_cls", SERVER_TARGETS)
def test_h_alias_normalizes_identically_on_both(target_cls: Any) -> None:
    """Bug: alias map drifts between modules; cfg accepts ``"H"``
    case-insensitively but wire only accepts ``"h"`` (or similar
    case-sensitivity drift). The two normalizations MUST agree on
    every input the user can write."""
    entry = LoraEntry.model_validate({"ref": "x", "branch": "h"})
    target = target_cls.model_validate({"ref": "x", "branch": "h"})
    assert entry.branch == "high_noise"
    assert target.branch == "high_noise"


@pytest.mark.parametrize("target_cls", SERVER_TARGETS)
def test_l_alias_normalizes_identically_on_both(target_cls: Any) -> None:
    """Same as the h alias, low-noise variant."""
    entry = LoraEntry.model_validate({"ref": "x", "branch": "l"})
    target = target_cls.model_validate({"ref": "x", "branch": "l"})
    assert entry.branch == "low_noise"
    assert target.branch == "low_noise"


@pytest.mark.parametrize("target_cls", SERVER_TARGETS)
def test_branch_default_matches(target_cls: type[BaseModel]) -> None:
    """Bug: one class defaults to ``"auto"``, the other to ``None``
    or some other token. A LoRA entry with no explicit branch then
    parses differently depending on whether it landed in cfg or in the
    wire payload."""
    assert (
        LoraEntry.model_fields["branch"].default
        == target_cls.model_fields["branch"].default
        == "auto"
    )


@pytest.mark.parametrize("target_cls", SERVER_TARGETS)
@pytest.mark.parametrize(
    ("kwargs", "expected_target"),
    [
        ({"branch": "h"}, "high_noise"),
        ({"branch": "l"}, "low_noise"),
        ({"branch": "auto"}, None),
        ({"target": "transformer_ref"}, "transformer_ref"),
        ({}, None),
    ],
)
def test_core_and_server_resolve_target_identically(
    target_cls: Any, kwargs: dict[str, str], expected_target: str | None
) -> None:
    """Both schemas resolve routing identically.

    Catches the alias being taught to one side only, which produces a cfg
    value the pod rejects at 422 after the card is already booked.
    """
    assert LoraEntry(ref="civitai:1@2", **kwargs).target == expected_target  # type: ignore[arg-type]
    assert target_cls(ref="civitai:1@2", **kwargs).target == expected_target


@pytest.mark.parametrize("target_cls", SERVER_TARGETS)
def test_branch_and_target_disagreement_is_refused_everywhere(
    target_cls: type[BaseModel],
) -> None:
    """Bug: one copy resolves a contradiction instead of refusing it.

    ``branch: "h"`` with ``target: "low_noise"`` has no defensible reading.
    A copy that silently picks one loads the LoRA into the wrong partition,
    which never errors and only shows up in the pixels.
    """
    payload = {"ref": "civitai:1@2", "branch": "h", "target": "low_noise"}
    with pytest.raises(ValueError, match="branch and target disagree"):
        LoraEntry.model_validate(payload)
    with pytest.raises(ValueError, match="branch and target disagree"):
        target_cls.model_validate(payload)
