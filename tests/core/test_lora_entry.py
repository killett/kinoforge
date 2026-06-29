"""LoraEntry validator tests (test-design skill: every assertion names a
concrete bug shape it would catch)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kinoforge.core.lora import LoraEntry


def test_default_strength_is_1_0() -> None:
    """Bug: a future edit defaults strength to 0.0 → every cfg-driven LoRA
    silently loads at zero weight."""
    e = LoraEntry(ref="civitai:1@2")
    assert e.strength == 1.0


def test_strength_lower_bound_inclusive() -> None:
    """Bug: a future edit changes ge=-2.0 to gt=-2.0 → the exact -2.0
    boundary value is rejected when it should pass."""
    e = LoraEntry(ref="civitai:1@2", strength=-2.0)
    assert e.strength == -2.0


def test_strength_upper_bound_inclusive() -> None:
    e = LoraEntry(ref="civitai:1@2", strength=2.0)
    assert e.strength == 2.0


def test_strength_below_lower_bound_rejected() -> None:
    """Bug: a future edit relaxes ge=-2.0 → a typoed -20 silently loads
    and produces noise output."""
    with pytest.raises(ValidationError) as exc:
        LoraEntry(ref="civitai:1@2", strength=-2.5)
    assert "strength" in str(exc.value)


def test_strength_above_upper_bound_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        LoraEntry(ref="civitai:1@2", strength=2.5)
    assert "strength" in str(exc.value)


def test_extra_field_forbidden() -> None:
    """Bug: a future edit drops extra='forbid' → cfg typos like
    `streng: 1.0` silently load with default strength."""
    with pytest.raises(ValidationError) as exc:
        LoraEntry(ref="civitai:1@2", strength=1.0, banana="yellow")  # type: ignore[call-arg]
    assert "extra" in str(exc.value).lower() or "banana" in str(exc.value)


def test_empty_ref_rejected() -> None:
    with pytest.raises(ValidationError):
        LoraEntry(ref="")


def test_sha256_pattern_accepts_valid_hex() -> None:
    e = LoraEntry(ref="x", sha256="a" * 64)
    assert e.sha256 == "a" * 64


def test_sha256_pattern_rejects_short_string() -> None:
    """Bug: a future edit drops the pattern → corrupted sha256 strings
    (e.g. 32-char MD5 mistakenly pasted) silently load and break integrity
    verification."""
    with pytest.raises(ValidationError):
        LoraEntry(ref="x", sha256="abc")


def test_sha256_accepts_empty_string() -> None:
    """Pattern explicitly allows empty (Pydantic-friendly None-ish)."""
    e = LoraEntry(ref="x", sha256="")
    assert e.sha256 == ""


def test_vault_lora_inherits_strength_and_defaults_to_1_0() -> None:
    """Bug: a future refactor breaks the VaultLoRA(LoraEntry) inheritance
    chain → vault-loaded LoRAs lose strength dimension silently."""
    from kinoforge.core.vault import VaultLoRA

    v = VaultLoRA(ref="civitai:1@2")
    assert v.strength == 1.0
    assert v.label is None


def test_vault_lora_label_field_present() -> None:
    from kinoforge.core.vault import VaultLoRA

    v = VaultLoRA(ref="x", label="my-secret-style")
    assert v.label == "my-secret-style"


def test_vault_lora_strength_obeys_lora_entry_bounds() -> None:
    """Bug: VaultLoRA could shadow/override LoraEntry's Field bounds."""
    from kinoforge.core.vault import VaultLoRA

    with pytest.raises(ValidationError):
        VaultLoRA(ref="x", strength=3.0)


# ---------------------------------------------------------------------------
# URL → canonical normalization via the ref field validator
# (Sub-project A — see docs/superpowers/specs/2026-06-28-lora-url-normalization-design.md)
# ---------------------------------------------------------------------------


def test_LoraEntry_normalizes_civitai_url_with_modelVersionId() -> None:
    entry = LoraEntry(
        ref="https://civitai.com/models/2197303/arcane-style?modelVersionId=2474081",
        strength=0.5,
        branch="high_noise",
    )
    assert entry.ref == "civitai:2197303@2474081"
    assert entry.strength == 0.5
    assert entry.branch == "high_noise"


def test_LoraEntry_normalizes_civarchive_url() -> None:
    entry = LoraEntry(
        ref="https://civarchive.com/models/2197303?modelVersionId=2474081",
    )
    assert entry.ref == "civarchive:2197303@2474081"


def test_LoraEntry_normalizes_hf_blob_url() -> None:
    entry = LoraEntry(
        ref="https://huggingface.co/Org/Repo/blob/main/sub/file.safetensors",
    )
    assert entry.ref == "hf:Org/Repo:sub/file.safetensors"


def test_LoraEntry_rejects_civitai_url_without_modelVersionId() -> None:
    """Bug catch: the validator must surface the ambiguity, AND the
    pydantic ValidationError text must NOT echo the URL (privacy)."""
    with pytest.raises(ValidationError) as excinfo:
        LoraEntry(
            ref="https://civitai.com/models/2197303/arcane-style",
        )
    text = str(excinfo.value)
    assert "civitai URL missing required ?modelVersionId=" in text
    # Privacy invariant — URL text must NOT appear in the ValidationError.
    assert "civitai.com" not in text
    assert "2197303" not in text
    assert "arcane-style" not in text


def test_LoraEntry_canonical_ref_passes_through_unchanged() -> None:
    entry = LoraEntry(ref="civitai:1234@5678")
    assert entry.ref == "civitai:1234@5678"
