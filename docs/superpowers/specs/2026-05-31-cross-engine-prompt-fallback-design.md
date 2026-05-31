# Layer J — Cross-engine prompt fallback

Shared prompt-routing helper closing the latent defect that hosted, diffusers,
and comfyui backends ignore the orchestrator-supplied prompt. Surfaced by the
Layer-I live smoke against fal.ai (PROGRESS Phase 19 follow-ups).

## 1. Problem

`GenerateClipStage` calls `engine.backend.submit(job)` with a `GenerationJob`
whose user prompt lives on `job.segments[0].prompt`, not in `job.spec`. Three
backends build their request body from `job.spec` only:

- `HostedAPIBackend.submit` — `body = dict(job.spec)`; no prompt fallback.
- `DiffusersBackend.submit` — same shape.
- `ComfyUIBackend.submit` — `graph = job.spec["graph"]`; prompt baked into a
  graph node, never routed from the segment.

Result: an orchestrator-driven `kinoforge generate "a cat"` would reach the
provider with no prompt text — the exact defect Fal hit and patched inline
in Layer-I Task 13. The three remaining engines must mirror that behavior,
but as a shared helper rather than four copies of the same fallback.

### 1.1 Necessary but not sufficient

This layer is a **prerequisite** for orchestrator-driven hosted/diffusers/
comfyui runs, not a complete unblock. `Orchestrator.generate` at
`src/kinoforge/core/orchestrator.py:605` hardcodes `base_spec={}`, so even
after this fix, those engines' `validate_spec` will still reject jobs for
missing required spec keys (`model`+`params`, `pipeline`+`scheduler`,
`graph`+`node_overrides`). Routing `base_spec` from YAML cfg is a separate,
broader layer (Layer K candidate); explicitly out of scope here. See §5.

The Fal smoke worked end-to-end because `FalEngine.validate_spec` only
requires a prompt — it has no `model`/`pipeline`/`graph` requirements.

## 2. Design overview

One pure helper, four engines opt in, validate-on-misconfiguration is opt-in.

### 2.1 `resolve_prompt(job) -> str | None`

New module `src/kinoforge/core/prompt_routing.py`:

```python
from kinoforge.core.interfaces import GenerationJob


def resolve_prompt(job: GenerationJob) -> str | None:
    """Return the prompt to use, or None if none available.

    Precedence: ``job.spec["prompt"]`` (explicit, config-supplied) wins over
    ``job.segments[0].prompt`` (orchestrator path). Empty strings and
    non-``str`` values do not count. Returns ``None`` when neither is
    available.

    Args:
        job: The :class:`~kinoforge.core.interfaces.GenerationJob` whose
            prompt to resolve.

    Returns:
        The prompt string, or ``None`` if neither location holds a non-empty
        ``str``.
    """
    spec_prompt = job.spec.get("prompt")
    if isinstance(spec_prompt, str) and spec_prompt:
        return spec_prompt
    if job.segments:
        seg_prompt = getattr(job.segments[0], "prompt", "")
        if isinstance(seg_prompt, str) and seg_prompt:
            return seg_prompt
    return None
```

Pure function, no I/O, no class state. Mirrors Fal's existing precedence
(spec-prompt wins). Engines call it once per `submit`; opt-in
`validate_spec` calls it to confirm availability.

### 2.2 Config additions (`src/kinoforge/core/config.py`)

```python
class HostedEngineConfig(BaseModel):
    # existing fields preserved
    prompt_body_key: str | None = "prompt"  # NEW

class DiffusersEngineConfig(BaseModel):
    # existing fields preserved
    prompt_body_key: str | None = "prompt"  # NEW
```

