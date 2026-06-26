"""Shared per-LoRA Pydantic schema for cfg + vault.

``LoraEntry`` is the canonical class used by both public cfg ``loras:``
blocks and vault ``loras:`` lists. ``VaultLoRA(LoraEntry)`` extends it
with a vault-internal ``label``. Future fields (P2 branch,
trigger_word, sampler_hints) land here once.

Privacy classification (P1):
  - ``ref``     — SENSITIVE per ephemeral spec D4.
  - ``strength`` — NON-SENSITIVE (low-entropy float; same posture as seed).
  - ``sha256``  — derived hash; per D4 derived hashes are sensitive.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from kinoforge.core.redaction import RedactionRegistry

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class LoraEntry(BaseModel):
    """One LoRA entry: ref + strength + optional sha256 + branch.

    See docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §6.1
    and docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md §2.

    Attributes:
        ref: Vendor-neutral model reference (e.g. ``"civitai:1234@5678"``
            or ``"hf:Org/Repo:filename"``). SENSITIVE under vault mode.
        strength: PEFT adapter weight applied via
            ``set_adapters(adapter_weights=...)``. Range hard-bounded
            to ``[-2.0, 2.0]`` (industry-standard a1111 LoRA range).
            Default 1.0. NON-SENSITIVE — same posture as ``seed`` /
            ``num_inference_steps``.
        sha256: Optional content hash for integrity verification.
            64-char lowercase hex OR empty string. Derived hash is
            sensitive per ephemeral spec D4.
        branch: Per-LoRA routing instruction for multi-transformer
            pipelines (Wan 2.2 high-noise/low-noise MoE). Canonical
            values: ``"high_noise"`` / ``"low_noise"`` / ``"auto"``.
            Accepts shortcuts ``"h"`` / ``"l"`` normalized at validation
            time so storage + wire share the canonical token. ``"auto"``
            is single-transformer-only; MoE pipelines reject ``"auto"``
            and require explicit branch (see server-side
            ``_resolve_transformer`` for the dispatch). NON-SENSITIVE
            (low-entropy enum; same posture as ``strength``).
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$|^$")
    branch: Literal["high_noise", "low_noise", "auto"] = Field(default="auto")

    @field_validator("branch", mode="before")
    @classmethod
    def _normalize_branch_alias(cls, v: Any) -> Any:  # noqa: ANN401
        """Normalize ``h`` / ``l`` shortcuts to canonical form.

        Runs ``mode="before"`` so the Literal constraint sees the
        canonical token. Mirror of ``LoraTarget._normalize_branch_alias``
        in ``kinoforge.engines.diffusers.servers.wan_t2v_server`` — parity
        is locked by ``tests/test_lora_schema_parity.py``. DO NOT diverge.
        """
        if v == "h":
            return "high_noise"
        if v == "l":
            return "low_noise"
        return v


def resolve_active_lora_stack(
    cfg: Any,  # noqa: ANN401
    vault: Any | None,  # noqa: ANN401
    *,
    cli_loras: list[LoraEntry] | None = None,
) -> list[LoraEntry]:
    """Resolve the final LoRA stack for this run.

    Precedence (P3-D3, P3-D4): CLI > vault > cfg.

    When ``cli_loras`` is not None, CLI wins entirely — vault.loras is
    bypassed and cfg.loras is replaced. If vault is loaded with
    non-empty ``.loras``, a single WARNING is emitted naming the count
    of bypassed refs (refs themselves never enter the log line per
    spec §4 P3-Privacy-4). CLI-supplied refs are registered with the
    global :class:`RedactionRegistry` BEFORE the WARNING fires so any
    later traceback containing a CLI ref is already redactable.

    When ``cli_loras`` is None, the P1 precedence rule applies
    unchanged: vault wins entirely; diverging non-empty cfg + vault
    raises :class:`LoraStackConflict`.

    Order in the returned list is the activation order (matters for
    ``set_adapters``).

    Args:
        cfg: A loaded :class:`kinoforge.core.config.Config` (typed
            ``Any`` to avoid a circular import).
        vault: An optional loaded :class:`kinoforge.core.vault.Vault`.
        cli_loras: Optional CLI-supplied stack from
            ``parse_loras_heredoc``. When ``None``, the P1 cfg/vault
            precedence rule runs. When a list (including empty), it
            wins entirely.

    Returns:
        Ordered list of :class:`LoraEntry`. Vault-only ``label`` field
        is stripped on upcast.

    Raises:
        LoraStackConflict: Only when ``cli_loras is None`` AND
            cfg.loras + vault.loras both non-empty with diverging refs.
    """
    from kinoforge.core.errors import LoraStackConflict

    if cli_loras is not None:
        RedactionRegistry.instance().add_many(
            [(lo.ref, "lora:ref") for lo in cli_loras]
        )
        if vault is not None and getattr(vault, "loras", None):
            logger.warning(
                "cli-loras-bypass-vault: --loras override applied; "
                "vault.loras (%d entries) bypassed for this run. "
                "Vault is unchanged on disk.",
                len(vault.loras),
            )
        return list(cli_loras)

    cfg_loras: list[LoraEntry] = list(getattr(cfg, "loras", []))
    if vault is None or not getattr(vault, "loras", None):
        return cfg_loras
    cfg_refs = {lo.ref for lo in cfg_loras}
    vault_refs = {lo.ref for lo in vault.loras}
    if cfg_loras and cfg_refs != vault_refs:
        raise LoraStackConflict(
            f"cfg.loras and vault.loras both set with diverging ref sets — "
            f"cfg={sorted(cfg_refs)}, vault={sorted(vault_refs)}; remove "
            f"cfg.loras and use vault.loras as sole source"
        )
    return [LoraEntry(**lo.model_dump(exclude={"label"})) for lo in vault.loras]
