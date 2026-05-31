# Layer K — Spec & params routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route YAML-supplied `spec` and `params` blocks through `Orchestrator.generate()` into `GenerationJob.spec` / `GenerationJob.params`, replacing the hardcoded `{}` empties at `src/kinoforge/core/orchestrator.py:604-605`. Unblock orchestrator-driven runs for hosted, diffusers, comfyui.

**Architecture:** Two new pydantic fields on `Config` (`spec: dict[str, Any]`, `params: dict[str, Any]`, both default `{}`). Orchestrator reads them at stage construction. A `try / except ValidationError:` wrapper around `stage.run()` tears down compute on bad spec, mirroring the existing `CapabilityMismatch` branch. No new interfaces, no new ABCs, no new registries. Permissive dict pass-through preserves the core-import-ban invariant.

**Tech Stack:** Python 3.11+, pydantic v2 for config, pytest. No new runtime deps.

**Spec:** `docs/superpowers/specs/2026-05-31-layer-k-spec-routing-design.md`

---

## File map

**Create:**
- `tests/test_e2e_spec_routing.py` — orchestrator → FakeProvider → FakeEngine round-trip through a real YAML file (Task 4).

**Modify:**
- `src/kinoforge/core/config.py` — add `from typing import Any` to imports + `spec` / `params` fields on `Config` class (Task 1).
- `src/kinoforge/core/orchestrator.py` — add `ValidationError` to errors import + swap `base_params={}` / `base_spec={}` at lines 604-605 → `dict(cfg.params)` / `dict(cfg.spec)` + wrap `stage.run()` in `try / except ValidationError:` teardown branch (Task 2).
- `tests/core/test_config.py` — 4 new tests for YAML round-trip + defaults + nested-type preservation (Task 1).
- `tests/core/test_orchestrator.py` — 3 new tests for cfg→job flow, dict-copy isolation, teardown on `ValidationError` (Task 2).
- `tests/core/test_strategy.py` — 2 new tests pinning `_audio_mode` authority + segment-wins over non-empty `base_params` (Task 3).
- `examples/configs/hosted.yaml` — add `spec:` + `params:` blocks; document `engine.hosted.model` vs `spec.model` duplication (Task 5).
- `examples/configs/diffusers.yaml` — add `spec: {pipeline, scheduler}` + `params:` (Task 5).
- `examples/configs/wan.yaml` — add `spec: {graph, node_overrides}` + `params:` (Task 5).
- `examples/configs/fal.yaml` — add commented-out optional `spec:` / `params:` example (Task 5).
- `tests/test_examples.py` — extend example-load assertions to verify non-empty `cfg.spec` for the 3 non-fal engines + empty for fal + fake (Task 5).
- `README.md` — new "Per-job spec & params" section + quickstart updates for hosted/diffusers/comfyui (Task 6).
- `PROGRESS.md` — Phase 21 entry; close follow-up #1; restate follow-up #2 (Task 6).

**Untouched (still load empty `spec:`/`params:` defaults):**
- `examples/configs/local-fake.yaml` (FakeEngine `required_spec_keys=set()`).

---

### Task 1: Config `spec` + `params` pydantic fields

**Goal:** Add two new top-level fields to `Config` (`spec: dict[str, Any] = {}`, `params: dict[str, Any] = {}`) and lock down YAML round-trip behavior.

**Files:**
- Modify: `src/kinoforge/core/config.py:14` (add `Any` to typing import); `src/kinoforge/core/config.py:415` (add two fields after `store: StoreConfig`)
- Test: `tests/core/test_config.py` (append 4 new tests at end of file)

**Acceptance Criteria:**
- [ ] `Config(**yaml.safe_load("engine: {kind: fake, precision: fp16}\nmodels: []\n"))` succeeds with `cfg.spec == {}` and `cfg.params == {}`.
- [ ] YAML containing `spec: {model: "X"}` and `params: {fps: 24}` produces `cfg.spec == {"model": "X"}` and `cfg.params == {"fps": 24}`.
- [ ] Nested types preserved through round-trip: `spec: {params: {guidance_scale: 5.0}, graph: {nodes: [1, 2]}}` survives as `dict`/`list`/`float`/`int` (no string coercion).
- [ ] `cfg.model_dump()["spec"]` returns the same dict (round-trip).
- [ ] Cross-field validator (`_validate_cross_fields`) unchanged — does not reference the new fields.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config.py` passes (ruff/format/mypy).

**Verify:** `pixi run pytest tests/core/test_config.py -v -k "spec or params"` → at least 4 new tests pass; total file test count grows by 4; no existing test in `test_config.py` regresses.

**Steps:**

- [ ] **Step 1: Write failing tests** — append at end of `tests/core/test_config.py`:

```python
def test_config_spec_defaults_to_empty_dict() -> None:
    """A YAML without spec: must produce cfg.spec == {} (not None, not missing).

    Bug catch: a typo like `spec: dict | None = None` would let downstream
    `dict(cfg.spec)` raise TypeError on configs that omit the block.
    """
    yaml_text = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
"""
    cfg = load_config(yaml_text)
    assert cfg.spec == {}
    assert cfg.params == {}


def test_config_spec_and_params_loaded_from_yaml() -> None:
    """spec: and params: blocks populate cfg.spec and cfg.params verbatim."""
    yaml_text = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
params:
  fps: 24
  num_frames: 81
spec:
  model: "wan-ai/Wan2.2-T2V-A14B"
"""
    cfg = load_config(yaml_text)
    assert cfg.spec == {"model": "wan-ai/Wan2.2-T2V-A14B"}
    assert cfg.params == {"fps": 24, "num_frames": 81}


def test_config_spec_preserves_nested_types() -> None:
    """Nested dicts/lists/floats/ints survive without string coercion.

    Bug catch: a `dict[str, str]` annotation would silently stringify
    guidance_scale=5.0 → "5.0" and break hosted's wire request body.
    """
    yaml_text = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
