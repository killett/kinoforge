# Design — Continuity / stitching fallback (Layer B, issue #1)

**Status:** validated 2026-05-29. Locked.
**Tracks:** GitHub issue [#1](https://github.com/killett/kinoforge/issues/1).
**Builds on:** Layer A (uri_for ABC, merged at `e3e6ddf`).
**Prior art:** Phase 10 prompt splitter — splitter emits N segments where segs
1..N-1 have `assets == []`. Layer B fills those with the previous segment's
rendered tail frame as an `init_image` `ConditioningAsset`.
**Brainstormed via:** `superpowers-extended-cc:brainstorming`.

---

## 1. Problem

For non-native engines (`profile.supports_native_extension == False`),
`core/strategy.py::decide` produces N independent single-segment
`GenerationJob` objects. Each renders in isolation — no information flows
from segment N to segment N+1. Visually, the resulting clips don't chain;
characters jump, scenes reset, lighting shifts. The splitter (Phase 10)
already produces the N-segment plan; Layer B threads the previous
segment's rendered tail frame into the next segment's `init_image`
conditioning slot so the engine renders a continuation.

## 2. Goal

Add the minimum surface to make non-native multi-segment runs chain
visually:

1. New concrete-default ABC method `extract_last_frame` on
   `GenerationEngine`. Default raises `NotImplementedError`. Engines
   that support continuity override it. FakeEngine implements for tests.
2. New pure helper `core/continuity.py::inject_tail_frame(next_job,
   prev_artifact, engine) -> GenerationJob` that returns a new job whose
   segment-0 carries the tail asset.
3. `GenerateClipStage` gains an `engine: GenerationEngine` field. Its
   render path replaces `pool.map(jobs)` with a sequential loop that
   calls `inject_tail_frame` between adjacent jobs when the request's
   mode includes `init_image` in `MODE_ROLE_REQUIREMENTS`.

## 3. Non-goals

- N-clip stitching (concat / crossfade) — separate follow-up issue.
- `flf2v` continuity — its role contract is `{first_frame, last_frame}`,
  no `init_image` slot; Layer B's mode dispatch skips it. The
  pre-existing gap (`flf2v + N > 1 + non-native` segments 1..N-1 are
  missing required roles) is documented as out of scope; not a Layer B
  regression.
- ComfyUI / Diffusers / Hosted engine `extract_last_frame` implementations
  — each engine gets its own follow-up.
- Persisting intermediate segment artifacts to the store. Layer B keeps
  the existing single-Artifact return contract (`results[-1]` persisted).

## 4. Design decisions (locked)

### 4.1 Render loop home: `GenerateClipStage`; continuity module is a pure helper

The sequential render → extract → inject → render loop lives inside
`GenerateClipStage.run`. `core/continuity.py` exposes one pure helper:
`inject_tail_frame(next_job, prev_artifact, engine) -> GenerationJob`.
Stage gains an `engine: GenerationEngine` field.

**Rationale:** stage already owns rendering responsibility. Adding the
loop there keeps the existing single-dispatch shape and avoids a new
abstraction layer over what amounts to five lines.

**Rejected alternative:** `core/continuity.py::chain(jobs, engine, pool)
-> list[Artifact]` owns the orchestration; stage delegates. More
literal match to the draft issue but adds a layer that hides minimal
logic.

### 4.2 Mode dispatch via `MODE_ROLE_REQUIREMENTS`

Chaining is enabled when
`"init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, set())`. Today
this evaluates true for `i2v` only. `t2v` (empty contract) and `flf2v`
(no `init_image`) both skip chaining. Future modes that adopt
`init_image` get chaining for free.

**Rationale:** decouples continuity logic from the mode-name vocabulary.
The role contract is the source of truth for what assets a mode accepts.

**Rejected alternative:** hard-coded `if mode == "i2v"`. Coupling to a
single mode name; rejected.

### 4.3 `extract_last_frame` ABC default is concrete and raises

Method is concrete on `GenerationEngine` with a default body that raises
`NotImplementedError("<EngineName> does not support tail-frame
extraction")`. Subclasses override to enable continuity.

**Rationale:** matches the draft issue text verbatim and minimises
boilerplate. Only FakeEngine needs to override for Layer B; ComfyUI /
Diffusers / Hosted inherit the raise. Multi-segment + non-native +
non-overriding engine = `NotImplementedError` at chain time. Acceptable:
those engines either support `supports_native_extension` (so the chain
branch never executes) or get the feature added in their own follow-up.

**Rejected alternative:** `@abstractmethod` requiring every engine to
implement, even with an explicit raise. Pattern-consistent with the
other 5 abstract methods but more code for the same end result.

### 4.4 `extract_last_frame` returns `ConditioningAsset` directly

Signature: `extract_last_frame(self, artifact: Artifact) ->
ConditioningAsset`. The asset is returned ready-to-use:
`kind="image"`, `role="init_image"`, `ref=<frame Artifact>`. Caller
(`inject_tail_frame`) does not assemble.

**Rationale:** the engine knows the conventional role and kind for a
tail frame in this context. Matches the draft issue verbatim.

**Rejected alternative:** return raw `Artifact`; caller wraps.
Marginally more flexible for future flf2v continuity but YAGNI today.

### 4.5 Layer B does NOT persist intermediate segment artifacts

`results[-1]` continues to be the single Artifact persisted via
`store.put_bytes`. Intermediate `results[0..N-2]` exist only in memory,
consumed in-loop for `extract_last_frame`. Stitching (separate issue)
will refactor the persistence contract when it ships.

**Rationale:** minimal Layer B surface. Avoids preempting stitching's
design.

### 4.6 `inject_tail_frame` REPLACES seg-0 assets, doesn't append

Splitter contract guarantees segs 1..N-1 have `assets == []`. The helper
sets `segments[0].assets = [tail_asset]` rather than prepending. If a
caller violates the splitter contract (segment already has assets), the
behaviour is "replace" — the helper is opinionated. Other segments in
`next_job.segments` (if any) are untouched.

**Rationale:** simplest correct behaviour for the contract this helper
serves. Future flexibility can revisit if a real need emerges.

## 5. Interfaces

### 5.1 `src/kinoforge/core/interfaces.py` — `GenerationEngine` gains concrete default

Add after the existing 5 abstract methods (`provision`, `backend`,
`profile_for`, `declared_flags`, `validate_spec`):

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

### 5.2 `src/kinoforge/core/continuity.py` — new module

```python
"""Tail-frame conditioning for non-native multi-segment runs.

Pure helper. The interleaved render -> extract -> inject -> render loop
lives in GenerateClipStage; this module is side-effect-free.
"""

from __future__ import annotations

from dataclasses import replace

from kinoforge.core.interfaces import (
    Artifact, GenerationEngine, GenerationJob,
)


def inject_tail_frame(
    next_job: GenerationJob,
    prev_artifact: Artifact,
    engine: GenerationEngine,
) -> GenerationJob:
    """Return a copy of next_job with prev's tail as seg-0 init_image.

    Splitter contract guarantees next_job.segments[0].assets == []; the
    helper replaces that list with [tail_asset]. Other segments in
    next_job (if any) are unchanged. Original is not mutated.

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
    return replace(
        next_job, segments=[new_seg_0, *next_job.segments[1:]]
    )
```

### 5.3 `src/kinoforge/engines/fake/__init__.py` — FakeEngine override

Add inside `FakeEngine`:

```python
def extract_last_frame(self, artifact: Artifact) -> ConditioningAsset:
    """Deterministic tail-frame asset for tests.

    Returns a ConditioningAsset whose ref carries a synthetic filename
    derived from the input artifact's filename, so tests can assert on
    a predictable shape without real image data.
    """
    return ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(
            filename=f"{artifact.filename}.tail.png",
            meta={"derived_from": artifact.filename},
        ),
    )
```

### 5.4 `src/kinoforge/pipeline/generate_clip.py` — stage refactor

**5.4a.** Add `engine: GenerationEngine` field to `GenerateClipStage`.

**5.4b.** Replace the existing `results = self.pool.map(jobs); last =
results[-1]` block with:

```python
should_chain = (
    "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, set())
)
results: list[Artifact] = []
for i, job in enumerate(jobs):
    if i > 0 and should_chain:
        job = inject_tail_frame(job, results[-1], self.engine)
    art = self.pool.submit(job).result()
    results.append(art)
last = results[-1]
```

**5.4c.** Update imports — add `MODE_ROLE_REQUIREMENTS`,
`GenerationEngine` from `kinoforge.core.interfaces`; add `inject_tail_frame`
from `kinoforge.core.continuity`.

**5.4d.** Update the `# DEFERRED: stitching across N artifacts.` comment
to clarify that continuity now ships and stitching alone remains
deferred.

### 5.5 `src/kinoforge/core/orchestrator.py` — pass engine into stage

Single construction site: `orchestrator.py:477`. The local variable
holding the engine is `resolved_engine` (assigned at line 324 inside
`generate()`). Add `engine=resolved_engine` to the kwargs:

```python
stage = GenerateClipStage(
    profile=profile,
    pool=pool,
    store=store,
    run_id=run_id,
    accepted_kinds=accepted_kinds,
    base_params={},
    base_spec={},
    engine=resolved_engine,  # NEW
)
```

## 6. Test plan (TDD red-first)

Each test has a `# Bug:` comment naming a concrete regression it would
catch, per the project `test-design` skill convention.

### 6.1 `tests/core/test_continuity.py` (new file)

1. `test_inject_tail_frame_replaces_seg0_assets` — input
   `segments[0].assets == []` → output `segments[0].assets == [tail]`.
2. `test_inject_tail_frame_preserves_other_segments` — input has
   `segments = [seg0, seg1]` → output's `segments[1]` identical to input.
3. `test_inject_tail_frame_does_not_mutate_input` — input job's
   `segments[0].assets` still `[]` after call.
4. `test_inject_tail_frame_raises_when_engine_extract_raises` — engine
   raising `NotImplementedError` propagates verbatim.

### 6.2 `tests/core/test_interfaces.py` (extend)

5. `test_extract_last_frame_default_raises_with_engine_name` — minimal
   `GenerationEngine` subclass overrides only the 5 abstract methods;
   calling `extract_last_frame` raises `NotImplementedError` whose
   message contains the class name.

### 6.3 `tests/engines/test_fake.py` (extend)

6. `test_fake_engine_extract_last_frame_returns_init_image_asset` —
   FakeEngine.extract_last_frame returns `ConditioningAsset` with
   `kind="image"`, `role="init_image"`, `ref.filename` derived
   deterministically from the input.

### 6.4 `tests/pipeline/test_generate_clip.py` (extend)

7. `test_stage_native_branch_i2v_no_chain` —
   `supports_native_extension=True`, mode `"i2v"` → 1 job rendered,
   `engine.extract_last_frame` never called (spy engine asserts).
8. `test_stage_non_native_i2v_n3_chains_segs_1_and_2` —
   `supports_native_extension=False`, mode `"i2v"`, 3 segments.
   RecordingBackend captures each submitted job's segment-0 assets.
   Verify: job 0 = original assets; jobs 1+2 each have one
   `ConditioningAsset(role="init_image")` whose `ref.filename` derives
   from the previous job's output.
9. `test_stage_non_native_t2v_n3_no_chain` — same setup, mode `"t2v"`
   → all 3 jobs submitted with seg-0 assets unchanged.
10. `test_stage_non_native_i2v_n1_no_chain` — N=1 → no inject call
    (i>0 never true; spy engine asserts not called).

### 6.5 Test construction-site updates (not new behavioural tests)

Grep confirms 4 test files construct `GenerateClipStage` directly:

- `tests/pipeline/test_generate_clip.py:55, 182, 202` — fixture +
  two direct constructions.
- `tests/core/test_pool.py:162, 163` — pool-swap test passes
  `**stage_kwargs`; just add `engine` to the kwargs dict.
- `tests/core/test_orchestrator.py` — uses `patch.object` on
  `GenerateClipStage.run`; does NOT construct the stage. No change
  needed.

For each construction site, add a `engine=<engine>` argument matching
the fixture's engine. Most fixtures already build a `FakeEngine`; reuse
that. These are mechanical updates to keep existing tests green, not
new behavioural tests.

## 7. Commits (atomic, conventional)

1. `feat(continuity): add inject_tail_frame helper + extract_last_frame
   ABC default + FakeEngine impl`
   - Files: `src/kinoforge/core/continuity.py` (new),
     `src/kinoforge/core/interfaces.py`,
     `src/kinoforge/engines/fake/__init__.py`,
     `tests/core/test_continuity.py` (new),
     `tests/core/test_interfaces.py`, `tests/engines/test_fake.py`.
2. `feat(pipeline): wire continuity into GenerateClipStage non-native branch`
   - Files: `src/kinoforge/pipeline/generate_clip.py`,
     `src/kinoforge/core/orchestrator.py`,
     `tests/pipeline/test_generate_clip.py`, plus any orchestrator test
     that constructs the stage.
   - Commit message includes `Closes #1` trailer.
3. `docs(progress): mark Layer B (continuity) complete`
   - Files: `PROGRESS.md`. Append Phase 12 entry, update Single next
     action.

Followed by the conventional `chore: mark all 3 Layer B tasks completed
in tasks.json snapshot` after final review (matches Layer A pattern).

## 8. Verification (acceptance criteria)

All must hold on the final commit:

1. `pixi run test` — net +11 tests pass (4 continuity + 1 ABC default +
   1 FakeEngine + 4 stage + 1 orchestrator-update).
2. `pixi run test-cov` — coverage ≥ 90%.
3. `pixi run typecheck` — mypy strict clean.
4. `pixi run lint` — ruff clean.
5. `pixi run pre-commit run --all-files` — all hooks Passed.
6. `grep -n "DEFERRED.*continuity" src/kinoforge/pipeline/generate_clip.py`
   → 0 matches (continuity is no longer deferred). Stitching DEFERRED
   comment may remain.
7. `core/continuity.py` imports only from `kinoforge.core.interfaces`
   (test_core_invariant scanner must stay green).
8. GitHub issue #1 closes via the `Closes #1` trailer on commit 2 when
   pushed.

## 9. Risk register

- **Risk:** `GenerateClipStage` consumer code outside the stage's
  existing tests breaks when adding the `engine` field. **Mitigation:**
  grep for `GenerateClipStage(` across `src/` and `tests/` during
  implementation; update each construction site.
- **Risk:** orchestrator's engine reference is hard to thread through.
  **Mitigation:** orchestrator already constructs the stage in
  `generate()` and already holds an engine — should be a one-line
  keyword addition. If not, escalate.
- **Risk:** `flf2v + N > 1 + non-native` user file finds the gap.
  **Mitigation:** documented as deferred; no Layer B regression. A
  follow-up may add an early error in `validate_request` or the
  splitter.
- **Risk:** The default-raising `extract_last_frame` on the ABC is
  surprising relative to the 5 existing `@abstractmethod`s.
  **Mitigation:** docstring is explicit; tests guard the behaviour.

## 10. Out of scope (explicitly deferred)

- Stitching N artifacts into one final clip (concat / crossfade) — own
  follow-up.
- `flf2v` continuity — different role contract; revisit when needed.
- ComfyUI / Diffusers / Hosted `extract_last_frame` implementations —
  one follow-up per engine.
- Persisting intermediate segment artifacts to the store.
- Validating per-segment role contracts post-split (pre-existing gap
  for `flf2v + N > 1`).
