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

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    pass


class LoraEntry(BaseModel):
    """One LoRA entry: ref + strength + optional sha256.

    See docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §6.1.

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
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$|^$")


def resolve_active_lora_stack(
    cfg: Any,  # noqa: ANN401
    vault: Any | None,  # noqa: ANN401
) -> list[LoraEntry]:
    """Resolve the final LoRA stack for this run.

    Precedence (matches vault spec D2's "always-on when vault loaded"
    rule):

    - Vault loaded with non-empty ``vault.loras`` → vault wins entirely.
      Cfg's ``loras:`` block is ignored to keep the "vault is sole
      owner of confidential refs" invariant load-bearing.
    - Vault absent OR vault.loras empty → cfg.loras is the stack.

    When both ``cfg.loras`` and ``vault.loras`` are populated with
    DIVERGING ref sets, ``LoraStackConflict`` raises (defensive — likely
    user mistake).

    Order in the returned list is the activation order (matters for
    ``set_adapters``).

    P3 will extend this signature to accept a CLI override merging
    against the cfg/vault baseline; P1 keeps the contract narrow.

    Args:
        cfg: A loaded :class:`kinoforge.core.config.Config` (typed as
            ``Any`` here to avoid a circular import).
        vault: An optional loaded :class:`kinoforge.core.vault.Vault`.

    Returns:
        Ordered list of :class:`LoraEntry`. Vault-only ``label`` field
        is stripped on upcast.

    Raises:
        LoraStackConflict: When both ``cfg.loras`` + ``vault.loras`` are
            populated and the ref sets differ.
    """
    from kinoforge.core.errors import LoraStackConflict

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
