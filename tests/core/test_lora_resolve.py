"""``resolve_active_lora_stack`` — cfg.loras vs vault.loras precedence."""

from __future__ import annotations

from typing import Any

import pytest

from kinoforge.core.errors import LoraStackConflict
from kinoforge.core.lora import LoraEntry, resolve_active_lora_stack
from kinoforge.core.vault import Vault, VaultLoRA


def _vault_with_loras(loras: list[VaultLoRA]) -> Vault:
    return Vault.model_validate(
        {
            "positive_prompt": "x",
            "loras": [lo.model_dump() for lo in loras],
        }
    )


class _StubCfg:
    """Minimal stand-in for ``Config`` carrying only the ``.loras`` attribute."""

    def __init__(self, loras: list[LoraEntry]) -> None:
        self.loras = loras


def test_no_vault_returns_cfg_loras() -> None:
    """Bug: a future edit makes vault=None silently empty the stack →
    every public-by-design cfg loses its LoRAs."""
    cfg = _StubCfg([LoraEntry(ref="civitai:1@2", strength=0.5)])
    result = resolve_active_lora_stack(cfg, None)
    assert len(result) == 1
    assert result[0].ref == "civitai:1@2"
    assert result[0].strength == 0.5


def test_vault_loras_win_over_cfg_loras_when_refs_match() -> None:
    """Bug: cfg.loras silently merged with vault.loras → user's
    public-by-design cfg leaks into the private resolution."""
    cfg = _StubCfg([LoraEntry(ref="civitai:1@2", strength=1.0)])
    vault = _vault_with_loras(
        [
            VaultLoRA(ref="civitai:1@2", strength=0.5, label="secret-style"),
        ]
    )
    result = resolve_active_lora_stack(cfg, vault)
    assert len(result) == 1
    assert result[0].ref == "civitai:1@2"
    assert result[0].strength == 0.5


def test_vault_label_stripped_on_upcast() -> None:
    """Bug: VaultLoRA's vault-only ``label`` leaks into the LoraEntry
    list sent to the orchestrator → label appears in the HTTP set_stack
    body in violation of ephemeral spec D4."""
    cfg = _StubCfg([])
    vault = _vault_with_loras(
        [
            VaultLoRA(ref="civitai:1@2", strength=0.5, label="my-secret-style"),
        ]
    )
    result = resolve_active_lora_stack(cfg, vault)
    # Output items must be plain LoraEntry (no `label` field exists on the
    # base class — VaultLoRA's vault-only label is stripped on upcast).
    assert type(result[0]) is LoraEntry
    assert "label" not in LoraEntry.model_fields


def test_diverging_cfg_vault_ref_sets_raises_lora_stack_conflict() -> None:
    cfg = _StubCfg([LoraEntry(ref="civitai:1@2")])
    vault = _vault_with_loras([VaultLoRA(ref="civitai:99@100")])
    with pytest.raises(LoraStackConflict) as exc:
        resolve_active_lora_stack(cfg, vault)
    assert "diverging" in str(exc.value)


def test_empty_vault_loras_falls_through_to_cfg() -> None:
    """Bug: vault loaded but with no loras should NOT block cfg.loras —
    vault's loras list is optional per the vault spec."""
    cfg = _StubCfg([LoraEntry(ref="civitai:1@2")])
    vault = _vault_with_loras([])
    result = resolve_active_lora_stack(cfg, vault)
    assert len(result) == 1
    assert result[0].ref == "civitai:1@2"


def test_error_subclasses_are_kinoforge_error() -> None:
    """Bug: someone changes the base class to plain Exception →
    KinoforgeError-catching handlers stop catching these."""
    from kinoforge.core.errors import (
        KinoforgeError,
        LoraStackConflict,
        SetStackRequestRejected,
    )

    _: Any = None
    assert issubclass(LoraStackConflict, KinoforgeError)
    assert issubclass(SetStackRequestRejected, KinoforgeError)
