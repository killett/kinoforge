# Layer K — spec & params routing (YAML → orchestrator)

**Date:** 2026-05-31
**Author:** Dr. Twinklebrane (+ Claude)
**Status:** Approved design — implementation plan pending
**Closes:** PROGRESS.md:154 follow-up #1 ("`base_spec` routing from YAML cfg into the
orchestrator")
**Does not close:** PROGRESS.md:154 follow-up #2 (`_artifact_bytes` HTTP seam
Authorization-header support) — deferred to Layer L.

## 1. Problem

`Orchestrator.generate()` hardcodes `base_params={}` and `base_spec={}` when constructing
`GenerateClipStage` at `src/kinoforge/core/orchestrator.py:604-605`. As a result:

| engine | orchestrator-driven run today | reason |
|---|---|---|
| fal | works | needs only a prompt; Layer J segment-prompt fallback covers it |
| hosted | **fails `validate_spec`** | requires `spec.model` + `spec.params` |
| diffusers | **fails `validate_spec`** | requires `spec.pipeline` + `spec.scheduler` |
| comfyui | **fails `validate_spec`** | requires `spec.graph` + `spec.node_overrides` |

There is no YAML surface for `spec:` or `params:`. The CLI cannot drive hosted, diffusers,
or comfyui end-to-end. Configuring them requires a Python caller that bypasses the
orchestrator and constructs `GenerationJob` directly.

## 2. Goal & non-goals

### Goal

Route YAML-supplied `spec` and `params` blocks through `Orchestrator.generate()` into
`GenerationJob.spec` / `GenerationJob.params`. After this layer, a CLI user can run
hosted, diffusers, or comfyui end-to-end with a YAML config alone — matching the fal.ai
quickstart shipped in Layer I.

### Non-goals (Layer L candidates)

- `_artifact_bytes` HTTP seam Authorization-header support (RunwayML / Pika).
- Per-segment spec overrides — segments stay params-only.
- `GenerationRequest`-level `spec` / `params` slots — orchestrator is the sole source.
- CLI flags (`--spec key=val`, `--param fps=24`) — YAML only this layer.

## 3. Resolved design questions

| Q | Decision | Why |
|---|---|---|
| Layer scope | Spec routing only (defer auth headers) | Smallest unblock; auth is independent. |
| YAML shape | Top-level `spec:` + `params:` siblings of `engine:` / `models:` | Cleanly separates per-job payload from engine setup; mirrors `lifecycle:` / `models:`. |
| Config-load validation | Permissive `dict[str, Any]`, no type-check | Preserves core-import-ban invariant: Config never imports engine schemas. `engine.validate_spec` is the sole gate. |
| Accessor shape | Plain pydantic fields (`cfg.spec`, `cfg.params`) | Smallest diff; matches the permissive-dict decision. |
| Teardown on `validate_spec` failure | Yes — mirror `CapabilityMismatch` branch | Cost-safety invariant: a typo in `spec:` must not cost idle pod time. |

## 4. YAML schema

Two new top-level sibling blocks. Both optional, default `{}`.

```yaml
engine:
  kind: hosted
  hosted:
    endpoint: "https://your-shim.example.com/inference"
    api_key_env: "MY_SHIM_KEY"
    health_url: "https://your-shim.example.com/health"
    url_path: video.url

params:                       # engine-neutral knobs (every engine reads same shape)
  fps: 24
  num_frames: 81
  steps: 30
  seed: 42

spec:                         # engine-interpreted payload
  model: "wan-ai/Wan2.2-T2V-A14B"
  params:
    guidance_scale: 5.0

lifecycle:
  budget: 5.0
```

### Required `spec.*` keys per engine

| engine | required `spec.*` keys | notes |
|---|---|---|
| hosted | `model`, `params` | `key_base(cfg)` reads `engine.hosted.model`; the wire request body reads `spec.model`. The two coincide in normal use; see §8 hosted-yaml ambiguity. |
| diffusers | `pipeline`, `scheduler` | |
| comfyui | `graph`, `node_overrides` | Optional: `asset_node_ids`, `prompt_node_ids`. |
| fal | — | Segment prompt suffices; `spec:` may be omitted entirely. |

Required keys are enforced by the existing `engine.validate_spec(job)` — Config performs
no type-check.

#### Note: top-level `params:` vs nested `spec.params:`

Hosted requires a `params` key **inside** `spec:` as a wire body field
(`HostedAPIBackend.submit` writes it into the request body). This is structurally
distinct from top-level `params:` (engine-neutral knobs like `fps` / `num_frames` that
flow into `GenerationJob.params` and merge segment-wins). There is **no merging** between
the two namespaces.

```yaml
params:               # → GenerationJob.params (engine-neutral, segment-wins)
  fps: 24
spec:
  model: "wan-..."
  params:             # → GenerationJob.spec["params"] (hosted wire body)
    guidance_scale: 5.0
```

