# Task 0 research — diffusers Wan API for per-transformer LoRA routing

**Date:** 2026-06-22
**Diffusers version surveyed:** v0.36.0 (≥0.32 floor per pod cfg
`examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml` +
`examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml`).
**Source surveyed:**

- `src/diffusers/loaders/lora_pipeline.py` (`WanLoraLoaderMixin`, lines
  3929–4106 at v0.36.0)
- `src/diffusers/loaders/lora_base.py` (`LoraBaseMixin` set/delete/unload),
  lines 481–874 at v0.36.0)

Probe scripts: not run — static source reading sufficed; diffusers + peft
do not live in any local pixi env (pod-side only). Functional confirmation
is deferred to the Tier-4 live smoke during Task 6 (the Tier-4 matrix
already exercises every routing path).

---

## Q1 — per-transformer kwarg on `load_lora_weights`

**Finding:** PRESENT.

**Kwarg name:** `load_into_transformer_2: bool` (default `False`).

**Source citation:** `diffusers/loaders/lora_pipeline.py:4078` (v0.36.0):

```python
load_into_transformer_2 = kwargs.pop("load_into_transformer_2", False)
if load_into_transformer_2:
    if not hasattr(self, "transformer_2"):
        raise AttributeError(
            f"'{type(self).__name__}' object has no attribute transformer_2"
            "Note that Wan2.1 models do not have a transformer_2 component."
            ...
        )
    self.load_lora_into_transformer(
        state_dict,
        transformer=self.transformer_2,
        adapter_name=adapter_name,
        ...
    )
else:
    self.load_lora_into_transformer(
        state_dict,
        transformer=getattr(self, self.transformer_name)
            if not hasattr(self, "transformer")
            else self.transformer,
        ...
    )
```

Class declares `_lora_loadable_modules = ["transformer", "transformer_2"]`
at `lora_pipeline.py:3934`. The kwarg is a boolean toggle, NOT a `transformer=`
object reference. This is a minor deviation from the plan's tentative
`load_into_transformer=<target>` shape — `_resolve_transformer` returns
`"transformer"` vs `"transformer_2"` as an attribute name, and the call
site translates to `load_into_transformer_2=(branch == "low_noise")`.

Functional probe (not run; documented for the Tier-4 verifier):

```python
import torch
from diffusers import WanPipeline

pipe = WanPipeline.from_pretrained("Wan-AI/Wan2.2-T2V-A14B-Diffusers", torch_dtype=torch.bfloat16)

# high-noise LoRA → self.transformer
pipe.load_lora_weights("/path/arcane-high.safetensors", adapter_name="probe_h")
# low-noise LoRA → self.transformer_2
pipe.load_lora_weights("/path/arcane-low.safetensors", adapter_name="probe_l",
                       load_into_transformer_2=True)
assert "probe_h" in pipe.transformer.peft_config
assert "probe_l" in pipe.transformer_2.peft_config
```

**Decision:** Approach 1 PRIMARY — `load_into_transformer_2=True/False`
boolean dispatch in `_replace_adapter_stack` + cold-boot loop. Approach 3
(transformer attribute rebind) is dropped from the implementation path;
kept in spec only as historical reference. `_resolve_transformer` keeps
its current contract (returns the target transformer attribute) — the
call site translates that into the `load_into_transformer_2` boolean.

---

## Q2 — `set_adapters` activation across split transformers

**Finding:** PARTIAL — global `LoraBaseMixin.set_adapters` iterates over
`_lora_loadable_modules` but passes ALL `adapter_names` to each
transformer. peft layers raise on unknown adapter names; safer to dispatch
per-transformer.

**Source citation:** `diffusers/loaders/lora_base.py:675–774` (v0.36.0):

```python
def set_adapters(self, adapter_names, adapter_weights=None):
    ...
    list_adapters = self.get_list_adapters()
    # eg {"transformer": ["a_high"], "transformer_2": ["a_low"]}
    all_adapters = {adapter for adapters in list_adapters.values() for adapter in adapters}
    missing_adapters = set(adapter_names) - all_adapters
    if len(missing_adapters) > 0:
        raise ValueError(
            f"Adapter name(s) {missing_adapters} not in the list of present adapters: {all_adapters}."
        )
    ...
    for component in self._lora_loadable_modules:
        model = getattr(self, component, None)
        # To guard for cases like Wan. In Wan2.1 and WanVace, we have a single denoiser.
        # Whereas in Wan 2.2, we have two denoisers.
        if model is None:
            continue
        ...
        model.set_adapters(adapter_names, _component_adapter_weights[component])
```

The pipe-level missing-adapter check uses the UNION of every transformer's
known adapters (line 733 `all_adapters`), so `{"a_high","a_low"}` clears
the pipe-level check. But line 774 passes BOTH names to each transformer's
peft `set_adapters` call. peft's per-model `set_adapters` raises when asked
to activate a name that isn't in its `peft_config`. The "guard for cases
like Wan" comment in source (line 750) acknowledges the dual-denoiser case
but does NOT auto-filter adapter names by transformer.

