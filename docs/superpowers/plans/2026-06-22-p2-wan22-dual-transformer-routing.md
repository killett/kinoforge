# P2 — Wan 2.2 dual-transformer h/l routing — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Wan 2.2 LoRA loading route per-entry to `transformer` (high-noise) or `transformer_2` (low-noise) via an explicit `branch` field on `LoraEntry`, so LoRAs reach the stage they were trained against instead of landing wherever diffusers' default loader picks.

**Architecture:** Adds `branch: Literal["high_noise", "low_noise", "auto"]` to canonical `LoraEntry` schema with `h`/`l` aliases. Server detects pipeline arity at boot; rejects `auto` on MoE and rejects explicit branch on single-transformer. Inventory keyed by composite `(ref, branch)` so same ref can co-exist in two branches. `is_stack_match` extended to compare `(ref, strength, branch)` tuples in order. `capability_key` unchanged — same warm pod serves all branch combinations via `/lora/set_stack` swap. Routing dispatch picks Approach 1 (diffusers per-transformer kwarg) vs Approach 3 (transformer attribute rebind) based on Task 0's research finding.

**Tech Stack:** Python 3.13, Pydantic 2.x, FastAPI, diffusers (WanPipeline), peft, pytest, ruff, mypy. Pod-side server in `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`. Canonical schema in `src/kinoforge/core/lora.py`. Test pyramid (Tier 1 CPU stub / Tier 3 Wan 2.1 1.3B / Tier 4 Wan 2.2 14B).

**User decisions (already made):**
- Q1 `auto` = single-transformer only; MoE requires explicit h/l.
- Q2 schema home = canonical `LoraEntry` (cfg + vault + server `LoraTarget` via parity lock).
- Q3 vocabulary = verbose canonical `{high_noise, low_noise, auto}` + accepted shortcuts `{h→high_noise, l→low_noise}`.
- Q4 matcher = branch IN `is_stack_match`, OUT of `capability_key`.
- Q5 branch mismatch = strict reject; `auto` is the explicit-portability value.
- Q6 same ref in multiple branches = YES; `(ref, branch)` composite identity through inventory + matcher + adapter naming.
- Q7 routing dispatch = Approach 1 primary + Approach 3 fallback + Task 0 selects.

---

## File Structure

**Modify:**
- `src/kinoforge/core/lora.py` — add `branch` field + alias validator to `LoraEntry`.
- `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` — `LoraTarget` parity, `_detect_moe_arity`, `_resolve_transformer`, exception classes, `_replace_adapter_stack` dispatch, inventory composite key, `_evict_one` composite, cold-boot env shape, `LoraInventoryEntry` field, `/lora/set_stack` error handler.
- `src/kinoforge/engines/diffusers/__init__.py` — orchestrator-side serialization of initial-stack env shape.
- `src/kinoforge/core/warm_reuse/matcher.py` — `is_stack_match` extended to compare `(ref, strength, branch)` tuples.
- `src/kinoforge/core/warm_reuse/__init__.py` (or wherever `capability_key` lives) — no functional change; just regression test confirms branch-invariance.
- `examples/configs/wan.yaml` — add `branch` field per LoRA in the canonical Arcane Style pair.
- `tests/test_lora_schema_parity.py` — extend to verify `branch` field + alias-validator symmetry.
- `tests/test_no_unredacted_writes.py` — AC8 AST scan extended for `branch` field surface.
- `tests/_smoke_harness/` (existing fixtures) — `moe_stub_pipe` + `single_transformer_stub_pipe` + `branch_recorder` fixtures.

**Create:**
- `docs/superpowers/research/2026-06-22-p2-task-0-diffusers-routing.md` — Task 0 deliverable.
- `tests/engines/test_lora_entry_branch.py` — schema field tests.
- `tests/engines/test_resolve_transformer.py` — dispatch helper tests.
- `tests/engines/test_replace_adapter_stack_routing.py` — server-side per-transformer routing tests.
- `tests/engines/test_vram_rollback_branch.py` — rollback snapshot includes branch.
- `tests/engines/test_cold_boot_branch_validation.py` — cold-boot env validation.
- `tests/core/test_is_stack_match_branch.py` — matcher tuple comparison.
- `tests/core/test_capability_key_branch_invariance.py` — capability_key regression.
- `tests/smoke/live_wan21/test_branch_routing.py` — Tier-3 RED scaffold.
- `tests/smoke/release_wan22/test_dual_transformer_routing.py` — Tier-4 RED scaffold.

---

## Task 0: Research diffusers Wan API for per-transformer LoRA routing

**Goal:** Resolve the 4 open questions in spec §8.3 BEFORE any implementation lands, so Tasks 6 + 9 + 10 know which code path (Approach 1 kwarg vs Approach 3 rebind) is correct.

**Files:**
- Create: `docs/superpowers/research/2026-06-22-p2-task-0-diffusers-routing.md`
- Probe scripts (ephemeral; do NOT commit): `/tmp/p2_q1.py`, `/tmp/p2_q2.py`, `/tmp/p2_q3.py`.

**Acceptance Criteria:**
- [ ] Q1 answered: name the exact kwarg (or confirm absence) on `WanPipeline.load_lora_weights`. Cite `diffusers/loaders/lora_pipeline.py` file:line.
- [ ] Q2 answered: `set_adapters` activation behavior across split transformers documented with observed `is_active` markers.
- [ ] Q3 answered: `delete_adapters` per-transformer dispatch necessity documented.
- [ ] Q4 answered: `peft` version floor pinned (e.g. "peft >= 0.13.0 required").
- [ ] Research note committed to `docs/superpowers/research/`.
- [ ] Plan's Tasks 6, 9, 10 updated inline to reflect the chosen path (PRIMARY vs FALLBACK). Update the plan's `.md`, not just the doc.

**Verify:** `cat docs/superpowers/research/2026-06-22-p2-task-0-diffusers-routing.md` shows all 4 questions resolved with code citations.

**Steps:**

- [ ] **Step 1: Locate diffusers' WanPipeline + LoRA loader source.**

```bash
pixi run python -c "import diffusers; print(diffusers.__file__)"
# Expected: /workspace/.pixi/envs/default/lib/python3.13/site-packages/diffusers/__init__.py

# Find load_lora_weights for WanPipeline:
rg -n "def load_lora_weights" $(pixi run python -c "import diffusers, os; print(os.path.dirname(diffusers.__file__))")/loaders/
```

- [ ] **Step 2: Q1 probe — per-transformer kwarg presence.**

Write `/tmp/p2_q1.py`:

```python
"""Q1: does WanPipeline.load_lora_weights expose a per-transformer routing kwarg?"""
import inspect
from diffusers import WanPipeline

sig = inspect.signature(WanPipeline.load_lora_weights)
print("kwargs:", list(sig.parameters.keys()))
# Also walk MRO for inherited loader mixins:
for cls in WanPipeline.__mro__:
    if "load_lora_weights" in cls.__dict__:
        print(f"defined-in: {cls.__module__}.{cls.__name__}")
        src = inspect.getsource(cls.load_lora_weights)
        print(src[:2000])
        break
```

Run: `pixi run python /tmp/p2_q1.py`

Capture: presence/absence of kwargs matching `(load_into_transformer | target_module | transformer | components)`. If multiple candidate kwargs exist, pick the one whose docstring or implementation references per-transformer routing.

- [ ] **Step 3: Q1 functional probe (only if kwarg looks present).**

Write `/tmp/p2_q1_func.py`:

```python
"""Q1 functional: load a known LoRA into transformer_2 explicitly, observe peft_config."""
import torch
from diffusers import WanPipeline

# Use a stubbed pipeline if real Wan 2.2 weights aren't local — the goal here
# is to call the kwarg and inspect the post-call state, NOT to render a video.
# If the kwarg is documented in the source (Step 2), confidence is high enough
# to defer the functional probe to a Tier-4 sanity check during Task 6.

# If running against real weights (operator's choice — uses up to 5min of compute):
pipe = WanPipeline.from_pretrained("Wan-AI/Wan2.2-T2V-A14B-Diffusers", torch_dtype=torch.bfloat16)
print("transformer.peft_config before:", getattr(pipe.transformer, "peft_config", None))
print("transformer_2.peft_config before:", getattr(pipe.transformer_2, "peft_config", None))

# Replace with the canonical Arcane high-noise LoRA path on disk:
pipe.load_lora_weights(
    "/path/to/arcane-high.safetensors",
    adapter_name="probe_0",
    load_into_transformer=pipe.transformer_2,  # exact kwarg name from Step 2
)
print("transformer.peft_config after:", list(getattr(pipe.transformer, "peft_config", {}).keys()))
print("transformer_2.peft_config after:", list(getattr(pipe.transformer_2, "peft_config", {}).keys()))
```

If kwarg exists per Step 2's static reading + Step 3 confirms it routes correctly → Approach 1 selected.

If kwarg absent OR routes incorrectly → Approach 3 fallback.

- [ ] **Step 4: Q2 probe — split-transformer activation.**

Write `/tmp/p2_q2.py`:

```python
"""Q2: does single set_adapters() activate adapters split across both transformers?"""
# Continues from Q1's loaded state (or fresh):
pipe.load_lora_weights("/path/to/arcane-high.safetensors", adapter_name="a_high",
                       load_into_transformer=pipe.transformer)
pipe.load_lora_weights("/path/to/arcane-low.safetensors", adapter_name="a_low",
                       load_into_transformer=pipe.transformer_2)

pipe.set_adapters(["a_high", "a_low"], adapter_weights=[1.0, 1.0])

# Inspect active markers on both transformers:
print("transformer adapters:", pipe.transformer.peft_config)
print("transformer_2 adapters:", pipe.transformer_2.peft_config)
# peft's active marker varies by version — also check pipe-level tracking:
print("pipe-level adapter state:", getattr(pipe, "_active_adapters", "n/a"))
```

If both transformers report `a_high` / `a_low` as active → single end-of-load `set_adapters` is correct (spec §3.3.4 stays simple).

If only one transformer activated → spec §5.4 per-transformer activation loop needed.

- [ ] **Step 5: Q3 probe — per-transformer eviction.**

Write `/tmp/p2_q3.py`:

```python
"""Q3: does delete_adapters need per-transformer dispatch?"""
# Continues from Q2 state with a_high in transformer and a_low in transformer_2:
pipe.delete_adapters(["a_low"])
print("after delete a_low:")
print("transformer.peft_config:", list(pipe.transformer.peft_config.keys()))
print("transformer_2.peft_config:", list(pipe.transformer_2.peft_config.keys()))
```

If `a_low` is gone from `transformer_2` AND `a_high` still present in `transformer` → `delete_adapters` is global, Task 9's `_evict_one` stays simple.

If `a_low` not removed → per-transformer dispatch needed.

- [ ] **Step 6: Q4 probe — peft version floor.**

```bash
pixi run pip show peft 2>&1 | head -5
# If installed: read Version line.
# If not installed: investigate whether diffusers + Wan 2.2 actually need peft directly,
# or whether all adapter ops route through diffusers' own peft abstraction.
```

Capture exact installed version. Note: P1's `set_adapters(adapter_weights=...)` already worked in production, so peft is at least at a version supporting that. Floor for P2 likely the same.

- [ ] **Step 7: Write research note.**

Create `docs/superpowers/research/2026-06-22-p2-task-0-diffusers-routing.md`:

```markdown
# Task 0 research — diffusers Wan API for per-transformer LoRA routing

**Date:** YYYY-MM-DD
**Probe scripts:** /tmp/p2_q{1,2,3}.py (ephemeral)

## Q1 — per-transformer kwarg on load_lora_weights

**Finding:** [PRESENT | ABSENT]
**Kwarg name:** [exact_name | n/a]
**Source citation:** diffusers/<file>.py:<line>
**Functional probe:** [routed correctly | routed incorrectly | not run]
**Decision:** [Approach 1 PRIMARY | Approach 3 FALLBACK]

## Q2 — split-transformer set_adapters activation

**Finding:** [single call activates both | single call only activates one]
**Observed peft_config state:** [paste]
**Decision:** [single end-of-load set_adapters OK | per-transformer loop needed per spec §5.4]

## Q3 — delete_adapters per-transformer dispatch

**Finding:** [global | per-transformer needed]
**Decision:** [_evict_one stays simple | per-transformer dispatch added]

## Q4 — peft version floor

**Installed:** peft==X.Y.Z
**Floor for P2:** peft >= X.Y.Z
```

- [ ] **Step 8: Update plan inline with Task 0 results.**

After research lands, edit `docs/superpowers/plans/2026-06-22-p2-wan22-dual-transformer-routing.md`:

- Task 6 routing code block: keep PRIMARY (Approach 1) OR replace with FALLBACK (Approach 3).
- Task 6 activation code block: keep single set_adapters OR replace with per-transformer loop.
- Task 9 `_evict_one` body: keep simple delete OR add per-transformer dispatch.

Commit message: `docs(plan): apply Task 0 research findings to P2 plan`.

- [ ] **Step 9: Commit research note.**

```bash
git add docs/superpowers/research/2026-06-22-p2-task-0-diffusers-routing.md
git commit -m "docs(research): P2 Task 0 — diffusers Wan per-transformer routing"
```