spec:
  params:
    guidance_scale: 5.0
    steps: 30
  graph:
    nodes: [1, 2, 3]
"""
    cfg = load_config(yaml_text)
    assert cfg.spec["params"]["guidance_scale"] == 5.0
    assert isinstance(cfg.spec["params"]["guidance_scale"], float)
    assert cfg.spec["params"]["steps"] == 30
    assert isinstance(cfg.spec["params"]["steps"], int)
    assert cfg.spec["graph"]["nodes"] == [1, 2, 3]


def test_config_spec_and_params_round_trip_via_model_dump() -> None:
    """cfg.model_dump() returns the same spec/params it loaded."""
    yaml_text = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
spec:
  pipeline: "DiffusionPipeline"
  scheduler: "DDIMScheduler"
params:
  seed: 42
"""
    cfg = load_config(yaml_text)
    dumped = cfg.model_dump()
    assert dumped["spec"] == {"pipeline": "DiffusionPipeline", "scheduler": "DDIMScheduler"}
    assert dumped["params"] == {"seed": 42}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/core/test_config.py -v -k "test_config_spec or test_config_params" 2>&1 | tail -15`
Expected: 4 failures with `AttributeError: 'Config' object has no attribute 'spec'` (or `pydantic.ValidationError` for the YAML that includes the new blocks — pydantic v2 rejects unknown keys by default if `extra="forbid"` is set, otherwise the keys are accepted but the attribute is missing). Either way, 4 reds.

- [ ] **Step 3: Implement Config fields**

Edit `src/kinoforge/core/config.py:14` — add `Any` to typing import:

```python
from typing import Any, Literal, Self
```

Edit `src/kinoforge/core/config.py:415` — append two fields after `store: StoreConfig = Field(default_factory=StoreConfig)`:

```python
class Config(BaseModel):
    """Top-level kinoforge configuration.

    Attributes:
        engine: Engine configuration block.
        models: List of model entries.
        compute: Optional compute block (omitted for hosted engines).
        lifecycle_cfg: Top-level lifecycle config (used for hosted engines).
            Loaded from the YAML ``lifecycle:`` key via an alias.
        splitter: Splitter selection block (defaults to heuristic).
        store: Artifact store selector block (defaults to kind='local').
        spec: Engine-interpreted per-job payload (e.g. ``model``/``params``
            for hosted, ``pipeline``/``scheduler`` for diffusers,
            ``graph``/``node_overrides`` for comfyui). Defaults to ``{}``.
            Required keys are enforced by ``engine.validate_spec(job)`` at
            generate-time, not at config load — Config stays engine-agnostic
            so the core-import-ban invariant holds.
        params: Engine-neutral knobs (fps, num_frames, steps, seed, ...)
            that flow into ``GenerationJob.params`` and merge segment-wins
            via ``Segment.params``. Defaults to ``{}``.
    """

    engine: EngineConfig
    models: list[ModelEntry]
    compute: ComputeConfig | None = None
    lifecycle_cfg: LifecycleConfig | None = Field(default=None, alias="lifecycle")
    splitter: SplitterConfig = Field(default_factory=SplitterConfig)
    store: StoreConfig = Field(default_factory=StoreConfig)
    spec: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run pytest tests/core/test_config.py -v 2>&1 | tail -20`
Expected: All `test_config.py` tests pass. Run the full config-test file (not just the new tests) to catch any regression — the cross-field validator should still raise on `lifecycle.idle_timeout >= lifecycle.max_lifetime` etc.

- [ ] **Step 5: Lint + typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config.py`
Expected: ruff/ruff-format/mypy all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/config.py tests/core/test_config.py
git commit -m "feat(config): Config.spec + Config.params pydantic fields (Layer K Task 1)"
```

```json:metadata
{"files": ["src/kinoforge/core/config.py", "tests/core/test_config.py"], "verifyCommand": "pixi run pytest tests/core/test_config.py -v", "acceptanceCriteria": ["YAML without spec/params blocks loads with both as {}", "YAML with spec+params populates cfg.spec/cfg.params verbatim", "nested dict/list/float/int types preserved", "model_dump round-trip equality"]}
```

---

### Task 2: Orchestrator wiring + `ValidationError` teardown

**Goal:** Route `cfg.spec` / `cfg.params` into `GenerateClipStage` and tear down compute when `engine.validate_spec` raises.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:26` (add `ValidationError` to errors import); `src/kinoforge/core/orchestrator.py:596-610` (swap empties + wrap `stage.run()` in try/except)
- Test: `tests/core/test_orchestrator.py` (append 3 new tests at end of file)

**Acceptance Criteria:**
- [ ] `cfg.spec={"k": "v"}` flows into `GenerationJob.spec` (observed via FakeEngine spy on `validate_spec` or `submit`).
- [ ] `cfg.params={"fps": 24}` flows into `GenerationJob.params`.
- [ ] Stage-side mutation of `job.spec` does not mutate `cfg.spec` (the `dict(...)` copy invariant — added in case a future engine mutates its spec).
- [ ] `engine.validate_spec` raising `ValidationError` triggers `resolved_provider.destroy_instance(instance.id)` exactly once when `requires_compute=True`, before the error is re-raised.
- [ ] When `requires_compute=False` (hosted/fal path), `destroy_instance` is not called (no instance was created).
- [ ] On the happy path (validate_spec returns), `destroy_instance` is not called.
- [ ] `pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py` passes.

**Verify:** `pixi run pytest tests/core/test_orchestrator.py -v` → all existing AC tests still pass + 3 new tests pass.

**Steps:**

- [ ] **Step 1: Write failing tests** — append to `tests/core/test_orchestrator.py`:

```python
def test_generate_routes_cfg_spec_into_job_spec(tmp_path: Path) -> None:
    """cfg.spec values reach GenerationJob.spec via stage.base_spec.

    Bug catch: hardcoded base_spec={} at orchestrator.py:605 means
    orchestrator-driven hosted/diffusers/comfyui runs fail validate_spec
    on every config typo for missing required spec keys.
    """
    cfg = _compute_cfg()
    cfg.spec = {"k": "v", "params": {"guidance_scale": 5.0}}
    cfg.params = {"fps": 24}

    captured: dict[str, Any] = {}

    class _SpySpecEngine(FakeEngine):
        def validate_spec(self, job: GenerationJob) -> None:
            captured["spec"] = dict(job.spec)
            captured["params"] = dict(job.params)
            super().validate_spec(job)

    engine = _SpySpecEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys={"k"},
    )

    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="hello", mode="t2v")

    with patch("kinoforge.core.registry.get_engine", return_value=engine):
        generate(
            cfg=cfg,
            request=request,
            store=store,
            run_id="run-spec-routing",
            state_dir=tmp_path,
        )

    assert captured["spec"]["k"] == "v"
    assert captured["spec"]["params"] == {"guidance_scale": 5.0}
    assert captured["params"] == {"fps": 24}


def test_generate_does_not_alias_cfg_spec_into_job_spec(tmp_path: Path) -> None:
    """A mutation of job.spec inside the engine does not bleed into cfg.spec.

    Bug catch: pydantic returns the underlying dict by reference. Without
    a defensive dict() copy at stage construction, an engine that does
    `job.spec["seen"] = True` corrupts the user's cfg.
    """
    cfg = _compute_cfg()
    cfg.spec = {"k": "v"}

    class _MutatingEngine(FakeEngine):
        def validate_spec(self, job: GenerationJob) -> None:
            job.spec["mutated_by_engine"] = True
            super().validate_spec(job)

    engine = _MutatingEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys={"k"},
    )

    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="hi", mode="t2v")

    with patch("kinoforge.core.registry.get_engine", return_value=engine):
        generate(
            cfg=cfg,
            request=request,
            store=store,
            run_id="run-isolation",
            state_dir=tmp_path,
        )

    assert "mutated_by_engine" not in cfg.spec


def test_generate_tears_down_compute_on_validate_spec_failure(
    tmp_path: Path,
) -> None:
    """ValidationError from engine.validate_spec → destroy_instance called once.

    Bug catch: without the teardown wrapper, a typo in spec: that
    triggers ValidationError leaves a RunPod pod billing until reap.
    """
    cfg = _compute_cfg()
    cfg.spec = {}  # empty — fails the required_spec_keys gate

    engine = FakeEngine(
        probe_profile=_probe_profile(),
        declared_flags_map={},
        required_spec_keys={"required_key"},  # cfg.spec is missing this
    )

    destroy_calls: list[str] = []

    class _TrackingProvider(LocalProvider):
        def destroy_instance(self, instance_id: str) -> None:
            destroy_calls.append(instance_id)
            super().destroy_instance(instance_id)

    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt="hi", mode="t2v")
    tracking_provider = _TrackingProvider()

    with (
        patch("kinoforge.core.registry.get_engine", return_value=engine),
        patch(
            "kinoforge.core.registry.get_provider", return_value=tracking_provider
        ),
        pytest.raises(ValidationError),
    ):
        generate(
            cfg=cfg,
            request=request,
            store=store,
            run_id="run-teardown",
            state_dir=tmp_path,
        )

    assert len(destroy_calls) == 1, (
        f"expected exactly one destroy_instance call, saw {destroy_calls!r}"
    )
```

Add at top of the file (with other imports):

```python
from kinoforge.core.errors import CapabilityMismatch, ValidationError
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/core/test_orchestrator.py -v -k "spec or teardown_on_validate" 2>&1 | tail -25`
Expected: 3 failures. The spec-routing test fails because `captured["spec"]` is `{}` (hardcoded). The isolation test fails for the same reason (engine never reaches its spy). The teardown test fails because `destroy_calls` stays empty — `ValidationError` propagates without firing teardown.

- [ ] **Step 3: Implement orchestrator wiring**

Edit `src/kinoforge/core/orchestrator.py:26` — add `ValidationError` to the existing errors import:

```python
from kinoforge.core.errors import CapabilityMismatch, CapacityError, ValidationError
```

Edit `src/kinoforge/core/orchestrator.py:596-610` — replace the existing `with ConcurrentPool() as pool:` block with:

```python
    # ------------------------------------------------------------------
    # Step 9 — run the pipeline stage
    #
    # ``dict(cfg.spec)`` / ``dict(cfg.params)`` defensively copies the
    # pydantic-owned dicts so stage-side mutation cannot leak back into
    # ``cfg``.  A ``ValidationError`` from ``engine.validate_spec`` is
    # treated like ``CapabilityMismatch``: tear down compute before
    # re-raising so a config typo cannot leave a billing pod alive.
    # ------------------------------------------------------------------
    with ConcurrentPool() as pool:
        pool.add(backend, max_in_flight=cfg.lifecycle().max_in_flight)
        stage = GenerateClipStage(
            profile=profile,
            pool=pool,
            store=store,
            run_id=run_id,
            accepted_kinds=accepted_kinds,
            base_params=dict(cfg.params),
            base_spec=dict(cfg.spec),
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run pytest tests/core/test_orchestrator.py -v 2>&1 | tail -30`
Expected: All existing AC tests pass (AC1–AC6 unchanged) plus the 3 new tests pass.

- [ ] **Step 5: Lint + typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py`
Expected: ruff/ruff-format/mypy all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py
git commit -m "feat(core): route cfg.spec/cfg.params + tear down on ValidationError (Layer K Task 2)"
```

```json:metadata
{"files": ["src/kinoforge/core/orchestrator.py", "tests/core/test_orchestrator.py"], "verifyCommand": "pixi run pytest tests/core/test_orchestrator.py -v", "acceptanceCriteria": ["cfg.spec flows into GenerationJob.spec", "cfg.params flows into GenerationJob.params", "dict(...) copy isolates cfg from stage mutations", "ValidationError triggers destroy_instance exactly once when instance exists", "no destroy_instance call on hosted/fal path or happy path"]}
```

