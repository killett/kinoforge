# Layer 8 — `model_identity` ABC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `model_identity(cfg) -> str` `@abstractmethod` to both `GenerationEngine` and `ImageEngine`, implement it on every engine using each engine's native cfg field, wire orchestrator to thread the result into `LocalOutputSink.publish`, and close the PROGRESS Phase 46/47 carry-forward where fal and ComfyUI filenames render as `unknown`.

**Architecture:** Display-only ABC method (independent of `key_base` cache identity). Each engine reads the cfg field it already interprets natively (hosted/diffusers → `spec.model`, fal → `engine.fal.endpoint`, comfyui → `models[base].ref` filename stem, bedrock → `engine.bedrock_video.model_id`). Orchestrator emits one `WARNING` per `deploy()` per stage when the engine returns `""`. `LocalOutputSink`'s existing `"unknown"` fallback covers empty returns — sink contract unchanged.

**Tech Stack:** Python 3.13, pydantic v2, pytest, pixi.

**Spec:** `docs/superpowers/specs/2026-06-08-model-identity-abc-design.md` (`a539b8c` + `8d17123`).

---

## File map

**Create:**
- `tests/core/test_engine_abc_contract.py` — cross-engine ABC contract test (both ABCs).
- `tests/integration/test_no_unknown_slug_for_example_configs.py` — regression lock walking `examples/configs/`.

**Modify:**
- `src/kinoforge/core/interfaces.py` — add `model_identity` abstractmethod to `GenerationEngine` (after line 514) and to `ImageEngine` (after line 329).
- `src/kinoforge/engines/hosted/__init__.py` — add `model_identity` to `HostedAPIEngine` (after line 673).
- `src/kinoforge/engines/diffusers/__init__.py` — add `model_identity` to `DiffusersEngine`.
- `src/kinoforge/engines/fal/__init__.py` — add `model_identity` to `FalEngine`.
- `src/kinoforge/engines/comfyui/__init__.py` — add `model_identity` to `ComfyUIEngine`.
- `src/kinoforge/engines/bedrock_video/__init__.py` — add `model_identity` to `BedrockVideoEngine`.
- `src/kinoforge/engines/fake/__init__.py` — add `model_identity` to `FakeEngine` (after line 231).
- `src/kinoforge/core/remote_backend.py` — add concrete `model_identity` to `RemoteSubmitPollEngine` (after line 374) so `ReplicateEngine` / `RunwayEngine` inherit it for free.
- `src/kinoforge/image_engines/replicate/__init__.py` — add `model_identity` to `ReplicateImageEngine`.
- `src/kinoforge/image_engines/fal/__init__.py` — add `model_identity` to `FalImageEngine`.
- `src/kinoforge/image_engines/fake/__init__.py` — add `model_identity` to `FakeImageEngine`.
- `src/kinoforge/core/orchestrator.py` — replace `_model = ...` at line 1110-1111 with `engine.model_identity(...)` call + WARNING; replace `_kf_model = ...` at line 1058-1059 likewise.
- `tests/core/test_profiles.py` — add `model_identity` stub to `_FakeEngine` at line 64.
- `tests/core/test_interfaces.py` — add `model_identity` stub to `_NonOverriding` at line 112.
- `tests/engines/test_hosted.py` / `test_diffusers.py` / `test_fal.py` / `test_comfyui.py` / `test_bedrock_video.py` / `test_fake.py` — add per-engine `model_identity` unit tests (2 each: native source + empty fallback).
- `tests/image_engines/test_replicate.py` / `test_fal.py` / `test_fake.py` — add per-image-engine `model_identity` unit tests (2 each).
- `tests/core/test_orchestrator.py` — add 3 wiring tests (clip threads, keyframe threads, WARNING on empty).
- `PROGRESS.md` — append Phase 48 closeout block.
- `README.md` — short paragraph in the existing Output section (if present) noting the filename slug now reflects engine-native model identity.

---

## Task 0: ABC additions + test-local stubs

