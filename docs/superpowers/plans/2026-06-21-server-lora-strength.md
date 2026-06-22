# P1 — Server per-LoRA strength weights — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land per-LoRA `strength` end-to-end (cfg + vault + HTTP API + server `set_adapters(adapter_weights=)` + matcher) on the kinoforge diffusers engine, while preserving warm-reuse semantics and the ephemeral/vault privacy invariants.

**Architecture:** New shared Pydantic `LoraEntry` model used by both the public cfg `loras:` block and the vault `loras:` list (`VaultLoRA(LoraEntry)` adds `label`). HTTP `/lora/set_stack` request migrates from `target_refs: list[str]` to `target: list[LoraTarget]`. Server-side `set_adapters` always passes `adapter_weights=`. Strength is mutable per-run and explicitly OUT of `capability_key` hash material; the warm-attach matcher learns to schedule a same-refs / different-strength set_stack call.

**Tech Stack:** Python 3.13, Pydantic V2, FastAPI (server), diffusers + PEFT (LoRA loaders), pytest, RunPod (live smokes).

**User decisions (already made):**
- D1: P1 first; P2 (Wan 2.2 dual-transformer h/l) + P3 (CLI `--loras` arg) deferred high-priority.
- D2: HTTP API shape = tagged objects `target: list[{ref, strength, ...}]`.
- D3: Cfg schema = dedicated top-level `loras:` block + shared `LoraEntry`; `VaultLoRA` inherits + adds `label`.
- D4: Strength does NOT enter `capability_key` hash material — strength is mutable per-run.
- D5: Strength range = `Field(ge=-2.0, le=2.0)`; default = 1.0.
- D6: Verify scope = unit + integration + Tier-3 live smoke ($0.30) + Tier-4 live smoke ($1.50). Total live ~$1.80.
- D7: Strength is non-sensitive; NOT registered with `RedactionRegistry`.
- D8: Pydantic `model_validator(mode="before")` auto-promotes both legacy cfgs and legacy HTTP payloads during a single transition window.
- D9: `LoraInventoryEntry.last_strength` surfaces strength via `/lora/inventory`.
- D10: Matcher checks BOTH refs and strength (math.isclose, rel_tol=1e-6).
- D11: Both cfg.loras + vault.loras populated with diverging ref sets → `LoraStackConflict`.

---

## File structure

**New files:**
- `src/kinoforge/core/lora.py` — `LoraEntry`, `resolve_active_lora_stack`, internal helpers.
- `tests/core/test_lora_entry.py` — LoraEntry validators.
- `tests/test_lora_schema_parity.py` — lockdown that `LoraEntry` and `LoraTarget` agree on `ref`+`strength` fields.
- `tests/core/test_lora_resolve.py` — `resolve_active_lora_stack` vault-vs-cfg semantics.
- `tests/core/test_config_loras_migration.py` — legacy `models: [{kind: lora}]` promotion.
- `tests/core/test_capability_key_strength.py` — strength NOT in identity hash.
- `tests/core/test_warm_reuse_matcher_strength.py` — matcher refs+strength equality.
- `tests/engines/diffusers/__init__.py` + `tests/engines/diffusers/test_wan_t2v_server_strength.py` — server-side wiring + rollback.
- `tests/test_adapters_build_set_stack_request.py` — bridge LoraEntry → LoraTarget.
- `tests/smoke/live_wan21/test_lora_strength_variation.py` — Tier-3 live smoke.
- `tests/smoke/release_wan22/test_lora_strength_variation.py` — Tier-4 live smoke.

**Modified files:**
- `src/kinoforge/core/config.py` — `Config.loras: list[LoraEntry]`, `_promote_legacy_kind_lora_to_loras_block` validator, `ModelEntry.kind` Literal narrowed (removes `"lora"`), `capability_key()` reads `self.loras`.
- `src/kinoforge/core/vault.py` — `VaultLoRA` now inherits from `LoraEntry`.
- `src/kinoforge/core/errors.py` — new `LoraStackConflict`, new `SetStackRequestRejected`, broaden `LoraSetAdaptersFailed` catch.
- `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` — `LoraTarget` model, `SetStackRequest` migration, `LoraInventoryEntry.last_strength`, `_reload_pipeline_loras` signature change, rollback strength restoration.
- `src/kinoforge/_adapters.py` — `build_set_stack_request` helper.
- `src/kinoforge/engines/diffusers/__init__.py` — engine integration switches to `build_set_stack_request`.
- `src/kinoforge/core/warm_reuse/matcher.py` — `is_stack_match` strength check.
- `tests/test_no_unredacted_writes.py` — extend AST scan coverage list with P1 write sites.
- `examples/configs/*.yaml` (sweep) — migrate `kind: lora` entries to top-level `loras:` block.
- `PROGRESS.md` — close-out anchor for P1 with commit hashes.

---

## Task 0: `LoraEntry` shared Pydantic model + `LoraTarget` server mirror + schema-parity lockdown

**Goal:** Land the canonical per-LoRA Pydantic class (`LoraEntry`) in `core/lora.py`, mirror it as `LoraTarget` in the wan_t2v_server module, and lock the two schemas in step via a parity test.

**Files:**
- Create: `src/kinoforge/core/lora.py`
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (add `LoraTarget` class)
- Modify: `src/kinoforge/core/vault.py` (`VaultLoRA` inherits `LoraEntry`)
- Create: `tests/core/test_lora_entry.py`
- Create: `tests/test_lora_schema_parity.py`

**Acceptance Criteria:**
- [ ] `LoraEntry(ref="x", strength=0.5)` constructs; `strength` default = 1.0 when omitted.
- [ ] `LoraEntry(ref="x", strength=2.5)` raises `ValidationError`.
- [ ] `LoraEntry(ref="x", strength=-2.5)` raises `ValidationError`.
- [ ] `LoraEntry(ref="x", strength=-2.0)` accepts; `LoraEntry(ref="x", strength=2.0)` accepts (bounds inclusive).
- [ ] `LoraEntry(ref="x", strength=1.0, banana="y")` raises (`extra="forbid"`).
- [ ] `LoraEntry(ref="x", sha256="abc")` raises (sha256 pattern: 64-char hex or empty).
- [ ] `VaultLoRA(ref="x", label="my-style")` constructs; `VaultLoRA.strength` defaults to 1.0.
- [ ] `LoraTarget` and `LoraEntry` agree on `ref` and `strength` field types + constraints (parity test).
- [ ] `pixi run lint` clean; `pixi run typecheck` clean.

**Verify:** `pixi run pytest tests/core/test_lora_entry.py tests/test_lora_schema_parity.py -v` → 9+ passed.

**Steps:**

- [ ] **Step 1: Create `src/kinoforge/core/lora.py` with `LoraEntry`.**

```python
"""Shared per-LoRA Pydantic schema for cfg + vault.

`LoraEntry` is the canonical class used by both public cfg `loras:` blocks
and vault `loras:` lists. `VaultLoRA(LoraEntry)` extends it with a
vault-internal `label`. Future fields (P2 branch, trigger_word,
sampler_hints) land here once.

Privacy classification (P1):
  - `ref`     — SENSITIVE per ephemeral spec D4.
  - `strength` — NON-SENSITIVE (low-entropy float; same posture as seed).
  - `sha256`  — derived hash; per D4 derived hashes are sensitive.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LoraEntry(BaseModel):
    """One LoRA entry: ref + strength + optional sha256.

    See docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §6.1.

    Attributes:
        ref: Vendor-neutral model reference (e.g. ``"civitai:1234@5678"`` or
            ``"hf:Org/Repo:filename"``). SENSITIVE under vault mode.
        strength: PEFT adapter weight applied via
            ``set_adapters(adapter_weights=...)``. Range hard-bounded to
            ``[-2.0, 2.0]`` (industry-standard a1111 LoRA range). Default 1.0.
            NON-SENSITIVE — same posture as ``seed`` / ``num_inference_steps``.
        sha256: Optional content hash for integrity verification. 64-char
            lowercase hex OR empty string. Derived hash is sensitive per
            ephemeral spec D4.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$|^$")
```

- [ ] **Step 2: Write the failing test `tests/core/test_lora_entry.py`.**

```python
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
        LoraEntry(ref="civitai:1@2", strength=1.0, banana="yellow")
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
```

- [ ] **Step 3: Run failing tests.**

Run: `pixi run pytest tests/core/test_lora_entry.py -v`
Expected: ImportError or 9-10 FAILED — `kinoforge.core.lora` does not exist yet.

- [ ] **Step 4: Run the tests after step 1 created the module.**

Run: `pixi run pytest tests/core/test_lora_entry.py -v`
Expected: 9-10 PASSED.

- [ ] **Step 5: Migrate `VaultLoRA` to inherit `LoraEntry`.**

Find the current `VaultLoRA` class in `src/kinoforge/core/vault.py` (around line 44) and replace it:

```python
# Replace existing VaultLoRA definition with:
from kinoforge.core.lora import LoraEntry


class VaultLoRA(LoraEntry):
    """Vault-side LoRA entry: LoraEntry + optional vault-internal label.

    `label` is vault-internal only — never persisted, never logged, never
    sent over the wire (stripped on upcast to LoraEntry inside
    ``resolve_active_lora_stack``).
    """

    label: str | None = None  # vault-only extension
```

- [ ] **Step 6: Add a VaultLoRA inheritance test to `tests/core/test_lora_entry.py`.**

Append:

```python
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
    from pydantic import ValidationError
    from kinoforge.core.vault import VaultLoRA

    with pytest.raises(ValidationError):
        VaultLoRA(ref="x", strength=3.0)
```

- [ ] **Step 7: Run the expanded tests.**

Run: `pixi run pytest tests/core/test_lora_entry.py -v`
Expected: 12 PASSED.

- [ ] **Step 8: Add `LoraTarget` to `wan_t2v_server.py`.**

Find the existing `class LoraInventoryEntry` block (around line 123) and insert `LoraTarget` just BEFORE the existing `SetStackRequest` class (around line 141). Do NOT change `SetStackRequest` yet — that's Task 3.

```python
class LoraTarget(BaseModel):
    """One entry in /lora/set_stack target list.

    Schema-equivalent to ``kinoforge.core.lora.LoraEntry`` but defined in
    the server module so the server has no import-time dependency on
    ``kinoforge.core.lora`` (server runs on the pod with a minimal
    dependency set). The lockstep invariant is locked by
    ``tests/test_lora_schema_parity.py``.

    See docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §6.3.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
```

If `ConfigDict` is not already imported at the top of `wan_t2v_server.py`, add it to the existing Pydantic import line. Same for `Field`.

- [ ] **Step 9: Write the schema parity lockdown `tests/test_lora_schema_parity.py`.**

```python
"""Lockdown: LoraEntry (core) and LoraTarget (server) must agree on the
shared field set so a future edit to either stays in sync.

Why two classes? See spec §6.3 — server runs in a slim pod env without
``kinoforge.core`` available, so the wire format is its own contract.
"""

from __future__ import annotations

from kinoforge.core.lora import LoraEntry
from kinoforge.engines.diffusers.servers.wan_t2v_server import LoraTarget


def _field_constraints(model_cls: type, field_name: str) -> dict[str, object]:
    """Return a small dict of constraint values for the named field."""
    field_info = model_cls.model_fields[field_name]
    # Pydantic V2 stores numeric bounds in metadata; pull them out.
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
    """Bug: a future edit changes ref's min_length on one but not the other.
    Both must reject empty strings identically."""
    e_field = LoraEntry.model_fields["ref"]
    t_field = LoraTarget.model_fields["ref"]
    assert e_field.annotation == t_field.annotation == str


def test_lora_entry_and_lora_target_share_strength_field_constraints() -> None:
    """Bug: bounds drift between the two — server accepts strength=3.0 that
    the cfg-side rejected, or vice-versa. Round-trip becomes lossy."""
    e = _field_constraints(LoraEntry, "strength")
    t = _field_constraints(LoraTarget, "strength")
    assert e["default"] == t["default"] == 1.0
    assert e["ge"] == t["ge"] == -2.0
    assert e["le"] == t["le"] == 2.0
    assert e["annotation"] == t["annotation"] == float


def test_both_models_forbid_extra_fields() -> None:
    """Bug: one model loses extra='forbid', allowing silent typos to
    cross the wire intact and confuse the receiver."""
    assert LoraEntry.model_config.get("extra") == "forbid"
    assert LoraTarget.model_config.get("extra") == "forbid"
```

- [ ] **Step 10: Run the parity test.**

Run: `pixi run pytest tests/test_lora_schema_parity.py -v`
Expected: 3 PASSED.

- [ ] **Step 11: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 12: Commit.**

```bash
git add src/kinoforge/core/lora.py src/kinoforge/core/vault.py \
        src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
        tests/core/test_lora_entry.py tests/test_lora_schema_parity.py
pixi run pre-commit run --files src/kinoforge/core/lora.py src/kinoforge/core/vault.py src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/core/test_lora_entry.py tests/test_lora_schema_parity.py
git commit -m "feat(p1): LoraEntry + VaultLoRA(LoraEntry) + LoraTarget + schema parity lockdown"
```

---

## Task 1: `Config.loras` block + legacy promoter validator + `ModelEntry.kind` narrowing

**Goal:** Add the top-level `loras: list[LoraEntry]` field to `Config`, write a `model_validator(mode="before")` that auto-promotes legacy `models: [{kind: lora, ...}]` entries with a `DeprecationWarning`, and remove `"lora"` from `ModelEntry.kind` Literal.

**Files:**
- Modify: `src/kinoforge/core/config.py`
- Create: `tests/core/test_config_loras_migration.py`