Reader takeaway: if a key matters to every engine, put it under top-level `params:`.
If it is engine-specific, put it under `spec:`.

### Effective-params precedence (segment-wins)

| layer | source | merged onto |
|---|---|---|
| job-level | `cfg.params` → `base_params` → `GenerationJob.params` | nothing |
| segment-level | `Segment.params` overrides `base_params` | `{**base_params, **segment.params}` per `core/strategy.py:27-28` |

Today the splitter emits empty per-segment `params`, so effective params = `cfg.params`.

### Spec precedence

Spec is **job-level only** (no segment override). `strategy.decide()` adds the
strategy-derived `_audio_mode` after merging — strategy always wins over a YAML
`spec._audio_mode`. Layer J's prompt routing precedence is preserved:
`resolve_prompt(job)` returns `job.spec["prompt"]` if YAML sets it, else
`segments[0].prompt`.

## 5. Config model change

Two new fields on `Config`. No new submodels. No alias.

```python
# src/kinoforge/core/config.py

class Config(BaseModel):
    engine: EngineConfig
    models: list[ModelEntry]
    compute: ComputeConfig | None = None
    lifecycle_cfg: LifecycleConfig | None = Field(default=None, alias="lifecycle")
    splitter: SplitterConfig = Field(default_factory=SplitterConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    spec: dict[str, Any] = Field(default_factory=dict)      # NEW (Layer K)
    params: dict[str, Any] = Field(default_factory=dict)    # NEW (Layer K)
```

- `dict[str, Any]` — no discriminated union by engine kind. Config stays engine-agnostic;
  the core-import-ban invariant is preserved.
- Absent YAML block → `{}` default. All existing example YAMLs (`wan`, `diffusers`,
  `hosted`, `fal`, `local-fake`) keep loading unchanged.
- Cross-field validator (`_validate_cross_fields`) is unchanged. No new validation in
  Config.

## 6. Orchestrator wiring

Single targeted change at `src/kinoforge/core/orchestrator.py:596-610`. Two-line swap
(constructor arguments) plus a `try/except ValidationError:` teardown wrapper around
`stage.run()`.

```python
with ConcurrentPool() as pool:
    pool.add(backend, max_in_flight=cfg.lifecycle().max_in_flight)
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id=run_id,
        accepted_kinds=accepted_kinds,
        base_params=dict(cfg.params),   # was {}
        base_spec=dict(cfg.spec),       # was {}
        engine=resolved_engine,
    )
    try:
        artifact = stage.run(request, segments_override=prompt_segments)
    except ValidationError:
        _log.warning(
            "spec validation failed; tearing down instance before re-raising"
        )
        if instance is not None and resolved_provider is not None:
            resolved_provider.destroy_instance(instance.id)
        raise
    _log.info("generate completed — artifact uri=%r", artifact.uri)
    return artifact
```

- `dict(...)` copy at construction prevents stage-side mutation from leaking back into
  `cfg` (pydantic returns the underlying dict by reference otherwise).
- Order of operations unchanged. `engine.validate_spec(job)` still runs inside
  `GenerateClipStage.run()` between `strategy.decide()` and `pool.map()` → before any
  `backend.submit()` wire I/O.