**Goal:** Add `@abstractmethod model_identity(cfg)` to both `GenerationEngine` and `ImageEngine` ABCs, and patch every in-repo subclass (source + test-local) with a working impl in the same commit so the test suite continues to instantiate them. Concrete impls land here so the ABC change is atomic — every engine subclass gets its `model_identity` before any test instantiates one.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py:515` (after `validate_spec` on `GenerationEngine`)
- Modify: `src/kinoforge/core/interfaces.py:329` (after `validate_spec` on `ImageEngine`)
- Modify: `src/kinoforge/engines/hosted/__init__.py` (after line 673)
- Modify: `src/kinoforge/engines/diffusers/__init__.py` (after the existing `validate_spec`)
- Modify: `src/kinoforge/engines/fal/__init__.py` (after the existing `validate_spec`)
- Modify: `src/kinoforge/engines/comfyui/__init__.py` (after the existing `validate_spec`)
- Modify: `src/kinoforge/engines/bedrock_video/__init__.py` (after the existing `validate_spec`)
- Modify: `src/kinoforge/engines/fake/__init__.py` (after line 231)
- Modify: `src/kinoforge/core/remote_backend.py:375` (after `validate_spec` on `RemoteSubmitPollEngine`)
- Modify: `src/kinoforge/image_engines/replicate/__init__.py`
- Modify: `src/kinoforge/image_engines/fal/__init__.py`
- Modify: `src/kinoforge/image_engines/fake/__init__.py`
- Modify: `tests/core/test_profiles.py:64` (add stub to `_FakeEngine`)
- Modify: `tests/core/test_interfaces.py:112` (add stub to `_NonOverriding` inside the test function)

**Acceptance Criteria:**
- [ ] `GenerationEngine.model_identity` is `@abstractmethod` returning `str`.
- [ ] `ImageEngine.model_identity` is `@abstractmethod` returning `str`.
- [ ] All 7 `GenerationEngine` concrete subclasses provide an impl: `HostedAPIEngine`, `DiffusersEngine`, `FalEngine`, `ComfyUIEngine`, `BedrockVideoEngine`, `FakeEngine`, `RemoteSubmitPollEngine`.
- [ ] All 3 `ImageEngine` concrete subclasses provide an impl: `ReplicateImageEngine`, `FalImageEngine`, `FakeImageEngine`.
- [ ] Both test-local subclasses (`_FakeEngine`, `_NonOverriding`) provide an impl.
- [ ] `pixi run test` passes (no `TypeError: Can't instantiate abstract class …` anywhere).

**Verify:** `pixi run test -q` → green, no regressions in existing 700+ tests.

**Steps:**

- [ ] **Step 1: Add abstractmethod to `GenerationEngine`**

Open `src/kinoforge/core/interfaces.py`. After the `validate_spec` method body (around line 514, before `extract_last_frame` on line 516), insert:

```python
    @abstractmethod
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return a human-readable model slug for sink filenames.

        Display-only; independent of CapabilityKey / cache identity (see
        ``HostedAPIEngine.key_base``).  Engines return the most specific
        human-grep-able surface they natively interpret: hosted ->
        ``cfg["spec"]["model"]``, fal -> ``cfg["engine"]["fal"]["endpoint"]``,
        comfyui -> filename stem of the ``kind == "base"`` entry in
        ``cfg["models"]``, etc.

        ``cfg`` is the same dict shape the engine receives in ``backend()``
        and ``validate_spec()``.  For the keyframe path that is the keyframe
        sub-cfg the stage feeds into the image engine, not the top-level
        Config.

        MUST NOT raise on a missing / empty source — return ``""`` instead.
        The orchestrator logs a single WARNING and the sink falls back to
        the literal ``"unknown"``.

        Args:
            cfg: Runtime configuration dict.

        Returns:
            Engine-native raw slug (slugified downstream by the sink) or
            ``""`` when the underlying field is absent / empty.
        """
        ...
```

- [ ] **Step 2: Add abstractmethod to `ImageEngine`**

Same file, after `validate_spec` on `ImageEngine` (around line 329):

```python
    @abstractmethod
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return a human-readable model slug for keyframe sink filenames.

        See :meth:`GenerationEngine.model_identity` for the full contract —
        identical semantics; declared separately because ``ImageEngine`` and
        ``GenerationEngine`` do not share a parent ABC today.
        """
        ...
```

- [ ] **Step 3: Add concrete impls (10 engines)**

Add the following method to each engine class. Insert after the existing `validate_spec` method body on each subclass. Each impl is 1-4 lines.

`src/kinoforge/engines/hosted/__init__.py` (after line 673, inside `HostedAPIEngine`):

```python
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Hosted identity is the wire-body model slug at ``spec.model``."""
        spec = cfg.get("spec", {})
        return str(spec.get("model", "") or "") if isinstance(spec, dict) else ""
```

`src/kinoforge/engines/diffusers/__init__.py` (inside `DiffusersEngine`):

```python
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Diffusers identity matches the hosted pattern — ``spec.model``."""
        spec = cfg.get("spec", {})
        return str(spec.get("model", "") or "") if isinstance(spec, dict) else ""
```

`src/kinoforge/engines/fal/__init__.py` (inside `FalEngine`):

```python
    def model_identity(self, cfg: dict[str, object]) -> str:
        """fal identity is the queue endpoint (e.g. ``fal-ai/wan-t2v``)."""
        engine_block = cfg.get("engine", {})
        if not isinstance(engine_block, dict):
            return ""
        fal_block = engine_block.get("fal", {})
        if not isinstance(fal_block, dict):
            return ""
        return str(fal_block.get("endpoint", "") or "")
```

`src/kinoforge/engines/comfyui/__init__.py` (inside `ComfyUIEngine`):

```python
    def model_identity(self, cfg: dict[str, object]) -> str:
        """ComfyUI identity is the filename stem of the kind=base model entry."""
        models = cfg.get("models", []) or []
        if not isinstance(models, list):
            return ""
        for entry in models:
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") == "base":
                ref = str(entry.get("ref", "") or "")
                if not ref:
                    return ""
                tail = ref.rsplit(":", 1)[-1] if ":" in ref else ref
                return tail.rsplit(".", 1)[0] if "." in tail else tail
        return ""
```

