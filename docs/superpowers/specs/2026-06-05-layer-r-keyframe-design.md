# Layer R — Keyframe stage + ImageEngine sibling ABC + pipeline list-walker (GitHub issue #4)

**Date:** 2026-06-05
**Layer label:** Layer R (Phase 32)
**Scope:** New `ImageEngine` / `ImageBackend` / `ImageProfile` parallel ABCs;
new `KeyframeStage` consuming an ImageEngine; orchestrator rewired around a
walking `Pipeline = list[Stage]` with shared typed `PipelineState`; one
concrete offline `FakeImageEngine` + one live `FalImageEngine` (fal-ai/flux-schnell).
**Issue:** GH #4 — keyframe / image-generation upstream Stage.
**Motivation:** Today the i2v / flf2v pipeline assumes the user pre-supplies
conditioning images (`init_image`, `first_frame`, `last_frame`). Without a
keyframe stage the user runs a separate image-gen tool, saves a file, and
threads it back into a YAML — a two-tool workflow. This layer collapses the
workflow into one config and lays the foundation infrastructure (pipeline
list-walker + image-engine sibling ABCs) every future stage layer (audio,
upscale, stitch, storyboard) will compose on top of.

---

## 1. Decisions locked during brainstorming

| Q | Topic | Decision | Rationale |
|---|---|---|---|
| Q1 | PipelineState shape | Slim: `PipelineState{request, artifacts: dict[str, Artifact]}` | Stages rewrite `request` via `dataclasses.replace`; intermediates keyed by stage name. Typed, minimal, easy to extend. Audio/upscale/stitch add NO new fields — they store outputs in `artifacts` dict. |
| Q2 | Role coverage v1 | Generic role-loop over `MODE_ROLE_REQUIREMENTS` (convention v1: every role kind is `"image"`) | Day-1 cost ≈ Option 1; saves ~30 LOC + reviewer round-trip per future mode. Future audio role lands as schema migration in that layer. |
| Q3 | `MODE_ROLE_REQUIREMENTS` schema | Migrate to `dict[str, dict[role, kind]]` | Single source of truth. Sidecar-table alternative locks in a two-table drift surface forever for zero expressive gain. |
| Q4 | ImageEngine ABC shape | Parallel sibling ABCs (`ImageEngine`/`ImageBackend`/`ImageProfile`) to the video trio; zero touch to existing 5 video engines | Image profile fields (`max_resolution`, `supported_modes`) are image-shaped; no lying about `max_frames`/`fps`. Provisioner is Protocol-typed on shape → serves both hierarchies unchanged. |
| Q5 | Concrete engines v1 | `FakeImageEngine` (offline tests) + `FalImageEngine` (live, `fal-ai/flux-schnell`) | Mirrors Phase 19 FalEngine pattern. Live smoke ≈ $0.05–0.10 per full run. Hosted/Diffusers image engines deferred. |
| Q6 | YAML keyframe schema | Top-level `prompt` (default for all roles) + optional per-role `roles.<name>.prompt/spec/params` override | Foundation: explicit, no implicit clip-prompt inheritance. Common i2v case is one-liner; flf2v can differentiate bookends. |
| Q7 | Detection rule | Block-present + per-role gap fill: stage fires when `cfg.keyframe is not None`; per role in `MODE_ROLE_REQUIREMENTS[mode]`, if user supplied → keep; else → generate | Allows partial fill (flf2v with user-supplied first_frame + generated last_frame). Foundation: orthogonal, predictable. |
| Q8 | Pipeline composition | Derived from cfg-block presence; orchestrator builds the list. No user-facing `pipeline:` YAML in v1 | Matches existing config-block opt-in pattern (`cfg.splitter`, `cfg.store`, `cfg.output`). v1 has 2 stages; explicit ordering becomes useful only with ≥3. |
| Q9 | Image profile cache namespace | Separate file per profile kind: `<key>.video.json` and `<key>.image.json` | Zero cross-deserialisation risk. ~10 LOC. JsonImageProfileCache subclass / wrapper of JsonProfileCache pattern. |
| Q10 | `PipelineState` location | `src/kinoforge/core/interfaces.py` | Matches existing project pattern (every dataclass lives in `interfaces.py`); avoids import cycle on Stage Protocol forward-ref. |
| Q11 | `segments_override` kwarg | Drop the kwarg; make `segments: list[Segment]` a constructor field on `GenerateClipStage` | Single-path stage body. Uniform Stage Protocol across KeyframeStage + GenerateClipStage. ~30 test edits collapsed by `_make_stage` helper. |

---

## 2. Architecture

### 2.1 Three orthogonal foundations shipped together

1. **Pipeline list-walker.** Orchestrator constructs `list[Stage]` from
   cfg-block presence, walks it via shared typed `PipelineState`. Replaces
   today's inline single-stage call.
