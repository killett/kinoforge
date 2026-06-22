"""Server-side P1 schema tests: SetStackRequest migration + LoraTarget
bounds.

These tests exercise the Pydantic surface only — they do NOT touch the
HTTP app or import diffusers. The server runs in a slim pod env; test
coverage at the schema level catches contract drift without paying the
diffusers import cost.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kinoforge.engines.diffusers.servers.wan_t2v_server import (
    LoraTarget,
    SetStackRequest,
)


def test_set_stack_request_accepts_new_shape() -> None:
    """Bug: a future edit reverts target back to target_refs only,
    breaking forward callers."""
    req = SetStackRequest.model_validate(
        {
            "target": [{"ref": "civitai:1@2", "strength": 0.5}],
            "download_specs": {},
        }
    )
    assert len(req.target) == 1
    assert req.target[0].ref == "civitai:1@2"
    assert req.target[0].strength == 0.5


def test_set_stack_request_legacy_target_refs_promotes_strength_1_0() -> None:
    """Bug: legacy callers (orchestrator running an older release) post
    target_refs: [...] — the migrator must accept and assign
    strength=1.0 so warm-pool clients survive the rolling deploy."""
    req = SetStackRequest.model_validate(
        {
            "target_refs": ["civitai:1@2", "hf:org/y:foo.safetensors"],
            "download_specs": {},
        }
    )
    assert [t.ref for t in req.target] == [
        "civitai:1@2",
        "hf:org/y:foo.safetensors",
    ]
    assert all(t.strength == 1.0 for t in req.target)


def test_set_stack_request_rejects_both_keys() -> None:
    """Bug: defense-in-depth — a client carrying BOTH legacy and new
    keys is a programming error; refuse rather than guess intent."""
    with pytest.raises((ValidationError, ValueError)) as exc:
        SetStackRequest.model_validate(
            {
                "target": [{"ref": "civitai:1@2", "strength": 1.0}],
                "target_refs": ["civitai:1@2"],
                "download_specs": {},
            }
        )
    msg = str(exc.value)
    assert "target_refs" in msg and "target" in msg


def test_lora_target_strength_out_of_range_rejected() -> None:
    """Bug: server-side bound enforcement matters even when the client
    validates — defense-in-depth against a tool bypassing the
    kinoforge CLI and posting raw to /lora/set_stack."""
    with pytest.raises(ValidationError) as exc:
        SetStackRequest.model_validate(
            {
                "target": [{"ref": "x", "strength": 3.0}],
                "download_specs": {},
            }
        )
    assert "strength" in str(exc.value)


def test_lora_target_construct_with_defaults() -> None:
    """Bug: LoraTarget loses its strength default → callers building
    targets manually must supply strength = 1.0 every time, easy to
    miss."""
    t = LoraTarget(ref="x")
    assert t.strength == 1.0