`src/kinoforge/engines/bedrock_video/__init__.py` (inside `BedrockVideoEngine`):

```python
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Bedrock identity is the Bedrock model id (e.g. ``luma.ray-v2:0``)."""
        engine_block = cfg.get("engine", {})
        if not isinstance(engine_block, dict):
            return ""
        bv_block = engine_block.get("bedrock_video", {})
        if not isinstance(bv_block, dict):
            return ""
        return str(bv_block.get("model_id", "") or "")
```

`src/kinoforge/engines/fake/__init__.py` (after line 231, inside `FakeEngine`):

```python
    def model_identity(self, cfg: dict[str, object]) -> str:
        """FakeEngine reads ``spec.model`` so offline tests can pin a slug."""
        spec = cfg.get("spec", {})
        return str(spec.get("model", "") or "") if isinstance(spec, dict) else ""
```

`src/kinoforge/core/remote_backend.py` (after line 374, inside `RemoteSubmitPollEngine`):

```python
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Remote-submit-poll engines (Replicate, Runway, Luma) read ``spec.model``.

        Subclasses may override if their identity surface diverges.
        """
        spec = cfg.get("spec", {})
        return str(spec.get("model", "") or "") if isinstance(spec, dict) else ""
```

`src/kinoforge/image_engines/replicate/__init__.py` (inside `ReplicateImageEngine`):

```python
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Replicate image identity is the prediction model slug at ``spec.model``."""
        spec = cfg.get("spec", {})
        return str(spec.get("model", "") or "") if isinstance(spec, dict) else ""
```

`src/kinoforge/image_engines/fal/__init__.py` (inside `FalImageEngine`):

```python
    def model_identity(self, cfg: dict[str, object]) -> str:
        """fal image identity is the queue endpoint."""
        engine_block = cfg.get("engine", {})
        if not isinstance(engine_block, dict):
            return ""
        fal_block = engine_block.get("fal", {})
        if not isinstance(fal_block, dict):
            return ""
        return str(fal_block.get("endpoint", "") or "")
```

`src/kinoforge/image_engines/fake/__init__.py` (inside `FakeImageEngine`):

```python
    def model_identity(self, cfg: dict[str, object]) -> str:
        """FakeImageEngine reads ``spec.model`` for test pins."""
        spec = cfg.get("spec", {})
        return str(spec.get("model", "") or "") if isinstance(spec, dict) else ""
```

- [ ] **Step 4: Patch test-local subclasses**

`tests/core/test_profiles.py` — inside `_FakeEngine` (after line 91, after the existing `validate_spec`):

```python
    def model_identity(self, cfg: dict[str, object]) -> str:  # noqa: D102
        return ""
```

`tests/core/test_interfaces.py` — inside `_NonOverriding` (after line 130, after the existing `validate_spec`):

```python
        def model_identity(self, cfg):  # noqa: ANN001
            return ""
```

- [ ] **Step 5: Run the full suite**

Run: `pixi run test -q`
Expected: green; no `TypeError: Can't instantiate abstract class …`. All pre-existing tests pass; no new tests yet.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/interfaces.py \
        src/kinoforge/core/remote_backend.py \
        src/kinoforge/engines/hosted/__init__.py \
        src/kinoforge/engines/diffusers/__init__.py \
        src/kinoforge/engines/fal/__init__.py \
        src/kinoforge/engines/comfyui/__init__.py \
        src/kinoforge/engines/bedrock_video/__init__.py \
        src/kinoforge/engines/fake/__init__.py \
        src/kinoforge/image_engines/replicate/__init__.py \
        src/kinoforge/image_engines/fal/__init__.py \
        src/kinoforge/image_engines/fake/__init__.py \
        tests/core/test_profiles.py \
        tests/core/test_interfaces.py
pixi run pre-commit run --files <staged files>
git commit -m "feat(engines): add model_identity ABC + per-engine impls (Layer 8 T0)"
```

---

## Task 1: Per-engine unit tests (`GenerationEngine` subclasses + `ImageEngine` subclasses) + cross-engine ABC contract test

**Goal:** Add 2 unit tests per concrete engine impl (happy path + empty fallback), plus one cross-engine contract test that iterates every registered engine and asserts the method shape. Lock the contract so a future engine cannot ship with a broken `model_identity`.

**Files:**
- Modify: `tests/engines/test_hosted.py`
- Modify: `tests/engines/test_diffusers.py`
- Modify: `tests/engines/test_fal.py`
- Modify: `tests/engines/test_comfyui.py`
- Modify: `tests/engines/test_bedrock_video.py`
- Modify: `tests/engines/test_fake.py`
- Modify: `tests/image_engines/test_replicate.py`
- Modify: `tests/image_engines/test_fal.py`
- Modify: `tests/image_engines/test_fake.py`
- Create: `tests/core/test_engine_abc_contract.py`

**Acceptance Criteria:**
- [ ] Every engine has 2 tests: native source returns expected slug; empty/missing returns `""`.
- [ ] `tests/core/test_engine_abc_contract.py` iterates the engine registry, instantiates each, asserts `model_identity({}) == ""` returns without raising.
- [ ] All tests fail before Task 0's impls land (sanity check: tests target the new method); all pass after.

**Verify:** `pixi run test tests/engines tests/image_engines tests/core/test_engine_abc_contract.py -v` → green; 20 new test cases (2 × 9 engines + 1 × ABC contract iterates both registries).

**Steps:**

- [ ] **Step 1: Write the per-engine happy + empty tests**

For each engine, append to its test module. Example for `tests/engines/test_hosted.py`:

```python
def test_hosted_model_identity_reads_spec_model() -> None:
    """HostedAPIEngine returns cfg.spec.model verbatim.

    Bug catch: reads from wrong field (e.g. engine.hosted.model after Layer M)
    or strips chars the sink would have kept.
    """
    eng = HostedAPIEngine(creds=_StubCreds(), http_get=lambda url, headers: b"OK")
    assert eng.model_identity({"spec": {"model": "bytedance/seedance-1-lite"}}) == (
        "bytedance/seedance-1-lite"
    )