---

### Task 3: Strategy precedence regression tests

**Goal:** Pin two invariants that Task 2 indirectly relies on but no existing test exercises with non-empty `base_params`: (a) `Segment.params` segment-wins-merges over a non-empty `base_params`; (b) `strategy.decide`'s `_audio_mode` injection overrides any user-supplied `spec._audio_mode`.

**Files:**
- Test: `tests/core/test_strategy.py` (append 2 new tests at end of file)

**Acceptance Criteria:**
- [ ] With `base_params={"fps": 24, "steps": 30}` and `Segment.params={"steps": 50}`, the merged `Segment.params` becomes `{"fps": 24, "steps": 50}`. Locked down as a separate behavior even if Task 2's e2e covers the happy path.
- [ ] With `spec={"_audio_mode": "wrong"}`, `strategy.decide` produces `job.spec["_audio_mode"]` derived from `profile.supports_joint_audio`, not the user's value.
- [ ] `pixi run pre-commit run --files tests/core/test_strategy.py` passes.

**Verify:** `pixi run pytest tests/core/test_strategy.py -v` → all existing tests + 2 new tests pass.

**Steps:**

- [ ] **Step 1: Write failing tests** — append to `tests/core/test_strategy.py`:

```python
def test_decide_segment_params_merge_over_non_empty_base_params() -> None:
    """Segment-wins merge holds when base_params is non-empty.

    Bug catch: existing tests pass {} as base_params, so a regression
    where _merged_segment was switched to base-wins would only break
    when a real cfg.params is routed in (Layer K).
    """
    from kinoforge.core.interfaces import ModelProfile, Segment
    from kinoforge.core.strategy import decide

    profile = ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )
    segments = [
        Segment(prompt="a", assets=[], params={"steps": 50}),
        Segment(prompt="b", assets=[], params={"fps": 30}),
    ]
    base_params = {"fps": 24, "steps": 30, "seed": 42}

    jobs = decide(profile, segments, base_params, {})

    # Non-native path: one job per segment.
    assert len(jobs) == 2
    assert jobs[0].segments[0].params == {"fps": 24, "steps": 50, "seed": 42}
    assert jobs[1].segments[0].params == {"fps": 30, "steps": 30, "seed": 42}
    # job.params is the unchanged base.
    assert jobs[0].params == base_params
    assert jobs[0].params is not base_params  # defensive copy


def test_decide_strategy_overrides_user_supplied_audio_mode() -> None:
    """A YAML spec._audio_mode never beats strategy.decide's derivation.

    Bug catch: a user routing spec: {_audio_mode: bogus} into the
    orchestrator (Layer K) must not be able to override the engine's
    audio strategy.
    """
    from kinoforge.core.interfaces import ModelProfile, Segment
    from kinoforge.core.strategy import decide

    profile = ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=True,
        supports_joint_audio=False,
    )
    segments = [Segment(prompt="a", assets=[], params={})]
    user_spec = {"_audio_mode": "user-set-wrong-value", "other": "x"}

    jobs = decide(profile, segments, {}, user_spec)

    assert len(jobs) == 1
    assert jobs[0].spec["_audio_mode"] != "user-set-wrong-value"
    # The actual derivation: supports_joint_audio=False → "separate".
    assert jobs[0].spec["_audio_mode"] == "separate"
    assert jobs[0].spec["other"] == "x"
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pixi run pytest tests/core/test_strategy.py -v -k "segment_params_merge or audio_mode" 2>&1 | tail -15`
Expected: 2 PASS. (These are regression locks for behavior already implemented in `strategy.decide` — they should pass on first run. If either fails, `strategy.decide` has drifted from the design and the bug is real.)

If either test fails, do **not** modify `core/strategy.py` — flag the failure to the user. Task 3 is pure lock-down, not behavior change. Discovering a regression here means Task 2's e2e routing would silently produce wrong artifacts.

- [ ] **Step 3: Lint**

Run: `pixi run pre-commit run --files tests/core/test_strategy.py`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/core/test_strategy.py
git commit -m "test(strategy): lock down segment-wins + strategy-authoritative _audio_mode (Layer K Task 3)"
```

```json:metadata
{"files": ["tests/core/test_strategy.py"], "verifyCommand": "pixi run pytest tests/core/test_strategy.py -v", "acceptanceCriteria": ["segment-wins over non-empty base_params", "strategy.decide audio_mode overrides user spec._audio_mode"]}
```

---

### Task 4: End-to-end YAML round-trip test

**Goal:** Lock down the full path — real YAML file → `load_config` → `generate()` → FakeEngine.validate_spec sees `cfg.spec` verbatim → artifact persisted to store — with a dedicated e2e test independent of `tests/core/test_orchestrator.py`'s unit-scale spies.

**Files:**
- Create: `tests/test_e2e_spec_routing.py`

**Acceptance Criteria:**
- [ ] Writes a real YAML file to `tmp_path` and loads it via `load_config(path.read_text())`.
- [ ] `generate()` succeeds + returns an `Artifact` whose `uri` is a readable file in the store root.
- [ ] FakeEngine's `validate_spec` observes `job.spec` containing the YAML's `spec:` block keys.
- [ ] No real network, no real subprocess, no real GPU (FakeEngine + LocalProvider + LocalArtifactStore).
- [ ] `pixi run pre-commit run --files tests/test_e2e_spec_routing.py` passes.

**Verify:** `pixi run pytest tests/test_e2e_spec_routing.py -v` → 1 test passes.

**Steps:**

- [ ] **Step 1: Write failing test** — create `tests/test_e2e_spec_routing.py`:

```python
"""End-to-end test: YAML spec/params blocks round-trip through Orchestrator.

Bug catch: a future refactor that drops the dict(cfg.spec) hand-off at
orchestrator.py would let unit tests in test_orchestrator.py still pass
(they mutate cfg.spec at fixture time) while real CLI users with their
YAML on disk would silently see empty job.spec.  This e2e test exercises
the same path the CLI takes: file → load_config → generate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

# Self-registration of fake + local adapters.
import kinoforge.engines.fake  # noqa: F401
import kinoforge.providers.local  # noqa: F401
import kinoforge.sources.http  # noqa: F401
from kinoforge.core.config import load_config
from kinoforge.core.interfaces import (
    GenerationJob,
    GenerationRequest,
    ModelProfile,
)
from kinoforge.core.orchestrator import generate
from kinoforge.engines.fake import FakeEngine
from kinoforge.stores.local import LocalArtifactStore


_YAML = """\
engine:
  kind: fake
  precision: fp16