**Acceptance Criteria:**
- [ ] `Config` accepts a top-level `loras: list[LoraEntry]` field.
- [ ] Loading a legacy cfg with `models: [{kind: lora, ref: ...}]` promotes those entries into `Config.loras` with default strength=1.0.
- [ ] Promotion emits a `DeprecationWarning` naming the promoted count.
- [ ] `ModelEntry(kind="lora", ref="x", target="loras")` raises `ValidationError` directly (the promoter runs at Config level, not ModelEntry level; bypassing the promoter must fail).
- [ ] Loading a new-shape cfg (top-level `loras:` block, no `kind: lora` in models) loads without warnings.
- [ ] A cfg with BOTH `loras:` block AND `models: [{kind: lora, ...}]` merges them, with the explicit `loras:` entries appearing FIRST in the resulting list (cfg author's intent wins on ordering).

**Verify:** `pixi run pytest tests/core/test_config_loras_migration.py -v` → 6+ passed.

**Steps:**

- [ ] **Step 1: Write the failing tests first.**

Create `tests/core/test_config_loras_migration.py`:

```python
"""Config migration tests: legacy `models: [{kind: lora, ...}]` →
new top-level `loras:` block."""

from __future__ import annotations

import warnings

import pytest
from pydantic import ValidationError

from kinoforge.core.config import Config, ModelEntry


def _base_cfg_dict(extra: dict[str, object]) -> dict[str, object]:
    """Return a minimum-viable cfg dict to which a test merges `extra`."""
    base = {
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
        ],
        "compute": {
            "provider": "local",
            "image": "fake:latest",
            "lifecycle": {"budget": 1.0},
        },
    }
    base.update(extra)
    return base


def test_legacy_models_kind_lora_promotes_to_loras_block() -> None:
    """Bug: the promoter silently drops legacy LoRAs, capability_key
    derivation no longer includes the LoRA refs, warm-reuse routes the
    user to the wrong pool of pods."""
    cfg_dict = _base_cfg_dict({
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
            {"ref": "civitai:1@2", "kind": "lora", "target": "loras"},
            {"ref": "hf:Org/y:foo.safetensors", "kind": "lora", "target": "loras"},
        ],
    })
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cfg = Config.model_validate(cfg_dict)
    assert len(cfg.loras) == 2
    assert cfg.loras[0].ref == "civitai:1@2"
    assert cfg.loras[0].strength == 1.0   # default per D5
    assert cfg.loras[1].ref == "hf:Org/y:foo.safetensors"
    # Legacy entries are removed from cfg.models
    assert all(m.kind != "lora" for m in cfg.models)


def test_legacy_promotion_emits_deprecation_warning() -> None:
    """Bug: the promoter silently auto-fixes legacy cfgs forever → operators
    never learn to update the cfg shape, the transition window never closes."""
    cfg_dict = _base_cfg_dict({
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
            {"ref": "civitai:1@2", "kind": "lora", "target": "loras"},
        ],
    })
    with pytest.warns(DeprecationWarning, match="legacy.*kind: lora.*promoted 1"):
        Config.model_validate(cfg_dict)


def test_new_shape_loads_without_warnings() -> None:
    """Bug: false-positive warnings on already-migrated cfgs."""
    cfg_dict = _base_cfg_dict({
        "loras": [
            {"ref": "civitai:1@2"},
            {"ref": "hf:Org/y:foo.safetensors", "strength": 0.7},
        ],
    })
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = Config.model_validate(cfg_dict)   # raises if a DeprecationWarning fires
    assert len(cfg.loras) == 2
    assert cfg.loras[1].strength == 0.7


def test_modelentry_rejects_kind_lora_directly() -> None:
    """Bug: bypassing the Config-level promoter by constructing a ModelEntry
    with kind='lora' should fail at the Literal level — otherwise we silently
    leak a kind=lora entry into Config.models and capability_key derivation
    reads the wrong list."""
    with pytest.raises(ValidationError):
        ModelEntry(ref="civitai:1@2", kind="lora", target="loras")  # type: ignore[arg-type]


def test_legacy_promotion_carries_sha256_through() -> None:
    cfg_dict = _base_cfg_dict({
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
            {
                "ref": "civitai:1@2",
                "kind": "lora",
                "target": "loras",
                "sha256": "a" * 64,
            },
        ],
    })
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cfg = Config.model_validate(cfg_dict)
    assert cfg.loras[0].sha256 == "a" * 64


def test_loras_block_and_legacy_models_kind_lora_merge_explicit_first() -> None:
    """Bug: a cfg with BOTH the new `loras:` block AND legacy `kind: lora`
    entries — the explicit (new-shape) entries must win on ordering so
    the cfg author's intent is preserved. set_adapters order matters."""
    cfg_dict = _base_cfg_dict({
        "loras": [{"ref": "civitai:99@100"}],
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
            {"ref": "civitai:1@2", "kind": "lora", "target": "loras"},
        ],
    })
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        cfg = Config.model_validate(cfg_dict)
    assert [lo.ref for lo in cfg.loras] == ["civitai:99@100", "civitai:1@2"]
```

- [ ] **Step 2: Run failing tests.**

Run: `pixi run pytest tests/core/test_config_loras_migration.py -v`
Expected: ImportError / failure (no `Config.loras`, no promoter, `kind: "lora"` still valid).

- [ ] **Step 3: Modify `src/kinoforge/core/config.py`.**

Locate the `ModelEntry` class (around line 494) and narrow the `kind` Literal:

```python
# CHANGE the existing ModelEntry.kind line FROM:
#     kind: Literal["base", "lora", "vae", "text_encoder", "clip_vision"]
# TO:
    kind: Literal["base", "vae", "text_encoder", "clip_vision"]
```

Locate the `class Config(BaseModel):` block. Add the `loras` field alongside `models`:

```python
# Add to Config field block:
    loras: list[LoraEntry] = []
```

Add the import at the top of `config.py`:

```python
from kinoforge.core.lora import LoraEntry
```

Add the `model_validator(mode="before")` to the `Config` class. Place it BEFORE any existing `@model_validator(mode="after")` validators on `Config`:

```python
    @model_validator(mode="before")
    @classmethod
    def _promote_legacy_kind_lora_to_loras_block(cls, data: Any) -> Any:
        """Auto-migrate legacy cfgs that put LoRAs under models:.

        Reads `models: [{kind: lora, ...}, ...]`, moves each LoRA entry
        into a new top-level `loras:` block (with default strength=1.0),
        removes them from `models:`. Existing explicit `loras:` entries
        win on ordering — they come first in the resulting list.

        Emits a DeprecationWarning when promotion fires so operators see
        which cfgs still ship the legacy shape.

        See docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §6.4.
        """
        if not isinstance(data, dict):
            return data
        models = data.get("models") or []
        legacy_loras = [
            m for m in models if isinstance(m, dict) and m.get("kind") == "lora"
        ]
        if not legacy_loras:
            return data
        non_lora_models = [
            m for m in models
            if not (isinstance(m, dict) and m.get("kind") == "lora")
        ]
        promoted = [
            {"ref": m["ref"], "sha256": m.get("sha256")}
            for m in legacy_loras
        ]
        data["models"] = non_lora_models
        data["loras"] = list(data.get("loras") or []) + promoted
        import warnings
        warnings.warn(
            f"cfg uses legacy `models: [{{kind: lora}}, ...]` shape; "
            f"promoted {len(promoted)} entries to top-level `loras:` block. "
            f"Update the cfg to the new shape.",
            DeprecationWarning,
            stacklevel=2,
        )
        return data
```

Confirm `Any` is imported (`from typing import Any`).

- [ ] **Step 4: Run tests to confirm GREEN.**

Run: `pixi run pytest tests/core/test_config_loras_migration.py -v`
Expected: 6 PASSED.

- [ ] **Step 5: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 6: Confirm no existing test regression.**

Run: `pixi run pytest tests/core/test_config.py -v`
Expected: all PASSED. If `kind: "lora"` appears in any existing test fixture, those tests need the same migration — promote inline via fixture update OR confirm the fixture still loads cleanly because the promoter handles it transparently.

- [ ] **Step 7: Commit.**

```bash
git add src/kinoforge/core/config.py tests/core/test_config_loras_migration.py
pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config_loras_migration.py
git commit -m "feat(p1): Config.loras block + legacy kind=lora promoter + ModelEntry.kind narrowing"
```

---

## Task 2: `capability_key()` reads `Config.loras` and excludes strength

**Goal:** Update `Config.capability_key()` to source LoRA refs from `self.loras` (the new block) rather than walking `self.models` for `kind="lora"`. Lock the invariant that strength is NOT in the hash material.

**Files:**
- Modify: `src/kinoforge/core/config.py` (capability_key method)
- Create: `tests/core/test_capability_key_strength.py`

**Acceptance Criteria:**
- [ ] `cfg.capability_key()` material includes `[lo.ref for lo in cfg.loras]` and does NOT walk `cfg.models` for `kind="lora"`.
- [ ] Two cfgs identical in `loras[*].ref` but differing in `loras[*].strength` hash to the SAME capability key (P1-Identity invariant).
- [ ] Two cfgs identical in their LoRA refs but where one shipped them under legacy `models: [{kind: lora}]` and one under new `loras:` block hash to the SAME capability key (migration is hash-stable).

**Verify:** `pixi run pytest tests/core/test_capability_key_strength.py tests/core/test_config.py -v` → all PASSED.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
"""Capability key invariants for the P1 loras block.

P1-Identity: strength is mutable per-run and MUST NOT enter the
capability_key hash material. Two cfgs identical in refs but differing
in strengths must derive the same key — otherwise warm-reuse routes
strength-tweak iterations to a fresh cold-boot.
"""

from __future__ import annotations

import warnings

from kinoforge.core.config import Config


def _cfg(loras_block: list[dict[str, object]] | None = None) -> Config:
    base = {
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
        ],
        "compute": {
            "provider": "local",
            "image": "fake:latest",
            "lifecycle": {"budget": 1.0},
        },
    }
    if loras_block is not None:
        base["loras"] = loras_block
    return Config.model_validate(base)


def test_capability_key_strength_invariant() -> None:
    """Bug: a future edit folds strength into the hash material → users
    re-running with strength=0.7 vs 1.0 cold-boot a fresh pod instead of
    reusing the warm one."""
    a = _cfg([{"ref": "civitai:1@2", "strength": 1.0}])
    b = _cfg([{"ref": "civitai:1@2", "strength": 0.5}])
    assert a.capability_key().derive() == b.capability_key().derive()


def test_capability_key_changes_when_ref_set_changes() -> None:
    """Bug: ref-set is silently dropped from the hash → different LoRA
    stacks alias to the same warm pool."""
    a = _cfg([{"ref": "civitai:1@2"}])
    b = _cfg([{"ref": "civitai:99@100"}])
    assert a.capability_key().derive() != b.capability_key().derive()


def test_capability_key_stable_across_legacy_to_new_shape_migration() -> None:
    """Bug: post-migration cfgs hash differently than pre-migration cfgs
    with the same refs — every operator who migrates their cfg invalidates
    their warm pool."""
    base_legacy = {
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
            {"ref": "civitai:1@2", "kind": "lora", "target": "loras"},
        ],
        "compute": {
            "provider": "local",
            "image": "fake:latest",
            "lifecycle": {"budget": 1.0},
        },
    }
    base_new = {
        "engine": {"kind": "fake", "precision": "fp16"},
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
        ],
        "loras": [{"ref": "civitai:1@2"}],
        "compute": {
            "provider": "local",
            "image": "fake:latest",
            "lifecycle": {"budget": 1.0},
        },
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        legacy_cfg = Config.model_validate(base_legacy)
    new_cfg = Config.model_validate(base_new)
    assert legacy_cfg.capability_key().derive() == new_cfg.capability_key().derive()


def test_capability_key_order_of_loras_matters() -> None:
    """Bug: a future edit sorts loras before hashing → swap order silently
    aliases to the same key, but set_adapters order affects output."""
    a = _cfg([{"ref": "civitai:1@2"}, {"ref": "civitai:3@4"}])
    b = _cfg([{"ref": "civitai:3@4"}, {"ref": "civitai:1@2"}])
    assert a.capability_key().derive() != b.capability_key().derive()
```

- [ ] **Step 2: Run failing tests.**

Run: `pixi run pytest tests/core/test_capability_key_strength.py -v`
Expected: failures — `capability_key` still walks `cfg.models` for kind=lora; not aware of `cfg.loras`.

- [ ] **Step 3: Update `Config.capability_key()` in `src/kinoforge/core/config.py`.**

Locate the existing `capability_key` method (around line 928). Replace the LoRA-walking section (around lines 940-945):

```python
# REPLACE the existing walk:
#     loras: list[str] = []
#     for entry in self.models:
#         if entry.kind == "base":
#             ...
#         elif entry.kind == "lora":
#             loras.append(entry.ref)
#     ...

# WITH:
        # P1 (2026-06-21): LoRA refs source from self.loras (new top-level
        # block). Strength is deliberately excluded — it's a mutable per-run
        # parameter applied via /lora/set_stack on warm-attach, not part of
        # the identity hash. Same-refs / different-strength runs reuse the
        # warm pod. See spec §7.
        loras_refs: list[str] = [lo.ref for lo in self.loras]
        base_model: str | None = None
        for entry in self.models:
            if entry.kind == "base":
                if base_model is not None:
                    # preserve existing "last base wins" semantics
                    pass
                base_model = entry.ref
        # ... rest of derivation unchanged; substitute loras_refs for loras
```

Verify the rest of `capability_key()` uses `loras_refs` everywhere the old `loras` local variable was used. Search for other `entry.kind == "lora"` reads in `config.py` and update or remove them.

- [ ] **Step 4: Run failing tests, then the broader cfg suite.**

Run: `pixi run pytest tests/core/test_capability_key_strength.py tests/core/test_config.py -v`
Expected: all PASSED.

- [ ] **Step 5: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/config.py tests/core/test_capability_key_strength.py
pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_capability_key_strength.py
git commit -m "feat(p1): capability_key reads Config.loras; strength stays out of hash"
```

---

## Task 3: `SetStackRequest` accepts new `target: list[LoraTarget]` shape with legacy promoter

**Goal:** Migrate the `/lora/set_stack` server-side request schema from `target_refs: list[str]` to `target: list[LoraTarget]`. Add a `model_validator(mode="before")` that auto-promotes legacy `target_refs` payloads with default strength=1.0 during a one-window transition. Reject the case where BOTH `target_refs` and `target` keys appear in the same request (defensive: client bug).

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (`SetStackRequest` class)
- Create: `tests/engines/diffusers/__init__.py` (empty marker)
- Create: `tests/engines/diffusers/test_wan_t2v_server_strength.py` (server schema tests)

**Acceptance Criteria:**
- [ ] `SetStackRequest(target=[LoraTarget(ref="x", strength=0.5)], download_specs={})` constructs.
- [ ] `SetStackRequest(target_refs=["x"], download_specs={})` auto-promotes to one `LoraTarget` with strength=1.0; constructed object has `req.target[0].strength == 1.0`.
- [ ] `SetStackRequest(target=[...], target_refs=[...], download_specs={})` raises `ValidationError` with message naming both keys.
- [ ] `SetStackRequest(target=[{"ref": "x", "strength": 3.0}], download_specs={})` raises `ValidationError` (server-side bound check).

**Verify:** `pixi run pytest tests/engines/diffusers/test_wan_t2v_server_strength.py -v` → 4+ passed.

**Steps:**

- [ ] **Step 1: Create the empty `__init__.py` for the new test subdir.**

```bash
mkdir -p tests/engines/diffusers
touch tests/engines/diffusers/__init__.py
```

- [ ] **Step 2: Write failing tests.**

`tests/engines/diffusers/test_wan_t2v_server_strength.py`:

```python
"""Server-side P1 schema tests: SetStackRequest migration + LoraTarget bounds.

These tests exercise the Pydantic surface only — they do NOT touch the
HTTP app or import diffusers. The server runs in a slim pod env;
test coverage at the schema level catches contract drift without paying
the diffusers import cost.
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
    req = SetStackRequest.model_validate({
        "target": [{"ref": "civitai:1@2", "strength": 0.5}],
        "download_specs": {},
    })
    assert len(req.target) == 1
    assert req.target[0].ref == "civitai:1@2"
    assert req.target[0].strength == 0.5


def test_set_stack_request_legacy_target_refs_promotes_strength_1_0() -> None:
    """Bug: legacy callers (orchestrator running an older release) post
    target_refs: [...] — the migrator must accept and assign strength=1.0
    so warm-pool clients survive the rolling deploy."""
    req = SetStackRequest.model_validate({
        "target_refs": ["civitai:1@2", "hf:org/y:foo.safetensors"],
        "download_specs": {},
    })
    assert [t.ref for t in req.target] == ["civitai:1@2", "hf:org/y:foo.safetensors"]
    assert all(t.strength == 1.0 for t in req.target)


def test_set_stack_request_rejects_both_keys() -> None:
    """Bug: defense-in-depth — a client carrying BOTH legacy and new keys
    is a programming error; refuse rather than guess intent."""
    with pytest.raises((ValidationError, ValueError)) as exc:
        SetStackRequest.model_validate({
            "target": [{"ref": "civitai:1@2", "strength": 1.0}],
            "target_refs": ["civitai:1@2"],
            "download_specs": {},
        })
    msg = str(exc.value)
    assert "target_refs" in msg and "target" in msg


def test_lora_target_strength_out_of_range_rejected() -> None:
    """Bug: server-side bound enforcement matters even when the client
    validates — defense-in-depth against a tool bypassing the kinoforge
    CLI and posting raw to /lora/set_stack."""
    with pytest.raises(ValidationError) as exc:
        SetStackRequest.model_validate({
            "target": [{"ref": "x", "strength": 3.0}],
            "download_specs": {},
        })
    assert "strength" in str(exc.value)
```

- [ ] **Step 3: Run failing tests.**

Run: `pixi run pytest tests/engines/diffusers/test_wan_t2v_server_strength.py -v`
Expected: failures — `SetStackRequest` still has `target_refs: list[str]`.

- [ ] **Step 4: Update `SetStackRequest` in `wan_t2v_server.py`.**

Locate the existing `class SetStackRequest(BaseModel):` (around line 141). Replace it:

```python
class SetStackRequest(BaseModel):
    """Declarative target LoRA stack for the pod.

    Order of ``target`` defines pipeline adapter ordering. Every ref in
    ``target`` that is not already in the pod's inventory must have a
    matching entry in ``download_specs``.

    Each ``LoraTarget`` carries its own strength which is plumbed to
    ``set_adapters(adapter_weights=...)`` server-side (P1, 2026-06-21).

    Migration: ``model_validator(mode="before")`` auto-promotes legacy
    ``target_refs: list[str]`` payloads (where every promoted entry gets
    strength=1.0) during a one-window transition. The shim is removed
    in the release after every in-flight pod has been rolled to a P1+
    image. See spec §12.10 for removal criteria.
    """

    model_config = ConfigDict(extra="forbid")

    target: list[LoraTarget]
    download_specs: dict[str, ArtifactDownloadSpec]

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_target_refs(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        has_legacy = "target_refs" in data
        has_new = "target" in data
        if has_legacy and has_new:
            # Both keys present is a client bug — refuse rather than guess.
            raise ValueError(
                "set_stack request carries BOTH legacy `target_refs` AND new "
                "`target` keys; specify exactly one"
            )
        if has_legacy:
            data["target"] = [
                {"ref": r, "strength": 1.0} for r in data["target_refs"]
            ]
            del data["target_refs"]
        return data
```

Confirm `model_validator` is imported from `pydantic`; if not, add it to the import line.

- [ ] **Step 5: Update every internal reader of `req.target_refs` to use `req.target`.**

Search `wan_t2v_server.py` for `target_refs` reads (the handler body around line 587 onward uses `req.target_refs` heavily). Replace each with the corresponding `req.target` access:

```python
# BEFORE:
#     target_set = set(req.target_refs)
#     to_download_refs = [r for r in req.target_refs if r not in current_set]
# AFTER:
        target_refs_list = [t.ref for t in req.target]
        target_set = set(target_refs_list)
        to_download_refs = [r for r in target_refs_list if r not in current_set]
```

Continue through the rest of `set_stack` handler. The `_reload_pipeline_loras(req.target_refs)` call (around line 710) — defer fix to Task 4 (that call needs LoraTarget objects, not refs, so the change is paired with the set_adapters wiring).

Note: for steps in this task, the SHORT-TERM fix is to compute `target_refs_list = [t.ref for t in req.target]` once at the top of the handler and pass that to the existing `_reload_pipeline_loras(target_refs_list)` call so the handler still works after the schema change. Task 4 will rewrite the call site properly.

- [ ] **Step 6: Run failing tests + existing server tests.**