def test_hosted_model_identity_empty_when_spec_model_missing() -> None:
    """Missing spec.model returns "" per ABC contract.

    Bug catch: raises ConfigError (confusing display contract with key_base
    cache contract), or returns the literal string "None".
    """
    eng = HostedAPIEngine(creds=_StubCreds(), http_get=lambda url, headers: b"OK")
    assert eng.model_identity({"spec": {}}) == ""
    assert eng.model_identity({}) == ""
```

Repeat the same shape for each engine, swapping the cfg fixture for the engine's native source:

- `diffusers` — `{"spec": {"model": "Wan-AI/Wan2.2-T2V-A14B-Diffusers"}}` → `"Wan-AI/Wan2.2-T2V-A14B-Diffusers"`.
- `fal` — `{"engine": {"fal": {"endpoint": "fal-ai/wan-t2v"}}}` → `"fal-ai/wan-t2v"`.
- `comfyui` — `{"models": [{"kind": "base", "ref": "hf:Kijai/WanVideo_comfy:Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors"}]}` → `"Wan2_1-I2V-14B-480P_fp8_e4m3fn"`.
- `bedrock_video` — `{"engine": {"bedrock_video": {"model_id": "luma.ray-v2:0"}}}` → `"luma.ray-v2:0"`.
- `fake` — `{"spec": {"model": "fake-model"}}` → `"fake-model"`.
- `replicate` (ImageEngine) — `{"spec": {"model": "black-forest-labs/flux-1.1-pro"}}` → `"black-forest-labs/flux-1.1-pro"`.
- `fal` (ImageEngine) — `{"engine": {"fal": {"endpoint": "fal-ai/flux/dev"}}}` → `"fal-ai/flux/dev"`.
- `fake` (ImageEngine) — `{"spec": {"model": "fake-image"}}` → `"fake-image"`.

For each engine, the empty-test asserts `model_identity({}) == ""` AND `model_identity({"spec": {}}) == ""` (or analogous engine-block-missing case).

Optional ComfyUI extra case (recommended):

```python
def test_comfyui_model_identity_strips_hf_repo_and_extension() -> None:
    """ComfyUI extracts filename stem from HF ref form 'hf:repo:path.safetensors'."""
    eng = _build_comfyui_engine_for_tests()
    cfg = {"models": [
        {"kind": "base", "ref": "hf:Kijai/WanVideo_comfy:Wan2_1-I2V-14B-480P_fp8_e4m3fn.safetensors"},
    ]}
    assert eng.model_identity(cfg) == "Wan2_1-I2V-14B-480P_fp8_e4m3fn"


def test_comfyui_model_identity_handles_no_base_entry() -> None:
    """Missing base entry returns '' (load-time validation usually prevents this
    but model_identity must not raise)."""
    eng = _build_comfyui_engine_for_tests()
    cfg = {"models": [{"kind": "lora", "ref": "hf:x/y:z.safetensors"}]}
    assert eng.model_identity(cfg) == ""
```

- [ ] **Step 2: Write the cross-engine ABC contract test**

Create `tests/core/test_engine_abc_contract.py`:

```python
"""Cross-engine ABC contract test for ``model_identity``.

Layer 8.  Iterates every registered GenerationEngine and ImageEngine, asserts
each exposes ``model_identity`` and that calling it on an empty cfg returns
``""`` without raising.  Bug this catches: a new engine ships with a
``model_identity`` that raises on missing field (violating §6 of the spec) or
returns a non-str.
"""

from __future__ import annotations

import pytest

# Trigger self-registration of every engine.
import kinoforge._adapters  # noqa: F401
from kinoforge.core import registry


def _all_video_engine_factories() -> list:
    return list(registry._ENGINES.values())  # noqa: SLF001 — test introspection.


def _all_image_engine_factories() -> list:
    return list(registry._IMAGE_ENGINES.values())  # noqa: SLF001 — test introspection.