2. **Parallel ImageEngine sibling ABC hierarchy.** `ImageEngine` /
   `ImageBackend` / `ImageProfile` mirror the video trio. Zero touch to
   existing 5 video engines.
3. **`KeyframeStage`** consuming an ImageEngine to fill missing role-tagged
   conditioning assets before `GenerateClipStage` runs.

### 2.2 Data flow

```
cfg + request
   │
   ▼
orchestrator.generate()
   │  - pre-resolve image_engine + image_backend + image_profile
   │      (only if cfg.keyframe is not None; via JsonImageProfileCache)
   │  - deploy_session for video engine (unchanged)
   │  - validate_request (unchanged, in orchestrator)
   │  - splitter (unchanged, in orchestrator)
   │  - build stages list:
   │      stages = []
   │      if cfg.keyframe: stages.append(KeyframeStage(...))
   │      stages.append(GenerateClipStage(..., segments=prompt_segments))
   │  - state = PipelineState(request=validated, artifacts={})
   │  - for stage in stages:
   │      state = stage.run(state)
   ▼
KeyframeStage.run(state)
   │  - for role, kind in MODE_ROLE_REQUIREMENTS[request.mode].items():
   │      if kind != "image": continue
   │      if role in {a.role for a in request.assets}: continue
   │      prompt = resolve_prompt(role)   # per-role > top-level
   │      job = ImageJob(spec=..., prompt=prompt, params=...)
   │      image_engine.validate_spec(job)
   │      job_id = image_backend.submit(job)
   │      artifact = image_backend.result(job_id)
   │      png_bytes = artifact_bytes(artifact)
   │      stored = store.put_bytes(run_id, f"keyframe-{role}.png", png_bytes)
   │      state.artifacts[f"keyframe-{role}"] = stored
   │      state.request.assets.append(
   │          ConditioningAsset(kind="image", role=role, ref=stored))
   │  - return state
   ▼
GenerateClipStage.run(state)
   │  - same as today; sees the now-populated request.assets
   │  - state.artifacts["clip"] = final Artifact
   │  - return state
   ▼
return state.artifacts["clip"], session.instance
```

### 2.3 Backwards compatibility

Configs without `keyframe:` block produce a 1-stage pipeline
`[GenerateClipStage]` — bit-identical to current behaviour. A backcompat
lockdown test (§9) freezes this in.

### 2.4 Out of scope (carry-forwards, see §10)

- HostedImageEngine + DiffusersImageEngine concretes
- Image-backend pool (parallel flf2v role fills)
- Keyframe caching across runs
- User-facing `pipeline:` YAML override
- Multi-pass refinement keyframes
- Stitching across multi-segment clips that share a keyframe
- Image-engine teardown on `ValidationError` (added when compute-bound image engine lands)

---

## 3. New ABCs (`src/kinoforge/core/interfaces.py`)

### 3.1 `ImageProfile`

```python
@dataclass
class ImageProfile:
    """Capabilities of an image-generation model, read at plan time from cache.

    Sibling of ModelProfile (the video one) but image-shaped only.
    No fps / max_frames / native_extension / joint_audio.
    """
    name: str
    max_resolution: tuple[int, int]
    supported_modes: set[str]  # subset of {"t2i", "i2i", "inpaint"}
```

### 3.2 `ImageJob`

```python
@dataclass
class ImageJob:
    """One image-generation unit of work.

    Sibling of GenerationJob but no segments concept — one prompt → one image.
    """
    spec: dict
    prompt: str
    params: dict = field(default_factory=dict)
```

### 3.3 `ImageBackend`

```python
class ImageBackend(ABC):
    """A live, ready image engine jobs are submitted to."""

    @abstractmethod
    def capabilities(self) -> ImageProfile: ...

    @abstractmethod
    def inspect_capabilities(self) -> ImageProfile: ...

    @abstractmethod
    def submit(self, job: ImageJob) -> str: ...

    @abstractmethod
    def result(self, job_id: str) -> Artifact: ...

    @abstractmethod
    def endpoints(self) -> dict[str, str]: ...
```

### 3.4 `ImageEngine`

```python
class ImageEngine(ABC):
    name: str
    requires_compute: bool
    requires_local_weights: bool

    @abstractmethod
    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None: ...

    @abstractmethod
    def backend(self, instance: Instance | None, cfg: dict[str, object]) -> ImageBackend: ...

    @abstractmethod
    def profile_for(self, key: CapabilityKey) -> ImageProfile: ...

    @abstractmethod
    def validate_spec(self, job: ImageJob) -> None: ...
```

### 3.5 Deliberate omissions vs `GenerationEngine`

- `extract_last_frame` — meaningless for image output.
- `render_provision` — v1 image engines are hosted/queue (Fake + Fal,
  `requires_compute=False`). Added when DiffusersImageEngine lands.
