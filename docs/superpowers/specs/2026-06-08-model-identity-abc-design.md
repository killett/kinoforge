# Layer 8 — `GenerationEngine.model_identity` ABC

**Status:** spec draft (brainstorming output)
**Date:** 2026-06-08
**Author:** Claude (caveman mode) for Dr. Twinklebrane
**Closes carry-forward:** PROGRESS Phase 46 + Phase 47 — `LocalOutputSink`
renders `model = "unknown"` for fal and ComfyUI configs because
`cfg.spec["model"]` is empty for engines that carry their model identity
elsewhere.

## 1. Problem statement

`Orchestrator.deploy()` populates the user-facing sink filename slug as

```python
_model = str(cfg.spec.get("model", "") or "") or None
```

This works for hosted engines (Replicate / Runway / Luma / Bedrock) where
the model identity ships at top-level `spec.model`. It does **not** work
for engines whose identity lives in their own cfg block:

| Engine    | Identity source                                | Today's slug |
|-----------|------------------------------------------------|--------------|
| hosted    | `cfg.spec.model`                               | correct      |
| diffusers | `cfg.spec.model`                               | correct      |
| fal       | `cfg.engine.fal.endpoint`                      | `unknown`    |
| comfyui   | `cfg.models[base].ref` filename stem           | `unknown`    |
| bedrock   | `cfg.engine.bedrock_video.model_id`            | `unknown`    |

Real artifacts in `successful-generations.md` carry filenames such as
`comfyui_unknown_...mp4` and `fal_unknown_...mp4`. Cosmetic today;
load-bearing later — provenance grep, batch sidecars, cost-attribution
work assume the slug is a real identity.

## 2. Goals

1. Every engine surfaces a real human-readable model identity to
   `LocalOutputSink` without per-engine special-casing at the call site.
2. Adding a new engine fails at construction time if the author forgets to
   wire identity — no silent `unknown` fallback for future layers.
3. Cache identity (`key_base`) and display identity (`model_identity`) are
   independently evolvable; coupling them under one method is rejected.
4. `LocalOutputSink` filename schema is unchanged — only the values
   plugged into it improve.

## 3. Non-goals

- No new YAML schema for any engine. Each engine reads its identity from
  the field it already interprets natively.
- No change to `key_base` on `HostedAPIEngine` (Layer M's decision to put
  hosted model identity at `spec.model` stands).
- No change to the `LocalOutputSink.publish` Protocol signature or the
  `format_filename` schema.
- No cost-attribution / sidecar work — that is the separate Layer 5
  candidate noted at PROGRESS:203.

## 4. Decisions (brainstorm Q1–Q6)

| Q | Decision                                                              |
|---|-----------------------------------------------------------------------|
| 1 | All engines covered via ABC generalization, not per-engine patches.   |
| 2 | New `model_identity(cfg) -> str` `@abstractmethod` on `GenerationEngine`. Separate from `key_base`. |
| 3 | Each engine reads its native source (see table in §1).                |
| 4 | Empty / missing source → return `""`; sink renders `"unknown"`; no raise. |
| 5 | `KeyframeStage` (Layer R) routes the image engine's `model_identity` through the same ABC seam. |
| 6 | Orchestrator emits one `WARNING` per `deploy()` per stage when the engine returns `""`. |

## 5. Architecture

### 5.1 ABC contract

`src/kinoforge/core/interfaces.py` — add to `class GenerationEngine`:

```python
@abstractmethod
def model_identity(self, cfg: dict[str, object]) -> str:
    """Return a human-readable model slug for sink filenames.

    Display-only; independent of CapabilityKey / cache identity (see
    ``HostedAPIEngine.key_base``). Engines return the most specific
    human-grep-able surface they natively interpret: hosted ->
    ``cfg["spec"]["model"]``, fal -> ``cfg["engine"]["fal"]["endpoint"]``,
    comfyui -> filename stem of the ``kind == "base"`` entry in
    ``cfg["models"]``, etc.

    ``cfg`` is the same dict shape the engine receives in ``backend()``
    and ``validate_spec()``. For the keyframe path, that is the keyframe
    sub-cfg the stage feeds into the image engine, not the top-level
    Config — see KeyframeStage in §5.4.

    MUST NOT raise on a missing / empty source — return ``""`` instead.
    The orchestrator will log a single WARNING and the sink will fall
    back to the literal ``"unknown"``. (``key_base`` continues to raise
    ConfigError on missing identity because cache-identity is a stricter
    contract than display-identity.)

    Returns:
        Engine-native raw slug (will be slugified by the sink).
    """
    ...
```