@pytest.mark.parametrize(
    "factory",
    _all_video_engine_factories(),
    ids=lambda f: getattr(f, "__name__", str(f)),
)
def test_every_video_engine_implements_model_identity(factory) -> None:  # noqa: ANN001
    eng = factory()
    assert hasattr(eng, "model_identity")
    assert callable(eng.model_identity)
    out = eng.model_identity({})
    assert isinstance(out, str), f"{type(eng).__name__}.model_identity returned {type(out)}"
    assert out == "", f"{type(eng).__name__}.model_identity({{}}) returned {out!r}"


@pytest.mark.parametrize(
    "factory",
    _all_image_engine_factories(),
    ids=lambda f: getattr(f, "__name__", str(f)),
)
def test_every_image_engine_implements_model_identity(factory) -> None:  # noqa: ANN001
    eng = factory()
    assert hasattr(eng, "model_identity")
    assert callable(eng.model_identity)
    out = eng.model_identity({})
    assert isinstance(out, str), f"{type(eng).__name__}.model_identity returned {type(out)}"
    assert out == "", f"{type(eng).__name__}.model_identity({{}}) returned {out!r}"
```

Note: if `registry._IMAGE_ENGINES` is named differently in `core/registry.py`, the executor must read the actual attribute name and update the test accordingly (use `rg "register_image_engine|_IMAGE_ENGINES|_image_engines" src/kinoforge/core/registry.py` to confirm).

- [ ] **Step 3: Run the tests, confirm green**

Run: `pixi run test tests/engines tests/image_engines tests/core/test_engine_abc_contract.py -v`
Expected: all new tests pass. ~20 test cases added.

- [ ] **Step 4: Commit**

```bash
git add tests/engines/ tests/image_engines/ tests/core/test_engine_abc_contract.py
pixi run pre-commit run --files <staged files>
git commit -m "test(engines): per-engine + ABC contract tests for model_identity (Layer 8 T1)"
```

---

## Task 2: Orchestrator clip-stage wiring

**Goal:** Replace the hard-coded `_model = cfg.spec.get("model", "")` in the clip-stage section of the orchestrator with a call to `session.engine.model_identity(cfg.model_dump())`. Log one WARNING per `deploy()` when the engine returns `""`. Cover with 2 tests (happy thread + WARNING on empty).

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:1108-1126`
- Modify: `tests/core/test_orchestrator.py`

**Acceptance Criteria:**
- [ ] `_model` is sourced from `session.engine.model_identity(cfg.model_dump())`.
- [ ] When the engine returns `""`, exactly one `WARNING` record is emitted naming the engine and noting `"will render filename slug as 'unknown'"`.
- [ ] `_model` is threaded into `GenerateClipStage(... model=_model)`.
- [ ] The 2 new orchestrator tests pass.

**Verify:** `pixi run test tests/core/test_orchestrator.py -v -k model_identity` → both new tests pass; `pixi run test -q` shows no regressions.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_orchestrator.py`:

```python
def test_orchestrator_threads_engine_model_identity_into_clip_stage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Orchestrator must source the sink's model slug from engine.model_identity,
    not from cfg.spec.model directly.

    Bug catch: orchestrator keeps the old direct read, so engines whose identity
    lives outside spec (fal, comfyui, bedrock) regress to ``unknown`` after this
    layer ships.
    """
    # Use FakeEngine + spy sink; build a config whose spec.model is empty but
    # whose engine block carries identity (forces the engine.model_identity
    # path to be the active one).
    captured: dict[str, object] = {}

    class _SpySink:
        def publish(self, data: bytes, **kwargs: object) -> str:
            captured.update(kwargs)
            return "/tmp/published.mp4"

    cfg = _minimal_cfg_for_fake_engine(spec_model="fake-pinned-model")
    # ... wire FakeEngine + SpySink + run deploy() + generate() ...
    assert captured.get("model") == "fake-pinned-model"


def test_orchestrator_warns_when_engine_model_identity_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the engine returns empty model identity, orchestrator logs WARNING
    naming the engine and threads None to the stage.

    Bug catch: silent fallback (no log line) means future ``unknown`` filenames
    surface only in user-visible artifacts, not in CI / smoke output.
    """
    caplog.set_level(logging.WARNING)
    cfg = _minimal_cfg_for_fake_engine(spec_model="")  # forces empty identity
    # ... wire + deploy() + generate() ...
    warnings = [r for r in caplog.records if "model identity" in r.message]
    assert len(warnings) == 1
    assert "fake" in warnings[0].message  # engine name appears in the log line