- `wait_for_ready`, `attach_get_instance` — same reason.
- `declared_flags` — flag declaration is video-specific
  (`supports_native_extension`, `supports_joint_audio`); revisit when an
  image-engine feature flag exists.

### 3.6 Registry helpers (`src/kinoforge/core/registry.py`)

```python
_image_engines: dict[str, Callable[[], ImageEngine]] = {}

def register_image_engine(name: str, factory: Callable[[], ImageEngine]) -> None:
    _image_engines[name] = factory

def get_image_engine(name: str) -> Callable[[], ImageEngine]:
    if name not in _image_engines:
        raise UnknownAdapter(f"unknown image engine: {name!r}")
    return _image_engines[name]
```

### 3.7 Cache namespacing

`JsonImageProfileCache(store)` is a thin wrapper / subclass of
`JsonProfileCache(store)` that swaps the filename suffix from `.json`
to `.image.json`. Reads/writes the same JSON shape but with
`ImageProfile` fields. Implementation: ~10 LOC.

### 3.8 Provisioner + CapabilityKey reuse

- `provisioner.provision()` is Protocol-typed on `_ProvisionConfig` shape
  (`name + requires_compute + requires_local_weights + provision` callable).
  `ImageEngine` satisfies it structurally → provisioner serves both
  hierarchies unchanged. Future LoRA weight downloads on image engines
  reuse the same downloader.
- `CapabilityKey(base_model, loras, engine, precision)` keys both video
  and image caches; the namespace split (§3.7) prevents cross-deserialisation.

---

## 4. `PipelineState` + `Stage` Protocol change

### 4.1 `PipelineState`

```python
# src/kinoforge/core/interfaces.py — added near the existing dataclasses

@dataclass(frozen=True)
class PipelineState:
    """State threaded between pipeline stages.

    Frozen wrapper; stages produce a new state via dataclasses.replace.
    The artifacts dict is mutable in-place (matches the project pattern
    where dataclass.replace handles top-level swaps but contained
    collections may be mutated for clarity).

    Keys in `artifacts` are stage-defined names. KeyframeStage writes
    `keyframe-<role>` (e.g. `keyframe-init_image`, `keyframe-first_frame`).
    GenerateClipStage writes `clip`. Future: `audio`, `upscaled`,
    `stitched`, etc.
    """
    request: GenerationRequest
    artifacts: dict[str, Artifact] = field(default_factory=dict)
```

### 4.2 `Stage` Protocol update

```python
@runtime_checkable
class Stage(Protocol):
    """A pipeline stage: PipelineState in, PipelineState out."""

    def run(self, state: PipelineState) -> PipelineState:
        ...
```

### 4.3 `GenerateClipStage` migration

The existing `run(request, *, segments_override=None) -> Artifact` signature
becomes `run(state) -> PipelineState`. The `segments_override` kwarg is
dropped; `segments: list[Segment]` becomes a constructor field always
populated by the orchestrator. The dual-path `if/else` branch in `run()` is
removed.

Tests construct via a `_make_stage(...)` helper that takes `segments=` and
returns a `GenerateClipStage`. The previous `stage.run(req)` call shape
becomes `stage.run(PipelineState(request=req)).artifacts["clip"]`. ~30 test
sites edited; ~50–80 assertion-line touches.

Final body sketch:

```python
def run(self, state: PipelineState) -> PipelineState:
    request = state.request
    jobs = decide(self.profile, self.segments, self.base_params, self.base_spec)

    for job in jobs:
        self.engine.validate_spec(job)

    should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, {})
    if not should_chain and len(jobs) > 1:
        results = list(self.pool.map(jobs))
    else:
        results = []
        for i, job in enumerate(jobs):
            if i > 0 and should_chain:
                # ... existing continuity chain unchanged ...
            art = self.pool.submit(job).result()
            results.append(art)
    last = results[-1]
    payload = artifact_bytes(last, self.http_get_bytes)   # shared helper
    stored = self.store.put_bytes(self.run_id, last.filename, payload)
    if self.sink is not None:
        ext = Path(last.filename).suffix or ".bin"
        self.sink.publish(
            payload, prompt=self.segments[-1].prompt,
            extension=ext, namespace=self.namespace,
        )
    return replace(
        state,
        artifacts={**state.artifacts, "clip": stored},
    )
```

### 4.4 Shared `artifact_bytes` helper

Extract `GenerateClipStage._artifact_bytes` into
`src/kinoforge/pipeline/artifact_bytes.py` (~40 LOC). Both
`GenerateClipStage` and `KeyframeStage` consume it. Existing tests for
`_artifact_bytes` move to `tests/pipeline/test_artifact_bytes.py`;
`GenerateClipStage._artifact_bytes` deleted (no production callers
outside the stage body itself; any test that touched the private method
is migrated to call `artifact_bytes(artifact, http_get_bytes)` directly).

---

## 5. `KeyframeStage` (`src/kinoforge/pipeline/keyframe.py`)