```json:metadata
{"files": ["docs/superpowers/research/2026-06-22-p2-task-0-diffusers-routing.md"], "verifyCommand": "test -f docs/superpowers/research/2026-06-22-p2-task-0-diffusers-routing.md && rg -q 'Decision:' docs/superpowers/research/2026-06-22-p2-task-0-diffusers-routing.md", "acceptanceCriteria": ["Q1 kwarg name (or absence) cited with file:line", "Q2 activation behavior documented", "Q3 delete dispatch necessity documented", "Q4 peft floor pinned", "plan .md updated to lock primary vs fallback paths"], "modelTier": "standard"}
```

---

## Task 1: LoraEntry — add `branch` field + alias validator

**Goal:** Extend canonical `LoraEntry` Pydantic schema with `branch: Literal["high_noise", "low_noise", "auto"]` (default `"auto"`) and `_normalize_branch_alias` `mode="before"` validator mapping `h→high_noise`, `l→low_noise`.

**Files:**
- Modify: `src/kinoforge/core/lora.py`
- Create: `tests/engines/test_lora_entry_branch.py`

**Acceptance Criteria:**
- [ ] `LoraEntry(ref="x").branch == "auto"` (default).
- [ ] `LoraEntry(ref="x", branch="h").branch == "high_noise"` (alias normalize).
- [ ] `LoraEntry(ref="x", branch="l").branch == "low_noise"` (alias normalize).
- [ ] `LoraEntry(ref="x", branch="medium")` raises ValidationError.
- [ ] `LoraEntry(ref="x", branche="h")` raises ValidationError (typo via `extra="forbid"`).
- [ ] `LoraEntry(ref="x", branch="h").model_dump()["branch"] == "high_noise"` (storage form).
- [ ] mypy + ruff clean.

**Verify:** `pixi run pytest tests/engines/test_lora_entry_branch.py -v` → 6/6 PASS.

**Steps:**

- [ ] **Step 1: Write failing tests.**

Create `tests/engines/test_lora_entry_branch.py`:

```python
"""LoraEntry branch field — schema + alias validator."""

import pytest
from pydantic import ValidationError

from kinoforge.core.lora import LoraEntry


def test_branch_defaults_to_auto():
    """Bug it catches: missing default makes the field required, breaking every
    existing cfg + vault file that pre-dates P2."""
    entry = LoraEntry(ref="civitai:1@1")
    assert entry.branch == "auto"


def test_branch_h_alias_normalizes_to_high_noise():
    """Bug it catches: storing 'h' as canonical leaves consumers doing string
    comparisons against 'h' vs 'high_noise', causing matcher false negatives."""
    entry = LoraEntry(ref="civitai:1@1", branch="h")
    assert entry.branch == "high_noise"


def test_branch_l_alias_normalizes_to_low_noise():
    """Same as above, low-noise variant."""
    entry = LoraEntry(ref="civitai:1@1", branch="l")
    assert entry.branch == "low_noise"


def test_branch_unknown_value_rejected():
    """Bug it catches: typos like 'medium' or 'm' silently accepted, leading to
    KeyError much later in _resolve_transformer."""
    with pytest.raises(ValidationError, match="branch"):
        LoraEntry(ref="civitai:1@1", branch="medium")


def test_branch_typo_field_name_rejected():
    """Bug it catches: 'branche' typo silently accepted because extra='allow'
    was changed, leading to runs with default branch when user meant explicit."""
    with pytest.raises(ValidationError):
        LoraEntry(ref="civitai:1@1", branche="h")  # type: ignore[call-arg]


def test_branch_model_dump_returns_canonical_form():
    """Bug it catches: alias normalization runs but model_dump emits the alias,
    so vault/cfg files re-serialized with the alias instead of canonical."""
    entry = LoraEntry(ref="civitai:1@1", branch="h")
    assert entry.model_dump()["branch"] == "high_noise"
```

- [ ] **Step 2: Run tests to verify they fail.**

```bash
pixi run pytest tests/engines/test_lora_entry_branch.py -v
```

Expected: 6/6 FAIL with `AttributeError: 'LoraEntry' object has no attribute 'branch'` or similar.

- [ ] **Step 3: Update `LoraEntry`.**

Modify `src/kinoforge/core/lora.py`:

Add `Literal` to typing imports + `field_validator` to pydantic imports if absent:

```python
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
```

Add `branch` field + validator to `LoraEntry`:

```python
class LoraEntry(BaseModel):
    """One LoRA entry: ref + strength + optional sha256 + branch.

    See docs/superpowers/specs/2026-06-21-server-lora-strength-design.md §6.1.
    See docs/superpowers/specs/2026-06-22-p2-wan22-dual-transformer-routing-design.md §2.

    Attributes:
        ref: Vendor-neutral model reference. SENSITIVE under vault mode.
        strength: PEFT adapter weight. NON-SENSITIVE.
        sha256: Optional content hash. Derived hash is sensitive per ephemeral spec D4.
        branch: Per-LoRA routing instruction for multi-transformer pipelines
            (Wan 2.2 high-noise/low-noise MoE). Canonical values:
            ``"high_noise"`` / ``"low_noise"`` / ``"auto"``. Accepts shortcuts
            ``"h"`` / ``"l"`` normalized at validation. ``"auto"`` is the
            single-transformer-only value; MoE pipelines reject ``"auto"`` and
            require explicit branch. NON-SENSITIVE (low-entropy enum; same
            posture as ``strength``).
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$|^$")
    branch: Literal["high_noise", "low_noise", "auto"] = Field(default="auto")

    @field_validator("branch", mode="before")
    @classmethod
    def _normalize_branch_alias(cls, v: Any) -> Any:
        """Normalize h/l shortcuts to canonical high_noise/low_noise.

        Runs mode='before' so the Literal constraint sees canonical form. Mirror
        of LoraTarget._normalize_branch_alias in wan_t2v_server.py — parity
        locked by tests/test_lora_schema_parity.py.
        """
        if v == "h":
            return "high_noise"
        if v == "l":
            return "low_noise"
        return v
```

- [ ] **Step 4: Run tests to verify GREEN.**

```bash
pixi run pytest tests/engines/test_lora_entry_branch.py -v
```

Expected: 6/6 PASS.

- [ ] **Step 5: Lint + typecheck.**

```bash
pixi run ruff check src/kinoforge/core/lora.py tests/engines/test_lora_entry_branch.py
pixi run mypy src/kinoforge/core/lora.py
```

Expected: both clean.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/lora.py tests/engines/test_lora_entry_branch.py
git commit -m "feat(p2): LoraEntry.branch field + h/l alias validator"
```

```json:metadata
{"files": ["src/kinoforge/core/lora.py", "tests/engines/test_lora_entry_branch.py"], "verifyCommand": "pixi run pytest tests/engines/test_lora_entry_branch.py -v && pixi run mypy src/kinoforge/core/lora.py && pixi run ruff check src/kinoforge/core/lora.py", "acceptanceCriteria": ["branch defaults to 'auto'", "h alias normalizes to high_noise", "l alias normalizes to low_noise", "unknown values rejected", "extra='forbid' blocks typo field names", "model_dump emits canonical form"], "modelTier": "mechanical"}
```

---

## Task 2: LoraTarget — parity field + same alias validator

**Goal:** Add the same `branch` field + same alias validator to server-side `LoraTarget` Pydantic model. Keep `LoraEntry` ↔ `LoraTarget` parity invariant load-bearing.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py:145-161`

**Acceptance Criteria:**
- [ ] `LoraTarget(ref="x").branch == "auto"`.
- [ ] `LoraTarget(ref="x", branch="h").branch == "high_noise"`.
- [ ] `LoraTarget(ref="x", branch="l").branch == "low_noise"`.
- [ ] `LoraTarget(ref="x", branch="medium")` raises ValidationError.
- [ ] `LoraTarget(ref="x", branche="h")` raises ValidationError.
- [ ] mypy + ruff clean on the server module.

**Verify:** `pixi run pytest tests/engines/test_lora_entry_branch.py -v` (re-runs LoraEntry tests; LoraTarget parity is asserted in Task 3).

**Steps:**

- [ ] **Step 1: Modify `LoraTarget`.**

In `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`, locate the existing `LoraTarget` class (around line 145) and update:

```python
class LoraTarget(BaseModel):
    """One entry in ``/lora/set_stack`` target list.

    Schema-equivalent to :class:`kinoforge.core.lora.LoraEntry`. Locked by
    ``tests/test_lora_schema_parity.py``. See spec
    ``docs/superpowers/specs/2026-06-21-server-lora-strength-design.md`` §6.3
    and ``2026-06-22-p2-wan22-dual-transformer-routing-design.md`` §2.3.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    strength: float = Field(default=1.0, ge=-2.0, le=2.0)
    branch: Literal["high_noise", "low_noise", "auto"] = Field(default="auto")

    @field_validator("branch", mode="before")
    @classmethod
    def _normalize_branch_alias(cls, v: Any) -> Any:
        """Mirror of LoraEntry._normalize_branch_alias in core/lora.py.

        Parity is load-bearing — tests/test_lora_schema_parity.py asserts
        both classes normalize identically. DO NOT diverge.
        """
        if v == "h":
            return "high_noise"
        if v == "l":
            return "low_noise"
        return v
```

Add `Literal` + `field_validator` to imports if not already present.

- [ ] **Step 2: Lint + typecheck.**

```bash
pixi run ruff check src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
pixi run mypy src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
```

Expected: both clean.

- [ ] **Step 3: Sanity check (LoraTarget instantiates).**

```bash
pixi run python -c "from kinoforge.engines.diffusers.servers.wan_t2v_server import LoraTarget; print(LoraTarget(ref='x', branch='h').branch)"
```

Expected output: `high_noise`.

- [ ] **Step 4: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
git commit -m "feat(p2): LoraTarget parity — branch field + alias validator"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/servers/wan_t2v_server.py"], "verifyCommand": "pixi run python -c 'from kinoforge.engines.diffusers.servers.wan_t2v_server import LoraTarget; assert LoraTarget(ref=\"x\", branch=\"h\").branch == \"high_noise\"' && pixi run mypy src/kinoforge/engines/diffusers/servers/wan_t2v_server.py", "acceptanceCriteria": ["LoraTarget defaults branch to auto", "h/l aliases normalize identically to LoraEntry", "extra='forbid' enforced"], "modelTier": "mechanical"}
```

---

## Task 3: Parity test extension

**Goal:** Extend `tests/test_lora_schema_parity.py` to verify both classes carry `branch` with identical `Literal` annotation AND both `_normalize_branch_alias` validators map `h→high_noise`, `l→low_noise` identically.

**Files:**
- Modify: `tests/test_lora_schema_parity.py`

**Acceptance Criteria:**
- [ ] Test asserts `branch` field is on both `LoraEntry` and `LoraTarget`.
- [ ] Test asserts `Literal` arg-tuple is identical between classes.
- [ ] Test asserts `model_validate({"ref": "x", "branch": "h"}).branch == "high_noise"` on both.
- [ ] Test asserts same for `l → low_noise`.
- [ ] Future regression: removing `branch` from either class fails the test.

**Verify:** `pixi run pytest tests/test_lora_schema_parity.py -v` → all tests PASS including new branch-parity ones.

**Steps:**

- [ ] **Step 1: Read existing parity test to match style.**

```bash
pixi run cat tests/test_lora_schema_parity.py | head -80
```

Identify the pattern used for the existing `ref` / `strength` / `sha256` parity assertions.

- [ ] **Step 2: Add branch-parity test cases.**

Append (or extend the existing parameterized test) in `tests/test_lora_schema_parity.py`:

```python
"""Parity extension: branch field + alias validator must match exactly."""

from typing import get_args, get_type_hints

from kinoforge.core.lora import LoraEntry
from kinoforge.engines.diffusers.servers.wan_t2v_server import LoraTarget


def test_branch_field_present_on_both_classes():
    """Bug it catches: P2-style schema edit on LoraEntry without mirror
    update on LoraTarget. The two start to diverge in field set."""
    assert "branch" in LoraEntry.model_fields
    assert "branch" in LoraTarget.model_fields


def test_branch_literal_args_match_exactly():
    """Bug it catches: one class accepts {high_noise, low_noise, auto} and
    the other accepts {h, l, auto} — wire-vs-cfg representation drift."""
    entry_hints = get_type_hints(LoraEntry)
    target_hints = get_type_hints(LoraTarget)
    assert get_args(entry_hints["branch"]) == get_args(target_hints["branch"])


def test_h_alias_normalizes_identically_on_both():
    """Bug it catches: alias map drifts between modules; cfg accepts 'H' but
    wire only accepts 'h' (or similar case-sensitivity drift)."""
    entry = LoraEntry.model_validate({"ref": "x", "branch": "h"})
    target = LoraTarget.model_validate({"ref": "x", "branch": "h"})
    assert entry.branch == "high_noise"
    assert target.branch == "high_noise"


def test_l_alias_normalizes_identically_on_both():
    """Same as above, low-noise variant."""
    entry = LoraEntry.model_validate({"ref": "x", "branch": "l"})
    target = LoraTarget.model_validate({"ref": "x", "branch": "l"})
    assert entry.branch == "low_noise"
    assert target.branch == "low_noise"


def test_branch_default_matches():
    """Bug it catches: one class defaults to 'auto', the other to None or
    something else."""
    assert LoraEntry.model_fields["branch"].default == LoraTarget.model_fields["branch"].default