Run: `pixi run pytest tests/engines/diffusers/test_wan_t2v_server_strength.py tests/engines/test_wan_t2v_server.py -v 2>&1 | tail -50`
Expected: new tests PASSED; existing server tests still PASSED (some may need fixture updates to use new shape — that's allowed in this task).

If any existing test posts `{"target_refs": [...]}` raw, leave them as-is — the migrator handles them. If any constructs `SetStackRequest(target_refs=...)` directly (not via `model_validate`), update those fixtures to use `SetStackRequest.model_validate({"target_refs": ...})` so the validator runs.

- [ ] **Step 7: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 8: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
        tests/engines/diffusers/__init__.py \
        tests/engines/diffusers/test_wan_t2v_server_strength.py
pixi run pre-commit run --files src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/diffusers/__init__.py tests/engines/diffusers/test_wan_t2v_server_strength.py
git commit -m "feat(p1): SetStackRequest tagged-object shape + legacy target_refs promoter"
```

---

## Task 4: Server-side `set_adapters(adapter_weights=)` wiring + `LoraInventoryEntry.last_strength`

**Goal:** Plumb per-LoRA `strength` from `SetStackRequest.target` through `_reload_pipeline_loras`, `_replace_adapter_stack`, and `_load_pipeline` into `pipe.set_adapters(names, adapter_weights=[...])`. Persist the active strength on each inventory entry (`last_strength` field).

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Modify: `tests/engines/diffusers/test_wan_t2v_server_strength.py`

**Acceptance Criteria:**
- [ ] `_replace_adapter_stack(target: list[LoraTarget])` (renamed/migrated from `_reload_pipeline_loras`) calls `pipe.set_adapters(names, adapter_weights=[t.strength for t in target])`.
- [ ] `_load_pipeline(initial_lora_stack=...)` accepts `list[LoraTarget] | None` and calls `set_adapters` with paired strengths on cold-boot.
- [ ] `LoraInventoryEntry` gains `last_strength: float | None = None`.
- [ ] `/lora/inventory` GET endpoint returns the `last_strength` field on each entry.
- [ ] After a successful `set_stack` with strength=1.2, the inventory entry for that ref has `last_strength=1.2`.
- [ ] `_inventory[ref]["last_strength"]` is populated/updated on every successful adapter activation.

**Verify:** `pixi run pytest tests/engines/diffusers/test_wan_t2v_server_strength.py tests/engines/test_wan_t2v_server.py -v` → all PASSED.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

Append to `tests/engines/diffusers/test_wan_t2v_server_strength.py`:

```python
def test_set_stack_passes_adapter_weights_to_set_adapters(monkeypatch):
    """Bug: a future edit drops the adapter_weights kwarg → every LoRA
    silently loads at strength=1.0 regardless of the request."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    calls: list[dict] = []

    class _FakePipe:
        def __init__(self) -> None:
            self._loaded: list[tuple[str, str]] = []

        def unload_lora_weights(self) -> None:
            self._loaded.clear()

        def load_lora_weights(self, path, *, adapter_name) -> None:  # noqa: ARG002
            self._loaded.append((path, adapter_name))

        def set_adapters(self, names, adapter_weights=None) -> None:
            calls.append({"names": list(names), "weights": list(adapter_weights or [])})

    fake_pipe = _FakePipe()
    monkeypatch.setattr(srv, "_pipeline", fake_pipe)
    monkeypatch.setitem(srv._inventory, "civitai:1@2", {
        "ref": "civitai:1@2", "filename": "a.safetensors", "size_bytes": 1,
        "loras_dir_path": "/tmp/a", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_0",
    })
    monkeypatch.setitem(srv._inventory, "civitai:3@4", {
        "ref": "civitai:3@4", "filename": "b.safetensors", "size_bytes": 1,
        "loras_dir_path": "/tmp/b", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_1",
    })

    target = [
        srv.LoraTarget(ref="civitai:1@2", strength=0.5),
        srv.LoraTarget(ref="civitai:3@4", strength=1.2),
    ]
    srv._replace_adapter_stack(target)

    assert len(calls) == 1
    assert calls[0]["weights"] == [0.5, 1.2]
    assert calls[0]["names"] == ["lora_0", "lora_1"]


def test_set_stack_persists_last_strength_on_inventory(monkeypatch):
    """Bug: a future edit forgets to write last_strength → matcher's
    same-refs / different-strength path always sees None → constant
    set_stack re-issues even when nothing changed."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    class _NoopPipe:
        def unload_lora_weights(self) -> None: pass
        def load_lora_weights(self, *a, **kw) -> None: pass
        def set_adapters(self, *a, **kw) -> None: pass

    monkeypatch.setattr(srv, "_pipeline", _NoopPipe())
    monkeypatch.setitem(srv._inventory, "civitai:1@2", {
        "ref": "civitai:1@2", "filename": "a.safetensors", "size_bytes": 1,
        "loras_dir_path": "/tmp/a", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_0",
    })
    srv._replace_adapter_stack([srv.LoraTarget(ref="civitai:1@2", strength=0.7)])
    assert srv._inventory["civitai:1@2"]["last_strength"] == 0.7


def test_inventory_snapshot_surfaces_last_strength(monkeypatch):
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    monkeypatch.setitem(srv._inventory, "civitai:1@2", {
        "ref": "civitai:1@2", "filename": "a.safetensors", "size_bytes": 1,
        "loras_dir_path": "/tmp/a", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_0", "last_strength": 1.2,
    })
    snap = srv._inventory_snapshot()
    assert snap[0].last_strength == 1.2


def test_inventory_entry_without_last_strength_renders_none(monkeypatch):
    """Bug: missing last_strength must default to None (pre-P1 entries),
    NOT raise."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    monkeypatch.setitem(srv._inventory, "civitai:9@9", {
        "ref": "civitai:9@9", "filename": "z.safetensors", "size_bytes": 1,
        "loras_dir_path": "/tmp/z", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_pending_civitai:9@9",
    })
    snap = srv._inventory_snapshot()
    matches = [e for e in snap if e.ref == "civitai:9@9"]
    assert len(matches) == 1
    assert matches[0].last_strength is None
```

- [ ] **Step 2: Run failing tests.**

Run: `pixi run pytest tests/engines/diffusers/test_wan_t2v_server_strength.py -v`
Expected: failures — `_replace_adapter_stack` doesn't exist (still `_reload_pipeline_loras`), `LoraInventoryEntry` has no `last_strength` field, etc.

- [ ] **Step 3: Update `LoraInventoryEntry`.**

In `wan_t2v_server.py`, locate the `class LoraInventoryEntry(BaseModel):` definition. Add the new field:

```python
class LoraInventoryEntry(BaseModel):
    """One row of the pod's LoRA inventory exposed over HTTP."""

    ref: str
    filename: str
    size_bytes: int
    downloaded_at_local: str
    last_used_at_local: str
    adapter_name: str
    last_strength: float | None = None  # P1: NEW; None when never activated
```

- [ ] **Step 4: Migrate `_reload_pipeline_loras` to `_replace_adapter_stack`.**

Locate the existing `_reload_pipeline_loras(target_refs: list[str])` function (around line 329). Replace its signature + body:

```python
def _replace_adapter_stack(target: list[LoraTarget]) -> None:
    """Replace the active pipeline adapter stack with ``target`` in order.

    Calls ``unload_lora_weights()`` first to clear any active adapters,
    then re-loads each target ref as ``lora_{i}``, then ``set_adapters``
    matches target order with paired ``adapter_weights``.

    Persists ``last_strength`` onto each inventory entry so the matcher's
    same-refs / different-strength path observes the current state.

    Args:
        target: Ordered list of :class:`LoraTarget`. ``target[i]`` maps
            to adapter name ``f"lora_{i}"``; ``target[i].strength`` becomes
            ``adapter_weights[i]``.
    """
    pipe = _pipeline
    if hasattr(pipe, "unload_lora_weights"):
        pipe.unload_lora_weights()
    names: list[str] = []
    weights: list[float] = []
    for i, t in enumerate(target):
        entry = _inventory[t.ref]
        name = f"lora_{i}"
        pipe.load_lora_weights(entry["loras_dir_path"], adapter_name=name)
        names.append(name)
        weights.append(t.strength)
        entry["adapter_name"] = name
        entry["last_strength"] = t.strength
    if names:
        pipe.set_adapters(names, adapter_weights=weights)
```

- [ ] **Step 5: Update every caller of the old `_reload_pipeline_loras`.**

In `wan_t2v_server.py`, search for `_reload_pipeline_loras(` calls. Two key sites:

```python
# Set-stack handler (around line 710):
# BEFORE:
#     await asyncio.to_thread(_reload_pipeline_loras, req.target_refs)
# AFTER:
            await asyncio.to_thread(_replace_adapter_stack, req.target)
```

```python
# Rollback path (around line 723):
# BEFORE:
#     await asyncio.to_thread(_reload_pipeline_loras, previous_refs)
# This needs the rollback strength snapshot — DEFER to Task 5.
# For now: leave _reload_pipeline_loras as an internal alias to keep
# the rollback compile-time happy until Task 5 lands the snapshot:
def _reload_pipeline_loras(refs: list[str]) -> None:
    """Legacy alias; rolls each ref forward at strength=1.0.

    Task 5 replaces this in the rollback path with the snapshotted
    target list (LoraTarget) so strength is restored too. This shim
    keeps Task 4 compileable but the rollback path is intentionally
    strength-lossy under this task.
    """
    _replace_adapter_stack([LoraTarget(ref=r, strength=1.0) for r in refs])
```

The earlier `target_refs_list = [t.ref for t in req.target]` shim from Task 3 stays for the eviction / download_specs logic that still needs refs (lines 588-705); only the `_reload_pipeline_loras` call site uses the new shape.

- [ ] **Step 6: Update `_load_pipeline` signature.**

Locate `_load_pipeline(*, initial_lora_stack=None)` (around line 354). Update:

```python
def _load_pipeline(
    *,
    initial_lora_stack: list[LoraTarget] | None = None,
) -> Any:
    """Construct the WanPipeline + optionally preload an initial LoRA stack.

    Args:
        initial_lora_stack: When non-empty, pre-loads each :class:`LoraTarget`
            at cold-boot. ``set_adapters`` is called with paired
            ``adapter_weights`` so cold-boot loads carry strength faithfully.
    """
    pipe_obj = _diffusers_load()
    if initial_lora_stack:
        names: list[str] = []
        weights: list[float] = []
        for i, t in enumerate(initial_lora_stack):
            entry = _inventory[t.ref]
            name = f"lora_{i}"
            pipe_obj.load_lora_weights(entry["loras_dir_path"], adapter_name=name)
            names.append(name)
            weights.append(t.strength)
            entry["adapter_name"] = name
            entry["last_strength"] = t.strength
        if names:
            pipe_obj.set_adapters(names, adapter_weights=weights)
    return pipe_obj
```

- [ ] **Step 7: Update callers of `_load_pipeline`.**

Search `wan_t2v_server.py` for `_load_pipeline(initial_lora_stack=initial)` (around line 476). The variable `initial` was a list of `(ref, download_spec)` tuples — needs to become `list[LoraTarget]`. Trace back to where `initial` is built; if it's read from cfg/state, it must be re-built using the new schema. If it's read from `_inventory` after a cold download phase, build the LoraTarget list with strength from a per-pod cold-boot snapshot.

For the cold-boot path: if `initial` is read from an environment variable (`KINOFORGE_INITIAL_LORA_STACK` or similar JSON), the JSON shape changes to `[{"ref": "...", "strength": ...}, ...]`. Grep for the read site and update the parse:

```python
# At the read site (typically around the lifespan startup):
#   initial = json.loads(os.environ.get("KINOFORGE_INITIAL_LORA_STACK", "[]"))
# becomes:
initial: list[LoraTarget] = [
    LoraTarget.model_validate(item)
    for item in json.loads(os.environ.get("KINOFORGE_INITIAL_LORA_STACK", "[]"))
]
```

(If the env shape carries `(ref, download_spec)` tuples, audit + update. The actual current shape may differ — read the existing code at the call site and adapt.)

- [ ] **Step 8: Run failing tests.**

Run: `pixi run pytest tests/engines/diffusers/test_wan_t2v_server_strength.py tests/engines/test_wan_t2v_server.py -v 2>&1 | tail -60`
Expected: new tests PASS; existing server tests still PASS.

- [ ] **Step 9: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 10: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
        tests/engines/diffusers/test_wan_t2v_server_strength.py
pixi run pre-commit run --files src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/diffusers/test_wan_t2v_server_strength.py
git commit -m "feat(p1): set_adapters(adapter_weights=) + LoraInventoryEntry.last_strength"
```

---

## Task 5: VRAM-OOM rollback restores both refs AND strengths

**Goal:** Extend the existing `_reload_pipeline_loras(previous_refs)` rollback at the OOM path (`wan_t2v_server.py:723`) to snapshot the previous `LoraTarget` list (including strengths) BEFORE the failed swap, and restore that snapshot — refs AND strengths — on rollback. Track `rollback_failed: bool` separately so the response surfaces the distinction.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Modify: `tests/engines/diffusers/test_wan_t2v_server_strength.py`

**Acceptance Criteria:**
- [ ] `set_stack` handler captures `previous_state: list[LoraTarget]` BEFORE any unload/load, including each previous ref's `last_strength` (defaulting to 1.0 if None).
- [ ] On `RuntimeError` containing "OOM" / "out of memory" from `_replace_adapter_stack`: rollback calls `_replace_adapter_stack(previous_state)` so refs AND strengths are restored.
- [ ] On `ValueError` from `_replace_adapter_stack` (PEFT rejection): treated symmetrically to OOM — rollback runs.
- [ ] If the rollback `_replace_adapter_stack(previous_state)` ITSELF raises: handler returns HTTP 500 with `detail = {"phase": "rollback", "rollback_failed": True}`.
- [ ] After a successful rollback, each previous ref's `_inventory[ref]["last_strength"]` equals its pre-swap value.

**Verify:** `pixi run pytest tests/engines/diffusers/test_wan_t2v_server_strength.py -v -k rollback` → 3 passed.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

Append to `tests/engines/diffusers/test_wan_t2v_server_strength.py`:

```python
def test_vram_oom_rollback_restores_strength(monkeypatch):
    """Bug: rollback only restores refs (legacy behavior); strengths get
    silently reset to 1.0 → subsequent generations on the warm pod use
    the wrong strength."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    set_adapters_calls: list[dict] = []

    class _OomThenOkPipe:
        def __init__(self) -> None:
            self.call_count = 0

        def unload_lora_weights(self) -> None:
            pass

        def load_lora_weights(self, *a, **kw) -> None:
            pass

        def set_adapters(self, names, adapter_weights=None) -> None:
            set_adapters_calls.append({
                "names": list(names),
                "weights": list(adapter_weights or []),
            })
            self.call_count += 1
            if self.call_count == 1:
                raise RuntimeError("CUDA out of memory")
            # 2nd call is the rollback — succeed.

    monkeypatch.setattr(srv, "_pipeline", _OomThenOkPipe())
    # Pre-populate inventory with a previous LoRA at strength=0.7
    monkeypatch.setitem(srv._inventory, "civitai:prev@1", {
        "ref": "civitai:prev@1", "filename": "p.safetensors", "size_bytes": 1,
        "loras_dir_path": "/tmp/p", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_0",
        "last_strength": 0.7,
    })

    # Simulate: snapshot previous state, call failing _replace_adapter_stack,
    # then rollback via _replace_adapter_stack(previous_state). The handler
    # under test contains this orchestration — call the function the handler
    # uses to perform that orchestration.

    # The handler's logic is exercised end-to-end in the integration test
    # below; here we verify the helper API directly.
    previous_state = srv._snapshot_inventory_as_targets()
    assert any(t.ref == "civitai:prev@1" and t.strength == 0.7 for t in previous_state)

    new_target = [srv.LoraTarget(ref="civitai:new@1", strength=1.5)]
    monkeypatch.setitem(srv._inventory, "civitai:new@1", {
        "ref": "civitai:new@1", "filename": "n.safetensors", "size_bytes": 1,
        "loras_dir_path": "/tmp/n", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_pending_new",
    })

    with pytest.raises(RuntimeError):
        srv._replace_adapter_stack(new_target)
    # Rollback restores previous state, refs AND strengths
    srv._replace_adapter_stack(previous_state)
    assert srv._inventory["civitai:prev@1"]["last_strength"] == 0.7
    # The rollback set_adapters call carried the same strength
    assert set_adapters_calls[-1]["weights"] == [0.7]


def test_value_error_also_triggers_rollback(monkeypatch):
    """Bug: PEFT can raise ValueError on unknown adapter name; existing
    handler caught only RuntimeError → ValueError leaks unswallowed and
    the pod is left in an unknown adapter state."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    class _AlwaysFailPipe:
        def unload_lora_weights(self) -> None: pass
        def load_lora_weights(self, *a, **kw) -> None: pass
        def set_adapters(self, *a, **kw) -> None:
            raise ValueError("unknown adapter name lora_0")

    monkeypatch.setattr(srv, "_pipeline", _AlwaysFailPipe())
    monkeypatch.setitem(srv._inventory, "civitai:x@1", {
        "ref": "civitai:x@1", "filename": "x.safetensors", "size_bytes": 1,
        "loras_dir_path": "/tmp/x", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_pending_x",
    })
    with pytest.raises(ValueError):
        srv._replace_adapter_stack([srv.LoraTarget(ref="civitai:x@1", strength=0.5)])


def test_snapshot_inventory_as_targets_defaults_missing_last_strength_to_1_0(monkeypatch):
    """Bug: a pre-P1 entry with no last_strength field crashes the snapshot
    → server can't roll back at all because it can't capture state."""
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    monkeypatch.setitem(srv._inventory, "civitai:9@9", {
        "ref": "civitai:9@9", "filename": "z.safetensors", "size_bytes": 1,
        "loras_dir_path": "/tmp/z", "downloaded_at_local": "x",
        "last_used_at_local": "x", "adapter_name": "lora_pending_9",
        # NOTE: no last_strength key
    })
    snap = srv._snapshot_inventory_as_targets()
    matches = [t for t in snap if t.ref == "civitai:9@9"]
    assert matches and matches[0].strength == 1.0
```

- [ ] **Step 2: Run failing tests.**

Run: `pixi run pytest tests/engines/diffusers/test_wan_t2v_server_strength.py -v -k rollback`
Expected: failures — `_snapshot_inventory_as_targets` doesn't exist; ValueError isn't caught.

- [ ] **Step 3: Add the snapshot helper to `wan_t2v_server.py`.**

Add just below the `_inventory_snapshot()` function (around line 161):

```python
def _snapshot_inventory_as_targets() -> list["LoraTarget"]:
    """Return the current inventory as an ordered LoraTarget list.

    Used by ``set_stack``'s VRAM-OOM rollback path: snapshots both refs
    AND last_strength values so the rollback restores the full prior
    state. Missing ``last_strength`` (pre-P1 entry) defaults to 1.0
    per the matcher's same shim.
    """
    return [
        LoraTarget(ref=v["ref"], strength=v.get("last_strength") or 1.0)
        for v in _inventory.values()
    ]
```

- [ ] **Step 4: Update the rollback path in the `set_stack` handler.**

Locate the existing OOM rollback (around line 709-731 in `wan_t2v_server.py`). Replace:

```python
# BEFORE:
#     try:
#         await asyncio.to_thread(_reload_pipeline_loras, req.target_refs)
#     except RuntimeError as e:
#         msg = str(e).lower()
#         if "out of memory" in msg or "oom" in msg:
#             dropped = [r for r in req.target_refs if r not in previous_refs]
#             ...
#             await asyncio.to_thread(_reload_pipeline_loras, previous_refs)
#             return SetStackResponse(...)
#         raise

# AFTER:
        previous_state = _snapshot_inventory_as_targets()
        try:
            await asyncio.to_thread(_replace_adapter_stack, req.target)
        except (RuntimeError, ValueError) as e:
            msg = str(e).lower()
            is_oom = "out of memory" in msg or "oom" in msg
            is_value = isinstance(e, ValueError)
            if not (is_oom or is_value):
                raise
            dropped_refs = [t.ref for t in req.target if t.ref not in {p.ref for p in previous_state}]
            for ref in dropped_refs:
                _inventory.pop(ref, None)
                dropped_spec = req.download_specs.get(ref)
                if dropped_spec is not None:
                    try:
                        (LORAS_DIR / dropped_spec.filename).unlink(missing_ok=True)
                    except OSError:
                        pass
            try:
                await asyncio.to_thread(_replace_adapter_stack, previous_state)
            except Exception:
                # Rollback ITSELF failed — pod is in unknown state. Surface
                # explicitly so the orchestrator can destroy the pod.
                raise HTTPException(
                    status_code=500,
                    detail={
                        "error": "rollback_failed",
                        "phase": "rollback",
                        "rollback_failed": True,
                        "underlying": str(e),
                    },
                ) from None
            return SetStackResponse(
                inventory=_inventory_snapshot(),
                free_bytes=_disk_free_bytes(LORAS_DIR),
                swap_rejected=SwapRejectedDetails(
                    reason="vram_oom" if is_oom else "set_adapters_value_error",
                    target_refs_dropped=dropped_refs,
                ),
            )
```

- [ ] **Step 5: Delete the legacy `_reload_pipeline_loras` shim from Task 4.**

The shim was a compile-time bridge. With rollback now using `_replace_adapter_stack(previous_state)` directly, the shim is unused. Delete it.

- [ ] **Step 6: Run failing tests + existing server tests.**

Run: `pixi run pytest tests/engines/diffusers/test_wan_t2v_server_strength.py tests/engines/test_wan_t2v_server.py -v 2>&1 | tail -60`
Expected: all PASS.

- [ ] **Step 7: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 8: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
        tests/engines/diffusers/test_wan_t2v_server_strength.py
pixi run pre-commit run --files src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/diffusers/test_wan_t2v_server_strength.py
git commit -m "feat(p1): VRAM-OOM rollback restores refs + strengths; broaden to ValueError"
```

---

## Task 6: `resolve_active_lora_stack` + new exception classes

**Goal:** Add the `resolve_active_lora_stack(cfg, vault)` helper in `core/lora.py` that resolves cfg.loras vs vault.loras precedence (vault wins; diverging refs raise `LoraStackConflict`). Add `LoraStackConflict` + `SetStackRequestRejected` exception classes to `core/errors.py`.

**Files:**
- Modify: `src/kinoforge/core/lora.py`
- Modify: `src/kinoforge/core/errors.py`
- Create: `tests/core/test_lora_resolve.py`

**Acceptance Criteria:**
- [ ] `resolve_active_lora_stack(cfg, vault=None)` returns `list(cfg.loras)` when vault is None.
- [ ] `resolve_active_lora_stack(cfg, vault)` returns the vault's LoRAs (upcast to `LoraEntry`, vault-only `label` stripped) when `vault.loras` is non-empty.
- [ ] When both `cfg.loras` and `vault.loras` are populated with the SAME ref set, vault wins and cfg is ignored (no error).
- [ ] When both are populated with DIVERGING ref sets, `LoraStackConflict(KinoforgeError)` raises.
- [ ] `LoraStackConflict` and `SetStackRequestRejected` are subclasses of `KinoforgeError`.

**Verify:** `pixi run pytest tests/core/test_lora_resolve.py -v` → 5 passed.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
"""resolve_active_lora_stack — cfg.loras vs vault.loras precedence."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import LoraStackConflict
from kinoforge.core.lora import LoraEntry, resolve_active_lora_stack
from kinoforge.core.vault import Vault, VaultLoRA


def _vault_with_loras(loras: list[VaultLoRA]) -> Vault:
    return Vault.model_validate({
        "positive_prompt": "x",  # exactly-one-of validator wants a prompt
        "loras": [lo.model_dump() for lo in loras],
    })


class _StubCfg:
    """Minimal stand-in for Config carrying only the .loras attribute."""

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
    vault = _vault_with_loras([
        VaultLoRA(ref="civitai:1@2", strength=0.5, label="secret-style"),
    ])
    result = resolve_active_lora_stack(cfg, vault)
    assert len(result) == 1
    assert result[0].ref == "civitai:1@2"
    assert result[0].strength == 0.5  # vault wins


def test_vault_label_stripped_on_upcast() -> None:
    """Bug: VaultLoRA's vault-only `label` leaks into the LoraEntry list
    sent to the orchestrator → label appears in the HTTP set_stack body
    in violation of ephemeral spec D4."""
    cfg = _StubCfg([])
    vault = _vault_with_loras([
        VaultLoRA(ref="civitai:1@2", strength=0.5, label="my-secret-style"),
    ])
    result = resolve_active_lora_stack(cfg, vault)
    assert result[0].__class__ is LoraEntry
    assert not hasattr(result[0], "label") or getattr(result[0], "label", None) is None


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
```

- [ ] **Step 2: Run failing tests.**

Run: `pixi run pytest tests/core/test_lora_resolve.py -v`
Expected: ImportError — `resolve_active_lora_stack` and `LoraStackConflict` don't exist.

- [ ] **Step 3: Add exception classes to `src/kinoforge/core/errors.py`.**

Locate the `KinoforgeError` class. Append new subclasses (anywhere in the file is fine; keep them together):

```python
class LoraStackConflict(KinoforgeError):
    """cfg.loras and vault.loras both populated with diverging ref sets.

    Resolution: remove cfg.loras and use vault.loras as sole source per
    ephemeral spec D2's "vault is the canonical confidential source" rule.
    """


class SetStackRequestRejected(KinoforgeError):
    """Pod's /lora/set_stack endpoint returned 4xx — usually request shape.

    Defense-in-depth: client validation should have caught the same Pydantic
    bounds, so this firing indicates a contract drift between client and
    server schemas.
    """
```

- [ ] **Step 4: Add `resolve_active_lora_stack` to `src/kinoforge/core/lora.py`.**

Append below `LoraEntry`:

```python
def resolve_active_lora_stack(
    cfg: "Any",
    vault: "Any | None",
) -> list[LoraEntry]:
    """Resolve the final LoRA stack for this run.

    Precedence (matches vault spec D2's "always-on when vault loaded" rule):
      - Vault loaded with non-empty ``vault.loras`` → vault wins entirely.
        Cfg's ``loras:`` block is ignored to keep the "vault is sole
        owner of confidential refs" invariant load-bearing.
      - Vault absent OR vault.loras empty → cfg.loras is the stack.

    When both ``cfg.loras`` and ``vault.loras`` are populated with
    DIVERGING ref sets, ``LoraStackConflict`` raises (defensive — likely
    user mistake).

    Order in the returned list is the activation order (matters for
    set_adapters).

    P3 will extend this signature to accept a CLI override merging
    against the cfg/vault baseline; P1 keeps the contract narrow.

    Args:
        cfg: A loaded :class:`kinoforge.core.config.Config` (typed as
            Any here to avoid a circular import).
        vault: An optional loaded :class:`kinoforge.core.vault.Vault`.

    Returns:
        Ordered list of :class:`LoraEntry`. Vault-only ``label`` field is
        stripped on upcast.

    Raises:
        LoraStackConflict: when both cfg.loras + vault.loras are populated
            and the ref sets differ.
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
    # Vault wins. Drop vault-only `label` on the upcast.
    return [
        LoraEntry(**lo.model_dump(exclude={"label"})) for lo in vault.loras
    ]
```

- [ ] **Step 5: Run failing tests.**

Run: `pixi run pytest tests/core/test_lora_resolve.py -v`
Expected: all 5 PASS.

- [ ] **Step 6: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 7: Commit.**

```bash
git add src/kinoforge/core/lora.py src/kinoforge/core/errors.py \
        tests/core/test_lora_resolve.py
pixi run pre-commit run --files src/kinoforge/core/lora.py src/kinoforge/core/errors.py tests/core/test_lora_resolve.py
git commit -m "feat(p1): resolve_active_lora_stack + LoraStackConflict + SetStackRequestRejected"
```

---

## Task 7: `build_set_stack_request` adapter helper

**Goal:** Add `build_set_stack_request(active_stack: list[LoraEntry], *, download_specs)` to `src/kinoforge/_adapters.py`. Bridges the core schema (`LoraEntry`) and the server schema (`LoraTarget`) without forcing the orchestrator to depend on the server module's internals.

**Files:**
- Modify: `src/kinoforge/_adapters.py`
- Create: `tests/test_adapters_build_set_stack_request.py`

**Acceptance Criteria:**
- [ ] `build_set_stack_request([LoraEntry(ref="a", strength=0.5), LoraEntry(ref="b", strength=1.2)], download_specs={"b": ds})` returns a `SetStackRequest` whose `target` has 2 entries in input order, strengths paired correctly.
- [ ] Empty `active_stack` returns `SetStackRequest(target=[], download_specs={...})`.

**Verify:** `pixi run pytest tests/test_adapters_build_set_stack_request.py -v` → 3 passed.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
"""build_set_stack_request: bridge LoraEntry (core) → LoraTarget (server)."""

from __future__ import annotations

from kinoforge._adapters import build_set_stack_request
from kinoforge.core.lora import LoraEntry
from kinoforge.engines.diffusers.servers.wan_t2v_server import (
    ArtifactDownloadSpec,
    SetStackRequest,
)


def _ds() -> ArtifactDownloadSpec:
    return ArtifactDownloadSpec(
        url="https://example.com/x.safetensors",
        filename="x.safetensors",
        size_hint=1,
    )


def test_pairs_strengths_in_order() -> None:
    """Bug: a future edit zips the list out of order → strengths land on
    the wrong refs and the wrong adapter weights apply."""
    stack = [
        LoraEntry(ref="a", strength=0.5),
        LoraEntry(ref="b", strength=1.2),
    ]
    req = build_set_stack_request(stack, download_specs={})
    assert isinstance(req, SetStackRequest)
    assert [t.ref for t in req.target] == ["a", "b"]
    assert [t.strength for t in req.target] == [0.5, 1.2]


def test_empty_stack_returns_empty_target() -> None:
    """Bug: a future edit treats empty stack as a contract violation;
    empty MUST be valid (unloads every active adapter on the pod)."""
    req = build_set_stack_request([], download_specs={})
    assert req.target == []


def test_download_specs_pass_through_unchanged() -> None:
    stack = [LoraEntry(ref="b", strength=1.0)]
    ds = _ds()
    req = build_set_stack_request(stack, download_specs={"b": ds})
    assert req.download_specs == {"b": ds}
```

- [ ] **Step 2: Run failing tests.**

Run: `pixi run pytest tests/test_adapters_build_set_stack_request.py -v`
Expected: ImportError — `build_set_stack_request` doesn't exist.

- [ ] **Step 3: Add `build_set_stack_request` to `src/kinoforge/_adapters.py`.**

Locate the existing `build_provider_for` / `build_heartbeat_endpoint_for` helpers. Add nearby:

```python
def build_set_stack_request(
    active_stack: "list[LoraEntry]",
    *,
    download_specs: "dict[str, ArtifactDownloadSpec]",
) -> "SetStackRequest":
    """Adapt a resolved LoRA stack to the server's request schema.

    Bridges the kinoforge.core.lora schema (LoraEntry) and the pod-side
    server schema (LoraTarget). Two distinct Pydantic models on purpose
    (P1 spec §6.3): server runs in a slim pod env without
    kinoforge.core available, so the wire format is its own contract.

    See docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §9.2.

    Args:
        active_stack: Ordered LoRA list resolved by
            ``kinoforge.core.lora.resolve_active_lora_stack``.
        download_specs: Per-ref download metadata for any ref the pod
            does not yet have on disk. Empty when every ref is already
            present in the pod's inventory.

    Returns:
        A :class:`SetStackRequest` ready to POST to the pod's
        ``/lora/set_stack`` endpoint.
    """
    from kinoforge.engines.diffusers.servers.wan_t2v_server import (
        LoraTarget,
        SetStackRequest,
    )

    return SetStackRequest(
        target=[
            LoraTarget(ref=lo.ref, strength=lo.strength) for lo in active_stack
        ],
        download_specs=download_specs,
    )
```

If the top of `_adapters.py` doesn't already TYPE_CHECKING-import `LoraEntry` etc., add string annotations OR move the import inside the function (as shown) to avoid module-level diffusers import.

- [ ] **Step 4: Run tests.**

Run: `pixi run pytest tests/test_adapters_build_set_stack_request.py -v`
Expected: all 3 PASS.

- [ ] **Step 5: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/_adapters.py tests/test_adapters_build_set_stack_request.py
pixi run pre-commit run --files src/kinoforge/_adapters.py tests/test_adapters_build_set_stack_request.py
git commit -m "feat(p1): build_set_stack_request adapter helper"
```

---

## Task 8: DiffusersEngine integration uses `build_set_stack_request`

**Goal:** Update `src/kinoforge/engines/diffusers/__init__.py` (the DiffusersEngine cold-boot and warm-attach paths) to call `build_set_stack_request` instead of building `{target_refs: [...]}` manually. Source the active stack via `resolve_active_lora_stack(cfg, vault)`.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py`
- Modify: `tests/engines/test_diffusers_engine.py` (if existing tests pin the legacy shape)
- Create or extend: `tests/engines/diffusers/test_diffusers_engine_strength_integration.py`

**Acceptance Criteria:**
- [ ] DiffusersEngine builds its set_stack POST body via `build_set_stack_request(resolve_active_lora_stack(cfg, vault), download_specs=...)`.
- [ ] When vault is None, the active stack comes from `cfg.loras`.
- [ ] When vault is set, the active stack comes from `vault.loras` (with `label` stripped).
- [ ] The orchestrator's call site is the SOLE producer of the request shape — no `{target_refs: ...}` literal remains in `kinoforge.engines.diffusers.*` after this task.

**Verify:** `pixi run pytest tests/engines/diffusers/ tests/engines/test_diffusers_engine.py -v 2>&1 | tail -40` → all PASSED. Also: `rg -n "target_refs" src/kinoforge/engines/diffusers/ | grep -v wan_t2v_server.py | grep -v 'migrate_legacy'` → empty.

**Steps:**

- [ ] **Step 1: Map every existing producer of the request shape inside the engine.**

```bash
rg -n "target_refs|\"target\":\s*\[" /workspace/src/kinoforge/engines/diffusers/
```

Inspect each hit. Note the file + line of every site that builds the request payload (typically inside `provision`, `_post_set_stack`, or similar).

- [ ] **Step 2: Write the failing integration test.**

```python
"""DiffusersEngine set_stack integration: cfg + vault → wire body."""

from __future__ import annotations

from typing import Any

import kinoforge.engines.fake  # noqa: F401 — self-register
from kinoforge.core.config import Config
from kinoforge.core.lora import LoraEntry
from kinoforge.core.vault import Vault, VaultLoRA


def _build_minimum_cfg(loras_block: list[dict[str, Any]]) -> Config:
    return Config.model_validate({
        "engine": {"kind": "diffusers", "precision": "fp16"},
        "models": [
            {"ref": "hf:Wan-AI/x", "kind": "base", "target": "diffusion_models"},
        ],
        "loras": loras_block,
        "compute": {
            "provider": "runpod",
            "image": "kinoforge:latest",
            "lifecycle": {"budget": 1.0},
        },
    })


def test_engine_resolves_active_stack_from_cfg_when_no_vault(monkeypatch):
    """Bug: engine ignores cfg.loras → cold-boot pod has no LoRAs loaded."""
    from kinoforge._adapters import build_set_stack_request
    from kinoforge.core.lora import resolve_active_lora_stack

    cfg = _build_minimum_cfg([{"ref": "civitai:1@2", "strength": 0.5}])
    active = resolve_active_lora_stack(cfg, vault=None)
    req = build_set_stack_request(active, download_specs={})
    assert [t.ref for t in req.target] == ["civitai:1@2"]
    assert [t.strength for t in req.target] == [0.5]


def test_engine_resolves_active_stack_from_vault_when_present():
    """Bug: vault present but engine still uses cfg.loras → confidential
    LoRAs never get loaded."""
    from kinoforge._adapters import build_set_stack_request
    from kinoforge.core.lora import resolve_active_lora_stack

    cfg = _build_minimum_cfg([])
    vault = Vault.model_validate({
        "positive_prompt": "x",
        "loras": [{"ref": "civitai:secret@1", "strength": 0.7, "label": "secret"}],
    })
    active = resolve_active_lora_stack(cfg, vault)
    req = build_set_stack_request(active, download_specs={})
    assert [t.ref for t in req.target] == ["civitai:secret@1"]
    assert [t.strength for t in req.target] == [0.7]
```

- [ ] **Step 3: Run failing tests.**

The tests above exercise `build_set_stack_request` + `resolve_active_lora_stack` together; they should PASS already from Tasks 6+7. Their value is as a regression fence for this task — but the engine-side integration is still legacy.

To exercise the engine integration directly, find the engine's `provision` method and the set_stack POST call. The actual engine integration test depends on its current structure; if the engine call site has a unit-testable helper that builds the wire body, write a test that asserts that helper now uses `build_set_stack_request`. If the engine inlines the body-building inline, refactor it to call the helper.

- [ ] **Step 4: Refactor the engine call site.**

Open `src/kinoforge/engines/diffusers/__init__.py`. Find the function that issues the `/lora/set_stack` POST during `provision`. Replace any inline `{"target_refs": [...]}` construction:

```python
# BEFORE (sketched; actual code may differ):
#     refs = [m.ref for m in cfg.models if m.kind == "lora"]
#     body = {"target_refs": refs, "download_specs": specs}

# AFTER:
        from kinoforge._adapters import build_set_stack_request
        from kinoforge.core.lora import resolve_active_lora_stack

        active_stack = resolve_active_lora_stack(cfg, _current_vault())
        req = build_set_stack_request(active_stack, download_specs=specs)
        body = req.model_dump()
```

`_current_vault()` is the existing helper from the ephemeral/vault implementation; if not exposed, use the existing pattern the codebase uses to read the active vault during provision.

- [ ] **Step 5: Run targeted regressions.**

```bash
pixi run pytest tests/engines/ tests/engines/diffusers/ -v 2>&1 | tail -50
```

Expected: all PASS. Some existing tests may pin the legacy `target_refs` shape — update those fixtures to use the new shape (or accept the migrator's auto-promotion).

- [ ] **Step 6: Lockdown grep.**

```bash
rg -n 'target_refs' src/kinoforge/engines/diffusers/ | grep -v 'wan_t2v_server.py' | grep -v 'migrate_legacy'
```

Expected: empty output. The only `target_refs` reference inside `engines/diffusers/` should be the legacy migrator inside `wan_t2v_server.py::SetStackRequest`.

- [ ] **Step 7: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 8: Commit.**

```bash
git add src/kinoforge/engines/diffusers/__init__.py \
        tests/engines/diffusers/test_diffusers_engine_strength_integration.py
pixi run pre-commit run --files src/kinoforge/engines/diffusers/__init__.py tests/engines/diffusers/test_diffusers_engine_strength_integration.py
git commit -m "feat(p1): DiffusersEngine uses build_set_stack_request + resolve_active_lora_stack"
```

---

## Task 9: Warm-attach matcher checks refs AND strength

**Goal:** Extend `is_stack_match(active, target)` in `src/kinoforge/core/warm_reuse/matcher.py` to compare BOTH the ref-order list AND the per-LoRA strength (via `math.isclose(rel_tol=1e-6)`). Treat pre-P1 inventory entries (`last_strength is None`) as 1.0 during the transition window.

**Files:**
- Modify: `src/kinoforge/core/warm_reuse/matcher.py`
- Create: `tests/core/test_warm_reuse_matcher_strength.py`

**Acceptance Criteria:**
- [ ] `is_stack_match(active, target)` returns True when refs AND strengths agree.
- [ ] Same refs, different strength → returns False.
- [ ] Same refs, strengths agree to `rel_tol=1e-6` (JSON float drift) → True.
- [ ] Inventory entry with `last_strength=None` is compared as 1.0.

**Verify:** `pixi run pytest tests/core/test_warm_reuse_matcher_strength.py -v` → 4 passed.

**Steps:**

- [ ] **Step 1: Write the failing tests.**

```python
"""Warm-attach matcher: refs AND strength equality (P1)."""

from __future__ import annotations

from kinoforge.core.lora import LoraEntry
from kinoforge.core.warm_reuse.matcher import is_stack_match
from kinoforge.engines.diffusers.servers.wan_t2v_server import LoraInventoryEntry


def _inv(ref: str, last_strength: float | None) -> LoraInventoryEntry:
    return LoraInventoryEntry(
        ref=ref, filename=f"{ref}.bin", size_bytes=1,
        downloaded_at_local="x", last_used_at_local="x",
        adapter_name="lora_0", last_strength=last_strength,
    )


def test_same_refs_same_strength_is_match() -> None:
    active = [_inv("civitai:1@2", 0.5)]
    target = [LoraEntry(ref="civitai:1@2", strength=0.5)]
    assert is_stack_match(active, target) is True


def test_same_refs_different_strength_not_match() -> None:
    """Bug: a future edit drops the strength check → user can't iterate
    on strength because the matcher silently keeps the old weight."""
    active = [_inv("civitai:1@2", 0.5)]
    target = [LoraEntry(ref="civitai:1@2", strength=1.5)]
    assert is_stack_match(active, target) is False


def test_isclose_tolerance_swallows_json_float_drift() -> None:
    """Bug: a future edit uses == instead of math.isclose → 0.1 round-
    tripped through JSON shows up as 0.10000000000000001 and the matcher
    schedules an unnecessary set_stack."""
    active = [_inv("civitai:1@2", 0.10000000000000001)]
    target = [LoraEntry(ref="civitai:1@2", strength=0.1)]
    assert is_stack_match(active, target) is True


def test_missing_last_strength_treated_as_1_0() -> None:
    """Bug: pre-P1 pod inventory entries (no last_strength) crash the
    matcher or compare as 0 → every warm-attach against a pre-P1 pod
    falsely fails to match."""
    active = [_inv("civitai:1@2", None)]
    target = [LoraEntry(ref="civitai:1@2", strength=1.0)]
    assert is_stack_match(active, target) is True
```

- [ ] **Step 2: Run failing tests.**

Run: `pixi run pytest tests/core/test_warm_reuse_matcher_strength.py -v`
Expected: failures — `is_stack_match` doesn't take this signature yet, or doesn't exist.

- [ ] **Step 3: Find / update / add `is_stack_match`.**

Open `src/kinoforge/core/warm_reuse/matcher.py`. If `is_stack_match` already exists, update its signature + body. If not, add:

```python
def is_stack_match(
    active: "list[LoraInventoryEntry]",
    target: "list[LoraEntry]",
) -> bool:
    """Return True iff the pod's active stack matches the run's target.

    P1: equality requires BOTH the ref-order list AND the per-LoRA
    strength to agree. ``math.isclose(rel_tol=1e-6)`` swallows JSON
    round-trip float drift. Pre-P1 inventory entries with
    ``last_strength=None`` are compared as 1.0.

    Args:
        active: Pod's current inventory snapshot (refs in adapter order).
        target: Run's resolved LoRA stack from
            ``resolve_active_lora_stack``.

    Returns:
        True iff refs match in order AND each pair's strength is close
        enough for ``math.isclose(rel_tol=1e-6)``.
    """
    import math

    if [a.ref for a in active] != [t.ref for t in target]:
        return False
    return all(
        math.isclose(
            a.last_strength if a.last_strength is not None else 1.0,
            t.strength,
            rel_tol=1e-6,
        )
        for a, t in zip(active, target, strict=True)
    )
```

If `is_stack_match` already existed with a different signature, audit every caller — the new signature returns `bool` and takes the two named arguments. Update callers if needed.

- [ ] **Step 4: Run failing tests.**

Run: `pixi run pytest tests/core/test_warm_reuse_matcher_strength.py -v`
Expected: all 4 PASS.

- [ ] **Step 5: Run the broader warm-reuse + matcher regression.**

```bash
pixi run pytest tests/core/test_warm_reuse* tests/core/test_b3_* -v 2>&1 | tail -40
```

Expected: all PASS. If existing matcher tests assumed refs-only equality, they need to be updated to populate `last_strength=1.0` (or accept the new behavior).

- [ ] **Step 6: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 7: Commit.**

```bash
git add src/kinoforge/core/warm_reuse/matcher.py \
        tests/core/test_warm_reuse_matcher_strength.py
pixi run pre-commit run --files src/kinoforge/core/warm_reuse/matcher.py tests/core/test_warm_reuse_matcher_strength.py
git commit -m "feat(p1): warm-attach matcher checks refs AND strength via math.isclose"
```

---

## Task 10: Extend `tests/test_no_unredacted_writes.py` AST scan coverage (P1-CI-1)

**Goal:** Tell the AST scan about the new P1 write sites that touch `.ref` (these MUST be in the scan's "watched modules" list so any future write site that emits raw refs trips the test).

**Files:**
- Modify: `tests/test_no_unredacted_writes.py`

**Acceptance Criteria:**
- [ ] `tests/test_no_unredacted_writes.py` covers each of the following modules / call sites:
  - `kinoforge/_adapters.py::build_set_stack_request`
  - `kinoforge/core/lora.py::resolve_active_lora_stack`
  - `kinoforge/core/warm_reuse/matcher.py::is_stack_match`
  - `kinoforge/engines/diffusers/servers/wan_t2v_server.py::set_stack` (existing — verify)
- [ ] Adding a fixture module that reads `.ref` outside the allow-list trips the scan.

**Verify:** `pixi run pytest tests/test_no_unredacted_writes.py -v` → all PASSED.

**Steps:**

- [ ] **Step 1: Read the existing scan.**

```bash
wc -l /workspace/tests/test_no_unredacted_writes.py
```

Open it. Find the "watched modules" / "allowlist" / "coverage list" constants — names vary by implementation. Likely structure:

```python
# Probably:
_COVERED_PATHS = [
    "src/kinoforge/core/...",
    ...
]
_ALLOWLIST = [
    "src/kinoforge/core/warm_reuse/redaction.py",
    ...
]
```

- [ ] **Step 2: Add P1 modules to the coverage list.**

Extend the coverage list with the four new write-site modules. Example diff (paths may differ from actual constants):

```python
# Append:
_COVERED_PATHS += [
    "src/kinoforge/_adapters.py",
    "src/kinoforge/core/lora.py",
    "src/kinoforge/core/warm_reuse/matcher.py",
]
```

`wan_t2v_server.py` was already in the existing coverage; verify.

- [ ] **Step 3: Add a fence test that the scan trips on a new bad write site.**

Append:

```python
def test_scan_trips_on_synthetic_bad_write_site(tmp_path):
    """Bug: a future edit relaxes the AST scan and a real LoRA-ref write
    site silently gets a pass. Synthetic test that proves the scan still
    has teeth."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def fn(lo):\n"
        "    import logging\n"
        "    logging.info('lora ref: %s', lo.ref)\n"
    )
    findings = _scan_file_for_unredacted_writes(bad)  # name per existing impl
    assert findings, "scan should have flagged the .ref read"
```

(Use the existing scanner function name — substitute `_scan_file_for_unredacted_writes` with whatever the test module actually exposes.)

- [ ] **Step 4: Run the test.**

Run: `pixi run pytest tests/test_no_unredacted_writes.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 6: Commit.**

```bash
git add tests/test_no_unredacted_writes.py
pixi run pre-commit run --files tests/test_no_unredacted_writes.py
git commit -m "test(p1): extend redaction AST scan with P1 write-site coverage"
```

---

## Task 11: In-tree cfg sweep — migrate every `kind: lora` to top-level `loras:` block

**Goal:** Migrate every example cfg that ships a `kind: lora` model entry to the new `loras:` top-level block. One commit; covers `wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml`, `wan22-14b-lora-flexible-warm-reuse-release.yaml`, `wan.yaml`, and any other cfg the grep surfaces.

**Files:**
- Modify: every cfg under `examples/configs/` containing `kind: lora`.

**Acceptance Criteria:**
- [ ] `rg -n 'kind: lora' examples/configs/` returns empty.
- [ ] Every migrated cfg loads via `load_config` with ZERO `DeprecationWarning` fired.
- [ ] Every migrated cfg derives the SAME capability_key as the pre-migration version (regression fence).

**Verify:**
```bash
rg -n 'kind: lora' examples/configs/
pixi run pytest tests/test_examples.py -v 2>&1 | tail -20
```
Expected: grep empty; all test_examples tests PASS.

**Steps:**

- [ ] **Step 1: Enumerate cfgs.**

```bash
rg -n 'kind: lora' /workspace/examples/configs/
```

Capture the list. Likely cfgs: `wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml`, `wan22-14b-lora-flexible-warm-reuse-release.yaml`, possibly more.

- [ ] **Step 2: For each cfg, derive its pre-migration capability_key.**

For each cfg path `P`:

```bash
pixi run python -c "
from kinoforge.core.config import load_config
import warnings
warnings.simplefilter('ignore', DeprecationWarning)
cfg = load_config('$P')
print('$P', cfg.capability_key().derive())
" >> /tmp/pre_migration_keys.txt
```

This baselines the keys. The post-migration sweep must match.

- [ ] **Step 3: Migrate each cfg.**

For each cfg, edit by hand. Example migration of `wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml`:

```yaml
# BEFORE:
models:
  - ref: "hf:Wan-AI/Wan2.1-T2V-1.3B"
    kind: base
    target: diffusion_models
  - ref: "civitai:1234@5678"
    kind: lora
    target: loras
  - ref: "hf:Org/x:foo.safetensors"
    kind: lora
    target: loras

# AFTER:
models:
  - ref: "hf:Wan-AI/Wan2.1-T2V-1.3B"
    kind: base
    target: diffusion_models
loras:
  - ref: "civitai:1234@5678"
  - ref: "hf:Org/x:foo.safetensors"
```

If any cfg's LoRA had a `sha256` entry, carry it onto the new block. Strength is OMITTED (defaults to 1.0) — that's the cfg-author's pre-migration intent.

- [ ] **Step 4: Verify no `kind: lora` survives.**

```bash
rg -n 'kind: lora' /workspace/examples/configs/
```

Expected: empty.

- [ ] **Step 5: Verify capability_key stability.**

For each cfg `P`:

```bash
pixi run python -c "
from kinoforge.core.config import load_config
cfg = load_config('$P')
print('$P', cfg.capability_key().derive())
" >> /tmp/post_migration_keys.txt
```

Diff:

```bash
diff /tmp/pre_migration_keys.txt /tmp/post_migration_keys.txt
```

Expected: empty diff. Any mismatch is a P1-Identity invariant violation — fix before commit.

- [ ] **Step 6: Run example-loading regression.**

```bash
pixi run pytest tests/test_examples.py -v 2>&1 | tail -20
```

Expected: every example loads cleanly. Capture any cfg that warns; fix.

Verify no DeprecationWarning fires by re-running with `-W error::DeprecationWarning`:

```bash
pixi run pytest tests/test_examples.py -W error::DeprecationWarning -v 2>&1 | tail -20
```

Expected: PASSES — any cfg that still warns becomes a hard fail.

- [ ] **Step 7: Lint + pre-commit.**

```bash
pixi run pre-commit run --files examples/configs/*.yaml
```

Expected: clean.

- [ ] **Step 8: Commit.**

```bash
git add examples/configs/*.yaml
git commit -m "chore(p1): migrate example cfgs to top-level loras: block"
```

---

## Task 12: Tier-3 live smoke RED scaffold — Wan 2.1 1.3B strength variation

**Goal:** Land the Tier-3 live smoke test file as a RED/xfail scaffold BEFORE any live spend (per CLAUDE.md durability rule "Commit RED scaffolds before any live spend"). The scaffold defines the test shape, fixtures, polling cadence, and pass/fail criteria — but the actual `pytest.mark.skip` keeps it from firing until Task 13's live-budget commit lifts the skip.

**Files:**
- Create: `tests/smoke/live_wan21/test_lora_strength_variation.py`

**Acceptance Criteria:**
- [ ] File exists, lints clean, imports the shared `_smoke_harness` modules (matching the existing `test_lora_swap_matrix.py` pattern in the same directory).
- [ ] Test function `test_strength_variation_warm_reuse_diff` is defined but marked `@pytest.mark.skip(reason="RED scaffold — Task 13 lifts to enable live run")`.
- [ ] The skip reason names Task 13 by number so it's discoverable on `pytest --collect-only`.
- [ ] Test body contains the full A/B harness — cold-boot run, warm-reuse run with different strength, pixel-diff assertion (the actual numeric threshold is a TBD-to-be-calibrated comment per spec §11.6 with the formula: `final_threshold = baseline_diff * 0.30`).

**Verify:** `pixi run pytest tests/smoke/live_wan21/test_lora_strength_variation.py --collect-only -v` → 1 collected, 1 skipped.

**Steps:**

- [ ] **Step 1: Write the scaffold.**

```python
"""Tier-3 live smoke: Wan 2.1 1.3B + 1 LoRA at strength=0.5 vs strength=1.5.

P1 verification — see docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §11.6.

RED scaffold per CLAUDE.md durability rule: committed BEFORE any live
spend. Task 13 lifts the skip after a live-budget commit.

Cost envelope: ~$0.30 against the $20 session authorization.

Harness shared with tests/smoke/live_wan21/test_lora_swap_matrix.py
(UA, api_key, URLError retry, leak sweep).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests._smoke_harness import budget, civitai, matrix, runpod_lifecycle  # noqa: F401

# RED scaffold gate — Task 13 deletes this skip after the per-task
# live-budget commit.
pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
        reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod smoke",
    ),
    pytest.mark.skip(reason="RED scaffold — Task 13 lifts to enable live run"),
]

REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml"
PROMPT_FILE = REPO / "examples/configs/prompts/field-realistic.txt"
_TAG = "kinoforge-smoke-tier-3-strength"
_BUDGET_CAP = 0.30
_SEED = 42
# Pixel-diff floor: calibrated per spec §11.6.
# Baseline (strength=1.0 reference clip) RMS = TBD; floor = baseline * 0.30.
# Task 13 establishes the actual baseline; if no calibration is yet
# recorded, the floor defaults to 0.05 with a logged WARN.
_PIXEL_DIFF_FLOOR = float(os.environ.get("KINOFORGE_P1_PIXEL_DIFF_FLOOR", "0.05"))


def _extract_pod_id(log_text: str) -> str:
    m = re.search(r"running provisioner\.provision for instance (\w+)", log_text)
    assert m is not None, f"no pod id in:\n{log_text[-2000:]}"
    return m.group(1)


def _run_generate(prompt: str, strength: float, log_path: Path, instance_id: str | None) -> Path:
    """Run a single kinoforge generate; return the produced clip path."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # The cfg has one LoRA; override its strength via environment so we
    # don't have to patch the YAML. Engine reads the override and
    # threads through resolve_active_lora_stack.
    env = {**os.environ, "KINOFORGE_P1_STRENGTH_OVERRIDE": str(strength)}
    args = [
        "pixi", "run", "python", "-m", "kinoforge", "generate",
        "--config", str(CFG),
        "--mode", "t2v",
        "--prompt", prompt,
        "--no-output-dir",
    ]
    if instance_id is not None:
        args += ["--instance-id", instance_id]
    with log_path.open("w") as f:
        proc = subprocess.run(args, env=env, stdout=f, stderr=subprocess.STDOUT, check=False)  # noqa: S603
    assert proc.returncode == 0, f"generate failed (rc={proc.returncode}); see {log_path}"
    output_dir = REPO / "output"
    clips = sorted(output_dir.glob(f"*.mp4"), key=lambda p: p.stat().st_mtime)
    assert clips, "no output clips found"
    return clips[-1]


def _rms_diff_first_frame_yuv(a: Path, b: Path) -> float:
    """Decode first frame of each clip to YUV; return RMS diff."""
    import numpy as np
    from PIL import Image
    # Decode via ffmpeg; the smoke harness's _ffmpeg_decode_first_frame
    # encapsulates this. Use whatever helper exists; if none, inline a
    # subprocess call to ffmpeg.
    from tests._smoke_harness.video import decode_first_frame_yuv  # type: ignore[import-not-found]
    ya = decode_first_frame_yuv(a)
    yb = decode_first_frame_yuv(b)
    diff = (ya.astype(np.float64) - yb.astype(np.float64))
    rms = float(np.sqrt((diff ** 2).mean()))
    return rms / 255.0  # normalize to [0, 1]


def test_strength_variation_warm_reuse_diff(tmp_path) -> None:
    """A: cold-boot at strength=0.5. B: warm-reuse at strength=1.5.

    Assertions:
      - Both runs produce MP4s.
      - Run B reuses the warm pod (no second cold-boot).
      - Pod's /lora/inventory reports last_strength=1.5 after Run B.
      - Pixel-diff RMS between Run A clip and Run B clip exceeds
        _PIXEL_DIFF_FLOOR (calibrated per spec §11.6).
    """
    prompt = PROMPT_FILE.read_text().strip()
    log_a = tmp_path / "run_a_cold_05.log"
    log_b = tmp_path / "run_b_warm_15.log"

    # Run A — cold boot at strength=0.5
    clip_a = _run_generate(prompt, strength=0.5, log_path=log_a, instance_id=None)
    pod_id = _extract_pod_id(log_a.read_text())

    try:
        # Run B — same pod, strength=1.5 (matcher schedules set_stack)
        clip_b = _run_generate(prompt, strength=1.5, log_path=log_b, instance_id=pod_id)

        # Pod's last_strength should now read 1.5
        from kinoforge.providers.runpod import RunPodProvider
        provider = RunPodProvider()
        endpoints = provider.endpoints(provider.get_instance(pod_id))
        import urllib.request, json
        with urllib.request.urlopen(f"{endpoints['http']}/lora/inventory") as resp:  # noqa: S310
            inv = json.loads(resp.read())
        last_strengths = {e["ref"]: e["last_strength"] for e in inv["inventory"]}
        assert 1.5 in last_strengths.values(), f"inventory: {last_strengths}"

        # Pixel-diff: strength change MUST move the output
        diff = _rms_diff_first_frame_yuv(clip_a, clip_b)
        assert diff > _PIXEL_DIFF_FLOOR, (
            f"strength=0.5 vs 1.5 RMS diff {diff:.4f} below floor "
            f"{_PIXEL_DIFF_FLOOR:.4f} — set_adapters likely ignored "
            f"adapter_weights"
        )
    finally:
        # Tear down the pod regardless of test outcome.
        subprocess.run(  # noqa: S603
            ["pixi", "run", "kinoforge", "destroy", "--id", pod_id],
            check=False,
        )
```

- [ ] **Step 2: Run collection to confirm the scaffold collects + skips.**

Run: `pixi run pytest tests/smoke/live_wan21/test_lora_strength_variation.py --collect-only -v`
Expected: `1 collected`. With `pixi run pytest tests/smoke/live_wan21/test_lora_strength_variation.py -v` → `1 skipped`.

- [ ] **Step 3: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean. Some imports inside the test (e.g. `tests._smoke_harness.video.decode_first_frame_yuv`) may not exist yet — if so, swap to whichever harness function does exist in `tests/_smoke_harness/`. The scaffold's import errors are acceptable AT THIS task because the skip prevents execution; resolution happens in Task 13.

- [ ] **Step 4: Commit (RED scaffold, before any live spend).**

```bash
git add tests/smoke/live_wan21/test_lora_strength_variation.py
pixi run pre-commit run --files tests/smoke/live_wan21/test_lora_strength_variation.py
git commit -m "test(p1): Tier-3 live smoke RED scaffold — Wan 2.1 1.3B strength variation"
```

---

## Task 13: Execute Tier-3 live smoke + verify GREEN

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the brainstorming session (decision D6: "All of them. Unit + integration + Tier-3 live smoke on Wan 2.1 1.3B + Tier-4 live smoke on Wan 2.2 14B"). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Run the Tier-3 live smoke against a real Wan 2.1 1.3B RunPod pod. Confirm strength wiring honors `adapter_weights=` end-to-end via the pixel-diff floor + last_strength inventory check.

**Files:**
- Modify: `tests/smoke/live_wan21/test_lora_strength_variation.py` (lift the RED skip)

**Acceptance Criteria:**
- [ ] `pixi run preflight` returns exit 0 (RUNPOD/HF creds present, zero active pods, clean tree).
- [ ] Test PASSES under `KINOFORGE_LIVE_TESTS=1`.
- [ ] Pod's `/lora/inventory` reports `last_strength=1.5` after Run B (locks D9 wiring).
- [ ] Pixel-diff RMS between strength=0.5 and strength=1.5 clips exceeds the calibrated floor (per spec §11.6: `final_threshold = baseline_RMS * 0.30`).
- [ ] Spend within $0.30 ± slop. Cumulative spend logged to PROGRESS.md.
- [ ] Pod monitored via RunPod `runtime.gpus.gpuUtilPercent` every 60–90s during run; idle for 3 consecutive probes → kill + fail fast (per CLAUDE.md "Live smoke monitoring" rule).
- [ ] Pod torn down cleanly after test (`kinoforge destroy` returns clean).

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/smoke/live_wan21/test_lora_strength_variation.py -v -s` → 1 PASSED.

**Steps:**

- [ ] **Step 1: Pre-flight.**

```bash
pixi run preflight
```

Expected: exit 0. If it fails (active pods, dirty tree, missing creds), resolve before continuing.

- [ ] **Step 2: Calibrate the pixel-diff floor.**

Before lifting the RED skip, calibrate the floor using a no-LoRA reference baseline. Run the cfg with strength=1.0 (cold-boot, no warm reuse) to establish baseline RMS:

```bash
# Capture a baseline-pair: same cfg / same seed, run twice cold (no LoRA strength change)
# RMS of two identical-strength runs establishes the floor; multiply by 30%
KINOFORGE_LIVE_TESTS=1 KINOFORGE_P1_STRENGTH_OVERRIDE=1.0 \
  pixi run python -m kinoforge generate --config examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml --mode t2v --prompt "$(cat examples/configs/prompts/field-realistic.txt)"
```

Capture the produced clip. Run a SECOND time with `--no-reuse` (new pod) at the SAME strength=1.0. Compute RMS between the two clips — this is the natural torch/cuda noise floor.

Then `0.30 * baseline_RMS` is the calibrated `_PIXEL_DIFF_FLOOR`. If the baseline RMS is below 0.10, use 0.05 as a hard floor.

Update the scaffold:

```python
# BEFORE:
#     _PIXEL_DIFF_FLOOR = float(os.environ.get("KINOFORGE_P1_PIXEL_DIFF_FLOOR", "0.05"))
# AFTER (substitute the calibrated value):
_PIXEL_DIFF_FLOOR = 0.08  # calibrated 2026-06-21 from baseline RMS = 0.27, floor = 0.27 * 0.30
```

Document the calibration in the test docstring with the date + baseline value.

- [ ] **Step 3: Lift the RED skip.**

In `tests/smoke/live_wan21/test_lora_strength_variation.py`, remove the `pytest.mark.skip` line from `pytestmark`:

```python
# BEFORE:
# pytestmark = [
#     pytest.mark.skipif(...),
#     pytest.mark.skip(reason="RED scaffold — Task 13 lifts to enable live run"),
# ]

# AFTER:
pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod smoke",
)
```

- [ ] **Step 4: Run the live smoke.**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/smoke/live_wan21/test_lora_strength_variation.py -v -s 2>&1 | tee /tmp/tier3_smoke.log
```

Expected: 1 PASSED. Time budget: 10–15 min (cold-boot + 2 generations + teardown).

During the run, in a separate terminal, poll RunPod stats:

```bash
while sleep 60; do
  pixi run python -c "
from kinoforge.providers.runpod import RunPodProvider
p = RunPodProvider()
insts = p.list_instances()
for i in insts:
  print(i.id, i.status, p.get_runtime(i.id))
"
done
```

If GPU util is 0% for 3 consecutive 60s polls while the test believes a generation is in flight, abort via Ctrl-C and `kinoforge destroy --id <pod>`.

- [ ] **Step 5: Verify acceptance criteria from the log.**

From `/tmp/tier3_smoke.log`, confirm:
- "last_strength=1.5" appears.
- Pixel-diff RMS line shows a value > the calibrated floor.
- Total spend (cost_rate * wall-time) ≈ $0.30 ± 25%.

- [ ] **Step 6: Update `successful-generations.md`.**

Per CLAUDE.md durability rule: same-tuple `(runpod, diffusers, Wan2.1-T2V-1.3B, t2v)` reuses entry #9; add a "See also" line:

```markdown
**See also: 2026-06-21 P1 Tier-3 strength variation smoke** — same cfg,
two runs (strength=0.5 cold, strength=1.5 warm-attached), pixel-diff
RMS 0.<X> confirms set_adapters(adapter_weights=) wiring. Logs in
git history at commit <SHA>.
```

- [ ] **Step 7: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 8: Commit.**

```bash
git add tests/smoke/live_wan21/test_lora_strength_variation.py successful-generations.md
pixi run pre-commit run --files tests/smoke/live_wan21/test_lora_strength_variation.py successful-generations.md
git commit -m "smoke(p1): Tier-3 GREEN — Wan 2.1 1.3B strength variation, RMS=<X>, spend=\$<Y>"
```

```json:metadata
{"userGate": true, "tags": ["user-gate"], "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/smoke/live_wan21/test_lora_strength_variation.py -v -s", "acceptanceCriteria": ["pixi run preflight returns 0", "test PASSES under KINOFORGE_LIVE_TESTS=1", "pod last_strength=1.5 after Run B", "pixel-diff RMS > calibrated floor", "spend within $0.30 +/- slop", "pod torn down cleanly"], "modelTier": "live-spend", "requireEvidenceTokens": [["strength-0.5", "Run A", "cold"], ["strength-1.5", "Run B", "warm"]]}
```

---

## Task 14: Tier-4 live smoke RED scaffold — Wan 2.2 14B strength variation

**Goal:** Land the Tier-4 live smoke test file as a RED scaffold mirroring Task 12's shape, sized for the Wan 2.2 14B target hardware (~$1.50 envelope).

**Files:**
- Create: `tests/smoke/release_wan22/test_lora_strength_variation.py`

**Acceptance Criteria:**
- [ ] File exists, lints clean.
- [ ] Test function `test_strength_variation_wan22_warm_reuse_diff` is marked `@pytest.mark.skip(reason="RED scaffold — Task 15 lifts to enable live run")`.
- [ ] Test body mirrors Task 12's harness: cold-boot at strength=0.5 → warm-attach at strength=1.5 → pixel-diff. Sized for Wan 2.2 14B cfg + Arcane Style LoRA from successful-generations.md entry #10.

**Verify:** `pixi run pytest tests/smoke/release_wan22/test_lora_strength_variation.py --collect-only -v` → 1 collected, 1 skipped.

**Steps:**

- [ ] **Step 1: Write the Tier-4 scaffold (mirror Task 12).**

```python
"""Tier-4 live smoke: Wan 2.2 14B + Arcane Style LoRA at strength=0.5 vs 1.5.