### 5.1 Construction

```python
@dataclass
class KeyframeStage:
    """Fills missing image-kind conditioning roles via an ImageEngine.

    Reads MODE_ROLE_REQUIREMENTS[request.mode] to discover required roles;
    for each role with kind == "image" not already present in request.assets,
    generates an image via the configured ImageEngine and appends a
    ConditioningAsset. User-supplied assets are preserved (per-role gap fill).
    """
    keyframe_cfg: KeyframeConfig
    image_engine: ImageEngine
    image_backend: ImageBackend
    image_profile: ImageProfile          # reserved for future spec validation
                                          # (capabilities check before submit)
    store: ArtifactStore
    run_id: str
    http_get_bytes: Callable[[str, dict[str, str]], bytes] | None = None
```

### 5.2 `run()` body

```python
def run(self, state: PipelineState) -> PipelineState:
    request = state.request
    required = MODE_ROLE_REQUIREMENTS.get(request.mode, {})
    have = {a.role for a in request.assets}

    new_assets = list(request.assets)
    new_artifacts = dict(state.artifacts)

    for role, kind in required.items():
        if kind != "image":
            continue
        if role in have:
            continue
        prompt = self._resolve_prompt(role)
        spec = self._resolve_spec(role)
        params = self._resolve_params(role)
        job = ImageJob(spec=spec, prompt=prompt, params=params)
        self.image_engine.validate_spec(job)
        job_id = self.image_backend.submit(job)
        artifact = self.image_backend.result(job_id)
        png_bytes = artifact_bytes(artifact, self.http_get_bytes)
        filename = f"keyframe-{role}.png"
        stored = self.store.put_bytes(self.run_id, filename, png_bytes)
        stored = replace(stored, filename=filename)
        new_assets.append(
            ConditioningAsset(kind="image", role=role, ref=stored)
        )
        new_artifacts[f"keyframe-{role}"] = stored

    new_request = replace(request, assets=new_assets)
    return replace(state, request=new_request, artifacts=new_artifacts)
```

### 5.3 Resolution helpers

```python
def _resolve_prompt(self, role: str) -> str:
    """Per-role override > top-level default. No clip-prompt inheritance."""
    role_block = (self.keyframe_cfg.roles or {}).get(role)
    if role_block and role_block.prompt:
        return role_block.prompt
    if self.keyframe_cfg.prompt:
        return self.keyframe_cfg.prompt
    raise ValidationError(
        f"keyframe role {role!r} has no prompt configured: set "
        f"keyframe.prompt or keyframe.roles.{role}.prompt"
    )

def _resolve_spec(self, role: str) -> dict:
    """Per-role spec overrides shallow-merged onto top-level keyframe spec."""
    base = dict(self.keyframe_cfg.spec or {})
    role_block = (self.keyframe_cfg.roles or {}).get(role)
    if role_block and role_block.spec:
        base.update(role_block.spec)
    return base

def _resolve_params(self, role: str) -> dict:
    """Per-role params shallow-merged onto top-level keyframe params."""
    base = dict(self.keyframe_cfg.params or {})
    role_block = (self.keyframe_cfg.roles or {}).get(role)
    if role_block and role_block.params:
        base.update(role_block.params)
    return base
```

### 5.4 Persistence convention

- Filename: `keyframe-<role>.png` (matches `seg-N-tail.png` continuity convention).
- Store path: `<store-root>/<run_id>/keyframe-<role>.png` — same `put_bytes` call as continuity.
- `state.artifacts[f"keyframe-{role}"]` carries the `Artifact` handle.
- Output sink (Layer O): **not** published. Keyframes are intermediates,
  not user-facing deliverables. A future `output_intermediates: true` knob
  can opt in.

### 5.5 Failure semantics

- `validate_spec` raises `ValidationError` → orchestrator's existing
  try/except wraps; teardown on compute path (video engine), no teardown
  on image side v1.
- `backend.submit` / `backend.result` raises → propagates; orchestrator
  wrapper does NOT teardown image side (no compute today). When
  DiffusersImageEngine lands, add image-engine teardown branch.
- `artifact_bytes` raises → wraps as `KeyframeFetchError(KinoforgeError)`.
- `_resolve_prompt` raises `ValidationError` — should be caught at cfg-load
  by pydantic validator (§7); stage raise is belt-and-braces.

### 5.6 Pool dispatch deferred

Flf2v fires 2 image jobs serially (~2-4 sec each for fal-flux-schnell).
Parallelisation via image-backend pool would require generalising
`ConcurrentPool` over `BackendPool[BackendT]` or adding `ImageBackendPool`.
YAGNI v1. Carry-forward §10.

---

## 6. `MODE_ROLE_REQUIREMENTS` schema migration

### 6.1 Schema change