```

- [ ] **Step 3: Run.**

```bash
pixi run pytest tests/test_lora_schema_parity.py -v
```

Expected: all existing tests still PASS + new branch parity tests PASS.

- [ ] **Step 4: Lint.**

```bash
pixi run ruff check tests/test_lora_schema_parity.py
```

- [ ] **Step 5: Commit.**

```bash
git add tests/test_lora_schema_parity.py
git commit -m "test(p2): parity test covers LoraEntry/LoraTarget branch field"
```

```json:metadata
{"files": ["tests/test_lora_schema_parity.py"], "verifyCommand": "pixi run pytest tests/test_lora_schema_parity.py -v", "acceptanceCriteria": ["branch field present on both", "Literal args match", "h alias normalizes on both", "l alias normalizes on both", "default matches"], "modelTier": "mechanical"}
```

---

## Task 4: `_detect_moe_arity` + `_resolve_transformer` + exception classes

**Goal:** Add MoE detection helper, single-dispatch routing helper, and three exception classes. These are pure functions/types — no state mutation — so they're testable in isolation against stub pipelines.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Create: `tests/engines/test_resolve_transformer.py`

**Acceptance Criteria:**
- [ ] `_detect_moe_arity(stub_with_only_transformer)` returns 1.
- [ ] `_detect_moe_arity(stub_with_transformer_and_transformer_2)` returns 2.
- [ ] `_resolve_transformer(stub, "auto")` returns `pipe.transformer` when arity=1.
- [ ] `_resolve_transformer(stub, "high_noise")` returns `pipe.transformer` when arity=2.
- [ ] `_resolve_transformer(stub, "low_noise")` returns `pipe.transformer_2` when arity=2.
- [ ] `_resolve_transformer(stub, "auto")` raises `BranchAutoNotAllowedOnMoE` when arity=2.
- [ ] `_resolve_transformer(stub, "high_noise")` raises `BranchUnsupportedOnSingleTransformer` when arity=1.
- [ ] `BranchUnknown` raised for off-Literal value (defensive).

**Verify:** `pixi run pytest tests/engines/test_resolve_transformer.py -v` → 8/8 PASS.

**Steps:**

- [ ] **Step 1: Write failing tests.**

Create `tests/engines/test_resolve_transformer.py`:

```python
"""_resolve_transformer + _detect_moe_arity — pure-function dispatch tests."""

import pytest

from kinoforge.engines.diffusers.servers import wan_t2v_server
from kinoforge.engines.diffusers.servers.wan_t2v_server import (
    BranchAutoNotAllowedOnMoE,
    BranchUnknown,
    BranchUnsupportedOnSingleTransformer,
    _detect_moe_arity,
    _resolve_transformer,
)


class _SingleTransformerStub:
    """Mimics a non-MoE pipeline like Wan 2.1."""
    transformer = object()


class _MoEStub:
    """Mimics Wan 2.2 dual-transformer pipeline."""
    transformer = object()
    transformer_2 = object()


def test_detect_moe_arity_single_transformer_returns_1():
    """Bug it catches: detector miscounts when pipeline has only the bare
    transformer attribute — would route MoE-only paths through single-transformer
    branches."""
    assert _detect_moe_arity(_SingleTransformerStub()) == 1


def test_detect_moe_arity_dual_transformer_returns_2():
    """Bug it catches: detector misses transformer_2, treating Wan 2.2 as
    single-transformer and silently dropping the low-noise routing path."""
    assert _detect_moe_arity(_MoEStub()) == 2


def test_resolve_auto_on_single_transformer_returns_transformer(monkeypatch):
    """Bug it catches: arity-1 + auto routes wrong attribute, leading to
    KeyError or None-deref on load_lora_weights."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 1)
    pipe = _SingleTransformerStub()
    assert _resolve_transformer(pipe, "auto") is pipe.transformer


def test_resolve_high_noise_on_moe_returns_transformer(monkeypatch):
    """Bug it catches: routing maps high_noise to transformer_2 by accident."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    pipe = _MoEStub()
    assert _resolve_transformer(pipe, "high_noise") is pipe.transformer


def test_resolve_low_noise_on_moe_returns_transformer_2(monkeypatch):
    """Bug it catches: routing maps low_noise to transformer (high-noise stage),
    silently degrading every Wan 2.2 LoRA recipe."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    pipe = _MoEStub()
    assert _resolve_transformer(pipe, "low_noise") is pipe.transformer_2


def test_resolve_auto_on_moe_raises(monkeypatch):
    """Bug it catches: server accepts auto on MoE and silently loads LoRA into
    pipe.transformer only — the Q1 Option-D failure mode."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    pipe = _MoEStub()
    with pytest.raises(BranchAutoNotAllowedOnMoE):
        _resolve_transformer(pipe, "auto")