```

Helper `_minimal_cfg_for_fake_engine` may already exist or need a small addition near the existing test fixtures. Reuse the pattern from existing `test_orchestrator.py` cases that exercise the FakeEngine path.

Run: `pixi run test tests/core/test_orchestrator.py -v -k model_identity`
Expected: FAIL (`KeyError` / `AssertionError`) because the orchestrator still uses the old code path.

- [ ] **Step 2: Replace the orchestrator clip-stage wiring**

Open `src/kinoforge/core/orchestrator.py`. Replace lines 1108-1126:

```python
        # ------------------------------------------------------------------
        # Build stage list from cfg-block presence (GenerateClipStage only
        # here — KeyframeStage already ran above when keyframe was set).
        # ------------------------------------------------------------------
        # Layer 8: provider + model for the OutputSink filename schema.
        # Provider = registered engine name; model = engine.model_identity(cfg)
        # so non-hosted engines (fal, comfyui, bedrock) get a real slug instead
        # of "unknown".  Empty return -> WARNING + None -> sink renders
        # "unknown".
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
        stages: list[Stage] = [
            GenerateClipStage(
                profile=session.profile,
                pool=session.pool,
                store=store,
                run_id=run_id,
                accepted_kinds=accepted_kinds,
                base_params=dict(cfg.params),
                base_spec=dict(cfg.spec),
                engine=session.engine,
                segments=prompt_segments,
                sink=sink,
                provider=_provider,
                model=_model,
            )
        ]
```

- [ ] **Step 3: Re-run the new tests, confirm green**

Run: `pixi run test tests/core/test_orchestrator.py -v -k model_identity`
Expected: PASS.

- [ ] **Step 4: Run the full suite, confirm no regressions**

Run: `pixi run test -q`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py
pixi run pre-commit run --files <staged files>
git commit -m "feat(orchestrator): clip stage reads engine.model_identity (Layer 8 T2)"
```

---

## Task 3: Orchestrator keyframe-stage wiring

**Goal:** Mirror Task 2's pattern for the keyframe path: read `resolved_image_engine.model_identity(...)` instead of `cfg.keyframe.spec.model`. Emit one WARNING per `deploy()` when empty. Cover with 1 test (keyframe thread).

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:1053-1071`
- Modify: `tests/core/test_orchestrator.py`

**Acceptance Criteria:**
- [ ] `_kf_model` is sourced from `resolved_image_engine.model_identity(...)`.
- [ ] Empty return → one WARNING naming the image engine.
- [ ] `KeyframeStage(..., model=_kf_model)` threads the value.
- [ ] New test asserts the keyframe path threads the image engine's identity into `sink.publish` for the keyframe artifact.

**Verify:** `pixi run test tests/core/test_orchestrator.py -v -k keyframe_model_identity` → pass.

**Steps:**

- [ ] **Step 1: Write the failing test**

Append to `tests/core/test_orchestrator.py`:

```python
def test_orchestrator_threads_image_engine_model_identity_into_keyframe_stage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keyframe path mirrors clip path: image_engine.model_identity drives the
    keyframe filename slug.

    Bug catch: keyframe stage keeps reading cfg.keyframe.spec.model directly,
    so a future image engine whose identity lives elsewhere (e.g. Luma Agents
    UNI-1) regresses to ``unknown``.
    """
    captured: list[dict[str, object]] = []

    class _SpySink:
        def publish(self, data: bytes, **kwargs: object) -> str:
            captured.append(dict(kwargs))
            return "/tmp/keyframe.png"

    cfg = _minimal_cfg_with_keyframe_for_fake_image_engine(spec_model="fake-img-model")
    # ... wire FakeImageEngine + FakeEngine + SpySink + run deploy() + generate() ...

    keyframe_publish = next(c for c in captured if c.get("kind", "").startswith("keyframe"))
    assert keyframe_publish["model"] == "fake-img-model"
```

Run: `pixi run test tests/core/test_orchestrator.py -v -k keyframe_model_identity`
Expected: FAIL.

- [ ] **Step 2: Replace the keyframe wiring**

Open `src/kinoforge/core/orchestrator.py`. Around lines 1053-1071, replace:

```python
        if cfg.keyframe is not None:
            # Layer 8 — keyframe stage now reads identity from the image engine,
            # symmetric with the clip stage in Task 2.
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
            try:
                state = KeyframeStage(
                    keyframe_cfg=cfg.keyframe,
                    image_engine=resolved_image_engine,  # type: ignore[arg-type]
                    image_backend=image_backend,  # type: ignore[arg-type]
                    image_profile=image_prof,  # type: ignore[arg-type]
                    store=store,
                    run_id=run_id,
                    sink=sink,
                    provider=_kf_provider,
                    model=_kf_model,
                ).run(state)
            except ValidationError:
                # … existing teardown branch unchanged …
```

Keep the existing `except ValidationError` block unchanged.

- [ ] **Step 3: Re-run, confirm green**

Run: `pixi run test tests/core/test_orchestrator.py -v -k keyframe_model_identity`
Expected: PASS.

- [ ] **Step 4: Full suite gate**

Run: `pixi run test -q`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py
pixi run pre-commit run --files <staged files>
git commit -m "feat(orchestrator): keyframe stage reads image_engine.model_identity (Layer 8 T3)"
```

---

## Task 4: Integration regression lock — no `unknown` slug for example YAMLs

**Goal:** Walk every YAML in `examples/configs/` (skipping `local-fake.yaml` and any other intentional fake), resolve the engine, call `model_identity(cfg.model_dump())`, assert the result is non-empty. Locks the spec invariant that all shipped example configs produce a real model slug.

**Files:**
- Create: `tests/integration/test_no_unknown_slug_for_example_configs.py`

**Acceptance Criteria:**
- [ ] Test enumerates every `examples/configs/*.yaml` matching the project's published examples.
- [ ] For each: `model_identity(...)` returns a non-empty string.
- [ ] Skips `local-fake.yaml` (and any yaml whose engine kind == `"fake"`).
- [ ] Test fails if any shipped example config produces an empty identity.

**Verify:** `pixi run test tests/integration/test_no_unknown_slug_for_example_configs.py -v` → green; each config contributes one assertion.

**Steps:**

- [ ] **Step 1: Write the test**

```python
"""Regression lock: every shipped example config produces a non-empty model
identity.