ComfyUI gets **no new cfg field**. `prompt_node_ids` lives in `job.spec`
alongside `graph`, `node_overrides`, and `asset_node_ids` (Q6=A — symmetry
with the existing ComfyUI spec surface beats cross-engine "all routing on
cfg" framing). See §2.3 ComfyUI block.

- Hosted/diffusers default `"prompt"` mirrors Fal's hardcoded behavior.
- Opt-out: `prompt_body_key: null` (or empty string treated as falsy).
- Hosted/diffusers `EngineConfig` already registered in cfg model — wiring
  follows the Layer-I cfg-strip lockdown pattern (commit `484e368`):
  pydantic round-trip test + E2E YAML→engine.backend wire test.

### 2.3 Backend wire-up

**`HostedAPIBackend.submit`** — insert after `body = dict(job.spec)`, before
asset-injection loop:

```python
if self._prompt_body_key:
    prompt = resolve_prompt(job)
    if prompt is not None:
        body.setdefault(self._prompt_body_key, prompt)
```

`setdefault` is defensive: when the configured key differs from `"prompt"`
but spec carries an explicit value at that key, the explicit value wins. (For
the common case where `prompt_body_key == "prompt"`, `resolve_prompt`
already enforces spec-precedence, so `setdefault` is redundant but cheap.)

**`DiffusersBackend.submit`** — identical pattern; `_prompt_body_key`
constructor-injected from cfg.

**`ComfyUIBackend.submit`** — new branch after `asset_node_ids` loop, before
the deep-merge into `graph`. `prompt_node_ids` comes from `job.spec`,
matching `asset_node_ids`:

```python
prompt_node_ids: dict[str, str] = job.spec.get("prompt_node_ids", {})
if prompt_node_ids:
    prompt = resolve_prompt(job)
    if prompt is not None:
        for _role, node_id in prompt_node_ids.items():
            node_patch = overrides.setdefault(str(node_id), {})
            inputs = node_patch.setdefault("inputs", {})
            inputs.setdefault("text", prompt)  # setdefault: explicit wins
```

Role key currently unused (mirror `asset_node_ids` shape for future per-role
routing). Input key hardcoded to `"text"` (matches CLIPTextEncode and most
prompt-encoder nodes); escape hatch via `node_overrides` directly.
`inputs.setdefault("text", ...)` preserves an explicit
`node_overrides[node_id]["inputs"]["text"]` from spec.

**`FalBackend.submit`** retrofit — replace the existing inline 4-line
fallback (currently at `src/kinoforge/engines/fal/__init__.py:236-243`) with:

```python
body = dict(job.spec)
prompt = resolve_prompt(job)
if prompt is not None:
    body.setdefault("prompt", prompt)
```

Behavior identical (spec-precedence preserved). Existing Fal tests
(`test_submit_falls_back_to_segment_prompt`,
`test_submit_spec_prompt_wins_over_segment_prompt`) pass unchanged.

### 2.4 `validate_spec` opt-in checks

Validate-when-routing-configured posture (avoids breaking legacy configs that
drive prompt entirely via `params` dict / baked-in graph).

**`HostedAPIEngine.validate_spec`** — append after existing checks:

```python
if self._prompt_body_key and resolve_prompt(job) is None:
    raise ValidationError(
        "hosted prompt_body_key is configured but no prompt found in "
        "job.spec or segments[0] — set spec.prompt, set segments[0].prompt, "
        "or disable routing with engine.hosted.prompt_body_key: null"
    )
```

Engine grows `_prompt_body_key` member added at `__init__` from cfg
(mirrors backend wire).

**`DiffusersEngine.validate_spec`** — identical pattern with
`engine.diffusers.prompt_body_key` in the error message.

**`ComfyUIEngine.validate_spec`** — append; reads from `job.spec`:

```python
prompt_node_ids: dict[str, str] = job.spec.get("prompt_node_ids", {})
if prompt_node_ids and resolve_prompt(job) is None:
    raise ValidationError(
        "comfyui job.spec.prompt_node_ids is configured but no prompt "
        "found in job.spec or segments[0] — set spec.prompt, set "
        "segments[0].prompt, or clear spec.prompt_node_ids"
    )
```

No engine-state addition — `validate_spec` stays stateless w.r.t. cfg for
ComfyUI, matching its existing `asset_node_ids` handling.

**`FalEngine.validate_spec`** unchanged. Fal's contract is engine-specific
stricter ("always require a prompt"); existing test
`test_validate_spec_accepts_prompt_on_segment` covers the segment path. Body
not refactored to use `resolve_prompt` — kept as-is to minimize churn on
already-shipped, already-tested code.

## 3. Test surface

### 3.1 Helper unit tests (`tests/core/test_prompt_routing.py`, ~8 tests)

1. spec-prompt returned when set + non-empty
2. segment-prompt returned when spec lacks key
3. spec-prompt wins when both set (precedence)
4. `None` when neither present
5. `None` when spec-prompt is `""` and no segments
6. `None` when spec-prompt is non-`str` (e.g. `int`) — guards type assumption
7. `None` when `job.segments` is empty
8. segment-prompt returned when spec-prompt is `""` (empty does not shadow)

### 3.2 Per-engine tests (5–6 each)

`tests/engines/test_hosted.py`, `test_diffusers.py`, `test_comfyui.py`:

- `submit_falls_back_to_segment_prompt` — body / node carries segment prompt
  when spec lacks
- `submit_spec_prompt_wins_over_segment_prompt` — explicit spec prompt
  preserved
- `submit_skips_when_routing_disabled` — `prompt_body_key=None` (hosted/
  diffusers) or `spec.prompt_node_ids` absent / empty (comfyui) → no
  injection, body / overrides unchanged
- `validate_spec_raises_when_routing_configured_and_no_prompt` — opt-in
  validation fires
- `validate_spec_passes_when_routing_unconfigured_and_no_prompt` — legacy
  path untouched
- (ComfyUI extra) `submit_does_not_overwrite_explicit_node_override_text` —
  `setdefault` semantics on `inputs.text`

### 3.3 Config tests (hosted + diffusers only)

- YAML round-trip for `prompt_body_key` on hosted (1 test)
- YAML round-trip for `prompt_body_key` on diffusers (1 test)
- E2E YAML→engine.backend wire for each new field (2 tests) — closes the
  Layer-I cfg-strip defect class (commit `484e368`)

No ComfyUI cfg tests — `prompt_node_ids` lives in spec, exercised by §3.2
per-engine tests.

### 3.4 Fal retrofit

Existing Fal tests verify behavior, not implementation. No new Fal tests; the
3 existing prompt-fallback tests must keep passing after the retrofit.

**Total new tests:** ~8 (helper) + 16 (engines: 5+5+6) + 4 (config) = **~28**.

## 4. Task breakdown (writing-plans will refine)

1. Helper module + 8 unit tests (TDD red-first).
2. Config: `prompt_body_key` on `HostedEngineConfig` + `DiffusersEngineConfig`
   + 2 round-trip tests. **No ComfyUI cfg change.**
3. `HostedAPIBackend` + `HostedAPIEngine` wire — 5 tests.
4. `DiffusersBackend` + `DiffusersEngine` wire — 5 tests.
5. `ComfyUIBackend` + `ComfyUIEngine` wire — 6 tests. Reads
   `prompt_node_ids` from `job.spec`.
6. `FalBackend.submit` retrofit; confirm existing Fal tests stay green.
7. E2E YAML→engine.backend wire tests for the 2 new cfg fields.
8. Update `examples/configs/{hosted,diffusers}.yaml` (commented
   `prompt_body_key` default); add or update `examples/configs/comfyui.yaml`
   showing `prompt_node_ids` in spec; PROGRESS Phase 20 entry; README
   mention.
9. Two-stage review (spec compliance → code quality) → `--no-ff` merge.

Dependencies: 1 blocks 3/4/5/6; 2 blocks 3/4; 7 follows 3/4; 8 follows
all implementation tasks.

## 5. Non-goals

- **Base_spec routing from cfg.** `Orchestrator.generate` hardcodes
  `base_spec={}` (`src/kinoforge/core/orchestrator.py:605`). Routing
  YAML-supplied spec into the orchestrator is a separate, broader layer
  (Layer K candidate). Hosted/Diffusers/ComfyUI orchestrator-driven runs
  remain blocked on missing required spec keys (`model`/`params`,
  `pipeline`/`scheduler`, `graph`/`node_overrides`) until that work lands;
  this layer only fixes the prompt-on-segment defect.
- No change to `Segment` / `GenerationJob` dataclasses.
- No change to `strategy.decide`, the orchestrator, or splitter wiring.
- No new engine adapter.
- No retroactive change to Fal's strict "always require prompt"
  `validate_spec` contract (engine-specific).
- No support for ComfyUI input keys other than `"text"`; non-standard
  encoders use `node_overrides` directly (existing escape hatch).
- No multi-prompt support for ComfyUI (the `role` key in `prompt_node_ids`
  is reserved for future per-role routing but currently iterates with a
  single segment prompt; all configured nodes receive the same text).

## 6. Compatibility & migration

- **Hosted / diffusers** legacy YAML without `prompt_body_key`: default
  `"prompt"` kicks in. If existing config relied on prompt coming via
  `params.prompt` or similar nested key, that still works — `params` is a
  dict in `body` and is untouched. Only the top-level `body["prompt"]`
  changes; nested `params.prompt` is independent.
- **Hosted / diffusers** servers that 422 on unknown top-level `"prompt"`
  field: set `prompt_body_key: null` in YAML to disable.
- **ComfyUI** legacy spec without `prompt_node_ids`: no behavior change;
  the existing baked-in prompt in the graph still drives the run.
- **Fal**: behavior preserved; retrofit is implementation-only.
- **No breaking CLI / public API changes.**

## 7. Open questions resolved

| # | Question | Resolution |
|---|---|---|
| Q1 | Scope — inline per engine, shared helper, or base class? | **B** (shared helper in `core/prompt_routing.py`) |
| Q2 | ComfyUI routing shape? | **A** (`prompt_node_ids: {role: node_id}`, hardcoded `inputs.text`) |
| Q3 | `validate_spec` posture? | **A** (validate when routing configured; legacy untouched) |
| Q4 | Default `prompt_body_key` for hosted/diffusers? | **A** (default `"prompt"`; opt-out via `null`) |
| Q5 | Retrofit Fal? | **A** (yes, behavior-preserving) |
| Q6 | ComfyUI `prompt_node_ids` location? | **A** (spec — mirrors `asset_node_ids`) |
