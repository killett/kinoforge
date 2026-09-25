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
import re
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import ParseResult, parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kinoforge.core.redaction import RedactionRegistry

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


_CIVITAI_HOSTS = frozenset({"civitai.com", "www.civitai.com"})
_CIVARCHIVE_HOSTS = frozenset({"civarchive.com", "www.civarchive.com"})
_HF_HOSTS = frozenset({"huggingface.co", "www.huggingface.co"})

_CIVITAI_LIKE_PATH = re.compile(r"^/models/(\d+)(?:/[^/]*)?/?$")
_HF_BLOB_PATH = re.compile(r"^/([^/]+)/([^/]+)/blob/([^/]+)/(.+?)/?$")


def _normalize_ref(value: str) -> str:
    """Normalize a URL-shaped LoRA ref to its canonical short form.

    Recognises:
      * civitai.com /models/<id>?...modelVersionId=<vid>... → civitai:<id>@<vid>
      * civarchive.com (same shape) → civarchive:<id>@<vid>
      * huggingface.co /<org>/<repo>/blob/<branch>/<file> → hf:<org>/<repo>:<file>

    Inputs already in canonical short form (``civitai:...``, ``hf:...``,
    ``file:...``, etc.) pass through unchanged. Unknown URL hosts pass
    through unchanged so the existing ``http`` source module still
    resolves them. HuggingFace bare-repo URLs are explicitly out of scope
    and pass through unchanged.

    Raises:
        ValueError: civitai or civarchive URL is missing the
            ``modelVersionId`` query parameter. The error message does
            NOT include the URL text (privacy invariant — same posture as
            ``LineError`` in ``cli/loras_arg.py``).
    """
    if not value.lower().startswith(("http://", "https://")):
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host in _CIVITAI_HOSTS:
        return _normalize_civitai_like(parsed, "civitai")
    if host in _CIVARCHIVE_HOSTS:
        return _normalize_civitai_like(parsed, "civarchive")
    if host in _HF_HOSTS:
        return _normalize_hf(parsed, original=value)
    return value


def _normalize_civitai_like(parsed: ParseResult, scheme: str) -> str:
    """Shared rule for civitai + civarchive URLs (identical path shape)."""
    m = _CIVITAI_LIKE_PATH.match(parsed.path)
    if m is None:
        return parsed.geturl()
    model_id = m.group(1)
    qs = parse_qs(parsed.query)
    version_ids = qs.get("modelVersionId") or qs.get("modelversionid")
    if not version_ids:
        # Privacy: NO URL text in the message. Operator sees the rule,
        # not the data they pasted.
        raise ValueError(
            f"{scheme} URL missing required ?modelVersionId=... query "
            f"parameter (canonical refs are version-pinned)"
        )
    return f"{scheme}:{model_id}@{version_ids[0]}"


def _normalize_hf(parsed: ParseResult, *, original: str) -> str:
    """Recognise HF blob URLs only; bare-repo and others pass through."""
    m = _HF_BLOB_PATH.match(parsed.path)
    if m is None:
        return original
    org, repo, branch, file_path = m.groups()
    if branch != "main":
        logger.warning(
            "hf URL branch=%s dropped; canonical hf: ref does not encode "
            "branch (only `main` is pinned implicitly)",
            branch,
        )
    return f"hf:{org}/{repo}:{file_path}"


#: Wan 2.2's mixture-of-experts branch vocabulary — the ONLY targets that may
#: be written back into `branch` (U60). Duplicated from
#: `wan_t2v_server.LORA_TARGET_UNIVERSE` rather than imported: `core` must not
#: import `engines`, and the server module carries a pod-only dependency set.
#: `tests/test_lora_target_to_branch_map.py` asserts the two agree.
_WAN_MOE_BRANCHES: frozenset[str] = frozenset({"high_noise", "low_noise"})


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
        target: Generalised routing token superseding ``branch``.
            ``branch`` names only Wan's MoE noise split; ``target`` also
            names workflow partitions (e.g. H3's ``transformer`` /
            ``transformer_ref``) that ``branch``'s Literal cannot
            express. ``None`` means "the pod profile's default target",
            which reproduces ``branch="auto"`` exactly. ``branch`` is
            deprecated in favor of this field; the two may not disagree.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$|^$")
    branch: Literal["high_noise", "low_noise", "auto"] = Field(default="auto")
    target: str | None = Field(default=None)

    @field_validator("ref", mode="before")
    @classmethod
    def _normalize_url_ref(cls, v: Any) -> Any:  # noqa: ANN401
        """Normalize URL-shaped refs to canonical short form.

        Runs ``mode="before"`` so pydantic's ``min_length=1`` check sees
        the post-normalization value. See module-level ``_normalize_ref``
        for the URL → canonical rules and the privacy invariant on raised
        errors.
        """
        if isinstance(v, str):
            return _normalize_ref(v)
        return v

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

    @model_validator(mode="after")
    def _resolve_branch_to_target(self) -> LoraEntry:
        """Map the deprecated `branch` onto `target`; refuse disagreement.

        `branch` is Wan's MoE vocabulary and cannot name H3's workflow
        partitions. `target` is resolved against the server profile's
        vocabulary instead. `None` means "the profile's default target",
        which reproduces `branch="auto"` exactly.
        """
        implied = None if self.branch == "auto" else self.branch
        if implied is not None and self.target is not None and implied != self.target:
            raise ValueError(
                f"branch and target disagree: branch={self.branch!r} implies "
                f"target={implied!r}, but target={self.target!r} was set; "
                f"set only `target` (branch is deprecated)"
            )
        if implied is not None and self.target is None:
            object.__setattr__(self, "target", implied)
            logger.warning(
                "deprecated-lora-branch: 1 entry used `branch`; it is mapped to "
                "`target`. Use `target:` — see docs/breaking-changes.md"
            )
        if implied is None and self.target in _WAN_MOE_BRANCHES:
            # U60, the reverse map. Without it a `target:`-only Wan entry keeps
            # `branch="auto"`, which `wan_t2v_server` refuses on a MoE pipeline
            # (`BranchAutoNotAllowedOnMoE`) — an HTTP 400 that arrives 25-30
            # minutes and one ~70 GB weight fetch after the operator started.
            # The config-load check that accepts `target: high_noise` on Wan is
            # what made the acceptance/routing gap reachable at all.
            #
            # Narrow on purpose: only when `branch` is still at its "auto"
            # default AND `target` names a Wan MoE branch. Mapping
            # unconditionally would put H3's `transformer` into a field whose
            # vocabulary is Wan's MoE tokens — U55 in the other direction, and
            # that defect was just fixed by making H3 stop doing exactly this.
            object.__setattr__(self, "branch", self.target)
        return self


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