```python
# src/kinoforge/core/interfaces.py:237

# BEFORE
MODE_ROLE_REQUIREMENTS: dict[str, set[str]] = {
    "t2v": set(),
    "i2v": {"init_image"},
    "flf2v": {"first_frame", "last_frame"},
}

# AFTER
MODE_ROLE_REQUIREMENTS: dict[str, dict[str, str]] = {
    "t2v": {},
    "i2v": {"init_image": "image"},
    "flf2v": {"first_frame": "image", "last_frame": "image"},
}
```

### 6.2 Touch sites (3 production + 1 test literal)

```python
# src/kinoforge/pipeline/generate_clip.py:166
# BEFORE
should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, set())
# AFTER
should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, {})
```

```python
# src/kinoforge/core/validation.py:62
# BEFORE
required_roles: set[str] = MODE_ROLE_REQUIREMENTS[request.mode]
# AFTER
required_roles: set[str] = set(MODE_ROLE_REQUIREMENTS[request.mode])
```

```python
# tests/core/test_interfaces.py:69
# BEFORE
assert MODE_ROLE_REQUIREMENTS == {
    "t2v": set(),
    "i2v": {"init_image"},
    "flf2v": {"first_frame", "last_frame"},
}
# AFTER
assert MODE_ROLE_REQUIREMENTS == {
    "t2v": {},
    "i2v": {"init_image": "image"},
    "flf2v": {"first_frame": "image", "last_frame": "image"},
}
```

### 6.3 New lockdown test (drift guard)

```python
# tests/core/test_interfaces.py — append

VALID_KINDS = {"image", "audio", "video"}

def test_mode_role_requirements_kinds_are_valid() -> None:
    """Every role kind must be one of the known kinds.

    Foundation: catches typos and accidental drift if someone adds a role
    with a freeform kind string. Update VALID_KINDS when a new media kind
    is introduced.
    """
    for mode, roles in MODE_ROLE_REQUIREMENTS.items():
        for role, kind in roles.items():
            assert kind in VALID_KINDS, (
                f"role {role!r} in mode {mode!r} has unknown kind {kind!r}; "
                f"valid kinds: {sorted(VALID_KINDS)}"
            )
```

### 6.4 New helper

```python
# src/kinoforge/core/interfaces.py — append near MODE_ROLE_REQUIREMENTS

def required_image_roles(mode: str) -> list[str]:
    """Return ordered list of image-kind roles required by `mode`.

    Order is dict-insertion order from MODE_ROLE_REQUIREMENTS so flf2v always
    returns [first_frame, last_frame], never [last_frame, first_frame].
    """
    return [role for role, kind in MODE_ROLE_REQUIREMENTS.get(mode, {}).items()
            if kind == "image"]
```

`KeyframeStage` uses this helper.

### 6.5 PROGRESS line 72 text update

```diff
- Continuity dispatch via `MODE_ROLE_REQUIREMENTS` — injects only when `"init_image"` in role contract (i2v today; t2v/flf2v skip); future modes automatic.
+ Continuity dispatch via `MODE_ROLE_REQUIREMENTS` — injects only when `"init_image"` in role contract keys (i2v today; t2v/flf2v skip); future modes automatic. Schema: `dict[mode, dict[role, kind]]` since Layer R.
```

Migration total: ~10 LOC across 3 prod sites + 1 test literal + 1 new lockdown test + 1 new helper.

---

## 7. Cfg model + YAML schema

### 7.1 Pydantic models (`src/kinoforge/core/config.py` — append)

```python
class KeyframeRoleOverride(BaseModel):
    """Per-role keyframe overrides (prompt / spec / params)."""
    prompt: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


class KeyframeConfig(BaseModel):
    """Keyframe-generation block. Presence opts the orchestrator into
    constructing a KeyframeStage at the head of the pipeline.

    Required: `engine` (image-engine registry name).
    Required by validator: either `prompt` (top-level default) OR
    `roles.<name>.prompt` for at least one role.
    """
    engine: str
    prompt: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    roles: dict[str, KeyframeRoleOverride] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _at_least_one_prompt(self) -> "KeyframeConfig":
        has_top = bool(self.prompt and self.prompt.strip())
        has_role = any(
            r.prompt and r.prompt.strip() for r in self.roles.values()
        )
        if not has_top and not has_role:
            raise ValueError(
                "keyframe block requires either top-level `prompt` "
                "or at least one `roles.<role>.prompt`"
            )
        return self

    @model_validator(mode="after")
    def _role_names_known(self) -> "KeyframeConfig":
        from kinoforge.core.interfaces import MODE_ROLE_REQUIREMENTS
        known = {
            role
            for roles in MODE_ROLE_REQUIREMENTS.values()
            for role in roles
        }
        unknown = set(self.roles) - known
        if unknown:
            raise ValueError(
                f"keyframe.roles contains unknown role(s): {sorted(unknown)}; "
                f"known: {sorted(known)}"
            )
        return self

    def capability_key(self) -> CapabilityKey:
        """Image-engine capability key for the image profile cache."""
        return CapabilityKey(
            base_model=str(self.spec.get("model", "")),
            loras=(),
            engine=self.engine,
            precision=str(self.spec.get("precision", "")),
        )


class Config(BaseModel):
    # ... existing fields ...
    keyframe: KeyframeConfig | None = None    # NEW
```

