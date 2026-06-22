# P1 — Server per-LoRA strength weights — design spec

**Status:** APPROVED (brainstorm 2026-06-21)
**Sub-project:** P1 of the CLI `--loras` arg decomposition (see PROGRESS.md
2026-06-21 anchor — P1 in progress; P2 + P3 deferred high-priority).
**Predecessor reading:**
- `docs/superpowers/specs/2026-06-08-ephemeral-workspaces-design.md` (vault
  + RedactionRegistry invariants this spec must satisfy)
- `docs/superpowers/specs/2026-06-20-lora-flexible-warm-reuse-design.md`
  (warm-reuse matcher mutability model this spec extends)
- `docs/superpowers/specs/2026-06-21-lora-smoke-pyramid-design.md` (live
  smoke pyramid this spec plugs Tier-3 + Tier-4 strength-variation tests
  into)

## 1. Motivation

The Wan T2V server (`src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`)
calls `pipe.set_adapters(names)` with no weights, so every active LoRA is
implicitly strength = 1.0. The cfg schema has no strength field either —
`ModelEntry` carries only `ref / kind / target / sha256`. No CLI surface
exposes strength.

The user's longer-term goal (P3 — CLI `--loras` arg) requires strength as
a per-LoRA dimension. P1 lands strength end-to-end on the existing
declarative `/lora/set_stack` API path; P2 (Wan 2.2 dual-transformer h/l
routing) and P3 (CLI surface) consume P1 as a prerequisite.

## 2. Scope

### In scope

- New shared Pydantic class `LoraEntry` (`src/kinoforge/core/lora.py`) with
  `ref` + `strength` + `sha256` fields. `extra="forbid"`.
- `VaultLoRA` migrates to `VaultLoRA(LoraEntry)` — inherits `LoraEntry` and
  adds vault-only `label`.
- New top-level `loras:` cfg block (`Config.loras: list[LoraEntry] = []`).
  `kind: lora` removed from `ModelEntry.kind` Literal union.
- `Config` model_validator(mode="before") auto-promotes legacy
  `models: [{kind: lora, ...}]` cfgs to the new shape with a
  `DeprecationWarning`.