P1 verification — see docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §11.6.

RED scaffold per CLAUDE.md durability rule: committed BEFORE any live
spend. Task 15 lifts the skip after the per-task live-budget commit.

Cost envelope: ~$1.50 against the $20 session authorization.

Note (spec §11.6): Wan 2.2 14B today loads LoRAs into the single
active transformer the server exposes; full dual-transformer h/l
routing is P2. Tier-4 confirms strength wiring works on the
single-transformer Wan 2.2 path.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests._smoke_harness import budget, civitai, matrix, runpod_lifecycle  # noqa: F401

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
        reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod smoke",
    ),
    pytest.mark.skip(reason="RED scaffold — Task 15 lifts to enable live run"),
]

REPO = Path(__file__).resolve().parents[3]
CFG = REPO / "examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml"
PROMPT_FILE = REPO / "examples/configs/prompts/field-realistic.txt"
_TAG = "kinoforge-smoke-tier-4-strength"
_BUDGET_CAP = 1.50
_PIXEL_DIFF_FLOOR = float(os.environ.get("KINOFORGE_P1_PIXEL_DIFF_FLOOR_T4", "0.08"))


def _extract_pod_id(log_text: str) -> str:
    m = re.search(r"running provisioner\.provision for instance (\w+)", log_text)
    assert m is not None, f"no pod id in:\n{log_text[-2000:]}"
    return m.group(1)