### 5.2 Per-engine implementations

```python
# engines/hosted/__init__.py
def model_identity(self, cfg):
    return str(cfg.get("spec", {}).get("model", "") or "")

# engines/diffusers/__init__.py
def model_identity(self, cfg):
    return str(cfg.get("spec", {}).get("model", "") or "")

# engines/fal/__init__.py
def model_identity(self, cfg):
    return str(
        cfg.get("engine", {}).get("fal", {}).get("endpoint", "") or ""
    )

# engines/comfyui/__init__.py
def model_identity(self, cfg):
    for entry in cfg.get("models", []) or []:
        if entry.get("kind") == "base":
            ref = str(entry.get("ref", ""))
            tail = ref.rsplit(":", 1)[-1] if ":" in ref else ref
            stem = tail.rsplit(".", 1)[0] if "." in tail else tail
            return stem
    return ""

# engines/bedrock_video/__init__.py
def model_identity(self, cfg):
    return str(
        cfg.get("engine", {}).get("bedrock_video", {}).get("model_id", "")
        or ""
    )

# engines/fake/__init__.py  (offline test path)
def model_identity(self, cfg):
    return str(cfg.get("spec", {}).get("model", "") or "")
```

### 5.3 Orchestrator wiring — clip stage

`src/kinoforge/core/orchestrator.py` near line 1110, replace

```python
_provider = getattr(session.engine, "name", None) or None
_model = str(cfg.spec.get("model", "") or "") or None
```

with

```python
_provider = getattr(session.engine, "name", None) or None
_cfg_dict = cfg.model_dump()
_raw_model = session.engine.model_identity(_cfg_dict)
if not _raw_model:
    _log.warning(
        "engine %s returned empty model identity; "
        "sink will render filename slug as 'unknown'",
        session.engine.name,
    )
_model = _raw_model or None
```

### 5.4 Orchestrator wiring — keyframe stage

Near line 1058, replace

```python
_kf_provider = getattr(resolved_image_engine, "name", None) or None
_kf_model = str((cfg.keyframe.spec or {}).get("model", "") or "") or None
```

with

```python
_kf_provider = getattr(resolved_image_engine, "name", None) or None
_kf_cfg_dict = cfg.keyframe.model_dump() if cfg.keyframe else {}
_raw_kf_model = resolved_image_engine.model_identity(_kf_cfg_dict)
if not _raw_kf_model:
    _log.warning(
        "image engine %s returned empty model identity; "
        "sink will render keyframe filename slug as 'unknown'",
        resolved_image_engine.name,
    )
_kf_model = _raw_kf_model or None
```

The keyframe sub-cfg shape is whatever the image engine accepts in its
`backend()` / `validate_spec()`; today that is a dict with at least
`spec`. Image engines whose identity lives elsewhere implement
`model_identity` to read from wherever the keyframe cfg block carries it.

### 5.5 Data flow

```
YAML  →  load_config()  →  Config (pydantic)
                              │
                              ▼
                       cfg.model_dump() (dict)
                              │
                              ▼
                    engine.model_identity(dict)
                              │
                              ▼
                      raw_slug : str
                              │
            ┌─────────────────┴──────────────────┐
            │                                    │
        non-empty                              empty
            │                                    │
            ▼                                    ▼
   orchestrator threads             orchestrator logs WARNING
   raw_slug as `model`              orchestrator threads None
            │                                    │
            └─────────────────┬──────────────────┘
                              ▼
              GenerateClipStage.run / KeyframeStage.run
                              │
                              ▼
            sink.publish(..., model=raw_slug | None)
                              │
                              ▼
       LocalOutputSink: slugify(model or "unknown", 24)
                              │
                              ▼
   format_filename(ts, provider, model, slug, ext)
                              │
                              ▼
                  atomic write to disk
```

### 5.6 ABC enforcement

The existing ABC stable-surface invariant test (created in Phase 41 Task
1 + extended in Phase 43 Task 1) iterates registered engines. The new
abstract method joins that list. Any new engine that omits
`model_identity` cannot be instantiated; the registration test trips
loud.