- `ValidationError` is `kinoforge.core.errors.ValidationError` (already raised by all
  four engines' `validate_spec`). No new error type.
- Teardown branch mirrors the existing `CapabilityMismatch` branch at lines 585-591.
  For hosted/fal (`requires_compute=False`), `instance` is `None` and the destroy call
  is skipped — same condition as the existing branch.

## 7. Testing (red-first, offline)

### Existing coverage that stays green

- `tests/core/test_config.py` — current cfg fields unchanged.
- `tests/core/test_orchestrator.py` — all today's tests pass empty `cfg.spec`/`cfg.params`
  (defaults). FakeEngine `required_spec_keys=set()` so empty spec validates.
- `tests/test_examples.py` — four existing example YAMLs still load (no `spec:`/`params:`
  blocks, default `{}`).

### New tests

| File | What it locks down |
|---|---|
| `tests/core/test_config.py` (add) | YAML with `spec: {model: "X"}` + `params: {fps: 24}` → `cfg.spec == {"model": "X"}`, `cfg.params == {"fps": 24}`. Absent blocks → empty dicts. Round-trip preserves types (int / float / nested dict / list). |
| `tests/core/test_orchestrator.py` (add) | (a) `cfg.spec` / `cfg.params` flow into `GenerationJob.spec` / `.params` via FakeEngine `required_spec_keys={"k"}` — empty `cfg.spec` → `ValidationError`; populated → success. (b) `dict(cfg.spec)` copy invariant: stage mutation does not mutate cfg. (c) Compute-teardown on `ValidationError`: FakeProvider with `destroy_instance` spy; assert called exactly once on bad spec; not called on good spec; not called when `requires_compute=False`. |
| `tests/core/test_strategy.py` (extend) | Segment-wins precedence over a non-empty `base_params`. Lock down `_audio_mode` overrides any YAML `spec._audio_mode`. |
| `tests/test_examples.py` (extend) | Updated `hosted.yaml` / `diffusers.yaml` / `wan.yaml` load + produce a non-empty `cfg.spec`. fal example stays minimal. `local-fake.yaml` stays untouched. |
| `tests/test_e2e_spec_routing.py` (new) | Orchestrator → FakeProvider → FakeEngine with `required_spec_keys={"model", "params"}` round-trip through a real YAML file written to `tmp_path`. Asserts artifact produced + observed `job.spec == cfg.spec` via a spy on `engine.validate_spec`. |

### Out of scope (no test)

- Real hosted / diffusers / comfyui dispatch — already covered by Layer J + earlier
  engine-level tests; only the wiring is new.
- Live fal.ai — still opt-in via `KINOFORGE_LIVE=1`.

## 8. Examples & docs

### Updated example YAMLs

- `examples/configs/hosted.yaml` — add `spec: {model, params}` and `params: {fps,
  num_frames, steps}`. Document the `engine.hosted.model` vs `spec.model` duplication
  (see hosted-yaml ambiguity below).
- `examples/configs/diffusers.yaml` — add `spec: {pipeline, scheduler}` and `params`.
- `examples/configs/wan.yaml` (comfyui) — add `spec: {graph, node_overrides}` (with
  a comment about `prompt_node_ids` / `asset_node_ids`) and `params`.
- `examples/configs/fal.yaml` — add a commented-out `spec:` / `params:` example showing
  the optional fields.
- `examples/configs/local-fake.yaml` — leave empty (FakeEngine `required_spec_keys=set()`
  so empty spec is valid).

### Hosted YAML ambiguity

`HostedEngineConfig.model` is required (`HostedAPIEngine.key_base(cfg)` uses it to derive
the `CapabilityKey`). YAML `spec.model` is what `HostedAPIBackend.submit` writes into the
wire request body.

| field | consumed by | purpose |
|---|---|---|
| `engine.hosted.model` | `key_base(cfg)` | `CapabilityKey` identity + profile cache key |
| `spec.model` | `HostedAPIBackend.submit` (`validate_spec` requires it) | wire request body |

In normal use they coincide. Layer K does **not** collapse them — keep both, document the
two-line duplication in the YAML comment. A future layer may merge them by having
`key_base(cfg)` fall back to `cfg.spec.model` when `engine.hosted.model` is absent.

### README updates

- New section: **"Per-job spec & params"** — required-spec table per engine, params
  semantics, segment-wins precedence pointer.
- Quickstart for hosted / diffusers / comfyui updated to reference the new YAML blocks.
  Today's quickstart only works end-to-end for fal.

### PROGRESS.md

- New "Phase 21 — Layer K (spec routing)" subsection mirroring Phase 20.
- Close PROGRESS:154 follow-up #1.
- Restate follow-up #2 (`_artifact_bytes` auth) as the next Layer L candidate.

## 9. Architectural invariants preserved

- **Core import ban.** Config remains engine-agnostic — no `kinoforge.engines.*` import
  in `core/config.py`.
- **Cost-safety.** `ValidationError` teardown branch matches the existing
  `CapabilityMismatch` branch — no new path that leaks compute on configuration error.
- **Strategy authority.** `_audio_mode` injected by `strategy.decide()` continues to
  override any user-supplied `spec._audio_mode`.
- **Pure functions.** `strategy.decide()` is unchanged. `dict(...)` copies in orchestrator
  preserve immutability invariants downstream.
- **Layer J prompt precedence.** `resolve_prompt(job)` is unchanged; YAML `spec.prompt`
  now reachable via this layer, but the precedence rule (`job.spec["prompt"]` →
  `segments[0].prompt`) is identical.

## 10. Out of scope but worth naming

- **`CLI --spec` / `--param` flags.** Useful for one-off overrides without editing YAML.
  YAGNI for Layer K; revisit when the live test corpus grows.
- **Per-engine config-load schemas.** Would catch typos earlier but require a registry
  hook + ABC method + breaking the core-import-ban or a separate registry. Defer until
  the runtime error story proves insufficient.
- **`GenerationRequest.spec` / `.params`.** A programmatic Python caller might want to
  override per request without rebuilding the cfg. Defer; no current consumer.
- **Collapsing `engine.hosted.model` ↔ `spec.model`.** See §8. Mechanical change once
  Layer K ships and we have a test corpus that exercises both fields.
