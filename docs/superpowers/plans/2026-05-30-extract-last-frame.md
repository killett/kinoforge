# `extract_last_frame` for real engines — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship per-engine `extract_last_frame` for ComfyUI, Diffusers, and HostedAPI so non-native multi-segment chain steps decode the prior render's tail frame instead of raising `NotImplementedError`.

**Architecture:** New shared helper `core.frames.ffmpeg_last_frame(bytes) -> bytes` shells out to `ffmpeg` via an injectable subprocess seam. ABC contract changes `extract_last_frame(artifact) -> bytes` (was `-> ConditioningAsset`); `GenerateClipStage` takes over persistence by calling `store.put_bytes(run_id, ..., bytes)` and wrapping into a `ConditioningAsset`. `inject_tail_frame` simplifies to a pure asset-injection helper. Each real engine backfills `Artifact.url` in `result()` so a uniform 5-line `extract_last_frame` body (HTTP GET → ffmpeg) works for all three.

**Tech Stack:** Python 3.x stdlib (`urllib.request`, `subprocess`), pytest. No new runtime deps. Tests inject fakes; no real `ffmpeg`, network, or engine traffic.

**Spec:** `docs/superpowers/specs/2026-05-30-extract-last-frame-design.md` (commit `e835908`).

---

## Task 1: `FrameExtractionError` + `core/frames.py` shared ffmpeg helper

**Goal:** New shared frame-extraction helper that all three engines call. Decoder isolated behind an injectable seam so tests pass a fake `run` callable and no real ffmpeg subprocess fires.

**Files:**
- Create: `src/kinoforge/core/frames.py`
- Modify: `src/kinoforge/core/errors.py` (add `FrameExtractionError`)
- Test: `tests/core/test_frames.py` (new)

**Acceptance Criteria:**
- [ ] `ffmpeg_last_frame(b"video bytes", run=fake)` calls `fake` exactly once with argv `["ffmpeg","-sseof","-1","-i","pipe:0","-frames:v","1","-f","image2pipe","-vcodec","png","pipe:1"]` and stdin `b"video bytes"`.
- [ ] Returns the bytes returned by `run` verbatim (no post-processing).
- [ ] When `run` raises (simulating non-zero exit), the exception type is `FrameExtractionError`.
- [ ] Default `run` (the production subprocess path) raises `FrameExtractionError` on non-zero ffmpeg exit with the stderr text included in the message.