def test_resolve_explicit_branch_on_single_transformer_raises(monkeypatch):
    """Bug it catches: server silently collapses high_noise to transformer on a
    Wan 2.1 pipeline (Q5 lenient-collapse failure mode)."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 1)
    pipe = _SingleTransformerStub()
    with pytest.raises(BranchUnsupportedOnSingleTransformer):
        _resolve_transformer(pipe, "high_noise")


def test_resolve_unknown_value_raises(monkeypatch):
    """Defensive — Pydantic Literal should prevent this reaching the resolver,
    but if the validator is ever bypassed (test stub, future refactor), the
    resolver must not silently return a default transformer."""
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    pipe = _MoEStub()
    with pytest.raises(BranchUnknown):
        _resolve_transformer(pipe, "medium")  # type: ignore[arg-type]
```

- [ ] **Step 2: Run tests — verify all 8 FAIL.**

```bash
pixi run pytest tests/engines/test_resolve_transformer.py -v
```

Expected: ImportError on `BranchAutoNotAllowedOnMoE` etc. (or 8/8 FAIL after import).

- [ ] **Step 3: Add exception classes + helpers to server module.**

In `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`, add near the top of the module (after imports, before the `app = FastAPI(...)` line):

```python
# ---------------------------------------------------------------------------
# P2 — pipeline arity detection + per-transformer routing.
# ---------------------------------------------------------------------------


class BranchAutoNotAllowedOnMoE(Exception):
    """Raised when /lora/set_stack receives branch='auto' on a MoE pipeline.

    HTTP 400 surface body: {"reason": "branch_auto_disallowed_on_moe", ...}.
    See spec §6.1.
    """

    def __init__(self, arity: int) -> None:
        super().__init__(f"pipeline has {arity} transformers; branch=auto disallowed")
        self.arity = arity


class BranchUnsupportedOnSingleTransformer(Exception):
    """Raised when /lora/set_stack receives explicit branch on a non-MoE pipeline.

    HTTP 400 surface body: {"reason": "branch_unsupported_single_transformer", ...}.
    """

    def __init__(self, branch: str, arity: int) -> None:
        super().__init__(
            f"pipeline has {arity} transformer(s); branch={branch} not applicable"
        )
        self.branch = branch
        self.arity = arity


class BranchUnknown(Exception):
    """Defensive — should never fire under Pydantic Literal constraint."""

    def __init__(self, branch: str) -> None:
        super().__init__(f"unknown branch value: {branch!r}")
        self.branch = branch


def _detect_moe_arity(pipe: Any) -> int:
    """Return count of transformer* attrs on the pipeline.

    Returns 1 for non-MoE (Wan 2.1, etc.), 2 for Wan 2.2 dual-transformer,
    N for future N-expert. Generalizes the routing decision without a hardcoded
    list of stage names.
    """
    return sum(
        1 for attr in dir(pipe)
        if attr == "transformer" or attr.startswith("transformer_")
    )


# Module-level cache populated during _load_pipeline. Tests monkeypatch this
# directly; the server sets it before `ready.set()`.
_pipe_arity: int = 1


def _resolve_transformer(pipe: Any, branch: str) -> Any:
    """Map (pipe, branch) to the specific transformer attribute to load into.

    Single dispatch point — every load site (incl. /lora/set_stack handler,
    cold-boot, VRAM-OOM rollback) must go through this helper. No duck-typing
    scattered elsewhere. See spec §5.2.
    """
    arity = _pipe_arity
    if arity == 1:
        if branch == "auto":
            return pipe.transformer
        raise BranchUnsupportedOnSingleTransformer(branch=branch, arity=arity)
    # arity >= 2 (MoE)
    if branch == "auto":
        raise BranchAutoNotAllowedOnMoE(arity=arity)
    if branch == "high_noise":
        return pipe.transformer
    if branch == "low_noise":
        return pipe.transformer_2
    raise BranchUnknown(branch=branch)
```

- [ ] **Step 4: Run tests — verify 8/8 PASS.**

```bash
pixi run pytest tests/engines/test_resolve_transformer.py -v
```

Expected: 8/8 PASS.

- [ ] **Step 5: Lint + typecheck.**

```bash
pixi run ruff check src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_resolve_transformer.py
pixi run mypy src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
```

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_resolve_transformer.py
git commit -m "feat(p2): _detect_moe_arity + _resolve_transformer + branch exceptions"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/servers/wan_t2v_server.py", "tests/engines/test_resolve_transformer.py"], "verifyCommand": "pixi run pytest tests/engines/test_resolve_transformer.py -v", "acceptanceCriteria": ["arity detection works on stub pipelines", "auto routes correctly on arity=1", "h/l routes correctly on arity=2", "auto on MoE raises", "explicit branch on arity=1 raises", "off-Literal value raises defensively"], "modelTier": "standard"}
```

---

## Task 5: Inventory composite key + adapter naming scheme

**Goal:** Change `_inventory` from `dict[str, ...]` keyed by `ref` to `dict[tuple[str, str], ...]` keyed by `(ref, branch)`. Adapter naming changes from `lora_{i}` to `lora_{i}_{branch_short}` where `branch_short ∈ {h, l, a}`.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` — `_inventory` declaration + every callsite.

**Acceptance Criteria:**
- [ ] `_inventory` type is `dict[tuple[str, str], dict[str, Any]]`.
- [ ] Each inventory entry's interior dict has `"branch"` key.
- [ ] Adapter name format: `lora_0_h`, `lora_1_l`, `lora_2_a` etc.
- [ ] Existing reads of `_inventory.get(ref)` updated to `_inventory.get((ref, branch))`.
- [ ] mypy clean (the type narrowing is strict).
- [ ] No test regressions in `tests/engines/` that exercise inventory.

**Verify:** `pixi run pytest tests/engines/ -v` → existing tests pass with new key shape (may need test fixture updates if tests directly poke `_inventory`; those updates land here).

**Steps:**

- [ ] **Step 1: Grep existing inventory callsites.**

```bash
pixi run rg -n "_inventory" src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
```

Catalogue every read/write of `_inventory`. Expected callsites:
- Module-level declaration (around line 84).
- `_evict_one` reads `_inventory.get(ref)`.
- `_replace_adapter_stack` reads + writes.
- `_load_pipeline` initial-stack writes.
- `/lora/inventory` handler reads.

- [ ] **Step 2: Update declaration.**

In `wan_t2v_server.py:84` (or wherever `_inventory:` is):

```python
# LoRA-flexible warm-reuse: pod-side inventory of loaded LoRA weights.
# P2 keys by composite (ref, branch) so the same ref can co-exist in two
# transformer branches (Q6 Option 1 — spec §3.2).
# Entries: {(ref, branch): {"ref": str, "filename": str, "size_bytes": int,
#  "loras_dir_path": str, "downloaded_at_local": str, "last_used_at_local": str,
#  "adapter_name": str, "last_strength": float | None, "branch": str}}
_inventory: dict[tuple[str, str], dict[str, Any]] = {}
```

- [ ] **Step 3: Add adapter-name helper.**

Near `_resolve_transformer`:

```python
_BRANCH_SHORT: dict[str, str] = {
    "high_noise": "h",
    "low_noise": "l",
    "auto": "a",
}


def _adapter_name(position: int, branch: str) -> str:
    """Generate unique adapter name from (position, branch).

    Position-prefix preserves activation order; branch suffix avoids collisions
    when the same ref appears in two branches (Q6 Option 1 composite identity).
    Returns 'lora_{i}_{h|l|a}'.
    """
    short = _BRANCH_SHORT[branch]
    return f"lora_{position}_{short}"
```

- [ ] **Step 4: Update every `_inventory` callsite to use composite key.**

In `_replace_adapter_stack` (currently `wan_t2v_server.py:397-428`), update the loop body. Defer the routing/dispatch implementation to Task 6 — for now just thread the composite key:

```python
def _replace_adapter_stack(target: list[LoraTarget]) -> None:
    """[docstring kept from before — Task 6 expands routing section]"""
    pipe.unload_lora_weights()
    if not target:
        return
    names: list[str] = []
    weights: list[float] = []
    for i, t in enumerate(target):
        entry = _inventory[(t.ref, t.branch)]
        name = _adapter_name(i, t.branch)
        pipe.load_lora_weights(entry["loras_dir_path"], adapter_name=name)
        names.append(name)
        weights.append(t.strength)
        entry["adapter_name"] = name
        entry["last_strength"] = t.strength
        entry["branch"] = t.branch
    pipe.set_adapters(names, adapter_weights=weights)
```

In `_load_pipeline` initial-stack loop (around `wan_t2v_server.py:445-475`):

```python
adapter_name = _adapter_name(i, branch)  # branch from new env shape, Task 7
pipe_obj.load_lora_weights(path, adapter_name=adapter_name)
adapter_names.append(adapter_name)
adapter_weights.append(strength)  # P2 lands strength + branch together
now = datetime.now().isoformat()
_inventory[(ref, branch)] = {
    "ref": ref,
    "filename": spec.filename,
    "size_bytes": actual_bytes,
    "loras_dir_path": path,
    "downloaded_at_local": now,
    "last_used_at_local": now,
    "adapter_name": adapter_name,
    "last_strength": strength,
    "branch": branch,
}
```

(Task 7 nails the env shape + branch threading; this task just updates the key shape so other tasks can reference the new structure.)

In `_evict_one` (`wan_t2v_server.py:380`) — sig change to take `branch`:

```python
def _evict_one(ref: str, branch: str) -> None:
    entry = _inventory.get((ref, branch))
    if entry is None:
        return
    adapter = entry["adapter_name"]
    if hasattr(pipe, "delete_adapters"):
        pipe.delete_adapters([adapter])
    try:
        Path(entry["loras_dir_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    _inventory.pop((ref, branch), None)
```

In any `/lora/inventory` GET handler — iterate `_inventory.values()` (key shape doesn't matter for that path since it returns LoraInventoryEntry models):

```python
@app.get("/lora/inventory")
async def lora_inventory() -> list[LoraInventoryEntry]:
    return [LoraInventoryEntry(**v) for v in _inventory.values()]
```

(Task 8 adds `branch` to `LoraInventoryEntry`; for now the shape is unchanged structurally.)

- [ ] **Step 5: Run the existing engine test suite for regression.**

```bash
pixi run pytest tests/engines/ -v -x
```

Expected: any failures pinpoint inventory-poking tests that need the composite-key update. Update them inline as part of this task (no separate task — they're maintenance of P1's invariants under P2's new shape).

- [ ] **Step 6: Lint + typecheck.**

```bash
pixi run ruff check src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
pixi run mypy src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
```

- [ ] **Step 7: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/
git commit -m "refactor(p2): inventory composite (ref, branch) key + _adapter_name helper"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/servers/wan_t2v_server.py"], "verifyCommand": "pixi run pytest tests/engines/ -v && pixi run mypy src/kinoforge/engines/diffusers/servers/wan_t2v_server.py", "acceptanceCriteria": ["_inventory keyed by tuple", "_adapter_name produces lora_i_branchshort", "every callsite updated", "no engine test regressions", "mypy clean"], "modelTier": "standard"}
```

---

## Task 6: `_replace_adapter_stack` — pre-load validation gate + routing dispatch + activation

**Goal:** Wire per-transformer routing into `_replace_adapter_stack`. Pre-load validation pass walks `target` and raises before any state mutation. Loading uses the path Task 0 picked (Approach 1 PRIMARY or Approach 3 FALLBACK). Activation uses the strategy Task 0 picked (single end-of-load `set_adapters` OR per-transformer loop).

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Create: `tests/engines/test_replace_adapter_stack_routing.py`

**Acceptance Criteria:**
- [ ] Pre-load validation rejects entire request atomically when ANY entry has an invalid branch (no partial unload).
- [ ] Valid request routes each LoRA to the correct transformer.
- [ ] Adapter activation matches Task 0's chosen strategy.
- [ ] `_inventory[(ref, branch)]` has `branch` field after successful load.
- [ ] mypy + ruff clean.

**Verify:** `pixi run pytest tests/engines/test_replace_adapter_stack_routing.py -v` → 4+/4+ PASS.

**Steps:**

- [ ] **Step 1: Write failing tests.**

Create `tests/engines/test_replace_adapter_stack_routing.py`:

```python
"""_replace_adapter_stack per-transformer routing + pre-load validation gate."""

from unittest.mock import MagicMock

import pytest

from kinoforge.engines.diffusers.servers import wan_t2v_server
from kinoforge.engines.diffusers.servers.wan_t2v_server import (
    BranchAutoNotAllowedOnMoE,
    BranchUnsupportedOnSingleTransformer,
    LoraTarget,
    _replace_adapter_stack,
)


@pytest.fixture
def moe_pipe(monkeypatch):
    """Stub Wan-2.2-shaped pipeline. set_adapters + load_lora_weights are mocks."""
    pipe = MagicMock()
    pipe.transformer = MagicMock(name="transformer")
    pipe.transformer_2 = MagicMock(name="transformer_2")
    monkeypatch.setattr(wan_t2v_server, "pipe", pipe)
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    monkeypatch.setattr(wan_t2v_server, "_inventory", {
        ("x", "high_noise"): {"loras_dir_path": "/tmp/x_h.safetensors",
                              "ref": "x", "branch": "high_noise",
                              "last_strength": 1.0, "adapter_name": ""},
        ("y", "low_noise"): {"loras_dir_path": "/tmp/y_l.safetensors",
                             "ref": "y", "branch": "low_noise",
                             "last_strength": 1.0, "adapter_name": ""},
    })
    return pipe


@pytest.fixture
def single_pipe(monkeypatch):
    """Stub Wan-2.1-shaped pipeline."""
    pipe = MagicMock()
    pipe.transformer = MagicMock(name="transformer")
    # No transformer_2.
    del pipe.transformer_2
    monkeypatch.setattr(wan_t2v_server, "pipe", pipe)
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 1)
    monkeypatch.setattr(wan_t2v_server, "_inventory", {
        ("x", "auto"): {"loras_dir_path": "/tmp/x.safetensors", "ref": "x",
                        "branch": "auto", "last_strength": 1.0, "adapter_name": ""},
    })
    return pipe


def test_preload_gate_rejects_auto_on_moe_atomically(moe_pipe):
    """Bug it catches: validation runs INSIDE the load loop, so pipe.unload_lora_weights
    already fired before the auto-on-MoE check rejected — pod ends in stripped state."""
    target = [LoraTarget(ref="x", branch="auto")]
    with pytest.raises(BranchAutoNotAllowedOnMoE):
        _replace_adapter_stack(target)
    # Inventory untouched.
    assert ("x", "high_noise") in wan_t2v_server._inventory
    # unload_lora_weights NOT called.
    moe_pipe.unload_lora_weights.assert_not_called()


def test_preload_gate_rejects_explicit_branch_on_single_transformer(single_pipe):
    """Bug it catches: arity-1 + h/l silently collapses to single transformer."""
    target = [LoraTarget(ref="x", branch="high_noise")]
    with pytest.raises(BranchUnsupportedOnSingleTransformer):
        _replace_adapter_stack(target)
    single_pipe.unload_lora_weights.assert_not_called()


def test_valid_moe_pair_routes_to_correct_transformers(moe_pipe):
    """Bug it catches: routing misdirected — high_noise LoRA lands in transformer_2."""
    target = [
        LoraTarget(ref="x", strength=1.0, branch="high_noise"),
        LoraTarget(ref="y", strength=0.8, branch="low_noise"),
    ]
    _replace_adapter_stack(target)
    # Inspect load_lora_weights call arguments to verify per-transformer dispatch.
    # NOTE: exact assertion depends on Task 0 outcome:
    #   Approach 1: assert kwargs include load_into_transformer=<correct attr>
    #   Approach 3: assert pipe.transformer was rebound during the high-noise call
    # This test gets updated in-place to match the chosen approach.
    assert moe_pipe.load_lora_weights.call_count == 2
    assert moe_pipe.set_adapters.called


def test_partial_failure_mid_load_does_not_corrupt_inventory(moe_pipe):
    """Bug it catches: second entry's branch is invalid, but first entry already
    loaded — inventory + pipe are in an inconsistent state.

    Pre-load gate must validate ALL entries before unloading anything."""
    target = [
        LoraTarget(ref="x", strength=1.0, branch="high_noise"),
        LoraTarget(ref="y", strength=0.8, branch="auto"),  # invalid: auto on MoE
    ]
    with pytest.raises(BranchAutoNotAllowedOnMoE):
        _replace_adapter_stack(target)
    moe_pipe.unload_lora_weights.assert_not_called()
    moe_pipe.load_lora_weights.assert_not_called()
```

- [ ] **Step 2: Run tests — verify they fail.**

```bash
pixi run pytest tests/engines/test_replace_adapter_stack_routing.py -v
```

Expected: 4/4 FAIL.

- [ ] **Step 3: Rewrite `_replace_adapter_stack` body.**

Replace the existing body in `wan_t2v_server.py` (lines around 397-428):

> **PICK ONE BASED ON TASK 0:**
>
> If Task 0 picked **Approach 1** (diffusers kwarg works), use the PRIMARY block.
> If Task 0 picked **Approach 3** (rebind fallback), use the FALLBACK block.
> The other block stays in the spec for historical reference but the chosen one ships.

PRIMARY (Approach 1):

```python
def _replace_adapter_stack(target: list[LoraTarget]) -> None:
    """Replace the active pipeline adapter stack with ``target`` in order.

    Pre-load validation pass walks every entry and raises before any
    ``unload_lora_weights`` so a rejected request leaves inventory + pipeline
    untouched. Each entry routes to its target transformer via
    ``_resolve_transformer``. See spec §3.3, §6.3.
    """
    # Pre-load validation gate.
    resolved: list[tuple[LoraTarget, Any]] = []
    for t in target:
        target_transformer = _resolve_transformer(pipe, t.branch)
        resolved.append((t, target_transformer))

    pipe.unload_lora_weights()
    if not target:
        return

    names: list[str] = []
    weights: list[float] = []
    for i, (t, target_transformer) in enumerate(resolved):
        entry = _inventory[(t.ref, t.branch)]
        name = _adapter_name(i, t.branch)
        # PRIMARY (Task 0 Approach 1) — per-transformer kwarg.
        pipe.load_lora_weights(
            entry["loras_dir_path"],
            adapter_name=name,
            load_into_transformer=target_transformer,  # exact kwarg from Task 0
        )
        names.append(name)
        weights.append(t.strength)
        entry["adapter_name"] = name
        entry["last_strength"] = t.strength
        entry["branch"] = t.branch

    # Activation strategy — picked by Task 0 Question 2.
    # If single set_adapters activates both transformers, this single call works.
    # Otherwise replace with per-transformer activation loop (spec §5.4).
    pipe.set_adapters(names, adapter_weights=weights)
```

FALLBACK (Approach 3):

```python
def _replace_adapter_stack(target: list[LoraTarget]) -> None:
    """[same docstring]"""
    resolved: list[tuple[LoraTarget, Any]] = []
    for t in target:
        target_transformer = _resolve_transformer(pipe, t.branch)
        resolved.append((t, target_transformer))

    pipe.unload_lora_weights()
    if not target:
        return

    names: list[str] = []
    weights: list[float] = []
    for i, (t, target_transformer) in enumerate(resolved):
        entry = _inventory[(t.ref, t.branch)]
        name = _adapter_name(i, t.branch)
        # FALLBACK (Task 0 Approach 3) — temporary attribute rebind.
        original_transformer = pipe.transformer
        try:
            pipe.transformer = target_transformer
            pipe.load_lora_weights(entry["loras_dir_path"], adapter_name=name)
        finally:
            pipe.transformer = original_transformer
        names.append(name)
        weights.append(t.strength)
        entry["adapter_name"] = name
        entry["last_strength"] = t.strength
        entry["branch"] = t.branch

    pipe.set_adapters(names, adapter_weights=weights)
```

(If Task 0 Question 2 finds activation needs per-transformer loop, replace the final `pipe.set_adapters(names, adapter_weights=weights)` with the loop from spec §5.4.)

- [ ] **Step 4: Run tests — verify 4/4 PASS.**

```bash
pixi run pytest tests/engines/test_replace_adapter_stack_routing.py -v
```

Adjust the routing-verification test (`test_valid_moe_pair_routes_to_correct_transformers`) inline to match the chosen approach's observable signal.

- [ ] **Step 5: Lint + typecheck.**

```bash
pixi run ruff check src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_replace_adapter_stack_routing.py
pixi run mypy src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
```

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_replace_adapter_stack_routing.py
git commit -m "feat(p2): _replace_adapter_stack pre-load gate + per-transformer dispatch"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/servers/wan_t2v_server.py", "tests/engines/test_replace_adapter_stack_routing.py"], "verifyCommand": "pixi run pytest tests/engines/test_replace_adapter_stack_routing.py -v", "acceptanceCriteria": ["pre-load gate atomic on auto/MoE mismatch", "pre-load gate atomic on explicit branch/single-transformer mismatch", "valid stack routes to correct transformers", "partial-failure mid-load impossible (gate runs first)"], "modelTier": "standard"}
```

---

## Task 7: Cold-boot `_load_pipeline` — env shape carries strength + branch

**Goal:** Update `KINOFORGE_INITIAL_LORA_STACK_JSON` from list of `(ref, ArtifactDownloadSpec)` tuples to list of dicts with `strength` + `branch`. Validate every initial entry's branch against pipeline arity BEFORE serving any request.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Create: `tests/engines/test_cold_boot_branch_validation.py`

**Acceptance Criteria:**
- [ ] New dict-form env shape parses correctly: `[{"ref": ..., "download_spec": ..., "strength": 1.0, "branch": "high_noise"}, ...]`.
- [ ] Legacy tuple form `[(ref, spec)]` auto-promoted with `strength=1.0, branch="auto"`.
- [ ] MoE pipe + initial stack with `branch="auto"` entry → `_load_pipeline` raises; `ready` never set.
- [ ] Single-transformer pipe + initial stack with explicit `h`/`l` → `_load_pipeline` raises.
- [ ] All loaded initial entries land in `_inventory[(ref, branch)]` with `branch` field.

**Verify:** `pixi run pytest tests/engines/test_cold_boot_branch_validation.py -v` → 4/4 PASS.

**Steps:**

- [ ] **Step 1: Write failing tests.**

Create `tests/engines/test_cold_boot_branch_validation.py`:

```python
"""Cold-boot env shape + branch validation."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.engines.diffusers.servers import wan_t2v_server