## 6. Error handling

| Condition                                       | Behaviour                                                |
|-------------------------------------------------|----------------------------------------------------------|
| Native source absent / empty                    | Engine returns `""`. Orchestrator logs `WARNING`. Sink renders `"unknown"`. |
| Engine raises inside `model_identity`           | Bug — `model_identity` MUST NOT raise. ABC docstring + per-engine tests pin this. |
| Engine missing `model_identity` impl            | `TypeError` at engine construction (Python ABC behaviour). Engine-registration tests fail. |
| `cfg.keyframe is None`                          | `_kf_cfg_dict = {}` → engine returns `""` → WARNING → `unknown`. Acceptable; keyframe absent means no keyframe artifact anyway. |
| ComfyUI cfg has no `kind: base` entry           | Engine returns `""`. Load-time validation (existing exactly-one-base rule) catches this earlier in practice. |

## 7. Testing

### 7.1 Per-engine unit tests (`tests/engines/test_<engine>.py`)

For each of `hosted` / `diffusers` / `fal` / `comfyui` / `bedrock_video` /
`fake`:

- `test_model_identity_reads_native_source` — minimal cfg dict produces
  the expected raw slug. Bug it catches: engine reads the wrong field
  (e.g. fal reading `spec.model` instead of `engine.fal.endpoint`).
- `test_model_identity_empty_when_source_missing` — empty cfg produces
  `""`. Bug it catches: engine raises on missing field, breaking the
  documented contract.

### 7.2 ABC contract test

`tests/core/test_engine_abc_contract.py` (new):

- Import every registered engine via the engine registry.
- Assert each instance has `model_identity` callable, return type `str`,
  accepts an empty dict without raising.
- Bug it catches: a new engine ships with a `model_identity` that raises
  on empty cfg, or returns non-str.

### 7.3 Orchestrator wiring tests (`tests/core/test_orchestrator.py`)

- `test_clip_stage_threads_model_identity_into_sink` — Fake engine +
  spying sink; assert `sink.publish` receives the engine's
  `model_identity` return.
- `test_orchestrator_warns_when_model_identity_empty` — `caplog` captures
  one `WARNING` containing the engine name.
- `test_keyframe_stage_threads_image_model_identity_into_sink` —
  symmetric assertion for keyframe path.

### 7.4 Integration regression lock

`tests/integration/test_no_unknown_slug_for_example_configs.py` (new):

- For each YAML in `examples/configs/` (skipping `local-fake.yaml`):
  - Load via `load_config`.
  - Resolve the engine, call `engine.model_identity(cfg.model_dump())`.
  - Assert the result is non-empty.
- Bug it catches: a future YAML shape change (new engine block, renamed
  field) silently drops identity for an example config.

### 7.5 Test count expectation

~+14 net tests (5 engines × 2 per-engine + ABC contract + 3 orchestrator
+ integration regression + FakeEngine). Existing tests should remain
green; any tests that constructed a custom `GenerationEngine` subclass
must add a `model_identity` stub.

## 8. Migration / rollout

1. Land the ABC + the six engine impls + the orchestrator wiring + tests
   in a single atomic phase. The ABC change is breaking for any
   downstream `GenerationEngine` subclass; the in-repo blast radius is
   the six known engines plus any test-local subclass.
2. `grep`-and-fix any test that constructs an ad-hoc engine subclass and
   does not provide `model_identity` — convert each to a one-liner
   returning `""` (these are display-only test doubles, no functional
   need for real identity).
3. Bump `LocalOutputSink` example filenames in README + PROGRESS only
   when a live re-fire produces a new artifact; no doc churn required at
   merge time.

## 9. Carry-forwards / out of scope

- `LocalOutputSink` `mode_identity` (e.g. `t2v` / `i2v` / `flf2v`)
  surface — useful for filename + provenance, not in scope here. Future
  layer can add `mode_identity` as a sibling ABC method.
- LoRA-stack identity for filename — similar story, future sibling
  method.
- S3OutputSink filename schema — Phase 38 / Layer W shipped store-level
  S3; sink-level S3 is a separate future layer.
- Per-prediction cost capture on `RemoteSubmitPollBackend` — already
  scoped at PROGRESS:203 as a Layer 5 candidate; unrelated to identity.

## 10. Open questions

None at spec-draft time. All Q1–Q6 resolved during brainstorming.