def _run_generate(prompt: str, strength: float, log_path: Path, instance_id: str | None) -> Path:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "KINOFORGE_P1_STRENGTH_OVERRIDE": str(strength)}
    args = [
        "pixi", "run", "python", "-m", "kinoforge", "generate",
        "--config", str(CFG),
        "--mode", "t2v",
        "--prompt", prompt,
        "--no-output-dir",
    ]
    if instance_id is not None:
        args += ["--instance-id", instance_id]
    with log_path.open("w") as f:
        proc = subprocess.run(args, env=env, stdout=f, stderr=subprocess.STDOUT, check=False)  # noqa: S603
    assert proc.returncode == 0, f"generate failed (rc={proc.returncode}); see {log_path}"
    output_dir = REPO / "output"
    clips = sorted(output_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    assert clips, "no output clips found"
    return clips[-1]


def _rms_diff_first_frame_yuv(a: Path, b: Path) -> float:
    import numpy as np
    from tests._smoke_harness.video import decode_first_frame_yuv  # type: ignore[import-not-found]
    ya = decode_first_frame_yuv(a)
    yb = decode_first_frame_yuv(b)
    diff = (ya.astype(np.float64) - yb.astype(np.float64))
    return float(np.sqrt((diff ** 2).mean())) / 255.0


def test_strength_variation_wan22_warm_reuse_diff(tmp_path) -> None:
    """A: cold-boot at strength=0.5. B: warm-reuse at strength=1.5."""
    prompt = PROMPT_FILE.read_text().strip()
    log_a = tmp_path / "run_a_cold_05.log"
    log_b = tmp_path / "run_b_warm_15.log"
    clip_a = _run_generate(prompt, strength=0.5, log_path=log_a, instance_id=None)
    pod_id = _extract_pod_id(log_a.read_text())
    try:
        clip_b = _run_generate(prompt, strength=1.5, log_path=log_b, instance_id=pod_id)
        from kinoforge.providers.runpod import RunPodProvider
        import urllib.request, json
        provider = RunPodProvider()
        endpoints = provider.endpoints(provider.get_instance(pod_id))
        with urllib.request.urlopen(f"{endpoints['http']}/lora/inventory") as resp:  # noqa: S310
            inv = json.loads(resp.read())
        last_strengths = {e["ref"]: e["last_strength"] for e in inv["inventory"]}
        assert 1.5 in last_strengths.values(), f"inventory: {last_strengths}"
        diff = _rms_diff_first_frame_yuv(clip_a, clip_b)
        assert diff > _PIXEL_DIFF_FLOOR, (
            f"Wan 2.2 14B strength=0.5 vs 1.5 RMS diff {diff:.4f} below "
            f"floor {_PIXEL_DIFF_FLOOR:.4f} — set_adapters likely ignored "
            f"adapter_weights on Wan 2.2 path"
        )
    finally:
        subprocess.run(  # noqa: S603
            ["pixi", "run", "kinoforge", "destroy", "--id", pod_id],
            check=False,
        )
```

- [ ] **Step 2: Confirm the scaffold collects + skips.**

Run: `pixi run pytest tests/smoke/release_wan22/test_lora_strength_variation.py --collect-only -v`
Expected: `1 collected`. With execute: `1 skipped`.

- [ ] **Step 3: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean. Any unresolved `decode_first_frame_yuv` import is acceptable at this task (resolved in Task 15).

- [ ] **Step 4: Commit (RED scaffold).**

```bash
git add tests/smoke/release_wan22/test_lora_strength_variation.py
pixi run pre-commit run --files tests/smoke/release_wan22/test_lora_strength_variation.py
git commit -m "test(p1): Tier-4 live smoke RED scaffold — Wan 2.2 14B strength variation"
```

---

## Task 15: Execute Tier-4 live smoke + verify GREEN

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the brainstorming session (decision D6). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Run the Tier-4 live smoke against a real Wan 2.2 14B RunPod pod. Confirm strength wiring on the larger dual-MoE pipeline.

**Files:**
- Modify: `tests/smoke/release_wan22/test_lora_strength_variation.py` (lift the RED skip)

**Acceptance Criteria:**
- [ ] `pixi run preflight` returns exit 0.
- [ ] Test PASSES under `KINOFORGE_LIVE_TESTS=1`.
- [ ] Pod's `/lora/inventory` reports `last_strength=1.5` after Run B.
- [ ] Pixel-diff RMS between strength=0.5 and strength=1.5 clips exceeds the calibrated Tier-4 floor.
- [ ] Spend within $1.50 ± slop.
- [ ] Pod monitored via RunPod stats every 60–90s; idle → kill + fail fast.
- [ ] Pod torn down cleanly.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/smoke/release_wan22/test_lora_strength_variation.py -v -s` → 1 PASSED.

**Steps:**

- [ ] **Step 1: Pre-flight.**

```bash
pixi run preflight
```

Expected: exit 0.

- [ ] **Step 2: Calibrate the Tier-4 pixel-diff floor.**

Same as Task 13 Step 2 but on the Wan 2.2 14B cfg. Capture baseline RMS between two strength=1.0 runs at the same seed; floor = 30% of baseline.

```python
# Update test file:
_PIXEL_DIFF_FLOOR = 0.<calibrated>  # 2026-06-21 calibration: baseline = <X>, floor = baseline * 0.30
```

- [ ] **Step 3: Lift the RED skip.**

```python
# BEFORE:
# pytestmark = [
#     pytest.mark.skipif(...),
#     pytest.mark.skip(reason="RED scaffold — Task 15 lifts to enable live run"),
# ]
# AFTER:
pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="set KINOFORGE_LIVE_TESTS=1 to run live RunPod smoke",
)
```

- [ ] **Step 4: Run the live smoke.**

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/smoke/release_wan22/test_lora_strength_variation.py -v -s 2>&1 | tee /tmp/tier4_smoke.log
```

Expected: 1 PASSED. Time budget: 30–45 min wall (cold-boot Wan 2.2 14B + 2 generations + teardown). Poll RunPod stats in parallel.

- [ ] **Step 5: Verify acceptance criteria from log.**

Confirm `last_strength=1.5` and pixel-diff > floor in `/tmp/tier4_smoke.log`.

- [ ] **Step 6: Update `successful-generations.md`.**

Same-tuple `(runpod, diffusers, Wan2.2-T2V-A14B, t2v)` reuses entry #8 / #10; add "See also" line.

- [ ] **Step 7: Lint + typecheck.**

Run: `pixi run lint && pixi run typecheck`
Expected: clean.

- [ ] **Step 8: Commit.**

```bash
git add tests/smoke/release_wan22/test_lora_strength_variation.py successful-generations.md
pixi run pre-commit run --files tests/smoke/release_wan22/test_lora_strength_variation.py successful-generations.md
git commit -m "smoke(p1): Tier-4 GREEN — Wan 2.2 14B strength variation, RMS=<X>, spend=\$<Y>"
```

```json:metadata
{"userGate": true, "tags": ["user-gate"], "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/smoke/release_wan22/test_lora_strength_variation.py -v -s", "acceptanceCriteria": ["pixi run preflight returns 0", "test PASSES under KINOFORGE_LIVE_TESTS=1", "pod last_strength=1.5 after Run B", "pixel-diff RMS > calibrated Tier-4 floor", "spend within $1.50 +/- slop", "pod torn down cleanly"], "modelTier": "live-spend", "requireEvidenceTokens": [["strength-0.5", "Run A", "wan22-cold"], ["strength-1.5", "Run B", "wan22-warm"]]}
```

---

## Task 16: PROGRESS.md close-out + push

**Goal:** Mark P1 CLOSED in PROGRESS.md with commit hash trail; push the branch.

**Files:**
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] PROGRESS.md "Next session" block names P1 CLOSED 2026-06-21 with the commit list (Tasks 0–15).
- [ ] P2 + P3 entries updated to remove P1's "IN PROGRESS" marker; both stay DEFERRED HIGH.
- [ ] Cumulative live spend logged (Tier-3 + Tier-4 actuals).
- [ ] `git push origin main` succeeds.
- [ ] CI green on the pushed commits.

**Verify:** `gh run list --branch main --limit 1` → success.

**Steps:**

- [ ] **Step 1: Add the P1 CLOSED block to PROGRESS.md.**

Insert at the top of the "Next session" section:

```markdown
**P1 — Server per-LoRA strength weights CLOSED 2026-06-21.** End-to-end
strength dimension landed on the kinoforge diffusers engine.

Commits (Tasks 0–15):
- `<SHA-T0>` feat(p1): LoraEntry + VaultLoRA(LoraEntry) + LoraTarget + schema parity lockdown
- `<SHA-T1>` feat(p1): Config.loras block + legacy kind=lora promoter + ModelEntry.kind narrowing
- `<SHA-T2>` feat(p1): capability_key reads Config.loras; strength stays out of hash
- `<SHA-T3>` feat(p1): SetStackRequest tagged-object shape + legacy target_refs promoter
- `<SHA-T4>` feat(p1): set_adapters(adapter_weights=) + LoraInventoryEntry.last_strength
- `<SHA-T5>` feat(p1): VRAM-OOM rollback restores refs + strengths; broaden to ValueError
- `<SHA-T6>` feat(p1): resolve_active_lora_stack + LoraStackConflict + SetStackRequestRejected
- `<SHA-T7>` feat(p1): build_set_stack_request adapter helper
- `<SHA-T8>` feat(p1): DiffusersEngine uses build_set_stack_request + resolve_active_lora_stack
- `<SHA-T9>` feat(p1): warm-attach matcher checks refs AND strength via math.isclose
- `<SHA-T10>` test(p1): extend redaction AST scan with P1 write-site coverage
- `<SHA-T11>` chore(p1): migrate example cfgs to top-level loras: block
- `<SHA-T12>` test(p1): Tier-3 live smoke RED scaffold
- `<SHA-T13>` smoke(p1): Tier-3 GREEN — Wan 2.1 1.3B
- `<SHA-T14>` test(p1): Tier-4 live smoke RED scaffold
- `<SHA-T15>` smoke(p1): Tier-4 GREEN — Wan 2.2 14B

Cumulative live spend: $<actual_total>. P2 + P3 remain DEFERRED HIGH
per the 2026-06-21 sub-project decomposition anchor.
```

Substitute the actual commit SHAs (run `git log --oneline -16` to capture them).

- [ ] **Step 2: Update the sub-project decomposition block.**

Remove "BRAINSTORM IN PROGRESS" from the P1 line; mark it "CLOSED 2026-06-21 — see commits above". Leave P2 + P3 marked DEFERRED HIGH unchanged.

- [ ] **Step 3: Pre-commit + commit.**

```bash
git add PROGRESS.md
pixi run pre-commit run --files PROGRESS.md
git commit -m "docs(progress): P1 server per-LoRA strength weights CLOSED"
```

- [ ] **Step 4: Push.**

```bash
git push origin main
```

- [ ] **Step 5: Wait for CI.**

```bash
gh run watch $(gh run list --branch main --limit 1 --json databaseId --jq '.[0].databaseId') --exit-status
```

Expected: success on ubuntu + macos.

---

## Self-review

**Spec coverage check:**
- Spec §2 In-scope items all covered:
  - `LoraEntry` Pydantic class → Task 0 ✓
  - `VaultLoRA(LoraEntry)` migration → Task 0 ✓
  - Top-level `loras:` cfg block + `_promote_*` validator → Task 1 ✓
  - `LoraTarget` server model → Task 0 ✓
  - `SetStackRequest` migration + legacy promoter → Task 3 ✓
  - `set_adapters(adapter_weights=)` end-to-end → Task 4 ✓
  - VRAM-OOM rollback strength restoration → Task 5 ✓
  - `LoraInventoryEntry.last_strength` → Task 4 ✓
  - `resolve_active_lora_stack` orchestrator helper → Task 6 ✓
  - `build_set_stack_request` adapter → Task 7 ✓
  - Engine integration → Task 8 ✓
  - Matcher extension → Task 9 ✓
  - In-tree cfg sweep → Task 11 ✓
  - Tier-3 live smoke → Tasks 12 + 13 ✓
  - Tier-4 live smoke → Tasks 14 + 15 ✓
- Spec §4 invariants:
  - P1-Privacy-1 (strength non-sensitive) → enforced by NOT registering strength with RedactionRegistry; covered by absence-of-write in Task 10 ✓
  - P1-Privacy-2 (`VaultLoRA.ref` inherited) → Task 0 (parity test) ✓
  - P1-CI-1 (AST scan extension) → Task 10 ✓
  - P1-Identity (strength out of capability_key) → Task 2 ✓
  - P1-Matcher (same-refs different-strength not match) → Task 9 ✓
  - P1-Float-Tolerance (isclose rel_tol=1e-6) → Task 9 ✓
  - P1-Rollback (refs + strengths restored) → Task 5 ✓
  - P1-Schema-Lockstep (LoraEntry ≡ LoraTarget) → Task 0 ✓
- Spec §12 migration plan steps 1–10 map to Tasks 0–11 + Task 16's close-out + (deferred to next release) shim removal ✓.

**Placeholder scan:** Searched for "TBD", "TODO", "implement later", "add appropriate". Two `<SHA-T...>` placeholders in Task 16's PROGRESS draft — those are intentional (filled at execution time from `git log`). The pixel-diff floor (0.05 default, calibrated in Tasks 13/15) has explicit calibration steps with formulas — not a hidden TBD.

**Type consistency:**
- `LoraEntry` defined in Task 0; consumed in Tasks 6, 7, 8, 9 — signature matches.
- `LoraTarget` defined in Task 0; consumed in Tasks 3, 4, 5, 7 — signature matches.
- `resolve_active_lora_stack(cfg, vault) -> list[LoraEntry]` consistent across Tasks 6 + 8.
- `build_set_stack_request(active_stack, *, download_specs)` consistent across Tasks 7 + 8.
- `is_stack_match(active, target) -> bool` consistent across Task 9 (single definition).
- `_replace_adapter_stack` introduced in Task 4 (renames `_reload_pipeline_loras`); rollback path in Task 5 uses the new name; legacy shim deleted by Task 5 Step 5.

**Gaps:** None found in spec coverage. The transition shim removal step (spec §12.10) is intentionally deferred to a future release per the spec criteria; no task in this plan removes shims.

---

> Heads up — I tagged 2 task(s) as user-gate (Tasks #13, #15). The plan runs end-to-end as-is. If you'd like automatic close-time enforcement, the JSON snippets are in `README.md` — paste them into `.claude/settings.json` (or `settings.local.json`). Happy to walk you through it; just say the word.