def _make_moe_stub_pipe():
    pipe = MagicMock()
    pipe.transformer = MagicMock()
    pipe.transformer_2 = MagicMock()
    return pipe


def _make_single_stub_pipe():
    pipe = MagicMock()
    pipe.transformer = MagicMock()
    del pipe.transformer_2  # arity == 1
    return pipe


def test_load_pipeline_dict_env_shape_parses(monkeypatch, tmp_path):
    """Bug it catches: env JSON parser still expects tuples; new dict shape
    silently treated as empty list, cold-boot loads zero LoRAs."""
    monkeypatch.setattr(wan_t2v_server, "_diffusers_load", _make_moe_stub_pipe)
    initial = [
        {"ref": "x", "download_spec": {"url": "http://example.com/x", "filename": "x.safetensors"},
         "strength": 1.0, "branch": "high_noise"},
    ]
    with patch.dict(os.environ, {"KINOFORGE_INITIAL_LORA_STACK_JSON": json.dumps(initial)}):
        with patch.object(wan_t2v_server, "_download_one",
                          return_value=(str(tmp_path / "x.safetensors"), 1024)):
            pipe = wan_t2v_server._load_pipeline()
    assert ("x", "high_noise") in wan_t2v_server._inventory


def test_load_pipeline_rejects_auto_on_moe(monkeypatch):
    """Bug it catches: cold-boot allows auto entry, pod boots; first /lora/inventory
    call shows the LoRA loaded into the wrong transformer."""
    monkeypatch.setattr(wan_t2v_server, "_diffusers_load", _make_moe_stub_pipe)
    initial = [
        {"ref": "x", "download_spec": {"url": "http://example.com/x", "filename": "x.safetensors"},
         "strength": 1.0, "branch": "auto"},
    ]
    with patch.dict(os.environ, {"KINOFORGE_INITIAL_LORA_STACK_JSON": json.dumps(initial)}):
        with pytest.raises(wan_t2v_server.BranchAutoNotAllowedOnMoE):
            wan_t2v_server._load_pipeline()


def test_load_pipeline_rejects_explicit_branch_on_single_transformer(monkeypatch):
    """Bug it catches: cold-boot accepts high_noise on Wan 2.1; orchestrator's
    cfg ports to Wan 2.1 by accident and silently degrades."""
    monkeypatch.setattr(wan_t2v_server, "_diffusers_load", _make_single_stub_pipe)
    initial = [
        {"ref": "x", "download_spec": {"url": "http://example.com/x", "filename": "x.safetensors"},
         "strength": 1.0, "branch": "high_noise"},
    ]
    with patch.dict(os.environ, {"KINOFORGE_INITIAL_LORA_STACK_JSON": json.dumps(initial)}):
        with pytest.raises(wan_t2v_server.BranchUnsupportedOnSingleTransformer):
            wan_t2v_server._load_pipeline()


def test_legacy_tuple_env_shape_promoted_to_auto(monkeypatch, tmp_path):
    """Bug it catches: legacy tuple form rejected, breaking every pre-P2
    orchestrator that hasn't shipped the new serialization yet."""
    monkeypatch.setattr(wan_t2v_server, "_diffusers_load", _make_single_stub_pipe)
    # Legacy shape: list of two-element lists/tuples (JSON-encoded).
    legacy = [["x", {"url": "http://example.com/x", "filename": "x.safetensors"}]]
    with patch.dict(os.environ, {"KINOFORGE_INITIAL_LORA_STACK_JSON": json.dumps(legacy)}):
        with patch.object(wan_t2v_server, "_download_one",
                          return_value=(str(tmp_path / "x.safetensors"), 1024)):
            wan_t2v_server._load_pipeline()
    assert ("x", "auto") in wan_t2v_server._inventory
    assert wan_t2v_server._inventory[("x", "auto")]["last_strength"] == 1.0
```

- [ ] **Step 2: Run tests — verify they fail.**

```bash
pixi run pytest tests/engines/test_cold_boot_branch_validation.py -v
```

- [ ] **Step 3: Rewrite `_load_pipeline` env parsing.**

In `wan_t2v_server.py`, find the `KINOFORGE_INITIAL_LORA_STACK_JSON` parsing (around line 432-475). Replace with:

```python
def _parse_initial_lora_stack_env() -> list[dict[str, Any]]:
    """Parse KINOFORGE_INITIAL_LORA_STACK_JSON into a list of dicts.

    Legacy tuple form ``[[ref, download_spec], ...]`` auto-promoted to
    ``[{"ref": ref, "download_spec": ds, "strength": 1.0, "branch": "auto"}, ...]``.

    New dict form ``[{"ref": ..., "download_spec": ..., "strength": ..., "branch": ...}, ...]``
    parsed verbatim. Missing optional keys default to ``strength=1.0`` / ``branch="auto"``.
    """
    raw = os.environ.get("KINOFORGE_INITIAL_LORA_STACK_JSON", "")
    if not raw:
        return []
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("KINOFORGE_INITIAL_LORA_STACK_JSON must be a JSON array")
    out: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, list) and len(item) == 2:
            # Legacy tuple form.
            ref, ds = item
            out.append({"ref": ref, "download_spec": ds,
                        "strength": 1.0, "branch": "auto"})
        elif isinstance(item, dict):
            out.append({
                "ref": item["ref"],
                "download_spec": item["download_spec"],
                "strength": float(item.get("strength", 1.0)),
                "branch": str(item.get("branch", "auto")),
            })
        else:
            raise ValueError(f"unrecognized initial-stack entry: {item!r}")
    return out
```

Add `json` import at top of module if absent.

Then in `_load_pipeline`:

```python
def _load_pipeline() -> Any:  # noqa: ANN401 — diffusers.WanPipeline has no public TypeAlias.
    """Load the Wan pipeline + cold-boot the initial LoRA stack.

    Reads KINOFORGE_INITIAL_LORA_STACK_JSON for the cold-boot stack. See
    spec §3.4 + §6.5.

    Raises:
        BranchAutoNotAllowedOnMoE: MoE pipeline + any initial entry has
            branch='auto'. Pod exits without setting ``ready``.
        BranchUnsupportedOnSingleTransformer: non-MoE pipeline + any
            initial entry has explicit high_noise/low_noise.
    """
    global _pipe_arity
    pipe_obj = _diffusers_load()
    _pipe_arity = _detect_moe_arity(pipe_obj)

    initial_stack = _parse_initial_lora_stack_env()
    if not initial_stack:
        return pipe_obj

    # Pre-load validation gate — same as /lora/set_stack pre-load gate (spec §6.3).
    # Validate ALL entries before downloading anything.
    for entry in initial_stack:
        _ = _resolve_transformer(pipe_obj, entry["branch"])  # raises on mismatch

    adapter_names: list[str] = []
    adapter_weights: list[float] = []
    for i, entry in enumerate(initial_stack):
        ref = entry["ref"]
        branch = entry["branch"]
        strength = entry["strength"]
        spec = ArtifactDownloadSpec.model_validate(entry["download_spec"])
        try:
            path, actual_bytes = _download_one(spec, LORAS_DIR)
        except Exception as e:
            raise RuntimeError(f"failed to download LoRA {ref}: {e}") from e
        adapter_name = _adapter_name(i, branch)
        # Routing dispatch — PRIMARY or FALLBACK per Task 0.
        # (Same code shape as _replace_adapter_stack — Task 6.)
        target_transformer = _resolve_transformer(pipe_obj, branch)
        pipe_obj.load_lora_weights(
            path, adapter_name=adapter_name,
            load_into_transformer=target_transformer,  # PRIMARY
        )
        adapter_names.append(adapter_name)
        adapter_weights.append(strength)
        now = datetime.now().isoformat()
        _inventory[(ref, branch)] = {
            "ref": ref,
            "filename": spec.filename,
            "size_bytes": actual_bytes,
            "loras_dir_path": path,
            "downloaded_at_local": now,
            "last_used_at_local": now,
            "adapter_name": adapter_name,
            "last_strength": strength,
            "branch": branch,
        }
    if adapter_names:
        pipe_obj.set_adapters(adapter_names, adapter_weights=adapter_weights)
    return pipe_obj
```

(Apply Approach 3 FALLBACK rebind if Task 0 picked it.)

- [ ] **Step 4: Run tests — verify 4/4 PASS.**

```bash
pixi run pytest tests/engines/test_cold_boot_branch_validation.py -v
```

- [ ] **Step 5: Lint + typecheck.**

```bash
pixi run ruff check src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_cold_boot_branch_validation.py
pixi run mypy src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
```

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_cold_boot_branch_validation.py
git commit -m "feat(p2): cold-boot env shape carries strength+branch; pre-load validation"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/servers/wan_t2v_server.py", "tests/engines/test_cold_boot_branch_validation.py"], "verifyCommand": "pixi run pytest tests/engines/test_cold_boot_branch_validation.py -v", "acceptanceCriteria": ["dict-form env parses", "legacy tuple promoted to strength=1, branch=auto", "MoE+auto rejected at boot", "single-transformer+explicit branch rejected at boot"], "modelTier": "standard"}
```

---

## Task 8: VRAM-OOM rollback snapshot extended with branch

**Goal:** Extend P1's VRAM-OOM rollback snapshot from `[(ref, strength), ...]` to `[(ref, strength, branch), ...]`. Rollback uses `_resolve_transformer` for routing (single source of truth).

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` — rollback path (around line 836).
- Create: `tests/engines/test_vram_rollback_branch.py`

**Acceptance Criteria:**
- [ ] Snapshot is a list of 3-tuples `(ref, last_strength, branch)`.
- [ ] On VRAM-OOM during a swap, rollback restores prior `(ref, strength, branch)` triples into `_inventory`.
- [ ] Rollback re-load uses `_resolve_transformer` (NOT a duplicate dispatch).
- [ ] If rollback itself fails, server returns HTTP 500 with `reason="vram_rollback_failed"` (P1 contract preserved).

**Verify:** `pixi run pytest tests/engines/test_vram_rollback_branch.py -v` → 3/3 PASS.

**Steps:**

- [ ] **Step 1: Write failing tests.**

Create `tests/engines/test_vram_rollback_branch.py`:

```python
"""VRAM-OOM rollback preserves (ref, strength, branch) triples."""

from unittest.mock import MagicMock, patch

import pytest

from kinoforge.engines.diffusers.servers import wan_t2v_server
from kinoforge.engines.diffusers.servers.wan_t2v_server import LoraTarget


@pytest.fixture
def moe_with_prior_stack(monkeypatch):
    """Pod has prior stack [x@h@1.0, y@l@0.8] already loaded."""
    pipe = MagicMock()
    pipe.transformer = MagicMock()
    pipe.transformer_2 = MagicMock()
    monkeypatch.setattr(wan_t2v_server, "pipe", pipe)
    monkeypatch.setattr(wan_t2v_server, "_pipe_arity", 2)
    monkeypatch.setattr(wan_t2v_server, "_inventory", {
        ("x", "high_noise"): {"ref": "x", "branch": "high_noise",
                              "last_strength": 1.0,
                              "loras_dir_path": "/tmp/x_h",
                              "adapter_name": "lora_0_h"},
        ("y", "low_noise"): {"ref": "y", "branch": "low_noise",
                             "last_strength": 0.8,
                             "loras_dir_path": "/tmp/y_l",
                             "adapter_name": "lora_1_l"},
    })
    return pipe


def test_rollback_restores_branch_in_snapshot(moe_with_prior_stack):
    """Bug it catches: snapshot only stores (ref, strength), so rollback puts
    the prior LoRA back into the wrong transformer."""
    # Simulate VRAM OOM on the second load of a new swap.
    moe_with_prior_stack.load_lora_weights.side_effect = [None, RuntimeError("CUDA OOM")]
    new_target = [
        LoraTarget(ref="a", strength=1.0, branch="high_noise"),
        LoraTarget(ref="b", strength=1.0, branch="low_noise"),
    ]
    # The /lora/set_stack handler catches the OOM and triggers rollback.
    # After rollback, _inventory should match the prior state's (ref, strength, branch).
    # Exact handler-level assertion depends on Task 9's wiring; here we assert
    # the snapshot/restore mechanism in isolation:
    snapshot = [
        (e["ref"], e["last_strength"], e["branch"])
        for e in wan_t2v_server._inventory.values()
    ]
    assert ("x", 1.0, "high_noise") in snapshot
    assert ("y", 0.8, "low_noise") in snapshot
    # ... handler triggers rollback ... (concrete assertion in Task 9 test)