**Decision:** per-transformer activation loop in `_replace_adapter_stack`
(spec §5.4). After loading each adapter into its target transformer,
activate them per-transformer by calling `model.set_adapters(...)` with
ONLY the names that were actually loaded into that model. Avoids the
KeyError class observed in upstream issue #12535. Pipe-level
`pipe.set_adapters` is NOT called.

Concrete activation strategy in code:

```python
# After loading every adapter into its target transformer:
per_transformer_names: dict[str, list[str]] = {"transformer": [], "transformer_2": []}
per_transformer_weights: dict[str, list[float]] = {"transformer": [], "transformer_2": []}
for (t, target_transformer_attr) in resolved:
    per_transformer_names[target_transformer_attr].append(adapter_name_for(t))
    per_transformer_weights[target_transformer_attr].append(t.strength)
for attr in ("transformer", "transformer_2"):
    model = getattr(pipe, attr, None)
    if model is not None and per_transformer_names[attr]:
        model.set_adapters(per_transformer_names[attr], per_transformer_weights[attr])
```

For single-transformer pipelines (Wan 2.1) the loop degenerates to a
single `pipe.transformer.set_adapters(...)` call, which matches P1's
shipped behavior.

---

## Q3 — `delete_adapters` per-transformer dispatch necessity

**Finding:** auto-dispatched globally. No per-transformer code needed at
our layer.

**Source citation:** `diffusers/loaders/lora_base.py:838–874` (v0.36.0):

```python
def delete_adapters(self, adapter_names):
    ...
    for component in self._lora_loadable_modules:
        model = getattr(self, component, None)
        if model is not None:
            if issubclass(model.__class__, ModelMixin):
                model.delete_adapters(adapter_names)
            ...
```

The base `delete_adapters` iterates `_lora_loadable_modules` and calls
`model.delete_adapters(adapter_names)` on each. peft's per-model
`delete_adapters` tolerates names that aren't in its `peft_config`
(no-ops with a debug message). Same logic for `unload_lora_weights`
(`lora_base.py:513–534`): iterates `_lora_loadable_modules` and calls
`model.unload_lora()` on each.

**Decision:** `_evict_one(ref, branch)` stays simple — single
`pipe.delete_adapters([entry["adapter_name"]])` call covers both
transformer cases. Composite-key change in Task 5 affects WHICH inventory
entry we look up; the eviction call itself is unchanged from P1.

---

## Q4 — peft version floor

**Installed:** N/A locally (pod-side only).

**Cfg-declared pin:** `peft>=0.13` (canonical pod cfg files
`wan22-14b-lora-flexible-warm-reuse-release.yaml` line 27 and
`wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml` line 27, both with the
comment "required by diffusers' LoRA loaders").

**Diffusers-side floor:** `lora_pipeline.py:129` and `:195` and `:406`
each enforce `is_peft_version("<", "0.13.0")` for the `low_cpu_mem_usage`
path. Diffusers itself documents 0.13.0 as the floor.

**Decision:** **peft >= 0.13.0** confirmed; matches existing pod cfg pin.
No bump needed for P2. The Tier-1 smoke stub already mocks peft at this
floor; no harness change.

---

## Summary table

| Q | Finding | Path chosen for P2 |
|---|---------|--------------------|
| Q1 | `load_into_transformer_2: bool` kwarg present | **Approach 1 PRIMARY** — boolean dispatch in load + activation paths |
| Q2 | global `set_adapters` passes all names to each transformer; risks KeyError on split adapters | **per-transformer activation loop** (spec §5.4); pipe-level `set_adapters` NOT used |
| Q3 | global `delete_adapters` auto-dispatches via `_lora_loadable_modules` | **simple `pipe.delete_adapters([name])`** in `_evict_one`; no per-transformer dispatch |
| Q4 | peft floor 0.13.0 enforced by diffusers + already pinned in pod cfgs | **no change** |

## Plan-doc update checklist (post-research; applied in this same task)

- [ ] Task 6 PRIMARY block: change kwarg from `load_into_transformer=<attr>`
  to `load_into_transformer_2=(branch == "low_noise")` boolean.
- [ ] Task 6 activation block: replace single end-of-load
  `pipe.set_adapters(...)` with per-transformer activation loop (Q2
  finding above).
- [ ] Task 6 FALLBACK (Approach 3 rebind): DROP — Q1 confirmed the kwarg
  exists.
- [ ] Task 7 cold-boot loop: same `load_into_transformer_2` boolean.
- [ ] Task 8 VRAM-OOM rollback: same `load_into_transformer_2` boolean.
- [ ] Task 9 `_evict_one`: no change — Q3 finding above.

These edits are applied in `docs/superpowers/plans/2026-06-22-p2-wan22-dual-transformer-routing.md`
in the same task commit (per plan Task 0 Step 8).