### 7.2 YAML example — i2v (`examples/configs/keyframe-fal-i2v.yaml`)

```yaml
# Keyframe + i2v: fal generates first frame, fal generates clip.
mode: i2v
prompt: "a cat walking through a sunlit meadow, soft motion"

engine:
  kind: fal
  fal:
    endpoint: "fal-ai/wan-i2v"

spec:
  model: "fal-ai/wan-i2v"

keyframe:
  engine: fal
  prompt: "photorealistic cat in a sunlit meadow, shot on 35mm film, shallow depth of field"
  spec:
    model: "fal-ai/flux-schnell"
    # size: [1024, 1024]   # optional
    # seed: 42             # optional

models:
  - kind: base
    ref: "fal://fal-ai/wan-i2v"

compute: null    # hosted/queue path

lifecycle:
  idle_timeout_s: 60
  max_lifetime_s: 600
  budget_usd: 1.0
  max_in_flight: 1
```

### 7.3 YAML example — flf2v with differentiated bookends (`examples/configs/keyframe-fal-flf2v.yaml`)

```yaml
mode: flf2v
prompt: "a cat morphing into a tiger, smooth transition"

engine:
  kind: fal
  fal:
    endpoint: "fal-ai/wan-flf2v"

spec:
  model: "fal-ai/wan-flf2v"

keyframe:
  engine: fal
  spec:
    model: "fal-ai/flux-schnell"
  roles:
    first_frame:
      prompt: "photorealistic cat sitting in meadow, centered, soft daylight"
      spec:
        seed: 42
    last_frame:
      prompt: "photorealistic tiger sitting in meadow, centered, same composition, same lighting"
      spec:
        seed: 43

models:
  - kind: base
    ref: "fal://fal-ai/wan-flf2v"

compute: null
lifecycle:
  idle_timeout_s: 60
  max_lifetime_s: 600
  budget_usd: 1.0
  max_in_flight: 1
```

### 7.4 No CLI changes

`cfg.keyframe` is loaded from YAML; CLI exposes no `--keyframe-*` flags in
v1. Future polish layer can add overrides.

---

## 8. Orchestrator changes

### 8.1 `generate()` body delta

Pre-pipeline (before `deploy_session`):

```python
# Build image engine + backend + profile (only if cfg.keyframe present)
image_backend = None
image_prof = None
resolved_image_engine = None
if cfg.keyframe is not None:
    resolved_image_engine = (
        image_engine
        if image_engine is not None
        else registry.get_image_engine(cfg.keyframe.engine)()
    )
    # Image engine receives ONLY the keyframe block, not the full cfg —
    # image engine never needs to know about the video engine config.
    kf_cfg_dict = cfg.keyframe.model_dump()
    resolved_image_engine.provision(None, kf_cfg_dict)   # hosted preflight
    image_backend = resolved_image_engine.backend(None, kf_cfg_dict)
    image_key = cfg.keyframe.capability_key()
    ipp = (
        image_profile_provider
        if image_profile_provider is not None
        else JsonImageProfileCache(store)
    )
    try:
        image_prof = ipp.resolve(image_key)
    except ProfileNotCached:
        image_prof = ipp.discover(image_key, resolved_image_engine, image_backend)
```