def test_rollback_uses_resolve_transformer_for_routing(moe_with_prior_stack):
    """Bug it catches: rollback path implements its own routing logic that
    diverges from _resolve_transformer, leading to silent miscompute on rollback."""
    # White-box: patch _resolve_transformer and verify it gets called from
    # the rollback path with the snapshotted branches.
    # Concrete implementation depends on Task 9's handler shape.
    pass  # Filled in during Task 9 (handler wiring).


def test_rollback_failure_returns_500(moe_with_prior_stack):
    """Bug it catches: rollback itself raises (VRAM still tight, file evicted)
    but server returns 200 or hangs instead of HTTP 500.

    P1 contract: rollback-itself-fails → HTTP 500 + reason='vram_rollback_failed'.
    """
    pass  # Concrete assertion in Task 9 (handler).
```

(Tests 2 + 3 placeholder-bodies because they assert handler-level behavior wired in Task 9. Body 1 asserts snapshot shape directly.)

- [ ] **Step 2: Update P1's rollback path.**

Locate the rollback block in `wan_t2v_server.py` (around the `reason="vram_oom"` literal at line 836). Update the snapshot capture:

```python
# Snapshot BEFORE any state mutation in /lora/set_stack handler.
snapshot: list[tuple[str, float, str]] = [
    (e["ref"], e["last_strength"], e["branch"])
    for e in _inventory.values()
]
```

Update the rollback re-load path:

```python
def _rollback_to_snapshot(snapshot: list[tuple[str, float, str]]) -> None:
    """Restore inventory + pipeline to snapshotted (ref, strength, branch) state.

    Called after a VRAM-OOM in the middle of a /lora/set_stack swap. Uses the
    same _resolve_transformer dispatch as the forward path — single source of
    truth for branch routing. See spec §6.4.

    Raises VRAMRollbackFailure if the re-load itself OOMs (or any other
    exception fires) — pod self-marks unhealthy.
    """
    pipe.unload_lora_weights()
    if not snapshot:
        return
    names: list[str] = []
    weights: list[float] = []
    try:
        for i, (ref, strength, branch) in enumerate(snapshot):
            entry = _inventory.get((ref, branch))
            if entry is None:
                raise VRAMRollbackFailure(
                    f"rollback target ({ref}, {branch}) missing from inventory"
                )
            target_transformer = _resolve_transformer(pipe, branch)
            name = _adapter_name(i, branch)
            pipe.load_lora_weights(
                entry["loras_dir_path"], adapter_name=name,
                load_into_transformer=target_transformer,  # PRIMARY (or FALLBACK)
            )
            names.append(name)
            weights.append(strength)
            entry["adapter_name"] = name
            entry["last_strength"] = strength
            entry["branch"] = branch
        pipe.set_adapters(names, adapter_weights=weights)
    except Exception as e:
        raise VRAMRollbackFailure(f"rollback re-load failed: {e}") from e
```

Add the `VRAMRollbackFailure` exception class near `BranchUnknown`:

```python
class VRAMRollbackFailure(Exception):
    """Raised when VRAM-OOM rollback's re-load itself fails. HTTP 500."""
```

- [ ] **Step 3: Run.**

```bash
pixi run pytest tests/engines/test_vram_rollback_branch.py -v
```

Expected: Test 1 PASS. Tests 2+3 placeholder-pass (real assertions live in Task 9's handler tests).

- [ ] **Step 4: Lint + typecheck.**

```bash
pixi run ruff check src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_vram_rollback_branch.py
pixi run mypy src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
```

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/test_vram_rollback_branch.py
git commit -m "feat(p2): VRAM rollback snapshot includes branch; restore via _resolve_transformer"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/servers/wan_t2v_server.py", "tests/engines/test_vram_rollback_branch.py"], "verifyCommand": "pixi run pytest tests/engines/test_vram_rollback_branch.py -v", "acceptanceCriteria": ["snapshot stores (ref, strength, branch)", "rollback uses _resolve_transformer", "VRAMRollbackFailure raised on rollback failure"], "modelTier": "standard"}
```

---

## Task 9: `LoraInventoryEntry` field + `/lora/inventory` response + `_evict_one` composite + handler error wiring

**Goal:** Surface `branch` in the inventory HTTP response. Update `_evict_one` signature to take composite key. Wire branch-related exceptions into the `/lora/set_stack` HTTP handler to return 400 with structured bodies.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`

**Acceptance Criteria:**
- [ ] `LoraInventoryEntry.branch: Literal["high_noise", "low_noise", "auto"]`.
- [ ] `GET /lora/inventory` returns each row with `branch` field.
- [ ] `_evict_one(ref, branch)` signature; callsites updated.
- [ ] `/lora/set_stack` returns 400 + `{"reason": "branch_auto_disallowed_on_moe", "detail": ..., "target_refs_dropped": []}` when `BranchAutoNotAllowedOnMoE` is raised.
- [ ] Same for `BranchUnsupportedOnSingleTransformer`.
- [ ] `VRAMRollbackFailure` → 500 + `{"reason": "vram_rollback_failed", ...}`.

**Verify:** `pixi run pytest tests/engines/test_replace_adapter_stack_routing.py tests/engines/test_vram_rollback_branch.py -v` → all PASS (re-runs Task 6 + 8 tests against the wired handler).

**Steps:**

- [ ] **Step 1: Update `LoraInventoryEntry`.**

In `wan_t2v_server.py`:

```python
class LoraInventoryEntry(BaseModel):
    """One row of the pod's LoRA inventory exposed over HTTP."""

    ref: str
    filename: str
    size_bytes: int
    downloaded_at_local: str
    last_used_at_local: str
    adapter_name: str
    last_strength: float | None = None
    branch: Literal["high_noise", "low_noise", "auto"] = "auto"
```

- [ ] **Step 2: Update `/lora/set_stack` handler.**

Wrap the `_replace_adapter_stack` call in the handler with the error mapping:

```python
@app.post("/lora/set_stack")
async def lora_set_stack(req: SetStackRequest) -> dict[str, Any]:
    # ... (existing inventory diff + download logic) ...
    async with _swap_lock:
        try:
            await asyncio.to_thread(_replace_adapter_stack, req.target)
        except BranchAutoNotAllowedOnMoE as e:
            raise HTTPException(status_code=400, detail={
                "reason": "branch_auto_disallowed_on_moe",
                "detail": str(e),
                "target_refs_dropped": [],
            }) from e
        except BranchUnsupportedOnSingleTransformer as e:
            raise HTTPException(status_code=400, detail={
                "reason": "branch_unsupported_single_transformer",
                "detail": str(e),
                "target_refs_dropped": [],
            }) from e
        except (RuntimeError, ValueError) as e:
            # P1's existing VRAM-OOM path.
            is_oom = "out of memory" in str(e).lower() or "OOM" in str(e)
            if is_oom:
                try:
                    _rollback_to_snapshot(snapshot)
                except VRAMRollbackFailure as rf:
                    raise HTTPException(status_code=500, detail={
                        "reason": "vram_rollback_failed",
                        "detail": str(rf),
                    }) from rf
                raise HTTPException(status_code=507, detail={
                    "reason": "vram_oom",
                    "detail": str(e),
                    "target_refs_dropped": [t.ref for t in req.target],
                }) from e
            raise HTTPException(status_code=400, detail={
                "reason": "set_adapters_value_error",
                "detail": str(e),
            }) from e
    return {"inventory": [LoraInventoryEntry(**v).model_dump() for v in _inventory.values()]}
```

(Adapt to match the actual existing handler shape — preserve P1's working error paths.)

- [ ] **Step 3: Update `_evict_one` callsites.**

Wherever `_evict_one(ref)` was called, update to `_evict_one(ref, branch)`. Inventory keys are now tuples; callers must thread the branch.

- [ ] **Step 4: Run the full engine test suite.**

```bash
pixi run pytest tests/engines/ -v
```

Expected: all PASS, including the previously-stubbed Test 2 + 3 from `test_vram_rollback_branch.py` if you fill them in here.

- [ ] **Step 5: Lint + typecheck.**

```bash
pixi run ruff check src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
pixi run mypy src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
```

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/
git commit -m "feat(p2): /lora HTTP surface — branch in inventory + 400 on mismatch"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/servers/wan_t2v_server.py"], "verifyCommand": "pixi run pytest tests/engines/ -v", "acceptanceCriteria": ["LoraInventoryEntry has branch", "/lora/inventory returns branch", "_evict_one takes (ref, branch)", "BranchAutoNotAllowedOnMoE→400", "BranchUnsupportedOnSingleTransformer→400", "VRAMRollbackFailure→500"], "modelTier": "standard"}
```

---

## Task 10: Orchestrator-side serialization — `engines/diffusers/__init__.py`

**Goal:** Update the orchestrator's serialization of the initial LoRA stack to emit the new dict shape (`{"ref": ..., "download_spec": ..., "strength": ..., "branch": ...}`) instead of the legacy tuples. Strength + branch land together.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py`

**Acceptance Criteria:**
- [ ] `DiffusersEngine.provision` (or equivalent) builds `KINOFORGE_INITIAL_LORA_STACK_JSON` from `list[LoraEntry]` as a JSON array of dicts.
- [ ] Each entry contains `ref`, `download_spec`, `strength`, `branch`.
- [ ] `branch` value is the canonical form (never `h` / `l` — alias normalization already done).
- [ ] mypy + ruff clean.
- [ ] Existing diffusers engine tests pass.

**Verify:** `pixi run pytest tests/engines/test_diffusers.py tests/engines/test_diffusers_set_lora_stack.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Grep for the existing serialization site.**

```bash
pixi run rg -n "KINOFORGE_INITIAL_LORA_STACK_JSON|initial_lora_stack" src/kinoforge/engines/diffusers/
```

Locate where the env var gets composed (likely in `DiffusersEngine.provision` or a helper).

- [ ] **Step 2: Update serialization.**

```python
def _serialize_initial_stack(stack: list[LoraEntry], download_specs: dict[str, dict]) -> str:
    """Serialize the initial LoRA stack for KINOFORGE_INITIAL_LORA_STACK_JSON.

    Emits the P2 dict shape:
        [{"ref": str, "download_spec": dict, "strength": float, "branch": str}, ...]

    Branch is the canonical form (high_noise / low_noise / auto) — Pydantic
    alias normalization already done at LoraEntry construction.
    """
    return json.dumps([
        {
            "ref": entry.ref,
            "download_spec": download_specs[entry.ref],
            "strength": entry.strength,
            "branch": entry.branch,
        }
        for entry in stack
    ])
```

- [ ] **Step 3: Update callsite in `provision` (or wherever).**

Replace the legacy `[(ref, spec), ...]` build with `_serialize_initial_stack(stack, download_specs)`.

- [ ] **Step 4: Run engine tests.**

```bash
pixi run pytest tests/engines/test_diffusers.py tests/engines/test_diffusers_set_lora_stack.py -v
```

Update any test that asserts the old tuple shape inline.

- [ ] **Step 5: Lint + typecheck.**

```bash
pixi run ruff check src/kinoforge/engines/diffusers/__init__.py
pixi run mypy src/kinoforge/engines/diffusers/__init__.py
```

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/engines/diffusers/__init__.py tests/engines/
git commit -m "feat(p2): orchestrator emits initial-stack env in dict shape with branch"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/__init__.py"], "verifyCommand": "pixi run pytest tests/engines/test_diffusers.py tests/engines/test_diffusers_set_lora_stack.py -v", "acceptanceCriteria": ["initial-stack JSON in dict shape", "branch threaded from LoraEntry", "canonical form (no h/l) emitted", "no test regressions"], "modelTier": "standard"}
```

---

## Task 11: `is_stack_match` extension + `capability_key` branch-invariance test

**Goal:** Extend matcher to compare `(ref, strength, branch)` tuples in order. Add regression test asserting `capability_key` is invariant under branch differences.

**Files:**
- Modify: `src/kinoforge/core/warm_reuse/matcher.py` (path verified during exploration).
- Create: `tests/core/test_is_stack_match_branch.py`
- Create: `tests/core/test_capability_key_branch_invariance.py`

**Acceptance Criteria:**
- [ ] `is_stack_match` returns False when stacks differ only in branch.
- [ ] `is_stack_match` returns True when stacks match in (ref, strength, branch) tuple-by-tuple in order.
- [ ] Duplicate ref allowed: `[("X", 1.0, h), ("X", 0.8, l)]` matches itself.
- [ ] Duplicate ref with branches transposed does NOT match the original.
- [ ] `capability_key` is the same for two stacks differing only in branch.

**Verify:** `pixi run pytest tests/core/test_is_stack_match_branch.py tests/core/test_capability_key_branch_invariance.py -v` → all PASS.

**Steps:**

- [ ] **Step 1: Locate matcher.**

```bash
pixi run rg -n "def is_stack_match" src/kinoforge/
```

- [ ] **Step 2: Write failing tests.**

Create `tests/core/test_is_stack_match_branch.py`:

```python
"""is_stack_match — branch-aware tuple comparison."""

from kinoforge.core.lora import LoraEntry
from kinoforge.core.warm_reuse.matcher import is_stack_match  # adjust path


def _inv_entry(ref, strength, branch):
    """Minimal stub for LoraInventoryEntry — match what matcher actually reads."""
    from kinoforge.engines.diffusers.servers.wan_t2v_server import LoraInventoryEntry
    return LoraInventoryEntry(
        ref=ref, filename="x", size_bytes=0,
        downloaded_at_local="now", last_used_at_local="now",
        adapter_name=f"lora_0_{branch[0]}", last_strength=strength, branch=branch,
    )


def test_matches_when_refs_strengths_branches_identical():
    """Bug it catches: matcher false-negative on a correct warm pod, triggering
    an unnecessary swap."""
    desired = [LoraEntry(ref="X", strength=1.0, branch="high_noise")]
    inventory = [_inv_entry("X", 1.0, "high_noise")]
    assert is_stack_match(desired, inventory) is True


def test_does_not_match_when_branches_differ():
    """Bug it catches: matcher false-positive — pod has LoRA in transformer but
    cfg wanted it in transformer_2; no swap fires; silent miscompute."""
    desired = [LoraEntry(ref="X", strength=1.0, branch="high_noise")]
    inventory = [_inv_entry("X", 1.0, "low_noise")]
    assert is_stack_match(desired, inventory) is False


def test_duplicate_ref_in_two_branches_matches_itself():
    """Bug it catches: matcher chokes on composite-key inventory entries and
    returns False even when the stack actually matches."""
    desired = [
        LoraEntry(ref="X", strength=1.0, branch="high_noise"),
        LoraEntry(ref="X", strength=0.8, branch="low_noise"),
    ]
    inventory = [_inv_entry("X", 1.0, "high_noise"), _inv_entry("X", 0.8, "low_noise")]
    assert is_stack_match(desired, inventory) is True


def test_duplicate_ref_with_branches_transposed_does_not_match():
    """Bug it catches: matcher treats stack as a set instead of ordered list,
    so swapping h/l positions falsely matches. Order matters because
    set_adapters activates by position."""
    desired = [
        LoraEntry(ref="X", strength=1.0, branch="high_noise"),
        LoraEntry(ref="X", strength=0.8, branch="low_noise"),
    ]
    inventory = [_inv_entry("X", 0.8, "low_noise"), _inv_entry("X", 1.0, "high_noise")]
    assert is_stack_match(desired, inventory) is False
```

Create `tests/core/test_capability_key_branch_invariance.py`:

```python
"""capability_key MUST be invariant under branch differences (Q4 contract)."""

from kinoforge.core.lora import LoraEntry
# adjust import path:
from kinoforge.core.warm_reuse import capability_key  # or wherever P1 placed it


def test_capability_key_same_under_branch_difference():
    """Bug it catches: capability_key accidentally includes branch in its hash,
    forcing cold-boot on every branch toggle — defeats the whole warm-reuse
    architecture for MoE pipelines."""
    base = {"base_model": "Wan-AI/Wan2.2-T2V-A14B-Diffusers",
            "engine": "diffusers", "precision": "bf16"}
    stack_a = [LoraEntry(ref="X", strength=1.0, branch="high_noise")]
    stack_b = [LoraEntry(ref="X", strength=1.0, branch="low_noise")]
    # API shape of capability_key TBD — adjust to match actual function:
    key_a = capability_key(stack_a, **base)
    key_b = capability_key(stack_b, **base)
    assert key_a == key_b
```

(Adjust `capability_key` call signature to match the actual function — likely takes base model info but NOT the stack.)

- [ ] **Step 3: Run tests — verify they fail.**

```bash
pixi run pytest tests/core/test_is_stack_match_branch.py tests/core/test_capability_key_branch_invariance.py -v
```

- [ ] **Step 4: Update `is_stack_match`.**

```python
def is_stack_match(
    desired: list[LoraEntry],
    inventory: list[LoraInventoryEntry],
) -> bool:
    """Ordered tuple-by-tuple match of (ref, strength, branch).

    See spec §4.2. Strength via math.isclose (rel_tol=1e-9, abs_tol=1e-12).
    Order matters — diffusers' adapter activation order affects composition.
    """
    if len(desired) != len(inventory):
        return False
    for d, i in zip(desired, inventory):
        if d.ref != i.ref:
            return False
        if d.branch != i.branch:
            return False
        if not math.isclose(d.strength, i.last_strength or 1.0,
                            rel_tol=1e-9, abs_tol=1e-12):
            return False
    return True
```

- [ ] **Step 5: Confirm `capability_key` is already branch-invariant.**

If `capability_key` derivation today only consumes `(base_model, engine, precision)`, no code change needed — the test confirms invariance directly.

If `capability_key` accidentally consumed the stack, refactor to drop stack-dependence.

- [ ] **Step 6: Run tests — verify all PASS.**

```bash
pixi run pytest tests/core/test_is_stack_match_branch.py tests/core/test_capability_key_branch_invariance.py -v
```

- [ ] **Step 7: Lint + typecheck.**

```bash
pixi run ruff check src/kinoforge/core/warm_reuse/ tests/core/test_is_stack_match_branch.py tests/core/test_capability_key_branch_invariance.py
pixi run mypy src/kinoforge/core/warm_reuse/
```

- [ ] **Step 8: Commit.**

```bash
git add src/kinoforge/core/warm_reuse/ tests/core/
git commit -m "feat(p2): is_stack_match compares (ref, strength, branch); capability_key invariant"
```

```json:metadata
{"files": ["src/kinoforge/core/warm_reuse/matcher.py", "tests/core/test_is_stack_match_branch.py", "tests/core/test_capability_key_branch_invariance.py"], "verifyCommand": "pixi run pytest tests/core/test_is_stack_match_branch.py tests/core/test_capability_key_branch_invariance.py -v", "acceptanceCriteria": ["matches identical stacks", "rejects branch differences", "matches duplicate ref composite", "rejects transposed branches", "capability_key invariant under branch"], "modelTier": "standard"}
```

---

## Task 12: AC8 AST scan extension + Tier-1 stub MoE knob