models:
  - ref: "https://example.com/fake.safetensors"
    kind: base
    target: checkpoints
compute:
  provider: local
  image: fake:latest
  lifecycle:
    budget: 1.0
params:
  fps: 24
  num_frames: 81
spec:
  model: "wan-ai/Wan2.2-T2V-A14B"
  params:
    guidance_scale: 5.0
"""


def _profile() -> ModelProfile:
    return ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


def test_yaml_spec_params_round_trip_into_job_via_orchestrator(
    tmp_path: Path,
) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(_YAML)

    cfg = load_config(yaml_path.read_text())

    # Sanity: load_config preserved the blocks.
    assert cfg.spec == {
        "model": "wan-ai/Wan2.2-T2V-A14B",
        "params": {"guidance_scale": 5.0},
    }
    assert cfg.params == {"fps": 24, "num_frames": 81}

    seen: dict[str, Any] = {}

    class _Spy(FakeEngine):
        def validate_spec(self, job: GenerationJob) -> None:
            seen["spec"] = dict(job.spec)
            seen["params"] = dict(job.params)
            super().validate_spec(job)

    engine = _Spy(
        probe_profile=_profile(),
        declared_flags_map={},
        required_spec_keys={"model"},
    )

    store_root = tmp_path / "store"
    store = LocalArtifactStore(store_root)
    request = GenerationRequest(prompt="hello world", mode="t2v")

    with patch("kinoforge.core.registry.get_engine", return_value=engine):
        artifact = generate(
            cfg=cfg,
            request=request,
            store=store,
            run_id="e2e-run",
            state_dir=tmp_path,
        )

    assert artifact.uri.startswith(str(store_root)) or artifact.uri.startswith(
        store_root.as_uri()
    )
    # Strategy.decide adds "_audio_mode"; everything else is user-supplied.
    assert seen["spec"]["model"] == "wan-ai/Wan2.2-T2V-A14B"
    assert seen["spec"]["params"] == {"guidance_scale": 5.0}
    assert seen["spec"]["_audio_mode"] == "separate"
    assert seen["params"] == {"fps": 24, "num_frames": 81}
```

- [ ] **Step 2: Run test to verify it fails (before Tasks 1-2 land)**

This task is blocked by Tasks 1+2; if executed in dependency order, the test passes immediately. If executed standalone before Task 1: the `load_config` assertion fails (no `spec`/`params` fields on Config). If executed standalone after Task 1 but before Task 2: the `seen["spec"]` assertion fails (orchestrator still passes `{}`).

Run: `pixi run pytest tests/test_e2e_spec_routing.py -v 2>&1 | tail -15`
Expected after Tasks 1+2: PASS.

- [ ] **Step 3: Lint**

Run: `pixi run pre-commit run --files tests/test_e2e_spec_routing.py`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e_spec_routing.py
git commit -m "test(e2e): YAML spec/params round-trip via Orchestrator (Layer K Task 4)"
```

```json:metadata
{"files": ["tests/test_e2e_spec_routing.py"], "verifyCommand": "pixi run pytest tests/test_e2e_spec_routing.py -v", "acceptanceCriteria": ["real YAML file round-trips through load_config + generate", "FakeEngine.validate_spec observes cfg.spec verbatim", "artifact persisted to LocalArtifactStore"]}
```

---

### Task 5: Example YAML updates + extended example-load tests

**Goal:** Update `hosted.yaml`, `diffusers.yaml`, `wan.yaml`, `fal.yaml` to demonstrate the new `spec:` + `params:` blocks. Lock down their effective shapes in `tests/test_examples.py`.

**Files:**
- Modify: `examples/configs/hosted.yaml` (add `spec: {model, params}` + `params: {fps, num_frames, steps}`; document `engine.hosted.model` vs `spec.model` duplication)
- Modify: `examples/configs/diffusers.yaml` (add `spec: {pipeline, scheduler}` + `params: {fps, num_frames, steps}`)
- Modify: `examples/configs/wan.yaml` (add `spec: {graph, node_overrides}` with a comment about `prompt_node_ids` + `params`)
- Modify: `examples/configs/fal.yaml` (add commented-out optional `spec:` / `params:` example block)
- Untouched: `examples/configs/local-fake.yaml`
- Modify: `tests/test_examples.py` (extend existing tests + add new assertions)

**Acceptance Criteria:**
- [ ] Every existing example YAML still loads via `load_config` (no regression in `test_example_config_loads`).
- [ ] `hosted.yaml`, `diffusers.yaml`, `wan.yaml` produce non-empty `cfg.spec` containing each engine's required keys.
- [ ] `fal.yaml`, `local-fake.yaml` produce empty `cfg.spec` (their `spec:` blocks remain commented-out / absent).
- [ ] `hosted.yaml` YAML comment explicitly documents that `engine.hosted.model` is consumed by `key_base(cfg)` while `spec.model` is consumed by the wire body, and warns the reader to keep them in sync.
- [ ] `pixi run pre-commit run --files examples/configs/{hosted,diffusers,wan,fal}.yaml tests/test_examples.py` passes.

**Verify:** `pixi run pytest tests/test_examples.py -v` → all existing tests pass + 3 new spec-shape tests pass.

**Steps:**

- [ ] **Step 1: Update `examples/configs/hosted.yaml`**

Append after the `lifecycle:` block (preserving the existing engine + models + lifecycle blocks):

```yaml
# --- Layer K: per-job spec & params -----------------------------------------
# spec: is the engine-interpreted wire payload.  HostedAPIBackend.submit reads
# spec.model + spec.params and writes them into the POST body.
#
# NOTE: spec.model and engine.hosted.model serve different purposes and must
# be kept in sync manually until a future layer collapses them:
#   - engine.hosted.model → HostedAPIEngine.key_base(cfg) → CapabilityKey
#     (cache identity + profile-discovery key)
#   - spec.model          → HostedAPIBackend.submit → wire request body
# If you change one, change the other.
spec:
  model: "wan-ai/Wan2.2-T2V-A14B"
  params:
    guidance_scale: 5.0

# params: are engine-neutral knobs every engine honors identically.  Flow into
# GenerationJob.params and merge segment-wins via Segment.params.
params:
  fps: 24
  num_frames: 81
  steps: 30
```

- [ ] **Step 2: Update `examples/configs/diffusers.yaml`**

Append after the existing `compute:` block:

```yaml
# --- Layer K: per-job spec & params -----------------------------------------
# DiffusersBackend.submit reads spec.pipeline + spec.scheduler to construct
# the request body for the headless diffusers server.
spec:
  pipeline: "DiffusionPipeline"
  scheduler: "DDIMScheduler"

params:
  fps: 24
  num_frames: 81
  steps: 30
```

- [ ] **Step 3: Update `examples/configs/wan.yaml`**

Append after the existing `compute:` block (above the `# --- Optional: artifact-store selection` block):

```yaml
# --- Layer K: per-job spec & params -----------------------------------------
# ComfyUIBackend.submit deep-merges spec.node_overrides onto spec.graph, then
# POSTs to /prompt.  asset_node_ids + prompt_node_ids (both optional) route
# Segment.assets and Segment.prompt into the graph; see the Layer J comment
# block below for prompt_node_ids details.
spec:
  graph:
    # Replace with your real ComfyUI workflow JSON. The empty stub here keeps
    # the example load test green; do not run generate() against it.
    nodes: []
  node_overrides: {}
  # asset_node_ids:
  #   init_image: "5"
  # prompt_node_ids:
  #   main: "6"

params:
  fps: 24
  num_frames: 81
  steps: 30
```

- [ ] **Step 4: Update `examples/configs/fal.yaml`**

Append after the existing `lifecycle:` block:

```yaml
# --- Layer K: per-job spec & params (optional for fal.ai) -------------------
# FalEngine.validate_spec does not require any spec keys (prompt comes from
# Segment.prompt via Layer J's resolve_prompt helper).  Uncomment to add
# engine-specific knobs to the queue submission body.
#
# spec:
#   guidance_scale: 5.0
#
# params:
#   fps: 24
#   num_frames: 81
```

- [ ] **Step 5: Write failing example-load tests** — append to `tests/test_examples.py`:

```python
def test_hosted_yaml_has_non_empty_spec() -> None:
    """examples/configs/hosted.yaml ships a spec: block with required keys."""
    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/hosted.yaml").read_text())
    assert "model" in cfg.spec
    assert "params" in cfg.spec
    # Sanity: documented duplication holds in the shipped example.
    assert cfg.spec["model"] == cfg.engine.hosted.model


def test_diffusers_yaml_has_non_empty_spec() -> None:
    """examples/configs/diffusers.yaml ships pipeline+scheduler in spec:."""
    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/diffusers.yaml").read_text())
    assert "pipeline" in cfg.spec
    assert "scheduler" in cfg.spec


def test_wan_yaml_has_non_empty_spec() -> None:
    """examples/configs/wan.yaml ships graph+node_overrides in spec:."""
    from kinoforge.core.config import load_config

    cfg = load_config(Path("examples/configs/wan.yaml").read_text())
    assert "graph" in cfg.spec
    assert "node_overrides" in cfg.spec


def test_fal_and_local_fake_yaml_have_empty_spec() -> None:
    """fal.yaml + local-fake.yaml keep cfg.spec = {} (no required spec keys)."""
    from kinoforge.core.config import load_config

    fal_cfg = load_config(Path("examples/configs/fal.yaml").read_text())
    fake_cfg = load_config(Path("examples/configs/local-fake.yaml").read_text())
    assert fal_cfg.spec == {}
    assert fake_cfg.spec == {}
```

Confirm `Path` is already imported at the top of `tests/test_examples.py`; if not, add `from pathlib import Path` to its imports.

- [ ] **Step 6: Run tests to verify**

Run: `pixi run pytest tests/test_examples.py -v 2>&1 | tail -25`
Expected: all existing tests pass (the 4 example YAMLs load under Task 1's new `spec:`/`params:` fields) + 4 new tests pass.

- [ ] **Step 7: Lint**

Run: `pixi run pre-commit run --files examples/configs/hosted.yaml examples/configs/diffusers.yaml examples/configs/wan.yaml examples/configs/fal.yaml tests/test_examples.py`
Expected: PASS (no trailing whitespace, end-of-file newline, etc.).

- [ ] **Step 8: Commit**

```bash
git add examples/configs/hosted.yaml examples/configs/diffusers.yaml examples/configs/wan.yaml examples/configs/fal.yaml tests/test_examples.py
git commit -m "feat(examples): spec + params blocks on hosted/diffusers/wan/fal YAML (Layer K Task 5)"
```

```json:metadata
{"files": ["examples/configs/hosted.yaml", "examples/configs/diffusers.yaml", "examples/configs/wan.yaml", "examples/configs/fal.yaml", "tests/test_examples.py"], "verifyCommand": "pixi run pytest tests/test_examples.py -v", "acceptanceCriteria": ["all example YAMLs still load", "hosted/diffusers/wan ship non-empty cfg.spec with required keys", "fal/local-fake keep cfg.spec={}", "hosted.yaml comment documents engine.hosted.model vs spec.model duplication"]}
```

---

### Task 6: README + PROGRESS.md + full suite gate

**Goal:** Document the new YAML surface, refresh PROGRESS, and run the entire test suite as the layer-completion gate.

**Files:**
- Modify: `README.md` (add "Per-job spec & params" section + quickstart updates for hosted/diffusers/comfyui)
- Modify: `PROGRESS.md` (Phase 21 entry; mark follow-up #1 closed; restate follow-up #2)

**Acceptance Criteria:**
- [ ] `README.md` contains a new heading "Per-job spec & params" (exact heading text for testability) with a per-engine table.
- [ ] `README.md` quickstart for hosted shows a `spec:` + `params:` block.
- [ ] `PROGRESS.md` "Single next action" section at line 150ish is updated to point at the Layer K merge SHA (placeholder until the merge commit lands).
- [ ] `PROGRESS.md` line ~154 follow-up #1 is removed (or moved to a closed-items section); follow-up #2 (`_artifact_bytes` auth) becomes the new top-of-list.
- [ ] `PROGRESS.md` gains a "Phase 21 — Layer K (spec routing)" subsection under "Post-MVP".
- [ ] `pixi run pre-commit run --all-files` passes.
- [ ] `pixi run pytest` (full suite) passes — including the live opt-in tests gated by `KINOFORGE_LIVE` which stay skipped.

**Verify:** `pixi run pytest 2>&1 | tail -5` → all tests pass with the new layer present; no regression in the existing 693 tests.

**Steps:**

- [ ] **Step 1: README — add the "Per-job spec & params" section**

Append a new top-level section to `README.md` (immediately after the existing engines section, before "## Real providers — fal.ai" — find the actual position with `rg -n "^## " README.md`):

```markdown
## Per-job spec & params

Two top-level YAML blocks supply per-job payload to the engine:

| block | flows into | who reads it | scope |
|---|---|---|---|
| `spec:` | `GenerationJob.spec` | `engine.validate_spec(job)` + `backend.submit(job)` | engine-interpreted (engine-specific shape) |
| `params:` | `GenerationJob.params` | every engine + every `Segment.params` (segment-wins merge) | engine-neutral knobs (fps, num_frames, steps, seed, ...) |

### Required `spec.*` keys per engine

| engine | required `spec.*` keys | notes |
|---|---|---|
| `hosted` | `model`, `params` | `spec.model` is the wire body; keep in sync with `engine.hosted.model` (cache identity) |
| `diffusers` | `pipeline`, `scheduler` | |
| `comfyui` | `graph`, `node_overrides` | optional: `asset_node_ids`, `prompt_node_ids` |
| `fal` | — | prompt comes from `Segment.prompt` via Layer J |

### Note: top-level `params:` vs nested `spec.params:`

Hosted requires a `params` key **inside** `spec:` (the wire body). This is structurally
distinct from top-level `params:` (engine-neutral knobs that flow into
`GenerationJob.params`). There is **no merging** between the two namespaces.

```yaml
params:               # → GenerationJob.params (engine-neutral, segment-wins)
  fps: 24
spec:
  model: "wan-..."
  params:             # → GenerationJob.spec["params"] (hosted wire body)
    guidance_scale: 5.0
```

### On `validate_spec` failure

When the orchestrator detects a `spec:` key missing for the configured engine,
it raises `ValidationError` and tears down any provisioned compute before
re-raising. A typo in your config will not cost idle pod time.
```

- [ ] **Step 2: README — refresh hosted/diffusers/comfyui quickstart snippets**

Locate the existing engine-specific quickstart sections (e.g. "## Engines" subsections for hosted, diffusers, comfyui — confirm exact headings with `rg -n "^### |^## " README.md | head -30`). Update each engine's YAML snippet to include `spec:` and `params:` blocks aligned with the new examples. If the existing README has no such snippets, add a minimal example pointing at `examples/configs/<engine>.yaml`.

- [ ] **Step 3: PROGRESS.md — Phase 21 entry**

Append to `PROGRESS.md` under "## Post-MVP" (after "### Phase 20 — Layer J ..."):

```markdown
### Phase 21 — Layer K (spec & params routing)

- [x] Task 1: Config.spec + Config.params pydantic fields + 4 round-trip tests — commit `<sha>`
- [x] Task 2: Orchestrator routes cfg.spec/cfg.params + ValidationError teardown + 3 tests — commit `<sha>`
- [x] Task 3: Strategy precedence regression locks (segment-wins + _audio_mode authority) — commit `<sha>`
- [x] Task 4: e2e YAML round-trip via Orchestrator — commit `<sha>`
- [x] Task 5: hosted/diffusers/wan/fal example YAMLs + extended example-load tests — commit `<sha>`
- [x] Task 6: README + PROGRESS + full suite gate — commit `<sha>`
- [x] Merge to main via `--no-ff` — merge commit `<sha>`

**Key design decisions:**
- Permissive `dict[str, Any]` (Q3=A): Config stays engine-agnostic, preserves the core-import-ban invariant. `engine.validate_spec` is the sole gate.
- Top-level YAML siblings (Q2=A): `spec:` and `params:` live alongside `engine:` / `models:` / `lifecycle:`, not nested per-engine.
- Teardown on `ValidationError` (Q5=A): orchestrator mirrors the existing `CapabilityMismatch` branch; a config typo does not leak compute.
- `dict(...)` copy at stage construction: defends against any future engine that mutates `job.spec`.

**Hosted YAML ambiguity (carried forward):** `engine.hosted.model` (cache identity) and `spec.model` (wire body) coincide today but are read by different callers. Documented in the example YAML; collapsing them is a Layer-L+ candidate.
```

Update the "Single next action" section near line 150 of `PROGRESS.md` to reference Layer K's merge commit and restate the remaining follow-up:

```markdown
## Single next action
**Layer K merged to main at `<sha>`.** Spec & params routing shipped — hosted/diffusers/comfyui now drive end-to-end through the orchestrator with YAML-supplied `spec:` + `params:` blocks. PROGRESS:154 follow-up #1 closed.

**Pending follow-ups (Layer L candidate):**
- `GenerateClipStage._artifact_bytes` HTTP seam normalization (Phase 19 follow-up; needs Authorization-header support for RunwayML/Pika).
- `engine.hosted.model` ↔ `spec.model` duplication collapse (Layer K hosted YAML ambiguity).
```

The two `<sha>` placeholders are filled in by a backfill commit immediately after the merge, matching the pattern of prior layers (see Layer J's `cc6a2cb`).

- [ ] **Step 4: Run full suite as the layer-completion gate**

Run: `pixi run pytest 2>&1 | tail -10`
Expected: all tests pass. The previous baseline was 693 tests post-Layer-J. New count should be ~705 (693 + 4 config + 3 orchestrator + 2 strategy + 1 e2e + 4 examples — approximate; actual count depends on parametrization).

- [ ] **Step 5: Pre-commit gate**

Run: `pixi run pre-commit run --all-files 2>&1 | tail -15`
Expected: every hook PASS (ruff/format/mypy/trailing-whitespace/end-of-file-fixer/check-merge-conflict/check-added-large-files/check-toml).

- [ ] **Step 6: Commit docs**

```bash
git add README.md PROGRESS.md
git commit -m "docs: Layer K spec/params routing — README + PROGRESS Phase 21 entry (Layer K Task 6)"
```

```json:metadata
{"files": ["README.md", "PROGRESS.md"], "verifyCommand": "pixi run pytest && pixi run pre-commit run --all-files", "acceptanceCriteria": ["README has Per-job spec & params section", "README hosted/diffusers/comfyui quickstarts show spec+params", "PROGRESS Phase 21 entry present", "Single next action updated to point at Layer K", "follow-up #1 closed; #2 restated as Layer L candidate", "full pytest suite passes", "pre-commit all-files passes"]}
```

---

## Self-review

### Spec coverage

| spec §  | covered by |
|---------|------------|
| §1 Problem (hardcoded `{}`) | Task 2 (the swap) + Task 4 (e2e proves the seam) |
| §2 Goal (route YAML→Job) | Tasks 1+2 |
| §2 Non-goals (auth headers, request-level spec, CLI flags, per-segment spec) | not present in any task |
| §3 Q1 scope = spec routing only | Task 2 (no auth-header work) |
| §3 Q2 top-level YAML | Task 1 (Config fields + tests) + Task 5 (example YAMLs) |
| §3 Q3 permissive dict | Task 1 (`dict[str, Any]`) — no per-engine schema added |
| §3 Q4 plain pydantic fields | Task 1 (no accessor methods) |
| §3 Q5 teardown on ValidationError | Task 2 (try/except + destroy_instance) |
| §4 YAML schema + per-engine required keys + top-level vs nested params clarifier | Task 5 (examples) + Task 6 (README) |
| §5 Config model change | Task 1 |
| §6 Orchestrator wiring + teardown | Task 2 |
| §7 Testing matrix (config / orchestrator / strategy / examples / e2e) | Tasks 1, 2, 3, 4, 5 in order |
| §8 Hosted YAML ambiguity documented | Task 5 (hosted.yaml comment) + Task 6 (README + PROGRESS) |
| §9 Architectural invariants preserved | covered passively by the `dict[str, Any]` choice in Task 1 — no test explicitly asserts core-import-ban, but `tests/test_core_invariant.py` from Phase 9 Task 24 already does, and the full-suite run in Task 6 includes it |
| §10 Out-of-scope items | none promoted into a task |

No gaps.

### Placeholder scan

- No "TBD"/"TODO"/"fill in later" in any task body.
- The two `<sha>` placeholders in Task 6 are real placeholders intentionally — they get filled by a backfill commit after `git merge --no-ff`, matching the pattern of prior layers (`Phase 20` uses `<sha>` in the same shape; backfilled at `ba420d8` for Layer J).
- "Replace with your real ComfyUI workflow JSON" inside the YAML edit (Step 3 of Task 5) is a user-facing comment, not a plan placeholder — comfyui's `graph` is workflow-specific by design; the empty stub is documented and the test only requires the key to exist.

### Type consistency

- `cfg.spec` and `cfg.params` are `dict[str, Any]` in Task 1 and used as such in Tasks 2/3/4/5.
- `GenerationJob.spec` is `dict` (untyped) in `core/interfaces.py:243` — `dict(cfg.spec)` keeps it `dict[str, Any]`-compatible at runtime; the existing `# type: ignore[type-arg]` on `GenerationJob.spec` covers the mypy gap, no new ignores needed.
- `ValidationError` import path is `kinoforge.core.errors.ValidationError` (verified at `src/kinoforge/core/errors.py:28`); used consistently in Tasks 2 and (indirectly) in the e2e test.
- FakeEngine constructor signature `(probe_profile, declared_flags_map, required_spec_keys)` matches the existing definition at `src/kinoforge/engines/fake/__init__.py:151` and is used consistently across Tasks 2 and 4.

No issues.

### Scope check

Six tasks, each commit-sized + independently verifiable. Dependency chain:
- Task 1 (Config) → no deps.
- Task 2 (Orchestrator wiring) → blocked by Task 1.
- Task 3 (Strategy regression locks) → no deps; can run anytime.
- Task 4 (e2e) → blocked by Tasks 1+2.
- Task 5 (Examples + tests) → blocked by Task 1 (else YAMLs fail to load).
- Task 6 (Docs + full suite) → blocked by Tasks 1–5.

No task touches more than one concern. Plan ships as one PR via `--no-ff` merge per repo convention.