Bug this catches: a future YAML shape change (renamed field, moved block,
new engine type) silently strips identity for an example config, putting
``unknown`` back in the filename schema for the next live smoke.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Trigger self-registration of every engine.
import kinoforge._adapters  # noqa: F401
from kinoforge.core import registry
from kinoforge.core.config import load_config


_EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "configs"
_SKIP_YAMLS = {
    "local-fake.yaml",  # intentional fake; identity doesn't matter.
}


def _collect_example_configs() -> list[Path]:
    return sorted(
        p for p in _EXAMPLE_DIR.glob("*.yaml") if p.name not in _SKIP_YAMLS
    )


@pytest.mark.parametrize(
    "config_path",
    _collect_example_configs(),
    ids=lambda p: p.name,
)
def test_example_config_produces_non_empty_model_identity(config_path: Path) -> None:
    cfg = load_config(str(config_path))
    if cfg.engine.kind == "fake":
        pytest.skip("fake engine — identity intentionally absent")
    engine_factory = registry.get_engine(cfg.engine.kind)
    engine = engine_factory()
    identity = engine.model_identity(cfg.model_dump())
    assert identity, (
        f"{config_path.name}: engine {cfg.engine.kind!r} returned empty "
        f"model_identity — would surface as 'unknown' in sink filename"
    )
```

- [ ] **Step 2: Run, confirm green**

Run: `pixi run test tests/integration/test_no_unknown_slug_for_example_configs.py -v`
Expected: PASS for every shipped example.

If any YAML fails (e.g. a keyframe-only YAML where the top-level engine has no identity), either:
1. Add it to `_SKIP_YAMLS` with a comment explaining why; or
2. Treat as a real bug — file gives a misleading example today; fix the YAML.

The executor MUST NOT change the assertion to make a failing YAML pass; that defeats the regression lock.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_no_unknown_slug_for_example_configs.py
pixi run pre-commit run --files tests/integration/test_no_unknown_slug_for_example_configs.py
git commit -m "test(integration): no 'unknown' slug for example configs (Layer 8 T4)"
```

---

## Task 5: PROGRESS + README + merge

**Goal:** Append a Phase 48 closeout block to `PROGRESS.md` documenting the layer; add a brief paragraph to `README.md` explaining the filename schema reflects engine-native model identity now; merge atomically.