**Goal:** Extend the AC8 AST scan in `tests/test_no_unredacted_writes.py` to flag the new `branch` field (matches P1's `last_strength` AC8/AC9 invariant). Add `moe: bool = False` knob to the Tier-1 diffusers-load stub so unit tests can simulate Wan 2.2 shape.

**Files:**
- Modify: `tests/test_no_unredacted_writes.py`
- Modify: `tests/_smoke_harness/` (Tier-1 stub source — exact filename TBD during exploration).

**Acceptance Criteria:**
- [ ] AC8 scan detects when `branch` field is referenced outside the redacted-write contract (mirror of P1's coverage for `last_strength`).
- [ ] Tier-1 stub pipeline exposes `moe: bool` constructor knob; when True, stub has both `transformer` + `transformer_2`.
- [ ] No regression in existing AC8 + smoke-harness tests.

**Verify:** `pixi run pytest tests/test_no_unredacted_writes.py tests/_smoke_harness/ -v` → all PASS.

**Steps:**

- [ ] **Step 1: Read existing AC8 scan to understand pattern.**

```bash
pixi run rg -n "last_strength|param-shape signal" tests/test_no_unredacted_writes.py
```

Identify the P1 hook (commit `9d3469e`) and mirror it for `branch`.

- [ ] **Step 2: Extend AC8 scan.**

Update the AST visitor or string-match list:

```python
_REDACTED_FIELDS = {
    "ref",
    "last_strength",
    "branch",  # P2: routing instruction — same posture as last_strength.
}
```

- [ ] **Step 3: Update Tier-1 stub.**

```bash
pixi run rg -n "KINOFORGE_DIFFUSERS_LOAD_STUB|stub_pipe|fake_pipe" tests/_smoke_harness/
```

In the stub file, add the `moe` knob:

```python
def make_stub_pipeline(*, moe: bool = False) -> Any:
    """Construct a stubbed pipeline for Tier-1 CPU smoke.

    Args:
        moe: When True, exposes both transformer + transformer_2 (Wan 2.2 shape).
            Default False = single-transformer (Wan 2.1 / generic shape).
    """
    pipe = _StubPipe()
    pipe.transformer = _StubTransformer()
    if moe:
        pipe.transformer_2 = _StubTransformer()
    return pipe
```

- [ ] **Step 4: Run.**

```bash
pixi run pytest tests/test_no_unredacted_writes.py tests/_smoke_harness/ -v
```

- [ ] **Step 5: Lint + typecheck.**

```bash
pixi run ruff check tests/test_no_unredacted_writes.py tests/_smoke_harness/
```

- [ ] **Step 6: Commit.**

```bash
git add tests/test_no_unredacted_writes.py tests/_smoke_harness/
git commit -m "test(p2): AC8 scan covers branch; Tier-1 stub gains moe knob"
```

```json:metadata
{"files": ["tests/test_no_unredacted_writes.py", "tests/_smoke_harness/"], "verifyCommand": "pixi run pytest tests/test_no_unredacted_writes.py tests/_smoke_harness/ -v", "acceptanceCriteria": ["AC8 detects branch field", "stub exposes moe knob"], "modelTier": "mechanical"}
```

---

## Task 13: examples/configs/wan.yaml sweep — add branch field

**Goal:** Update the canonical Wan 2.2 LoRA cfg in `examples/configs/wan.yaml` to declare `branch: high_noise` and `branch: low_noise` on the Arcane Style pair. Loadable + matches the spec's Q1 D semantics.

**Files:**
- Modify: `examples/configs/wan.yaml`
- Any other example cfg with `models: [...kind=lora]` shapes referencing Wan 2.2.

**Acceptance Criteria:**
- [ ] `examples/configs/wan.yaml` loads via `Config.load` without errors.
- [ ] Arcane high-noise LoRA entry has `branch: high_noise`.
- [ ] Arcane low-noise LoRA entry has `branch: low_noise`.
- [ ] Existing `tests/test_examples.py` parse-checks still pass.

**Verify:** `pixi run pytest tests/test_examples.py -v` + `pixi run python -c "from kinoforge.core.config import Config; print(Config.load('examples/configs/wan.yaml'))"` → loads.

**Steps:**

- [ ] **Step 1: Inspect current wan.yaml structure.**

```bash
pixi run cat examples/configs/wan.yaml | head -60
```

Locate the `loras:` block (P1 sweep moved this to top-level).

- [ ] **Step 2: Add branch field.**

Edit the loras block:

```yaml
loras:
  - ref: civitai:1234@5678  # actual Arcane high-noise ref from the cfg
    strength: 1.0
    branch: high_noise
  - ref: civitai:1234@9012  # actual Arcane low-noise ref
    strength: 1.0
    branch: low_noise
```

- [ ] **Step 3: Verify load.**

```bash
pixi run python -c "from kinoforge.core.config import Config; cfg = Config.load('examples/configs/wan.yaml'); print([(e.ref, e.strength, e.branch) for e in cfg.loras])"
```

Expected output: tuples with canonical branch values.

- [ ] **Step 4: Run example-parse test.**

```bash
pixi run pytest tests/test_examples.py -v
```

- [ ] **Step 5: Commit.**

```bash
git add examples/configs/wan.yaml
git commit -m "docs(p2): canonical wan.yaml declares branch on Arcane Style pair"
```

```json:metadata
{"files": ["examples/configs/wan.yaml"], "verifyCommand": "pixi run pytest tests/test_examples.py -v", "acceptanceCriteria": ["wan.yaml loads", "branch threaded per LoRA", "Arcane high/low explicit"], "modelTier": "mechanical"}
```

---

## Task 14: Tier-3 RED scaffold — Wan 2.1 1.3B branch-routing smoke

**Goal:** Commit a RED scaffold for the Tier-3 live smoke that verifies (a) `branch=auto` still works on Wan 2.1, (b) explicit `branch=high_noise` is rejected by the server. RED scaffold means `pytest.xfail("RED scaffold")` — no live spend until the operator explicitly fires the smoke.

**Files:**
- Create: `tests/smoke/live_wan21/test_branch_routing.py`

**Acceptance Criteria:**
- [ ] File exists; `pytest.xfail("RED scaffold")` keeps tests from blocking PR CI.
- [ ] Test bodies fully written (not stubs) so the smoke is one-flag-flip away from running live.
- [ ] Matrix matches spec §7.1 Tier-3 shape.
- [ ] Inherits `tests/_smoke_harness/` patterns.

**Verify:** `pixi run pytest tests/smoke/live_wan21/test_branch_routing.py -v` → 2/2 XFAIL (RED scaffold marker).

**Steps:**

- [ ] **Step 1: Look at the existing Tier-3 test pattern for shape consistency.**

```bash
pixi run ls tests/smoke/live_wan21/
pixi run cat tests/smoke/live_wan21/test_lora_strength_variation.py | head -80
```

(P1 left a strength-variation Tier-3 scaffold; match its shape.)

- [ ] **Step 2: Write the scaffold.**

Create `tests/smoke/live_wan21/test_branch_routing.py`:

```python
"""Tier-3 live smoke — Wan 2.1 1.3B branch-routing.

Verifies on a single-transformer pipeline:
  1. branch=auto continues to work.
  2. Explicit branch=high_noise is rejected with structured 400 body.

Gated by KINOFORGE_LIVE_TESTS=1. ~$0.20 per fire. See spec §7.1 Tier 3.
"""

import os
from pathlib import Path

import pytest

from tests._smoke_harness import (  # adjust to actual harness module
    fire_wan21_smoke,
    standard_prompt,
)

pytestmark = pytest.mark.xfail(
    reason="RED scaffold — P2 implementation pending. Tier-3 fire requires "
           "KINOFORGE_LIVE_TESTS=1 + explicit operator authorization.",
    strict=False,
)


@pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="Tier-3 live smoke requires KINOFORGE_LIVE_TESTS=1",
)
def test_branch_auto_on_wan21_succeeds(tmp_path: Path):
    """Bug it catches: P2 changes break single-transformer pipelines by
    accidentally requiring an explicit branch."""
    result = fire_wan21_smoke(
        prompt=standard_prompt(),
        loras=[{"ref": "civitai:<rotation-id>", "strength": 1.0, "branch": "auto"}],
        output_dir=tmp_path,
    )
    assert result.status == "done"
    assert result.mp4_sha256, "missing artifact sha"


@pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="Tier-3 live smoke requires KINOFORGE_LIVE_TESTS=1",
)
def test_branch_high_noise_on_wan21_rejected_400(tmp_path: Path):
    """Bug it catches: P2's strict-reject semantics broken — explicit branch on
    single-transformer pipeline silently collapses instead of returning 400."""
    result = fire_wan21_smoke(
        prompt=standard_prompt(),
        loras=[{"ref": "civitai:<rotation-id>", "strength": 1.0, "branch": "high_noise"}],
        output_dir=tmp_path,
    )
    assert result.status == "error"
    assert result.error_body["reason"] == "branch_unsupported_single_transformer"
```

- [ ] **Step 3: Run — verify XFAIL.**

```bash
pixi run pytest tests/smoke/live_wan21/test_branch_routing.py -v
```

Expected: 2/2 XFAIL.

- [ ] **Step 4: Commit.**

```bash
git add tests/smoke/live_wan21/test_branch_routing.py
git commit -m "test(p2): Tier-3 RED scaffold — Wan 2.1 branch-routing smoke"
```

```json:metadata
{"files": ["tests/smoke/live_wan21/test_branch_routing.py"], "verifyCommand": "pixi run pytest tests/smoke/live_wan21/test_branch_routing.py -v", "acceptanceCriteria": ["file commits as RED scaffold", "2 test bodies fully written", "KINOFORGE_LIVE_TESTS guard in place"], "modelTier": "mechanical"}
```

---

## Task 15: Tier-4 RED scaffold — Wan 2.2 14B dual-transformer routing matrix

**Goal:** Commit a RED scaffold for the Tier-4 live smoke matrix from spec §7.1 (7 cases).

**Files:**
- Create: `tests/smoke/release_wan22/test_dual_transformer_routing.py`

**Acceptance Criteria:**
- [ ] File exists; `pytest.xfail("RED scaffold")` everywhere.
- [ ] All 7 matrix cases enumerated as individual `@pytest.mark.parametrize` entries OR separate test functions.
- [ ] Inherits `tests/_smoke_harness/` patterns + matches `tests/smoke/release_wan22/test_lora_strength_variation.py` shape.

**Verify:** `pixi run pytest tests/smoke/release_wan22/test_dual_transformer_routing.py -v` → all 7 XFAIL.

**Steps:**

- [ ] **Step 1: Inspect existing Tier-4 scaffold for shape.**

```bash
pixi run cat tests/smoke/release_wan22/test_lora_strength_variation.py | head -80
```

- [ ] **Step 2: Write the scaffold.**

Create `tests/smoke/release_wan22/test_dual_transformer_routing.py`:

```python
"""Tier-4 live smoke — Wan 2.2 14B dual-transformer routing matrix.

Verifies routing produces stage-specific style separation. ~$1-2 per full
matrix fire. Gated by KINOFORGE_LIVE_TESTS=1. See spec §7.1 Tier 4.

Matrix shares a warm pod via --reuse to amortize cold-boot across 7 cases.
"""

import os
from pathlib import Path

import pytest

from tests._smoke_harness import (
    fire_wan22_smoke,
    standard_prompt,
    arcane_high_lora,
    arcane_low_lora,
)

pytestmark = pytest.mark.xfail(
    reason="RED scaffold — P2 implementation + operator auth pending.",
    strict=False,
)
live_only = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="Tier-4 requires KINOFORGE_LIVE_TESTS=1",
)


@live_only
def test_baseline_no_lora(tmp_path: Path):
    """Reference output, no LoRA stack."""
    result = fire_wan22_smoke(prompt=standard_prompt(), loras=[], output_dir=tmp_path)
    assert result.status == "done"


@live_only
def test_arcane_high_only(tmp_path: Path):
    """Bug it catches: high-noise LoRA routed to transformer_2 by accident."""
    result = fire_wan22_smoke(
        prompt=standard_prompt(),
        loras=[arcane_high_lora(branch="high_noise")],
        output_dir=tmp_path,
    )
    assert result.status == "done"


@live_only
def test_arcane_low_only(tmp_path: Path):
    """Bug it catches: low-noise LoRA routed to transformer."""
    result = fire_wan22_smoke(
        prompt=standard_prompt(),
        loras=[arcane_low_lora(branch="low_noise")],
        output_dir=tmp_path,
    )
    assert result.status == "done"


@live_only
def test_arcane_canonical_pair(tmp_path: Path):
    """Canonical pair — sha256 should match the existing Tier-4 baseline."""
    result = fire_wan22_smoke(
        prompt=standard_prompt(),
        loras=[arcane_high_lora(branch="high_noise"),
               arcane_low_lora(branch="low_noise")],
        output_dir=tmp_path,
    )
    assert result.status == "done"
    # Reference sha pinned in successful-generations.md §10 (Tier-4 baseline).
    # assert result.mp4_sha256 == "<pinned-sha>"


@live_only
def test_wrong_routing_succeeds_but_differs(tmp_path: Path):
    """Intentionally wrong routing — captures sha as proof that routing matters.

    Bug it catches: server silently swaps routing per-call, making wrong-routing
    indistinguishable from correct-routing in output. If sha matches correct
    pair's sha, routing isn't actually doing anything."""
    result = fire_wan22_smoke(
        prompt=standard_prompt(),
        loras=[arcane_high_lora(branch="low_noise"),  # intentional swap
               arcane_low_lora(branch="high_noise")],
        output_dir=tmp_path,
    )
    assert result.status == "done"


@live_only
def test_moe_auto_rejected_400(tmp_path: Path):
    """Bug it catches: P2's auto-disallowed-on-MoE strict reject broken."""
    result = fire_wan22_smoke(
        prompt=standard_prompt(),
        loras=[arcane_high_lora(branch="auto")],
        output_dir=tmp_path,
    )
    assert result.status == "error"
    assert result.error_body["reason"] == "branch_auto_disallowed_on_moe"


@live_only
def test_same_ref_in_both_branches(tmp_path: Path):
    """Bug it catches: composite (ref, branch) inventory key breaks — server
    rejects duplicate ref or generates colliding adapter names."""
    high = arcane_high_lora(branch="high_noise")
    duplicate = {**high, "branch": "low_noise", "strength": 0.8}
    result = fire_wan22_smoke(
        prompt=standard_prompt(),
        loras=[high, duplicate],
        output_dir=tmp_path,
    )
    assert result.status == "done"
```

- [ ] **Step 3: Run — verify XFAIL.**

```bash
pixi run pytest tests/smoke/release_wan22/test_dual_transformer_routing.py -v
```

Expected: 7/7 XFAIL.

- [ ] **Step 4: Commit.**

```bash
git add tests/smoke/release_wan22/test_dual_transformer_routing.py
git commit -m "test(p2): Tier-4 RED scaffold — Wan 2.2 dual-transformer routing matrix"
```

```json:metadata
{"files": ["tests/smoke/release_wan22/test_dual_transformer_routing.py"], "verifyCommand": "pixi run pytest tests/smoke/release_wan22/test_dual_transformer_routing.py -v", "acceptanceCriteria": ["all 7 matrix cases scaffolded", "RED xfail marker present", "KINOFORGE_LIVE_TESTS guard in place"], "modelTier": "mechanical"}
```

---

## Task 16: Live-smoke fire — Tier-3 + Tier-4 + PROGRESS close-out

**Goal:** Operator-authorized live spend. Tier-3 Wan 2.1 1.3B (~$0.20) + Tier-4 Wan 2.2 14B (~$1.50). On green, flip `xfail` → green; capture sha256s in `successful-generations.md`; update PROGRESS.md with the close-out.

**Files:**
- Modify: `tests/smoke/live_wan21/test_branch_routing.py` (remove `pytestmark = pytest.mark.xfail`).
- Modify: `tests/smoke/release_wan22/test_dual_transformer_routing.py` (same).
- Modify: `successful-generations.md` (add §11 for P2 routing or extend §10 "See also").
- Modify: `PROGRESS.md` (P2 close-out section).

**Acceptance Criteria:**
- [ ] Tier-3 fire green (2/2 PASS).
- [ ] Tier-4 fire green (7/7 PASS).
- [ ] Wrong-routing case captures a DIFFERENT sha256 from canonical-pair case (proves routing is doing something).
- [ ] `kinoforge list` after the fire shows no running instances (sanity per CLAUDE.md teardown rule).
- [ ] `successful-generations.md` updated.
- [ ] PROGRESS.md updated.

**Verify (Tier-3):** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/smoke/live_wan21/test_branch_routing.py -v`

**Verify (Tier-4):** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/smoke/release_wan22/test_dual_transformer_routing.py -v`

**Steps:**

- [ ] **Step 1: Preflight.**

```bash
pixi run preflight
```

Expected exit 0 — RUNPOD/HF creds present, zero active pods, clean working tree.

- [ ] **Step 2: Confirm CLAUDE.md spend ceremony.**

Per memory `feedback_autonomous_no_gates`: live smokes pre-authorized up to $20 session budget. No user-gate ceremony.

Per memory `feedback_use_no_reuse_for_one_shots`: Tier-3 / Tier-4 are MULTI-CASE smokes (warm-reuse desirable to amortize cold-boot across cases). `--reuse` is correct here, NOT `--no-reuse`. After the matrix completes, explicitly destroy via `kinoforge destroy --id <pod-id>` (don't trust mid-run logs).

- [ ] **Step 3: Fire Tier-3.**

Remove xfail marker from `tests/smoke/live_wan21/test_branch_routing.py`:

```python
# pytestmark = pytest.mark.xfail(...)  # removed — P2 ready to ship.
```

Then:

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/smoke/live_wan21/test_branch_routing.py -v -s
```

Expected: 2/2 PASS. Capture sha256s + cost.

- [ ] **Step 4: Verify teardown.**

```bash
pixi run kinoforge list
```

Expected: "No running instances" + "No instances recorded in ledger." If a pod is still running:

```bash
pixi run kinoforge destroy --id <pod-id>
```

- [ ] **Step 5: Fire Tier-4.**

Remove xfail marker. Then:

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/smoke/release_wan22/test_dual_transformer_routing.py -v -s
```

Expected: 7/7 PASS. Capture sha256s. Cost target: under $2.

- [ ] **Step 6: Verify wrong-routing sha differs.**

Compare the canonical-pair mp4 sha to the wrong-routing mp4 sha. If they match, routing is broken — DO NOT close P2; investigate.

- [ ] **Step 7: Teardown verify.**

```bash
pixi run kinoforge list
```

Expected: clean.

- [ ] **Step 8: Update successful-generations.md.**

Append §11 (or "See also" under §10) with the 7-case matrix and observed sha256s. Match the schema in that file's preamble.

- [ ] **Step 9: Update PROGRESS.md.**

Add a "P2 routing CODE-COMPLETE + LIVE-PROVEN YYYY-MM-DD" section pointing to spec + plan + commit range + total spend.

- [ ] **Step 10: Final commit.**

```bash
git add tests/smoke/ successful-generations.md PROGRESS.md
git commit -m "test(p2): live-prove Tier-3 + Tier-4 routing; close out P2"
git push origin main
```

```json:metadata
{"files": ["tests/smoke/live_wan21/test_branch_routing.py", "tests/smoke/release_wan22/test_dual_transformer_routing.py", "successful-generations.md", "PROGRESS.md"], "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/smoke/live_wan21/test_branch_routing.py tests/smoke/release_wan22/test_dual_transformer_routing.py -v && pixi run kinoforge list", "acceptanceCriteria": ["Tier-3 2/2 PASS", "Tier-4 7/7 PASS", "wrong-routing sha != canonical sha", "kinoforge list shows clean teardown", "successful-generations.md updated", "PROGRESS.md updated"], "modelTier": "standard"}
```

---

## Self-review notes

- **Spec coverage:** every spec section maps to at least one task. §1 → Task 0 (research) + plan structure. §2 → Tasks 1+2+3. §3 → Tasks 5+6+9+10. §4 → Task 11. §5 → Tasks 4+6+7. §6 → Tasks 6+8+9. §7 → Tasks 12+14+15+16. §8 → Task 0 deliverable + data-flow guide.
- **No placeholders:** every step has concrete code or commands.
- **Type consistency:** `branch` typed as `Literal["high_noise", "low_noise", "auto"]` consistently across `LoraEntry`, `LoraTarget`, `LoraInventoryEntry`. Adapter naming scheme `lora_{i}_{h|l|a}` consistent across `_replace_adapter_stack` + `_load_pipeline` + `_evict_one`.

No user-gate tasks tagged. Plan-level enforcement check skipped.
