# Continuity Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thread the previous segment's rendered tail frame into the next segment's `init_image` slot for non-native multi-segment runs, so generated clips chain visually instead of starting from scratch each segment.

**Architecture:** New pure helper `core/continuity.py::inject_tail_frame(next_job, prev_artifact, engine) -> GenerationJob`; new concrete-default `extract_last_frame` on `GenerationEngine` ABC (raises `NotImplementedError`); FakeEngine overrides; `GenerateClipStage` gains an `engine` field and replaces `pool.map(jobs)` with a mode-aware sequential loop that calls `inject_tail_frame` between adjacent jobs when `request.mode`'s role contract contains `init_image`.

**Tech Stack:** Python 3.12+, pixi-managed env, pytest, mypy strict, ruff strict, stdlib `dataclasses.replace` for immutable updates.

**Spec:** `docs/superpowers/specs/2026-05-29-continuity-fallback-design.md` (committed at `959bd44`). Closes GitHub issue #1.

---

## File Map

| Path | Change |
|---|---|
| `src/kinoforge/core/interfaces.py` | Add concrete `extract_last_frame` default on `GenerationEngine` that raises `NotImplementedError` |
| `src/kinoforge/core/continuity.py` | **New file.** Single pure helper `inject_tail_frame` |
| `src/kinoforge/engines/fake/__init__.py` | Override `extract_last_frame` in `FakeEngine` (deterministic synthetic tail artifact) |
| `src/kinoforge/pipeline/generate_clip.py` | Add `engine: GenerationEngine` field; replace `pool.map(jobs)` with mode-aware loop |
| `src/kinoforge/core/orchestrator.py` | One-line addition: `engine=resolved_engine` in `GenerateClipStage(...)` call at line 477 |
| `tests/core/test_continuity.py` | **New file.** 4 tests for `inject_tail_frame` |
| `tests/core/test_interfaces.py` | Extend with `extract_last_frame` default-raises test |
| `tests/engines/test_fake.py` | Extend with `FakeEngine.extract_last_frame` test |
| `tests/pipeline/test_generate_clip.py` | Add 4 stage chain-behaviour tests; update existing 3 `GenerateClipStage(...)` calls to pass `engine=` |
| `tests/core/test_pool.py` | Update `stage_kwargs` dict at line 153 to include `engine=...` |
| `PROGRESS.md` | Mark Layer B complete; point next action at Layer C (issue #5 S3/GCS) |

---

## Task 1: Add `inject_tail_frame` helper + `extract_last_frame` ABC default + FakeEngine override

**Goal:** New pure helper + ABC default + Fake override land together with full TDD coverage. After this task, the building blocks exist; Task 2 wires them into the stage.

**Files:**
- Create: `src/kinoforge/core/continuity.py`
- Modify: `src/kinoforge/core/interfaces.py` (add `extract_last_frame` after the 5 existing abstract methods on `GenerationEngine`, around line 328)
- Modify: `src/kinoforge/engines/fake/__init__.py` (override `extract_last_frame` after `validate_spec`, around line 229)
- Create: `tests/core/test_continuity.py`
- Modify: `tests/core/test_interfaces.py` (add ABC default-raises test)
- Modify: `tests/engines/test_fake.py` (add `extract_last_frame` test)

**Acceptance Criteria:**
- [ ] `core/continuity.py` exposes `inject_tail_frame(next_job, prev_artifact, engine) -> GenerationJob`
- [ ] `GenerationEngine.extract_last_frame(artifact)` default raises `NotImplementedError` with the class name in the message
- [ ] `FakeEngine.extract_last_frame(artifact)` returns `ConditioningAsset(kind="image", role="init_image", ref=Artifact(filename=f"{artifact.filename}.tail.png", meta={"derived_from": artifact.filename}))`
- [ ] Test 1: `inject_tail_frame` replaces seg-0 assets with `[tail_asset]`
- [ ] Test 2: `inject_tail_frame` preserves segments beyond index 0
- [ ] Test 3: `inject_tail_frame` does not mutate the input job
- [ ] Test 4: `inject_tail_frame` propagates `NotImplementedError` from engine
- [ ] Test 5: ABC default raises with engine class name in message
- [ ] Test 6: `FakeEngine.extract_last_frame` returns the expected `ConditioningAsset` shape
- [ ] `pixi run pre-commit run --files <changed>` green
- [ ] `pixi run test` green; coverage ≥ 90%

**Verify:** `pixi run test tests/core/test_continuity.py tests/core/test_interfaces.py tests/engines/test_fake.py -v` → 6 new tests + all existing pass.

**Steps:**

- [ ] **Step 1: Write the first failing test in `tests/core/test_continuity.py`**

Create `/workspace/tests/core/test_continuity.py` with the following content:

```python
"""Tests for core.continuity.inject_tail_frame — pure helper for tail-frame conditioning.

Spec: docs/superpowers/specs/2026-05-29-continuity-fallback-design.md §6.1
"""

from __future__ import annotations

import pytest

from kinoforge.core.continuity import inject_tail_frame
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationEngine,
    GenerationJob,
    Segment,
)


class _FakeExtractor(GenerationEngine):
    """Minimal engine override: only extract_last_frame is meaningful."""

    name: str = "fake-extractor"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def provision(self, instance, cfg):  # noqa: ANN001, D102
        pass

    def backend(self, instance, cfg):  # noqa: ANN001, D102
        raise NotImplementedError

    def profile_for(self, key):  # noqa: ANN001, D102
        raise NotImplementedError

    def declared_flags(self, key):  # noqa: ANN001, D102
        return {}

    def validate_spec(self, job):  # noqa: ANN001, D102
        pass

    def extract_last_frame(self, artifact: Artifact) -> ConditioningAsset:
        return ConditioningAsset(
            kind="image",
            role="init_image",
            ref=Artifact(filename=f"{artifact.filename}.tail.png"),
        )


def _make_job(*, prompts: list[str], seg0_assets: list[ConditioningAsset] | None = None) -> GenerationJob:
    segs = [
        Segment(prompt=p, assets=list(seg0_assets) if (i == 0 and seg0_assets) else [])
        for i, p in enumerate(prompts)
    ]
    return GenerationJob(spec={}, segments=segs, params={})


def test_inject_tail_frame_replaces_seg0_assets() -> None:
    """When seg-0 starts empty, after inject it contains exactly [tail_asset].

    Bug this catches: helper appends instead of replacing -> splitter contract drift
    (segs 1..N-1 are guaranteed empty assets; appending would still work today but
    breaks the invariant the rest of the pipeline relies on).
    """
    next_job = _make_job(prompts=["next"])
    prev_artifact = Artifact(filename="prev.mp4")
    engine = _FakeExtractor()

    out = inject_tail_frame(next_job, prev_artifact, engine)

    assert len(out.segments[0].assets) == 1
    asset = out.segments[0].assets[0]
    assert asset.kind == "image"
    assert asset.role == "init_image"
    assert asset.ref.filename == "prev.mp4.tail.png"


def test_inject_tail_frame_preserves_other_segments() -> None:
    """Segments beyond index 0 are passed through unchanged.

    Bug this catches: helper rebuilds all segments instead of just seg-0.
    """
    next_job = _make_job(prompts=["seg0", "seg1", "seg2"])
    original_seg1 = next_job.segments[1]
    original_seg2 = next_job.segments[2]

    out = inject_tail_frame(next_job, Artifact(filename="p.mp4"), _FakeExtractor())

    assert out.segments[1] is original_seg1
    assert out.segments[2] is original_seg2


def test_inject_tail_frame_does_not_mutate_input() -> None:
    """Input job's seg-0 assets remain [] after the call.

    Bug this catches: helper mutates in place (e.g. .append) on the input segment.
    """
    next_job = _make_job(prompts=["next"])
    assert next_job.segments[0].assets == []

    inject_tail_frame(next_job, Artifact(filename="p.mp4"), _FakeExtractor())

    assert next_job.segments[0].assets == []


def test_inject_tail_frame_raises_when_engine_extract_raises() -> None:
    """NotImplementedError from engine.extract_last_frame propagates.

    Bug this catches: helper swallows the raise or wraps in a different exception.
    """

    class _Raising(_FakeExtractor):
        def extract_last_frame(self, artifact):  # noqa: ANN001
            raise NotImplementedError("nope")

    with pytest.raises(NotImplementedError, match="nope"):
        inject_tail_frame(_make_job(prompts=["x"]), Artifact(filename="p.mp4"), _Raising())
```

- [ ] **Step 2: Run the test and confirm it FAILS (no module)**

```bash
pixi run test tests/core/test_continuity.py::test_inject_tail_frame_replaces_seg0_assets -v
```

Expected: `ModuleNotFoundError: No module named 'kinoforge.core.continuity'` (or `ImportError`).

- [ ] **Step 3: Create `src/kinoforge/core/continuity.py`**

```python
"""Tail-frame conditioning for non-native multi-segment runs.

Pure helper. The interleaved render -> extract -> inject -> render loop lives
in GenerateClipStage; this module is side-effect-free.
"""

from __future__ import annotations

from dataclasses import replace

from kinoforge.core.interfaces import (
    Artifact,
    GenerationEngine,
    GenerationJob,
)


def inject_tail_frame(
    next_job: GenerationJob,
    prev_artifact: Artifact,
    engine: GenerationEngine,
) -> GenerationJob:
    """Return a copy of next_job with prev's tail as seg-0 init_image.

    Splitter contract guarantees next_job.segments[0].assets == []; the helper
    replaces that list with [tail_asset]. Other segments in next_job (if any)
    are unchanged. Original is not mutated.

    Args:
        next_job: The job that will be submitted next.
        prev_artifact: The artifact returned by the previous job's render.
        engine: Engine that knows how to extract a frame.

    Returns:
        New GenerationJob with the conditioning hand-off applied.

    Raises:
        NotImplementedError: engine.extract_last_frame raises.
    """
    tail_asset = engine.extract_last_frame(prev_artifact)
    new_seg_0 = replace(next_job.segments[0], assets=[tail_asset])
    return replace(next_job, segments=[new_seg_0, *next_job.segments[1:]])
```

- [ ] **Step 4: Add the `extract_last_frame` default to the ABC**

In `/workspace/src/kinoforge/core/interfaces.py`, locate the `GenerationEngine` class (around lines 306-328). After the existing `validate_spec` abstract method (around line 328), add this concrete (NOT @abstractmethod) method:

```python
    def extract_last_frame(self, artifact: Artifact) -> ConditioningAsset:
        """Extract last frame of a rendered clip as an init_image asset.

        Default raises; subclass to enable continuity for this engine.

        Args:
            artifact: A clip Artifact returned by backend.result() with a
                populated uri (real engines) or in-memory filename + meta
                (FakeEngine test path).

        Returns:
            ConditioningAsset(kind="image", role="init_image", ref=<frame>).

        Raises:
            NotImplementedError: Engine doesn't support tail-frame extraction.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support tail-frame extraction"
        )
```

- [ ] **Step 5: Run test 1 — confirm it PASSES**

```bash
pixi run test tests/core/test_continuity.py::test_inject_tail_frame_replaces_seg0_assets -v
```

Expected: 1 passed.

- [ ] **Step 6: Run all 4 continuity tests — confirm they PASS**

```bash
pixi run test tests/core/test_continuity.py -v
```

Expected: 4 passed.

- [ ] **Step 7: Add the ABC-default-raises test in `tests/core/test_interfaces.py`**

Append to `/workspace/tests/core/test_interfaces.py` (the file currently has tests for the existing ABCs; just add at the end):

```python
# ---------------------------------------------------------------------------
# GenerationEngine.extract_last_frame default behaviour
# ---------------------------------------------------------------------------


def test_extract_last_frame_default_raises_with_engine_name() -> None:
    """A GenerationEngine subclass that doesn't override extract_last_frame
    must raise NotImplementedError with the class name in the message.

    Bug this catches: default body forgets to include the engine class name,
    making runtime errors uninformative when a multi-segment run hits an
    engine that didn't opt in to continuity.
    """
    from kinoforge.core.interfaces import (
        Artifact,
        ConditioningAsset,
        GenerationEngine,
    )

    class _NonOverriding(GenerationEngine):
        name: str = "non-overriding"
        requires_compute: bool = False
        requires_local_weights: bool = False

        def provision(self, instance, cfg):  # noqa: ANN001
            pass

        def backend(self, instance, cfg):  # noqa: ANN001
            raise NotImplementedError

        def profile_for(self, key):  # noqa: ANN001
            raise NotImplementedError

        def declared_flags(self, key):  # noqa: ANN001
            return {}

        def validate_spec(self, job):  # noqa: ANN001
            pass

    eng = _NonOverriding()
    with pytest.raises(NotImplementedError, match="_NonOverriding"):
        eng.extract_last_frame(Artifact(filename="x.mp4"))
```

If `pytest` is not yet imported at the top of `test_interfaces.py`, add `import pytest` to its imports.

- [ ] **Step 8: Run the new ABC test — confirm it PASSES**

```bash
pixi run test tests/core/test_interfaces.py::test_extract_last_frame_default_raises_with_engine_name -v
```

Expected: 1 passed.

- [ ] **Step 9: Override `extract_last_frame` in `FakeEngine`**

In `/workspace/src/kinoforge/engines/fake/__init__.py`, after the existing `validate_spec` method (around line 229, before the `# ---` separator at line 232), add:

```python
    def extract_last_frame(self, artifact: Artifact) -> "ConditioningAsset":
        """Deterministic tail-frame asset for tests.

        Returns a ConditioningAsset whose ref carries a synthetic filename
        derived from the input artifact's filename, so tests can assert on
        a predictable shape without real image data.

        Args:
            artifact: A clip Artifact from a prior render.

        Returns:
            ConditioningAsset(kind="image", role="init_image", ref=Artifact(
                filename=f"{artifact.filename}.tail.png",
                meta={"derived_from": artifact.filename},
            ))
        """
        from kinoforge.core.interfaces import ConditioningAsset

        return ConditioningAsset(
            kind="image",
            role="init_image",
            ref=Artifact(
                filename=f"{artifact.filename}.tail.png",
                meta={"derived_from": artifact.filename},
            ),
        )
```

The local `from kinoforge.core.interfaces import ConditioningAsset` import inside the method avoids growing the module-level import list and matches the project's "import only what you need where you need it" pattern for newly-added types. The forward-string annotation on the return type avoids requiring the import at module scope.

(Alternative: add `ConditioningAsset` to the existing module-level import block at lines 25-33 and drop the inline import + forward-string. Either is acceptable per project style; pick whichever the implementer finds cleaner.)

- [ ] **Step 10: Add the FakeEngine extract_last_frame test in `tests/engines/test_fake.py`**

Append a new test at the end of `/workspace/tests/engines/test_fake.py`:

```python
# ---------------------------------------------------------------------------
# extract_last_frame override
# ---------------------------------------------------------------------------


def test_fake_engine_extract_last_frame_returns_init_image_asset() -> None:
    """FakeEngine.extract_last_frame returns a deterministic init_image asset.

    Bug this catches: override returns wrong kind/role, or filename is not
    derived deterministically from input (breaks cross-instance test
    reproducibility).
    """
    from kinoforge.core.interfaces import (
        Artifact,
        ConditioningAsset,
        ModelProfile,
    )
    from kinoforge.engines.fake import FakeEngine

    probe = ModelProfile(
        name="fake",
        max_frames=16,
        fps=8,
        supported_modes={"t2v"},
        max_resolution=(512, 512),
        supports_native_extension=False,
        supports_joint_audio=False,
    )
    engine = FakeEngine(
        probe_profile=probe,
        declared_flags_map={},
        required_spec_keys=set(),
    )

    input_artifact = Artifact(filename="clip-deadbeef0123.mp4")
    asset = engine.extract_last_frame(input_artifact)

    assert isinstance(asset, ConditioningAsset)
    assert asset.kind == "image"
    assert asset.role == "init_image"
    assert asset.ref.filename == "clip-deadbeef0123.mp4.tail.png"
    assert asset.ref.meta == {"derived_from": "clip-deadbeef0123.mp4"}
```

If the existing `test_fake.py` doesn't already have a module-level imports block for these names, the inline imports inside the test keep it self-contained.

- [ ] **Step 11: Run the FakeEngine test — confirm it PASSES**

```bash
pixi run test tests/engines/test_fake.py::test_fake_engine_extract_last_frame_returns_init_image_asset -v
```

Expected: 1 passed.

- [ ] **Step 12: Run the full test suite + pre-commit + coverage**

```bash
pixi run pre-commit run --files src/kinoforge/core/continuity.py src/kinoforge/core/interfaces.py src/kinoforge/engines/fake/__init__.py tests/core/test_continuity.py tests/core/test_interfaces.py tests/engines/test_fake.py
pixi run test
pixi run test-cov
```

Expected: all pre-commit hooks Passed; full suite passes; coverage ≥ 90%.

- [ ] **Step 13: Commit**

```bash
git add src/kinoforge/core/continuity.py src/kinoforge/core/interfaces.py src/kinoforge/engines/fake/__init__.py tests/core/test_continuity.py tests/core/test_interfaces.py tests/engines/test_fake.py
git commit -m "$(cat <<'EOF'
feat(continuity): add inject_tail_frame helper + extract_last_frame ABC default + FakeEngine impl

New pure helper core/continuity.py::inject_tail_frame(next_job, prev_artifact,
engine) -> GenerationJob; GenerationEngine.extract_last_frame is a concrete
default that raises NotImplementedError so engines opt in; FakeEngine overrides
with a deterministic synthetic tail asset for tests. Wiring into
GenerateClipStage is the next commit.

Refs #1

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Wire continuity into `GenerateClipStage` non-native branch

**Goal:** Stage replaces `pool.map(jobs)` with a mode-aware sequential loop that calls `inject_tail_frame` between adjacent jobs when the mode's role contract contains `init_image`. Orchestrator + tests updated for the new `engine` field.

**Files:**
- Modify: `src/kinoforge/pipeline/generate_clip.py` (add `engine: GenerationEngine` field; replace `pool.map` with the loop; update imports)
- Modify: `src/kinoforge/core/orchestrator.py` (line 477: add `engine=resolved_engine` kwarg)
- Modify: `tests/pipeline/test_generate_clip.py` (update fixture + 2 direct constructions to pass `engine=`; add 4 new behaviour tests)
- Modify: `tests/core/test_pool.py` (line 153 stage_kwargs dict: add `engine=...`)

**Acceptance Criteria:**
- [ ] `GenerateClipStage` has `engine: GenerationEngine` field
- [ ] `run()` uses `MODE_ROLE_REQUIREMENTS.get(request.mode, set())` to decide chaining
- [ ] When `should_chain and i > 0`, `inject_tail_frame` is called with prev artifact + engine
- [ ] All 3 existing `GenerateClipStage(...)` sites in `tests/pipeline/test_generate_clip.py` pass `engine=...`
- [ ] `tests/core/test_pool.py:153` `stage_kwargs` dict includes `engine=...`
- [ ] `src/kinoforge/core/orchestrator.py:477` includes `engine=resolved_engine`
- [ ] Test 7: native branch + i2v → 1 render, no chain (spy engine asserts not called)
- [ ] Test 8: non-native + i2v + N=3 → jobs 1+2 receive `init_image` ConditioningAsset whose ref.filename derives from prev render
- [ ] Test 9: non-native + t2v + N=3 → no chain (no init_image in mode contract)
- [ ] Test 10: non-native + i2v + N=1 → no chain (no `i > 0`)
- [ ] `pixi run pre-commit run --files <changed>` green
- [ ] `pixi run test` green; coverage ≥ 90%
- [ ] Commit message includes `Closes #1` trailer

**Verify:** `pixi run test tests/pipeline/test_generate_clip.py tests/core/test_pool.py -v && pixi run test` → 4 new tests + all existing pass.

**Steps:**

- [ ] **Step 1: Write the primary failing test (non-native + i2v + N=3 chains)**

Add this RecordingBackend class + test to `/workspace/tests/pipeline/test_generate_clip.py` (append after the existing `CountingBackend` class around line 121):

```python
# ---------------------------------------------------------------------------
# Continuity (Layer B) — chain tail-frame into next segment's init_image
# ---------------------------------------------------------------------------


class RecordingBackend(FakeBackend):
    """FakeBackend that records each submitted job's seg-0 assets."""

    def __init__(self, probe: ModelProfile) -> None:
        super().__init__(probe=probe)
        self.submitted_seg0_assets: list[list] = []  # type: ignore[type-arg]

    def submit(self, job: GenerationJob) -> str:
        # Capture a snapshot of the first segment's assets at submit time.
        self.submitted_seg0_assets.append(list(job.segments[0].assets))
        return super().submit(job)


def _fake_engine_for_tests(probe: ModelProfile):  # noqa: ANN202
    """Construct a FakeEngine with no declared flags and no required spec keys."""
    from kinoforge.engines.fake import FakeEngine

    return FakeEngine(
        probe_profile=probe,
        declared_flags_map={},
        required_spec_keys=set(),
    )


def test_stage_non_native_i2v_n3_chains_segs_1_and_2(tmp_path: Path) -> None:
    """Non-native + i2v + 3 segments → jobs 1 and 2 receive prev tail as init_image.

    Bug this catches: chain skips a segment, or order is wrong (e.g. seg 1 gets
    seg 0's tail but seg 2 also gets seg 0's tail instead of seg 1's).
    """
    profile = _profile(supports_native_extension=False)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    engine = _fake_engine_for_tests(profile)

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="chain-i2v",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,
    )

    segments = [Segment(prompt=f"segment {i}") for i in range(3)]
    stage.run(
        GenerationRequest(prompt="ignored", mode="i2v"),
        segments_override=segments,
    )

    # Job 0: no chain (no prior render to extract from).
    assert backend.submitted_seg0_assets[0] == []
    # Jobs 1 and 2: exactly one ConditioningAsset, role=init_image, kind=image.
    for i in (1, 2):
        assets = backend.submitted_seg0_assets[i]
        assert len(assets) == 1
        asset = assets[0]
        assert asset.kind == "image"
        assert asset.role == "init_image"
        # Filename derived from the prev render's output filename.
        assert asset.ref.filename.endswith(".tail.png")
```

This test imports `Segment`, `GenerationJob`, `GenerationRequest`, `ModelProfile` (already imported) and uses `LocalArtifactStore` + `SequentialPool` (already imported).

- [ ] **Step 2: Run the test — confirm it FAILS**

```bash
pixi run test tests/pipeline/test_generate_clip.py::test_stage_non_native_i2v_n3_chains_segs_1_and_2 -v
```

Expected: One of:
- `TypeError: GenerateClipStage.__init__() got an unexpected keyword argument 'engine'` (if engine field not yet added), OR
- AssertionError on the asset content (if engine field somehow exists but loop didn't change).

- [ ] **Step 3: Add `engine` field + new loop in `src/kinoforge/pipeline/generate_clip.py`**

Apply these edits to `/workspace/src/kinoforge/pipeline/generate_clip.py`:

**3a.** Update imports at the top of the file. The existing imports are:

```python
from kinoforge.core.interfaces import (
    Artifact,
    BackendPool,
    GenerationRequest,
    ModelProfile,
    Segment,
)
from kinoforge.core.strategy import decide
from kinoforge.core.validation import validate_request
from kinoforge.stores.base import ArtifactStore
```

Replace with:

```python
from kinoforge.core.continuity import inject_tail_frame
from kinoforge.core.interfaces import (
    Artifact,
    BackendPool,
    GenerationEngine,
    GenerationRequest,
    MODE_ROLE_REQUIREMENTS,
    ModelProfile,
    Segment,
)
from kinoforge.core.strategy import decide
from kinoforge.core.validation import validate_request
from kinoforge.stores.base import ArtifactStore
```

**3b.** Add `engine: GenerationEngine` field to the `GenerateClipStage` dataclass. The existing fields (around lines 48-54) become:

```python
@dataclass
class GenerateClipStage:
    """..."""

    profile: ModelProfile
    pool: BackendPool
    store: ArtifactStore
    run_id: str
    accepted_kinds: set[str]
    base_params: dict  # type: ignore[type-arg]
    base_spec: dict  # type: ignore[type-arg]
    engine: GenerationEngine
```

**3c.** Replace the existing render block in `run()`. The current code at lines 90-97 is:

```python
        jobs = decide(self.profile, segments, self.base_params, self.base_spec)

        # Single-clip happy path produces one Artifact; native-extension also
        # produces a single Artifact (one N-segment job). The non-native fan-out
        # returns N Artifacts — for this single-clip seam we return the last one
        # and DEFER stitching/continuity.
        results = self.pool.map(jobs)
        last = results[-1]  # DEFERRED: stitching across N artifacts.
```

Replace with:

```python
        jobs = decide(self.profile, segments, self.base_params, self.base_spec)

        # Continuity: for modes whose role contract accepts init_image (today
        # i2v only), thread each rendered tail-frame into the next segment's
        # init_image slot. Stitching across the N artifacts is DEFERRED to its
        # own follow-up; we still persist only the last artifact below.
        should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, set())
        results: list[Artifact] = []
        for i, job in enumerate(jobs):
            if i > 0 and should_chain:
                job = inject_tail_frame(job, results[-1], self.engine)
            art = self.pool.submit(job).result()
            results.append(art)
        last = results[-1]
```

- [ ] **Step 4: Update `src/kinoforge/core/orchestrator.py:477`**

Change the `GenerateClipStage(...)` construction at line 477 from:

```python
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id=run_id,
        accepted_kinds=accepted_kinds,
        base_params={},
        base_spec={},
    )
```

To:

```python
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id=run_id,
        accepted_kinds=accepted_kinds,
        base_params={},
        base_spec={},
        engine=resolved_engine,
    )
```

- [ ] **Step 5: Update the test fixture + 2 direct constructions in `tests/pipeline/test_generate_clip.py`**

**5a.** In `_make_stage` (around line 46-63), add `engine: GenerationEngine | None = None` to the keyword args and pass it through. The fixture becomes:

```python
def _make_stage(
    tmp_path: Path,
    *,
    profile: ModelProfile,
    backend: FakeBackend,
    run_id: str = "run-001",
    engine: object | None = None,
) -> GenerateClipStage:
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    if engine is None:
        engine = _fake_engine_for_tests(profile)
    return GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id=run_id,
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,  # type: ignore[arg-type]
    )
```

The `_fake_engine_for_tests` helper was added in Step 1 (RecordingBackend section); reuse it. The `object` annotation + `# type: ignore` keeps the fixture's signature loose for tests that may pass FakeEngine-derived doubles.

**5b.** Update the direct construction at line ~182 (in `test_round_trip_bytes`):

```python
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="rt-test",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=_fake_engine_for_tests(profile),
    )
```

**5c.** Update the direct construction at line ~202 (same test, `stage2`):

```python
    stage2 = GenerateClipStage(
        profile=profile,
        pool=pool2,
        store=store2,
        run_id="rt-test",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=_fake_engine_for_tests(profile),
    )
```

- [ ] **Step 6: Update `tests/core/test_pool.py:153` stage_kwargs dict**

In `test_pool_swap_same_result` (around line 153), the `stage_kwargs` dict needs an `engine` key. Update from:

```python
    stage_kwargs = dict(
        profile=probe,
        store=store1,
        run_id="swap-test",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
    )
```

To:

```python
    from kinoforge.engines.fake import FakeEngine

    stage_kwargs = dict(
        profile=probe,
        store=store1,
        run_id="swap-test",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=FakeEngine(
            probe_profile=probe, declared_flags_map={}, required_spec_keys=set()
        ),
    )
```

- [ ] **Step 7: Run the primary test from Step 1 — confirm it now PASSES**

```bash
pixi run test tests/pipeline/test_generate_clip.py::test_stage_non_native_i2v_n3_chains_segs_1_and_2 -v
```

Expected: 1 passed.

- [ ] **Step 8: Add tests 7, 9, 10 to `tests/pipeline/test_generate_clip.py`**

Append after the chain test from Step 1:

```python
class _SpyEngine:
    """Spy engine: asserts extract_last_frame is never called."""

    def __init__(self) -> None:
        self.extract_calls = 0

    def extract_last_frame(self, artifact):  # noqa: ANN001
        self.extract_calls += 1
        # Return a valid asset in case the test does call us — but the assertion
        # below catches the bug regardless.
        from kinoforge.core.interfaces import Artifact, ConditioningAsset

        return ConditioningAsset(
            kind="image", role="init_image", ref=Artifact(filename="spy.tail.png")
        )


def test_stage_native_branch_i2v_no_chain(tmp_path: Path) -> None:
    """Native branch (1 job) + i2v → chain never triggers (i > 0 never true).

    Bug this catches: chain accidentally runs on N=1 jobs, calling extract_last_frame
    on the (nonexistent) prior render.
    """
    profile = _profile(supports_native_extension=True)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    spy = _SpyEngine()

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="native-i2v",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=spy,  # type: ignore[arg-type]
    )

    segments = [Segment(prompt=f"seg {i}") for i in range(3)]
    stage.run(
        GenerationRequest(prompt="ignored", mode="i2v"),
        segments_override=segments,
    )

    # Native branch: 1 job submitted.
    assert len(backend.submitted_seg0_assets) == 1
    assert spy.extract_calls == 0


def test_stage_non_native_t2v_n3_no_chain(tmp_path: Path) -> None:
    """Non-native + t2v + 3 segments → no chain (no init_image in t2v role contract).

    Bug this catches: chain mistakenly triggers for modes that don't accept
    init_image, breaking validate_spec or producing wrong-shape jobs.
    """
    profile = _profile(supports_native_extension=False)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    spy = _SpyEngine()

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="t2v-no-chain",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=spy,  # type: ignore[arg-type]
    )

    segments = [Segment(prompt=f"seg {i}") for i in range(3)]
    stage.run(
        GenerationRequest(prompt="ignored", mode="t2v"),
        segments_override=segments,
    )

    # 3 jobs submitted; all with empty seg-0 assets; spy never invoked.
    assert len(backend.submitted_seg0_assets) == 3
    for assets in backend.submitted_seg0_assets:
        assert assets == []
    assert spy.extract_calls == 0


def test_stage_non_native_i2v_n1_no_chain(tmp_path: Path) -> None:
    """Non-native + i2v + 1 segment → no chain (i > 0 never true).

    Bug this catches: off-by-one tries to inject on the first segment, calling
    extract_last_frame with no prior artifact.
    """
    profile = _profile(supports_native_extension=False)
    backend = RecordingBackend(probe=profile)
    pool = SequentialPool(backend)
    store = LocalArtifactStore(tmp_path)
    spy = _SpyEngine()

    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="i2v-n1",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=spy,  # type: ignore[arg-type]
    )

    stage.run(
        GenerationRequest(prompt="ignored", mode="i2v"),
        segments_override=[Segment(prompt="only")],
    )

    assert len(backend.submitted_seg0_assets) == 1
    assert spy.extract_calls == 0
```

- [ ] **Step 9: Run all 4 new stage tests — confirm all PASS**

```bash
pixi run test tests/pipeline/test_generate_clip.py -v -k "chain or no_chain"
```

Expected: 4 passed.

- [ ] **Step 10: Run full pre-commit + full test suite + coverage**

```bash
pixi run pre-commit run --files src/kinoforge/pipeline/generate_clip.py src/kinoforge/core/orchestrator.py tests/pipeline/test_generate_clip.py tests/core/test_pool.py
pixi run test
pixi run test-cov
```

Expected: pre-commit all hooks Passed; full test suite green; coverage ≥ 90%.

- [ ] **Step 11: Verify the DEFERRED-continuity comment is gone**

```bash
grep -n "DEFERRED.*continuity\|DEFER stitching/continuity" src/kinoforge/pipeline/generate_clip.py
```

Expected: 0 matches (the old "DEFER stitching/continuity" comment was replaced by the new continuity logic; stitching alone is still deferred but the joint comment is gone).

- [ ] **Step 12: Commit**

```bash
git add src/kinoforge/pipeline/generate_clip.py src/kinoforge/core/orchestrator.py tests/pipeline/test_generate_clip.py tests/core/test_pool.py
git commit -m "$(cat <<'EOF'
feat(pipeline): wire continuity into GenerateClipStage non-native branch

GenerateClipStage gains an engine: GenerationEngine field. The render path
replaces pool.map(jobs) with a sequential loop that calls inject_tail_frame
between adjacent jobs when MODE_ROLE_REQUIREMENTS[request.mode] contains
init_image (i2v today). Native branch (1 job) and modes without init_image
in the contract (t2v, flf2v) are unaffected. Intermediate segment artifacts
remain in-memory; only the last is persisted (stitching is its own deferred
follow-up). Orchestrator + 2 test files updated to pass engine= through new
construction sites.

Closes #1

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Update `PROGRESS.md` — mark Layer B complete; point next action at Layer C

**Goal:** Per project `CLAUDE.md` durability rules — keep the recovery index current. Single next action now points at Layer C (S3/GCS stores, GitHub issue #5; unblocked by Layer A's `uri_for`).

**Files:**
- Modify: `PROGRESS.md` (append under "Post-MVP" section; update "Single next action")

**Acceptance Criteria:**
- [ ] New "Phase 12 — continuity fallback (deferred layer B, GitHub issue #1)" subsection added after Phase 11 with both Task 1 + Task 2 commit SHAs
- [ ] "Single next action" body rewritten to reflect Layer B done + Layer C (S3/GCS, issue #5) as next
- [ ] `pixi run pre-commit run --files PROGRESS.md` green

**Verify:** `git diff PROGRESS.md && pixi run pre-commit run --files PROGRESS.md` → diff shows the two additions; pre-commit green.

**Steps:**

- [ ] **Step 1: Capture both Task SHAs**

```bash
git log --oneline -5
```

Identify Task 1's commit (`feat(continuity): add inject_tail_frame ...`) and Task 2's commit (`feat(pipeline): wire continuity into GenerateClipStage ...`). Call them `<TASK1_SHA>` and `<TASK2_SHA>` below.

- [ ] **Step 2: Append Phase 12 subsection to `PROGRESS.md`**

Locate the existing "Phase 11 — uri_for ABC" block under "## Post-MVP". Add directly after it:

```markdown
### Phase 12 — continuity fallback (deferred layer B, GitHub issue #1)
- [x] Task 1: Add `inject_tail_frame` helper + `extract_last_frame` ABC default + FakeEngine impl — commit `<TASK1_SHA>`
- [x] Task 2: Wire continuity into GenerateClipStage non-native branch — commit `<TASK2_SHA>` (closes #1)
```

- [ ] **Step 3: Rewrite "Single next action" section body**

Find the existing "## Single next action" section. Replace its body (keep the heading) with:

```markdown
**Layer B (continuity, issue #1) complete.** All acceptance criteria met:
`pixi run pre-commit run --all-files` clean; `pixi run test-cov` reports
90%+ coverage; non-native multi-segment runs in modes with `init_image`
in `MODE_ROLE_REQUIREMENTS` (today: i2v) now thread the previous segment's
tail frame into the next segment's init_image slot via FakeEngine's
`extract_last_frame` override. Issue #1 closed. Stitching of N intermediate
artifacts remains deferred (separate issue).

**Next: Layer C — S3 / GCS artifact stores (GitHub issue #5).**
Layer A's `ArtifactStore.uri_for` ABC contract makes this layer
implementable: add `S3ArtifactStore` and/or `GCSArtifactStore` under
`src/kinoforge/stores/<name>/`, each satisfying the 7-method ABC including
`uri_for(run_id, name) -> str` returning the scheme-qualified URL. Adapter
self-registers under `"s3"` / `"gcs"`. Begin with the
`superpowers-extended-cc:brainstorming` skill.
```

- [ ] **Step 4: Run pre-commit**

```bash
pixi run pre-commit run --files PROGRESS.md
```

Expected: all hooks Passed.

- [ ] **Step 5: Commit**

```bash
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(progress): mark Layer B (continuity) complete

Layer B acceptance pass green: pre-commit clean, coverage >= 90%,
non-native multi-segment runs in i2v mode now thread tail-frame
into next-segment init_image via the new continuity helper. Issue #1
closed. Stitching remains its own deferred follow-up. Single next
action points at Layer C (S3/GCS stores, issue #5) — unblocked by
Layer A's uri_for ABC.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## End-of-plan acceptance check

After all three tasks ship, run:

```bash
pixi run pre-commit run --all-files
pixi run test
pixi run test-cov
pixi run typecheck
pixi run lint
grep -n "DEFERRED.*continuity\|DEFER stitching/continuity" src/kinoforge/pipeline/generate_clip.py
git log --oneline main..HEAD
git status
```

Expected:
- All hooks Passed.
- All tests pass; net +6 tests (4 continuity + 1 ABC + 1 FakeEngine in Task 1; +4 stage tests in Task 2 minus the 3 stage construction-site updates that don't count as new behavioural tests). Net total ~10 new tests including the rec­ord­ing-backend tests.
- Coverage ≥ 90%.
- mypy + ruff strict clean.
- Continuity DEFERRED comment grep → 0 matches.
- 3 implementation commits on the build branch ahead of main.
- Working tree clean.

GitHub issue #1 closes automatically when the Task 2 commit is pushed (via `Closes #1` trailer). Issue #5 (S3/GCS stores) becomes the next layer's target.