**Files:**
- Modify: `PROGRESS.md`
- Modify: `README.md` (if there's an existing "Output / Sink" section)

**Acceptance Criteria:**
- [ ] PROGRESS.md has a new `### Phase 48 — Layer 8 (model_identity ABC)` section listing the 5 tasks with their commit SHAs.
- [ ] Carry-forward lines on PROGRESS Phase 46/47 are struck through with `~~...~~ — **CLOSED** by Phase 48 (Layer 8)`.
- [ ] README "Output / Sink" paragraph (or equivalent) notes the model slug now reflects engine-native identity.
- [ ] `pixi run test -q` green; `pixi run lint`, `pixi run format --check`, `pixi run typecheck` green.

**Verify:** `pixi run test -q && pixi run lint && pixi run typecheck` all green.

**Steps:**

- [ ] **Step 1: Append the Phase 48 block to PROGRESS.md**

After the Phase 47 block at the end of `PROGRESS.md`, append:

```markdown

### Phase 48 — Layer 8 (model_identity ABC)

Closes the Phase 46 + Phase 47 carry-forward where LocalOutputSink rendered
`model = "unknown"` for fal and ComfyUI configs because cfg.spec["model"]
was empty for engines that carry their identity elsewhere.

Adds `@abstractmethod model_identity(cfg) -> str` to both `GenerationEngine`
and `ImageEngine`. Each engine reads its native cfg surface (hosted /
diffusers / replicate-image -> spec.model; fal -> engine.fal.endpoint;
comfyui -> models[base].ref filename stem; bedrock -> engine.bedrock_video.model_id).
Orchestrator emits one WARNING per deploy() per stage when the engine
returns ``""``; sink falls back to the literal ``"unknown"`` as before.

Spec: `docs/superpowers/specs/2026-06-08-model-identity-abc-design.md`
(`a539b8c` + `8d17123`).
Plan: `docs/superpowers/plans/2026-06-09-layer-8-model-identity-abc.md`.

- [x] Task 0: ABC additions + per-engine concrete impls + test-local stubs — commit `<sha-0>`
- [x] Task 1: Per-engine unit tests + cross-engine ABC contract test — commit `<sha-1>`
- [x] Task 2: Orchestrator clip stage reads engine.model_identity — commit `<sha-2>`
- [x] Task 3: Orchestrator keyframe stage reads image_engine.model_identity — commit `<sha-3>`
- [x] Task 4: Integration regression lock for example YAMLs — commit `<sha-4>`
- [x] Task 5: PROGRESS + README + merge — commit `<sha-5>`

**Key design decisions:**
- Separate ABC method (display-only), independent of `HostedAPIEngine.key_base`
  (cache identity).  Conflating the two would force cache-identity tightening
  (e.g. comfyui workflow-hash for distinct LoRA stacks) to track filename
  aesthetics, which is the wrong direction.
- Each engine reads the cfg field it ALREADY interprets natively — no new
  schema surfaces, no Layer M reversal.
- Empty -> "" -> WARNING -> sink "unknown" fallback.  Engine MUST NOT raise;
  cache-identity contract (`key_base`) stays stricter than display contract
  (`model_identity`).
- `ImageEngine` gets its own copy of the abstract method (parallel ABCs do
  not share a parent today; introducing one is out of scope).

**Test count delta:** +~22 net (per-engine 18 + ABC contract 2 + orchestrator 3
+ integration regression N — N = number of example YAMLs minus skips).
```

- [ ] **Step 2: Strike through closed carry-forwards**

Find the Phase 46 carry-forwards block (around PROGRESS:2080-2082) and the Phase 47 carry-forwards block (around PROGRESS:2097-2098). Replace each of the two `LocalOutputSink renders the model slug as unknown ...` bullets with:

```
- ~~LocalOutputSink renders the `model` slug as `unknown` for the fal config because
  `cfg.engine.fal.endpoint` isn't propagated to the sink.~~ — **CLOSED** by Phase 48 (Layer 8).
```

Apply the same pattern to the ComfyUI carry-forward bullet on Phase 47.

- [ ] **Step 3: README touch-up**

Find the existing "Output", "Sink", or "Filenames" section of `README.md` (use `rg -n 'OutputSink|filename|sink' README.md` to locate). Append or update with a sentence:

```
Filename slugs now reflect engine-native model identity: hosted engines use
`spec.model`, fal uses `engine.fal.endpoint`, ComfyUI uses the filename stem
of the base model entry, and Bedrock uses the model id. Engines that cannot
surface a real identity log a WARNING and the slug falls back to `unknown`.
```

If `README.md` has no relevant section, skip this step and document in `docs/` instead (executor judgment).

- [ ] **Step 4: Final gate**

Run:
```bash
pixi run test -q
pixi run lint
pixi run typecheck
```
Expected: all green.

- [ ] **Step 5: Commit + (optional) merge**

```bash
git add PROGRESS.md README.md
pixi run pre-commit run --files PROGRESS.md README.md
git commit -m "docs(progress+readme): Phase 48 closeout — Layer 8 model_identity ABC"
```

If using a feature branch, finalize with `git merge --no-ff <branch>` per project convention; otherwise this commit lands on `main`.

---

## Self-review

**Spec coverage:**

| Spec section | Covered by                                                              |
|--------------|--------------------------------------------------------------------------|
| §5.1 ABC contract            | Task 0 Step 1 + Step 2                                   |
| §5.2 per-engine impls        | Task 0 Step 3                                            |
| §5.3 orchestrator clip wire  | Task 2                                                   |
| §5.4 orchestrator kf wire    | Task 3                                                   |
| §5.5 data flow               | Tasks 0/2/3 collectively                                 |
| §5.6 ABC enforcement         | Task 1 ABC contract test                                 |
| §6 error handling            | Tasks 0/2/3 (engines return ""; orchestrator WARNING)    |
| §7.1 per-engine unit tests   | Task 1                                                   |
| §7.2 ABC contract test       | Task 1                                                   |
| §7.3 orchestrator wiring tests | Tasks 2 + 3                                            |
| §7.4 integration regression  | Task 4                                                   |
| §7.5 test count expectation  | Task 5 PROGRESS block notes delta                        |
| §8 migration / rollout       | Task 0 handles atomic landing; Task 5 documents          |
| §9 carry-forwards / oos      | Task 5 PROGRESS block carries them forward verbatim      |
| §10 open questions           | None — nothing to plan for                               |

No spec section uncovered.

**Placeholder scan:** No "TBD", "TODO", "implement later". Every code block contains the actual code an executor pastes. Commit SHAs in Task 5's PROGRESS block use `<sha-N>` placeholders; the executor fills these in at commit time per the project convention (same pattern as every prior phase block in PROGRESS.md).

**Type consistency:**
- `model_identity` signature is `(self, cfg: dict[str, object]) -> str` in every snippet.
- `cfg.model_dump()` returns `dict[str, Any]` — assignment-compatible with the parameter type.
- `_model` / `_kf_model` typed as `str | None` (matches the existing pattern at the GenerateClipStage / KeyframeStage construction site).
- Logger reference `_log` matches the existing `_log = get_logger("orchestrator")` at orchestrator.py:75.

No naming drift between tasks.