Inside `deploy_session` (replaces today's inline `GenerateClipStage` call):

```python
stages: list[Stage] = []
if cfg.keyframe is not None:
    stages.append(KeyframeStage(
        keyframe_cfg=cfg.keyframe,
        image_engine=resolved_image_engine,
        image_backend=image_backend,
        image_profile=image_prof,
        store=store,
        run_id=run_id,
    ))
stages.append(GenerateClipStage(
    profile=session.profile,
    pool=session.pool,
    store=store,
    run_id=run_id,
    accepted_kinds=accepted_kinds,
    base_params=dict(cfg.params),
    base_spec=dict(cfg.spec),
    engine=session.engine,
    segments=prompt_segments,    # NEW — formerly via run() kwarg
    sink=sink,
))

state = PipelineState(request=validated, artifacts={})
try:
    for stage in stages:
        state = stage.run(state)
except ValidationError:
    _log.warning("spec validation failed; tearing down instance before re-raising")
    if (
        session.instance is not None
        and session.provider is not None
        and not _caller_supplied_instance
    ):
        session.provider.destroy_instance(session.instance.id)
    raise

artifact = state.artifacts["clip"]
_log.info("generate completed — artifact uri=%r", artifact.uri)
owned_instance = None if _caller_supplied_instance else session.instance
return artifact, owned_instance
```

### 8.2 Notable choices

- **Image engine resolved BEFORE `deploy_session`.** Cheap (no compute);
  misconfigured `cfg.keyframe.engine` (`UnknownAdapter`) fails fast before
  spinning up a paid GPU pod.
- **No image-engine teardown on `ValidationError` v1.** Image engines are
  hosted (no compute). DiffusersImageEngine adds the teardown branch.
- **`ImageProfileProvider` test seam.** New ABC parallel to
  `ModelProfileProvider`; default `JsonImageProfileCache(store)`.
- **Test injection points** added to `generate()` signature:
  `image_engine`, `image_profile_provider`. Mirrors `engine=` /
  `profile_provider=` pattern. `batch_generate()` gets the same params.

### 8.3 `batch.py` mirror

`batch_generate()` at `src/kinoforge/core/batch.py:250` gets the same
image-engine pre-resolution + stage-list construction. Per-entry overrides
for `cfg.keyframe.prompt` (via existing shallow-merge mechanism) are honored
uniformly.

### 8.4 `deploy()` top-level CLI

No change — `deploy()` only sets up compute; the keyframe stage is a
`generate()`-time concern.

---

## 9. Concrete engines

### 9.1 `FakeImageEngine` (offline tests; `src/kinoforge/image_engines/fake/__init__.py`)

Deterministic submit IDs (sha256 over prompt + spec); `result()` returns
`Artifact(filename=..., meta={...})` with `_synthetic: True` marker. The
`artifact_bytes()` helper's synthetic-fallback branch produces deterministic
bytes from filename + meta repr. Tests assert bytes flow end-to-end through
`store.put_bytes`; nothing decodes them as real PNG. Real PNG production is
the Fal engine's job.

Self-registers under `"fake"` in the image-engine registry.

### 9.2 `FalImageEngine` (live; `src/kinoforge/image_engines/fal/__init__.py`)

- Shares `src/kinoforge/engines/fal/wire.py` helpers directly (status enum +
  URL builders are pure functions; no extraction needed).
- HTTP I/O via injected `http_post` / `http_get` seams; default `_default_post`
  / `_default_get` use urllib with `User-Agent: kinoforge/0.1` (consistent
  with PROGRESS bug-catch trail on edge-proxy 403s).
- `submit` POSTs to `https://queue.fal.run/<endpoint>`; `result` polls
  status_url then fetches response_url; extracts `images[0].url`.
- `Artifact(url=url, filename=...)` — fal signed URLs need no auth for fetch.
- `provision(None, cfg)` validates `FAL_KEY` present via
  `EnvCredentialProvider`; raises `AuthError` if missing.
- `profile_for(key)` returns a **static** `ImageProfile` (max_resolution
  `(1024, 1024)`, supported_modes `{"t2i"}`). Dynamic capability sniffing
  per-endpoint is a carry-forward.
- `validate_spec(job)` requires non-empty `prompt` + `spec.model`.

Self-registers under `"fal"` in the image-engine registry.

**Live-smoke endpoint:** `fal-ai/flux-schnell` (~$0.003/image, ~2-3 sec per
request).

---

## 10. Tests + live smoke + backwards compat + carry-forwards

### 10.1 Offline unit tests

| File | Surface | New tests |
|---|---|---|
| `tests/core/test_image_interfaces.py` | ImageEngine/Backend/Profile/Job ABCs + dataclasses | ~5 |
| `tests/core/test_image_profile_cache.py` | JsonImageProfileCache (`.image.json` namespace, isolation from video cache) | ~10 |
| `tests/core/test_keyframe_config.py` | KeyframeConfig pydantic validators | ~8 |
| `tests/pipeline/test_pipeline_state.py` | PipelineState dataclass + Stage Protocol structural check | ~3 |
| `tests/pipeline/test_keyframe_stage.py` | i2v fill, flf2v fill both, partial user-supplied (gap-fill), prompt resolution, spec/params shallow-merge, missing-prompt raise, kind-filter | ~12 |
| `tests/pipeline/test_artifact_bytes.py` | Extracted helper (existing `_artifact_bytes` tests moved + 2 new) | ~8 (4 moved, 4 new) |
| `tests/image_engines/test_fake.py` | FakeImageEngine deterministic submit IDs, profile_for, validate_spec gates | ~6 |
| `tests/image_engines/test_fal.py` | FalImageBackend submit POST shape, result poll-then-fetch, error paths, AuthError | ~12 |
| `tests/core/test_orchestrator.py` | generate() builds 1-stage default, 2-stage when cfg.keyframe present, ImageEngine resolved before deploy_session, profile cache miss → discover, ValidationError teardown | ~6 |
| `tests/test_examples.py` | Both new YAML examples parse + cfg.keyframe populated | ~4 |
| `tests/pipeline/test_generate_clip.py` | Existing tests migrated to `segments=` constructor; 0 net new, ~30 site edits | 0 net |
| `tests/test_pipeline_invariant.py` | Extend core-import-ban allowlist for `image_engines/`; assert ImageEngine ABCs not imported by `core/` modules | ~1 |

**Total new offline tests: ~75.** Existing test migrations: ~30 sites.
Test count delta: **1111 → ~1186**.

### 10.2 Live smoke (gated on `KINOFORGE_LIVE_TESTS=1`)

`tests/live/test_keyframe_fal_live.py` — 2 tests:

```python
def test_keyframe_fal_i2v_live(tmp_path: Path) -> None:
    """End-to-end: cfg.keyframe + mode=i2v → fal generates init_image →
    wan-i2v consumes it → MP4 output exists.

    Real spend: ~$0.003 (keyframe) + ~$0.02 (clip) ≈ $0.025.
    """
    # load examples/configs/keyframe-fal-i2v.yaml
    # run kinoforge generate
    # assert state.artifacts["keyframe-init_image"] persisted under run_id
    # assert state.artifacts["clip"] persisted under run_id
    # assert keyframe PNG starts with PNG magic bytes 0x89504E47
    # assert clip MP4 starts with ftyp/isom magic

def test_keyframe_fal_flf2v_live(tmp_path: Path) -> None:
    """flf2v variant — fal generates both bookends with differentiated prompts,
    wan-flf2v morphs between them.

    Real spend: ~$0.006 (2 keyframes) + ~$0.025 (clip) ≈ $0.031.
    """
    # assert keyframe-first_frame + keyframe-last_frame both persisted
    # assert their bytes differ (different prompts → different images)
```

**Total live-smoke ceiling: ~$0.05–0.10 per full run.** Layer-R budget
projection: **$0.20** (allows ~3 full re-runs during bug-fix wave).

Default-skip count: 6 → 8 (3 existing + 2 HF + 1 SkyPilot + 2 Layer R).

### 10.3 Backwards compat lockdown (`tests/test_layer_r_backcompat.py`)

1. **`cfg.keyframe is None` → 1-stage pipeline.** Construct `generate()`
   with a no-keyframe cfg + `engine=FakeEngine`; assert exactly one stage
   was constructed; output bytes match a baseline hash from a pre-Layer-R
   run captured into fixtures.
2. **Existing YAML examples produce zero-keyframe pipelines.** Iterate
   `examples/configs/*.yaml` (excluding the 2 new keyframe-*.yaml); load
   each via `load_config`; assert `cfg.keyframe is None`.
3. **MODE_ROLE_REQUIREMENTS dict schema migration is byte-compatible for `in`
   operator usage.** Lockdown: `"init_image" in MODE_ROLE_REQUIREMENTS["i2v"]`
   returns True both before and after migration.

### 10.4 Carry-forwards (PROGRESS Phase 32 "Out of scope")

- **HostedImageEngine** (Together / Replicate / OpenAI Images) — proves
  direct-HTTP shape for image side.
- **DiffusersImageEngine** (SDXL on RunPod) — proves local-GPU compute
  path; first ImageEngine with `requires_compute=True`. Adds image-engine
  teardown branch to orchestrator.
- **Image-backend pool** for parallel flf2v role fills (2-job
  parallelisation). Today serial.
- **Keyframe caching across runs** — same `(prompt, seed, engine, model)`
  → reuse stored artifact via ArtifactStore lookup before submit. Naturally
  extends via `store.get_bytes` pre-check.
- **User-facing `pipeline:` YAML override** — explicit stage ordering. Add
  when ≥3 stages exist and order ambiguity is real.
- **`output_intermediates: true` cfg knob** — publish keyframes (not just
  clip) to user-facing sink.
- **LoRA support on image engines** — extend `ImageProfile` with
  `loras: tuple[str, ...]`; capability_key picks it up.
- **Dynamic fal capability sniffing** — per-endpoint `max_resolution`
  instead of static 1024×1024.
- **Splitter into `GenerateClipStage`** — eliminates orchestrator's splitter
  knowledge; cleaner separation. Touched by §8 refactor but deferred.
- **Multi-pass refinement keyframes** — chain
  `KeyframeStage → KeyframeRefineStage → GenerateClipStage`.
- **Stitching across multi-segment clips sharing one keyframe** — orthogonal
  to segment continuity.

### 10.5 Phase metadata

- **Spec:** `docs/superpowers/specs/2026-06-05-layer-r-keyframe-design.md` (this file).
- **Plan:** `docs/superpowers/plans/2026-06-05-layer-r-keyframe.md` (to follow).
- **PROGRESS entry:** Phase 32 — Layer R (keyframe stage + ImageEngine sibling ABC + pipeline list-walker).
- **Closes:** GH #4 (keyframe / image-generation upstream Stage).
- **Layer-R budget:** $0.20 live spend ceiling (~3 full re-runs during bug-fix wave).