- New server-side Pydantic class `LoraTarget` mirroring `LoraEntry` shape
  (defined in the server module to keep the pod's import graph slim).
- `SetStackRequest` migrates `target_refs: list[str]` → `target: list[LoraTarget]`.
  Pydantic model_validator(mode="before") accepts BOTH shapes for one
  transition window.
- `_replace_adapter_stack` + `_load_pipeline` call `set_adapters(names,
  adapter_weights=[...])` end-to-end.
- VRAM-OOM rollback path captures + restores previous strengths.
- `LoraInventoryEntry` gains `last_strength: float | None` so
  `kinoforge pod lora ls` surfaces the active strength.
- Orchestrator helper `resolve_active_lora_stack(cfg, vault)` resolves
  cfg.loras vs vault.loras precedence (vault wins; diverging ref sets
  raise `LoraStackConflict`).
- Adapter helper `build_set_stack_request(active_stack, download_specs)`
  bridges `LoraEntry` ↔ `LoraTarget`.
- Warm-attach matcher (`kinoforge.core.warm_reuse.matcher.is_stack_match`)
  extended: same-refs / different-strength is NO LONGER a match — schedules
  a set_stack call to update weights even when no download/evict needed.
- Sweep commit migrates every in-tree example cfg (`wan21-1_3b-lora-...`,
  `wan22-14b-lora-...`, `wan.yaml`, any `kind: lora` site) to the new
  `loras:` block shape.
- Tier-3 live smoke (Wan 2.1 1.3B, strength=0.5 vs 1.5, ~$0.30).
- Tier-4 live smoke (Wan 2.2 14B, strength=0.5 vs 1.5, ~$1.50).

### Out of scope (deferred to P2 / P3)

- Wan 2.2 high/low dual-transformer routing (P2 — `branch: h | l | auto`
  field on `LoraEntry`).
- CLI `--loras` arg surface (P3) — heredoc parser, ref shorthand
  expansion, override-vs-append semantics against cfg/vault.
- Trigger word + sampler hints metadata (future LoRA fields).
- Per-segment LoRA stacks (explicitly out of scope per ephemeral spec §2).
- Hosted Bearer engine support (P1 is diffusers-engine only; hosted
  engines have no equivalent LoRA stack API).

### Public-by-design (deliberately stays unchanged)

- Strength is a NON-SENSITIVE field (low entropy, not user-identifying).
  Strength values MAY appear unredacted in logs / cache / batch summaries
  / error tracebacks — same posture as `seed`, `num_inference_steps`, `fps`.
- LoRA `ref` strings in committed example cfgs (e.g.
  `wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml`) remain public-by-design
  per the ephemeral spec's D6 rule for repo-committed material.

## 3. Decisions locked during brainstorm

| # | Decision | Value | Why |
|---|---|---|---|
| D1 | Sub-project ordering | Brainstorm P1 first; P2 + P3 deferred. | P1 has no dependencies; P3 depends on P1+P2; P2 independent of P1. Cleanest dependency direction. |
| D2 | HTTP API shape | Tagged objects: `target: list[{ref, strength, ...}]` replacing legacy `target_refs: list[str]`. | Parallel-list approach would force a second sibling list when P2 adds `branch`, then a third for trigger_word, etc. Tagged objects accept future per-LoRA fields with no schema sprawl. Migration cost is one-time, internal-only — `/lora/set_stack` has no external producers today. |
| D3 | Cfg schema | Dedicated top-level `loras:` block; shared `LoraEntry` Pydantic class; `VaultLoRA` inherits + adds `label`. | LoRAs are conceptually different from base/vae/text_encoder (strength, branch, trigger_word, source_url, order-matters). Single `LoraEntry` class is reusable by both public cfg and vault. Capability_key derivation already separates LoRAs in its hash material — the schema is the only place still pretending they're uniform. Vault context reinforces: same logical thing should not have two divergent Pydantic models. |
| D4 | Capability key & strength | Strength does NOT enter `capability_key` hash material. Identity = (base, engine, precision, lora refs SET). Strength is a mutable per-run parameter applied via `set_adapters(adapter_weights=)` on warm-attach. | Lora-flexible-warm-reuse design treats the LoRA stack as mutable on a warm pod. Strength in identity would defeat warm-reuse for any iteration; the iterate-with-different-strengths workflow is exactly what users do most. |
| D5 | Strength range | Pydantic `Field(ge=-2.0, le=2.0)`; default = 1.0 when omitted. | Industry-standard a1111 LoRA range. Tight bounds catch typos at load time. NaN/inf/extreme rejection comes free from `ge/le`. |
| D6 | Verify scope | Unit + integration + Tier-3 live smoke ($0.30) + Tier-4 live smoke ($1.50). Total live budget ~$1.80. | Tier-3 confirms wiring on the cheap Wan 2.1 1.3B pod; Tier-4 confirms on the Wan 2.2 14B target hardware (relevant prelude to P2). Visual diff between strength=0.5 and strength=1.5 with same seed locks in end-to-end honor of the weight. |
| D7 | Privacy posture for strength | `RedactionRegistry` does NOT register strength values. Strength is non-sensitive per ephemeral D4 scope (sensitive set = `ref` + filename + label + derived hashes only). | Strength is a low-entropy float; cannot identify a user or a LoRA on its own. |
| D8 | Migration handling | Pydantic `model_validator(mode="before")` auto-promotes BOTH legacy cfg `models: [{kind: lora, ...}]` AND legacy server `target_refs: [...]` payloads to the new shape during a single transition window. `DeprecationWarning` emitted on cfg promotion. | One-time migration, internal-only — no external producers of the HTTP API; small set of in-tree cfgs migrate in one sweep commit. |
| D9 | Server inventory surface | `LoraInventoryEntry.last_strength: float \| None` added; populated on every successful `set_adapters` write; surfaced via `GET /lora/inventory` + `kinoforge pod lora ls`. | Operator can confirm strength wiring without running a full generation. Also load-bearing for the matcher (D10). |
| D10 | Matcher equality | Warm-attach `is_stack_match` checks BOTH refs AND strength (math.isclose with rel_tol=1e-6). Same-refs / different-strength schedules a set_stack call. | Without this, a user changing strength on a warm pod would silently keep the old weights. The set_stack call is cheap on same-refs (no download/evict). |
| D11 | Conflict resolution | When BOTH cfg.loras AND vault.loras are populated with diverging ref sets, raise `LoraStackConflict(KinoforgeError)`. Matching ref sets (with possibly-different strengths) is also rejected — vault is sole authoritative source when loaded. | Defensive: the user almost certainly made a copy-paste mistake. Vault spec D2 establishes vault as the canonical confidential source. |

## 4. Key invariants

- **P1-Privacy-1:** strength values are non-sensitive and explicitly OUT of
  the ephemeral spec's D4 sensitivity scope. `RedactionRegistry` does NOT
  register them. They MAY appear in logs / cache / batch summaries.
- **P1-Privacy-2:** `LoraEntry.ref` + `VaultLoRA.label` remain sensitive
  per D4. Vault loader's existing `add_many` registration site iterates
  `vault.loras` and reads `.ref` — `VaultLoRA(LoraEntry)` inheritance
  preserves the field shape, so the registration call needs zero edits.
- **P1-CI-1:** `tests/test_no_unredacted_writes.py`'s AST scan extends
  coverage to every new write site introduced by P1: `LoraTarget` /
  `LoraEntry` `.ref` reads in `kinoforge/_adapters.py::build_set_stack_request`,
  `kinoforge/core/lora.py::resolve_active_lora_stack`,
  `kinoforge/core/warm_reuse/matcher.py::is_stack_match`,
  and `wan_t2v_server.py::set_stack` request body logging.
- **P1-Identity:** strength is NEVER in `capability_key` hash material.
  Two cfgs identical in refs but differing in strengths hash to the SAME
  key (locked by `test_capability_key_strength_invariant`).
- **P1-Matcher:** same-refs / different-strength is NOT a stack match
  (locked by `test_matcher_same_refs_different_strength_not_match`).
- **P1-Float-Tolerance:** matcher strength equality uses
  `math.isclose(rel_tol=1e-6)` to dodge JSON round-trip drift (locked by
  `test_matcher_isclose_tolerance_swallows_json_float_drift`).
- **P1-Rollback:** VRAM-OOM rollback restores BOTH refs AND strengths to
  pre-swap state (locked by
  `test_set_adapters_rollback_restores_strength_on_value_error`).
- **P1-Schema-Lockstep:** `LoraEntry` (core) and `LoraTarget` (server)
  shapes must agree on `ref` + `strength` (locked by
  `tests/test_lora_schema_parity.py`).

## 5. Architecture overview

```
┌─────────────────────────┐
│  examples/configs/*.yaml │
│    + ~/.kinoforge/vault/ │  ──── load_config()/load_vault() ──┐
└─────────────────────────┘                                     │
            ▲                                                   ▼
            │ pydantic model_validator(mode="before")    ┌────────────────────┐
            │ auto-promotes legacy                       │  LoraEntry         │
            │ `models: [{kind:lora,...}]` →              │  (shared model)    │
            │ `loras: [...]` on legacy cfgs              │  ref / strength /  │
                                                         │  sha256            │
                                                         └─────────┬──────────┘
                                                                   │
                       VaultLoRA(LoraEntry) adds `label`    ───────┤
                                                                   │
                                                                   ▼
                                                         ┌────────────────────┐
                                                         │  orchestrator      │
                                                         │  resolve_active_   │
                                                         │  lora_stack(...)   │
                                                         │  → list[LoraEntry] │
                                                         └─────────┬──────────┘
                                                                   │
                                                build_set_stack_request()
                                                                   │
                                                                   ▼  POST /lora/set_stack
                                                         ┌────────────────────┐
                                                         │  wan_t2v_server    │
                                                         │  SetStackRequest   │
                                                         │  target:list[Lora…]│
                                                         │  → set_adapters(   │
                                                         │     names,         │
                                                         │     adapter_weights)│
                                                         └────────────────────┘
```

## 6. Schemas

### 6.1 `LoraEntry` (new — `src/kinoforge/core/lora.py`)

```python
class LoraEntry(BaseModel):
    """Canonical per-LoRA shape used by both public cfg `loras:` blocks
    and vault `loras:` lists.

    Forward-compat anchor: P2 adds `branch: Literal["h", "l", "auto"]`;
    future fields (trigger_word, sampler_hints, source_url) land here.

    Privacy classification (P1):
      - `ref`     — SENSITIVE per ephemeral-workspaces spec D4
      - `strength` — NON-SENSITIVE (low-entropy float; same posture as seed)
      - `sha256`  — derived hash; per D4 derived hashes are sensitive
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$|^$")
```

### 6.2 `VaultLoRA` (migrated — `src/kinoforge/core/vault.py`)

```python
class VaultLoRA(LoraEntry):
    """Vault-side LoRA entry: LoraEntry + optional vault-internal label.

    `label` is vault-internal only — never persisted, never logged, never
    sent over the wire (stripped on upcast to LoraEntry inside
    resolve_active_lora_stack).
    """

    label: str | None = None  # vault-only extension
```

### 6.3 `LoraTarget` + `SetStackRequest` (server — `wan_t2v_server.py`)

```python
class LoraTarget(BaseModel):
    """One entry in /lora/set_stack target list. Schema-equivalent to
    LoraEntry but defined in the server module so the server has no
    import-time dependency on kinoforge.core.lora (server runs on the
    pod with a minimal dependency set)."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)


class SetStackRequest(BaseModel):
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

### 6.4 `Config` migration (`src/kinoforge/core/config.py`)

```python
class Config(BaseModel):
    # ... existing fields ...
    models: list[ModelEntry] = []
    loras: list[LoraEntry] = []  # new

    @model_validator(mode="before")
    @classmethod
    def _promote_legacy_kind_lora_to_loras_block(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        models = data.get("models") or []
        legacy_loras = [
            m for m in models if isinstance(m, dict) and m.get("kind") == "lora"
        ]
        if not legacy_loras:
            return data
        non_lora_models = [
            m for m in models if not (isinstance(m, dict) and m.get("kind") == "lora")
        ]
        promoted = [
            {"ref": m["ref"], "sha256": m.get("sha256")} for m in legacy_loras
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


class ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    # "lora" removed from Literal — forces all kind=lora through the promoter.
    kind: Literal["base", "vae", "text_encoder", "clip_vision"]
    target: str
    sha256: str | None = None
```

### 6.5 `LoraInventoryEntry` extension (`wan_t2v_server.py`)

```python
class LoraInventoryEntry(BaseModel):
    ref: str
    filename: str
    size_bytes: int
    downloaded_at_local: str
    last_used_at_local: str
    adapter_name: str
    last_strength: float | None = None   # NEW; None when never activated
```

## 7. Capability key derivation

```python
def capability_key(self) -> CapabilityKey:
    """P1: LoRA refs come from self.loras (not from models[kind=lora]).
    Strength is NOT in the hash material — strength is a mutable per-run
    parameter, set via /lora/set_stack on a warm-attached pod."""

    loras: list[str] = [lo.ref for lo in self.loras]
    # ... base_model + engine + precision derivation unchanged ...
    return CapabilityKey(
        base_model=base_model_ref,
        loras=tuple(loras),   # refs only; strength deliberately excluded
        engine=self.engine.kind,
        precision=self.engine.precision,
    )
```

Vault-side `compute_profile_alias` material (`vault.py:204`) stays at
`[lo.ref for lo in vault.loras]` — no change required because strength
is also out of the alias for the same reason.

## 8. Server-side `set_adapters` wiring

```python
def _replace_adapter_stack(target: list[LoraTarget]) -> None:
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
        entry["last_strength"] = t.strength   # NEW
    if names:
        pipe.set_adapters(names, adapter_weights=weights)


def _load_pipeline(*, initial_lora_stack: list[LoraTarget] | None = None) -> Any:
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

## 9. Orchestrator + matcher plumbing

### 9.1 `resolve_active_lora_stack`

```python
# src/kinoforge/core/lora.py

def resolve_active_lora_stack(
    cfg: Config,
    vault: Vault | None,
) -> list[LoraEntry]:
    """Vault wins entirely when loaded; diverging refs raise."""
    if vault is not None and vault.loras:
        cfg_refs = {lo.ref for lo in cfg.loras}
        vault_refs = {lo.ref for lo in vault.loras}
        if cfg.loras and cfg_refs != vault_refs:
            raise LoraStackConflict(
                "cfg.loras and vault.loras both set with diverging ref sets — "
                "remove cfg.loras and use vault.loras as sole source"
            )
        # Drop vault-only `label` on the upcast.
        return [LoraEntry(**lo.model_dump(exclude={"label"})) for lo in vault.loras]
    return list(cfg.loras)
```

### 9.2 `build_set_stack_request`

```python
# src/kinoforge/_adapters.py

def build_set_stack_request(
    active_stack: list[LoraEntry],
    *,
    download_specs: dict[str, ArtifactDownloadSpec],
) -> SetStackRequest:
    return SetStackRequest(
        target=[
            LoraTarget(ref=lo.ref, strength=lo.strength) for lo in active_stack
        ],
        download_specs=download_specs,
    )
```

### 9.3 Matcher extension

```python
# src/kinoforge/core/warm_reuse/matcher.py

def is_stack_match(
    active: list[LoraInventoryEntry],
    target: list[LoraEntry],
) -> bool:
    if [a.ref for a in active] != [t.ref for t in target]:
        return False
    return all(
        math.isclose(a.last_strength or 1.0, t.strength, rel_tol=1e-6)
        for a, t in zip(active, target, strict=True)
    )
```

## 10. Error handling

| # | Where | Cause | Class | HTTP/CLI surface |
|---|---|---|---|---|
| E1 | Cfg load | strength outside [-2.0, 2.0] | `ValidationError` (Pydantic) | CLI exit 1 with `loras.0.strength` field path |
| E2 | Vault load | same | `ValidationError` | CLI exit 1; refs in traceback substituted via root redaction filter |
| E3 | `resolve_active_lora_stack` | cfg.loras + vault.loras refs diverge | `LoraStackConflict(KinoforgeError)` — NEW | CLI exit 1 |
| E4 | `/lora/set_stack` server | bad request shape passes client validation but fails server | 422 Pydantic | orchestrator surfaces as `SetStackRequestRejected(KinoforgeError)` — NEW |
| E5 | `_replace_adapter_stack` | `set_adapters(adapter_weights=)` raises | existing `LoraSetAdaptersFailed`; broaden catch from `RuntimeError` to also include `ValueError` (PEFT rejection) | server 500 with `phase: "set_adapters"`; rollback restores refs + strengths |
| E6 | Matcher | pod inventory has `last_strength=None` (pre-P1 image) | none — graceful degrade | `(a.last_strength or 1.0)` shim; DEBUG log; shim removed in release after P1 |

Rollback ordering (load-bearing invariant):

1. Snapshot `previous_state: list[LoraTarget]` BEFORE unload.
2. Unload failed-new adapters.
3. Reload previous refs with previous strengths.
4. `set_adapters(previous_names, adapter_weights=previous_weights)`.
5. Restore `_inventory[ref]["last_strength"]`.
6. ONLY THEN return HTTPException.

If step 4 raises: return 500 with `phase: "rollback"` and
`rollback_failed: true`. Orchestrator destroys the pod via existing handler.

## 11. Test scope

### 11.1 `LoraEntry` + schema (Section 2)

- `test_lora_entry_strength_default_is_1_0`
- `test_lora_entry_strength_lower_bound_rejected` (strength=-2.5 → ValidationError)
- `test_lora_entry_strength_upper_bound_rejected` (strength=3.0 → ValidationError)
- `test_lora_entry_strength_at_bounds_accepted` (strength=-2.0 and +2.0)
- `test_lora_entry_extra_field_rejected` (extras forbidden)
- `test_lora_entry_sha256_pattern_rejects_short_string`
- `test_vault_lora_inherits_strength_field`
- `test_vault_lora_label_field_present`
- `test_lora_schema_parity` (lockdown: LoraEntry and LoraTarget agree on ref+strength)

### 11.2 Cfg migration (Section 3)

- `test_legacy_models_kind_lora_promotes_to_loras_block`
- `test_legacy_promotion_emits_deprecation_warning`
- `test_new_loras_block_loads_directly`
- `test_modelentry_rejects_kind_lora_at_validator_level`
- `test_capability_key_uses_loras_block_not_models`
- `test_capability_key_strength_invariant` (P1-Identity)
- Sweep: every migrated example cfg loads with zero DeprecationWarning

### 11.3 Server (Section 4)

- `test_set_stack_passes_adapter_weights_to_set_adapters`
- `test_legacy_target_refs_promotes_to_target_strength_1_0`
- `test_strength_out_of_range_rejected_at_request_validation`
- `test_initial_lora_stack_carries_strength_through_cold_boot`
- `test_vram_oom_rollback_restores_strength_not_just_refs`
- `test_inventory_endpoint_surfaces_last_strength`

### 11.4 Orchestrator + matcher (Section 5)

- `test_resolve_active_lora_stack_vault_wins`
- `test_resolve_active_lora_stack_diverging_refs_raises`
- `test_resolve_active_lora_stack_label_stripped_on_upcast`
- `test_build_set_stack_request_pairs_strengths_in_order`
- `test_matcher_same_refs_different_strength_not_match` (P1-Matcher)
- `test_matcher_isclose_tolerance_swallows_json_float_drift` (P1-Float-Tolerance)

### 11.5 Error handling (Section 6)

- `test_strength_out_of_range_at_cfg_load_raises_validation_error`
- `test_strength_out_of_range_at_vault_load_raises_redacted_validation_error`
- `test_diverging_cfg_vault_ref_sets_raises_lora_stack_conflict`
- `test_set_adapters_rollback_restores_strength_on_value_error` (P1-Rollback)
- `test_set_adapters_rollback_failure_returns_500_phase_rollback`
- `test_matcher_missing_last_strength_treats_as_1_0`
- `test_no_unredacted_writes_covers_p1_lora_targets` (P1-CI-1)

### 11.6 Live smokes (Section 7)

- Tier-3: `tests/smoke/release_wan21/test_lora_strength_variation.py`
  - Single warm pod, two consecutive `kinoforge generate` calls (strength=0.5
    then strength=1.5). Same seed + same prompt + same refs.
  - Assert: both runs produce MP4s; pod reused (no cold-boot); pod's
    `last_strength` reads 1.5 after Run B; pixel-diff RMS > 0.05 between
    Run A and Run B clips on first-frame YUV. **The 0.05 floor is a spec
    proposal subject to one-time calibration during plan execution** —
    a dry run on the recorded fixture clips from
    successful-generations.md #9 establishes the actual baseline
    separation between strength=0.5 and strength=1.5 outputs; the final
    threshold lands at ~30% of that baseline so genuine wiring breakage
    falls below but normal torch/cuda-version drift stays above.
  - Pre-flight: `pixi run preflight` mandatory.
  - Polling: RunPod `runtime.gpus.gpuUtilPercent` every 60–90s; idle for 3
    consecutive probes → kill + fail fast.
  - Spend envelope: ~$0.30.
- Tier-4: `tests/smoke/release_wan22/test_lora_strength_variation.py`
  - Same shape on Wan 2.2 14B. Arcane Style LoRA from
    successful-generations.md entry #10. Spend envelope: ~$1.50.
  - Note: Wan 2.2 14B today loads LoRAs into the single active transformer
    the server exposes. Full dual-transformer h/l routing is P2's burden;
    Tier-4 confirms strength wiring works on the single-transformer Wan 2.2
    path.
- Standard test prompt: `/workspace/examples/configs/prompts/field-realistic.txt`
  verbatim per memory `feedback_standard_test_prompt`.
- successful-generations.md: "See also" lines under entries #8 (Wan 2.2)
  and #9 (Wan 2.1 1.3B) — no new section since the strength variation
  reuses existing `(provider, engine, model, mode)` tuples.

## 12. Migration plan

1. **Land schemas + validators (no behavior change).** New `LoraEntry`,
   `VaultLoRA(LoraEntry)`, `Config.loras: list[LoraEntry]`,
   `Config._promote_legacy_kind_lora_to_loras_block` validator,
   `ModelEntry.kind` Literal narrowing.
2. **Land HTTP migration validator (no behavior change).**
   `SetStackRequest._migrate_legacy_target_refs`,
   `LoraTarget`, `SetStackRequest.target` field. Server still receives
   `target_refs: [...]` from existing orchestrator callers and auto-promotes.
3. **Land server-side weights threading.** `_replace_adapter_stack` +
   `_load_pipeline` call `set_adapters(names, adapter_weights=[...])`.
   `LoraInventoryEntry.last_strength` populated. Inventory endpoint
   surfaces it.
4. **Land VRAM-OOM rollback extension.** Snapshot + restore strength.
5. **Land orchestrator helpers.** `resolve_active_lora_stack`,
   `build_set_stack_request`. Engine integration switches to
   `build_set_stack_request(...)` call.
6. **Land matcher extension.** `is_stack_match` checks strength via
   `math.isclose(rel_tol=1e-6)`. Same-refs / different-strength now
   schedules a set_stack.
7. **Sweep in-tree cfgs.** Migrate every `kind: lora` site to the new
   `loras:` block. ONE commit.
8. **Tier-3 + Tier-4 live smokes.** RED scaffold committed BEFORE live
   spend per durability rule. Live spend authorized once both scaffolds
   green-able.
9. **PROGRESS.md close-out.** P1 entry marked CLOSED with commit hash
   trail. P2 + P3 remain anchored in the deferred queue.
10. **(One release after P1 ships) Transition shim removal.**
    Removal criteria — ALL must be true before step 10 executes:
    - Zero in-tree cfgs still load with `DeprecationWarning` (cfg sweep
      §12.7 covers in-tree; external cfgs are operator responsibility).
    - Every in-flight RunPod pod across the operator's account has been
      destroyed and re-cold-booted on the new server image (so no pod
      still serves the legacy `target_refs` request shape).
    - No CI run in the prior two-week window emitted the
      `"pod inventory missing last_strength"` DEBUG log line.

    When the criteria hold, remove `SetStackRequest._migrate_legacy_target_refs`,
    `Config._promote_legacy_kind_lora_to_loras_block`, and the
    `(a.last_strength or 1.0)` matcher shim. Each removal lands in its
    own atomic commit so a regression bisect can isolate the offender.

## 13. Open questions for the implementation plan (writing-plans skill consumes)

- Task granularity for the migration plan (sections 12.1–12.10 each
  correspond to ≥1 task; writing-plans decides the split).
- Sequencing of in-tree cfg sweep vs. the validator landing — likely
  validator lands first so the sweep doesn't trip the still-tightening
  `Literal` constraint mid-PR.
- Tier-3 + Tier-4 smoke harness wiring: extend the existing pyramid
  harness with a `--strength-variation` mode or write a new top-level
  driver. Writing-plans decides.

---

**End of P1 design spec. Approval gate: section-by-section approvals
captured during brainstorm 2026-06-21.**
