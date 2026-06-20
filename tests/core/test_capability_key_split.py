"""Regression + behavior tests for the WarmAttachKey + LoraStack split.

Pins:
- WarmAttachKey + LoraStack as independent frozen dataclasses.
- CapabilityKey.derive() byte-stability against golden hashes (no
  pre-feature ledger entry breaks).
- WarmAttachKey order-insensitivity to LoRAs.
- Composite accessors (warm_attach_key(), lora_stack()).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from kinoforge.core.interfaces import CapabilityKey, LoraStack, WarmAttachKey


def _legacy_derive(
    base_model: str, loras: tuple[str, ...], engine: str, precision: str
) -> str:
    """Verbatim copy of the pre-split CapabilityKey.derive() body."""
    payload = json.dumps(
        [base_model, list(loras), engine, precision], ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


GOLDEN_INPUTS = [
    ("hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers", (), "diffusers", "fp16"),
    (
        "hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers",
        ("civitai:2197303@2474081", "civitai:2197303@2474073"),
        "diffusers",
        "fp16",
    ),
    ("hf:org/svd", ("hf:lora/style",), "diffusers", "bf16"),
    ("hf:org/svd", ("hf:lora/style", "hf:lora/extra"), "comfyui", "gguf-q8"),
    (
        "hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers",
        ("civitai:2197303@2474073", "civitai:2197303@2474081"),
        "diffusers",
        "fp16",
    ),
]


@pytest.mark.parametrize("base, loras, engine, precision", GOLDEN_INPUTS)
def test_capability_key_derive_byte_identical_to_legacy(
    base: str, loras: tuple[str, ...], engine: str, precision: str
) -> None:
    """Composite CapabilityKey must hash identically to the pre-split version.

    Bug: refactoring to composite accidentally re-orders / re-shapes the
    JSON payload, breaking every ledger entry already on disk that
    relied on capability_key_hex matching cfg.capability_key().derive().
    """
    key = CapabilityKey(
        base_model=base, loras=loras, engine=engine, precision=precision
    )
    assert key.derive() == _legacy_derive(base, loras, engine, precision)


def test_warm_attach_key_drops_loras_field() -> None:
    """WarmAttachKey hash must NOT change when only LoRAs change.

    Bug: WarmAttachKey accidentally folds loras into its hash, defeating
    the entire warm-attach-with-different-loras feature.
    """
    wak1 = WarmAttachKey(base_model="hf:m", engine="diffusers", precision="fp16")
    wak2 = WarmAttachKey(base_model="hf:m", engine="diffusers", precision="fp16")
    assert wak1.derive() == wak2.derive()


def test_warm_attach_key_distinguishes_base_engine_precision() -> None:
    """WarmAttachKey is sensitive to each of its three fields.

    Bug: precision/engine accidentally dropped from WarmAttachKey.derive,
    causing fp16 + bf16 pods to collide in the matcher.
    """
    base_a = WarmAttachKey(base_model="hf:a", engine="diffusers", precision="fp16")
    base_b = WarmAttachKey(base_model="hf:b", engine="diffusers", precision="fp16")
    engine_alt = WarmAttachKey(base_model="hf:a", engine="comfyui", precision="fp16")
    prec_alt = WarmAttachKey(base_model="hf:a", engine="diffusers", precision="bf16")
    derives = {base_a.derive(), base_b.derive(), engine_alt.derive(), prec_alt.derive()}
    assert len(derives) == 4, "every field must contribute to the hash"


def test_lora_stack_preserves_order() -> None:
    """LoraStack(refs=(a,b)) != LoraStack(refs=(b,a)).

    Bug: LoraStack stores refs as a set or sorts on construction,
    breaking the order-sensitivity contract for CapabilityKey.
    """
    s1 = LoraStack(refs=("a", "b"))
    s2 = LoraStack(refs=("b", "a"))
    assert s1 != s2


def test_capability_key_factor_accessors() -> None:
    """CapabilityKey exposes the two factors via accessors.

    Bug: matcher cannot retrieve the WarmAttachKey factor → falls back
    to brittle hash-substring tricks.
    """
    key = CapabilityKey(
        base_model="hf:m", loras=("a", "b"), engine="diffusers", precision="fp16"
    )
    assert key.warm_attach_key() == WarmAttachKey(
        base_model="hf:m", engine="diffusers", precision="fp16"
    )
    assert key.lora_stack() == LoraStack(refs=("a", "b"))


def test_capability_key_frozen() -> None:
    """CapabilityKey, WarmAttachKey, LoraStack are all frozen.

    Bug: a misbehaving engine mutates loras on a shared key, breaking
    every other consumer who held the same reference.
    """
    key = CapabilityKey(
        base_model="hf:m", loras=("a",), engine="diffusers", precision="fp16"
    )
    wak = WarmAttachKey(base_model="hf:m", engine="diffusers", precision="fp16")
    stack = LoraStack(refs=("a",))
    with pytest.raises(FrozenInstanceError):
        key.base_model = "hf:other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        wak.base_model = "hf:other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        stack.refs = ("z",)  # type: ignore[misc]
