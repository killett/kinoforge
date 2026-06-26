"""P3 resolver tests — CLI > vault > cfg precedence (spec §11.2)."""

from __future__ import annotations

import logging
import re
from typing import Any
from unittest.mock import patch

import pytest

from kinoforge.core.lora import LoraEntry, resolve_active_lora_stack


class _StubCfg:
    def __init__(self, loras: list[LoraEntry]) -> None:
        self.loras = loras


class _StubVault:
    def __init__(self, loras: list[_StubVaultLoRA]) -> None:
        self.loras = loras


class _StubVaultLoRA:
    """Mirror of VaultLoRA: ref + strength + sha256 + branch + label."""

    def __init__(
        self,
        ref: str,
        strength: float = 1.0,
        branch: str = "auto",
        label: str | None = None,
    ) -> None:
        self.ref = ref
        self.strength = strength
        self.branch = branch
        self.label = label
        self.sha256 = None

    def model_dump(self, exclude: set[str] | None = None) -> dict[str, Any]:
        excl = exclude or set()
        out = {
            "ref": self.ref,
            "strength": self.strength,
            "branch": self.branch,
            "sha256": self.sha256,
            "label": self.label,
        }
        return {k: v for k, v in out.items() if k not in excl}


# --- existing P1 path preserved (cli_loras=None) ---


def test_cli_loras_none_falls_back_to_p1_path() -> None:
    cfg = _StubCfg([LoraEntry(ref="civitai:1@1", strength=0.5)])
    assert resolve_active_lora_stack(cfg, None, cli_loras=None) == cfg.loras


def test_cli_loras_default_is_none_keeps_p1_signature_compat() -> None:
    """P3 signature extension must NOT break callers that pass only 2 args."""
    cfg = _StubCfg([LoraEntry(ref="civitai:1@1")])
    assert resolve_active_lora_stack(cfg, None) == cfg.loras


# --- CLI override ---


def test_cli_loras_overrides_cfg_loras_when_vault_empty() -> None:
    cfg = _StubCfg([LoraEntry(ref="civitai:cfg@1", strength=0.3)])
    cli = [LoraEntry(ref="civitai:cli@1", strength=0.9)]
    result = resolve_active_lora_stack(cfg, None, cli_loras=cli)
    assert result == cli


def test_cli_loras_overrides_vault_loras_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _StubCfg([])
    vault = _StubVault([_StubVaultLoRA("civitai:vault@1")])
    cli = [LoraEntry(ref="civitai:cli@1", strength=0.7)]
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.lora"):
        result = resolve_active_lora_stack(cfg, vault, cli_loras=cli)
    assert result == cli
    assert any("cli-loras-bypass-vault" in rec.message for rec in caplog.records)
    assert any("1 entries" in rec.message for rec in caplog.records)


def test_cli_loras_empty_list_overrides_to_empty_stack() -> None:
    """D9: empty cli_loras (not None) overrides cfg.loras."""
    cfg = _StubCfg([LoraEntry(ref="civitai:cfg@1")])
    result = resolve_active_lora_stack(cfg, None, cli_loras=[])
    assert result == []


def test_cli_loras_skips_p1_d11_conflict_check() -> None:
    """When CLI wins, the diverging-refs LoraStackConflict must NOT fire."""
    cfg = _StubCfg([LoraEntry(ref="civitai:cfg@1")])
    vault = _StubVault([_StubVaultLoRA("civitai:vault@1")])
    cli = [LoraEntry(ref="civitai:cli@1")]
    result = resolve_active_lora_stack(cfg, vault, cli_loras=cli)
    assert result == cli


def test_cli_loras_refs_registered_with_redaction_registry() -> None:
    cli = [LoraEntry(ref="civitai:cli@1"), LoraEntry(ref="civitai:cli@2")]
    with patch("kinoforge.core.lora.RedactionRegistry") as mock_reg_cls:
        mock_inst = mock_reg_cls.instance.return_value
        resolve_active_lora_stack(_StubCfg([]), None, cli_loras=cli)
    mock_inst.add_many.assert_called_once()
    pairs = mock_inst.add_many.call_args[0][0]
    refs = {p[0] for p in pairs}
    assert refs == {"civitai:cli@1", "civitai:cli@2"}


def test_cli_loras_warning_contains_no_ref_strings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _StubCfg([])
    vault = _StubVault([_StubVaultLoRA("civitai:secret-vault@1")])
    cli = [LoraEntry(ref="civitai:secret-cli@1")]
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.lora"):
        resolve_active_lora_stack(cfg, vault, cli_loras=cli)
    combined = " ".join(rec.message for rec in caplog.records)
    assert "secret-vault" not in combined
    assert "secret-cli" not in combined
    assert re.search(r"\bvault\.loras \(\d+ entries\) bypassed", combined)


def test_cli_loras_warning_fires_only_when_vault_nonempty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cli = [LoraEntry(ref="civitai:cli@1")]
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.lora"):
        resolve_active_lora_stack(_StubCfg([]), None, cli_loras=cli)
    assert not any("cli-loras-bypass-vault" in rec.message for rec in caplog.records)


def test_cli_loras_warning_does_not_fire_when_vault_loras_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    vault = _StubVault([])
    cli = [LoraEntry(ref="civitai:cli@1")]
    with caplog.at_level(logging.WARNING, logger="kinoforge.core.lora"):
        resolve_active_lora_stack(_StubCfg([]), vault, cli_loras=cli)
    assert not any("cli-loras-bypass-vault" in rec.message for rec in caplog.records)


def test_cli_loras_redaction_registered_before_warning_emits() -> None:
    """Ordering invariant: refs hit RedactionRegistry BEFORE WARNING fires."""
    calls: list[str] = []
    cli = [LoraEntry(ref="civitai:cli@1")]

    with patch("kinoforge.core.lora.RedactionRegistry") as mock_reg_cls:
        mock_inst = mock_reg_cls.instance.return_value
        mock_inst.add_many.side_effect = lambda _pairs: calls.append("register")
        with patch("kinoforge.core.lora.logger") as mock_logger:
            mock_logger.warning.side_effect = lambda *_a, **_k: calls.append("warn")
            vault = _StubVault([_StubVaultLoRA("civitai:vault@1")])
            resolve_active_lora_stack(_StubCfg([]), vault, cli_loras=cli)

    assert calls == ["register", "warn"]