**Verify:** `pixi run test tests/core/test_frames.py -v` → 4 passed.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_frames.py`:

```python
"""Tests for core.frames.ffmpeg_last_frame — shared frame decoder.

Spec: docs/superpowers/specs/2026-05-30-extract-last-frame-design.md §4.1
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import FrameExtractionError
from kinoforge.core.frames import ffmpeg_last_frame

_EXPECTED_ARGV = [
    "ffmpeg",
    "-sseof", "-1",
    "-i", "pipe:0",
    "-frames:v", "1",
    "-f", "image2pipe",
    "-vcodec", "png",
    "pipe:1",
]


def test_ffmpeg_last_frame_calls_run_with_canonical_argv() -> None:
    """The exact argv we ship to ffmpeg must be the documented one.

    Bug this catches: anyone reorders flags or silently swaps -vcodec for
    -c:v; the test pins the exact wire format the helper guarantees.
    """
    calls: list[tuple[list[str], bytes]] = []

    def fake_run(argv: list[str], stdin: bytes) -> bytes:
        calls.append((argv, stdin))
        return b"PNG_OUT"

    ffmpeg_last_frame(b"VIDEO_IN", run=fake_run)

    assert len(calls) == 1
    assert calls[0][0] == _EXPECTED_ARGV
    assert calls[0][1] == b"VIDEO_IN"


def test_ffmpeg_last_frame_returns_run_output_verbatim() -> None:
    """Helper passes through bytes from run without re-encoding.

    Bug this catches: helper tries to decode/re-encode the PNG and corrupts
    arbitrary bytes that happen to look like image headers.
    """
    sentinel = b"\x89PNG\r\n\x1a\nDETERMINISTIC"

    def fake_run(argv: list[str], stdin: bytes) -> bytes:
        return sentinel

    assert ffmpeg_last_frame(b"anything", run=fake_run) is sentinel


def test_ffmpeg_last_frame_wraps_run_exception_as_frame_extraction_error() -> None:
    """A raising run is the production failure shape; helper must surface it
    as FrameExtractionError so callers have ONE exception type to catch.

    Bug this catches: callers wrap the wrong exception type and downstream
    error handling misses the real failure mode.
    """

    def boom(argv: list[str], stdin: bytes) -> bytes:
        raise FrameExtractionError("ffmpeg exit 1: invalid input")

    with pytest.raises(FrameExtractionError, match="ffmpeg exit 1"):
        ffmpeg_last_frame(b"bad", run=boom)


def test_default_run_raises_frame_extraction_error_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped default `_default_run` raises FrameExtractionError on
    non-zero subprocess.run exit and includes stderr in the message.

    Bug this catches: default path returns silently or raises raw
    CalledProcessError, leaking subprocess details into engine code.
    """
    import subprocess

    from kinoforge.core import frames

    class _FakeCompleted:
        returncode = 2
        stdout = b""
        stderr = b"Invalid data found when processing input"

    def fake_subprocess_run(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return _FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    with pytest.raises(FrameExtractionError, match="ffmpeg exit 2"):
        frames._default_run(_EXPECTED_ARGV, b"x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/core/test_frames.py -v`
Expected: 4 errors with `ImportError: cannot import name 'FrameExtractionError'` and `ModuleNotFoundError: No module named 'kinoforge.core.frames'`.

- [ ] **Step 3: Add the new error type**

Edit `src/kinoforge/core/errors.py`, append at the end:

```python
class FrameExtractionError(KinoforgeError):
    """Raised when a frame cannot be decoded from an Artifact's video bytes."""
```

- [ ] **Step 4: Create the frames module**

Create `src/kinoforge/core/frames.py`:

```python
"""Shared ffmpeg-based last-frame decoder used by every real engine.

Engines call `ffmpeg_last_frame(video_bytes)` to get the last frame as PNG
bytes; the subprocess seam is injectable so tests never spawn a real ffmpeg.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from kinoforge.core.errors import FrameExtractionError

#: The exact argv shipped to ffmpeg. Reads video from stdin, writes one PNG
#: frame (the last) to stdout. `-sseof -1` seeks to 1s before EOF; combined
#: with `-frames:v 1` ffmpeg emits a single frame at end-of-stream.
_FFMPEG_ARGV: list[str] = [
    "ffmpeg",
    "-sseof", "-1",
    "-i", "pipe:0",
    "-frames:v", "1",
    "-f", "image2pipe",
    "-vcodec", "png",
    "pipe:1",
]


def _default_run(argv: list[str], stdin: bytes) -> bytes:
    """Run *argv* with *stdin* piped; return stdout; raise on non-zero exit.

    Args:
        argv: The ffmpeg command line.
        stdin: Bytes piped to the subprocess on stdin.

    Returns:
        The subprocess's stdout bytes.

    Raises:
        FrameExtractionError: ffmpeg exited non-zero. Message includes a
            truncated stderr substring for diagnostics.
    """
    proc = subprocess.run(  # noqa: S603
        argv, input=stdin, capture_output=True, check=False
    )
    if proc.returncode != 0:
        stderr_snip = proc.stderr.decode(errors="replace")[:512]
        raise FrameExtractionError(
            f"ffmpeg exit {proc.returncode}: {stderr_snip}"
        )
    return proc.stdout


def ffmpeg_last_frame(
    video_bytes: bytes,
    *,
    run: Callable[[list[str], bytes], bytes] = _default_run,
) -> bytes:
    """Decode the last frame of *video_bytes* as PNG bytes.

    Args:
        video_bytes: Encoded video bytes (any format ffmpeg accepts).
        run: Injectable subprocess seam ``(argv, stdin) -> stdout``.

    Returns:
        PNG-encoded last frame as bytes.

    Raises:
        FrameExtractionError: ffmpeg exited non-zero or *run* raised.
    """
    return run(_FFMPEG_ARGV, video_bytes)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pixi run test tests/core/test_frames.py -v`
Expected: 4 passed.

- [ ] **Step 6: Lint, format, typecheck, hook check**

Run: `pixi run pre-commit run --files src/kinoforge/core/frames.py src/kinoforge/core/errors.py tests/core/test_frames.py`
Expected: all hooks pass.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/frames.py src/kinoforge/core/errors.py tests/core/test_frames.py
git commit -m "feat(frames): add shared ffmpeg_last_frame helper + FrameExtractionError

Injectable subprocess seam so engines call a single decoder and tests
pass a fake run callable. Canonical argv pinned by test. Default path
raises FrameExtractionError on non-zero ffmpeg exit with stderr substring."
```

---

## Task 2: ABC contract change + helper simplification + `FakeEngine` bytes return

**Goal:** Change `GenerationEngine.extract_last_frame` to return `bytes`. Simplify `inject_tail_frame` to a pure asset-injection helper (engine arg removed). Update `FakeEngine` to return deterministic bytes. Update the three impacted test files.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (ABC default at lines 330–348)
- Modify: `src/kinoforge/core/continuity.py` (entire `inject_tail_frame` body)
- Modify: `src/kinoforge/engines/fake/__init__.py` (lines 232–255)
- Modify: `tests/core/test_interfaces.py` (the ABC-default test)
- Modify: `tests/core/test_continuity.py` (full rewrite of helper tests)
- Modify: `tests/engines/test_fake.py` (one test renamed + rewritten)

**Acceptance Criteria:**
- [ ] `GenerationEngine.extract_last_frame` return annotation is `bytes`; default body still raises `NotImplementedError` with `type(self).__name__` in the message.
- [ ] `inject_tail_frame(next_job, tail_asset)` — two positional args, no engine. Returns a `GenerationJob` with seg-0 assets replaced by `[tail_asset]`. Does not mutate input.
- [ ] `FakeEngine.extract_last_frame(artifact)` returns `f"FAKE_TAIL:{artifact.filename}".encode()`.
- [ ] `tests/core/test_continuity.py` has 3 tests (replace, preserve, no-mutation); engine-extract and engine-raises tests are gone.
- [ ] `tests/core/test_interfaces.py::test_extract_last_frame_default_raises_with_engine_name` still passes; the local subclass's return annotation is `bytes`.
- [ ] `tests/engines/test_fake.py` has `test_fake_engine_extract_last_frame_returns_deterministic_bytes` asserting on the exact bytes.

**Verify:** `pixi run test tests/core/test_continuity.py tests/core/test_interfaces.py tests/engines/test_fake.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests first — `tests/core/test_continuity.py`**

Overwrite `tests/core/test_continuity.py` with:

```python
"""Tests for core.continuity.inject_tail_frame — pure asset-injection helper.

Spec: docs/superpowers/specs/2026-05-30-extract-last-frame-design.md §4.4
"""

from __future__ import annotations

from kinoforge.core.continuity import inject_tail_frame
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationJob,
    Segment,
)


def _make_job(*, prompts: list[str]) -> GenerationJob:
    segs = [Segment(prompt=p, assets=[]) for p in prompts]
    return GenerationJob(spec={}, segments=segs, params={})


def _tail_asset(filename: str = "tail.png") -> ConditioningAsset:
    return ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename=filename, uri=f"file:///{filename}"),
    )


def test_inject_tail_frame_replaces_seg0_assets() -> None:
    """seg-0 ends with exactly [tail_asset]; the passed asset is preserved by identity.

    Bug this catches: helper appends instead of replacing, OR wraps/copies the
    asset so callers can't equate by identity.
    """
    next_job = _make_job(prompts=["next"])
    asset = _tail_asset()

    out = inject_tail_frame(next_job, asset)

    assert len(out.segments[0].assets) == 1
    assert out.segments[0].assets[0] is asset


def test_inject_tail_frame_preserves_other_segments() -> None:
    """Segments beyond index 0 are passed through unchanged.

    Bug this catches: helper rebuilds all segments instead of just seg-0.
    """
    next_job = _make_job(prompts=["seg0", "seg1", "seg2"])
    original_seg1 = next_job.segments[1]
    original_seg2 = next_job.segments[2]

    out = inject_tail_frame(next_job, _tail_asset())

    assert out.segments[1] is original_seg1
    assert out.segments[2] is original_seg2


def test_inject_tail_frame_does_not_mutate_input() -> None:
    """Input job's seg-0 assets remain [] after the call.

    Bug this catches: helper mutates in place (e.g. .append) on the input.
    """
    next_job = _make_job(prompts=["next"])
    assert next_job.segments[0].assets == []

    inject_tail_frame(next_job, _tail_asset())

    assert next_job.segments[0].assets == []
```

- [ ] **Step 2: Update `tests/core/test_interfaces.py` ABC-default test**

Find the test that asserts the ABC default raises (around line 94–133). The local subclass currently declares `extract_last_frame(self, artifact) -> ConditioningAsset`. Change the return annotation to `bytes` — assertion content stays the same. Read the current shape first; only the annotation in the test stub changes.

The exact edit will look like:

```python
# Before:
class _MinimalEngine(GenerationEngine):
    ...
    def extract_last_frame(self, artifact: Artifact) -> ConditioningAsset:
        # delegates to ABC default
        return super().extract_last_frame(artifact)
```

becomes

```python
class _MinimalEngine(GenerationEngine):
    ...
    def extract_last_frame(self, artifact: Artifact) -> bytes:
        return super().extract_last_frame(artifact)
```

If the test does not override `extract_last_frame` at all (i.e. relies on a pass-through), no change is needed beyond removing any `ConditioningAsset` import that becomes unused. Verify by reading the file first.

- [ ] **Step 3: Rewrite the relevant `tests/engines/test_fake.py` test**

Find `test_fake_engine_extract_last_frame_returns_init_image_asset` (around line 362). Replace it with:

```python
def test_fake_engine_extract_last_frame_returns_deterministic_bytes() -> None:
    """FakeEngine.extract_last_frame returns deterministic bytes derived from
    artifact.filename. Lets continuity tests assert on exact tail content.

    Bug this catches: FakeEngine returns randomized or empty bytes, breaking
    deterministic continuity assertions downstream.
    """
    engine = FakeEngine(
        probe_profile=_DEFAULT_PROBE,
        declared_flags_map={},
        required_spec_keys=set(),
    )
    input_artifact = Artifact(filename="prev.mp4")

    out = engine.extract_last_frame(input_artifact)

    assert out == b"FAKE_TAIL:prev.mp4"
```

Adjust the `_DEFAULT_PROBE` reference to whatever name the test file already uses (read the file to confirm — likely an existing module-level fixture).

- [ ] **Step 4: Run tests to verify they fail**

Run: `pixi run test tests/core/test_continuity.py tests/core/test_interfaces.py tests/engines/test_fake.py -v`
Expected: failures of various shapes — old `inject_tail_frame` 3-arg signature mismatch, `ConditioningAsset` returned where `bytes` expected, etc.

- [ ] **Step 5: Change the ABC default**

Edit `src/kinoforge/core/interfaces.py` lines 330–348 (the `extract_last_frame` default). Replace with:

```python
    def extract_last_frame(self, artifact: Artifact) -> bytes:
        """Decode the last frame of a rendered clip as PNG bytes.

        Default raises; subclass to enable continuity for this engine.

        Args:
            artifact: A clip Artifact returned by backend.result() whose
                ``url`` field is populated with a fetchable location.

        Returns:
            PNG-encoded bytes of the last frame.

        Raises:
            NotImplementedError: Engine doesn't support tail-frame extraction.
            FrameExtractionError: Extraction failed at fetch or decode time.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support tail-frame extraction"
        )
```

If `ConditioningAsset` was imported solely for this annotation and no other call site in this file uses it, remove the import. (Likely still used elsewhere in the file — leave it if so.)

- [ ] **Step 6: Simplify `inject_tail_frame`**

Overwrite `src/kinoforge/core/continuity.py` with:

```python
"""Tail-frame asset injection for non-native multi-segment runs.

Pure helper. The engine + extract + persist + wrap pipeline lives in
GenerateClipStage; this module is side-effect-free dataclass juggling.
"""

from __future__ import annotations

from dataclasses import replace

from kinoforge.core.interfaces import (
    ConditioningAsset,
    GenerationJob,
)


def inject_tail_frame(
    next_job: GenerationJob,
    tail_asset: ConditioningAsset,
) -> GenerationJob:
    """Return a copy of next_job with seg-0 assets replaced by [tail_asset].

    Splitter contract guarantees ``next_job.segments[0].assets == []``; this
    helper replaces that list with ``[tail_asset]``. Segments beyond index 0
    are unchanged. Original is not mutated.

    Args:
        next_job: The job that will be submitted next.
        tail_asset: The conditioning asset (typically built by the stage from
            ``engine.extract_last_frame`` bytes persisted into the store).

    Returns:
        New GenerationJob with the conditioning hand-off applied.
    """
    new_seg_0 = replace(next_job.segments[0], assets=[tail_asset])
    return replace(next_job, segments=[new_seg_0, *next_job.segments[1:]])
```

- [ ] **Step 7: Update FakeEngine**

Edit `src/kinoforge/engines/fake/__init__.py` lines 232–255 (the `extract_last_frame` override). Replace with:

```python
    def extract_last_frame(self, artifact: Artifact) -> bytes:
        """Return deterministic bytes derived from the artifact's filename.

        Not a real PNG — the byte string is structured so tests can assert
        on its exact content without needing image-decoding libraries.

        Args:
            artifact: A clip Artifact from a prior render.

        Returns:
            ``f"FAKE_TAIL:{artifact.filename}".encode()``
        """
        return f"FAKE_TAIL:{artifact.filename}".encode()
```

If `ConditioningAsset` is only used by the old impl in this file, remove the import.

- [ ] **Step 8: Run tests to verify they pass**

Run: `pixi run test tests/core/test_continuity.py tests/core/test_interfaces.py tests/engines/test_fake.py -v`
Expected: all pass.

- [ ] **Step 9: Run the whole suite to catch ripples**

Run: `pixi run test -x`
Expected: failures only in `tests/pipeline/test_generate_clip.py` (the chain test still uses old 3-arg signature via the helper — this is what Task 3 fixes).

If failures appear in any other test file, stop and reconcile before continuing.

- [ ] **Step 10: Lint/format/typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/core/continuity.py src/kinoforge/engines/fake/__init__.py tests/core/test_continuity.py tests/core/test_interfaces.py tests/engines/test_fake.py`
Expected: all hooks pass.

- [ ] **Step 11: Commit**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/core/continuity.py src/kinoforge/engines/fake/__init__.py tests/core/test_continuity.py tests/core/test_interfaces.py tests/engines/test_fake.py
git commit -m "refactor(continuity): ABC returns bytes; helper drops engine arg

extract_last_frame now returns PNG bytes (was ConditioningAsset). Stage
will take over persistence in the next task — engines decode, stage stores.
inject_tail_frame simplifies to pure asset-injection (no engine, no
extract call). FakeEngine returns deterministic bytes for test asserts.

GenerateClipStage non-native chain temporarily broken; restored in Task 3."
```

---

## Task 3: `GenerateClipStage` non-native rewiring

**Goal:** Restore the non-native chain by having the stage call `engine.extract_last_frame` for bytes, persist via `store.put_bytes(run_id, ..., bytes)`, wrap into a `ConditioningAsset`, then call the simplified `inject_tail_frame`. Update the chain test for the new shape.

**Files:**
- Modify: `src/kinoforge/pipeline/generate_clip.py` (lines 16–112 — import, chain loop)
- Modify: `tests/pipeline/test_generate_clip.py` (existing chain test at lines 188–227 + `_SpyEngine` at 295–309)

**Acceptance Criteria:**
- [ ] Stage's non-native chain step calls `self.engine.extract_last_frame(results[-1])` → bytes, then `self.store.put_bytes(self.run_id, f"seg-{i-1}-tail.png", bytes)` → `Artifact`, wraps that into `ConditioningAsset(kind="image", role="init_image", ref=that_artifact)`, then `inject_tail_frame(job, asset)`.
- [ ] Tail PNG is named exactly `f"seg-{i-1}-tail.png"` where `i` is the loop index (so seg-0's tail lands at `seg-0-tail.png`, seg-1's at `seg-1-tail.png`).
- [ ] Existing chain test `test_stage_non_native_i2v_n3_chains_segs_1_and_2` still passes — asset's `ref.uri` matches the value `store.put_bytes` returned; `ref.filename` ends with `-tail.png`.
- [ ] `_SpyEngine` at lines 295–309 deleted (no longer needed — the existing `RecordingBackend` + a bytes-returning FakeEngine variant covers the assertions).
- [ ] New test `test_stage_chain_persists_tail_via_store` asserts that exactly one `store.put_bytes` call landed under `run_id` with name `"seg-0-tail.png"` between segments 0 and 1.
- [ ] `test_stage_native_branch_i2v_no_chain`, `test_stage_non_native_t2v_n3_no_chain`, `test_stage_non_native_i2v_n1_no_chain` (which used `_SpyEngine.extract_calls`) updated to use a count-based wrapper around the bytes-returning FakeEngine — assertions become `assert engine.extract_calls == 0`.

**Verify:** `pixi run test tests/pipeline/test_generate_clip.py -v` → all pass (the previously-failing chain test now green).

**Steps:**

- [ ] **Step 1: Read the current stage and its chain test**

Run: `pixi run test tests/pipeline/test_generate_clip.py::test_stage_non_native_i2v_n3_chains_segs_1_and_2 -v`
Expected: FAIL with TypeError on `inject_tail_frame` extra arg.

- [ ] **Step 2: Update the stage**

Edit `src/kinoforge/pipeline/generate_clip.py`:

Change the import block at lines 16–28 (add `ConditioningAsset`):

```python
from kinoforge.core.continuity import inject_tail_frame
from kinoforge.core.interfaces import (
    MODE_ROLE_REQUIREMENTS,
    Artifact,
    BackendPool,
    ConditioningAsset,
    GenerationEngine,
    GenerationRequest,
    ModelProfile,
    Segment,
)
from kinoforge.core.strategy import decide
from kinoforge.core.validation import validate_request
from kinoforge.stores.base import ArtifactStore
```

Replace the chain loop at lines 102–107 (currently the `for i, job in enumerate(jobs)` block) with:

```python
        results: list[Artifact] = []
        for i, job in enumerate(jobs):
            if i > 0 and should_chain:
                tail_bytes = self.engine.extract_last_frame(results[-1])
                tail_name = f"seg-{i - 1}-tail.png"
                tail_artifact = self.store.put_bytes(
                    self.run_id, tail_name, tail_bytes
                )
                tail_asset = ConditioningAsset(
                    kind="image",
                    role="init_image",
                    ref=tail_artifact,
                )
                job = inject_tail_frame(job, tail_asset)
            art = self.pool.submit(job).result()
            results.append(art)
        last = results[-1]
```

Note: `store.put_bytes` returns an `Artifact` (per `LocalArtifactStore` impl) — that's the `ref` of the new `ConditioningAsset`. Verify by reading `src/kinoforge/stores/local.py` if uncertain.

- [ ] **Step 3: Verify the existing chain test now passes**

The existing `test_stage_non_native_i2v_n3_chains_segs_1_and_2` at line 188 asserts `asset.ref.filename.endswith(".tail.png")`. The new code produces `ref.filename = "seg-0-tail.png"` (and `seg-1-tail.png`) — both end with `-tail.png`, NOT `.tail.png`. Update the assertion at line 227:

```python
        assert asset.ref.filename.endswith("-tail.png")
```

(Single character: dot→dash. The old FakeEngine impl wrote `"{prev}.tail.png"`; the new stage impl writes `"seg-{i-1}-tail.png"`.)

- [ ] **Step 4: Delete the obsolete `_SpyEngine`**

In `tests/pipeline/test_generate_clip.py`, delete lines 295–309 (the `_SpyEngine` class — class declaration + `__init__` + `extract_last_frame`).

- [ ] **Step 5: Replace the three "no-chain" tests' spy with a counting FakeEngine wrapper**

The three tests `test_stage_native_branch_i2v_no_chain`, `test_stage_non_native_t2v_n3_no_chain`, `test_stage_non_native_i2v_n1_no_chain` use `_SpyEngine` and assert `spy.extract_calls == 0`. Replace each `spy = _SpyEngine()` line with:

```python
        engine = _CountingExtractEngine(probe=profile)
```

Add this helper near `_fake_engine_for_tests` (around line 177):

```python
class _CountingExtractEngine:
    """Wraps a FakeEngine; counts extract_last_frame calls.

    The stage type-hints `engine: GenerationEngine`; we duck-type since the
    only methods the stage calls are extract_last_frame (here) and (via the
    submit path) nothing else on the engine itself — the pool drives submit.
    """

    def __init__(self, probe: ModelProfile) -> None:
        from kinoforge.engines.fake import FakeEngine

        self._inner = FakeEngine(
            probe_profile=probe,
            declared_flags_map={},
            required_spec_keys=set(),
        )
        self.extract_calls = 0

    def extract_last_frame(self, artifact: Artifact) -> bytes:
        self.extract_calls += 1
        return self._inner.extract_last_frame(artifact)
```

(`Artifact` and `ModelProfile` are already imported in this file.)

Each of the three tests' construction calls and assertions change:

```python
        # was:
        spy = _SpyEngine()
        stage = ...(engine=spy)
        assert spy.extract_calls == 0
        # becomes:
        engine = _CountingExtractEngine(probe=profile)
        stage = ...(engine=engine)
        assert engine.extract_calls == 0
```

The `# type: ignore[arg-type]` comments next to `engine=spy` can stay or be removed — the duck-typed shape satisfies the protocol surface the stage actually uses.

- [ ] **Step 6: Add the new persistence-assertion test**

Append to `tests/pipeline/test_generate_clip.py`:

```python
def test_stage_chain_persists_tail_via_store(tmp_path: Path) -> None:
    """Non-native chain writes one tail PNG per gap via store.put_bytes,
    under the stage's run_id namespace, with name 'seg-<i>-tail.png'.

    Bug this catches: stage skips persistence, persists under wrong run_id,
    or names files inconsistently — breaking `kinoforge gc --run` cleanup.
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
        run_id="run-persist",
        accepted_kinds={"image"},
        base_params={},
        base_spec={},
        engine=engine,
    )

    segments = [Segment(prompt=f"seg {i}") for i in range(3)]
    stage.run(
        GenerationRequest(prompt="ignored", mode="i2v"),
        segments_override=segments,
    )

    listed = store.list("run-persist")
    tails = sorted(n for n in listed if n.endswith("-tail.png"))
    # 3 segments → 2 chain gaps → 2 tail PNGs.
    assert tails == ["seg-0-tail.png", "seg-1-tail.png"]

    # Bytes round-trip: store returned the FakeEngine's deterministic bytes.
    seg0_tail_bytes = store.get_bytes(
        store.uri_for("run-persist", "seg-0-tail.png")
    )
    assert seg0_tail_bytes.startswith(b"FAKE_TAIL:")
```

- [ ] **Step 7: Run the file's full suite**

Run: `pixi run test tests/pipeline/test_generate_clip.py -v`
Expected: all pass, including the new test.

- [ ] **Step 8: Run the whole suite to catch ripples**

Run: `pixi run test`
Expected: all pass. Engine-level `extract_last_frame` impls still inherit raising default (Tasks 4–6 address that), but no test exercises that path on the real engines yet.

- [ ] **Step 9: Lint/format/typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/pipeline/generate_clip.py tests/pipeline/test_generate_clip.py`
Expected: all hooks pass.

- [ ] **Step 10: Commit**

```bash
git add src/kinoforge/pipeline/generate_clip.py tests/pipeline/test_generate_clip.py
git commit -m "feat(stage): rewire non-native chain to persist tail PNG via store

Stage now: extract_last_frame -> bytes, store.put_bytes(run_id, name, bytes)
-> Artifact, wrap into ConditioningAsset(role=init_image), inject_tail_frame.
Tail PNGs land in the same run_id namespace as the final clip artifact,
so 'kinoforge gc --run <id>' cleans both.

Existing chain test asserts updated; new persistence test asserts tails
appear in store.list with deterministic FAKE_TAIL: bytes."
```

---

## Task 4: ComfyUI `result()` URL backfill + `extract_last_frame` + 2 seams

**Goal:** `ComfyUIBackend.result()` populates `Artifact.url = f"{base_url}/view?filename={fn}&type=output"`. `ComfyUIEngine` gains `http_get_bytes` and `ffmpeg_run` seams. `extract_last_frame` body is the 5-liner: empty-URL guard → GET → ffmpeg.

**Files:**
- Modify: `src/kinoforge/engines/comfyui/__init__.py` (imports, default-helpers section, `ComfyUIBackend.result`, `ComfyUIEngine.__init__`, new `extract_last_frame` method)
- Modify: `tests/engines/test_comfyui.py` (4 new tests; possibly update `_make_engine` helper to pass the new seams as no-ops by default)

**Acceptance Criteria:**
- [ ] `ComfyUIBackend.result()` returns `Artifact(filename=..., url=f"{base_url}/view?filename={filename}&type=output", meta={"prompt_id": job_id})`.
- [ ] `ComfyUIEngine.__init__` accepts `http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes` and `ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run`.
- [ ] `ComfyUIEngine.extract_last_frame(artifact)` raises `FrameExtractionError` when `artifact.url == ""`; otherwise calls `http_get_bytes(artifact.url)` then `ffmpeg_last_frame(bytes, run=ffmpeg_run)`; returns the bytes.
- [ ] Test: `result()` populates URL with the documented `/view?filename=...&type=output` shape.
- [ ] Test: `extract_last_frame` calls `http_get_bytes` once with `artifact.url`, passes the result to ffmpeg seam, returns ffmpeg seam's bytes.
- [ ] Test: empty `artifact.url` → `FrameExtractionError`.
- [ ] Test: `_urllib_get_bytes` default exists and is callable (smoke test — does not hit network).

**Verify:** `pixi run test tests/engines/test_comfyui.py -v` → all pass (existing + 4 new).

**Steps:**

- [ ] **Step 1: Write failing tests**

Append to `tests/engines/test_comfyui.py`:

```python
# ---------------------------------------------------------------------------
# extract_last_frame + result() URL backfill (Layer extract_last_frame)
# ---------------------------------------------------------------------------


from kinoforge.core.errors import FrameExtractionError


def test_result_populates_url_with_view_query() -> None:
    """ComfyUIBackend.result() backfills Artifact.url with /view?filename=...&type=output.

    Bug this catches: URL not set, or wrong query shape, leaving
    extract_last_frame unable to fetch the rendered bytes.
    """
    from kinoforge.engines.comfyui import ComfyUIBackend

    history_payload = {
        "PROMPT_ID": {
            "outputs": {
                "9": {"files": [{"filename": "clip.mp4"}]},
            }
        }
    }

    backend = ComfyUIBackend(
        http_post=lambda url, body: {"prompt_id": "PROMPT_ID"},
        http_get=lambda url: history_payload,
        base_url="http://localhost:8188",
        probe=_DEFAULT_PROBE,
        sleep=lambda s: None,
    )

    artifact = backend.result("PROMPT_ID")

    assert artifact.filename == "clip.mp4"
    assert artifact.url == "http://localhost:8188/view?filename=clip.mp4&type=output"
    assert artifact.meta == {"prompt_id": "PROMPT_ID"}


def test_extract_last_frame_fetches_url_and_calls_ffmpeg() -> None:
    """extract_last_frame: http_get_bytes(artifact.url) -> ffmpeg_run -> return.

    Bug this catches: engine fetches the wrong URL (e.g. from meta), or
    skips ffmpeg, or drops the bytes returned by the decoder.
    """
    fetch_calls: list[str] = []
    ffmpeg_calls: list[tuple[list[str], bytes]] = []

    def fake_fetch(url: str) -> bytes:
        fetch_calls.append(url)
        return b"VIDEO_BYTES"

    def fake_ffmpeg(argv: list[str], stdin: bytes) -> bytes:
        ffmpeg_calls.append((argv, stdin))
        return b"PNG_BYTES"

    engine = _make_engine(http_get_bytes=fake_fetch, ffmpeg_run=fake_ffmpeg)

    artifact = Artifact(
        filename="clip.mp4",
        url="http://localhost:8188/view?filename=clip.mp4&type=output",
        meta={"prompt_id": "X"},
    )

    out = engine.extract_last_frame(artifact)

    assert out == b"PNG_BYTES"
    assert fetch_calls == [
        "http://localhost:8188/view?filename=clip.mp4&type=output"
    ]
    assert len(ffmpeg_calls) == 1
    assert ffmpeg_calls[0][1] == b"VIDEO_BYTES"


def test_extract_last_frame_raises_on_empty_url() -> None:
    """artifact.url == '' is unrecoverable; raise FrameExtractionError with
    engine class name in the message.

    Bug this catches: engine swallows the bad input and hits ffmpeg with
    empty bytes (which produces a less actionable error).
    """
    engine = _make_engine()
    artifact = Artifact(filename="clip.mp4", url="", meta={})

    with pytest.raises(FrameExtractionError, match="ComfyUIEngine"):
        engine.extract_last_frame(artifact)


def test_urllib_get_bytes_default_is_callable() -> None:
    """The shipped default for http_get_bytes is a real callable, not None.

    Bug this catches: engine constructor accepts None for the seam, making
    extract_last_frame crash at call time on production paths.
    """
    from kinoforge.engines.comfyui import _urllib_get_bytes

    assert callable(_urllib_get_bytes)
```

Also update `_make_engine` (lines 70–80) to accept and pass through the new seams. Edit the `defaults` dict to add:

```python
        "http_get_bytes": lambda url: b"",
        "ffmpeg_run": lambda argv, stdin: b"",
```

So the full helper becomes:

```python
def _make_engine(**kwargs: Any) -> ComfyUIEngine:
    """Return a ComfyUIEngine with all I/O seams replaced by safe no-ops."""
    defaults: dict[str, Any] = {
        "run_cmd": lambda argv, cwd=None: None,
        "file_exists": lambda p: False,
        "route_file": lambda src, dst_dir: None,
        "http_post": lambda url, body: {},
        "http_get": lambda url: {},
        "http_get_bytes": lambda url: b"",
        "ffmpeg_run": lambda argv, stdin: b"",
        "sleep": lambda s: None,
        "probe_profile": _DEFAULT_PROBE,
    }
    defaults.update(kwargs)
    return ComfyUIEngine(**defaults)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/engines/test_comfyui.py -v -k "result_populates_url or extract_last_frame or urllib_get_bytes_default"`
Expected: 4 failures — `TypeError` on extra `__init__` kwargs, missing methods.

- [ ] **Step 3: Implement the engine changes**

Edit `src/kinoforge/engines/comfyui/__init__.py`:

**Imports** — add at the top with the others:

```python
from kinoforge.core import frames
from kinoforge.core.errors import FrameExtractionError, ValidationError
```

(Replace the existing `from kinoforge.core.errors import ValidationError` line with the line above.)

**New helper** — add to the "Real I/O helpers" section after `_shutil_move`:

```python
def _urllib_get_bytes(url: str) -> bytes:
    """GET *url* and return the raw response body as bytes.

    Args:
        url: Endpoint URL.

    Returns:
        Response body as bytes.
    """
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return bytes(resp.read())
```

**`ComfyUIBackend.result()`** — at line 251, change the return statement from:

```python
                return Artifact(filename=filename, meta={"prompt_id": job_id})
```

to:

```python
                view_url = f"{self._base_url}/view?filename={filename}&type=output"
                return Artifact(
                    filename=filename,
                    url=view_url,
                    meta={"prompt_id": job_id},
                )
```

**`ComfyUIEngine.__init__`** — add two new kwargs (insert into the signature after `http_get`):

```python
    def __init__(
        self,
        *,
        run_cmd: Callable[[list[str], str | None], None] = _subprocess_run,
        file_exists: Callable[[str], bool] = _path_exists,
        route_file: Callable[[str, str], None] = _shutil_move,
        http_post: Callable[[str, Any], dict[str, Any]] = _urllib_post_json,
        http_get: Callable[[str], dict[str, Any]] = _urllib_get_json,
        http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
        ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run,
        sleep: Callable[[float], None] = time.sleep,
        probe_profile: ModelProfile = _DEFAULT_PROBE,
        flags_table: dict[str, dict[str, bool]] | None = None,
        comfyui_root: str = _DEFAULT_COMFYUI_ROOT,
    ) -> None:
```

Assign in the body (after `self._http_get = http_get`):

```python
        self._http_get_bytes = http_get_bytes
        self._ffmpeg_run = ffmpeg_run
```

Update the docstring `Args:` block to include the two new seams (one-line each, matching surrounding style).

**New method `extract_last_frame`** — add at the end of `ComfyUIEngine`, after `validate_spec`:

```python
    def extract_last_frame(self, artifact: Artifact) -> bytes:
        """Fetch the rendered video bytes via HTTP and decode the last frame.

        Args:
            artifact: A clip Artifact returned by :meth:`ComfyUIBackend.result`
                with ``url`` populated.

        Returns:
            PNG-encoded last frame as bytes.

        Raises:
            FrameExtractionError: ``artifact.url`` is empty, or the fetch or
                ffmpeg decode failed.
        """
        if not artifact.url:
            raise FrameExtractionError(
                f"{type(self).__name__}: artifact.url is empty; "
                "cannot fetch video bytes"
            )
        video_bytes = self._http_get_bytes(artifact.url)
        return frames.ffmpeg_last_frame(video_bytes, run=self._ffmpeg_run)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pixi run test tests/engines/test_comfyui.py -v`
Expected: all pass.

- [ ] **Step 5: Lint/format/typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/engines/comfyui/__init__.py tests/engines/test_comfyui.py`
Expected: all hooks pass.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/engines/comfyui/__init__.py tests/engines/test_comfyui.py
git commit -m "feat(comfyui): result() backfills /view URL + extract_last_frame impl

ComfyUIBackend.result() now sets Artifact.url to the /view?filename=&type=output
endpoint so extract_last_frame can fetch the rendered video bytes via HTTP.

ComfyUIEngine gains http_get_bytes + ffmpeg_run seams (both injectable;
tests pass spies). extract_last_frame is the uniform 5-liner:
empty-url guard -> http_get_bytes -> ffmpeg_last_frame."
```

---

## Task 5: Diffusers `result()` URL passthrough + `extract_last_frame` + 2 seams + server contract doc

**Goal:** `DiffusersBackend.result()` reads `data["url"]` and populates `Artifact.url`. Engine gains `http_get_bytes` + `ffmpeg_run` seams. `extract_last_frame` body is the same 5-liner. README documents the server-side `url` field requirement.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py`
- Modify: `tests/engines/test_diffusers.py`
- Modify: `README.md` (under "Engines" / Diffusers subsection if present; otherwise append a "Diffusers server contract" subsection under the appropriate top-level heading)

**Acceptance Criteria:**
- [ ] `DiffusersBackend.result()` returns `Artifact(filename=..., url=str(data.get("url", "")), meta={"job_id": job_id})`.
- [ ] `DiffusersEngine.__init__` accepts `http_get_bytes` and `ffmpeg_run` seams (same shape as ComfyUI's).
- [ ] `DiffusersEngine.extract_last_frame` body is identical to ComfyUI's (engine class name in error message differs).
- [ ] Test: `result()` reads `url` from the polled response.
- [ ] Test: `result()` defaults to `url=""` when server omits the field.
- [ ] Test: `extract_last_frame` GET + ffmpeg path (same as ComfyUI).
- [ ] Test: `extract_last_frame` empty-URL → `FrameExtractionError` mentioning `DiffusersEngine`.
- [ ] README documents the server-side `"url"` field contract.

**Verify:** `pixi run test tests/engines/test_diffusers.py -v` → all pass (existing + 4 new).

**Steps:**

- [ ] **Step 1: Read the existing diffusers test layout**

Run: `head -100 tests/engines/test_diffusers.py`

Adapt the new tests to match the file's existing fixture style. The test code below assumes a similar `_make_engine(**kwargs)` helper exists; if the file uses an inline construction pattern, mirror that.

- [ ] **Step 2: Write failing tests**

Append to `tests/engines/test_diffusers.py`:

```python
# ---------------------------------------------------------------------------
# extract_last_frame + result() URL passthrough (Layer extract_last_frame)
# ---------------------------------------------------------------------------


from kinoforge.core.errors import FrameExtractionError
from kinoforge.core.interfaces import Artifact as _Artifact_for_tests


def test_result_passes_url_from_server_response() -> None:
    """DiffusersBackend.result() reads 'url' from the polled response body.

    Bug this catches: backend ignores the new field, leaving extract_last_frame
    with nothing to fetch.
    """
    from kinoforge.engines.diffusers import DiffusersBackend

    payload = {
        "status": "done",
        "filename": "clip.mp4",
        "url": "http://127.0.0.1:8000/file/clip.mp4",
    }
    backend = DiffusersBackend(
        http_post=lambda url, body: {"job_id": "JOB"},
        http_get=lambda url: payload,
        base_url="http://127.0.0.1:8000",
        probe_profile=_DEFAULT_PROBE,
        sleep=lambda s: None,
    )

    artifact = backend.result("JOB")

    assert artifact.filename == "clip.mp4"
    assert artifact.url == "http://127.0.0.1:8000/file/clip.mp4"


def test_result_defaults_url_to_empty_string_when_server_omits_field() -> None:
    """A server that doesn't return 'url' leaves Artifact.url == ''.
    extract_last_frame will then raise FrameExtractionError with a clear
    message — preferable to a corrupt download.

    Bug this catches: backend crashes with KeyError, leaking server-shape
    details into engine-layer code.
    """
    from kinoforge.engines.diffusers import DiffusersBackend

    backend = DiffusersBackend(
        http_post=lambda url, body: {"job_id": "JOB"},
        http_get=lambda url: {"status": "done", "filename": "clip.mp4"},
        base_url="http://127.0.0.1:8000",
        probe_profile=_DEFAULT_PROBE,
        sleep=lambda s: None,
    )

    artifact = backend.result("JOB")

    assert artifact.url == ""


def test_extract_last_frame_fetches_url_and_calls_ffmpeg() -> None:
    """Same shape as ComfyUI extract test, with DiffusersEngine.

    Bug this catches: engine drops the fetched bytes or skips ffmpeg.
    """
    fetch_calls: list[str] = []
    ffmpeg_calls: list[tuple[list[str], bytes]] = []

    def fake_fetch(url: str) -> bytes:
        fetch_calls.append(url)
        return b"VIDEO"

    def fake_ffmpeg(argv: list[str], stdin: bytes) -> bytes:
        ffmpeg_calls.append((argv, stdin))
        return b"PNG"

    engine = _make_engine(http_get_bytes=fake_fetch, ffmpeg_run=fake_ffmpeg)

    artifact = _Artifact_for_tests(
        filename="clip.mp4",
        url="http://127.0.0.1:8000/file/clip.mp4",
        meta={"job_id": "X"},
    )

    out = engine.extract_last_frame(artifact)

    assert out == b"PNG"
    assert fetch_calls == ["http://127.0.0.1:8000/file/clip.mp4"]
    assert ffmpeg_calls[0][1] == b"VIDEO"


def test_extract_last_frame_raises_on_empty_url() -> None:
    """artifact.url == '' raises FrameExtractionError mentioning DiffusersEngine.

    Bug this catches: shared body copy-paste leaves the wrong class name.
    """
    engine = _make_engine()
    artifact = _Artifact_for_tests(filename="clip.mp4", url="", meta={})

    with pytest.raises(FrameExtractionError, match="DiffusersEngine"):
        engine.extract_last_frame(artifact)
```

If `_make_engine` doesn't exist or doesn't accept the new seams, add/update it the same way Task 4 step 1 added them for ComfyUI: include `http_get_bytes` and `ffmpeg_run` as defaulted no-ops.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pixi run test tests/engines/test_diffusers.py -v -k "url or extract_last_frame"`
Expected: 4 failures.

- [ ] **Step 4: Implement the engine changes**

Edit `src/kinoforge/engines/diffusers/__init__.py`:

**Imports** — add:

```python
from kinoforge.core import frames
from kinoforge.core.errors import FrameExtractionError, ValidationError
```

(replacing the existing `from kinoforge.core.errors import ValidationError`)

**New helper** — add to the "Real I/O helpers" section:

```python
def _urllib_get_bytes(url: str) -> bytes:
    """GET *url* and return the raw response body as bytes.

    Args:
        url: Endpoint URL.

    Returns:
        Response body as bytes.
    """
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return bytes(resp.read())
```

**`DiffusersBackend.result()`** — at line 198, change the return statement from:

```python
                filename = str(data.get("filename", ""))
                return Artifact(filename=filename, meta={"job_id": job_id})
```

to:

```python
                filename = str(data.get("filename", ""))
                url = str(data.get("url", ""))
                return Artifact(filename=filename, url=url, meta={"job_id": job_id})
```

**`DiffusersEngine.__init__`** — add `http_get_bytes` and `ffmpeg_run` kwargs (same shape as Task 4):

```python
        http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
        ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run,
```

Body assignments after the existing `self._http_get = http_get`:

```python
        self._http_get_bytes = http_get_bytes
        self._ffmpeg_run = ffmpeg_run
```

**New `extract_last_frame`** — at end of `DiffusersEngine`:

```python
    def extract_last_frame(self, artifact: Artifact) -> bytes:
        """Fetch the rendered video bytes via HTTP and decode the last frame.

        Args:
            artifact: A clip Artifact returned by :meth:`DiffusersBackend.result`
                with ``url`` populated by the inference server.

        Returns:
            PNG-encoded last frame as bytes.

        Raises:
            FrameExtractionError: ``artifact.url`` is empty (server omitted
                the ``url`` field from its response), or the fetch or ffmpeg
                decode failed.
        """
        if not artifact.url:
            raise FrameExtractionError(
                f"{type(self).__name__}: artifact.url is empty; "
                "cannot fetch video bytes"
            )
        video_bytes = self._http_get_bytes(artifact.url)
        return frames.ffmpeg_last_frame(video_bytes, run=self._ffmpeg_run)
```

- [ ] **Step 5: Document the server contract in README**

Find the Diffusers section of `README.md` (search for `Diffusers` heading). Append a subsection:

```markdown
### Diffusers inference-server response contract

`DiffusersBackend.result()` polls `GET /status/{job_id}` and reads two
fields from a successful (`status: done`) response:

- `filename` — display name for the produced clip.
- `url` — HTTP-fetchable location for the produced clip (e.g.
  `http://127.0.0.1:8000/file/clip.mp4`). Required for non-native
  multi-segment runs (`extract_last_frame` GETs this URL to decode the
  tail frame). Servers that omit it leave `Artifact.url == ""`; calling
  `extract_last_frame` then raises `FrameExtractionError` with a clear
  message instead of attempting a corrupt fetch.
```

If the README doesn't already have a Diffusers section, add this under the most natural existing heading (e.g. "Engines"). Read the file before editing.

- [ ] **Step 6: Run tests to verify they pass**

Run: `pixi run test tests/engines/test_diffusers.py -v`
Expected: all pass.

- [ ] **Step 7: Lint/format/typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/engines/diffusers/__init__.py tests/engines/test_diffusers.py README.md`
Expected: all hooks pass.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/engines/diffusers/__init__.py tests/engines/test_diffusers.py README.md
git commit -m "feat(diffusers): result() reads server url + extract_last_frame impl

DiffusersBackend.result() now reads 'url' from the /status/{job_id}
response body and populates Artifact.url. Missing field defaults to ''
so extract_last_frame raises FrameExtractionError with a clear message
rather than attempting a corrupt fetch.

DiffusersEngine gains http_get_bytes + ffmpeg_run seams; extract_last_frame
is the uniform 5-liner. README documents the new server-side field."
```

---

## Task 6: Hosted `url_path` cfg + dot-walker + `result()` backfill + `extract_last_frame` + 2 seams

**Goal:** `HostedAPIBackend` learns a `url_path` (e.g. `"video.url"`); `result()` walks the polled response with `_walk_dot_path` and populates `Artifact.url`. Engine gains the standard two seams. `extract_last_frame` is the 5-liner. Cfg key `cfg["engine"]["hosted"]["url_path"]` threaded through `backend()`. Example config updated.

**Files:**
- Modify: `src/kinoforge/engines/hosted/__init__.py`
- Modify: `tests/engines/test_hosted.py`
- Modify: `examples/configs/hosted.yaml` (add `url_path: video.url`)
- Modify: `README.md` (document `url_path` cfg under Hosted)

**Acceptance Criteria:**
- [ ] `_walk_dot_path({"video": {"url": "X"}}, "video.url") == "X"`.
- [ ] `_walk_dot_path({"video": {"url": "X"}}, "missing.url") == ""`.
- [ ] `_walk_dot_path({}, "") == ""` (empty path returns empty).
- [ ] `_walk_dot_path({"v": {"url": 42}}, "v.url") == ""` (non-string terminal returns empty).
- [ ] `HostedAPIBackend.__init__` accepts `url_path: str = ""`.
- [ ] `HostedAPIBackend.result()` returns `Artifact(filename=..., url=_walk_dot_path(data, self._url_path) if self._url_path else "", meta={"job_id": job_id})`.
- [ ] `HostedAPIEngine.backend(instance, cfg)` reads `cfg["engine"]["hosted"]["url_path"]` and threads to `HostedAPIBackend.__init__(url_path=...)`.
- [ ] `HostedAPIEngine.__init__` accepts `http_get_bytes` + `ffmpeg_run` seams (same shape as ComfyUI/Diffusers).
- [ ] `HostedAPIEngine.extract_last_frame` body identical to the other two; error message names `HostedAPIEngine`.
- [ ] `examples/configs/hosted.yaml` includes `url_path: video.url`.
- [ ] README documents `url_path` under the Hosted engine section.

**Verify:** `pixi run test tests/engines/test_hosted.py -v` → all pass (existing + 6 new).

**Steps:**

- [ ] **Step 1: Write failing tests**

Append to `tests/engines/test_hosted.py`:

```python
# ---------------------------------------------------------------------------
# extract_last_frame + url_path dot-walker (Layer extract_last_frame)
# ---------------------------------------------------------------------------


from kinoforge.core.errors import FrameExtractionError


def test_walk_dot_path_resolves_nested_string() -> None:
    """video.url -> nested string lookup works.

    Bug this catches: walker only handles top-level keys.
    """
    from kinoforge.engines.hosted import _walk_dot_path

    assert _walk_dot_path({"video": {"url": "X"}}, "video.url") == "X"


def test_walk_dot_path_returns_empty_on_missing_intermediate_key() -> None:
    """Any missing step short-circuits to ''; no KeyError leaks out.

    Bug this catches: walker raises on the first missing key, breaking
    backends that point at providers returning sparse responses.
    """
    from kinoforge.engines.hosted import _walk_dot_path

    assert _walk_dot_path({"video": {"url": "X"}}, "missing.url") == ""


def test_walk_dot_path_returns_empty_on_non_string_terminal() -> None:
    """If the walked path lands on a non-string (e.g. int, dict), return ''.

    Bug this catches: walker str()-casts arbitrary values, producing
    fake URLs like 'http://...' from random response payloads.
    """
    from kinoforge.engines.hosted import _walk_dot_path

    assert _walk_dot_path({"v": {"url": 42}}, "v.url") == ""


def test_result_uses_url_path_to_backfill_artifact_url() -> None:
    """HostedAPIBackend.result() walks url_path and populates Artifact.url.

    Bug this catches: backend ignores url_path or always returns ''.
    """
    from kinoforge.engines.hosted import HostedAPIBackend

    payload = {
        "status": "done",
        "filename": "clip.mp4",
        "video": {"url": "https://cdn.fal.run/clip.mp4"},
    }
    backend = HostedAPIBackend(
        http_post=lambda url, body: {"job_id": "JOB"},
        http_get=lambda url: payload,
        endpoint="https://fal.run/fal-ai/ltx",
        probe_profile=_DEFAULT_PROBE,
        sleep=lambda s: None,
        url_path="video.url",
    )

    artifact = backend.result("JOB")

    assert artifact.url == "https://cdn.fal.run/clip.mp4"


def test_extract_last_frame_fetches_url_and_calls_ffmpeg() -> None:
    """Same shape as the other two engines, with HostedAPIEngine.

    Bug this catches: engine drops bytes or skips ffmpeg.
    """
    from kinoforge.core.interfaces import Artifact as _Artifact

    fetch_calls: list[str] = []
    ffmpeg_calls: list[tuple[list[str], bytes]] = []

    def fake_fetch(url: str) -> bytes:
        fetch_calls.append(url)
        return b"VIDEO"

    def fake_ffmpeg(argv: list[str], stdin: bytes) -> bytes:
        ffmpeg_calls.append((argv, stdin))
        return b"PNG"

    engine = HostedAPIEngine(
        http_get_bytes=fake_fetch,
        ffmpeg_run=fake_ffmpeg,
    )
    artifact = _Artifact(
        filename="clip.mp4",
        url="https://cdn.fal.run/clip.mp4",
        meta={"job_id": "X"},
    )

    out = engine.extract_last_frame(artifact)

    assert out == b"PNG"
    assert fetch_calls == ["https://cdn.fal.run/clip.mp4"]
    assert ffmpeg_calls[0][1] == b"VIDEO"


def test_extract_last_frame_raises_on_empty_url() -> None:
    """Empty url raises FrameExtractionError mentioning HostedAPIEngine.

    Bug this catches: copy-paste shared body leaves the wrong class name.
    """
    from kinoforge.core.interfaces import Artifact as _Artifact

    engine = HostedAPIEngine()
    artifact = _Artifact(filename="clip.mp4", url="", meta={})

    with pytest.raises(FrameExtractionError, match="HostedAPIEngine"):
        engine.extract_last_frame(artifact)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run test tests/engines/test_hosted.py -v -k "walk_dot_path or url_path or extract_last_frame"`
Expected: 6 failures — missing `_walk_dot_path`, missing kwargs, missing method.

- [ ] **Step 3: Implement the engine changes**

Edit `src/kinoforge/engines/hosted/__init__.py`:

**Imports** — add:

```python
from kinoforge.core import frames
from kinoforge.core.errors import (
    AuthError,
    FrameExtractionError,
    KinoforgeError,
    ValidationError,
)
```

(replacing the existing AuthError/KinoforgeError/ValidationError import)

**New helpers** — add to the "Real I/O helpers" section:

```python
def _urllib_get_bytes(url: str) -> bytes:
    """GET *url* and return the raw response body as bytes."""
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return bytes(resp.read())


def _walk_dot_path(data: dict[str, Any], path: str) -> str:
    """Walk dot-separated keys through *data*; return empty string on any miss.

    Args:
        data: The dict to walk.
        path: Dot-separated key path, e.g. ``"video.url"``. Empty path
            returns ``""``.

    Returns:
        The string at the walked path, or ``""`` if any step is missing,
        any intermediate node is not a dict, or the terminal value is not
        a string.

    Examples:
        >>> _walk_dot_path({"video": {"url": "X"}}, "video.url")
        'X'
        >>> _walk_dot_path({"video": {"url": "X"}}, "missing.url")
        ''
    """
    if not path:
        return ""
    node: Any = data
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return ""
        node = node[key]
    return node if isinstance(node, str) else ""
```

**`HostedAPIBackend.__init__`** — add `url_path` kwarg:

```python
    def __init__(
        self,
        *,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
        http_get: Callable[[str], dict[str, Any]],
        endpoint: str,
        probe_profile: ModelProfile,
        sleep: Callable[[float], None] = time.sleep,
        url_path: str = "",
    ) -> None:
```

Body assignment after `self._sleep = sleep`:

```python
        self._url_path = url_path
```

**`HostedAPIBackend.result()`** — at line 225, change:

```python
            if data.get("status") == "done":
                filename = str(data.get("filename", ""))
                return Artifact(filename=filename, meta={"job_id": job_id})
```

to:

```python
            if data.get("status") == "done":
                filename = str(data.get("filename", ""))
                url = _walk_dot_path(data, self._url_path)
                return Artifact(
                    filename=filename, url=url, meta={"job_id": job_id}
                )
```

**`HostedAPIEngine.__init__`** — add `http_get_bytes` and `ffmpeg_run` seams (same shape):

```python
        http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
        ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run,
```

Body:

```python
        self._http_get_bytes = http_get_bytes
        self._ffmpeg_run = ffmpeg_run
```

**`HostedAPIEngine.backend()`** — thread `url_path` from cfg. Change the existing body from:

```python
        endpoint: str = str(hosted_cfg.get("endpoint", ""))
        return HostedAPIBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            endpoint=endpoint,
            probe_profile=self._probe,
            sleep=self._sleep,
        )
```

to:

```python
        endpoint: str = str(hosted_cfg.get("endpoint", ""))
        url_path: str = str(hosted_cfg.get("url_path", ""))
        return HostedAPIBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            endpoint=endpoint,
            probe_profile=self._probe,
            sleep=self._sleep,
            url_path=url_path,
        )
```

**New `extract_last_frame`** — at end of `HostedAPIEngine`:

```python
    def extract_last_frame(self, artifact: Artifact) -> bytes:
        """Fetch the rendered video bytes via HTTP and decode the last frame.

        The artifact's URL is populated by :meth:`HostedAPIBackend.result`
        from ``cfg["engine"]["hosted"]["url_path"]`` walked over the API
        response body.  Providers vary on response shape; configure the
        path per provider.

        Args:
            artifact: A clip Artifact with ``url`` populated.

        Returns:
            PNG-encoded last frame as bytes.

        Raises:
            FrameExtractionError: ``artifact.url`` is empty (url_path
                unset, missing, or pointed at non-string), or the fetch or
                ffmpeg decode failed.
        """
        if not artifact.url:
            raise FrameExtractionError(
                f"{type(self).__name__}: artifact.url is empty; "
                "cannot fetch video bytes"
            )
        video_bytes = self._http_get_bytes(artifact.url)
        return frames.ffmpeg_last_frame(video_bytes, run=self._ffmpeg_run)
```

- [ ] **Step 4: Update example config**

Edit `examples/configs/hosted.yaml`. Under the `engine.hosted` block, add a `url_path` line. Read the file first to place it adjacent to the existing keys. Typical placement:

```yaml
engine:
  hosted:
    provider: fal
    endpoint: https://fal.run/fal-ai/ltx-video
    model: ltx-2
    api_key_env: FAL_KEY
    health_url: https://fal.run/health
    url_path: video.url        # NEW: where the rendered-video URL lives in the response
```

- [ ] **Step 5: Update README**

Find the Hosted section in `README.md`. Append a subsection or extend the existing config docs:

```markdown
### Hosted response URL — `url_path`

Hosted providers vary on response body shape. Configure
`engine.hosted.url_path` as a dot-separated path into the
`/status/{job_id}` response body where the rendered video's URL lives.

Examples:

| Provider response | `url_path` |
|---|---|
| `{"video": {"url": "..."}}` | `video.url` |
| `{"output_url": "..."}` | `output_url` |

The walker returns `""` for missing paths or non-string terminals; the
engine then raises `FrameExtractionError` rather than fetching a bogus
URL. Array indexing (e.g. `results[0].url`) is not supported.
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pixi run test tests/engines/test_hosted.py -v`
Expected: all pass.

- [ ] **Step 7: Run the full suite**

Run: `pixi run test`
Expected: all pass.

- [ ] **Step 8: Lint/format/typecheck**

Run: `pixi run pre-commit run --files src/kinoforge/engines/hosted/__init__.py tests/engines/test_hosted.py examples/configs/hosted.yaml README.md`
Expected: all hooks pass.

- [ ] **Step 9: Commit**

```bash
git add src/kinoforge/engines/hosted/__init__.py tests/engines/test_hosted.py examples/configs/hosted.yaml README.md
git commit -m "feat(hosted): url_path cfg + dot-walker + extract_last_frame impl

HostedAPIBackend learns a url_path (e.g. 'video.url') threaded from
cfg[engine][hosted][url_path]. result() walks the polled response with
_walk_dot_path and populates Artifact.url. Walker is dict-only;
non-string terminals and missing keys both return ''.

HostedAPIEngine gains http_get_bytes + ffmpeg_run seams; extract_last_frame
is the uniform 5-liner. README + example config document the new key."
```

---

## Task 7: PROGRESS update + Layer F entry in "Known limitations"

**Goal:** Update `PROGRESS.md` to mark the per-engine `extract_last_frame` follow-up complete and add the deferred Layer F (engine submit() asset-wiring) as a new entry under "Known limitations & follow-ups".

**Files:**
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] The first bullet under "Per-engine follow-ups" — the one about `ComfyUIEngine`, `DiffusersEngine`, `HostedAPIEngine` inheriting the raising default — replaced or removed (per-engine impl now ships).
- [ ] New "Architectural follow-ups" entry: `Layer F: engine submit() does not consume seg-0 assets — non-native multi-segment runs persist tail PNGs via the stage but next render reads only job.spec; tail asset is currently dead weight. Each engine needs per-asset-role spec-injection (ComfyUI LoadImage node, Diffusers init_image param, Hosted provider-specific URL field).`
- [ ] "Single next action" section updated to reflect that this layer shipped; recommend Layer F or one of the prior options as next.
- [ ] A new "Phase 15 — per-engine extract_last_frame (post-MVP Layer E)" section in the Post-MVP block with the 6 implementation task commits listed.

**Verify:** `grep -A2 "Layer F" PROGRESS.md` returns the new entry; `grep "Phase 15" PROGRESS.md` returns the new section header.

**Steps:**

- [ ] **Step 1: Read the current PROGRESS state**

Read `PROGRESS.md` end-to-end (it's ~211 lines).

- [ ] **Step 2: Remove the now-obsolete follow-up bullet**

Find the "Per-engine follow-ups (no GitHub issue yet):" section. Remove the bullet about `extract_last_frame` raising on non-native runs. If that's the only bullet under the heading, also remove the heading.

- [ ] **Step 3: Add Layer F entry under "Architectural follow-ups"**

Append a new bullet to the "Architectural follow-ups:" list:

```markdown
- **Layer F: engine `submit()` ignores seg-0 assets.** The non-native chain now
  persists tail PNGs (via the stage's `store.put_bytes`) and injects a
  `ConditioningAsset` into `next_job.segments[0]`, but each engine's `submit()`
  body reads only `job.spec` — the tail asset is currently dead weight at
  render time. Wiring per asset role into each engine's spec template is the
  next layer: ComfyUI `LoadImage` node injection, Diffusers `init_image` param,
  Hosted provider-specific URL field.
```

- [ ] **Step 4: Update "Single next action"**

Replace the existing "Single next action" content with:

```markdown
**Layer E (per-engine `extract_last_frame`) complete.** All acceptance
criteria met across ComfyUI, Diffusers, and Hosted; shared
`core/frames.ffmpeg_last_frame` decoder; stage persists tail PNGs into
the run_id namespace via `store.put_bytes`. `pixi run test` reports
N passed; mypy strict + ruff + pre-commit clean.

**Next: pick from the layered roadmap.** Two plausible next layers:

1. **Layer F — engine asset-wiring (`submit()` consumes seg-0 assets).**
   Closes the rest of the non-native multi-segment story: each engine
   reads `job.segments[0].assets`, finds the `init_image` role, and
   folds its `ref.uri` into the spec/graph the engine submits. Surface
   is per-engine: ComfyUI `LoadImage` node injection, Diffusers
   `init_image` param, Hosted provider-specific URL field.

2. **Layer #4 — Concurrent backend scheduler (GitHub issue #3).**
   Drop-in `ConcurrentPool` behind the existing `BackendPool` ABC. Pure
   dispatch concern; no other modules touched.

Begin the chosen layer with the
`superpowers-extended-cc:brainstorming` skill.
```

(Update the test-pass count `N` to whatever `pixi run test` reports at this task's verify step.)

- [ ] **Step 5: Add Phase 15 section under Post-MVP**

Append after the existing "Phase 14" subsection:

```markdown
### Phase 15 — per-engine extract_last_frame (post-MVP Layer E)
- [x] Task 1: `FrameExtractionError` + `core/frames.ffmpeg_last_frame` helper — commit `<sha1>`
- [x] Task 2: ABC contract change + helper simplification + FakeEngine bytes return — commit `<sha2>`
- [x] Task 3: GenerateClipStage non-native rewiring — commit `<sha3>`
- [x] Task 4: ComfyUI result() URL backfill + extract_last_frame + 2 seams — commit `<sha4>`
- [x] Task 5: Diffusers result() URL passthrough + extract_last_frame + 2 seams + server contract doc — commit `<sha5>`
- [x] Task 6: Hosted url_path cfg + dot-walker + result() backfill + extract_last_frame + 2 seams — commit `<sha6>`
```

Replace `<sha1>`–`<sha6>` with the actual commit SHAs from `git log --oneline -10`. Use a real lookup; do not invent SHAs.

- [ ] **Step 6: Verify**

Run: `grep -A2 "Layer F" PROGRESS.md`
Expected: the new entry under "Architectural follow-ups".

Run: `grep "Phase 15" PROGRESS.md`
Expected: the new section header.

- [ ] **Step 7: Lint check**

Run: `pixi run pre-commit run --files PROGRESS.md`
Expected: hooks pass (markdown-specific hooks are typically just trailing-whitespace + end-of-file-fixer).

- [ ] **Step 8: Commit**

```bash
git add PROGRESS.md
git commit -m "docs(progress): mark per-engine extract_last_frame (Layer E) complete

Removes the Per-engine follow-ups bullet about extract_last_frame raising,
adds Layer F (engine submit() ignores seg-0 assets) under Architectural
follow-ups, refreshes Single next action, adds Phase 15 to Post-MVP."
```

---

## Self-review notes

**Spec coverage:** Every section of the spec maps to a task (§4.1→T1, §4.2→T1, §4.3→T2, §4.4→T2, §4.5→T3, §4.6→T4/T5/T6, §4.7→T4/T5/T6, §4.8→T2, §7 risks → README docs in T5/T6, PROGRESS in T7).

**Placeholder scan:** No "TBD", no "implement appropriate error handling", no "similar to Task N". Every code block is the actual code or the actual edit.

**Type consistency:** `extract_last_frame(artifact) -> bytes` consistent across T2 (ABC), T2 (FakeEngine), T4/T5/T6 (three real engines). `inject_tail_frame(next_job, tail_asset)` consistent across T2 (helper rewrite) and T3 (stage call). `_walk_dot_path(data, path) -> str` consistent across T6 (impl) and T6 (tests).

**No user-gate tasks** — this is a regular implementation plan. No gate-language triggers fired during the scan.
