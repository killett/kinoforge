# Video Upscaling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship engine-agnostic video upscaling with SeedVR2 3B FP8 as the default engine, a new `kinoforge upscale` CLI command, and pipeline-stage integration triggered by an `upscale:` cfg block.

**Architecture:** New `UpscalerEngine` ABC + registry, sibling to `GenerationEngine` / `ImageEngine`. SeedVR2 runs as direct upstream Python on the same FastAPI server as Wan T2V via new `/upscale` + `/upscale/status/{id}` endpoints. An in-process model registry with LRU CPU eviction shares VRAM between Wan and SeedVR2; `CapabilityKey` gains `stages` + `upscaler` + `upscaler_precision` factors with byte-equal hash backward-compat for legacy ledger entries.

**Tech Stack:** Python 3.13 / pydantic v2 / pytest / FastAPI / diffusers / huggingface-hub (existing kinoforge stack); upstream `ByteDance-Seed/SeedVR` pinned by commit SHA for the inference module.

**User decisions (already made):**
- Engine surface: new `UpscalerEngine` ABC + `register_upscaler` registry.
- Pod topology: same FastAPI on same pod, add `/upscale` + `/upscale/status/{id}`.
- CLI shape: `kinoforge upscale` (standalone) + cfg `upscale:` block triggers in-pipeline stage; no `--upscale` flag on `generate`.
- Scale grammar: polymorphic `ScaleTarget` — `Nx` works in v1, `Np` parses then raises `NotYetImplementedError`.
- SeedVR2 runtime: direct Python from upstream `ByteDance-Seed/SeedVR` repo, not ComfyUI.
- Warm-reuse: add `stages` + `upscaler` + `upscaler_precision` factors to `CapabilityKey`; ledger backward-compatible via conditional-extend in `derive()`.
- VRAM: server-side LRU CPU/disk eviction across stages with a hard-floor `VRAMEvictionFailed` when the target model alone exceeds GPU capacity.
- Default variant: SeedVR2 3B FP8.

**Spec:** `docs/superpowers/specs/2026-06-28-video-upscaling-design.md` (commit `3b0b450`)

**Live-spend memories in effect:**
- `feedback_autonomous_no_gates` — live smokes pre-authorized; mechanical preflight only; no `userGate` tags on smoke tasks.
- `feedback_use_no_reuse_for_one_shots` — `--no-reuse` on one-shot smokes; verify with `kinoforge list` after exit.
- `feedback_standard_test_prompt` — generation half of multi-stage smoke reads `/workspace/examples/configs/prompts/field-realistic.txt` verbatim.
- `feedback_proactive_pod_stats` — poll RunPod runtime GPU/CPU/mem every 60-90s during live smokes; bail early on idle-pod signature.
- `commit RED scaffolds before any live spend` (CLAUDE.md) — RED smoke commits land before live-spend tasks.

---

## Task list and dependencies

```
T0 ScaleTarget + errors                (foundation, no deps)
T1 UpscaleJob/UpscaleResult types      (deps: T0)
T2 UpscalerEngine ABC + registry       (deps: T1)
T3 CapabilityKey stages factor         (foundation, no deps — runs parallel to T0)
T4 WarmAttachKey stages factor         (deps: T3)
T5 UpscaleConfig + SeedVR2EngineConfig (deps: T0, T2, T3)
T6 UpscaleStage (pipeline)             (deps: T2)
T7 Warm-matcher subset pass            (deps: T3, T5)
T8 SeedVR2Runtime wrapper              (deps: T2)
T9 _fetch_weights CLI module           (deps: T2)
T10 SeedVR2Engine (UpscalerEngine impl)(deps: T2, T8, T9)
T11 Server LRU registry                (deps: none — server-only)
T12 Server /upscale endpoints          (deps: T11)
T13 /health payload extension          (deps: T12)
T14 /health-driven matcher preflight   (deps: T7, T13)
T15 _cmd_upscale CLI wiring            (deps: T5, T10, T14)
T16 _adapters self-register + docs     (deps: T15)
T17 Live smoke RED scaffold (upscale)  (deps: T16)
T18 Live smoke GREEN (upscale-only)    (deps: T17)
T19 Live smoke (wan+upscale warm-reuse)(deps: T18)
T20 PROGRESS.md close                  (deps: T19)
```

---

## Task 0: `ScaleTarget` parser + `UnsupportedScaleError` + `NotYetImplementedError`

**Goal:** Pure-function scale-target parser plus two new exception types. Zero kinoforge dependencies; ideal TDD opener.

**Files:**
- Create: `src/kinoforge/core/scale_target.py`
- Modify: `src/kinoforge/core/errors.py`
- Test: `tests/core/test_scale_target.py`

**Acceptance Criteria:**
- [ ] `ScaleTarget.parse("2x")` → `ScaleTarget(kind="factor", value=2.0)`
- [ ] `ScaleTarget.parse("4x")` → `ScaleTarget(kind="factor", value=4.0)`
- [ ] `ScaleTarget.parse("1.5x")` → `ScaleTarget(kind="factor", value=1.5)`
- [ ] `ScaleTarget.parse("1080p")` → `ScaleTarget(kind="height", value=1080)`
- [ ] `ScaleTarget.parse("720p")` → `ScaleTarget(kind="height", value=720)`
- [ ] `ScaleTarget.parse("2160p")` → `ScaleTarget(kind="height", value=2160)`
- [ ] `ScaleTarget.parse("bogus")` raises `ValueError` with message containing "expected `Nx` or `Np` token"
- [ ] `ScaleTarget.parse("0x")` raises `ValueError` (zero/negative factor refused)
- [ ] `ScaleTarget.parse("-1x")` raises `ValueError`
- [ ] `ScaleTarget(kind="factor", value=2.0)` is frozen — assignment raises `FrozenInstanceError`
- [ ] `UnsupportedScaleError(scale, engine_name)` subclasses `KinoforgeError`; `str(err)` mentions both fields
- [ ] `NotYetImplementedError(message)` subclasses `KinoforgeError`

**Verify:** `pixi run pytest tests/core/test_scale_target.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/core/__init__.py` (empty) if not present, then `tests/core/test_scale_target.py`:

```python
"""Tests for ScaleTarget polymorphic parser."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget


class TestParseFactor:
    """Behaviour: parse `Nx` token shapes."""

    def test_parse_2x(self) -> None:
        assert ScaleTarget.parse("2x") == ScaleTarget(kind="factor", value=2.0)

    def test_parse_4x(self) -> None:
        assert ScaleTarget.parse("4x") == ScaleTarget(kind="factor", value=4.0)

    def test_parse_fractional(self) -> None:
        assert ScaleTarget.parse("1.5x") == ScaleTarget(kind="factor", value=1.5)


class TestParseHeight:
    """Behaviour: parse `Np` token shapes (parses now; consumer raises later)."""

    @pytest.mark.parametrize("raw,h", [("1080p", 1080), ("720p", 720), ("2160p", 2160)])
    def test_parse_height_tokens(self, raw: str, h: int) -> None:
        assert ScaleTarget.parse(raw) == ScaleTarget(kind="height", value=float(h))


class TestParseRejects:
    """Behaviour: malformed tokens raise ValueError."""

    @pytest.mark.parametrize("raw", ["bogus", "2", "x", "px", "1080", "1080P", "2X", ""])
    def test_rejects_malformed(self, raw: str) -> None:
        with pytest.raises(ValueError, match="expected `Nx` or `Np` token"):
            ScaleTarget.parse(raw)

    @pytest.mark.parametrize("raw", ["0x", "-1x", "0p", "-1080p"])
    def test_rejects_non_positive(self, raw: str) -> None:
        with pytest.raises(ValueError):
            ScaleTarget.parse(raw)


class TestFrozenDataclass:
    def test_assignment_raises(self) -> None:
        t = ScaleTarget(kind="factor", value=2.0)
        with pytest.raises(FrozenInstanceError):
            t.kind = "height"  # type: ignore[misc]


class TestUnsupportedScaleError:
    def test_message_mentions_both(self) -> None:
        err = UnsupportedScaleError(
            scale=ScaleTarget(kind="factor", value=3.0), engine_name="seedvr2"
        )
        msg = str(err)
        assert "seedvr2" in msg
        assert "3" in msg


class TestNotYetImplementedError:
    def test_is_kinoforge_error(self) -> None:
        from kinoforge.core.errors import KinoforgeError

        assert issubclass(NotYetImplementedError, KinoforgeError)
```

- [ ] **Step 2: Run test to confirm RED**

```
pixi run pytest tests/core/test_scale_target.py -v
```

Expected: ImportError or "No module named kinoforge.core.scale_target" — every test fails at collection.

- [ ] **Step 3: Add the two exceptions to `core/errors.py`**

Append to `src/kinoforge/core/errors.py`:

```python
class NotYetImplementedError(KinoforgeError):
    """Raised when a code path is intentionally deferred to a future session.

    Distinct from stdlib NotImplementedError (ABC abstract method) — this is
    explicit "we chose to parse-then-raise instead of refuse-at-parse-time"
    semantics. See ScaleTarget(kind="height").
    """


class UnsupportedScaleError(KinoforgeError):
    """Raised when an UpscalerEngine refuses a ScaleTarget its model can't serve.

    Carries enough context for post-mortem without session memory.
    """

    def __init__(self, scale: object, engine_name: str) -> None:
        super().__init__(
            f"engine {engine_name!r} does not support scale {scale!r}; "
            f"declared supported_scales gates this refusal"
        )
        self.scale = scale
        self.engine_name = engine_name
```

- [ ] **Step 4: Create `src/kinoforge/core/scale_target.py`**

```python
"""Polymorphic scale target for video upscaling.

v1 supports `kind="factor"` (any positive float). `kind="height"` parses but
is refused by every v1 consumer (`UpscaleStage`, `SeedVR2Runtime`) with
NotYetImplementedError. The CLI surface is final on day one; a future
session adds height-target arithmetic plus the swappable downscale method.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# Anchored regex — accepts "2x", "1.5x", "1080p". Rejects trailing junk.
_FACTOR_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)x$")
_HEIGHT_RE = re.compile(r"^([0-9]+)p$")


@dataclass(frozen=True)
class ScaleTarget:
    """Polymorphic scale target.

    Grammar:
      "2x", "4x", "1.5x"   -> ScaleTarget(kind="factor", value=2.0)
      "1080p", "720p"      -> ScaleTarget(kind="height", value=1080.0)

    v1 engines MUST raise NotYetImplementedError on kind="height".
    """

    kind: Literal["factor", "height"]
    value: float

    @classmethod
    def parse(cls, raw: str) -> ScaleTarget:
        """Parse a raw CLI / cfg token into a ScaleTarget.

        Args:
            raw: User-supplied scale token (e.g. "2x", "1080p").

        Returns:
            Parsed ScaleTarget. ``kind="factor"`` or ``kind="height"``.

        Raises:
            ValueError: Token does not match the `Nx` / `Np` grammar, or
                resolves to a non-positive value.
        """
        m = _FACTOR_RE.match(raw)
        if m is not None:
            value = float(m.group(1))
            if value <= 0:
                raise ValueError(
                    f"scale factor must be positive; got {raw!r} -> {value}"
                )
            return cls(kind="factor", value=value)

        m = _HEIGHT_RE.match(raw)
        if m is not None:
            value = int(m.group(1))
            if value <= 0:
                raise ValueError(
                    f"scale height must be positive; got {raw!r} -> {value}"
                )
            return cls(kind="height", value=float(value))

        raise ValueError(
            f"unrecognised scale token {raw!r}; expected `Nx` or `Np` token "
            f"(e.g. '2x', '1.5x', '1080p')"
        )
```

- [ ] **Step 5: Run tests to confirm GREEN**

```
pixi run pytest tests/core/test_scale_target.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/core/scale_target.py \
  src/kinoforge/core/errors.py \
  tests/core/test_scale_target.py
git add src/kinoforge/core/scale_target.py \
        src/kinoforge/core/errors.py \
        tests/core/test_scale_target.py \
        tests/core/__init__.py
git commit -m "feat(upscale): ScaleTarget polymorphic parser + UnsupportedScaleError + NotYetImplementedError"
```

```json:metadata
{"files": ["src/kinoforge/core/scale_target.py", "src/kinoforge/core/errors.py", "tests/core/test_scale_target.py"], "verifyCommand": "pixi run pytest tests/core/test_scale_target.py -v", "acceptanceCriteria": ["ScaleTarget.parse('2x') returns factor 2.0", "ScaleTarget.parse('1080p') returns height 1080", "ScaleTarget.parse rejects bogus tokens", "UnsupportedScaleError subclasses KinoforgeError", "NotYetImplementedError subclasses KinoforgeError"], "modelTier": "mechanical"}
```

---

## Task 1: `UpscaleJob` and `UpscaleResult` dataclasses

**Goal:** Engine-agnostic job/result shapes for video-in/video-out work. No prompt, no segments — minimal surface.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (insert after `GenerationJob` class)
- Test: `tests/core/test_upscale_types.py`

**Acceptance Criteria:**
- [ ] `UpscaleJob(source=art, scale=ScaleTarget(kind="factor", value=2.0))` instantiates with empty `params`
- [ ] `UpscaleJob` is frozen
- [ ] `UpscaleResult(artifact=art, input_resolution=(640,480), output_resolution=(1280,960), elapsed_s=12.3)` instantiates with empty `engine_meta`
- [ ] `UpscaleResult` is frozen
- [ ] Both types import cleanly from `kinoforge.core.interfaces`

**Verify:** `pixi run pytest tests/core/test_upscale_types.py -v` → all pass; `pixi run mypy src/kinoforge/core/interfaces.py` clean.

**Steps:**

- [ ] **Step 1: RED — create `tests/core/test_upscale_types.py`**

```python
"""Tests for UpscaleJob and UpscaleResult shapes."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from kinoforge.core.interfaces import Artifact, UpscaleJob, UpscaleResult
from kinoforge.core.scale_target import ScaleTarget


def _art() -> Artifact:
    return Artifact(uri="file:///tmp/in.mp4", sha256="0" * 64, size=0)


class TestUpscaleJob:
    def test_defaults(self) -> None:
        j = UpscaleJob(source=_art(), scale=ScaleTarget(kind="factor", value=2.0))
        assert j.params == {}

    def test_frozen(self) -> None:
        j = UpscaleJob(source=_art(), scale=ScaleTarget(kind="factor", value=2.0))
        with pytest.raises(FrozenInstanceError):
            j.scale = ScaleTarget(kind="factor", value=4.0)  # type: ignore[misc]


class TestUpscaleResult:
    def test_defaults(self) -> None:
        r = UpscaleResult(
            artifact=_art(),
            input_resolution=(640, 480),
            output_resolution=(1280, 960),
            elapsed_s=12.3,
        )
        assert r.engine_meta == {}

    def test_frozen(self) -> None:
        r = UpscaleResult(
            artifact=_art(),
            input_resolution=(640, 480),
            output_resolution=(1280, 960),
            elapsed_s=12.3,
        )
        with pytest.raises(FrozenInstanceError):
            r.elapsed_s = 0.0  # type: ignore[misc]
```

- [ ] **Step 2: Verify RED**

```
pixi run pytest tests/core/test_upscale_types.py -v
```

Expected: `ImportError: cannot import name 'UpscaleJob' from 'kinoforge.core.interfaces'`.

- [ ] **Step 3: Add types to `interfaces.py`**

Find the existing `class GenerationJob:` block (around line 550). Insert immediately AFTER it (before `class ModelProfileProvider(ABC):`):

```python
@dataclass(frozen=True)
class UpscaleJob:
    """One unit of upscale work — engine-agnostic.

    No prompt, no segments, no LoRA stack — upscaling is video-in / video-out.

    Attributes:
        source: Input video Artifact (uri set by ArtifactStore or pointing at a
            local path readable by the engine).
        scale: ScaleTarget. v1 engines MUST raise NotYetImplementedError on
            ``kind="height"``.
        params: Engine-specific overrides (e.g. tile_size, steps, denoise);
            engines validate via ``validate_spec``.
    """

    source: Artifact
    scale: ScaleTarget
    params: dict = field(default_factory=dict)  # type: ignore[type-arg]


@dataclass(frozen=True)
class UpscaleResult:
    """Output of one upscale job.

    Attributes:
        artifact: Rendered upscaled video.
        input_resolution: ``(width, height)`` measured from the source clip.
        output_resolution: ``(width, height)`` of the rendered output.
        elapsed_s: Wall-clock seconds spent inside the engine.
        engine_meta: Engine-specific telemetry (e.g. SeedVR2 tile count,
            denoise steps used). Free-form open dict.
    """

    artifact: Artifact
    input_resolution: tuple[int, int]
    output_resolution: tuple[int, int]
    elapsed_s: float
    engine_meta: dict = field(default_factory=dict)  # type: ignore[type-arg]
```

Add to the imports at the top of `interfaces.py`:

```python
from kinoforge.core.scale_target import ScaleTarget
```

(Insert in alphabetical position among existing `from kinoforge.core.*` imports.)

- [ ] **Step 4: GREEN**

```
pixi run pytest tests/core/test_upscale_types.py -v
pixi run mypy src/kinoforge/core/interfaces.py
```

Expected: all tests pass; mypy clean.

- [ ] **Step 5: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/core/interfaces.py \
  tests/core/test_upscale_types.py
git add src/kinoforge/core/interfaces.py tests/core/test_upscale_types.py
git commit -m "feat(upscale): UpscaleJob + UpscaleResult dataclasses in core.interfaces"
```

```json:metadata
{"files": ["src/kinoforge/core/interfaces.py", "tests/core/test_upscale_types.py"], "verifyCommand": "pixi run pytest tests/core/test_upscale_types.py -v && pixi run mypy src/kinoforge/core/interfaces.py", "acceptanceCriteria": ["UpscaleJob and UpscaleResult import from kinoforge.core.interfaces", "Both are frozen dataclasses", "Defaults: params={}, engine_meta={}"], "modelTier": "mechanical"}
```

---

## Task 2: `UpscalerEngine` ABC + `register_upscaler` registry + remaining new exceptions

**Goal:** The engine-agnostic contract every upscaler must satisfy, plus the registry that adapters self-register against, plus the remaining new exception types from §8.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py`
- Modify: `src/kinoforge/core/registry.py`
- Modify: `src/kinoforge/core/errors.py`
- Test: `tests/core/test_upscaler_registry.py`

**Acceptance Criteria:**
- [ ] `UpscalerEngine` is `ABC` with abstract `provision`, `upscale`, `validate_spec`, `model_identity`; `name`, `requires_compute`, `requires_local_weights`, `supported_scales` are class attributes
- [ ] `register_upscaler("name", factory)` stores the factory; second call with same name raises `UnknownAdapter`-shaped error consistent with `register_engine`
- [ ] `get_upscaler("name")()` returns engine; `get_upscaler("missing")` raises `UnknownAdapter`
- [ ] `upscaler_names()` returns a sorted list of registered names
- [ ] `UpscaleFailed`, `VRAMEvictionFailed`, `StageMismatch` subclass `KinoforgeError` and carry the context fields named in spec §8

**Verify:** `pixi run pytest tests/core/test_upscaler_registry.py -v` → all pass.

**Steps:**

- [ ] **Step 1: RED — `tests/core/test_upscaler_registry.py`**

```python
"""Tests for register_upscaler / get_upscaler / upscaler_names."""

from __future__ import annotations

from typing import cast

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import (
    StageMismatch,
    UnknownAdapter,
    UpscaleFailed,
    VRAMEvictionFailed,
)
from kinoforge.core.interfaces import UpscalerEngine


class _FakeEngine(UpscalerEngine):  # minimal concrete impl for registry test
    name = "_fake_upscaler"
    requires_compute = False
    requires_local_weights = False
    supported_scales = ()

    def provision(self, instance, cfg, *, cancel_token=None):  # type: ignore[no-untyped-def]
        return None

    def upscale(self, instance, job, cfg, *, cancel_token=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    def validate_spec(self, job):  # type: ignore[no-untyped-def]
        return None

    def model_identity(self, cfg):  # type: ignore[no-untyped-def]
        return "_fake_upscaler"


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot/restore the module-level _upscalers dict so tests don't leak."""
    snap = dict(registry._upscalers)
    registry._upscalers.clear()
    yield
    registry._upscalers.clear()
    registry._upscalers.update(snap)


class TestRegisterUpscaler:
    def test_register_and_get(self) -> None:
        registry.register_upscaler("fake", _FakeEngine)
        eng = registry.get_upscaler("fake")()
        assert eng.name == "_fake_upscaler"

    def test_duplicate_raises(self) -> None:
        registry.register_upscaler("fake", _FakeEngine)
        with pytest.raises(UnknownAdapter, match="already registered"):
            registry.register_upscaler("fake", _FakeEngine)

    def test_get_missing_raises(self) -> None:
        with pytest.raises(UnknownAdapter, match="no upscaler registered"):
            registry.get_upscaler("nope")

    def test_names_sorted(self) -> None:
        registry.register_upscaler("zeta", _FakeEngine)
        registry.register_upscaler("alpha", _FakeEngine)
        assert registry.upscaler_names() == ["alpha", "zeta"]


class TestNewErrors:
    def test_upscale_failed_carries_context(self) -> None:
        err = UpscaleFailed(job_id="j-123", server_error="cuda OOM")
        assert "j-123" in str(err)
        assert "cuda OOM" in str(err)

    def test_vram_eviction_failed_carries_context(self) -> None:
        err = VRAMEvictionFailed(model="seedvr2-7b", reason="target exceeds GPU")
        assert "seedvr2-7b" in str(err)
        assert err.model == "seedvr2-7b"

    def test_stage_mismatch_carries_axes(self) -> None:
        err = StageMismatch(want=("t2v", "upscale"), have=("t2v",))
        assert err.want == ("t2v", "upscale")
        assert err.have == ("t2v",)
```

**Note on `UnknownAdapter`:** confirm via `rg "class UnknownAdapter" src/kinoforge/core/errors.py` that this exists. If `register_engine`'s duplicate path uses a different error (e.g. `ValueError`), match that — the test's `pytest.raises` should pin whatever the existing `register_engine` raises. Update the test type to match.

- [ ] **Step 2: Confirm RED**

```
pixi run pytest tests/core/test_upscaler_registry.py -v
```

Expected: `AttributeError: module 'kinoforge.core.registry' has no attribute '_upscalers'` and `ImportError: UpscalerEngine`.

- [ ] **Step 3: Add `UpscalerEngine` to `interfaces.py`**

Find the existing `class GenerationEngine(ABC):` block. Insert AFTER its closing methods (after `wait_for_ready`, around line 815):

```python
class UpscalerEngine(ABC):
    """A swappable video upscaler; owns env setup; declares supported scales.

    No prompt, no segments, no LoRA stack — upscaling is video-in/video-out.
    Separate from GenerationEngine because the surfaces don't overlap.

    Attributes:
        name: Registry key (e.g. "seedvr2").
        requires_compute: True when this engine needs a remote pod.
        requires_local_weights: True when the engine downloads weights into
            the pod's weight directory.
        supported_scales: Declared support; matcher pre-flight + validate_spec
            consult this. Empty tuple means "engine claims to accept any
            ScaleTarget" (use sparingly).
    """

    name: str
    requires_compute: bool
    requires_local_weights: bool
    supported_scales: tuple[ScaleTarget, ...]

    @abstractmethod
    def provision(  # noqa: D102
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None: ...

    @abstractmethod
    def upscale(  # noqa: D102
        self,
        instance: Instance | None,
        job: UpscaleJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> UpscaleResult: ...

    @abstractmethod
    def validate_spec(self, job: UpscaleJob) -> None:
        """Raise on engine-unsupportable job. SeedVR2 3B refuses scale='3x'."""
        ...

    @abstractmethod
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Sink-filename slug (e.g. 'seedvr2-3b-fp8'). MUST NOT raise on missing fields."""
        ...

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Emit boot payload. Default raises; remote-capable engines override."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support remote provisioning"
        )

    def attach_get_instance(
        self,
        get_instance: Callable[[str], Instance],
    ) -> None:
        """Wire provider lookup; mirrors GenerationEngine.attach_get_instance."""
        self._get_instance = get_instance  # noqa: SLF001
```

- [ ] **Step 4: Add registry functions to `registry.py`**

After the existing `_engines` declaration block, add:

```python
_upscalers: dict[str, Callable[[], UpscalerEngine]] = {}
```

After `register_engine` / `get_engine`, append:

```python
def register_upscaler(name: str, factory: Callable[[], UpscalerEngine]) -> None:
    """Register an upscaler factory under ``name``.

    Args:
        name: Registry key (e.g. "seedvr2").
        factory: Zero-arg callable returning an UpscalerEngine instance.

    Raises:
        UnknownAdapter: ``name`` already registered (reusing the same error
            type as register_engine for shape consistency).
    """
    if name in _upscalers:
        raise UnknownAdapter(f"upscaler {name!r} already registered")
    _upscalers[name] = factory


def get_upscaler(name: str) -> Callable[[], UpscalerEngine]:
    """Return the factory for ``name``.

    Raises:
        UnknownAdapter: No upscaler registered under ``name``.
    """
    try:
        return _upscalers[name]
    except KeyError as exc:
        raise UnknownAdapter(
            f"no upscaler registered as {name!r}; "
            f"known: {sorted(_upscalers)}"
        ) from exc


def upscaler_names() -> list[str]:
    """Return all registered upscaler names, sorted."""
    return sorted(_upscalers)
```

Add `UpscalerEngine` to the existing `from kinoforge.core.interfaces import (...)` block.

**If `UnknownAdapter` does not exist** in `core/errors.py`: confirm via `rg "class UnknownAdapter" src/kinoforge/core/errors.py`. If it's defined under a different name (e.g. `UnknownEngine`), use whatever `register_engine` raises today. Adjust the test in Step 1 to match.

- [ ] **Step 5: Add `UpscaleFailed`, `VRAMEvictionFailed`, `StageMismatch` to `core/errors.py`**

Append:

```python
class UpscaleFailed(KinoforgeError):
    """Server-side upscale job entered an error state."""

    def __init__(self, job_id: str, server_error: str) -> None:
        super().__init__(
            f"upscale job {job_id} failed on server: {server_error}"
        )
        self.job_id = job_id
        self.server_error = server_error


class VRAMEvictionFailed(KinoforgeError):
    """Eviction policy exhausted all targets and the requested model still doesn't fit."""

    def __init__(self, model: str, reason: str) -> None:
        super().__init__(f"VRAM eviction failed for {model}: {reason}")
        self.model = model
        self.reason = reason


class StageMismatch(KinoforgeError):
    """Pod /health capabilities disagree with cfg's stages requirement."""

    def __init__(self, want: tuple[str, ...], have: tuple[str, ...]) -> None:
        super().__init__(
            f"pod missing stages: want={want!r}, have={have!r}"
        )
        self.want = want
        self.have = have
```

- [ ] **Step 6: GREEN**

```
pixi run pytest tests/core/test_upscaler_registry.py -v
pixi run mypy src/kinoforge/core/interfaces.py src/kinoforge/core/registry.py src/kinoforge/core/errors.py
```

Expected: all tests pass; mypy clean.

- [ ] **Step 7: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/core/interfaces.py \
  src/kinoforge/core/registry.py \
  src/kinoforge/core/errors.py \
  tests/core/test_upscaler_registry.py
git add src/kinoforge/core/interfaces.py \
        src/kinoforge/core/registry.py \
        src/kinoforge/core/errors.py \
        tests/core/test_upscaler_registry.py
git commit -m "feat(upscale): UpscalerEngine ABC + register_upscaler registry + UpscaleFailed/VRAMEvictionFailed/StageMismatch errors"
```

```json:metadata
{"files": ["src/kinoforge/core/interfaces.py", "src/kinoforge/core/registry.py", "src/kinoforge/core/errors.py", "tests/core/test_upscaler_registry.py"], "verifyCommand": "pixi run pytest tests/core/test_upscaler_registry.py -v", "acceptanceCriteria": ["UpscalerEngine is an ABC with required abstract methods", "register_upscaler stores factories and rejects duplicates", "get_upscaler returns factory or raises UnknownAdapter", "upscaler_names returns sorted list", "Three new exceptions subclass KinoforgeError and expose context"], "modelTier": "mechanical"}
```

---

## Task 3: `CapabilityKey` `stages` / `upscaler` / `upscaler_precision` factors + backward-compat hash

**Goal:** Extend `CapabilityKey` with three new factors via the conditional-extend trick so every existing ledger entry keeps deriving the same hash.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (the `CapabilityKey` dataclass + its `derive`)
- Test: `tests/core/test_capability_key_stages.py`

**Acceptance Criteria:**
- [ ] `CapabilityKey(base_model="hf:x", engine="diffusers", precision="fp8").derive()` equals a frozen golden hex string captured from `git show HEAD~:src/kinoforge/core/interfaces.py` running the *pre-change* code (legacy invariant)
- [ ] `CapabilityKey(...).derive()` with non-default `stages` / `upscaler` / `upscaler_precision` differs from the legacy hash
- [ ] `derive()` round-trips: same fields → same hash, every time (5x repeat asserts equality)
- [ ] `warm_attach_key()` returns the same `WarmAttachKey` shape as before (Task 4 extends `WarmAttachKey` itself)
- [ ] Order-sensitivity preserved for `stages` (`("t2v","upscale")` ≠ `("upscale","t2v")`)

**Verify:** `pixi run pytest tests/core/test_capability_key_stages.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Capture the legacy golden hash BEFORE the change**

Run this once (no commit yet):

```bash
pixi run python -c "
from kinoforge.core.interfaces import CapabilityKey
k = CapabilityKey(base_model='hf:org/m', loras=('hf:org/lora1',), engine='diffusers', precision='fp8')
print('LEGACY_GOLDEN_HASH =', repr(k.derive()))
"
```

Copy the output value (a 64-char hex string) into the test as `_LEGACY_GOLDEN_HASH`.

- [ ] **Step 2: RED — `tests/core/test_capability_key_stages.py`**

```python
"""CapabilityKey stages/upscaler factors with backward-compat hash."""

from __future__ import annotations

from kinoforge.core.interfaces import CapabilityKey

# Frozen output from the pre-stages-factor implementation. If this value
# changes, EVERY warm-pod ledger entry written before the change becomes
# unmatchable. This test is the mandatory pre-merge gate.
_LEGACY_GOLDEN_HASH = (
    "REPLACE_WITH_STEP_1_OUTPUT"  # 64-char hex from Step 1 above
)


class TestBackwardCompatHash:
    def test_legacy_shape_matches_golden(self) -> None:
        k = CapabilityKey(
            base_model="hf:org/m",
            loras=("hf:org/lora1",),
            engine="diffusers",
            precision="fp8",
        )
        assert k.derive() == _LEGACY_GOLDEN_HASH

    def test_default_stages_factor_does_not_change_hash(self) -> None:
        legacy = CapabilityKey(base_model="hf:x", engine="diffusers", precision="fp8")
        explicit_empty = CapabilityKey(
            base_model="hf:x",
            engine="diffusers",
            precision="fp8",
            stages=(),
            upscaler="",
            upscaler_precision="",
        )
        assert legacy.derive() == explicit_empty.derive()


class TestNewFactorsChangeHash:
    def test_non_default_stages_differs(self) -> None:
        legacy = CapabilityKey(base_model="hf:x", engine="diffusers", precision="fp8")
        with_stages = CapabilityKey(
            base_model="hf:x",
            engine="diffusers",
            precision="fp8",
            stages=("t2v", "upscale"),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        assert legacy.derive() != with_stages.derive()

    def test_stage_order_matters(self) -> None:
        a = CapabilityKey(
            base_model="hf:x",
            stages=("t2v", "upscale"),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        b = CapabilityKey(
            base_model="hf:x",
            stages=("upscale", "t2v"),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        assert a.derive() != b.derive()


class TestDeterminism:
    def test_repeated_derive_is_stable(self) -> None:
        k = CapabilityKey(
            base_model="hf:x",
            stages=("t2v", "upscale"),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        outputs = {k.derive() for _ in range(5)}
        assert len(outputs) == 1


class TestWarmAttachKeyShape:
    def test_warm_attach_key_unchanged_signature(self) -> None:
        # Task 4 changes the warm_attach_key body; this test only pins the
        # legacy shape — base_model + engine + precision — survives.
        k = CapabilityKey(base_model="hf:x", engine="diffusers", precision="fp8")
        wak = k.warm_attach_key()
        assert wak.base_model == "hf:x"
        assert wak.engine == "diffusers"
        assert wak.precision == "fp8"
```

- [ ] **Step 3: Verify RED (TypeError on extra kwargs)**

```
pixi run pytest tests/core/test_capability_key_stages.py -v
```

Expected: `TypeError: CapabilityKey.__init__() got an unexpected keyword argument 'stages'`.

- [ ] **Step 4: Extend `CapabilityKey` in `interfaces.py`**

Replace the existing `class CapabilityKey:` block (around line 318) with:

```python
@dataclass(frozen=True)
class CapabilityKey:
    """Full identity a ModelProfile depends on. derive() is the stable cache key.

    Composite over WarmAttachKey + LoraStack + (new in 2026-06-28) stage tags.
    derive() produces byte-identical output to the pre-stages-factor
    implementation when stages == () AND upscaler == "" AND
    upscaler_precision == "", so every existing ledger entry keeps matching.

    Attributes:
        base_model: Base-model vendor-neutral ref.
        loras: Ordered LoRA stack; order matters and contributes to the key.
        engine: Engine name.
        precision: Precision/quantization.
        stages: Pipeline stages this cfg/pod actually supports. Empty tuple
            preserves the legacy hash space (a pure-generate pod or cfg with
            no upscale block). Non-empty values participate in the hash.
        upscaler: Upscaler registry key when stages includes "upscale".
        upscaler_precision: Variant+precision slug for the upscaler
            (e.g. "3b-fp8" for SeedVR2 3B FP8).
    """

    base_model: str
    loras: tuple[str, ...] = ()
    engine: str = ""
    precision: str = ""
    stages: tuple[str, ...] = ()
    upscaler: str = ""
    upscaler_precision: str = ""

    def derive(self) -> str:
        """Stable, order-sensitive sha256 over all fields.

        Backward-compat invariant: when ``stages == ()`` AND ``upscaler == ""``
        AND ``upscaler_precision == ""``, derive() returns byte-identical
        output to the pre-change implementation. This is enforced by the
        conditional-extend below — the legacy payload shape is preserved
        whenever the new fields are at their defaults.
        """
        base: list[object] = [
            self.base_model,
            list(self.loras),
            self.engine,
            self.precision,
        ]
        if self.stages or self.upscaler or self.upscaler_precision:
            base.extend([list(self.stages), self.upscaler, self.upscaler_precision])
        payload = json.dumps(base, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def warm_attach_key(self) -> WarmAttachKey:
        """Return the WarmAttachKey factor (base + engine + precision).

        Stage / upscaler factors live on the extended WarmAttachKey shipped
        in Task 4 — this method's return shape is unchanged so callers that
        ignore stages keep working.
        """
        return WarmAttachKey(
            base_model=self.base_model, engine=self.engine, precision=self.precision
        )

    def lora_stack(self) -> LoraStack:
        """Return the LoraStack factor (ordered LoRA refs). Unchanged."""
        return LoraStack(refs=self.loras)
```

- [ ] **Step 5: GREEN**

```
pixi run pytest tests/core/test_capability_key_stages.py -v
```

Expected: all five tests pass. The golden-hash test in particular MUST pass — if it fails, the conditional-extend logic is broken and ALL existing pods are about to become unmatchable.

- [ ] **Step 6: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/core/interfaces.py \
  tests/core/test_capability_key_stages.py
git add src/kinoforge/core/interfaces.py tests/core/test_capability_key_stages.py
git commit -m "feat(upscale): CapabilityKey stages/upscaler/upscaler_precision factors with backward-compat hash

Frozen golden-hash test guards the byte-equal invariant for legacy ledger
entries. Conditional-extend in derive() preserves the legacy payload shape
when new fields are at their defaults."
```

```json:metadata
{"files": ["src/kinoforge/core/interfaces.py", "tests/core/test_capability_key_stages.py"], "verifyCommand": "pixi run pytest tests/core/test_capability_key_stages.py -v", "acceptanceCriteria": ["Legacy CapabilityKey derive() matches frozen golden hash", "Explicit empty stages produces same hash as default", "Non-default stages produces a different hash", "Stage order matters", "warm_attach_key() shape preserved"], "modelTier": "mechanical"}
```

---

## Task 4: `WarmAttachKey` `stages` / `upscaler` / `upscaler_precision` factors

**Goal:** Same backward-compat factor extension for `WarmAttachKey` so `--attach-pod <id>` validation and other consumers participate in the upscale-aware identity.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (the `WarmAttachKey` dataclass)
- Modify: `tests/core/test_capability_key_stages.py` (extend with WarmAttachKey-specific cases)

**Acceptance Criteria:**
- [ ] `WarmAttachKey(base_model="x", engine="e", precision="p").derive()` matches the frozen pre-change golden hex string
- [ ] `WarmAttachKey(..., stages=(...), upscaler="seedvr2", upscaler_precision="3b-fp8").derive()` differs from the legacy hash
- [ ] `CapabilityKey.warm_attach_key()` now populates the new factors too

**Verify:** `pixi run pytest tests/core/test_capability_key_stages.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Capture WarmAttachKey legacy golden hash** (before change)

```bash
pixi run python -c "
from kinoforge.core.interfaces import WarmAttachKey
k = WarmAttachKey(base_model='hf:x', engine='diffusers', precision='fp8')
print('WAK_LEGACY =', repr(k.derive()))
"
```

- [ ] **Step 2: RED — add tests to `test_capability_key_stages.py`**

Append:

```python
from kinoforge.core.interfaces import WarmAttachKey

_WAK_LEGACY_GOLDEN_HASH = "REPLACE_WITH_STEP_1_OUTPUT"


class TestWarmAttachKeyBackwardCompat:
    def test_legacy_shape_matches_golden(self) -> None:
        k = WarmAttachKey(base_model="hf:x", engine="diffusers", precision="fp8")
        assert k.derive() == _WAK_LEGACY_GOLDEN_HASH

    def test_default_factors_match_legacy(self) -> None:
        legacy = WarmAttachKey(base_model="hf:x", engine="diffusers", precision="fp8")
        explicit_empty = WarmAttachKey(
            base_model="hf:x",
            engine="diffusers",
            precision="fp8",
            stages=(),
            upscaler="",
            upscaler_precision="",
        )
        assert legacy.derive() == explicit_empty.derive()

    def test_non_default_differs(self) -> None:
        legacy = WarmAttachKey(base_model="hf:x", engine="diffusers", precision="fp8")
        extended = WarmAttachKey(
            base_model="hf:x",
            engine="diffusers",
            precision="fp8",
            stages=("upscale",),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        assert legacy.derive() != extended.derive()


class TestCapabilityKeyToWarmAttachKey:
    def test_propagates_new_factors(self) -> None:
        ck = CapabilityKey(
            base_model="hf:x",
            engine="diffusers",
            precision="fp8",
            stages=("t2v", "upscale"),
            upscaler="seedvr2",
            upscaler_precision="3b-fp8",
        )
        wak = ck.warm_attach_key()
        assert wak.stages == ("t2v", "upscale")
        assert wak.upscaler == "seedvr2"
        assert wak.upscaler_precision == "3b-fp8"
```

- [ ] **Step 3: Confirm RED**

```
pixi run pytest tests/core/test_capability_key_stages.py::TestWarmAttachKeyBackwardCompat tests/core/test_capability_key_stages.py::TestCapabilityKeyToWarmAttachKey -v
```

Expected: failures on `unexpected keyword argument 'stages'` and on the new attributes missing.

- [ ] **Step 4: Extend `WarmAttachKey` in `interfaces.py`**

Locate the existing `class WarmAttachKey:` block (around line 279). Replace with:

```python
@dataclass(frozen=True)
class WarmAttachKey:
    """Subset of CapabilityKey used to validate --attach-pod and warm-reuse.

    The base + engine + precision triple identifies the pod's loaded primary
    pipeline. The new stages / upscaler / upscaler_precision factors
    participate in the hash only when non-default, preserving the legacy
    hash space for pure-generate pods.
    """

    base_model: str
    engine: str = ""
    precision: str = ""
    stages: tuple[str, ...] = ()
    upscaler: str = ""
    upscaler_precision: str = ""

    def derive(self) -> str:
        """Backward-compat hash; conditional-extend mirrors CapabilityKey."""
        base: list[object] = [self.base_model, self.engine, self.precision]
        if self.stages or self.upscaler or self.upscaler_precision:
            base.extend([list(self.stages), self.upscaler, self.upscaler_precision])
        payload = json.dumps(base, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

- [ ] **Step 5: Update `CapabilityKey.warm_attach_key()` to propagate factors**

```python
def warm_attach_key(self) -> WarmAttachKey:
    """Return the WarmAttachKey factor with upscale-aware fields populated."""
    return WarmAttachKey(
        base_model=self.base_model,
        engine=self.engine,
        precision=self.precision,
        stages=self.stages,
        upscaler=self.upscaler,
        upscaler_precision=self.upscaler_precision,
    )
```

- [ ] **Step 6: GREEN**

```
pixi run pytest tests/core/test_capability_key_stages.py -v
```

Expected: all tests in the file pass (both Task 3 and Task 4 cases).

- [ ] **Step 7: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/core/interfaces.py \
  tests/core/test_capability_key_stages.py
git add src/kinoforge/core/interfaces.py tests/core/test_capability_key_stages.py
git commit -m "feat(upscale): WarmAttachKey stages/upscaler/upscaler_precision factors

Mirrors the CapabilityKey conditional-extend trick. CapabilityKey.warm_attach_key()
now propagates the new factors so --attach-pod validation sees the full identity."
```

```json:metadata
{"files": ["src/kinoforge/core/interfaces.py", "tests/core/test_capability_key_stages.py"], "verifyCommand": "pixi run pytest tests/core/test_capability_key_stages.py -v", "acceptanceCriteria": ["WarmAttachKey legacy hash matches frozen golden", "Default factors produce legacy hash", "Non-default factors produce different hash", "CapabilityKey.warm_attach_key() propagates new factors"], "modelTier": "mechanical"}
```

---

## Task 5: `UpscaleConfig` + `SeedVR2EngineConfig` + `Config.capability_key()` populates new factors

**Goal:** Pydantic models for the `upscale:` cfg block, with the variant-aware `weights_ref` validator, and the wiring on `Config.capability_key()` that populates `stages` / `upscaler` / `upscaler_precision`.

**Files:**
- Modify: `src/kinoforge/core/config.py`
- Test: `tests/core/test_config_upscale.py`

**Acceptance Criteria:**
- [ ] `UpscaleConfig(engine="seedvr2", scale="2x", seedvr2=SeedVR2EngineConfig())` round-trips through YAML
- [ ] `SeedVR2EngineConfig.weights_ref` defaults to `None`; a `model_validator` populates `"hf:ByteDance-Seed/SeedVR2-3B"` when variant=3B precision=fp8, and `"hf:ByteDance-Seed/SeedVR2-7B"` when variant=7B
- [ ] Explicit `weights_ref="hf:foo/bar"` is preserved
- [ ] Pure-generate cfg (no `upscale:` block) → `capability_key().stages == ()` and `.upscaler == ""` (legacy hash preserved)
- [ ] Generate-with-upscale cfg → `capability_key().stages == ("t2v", "upscale")`, `.upscaler == "seedvr2"`, `.upscaler_precision == "3b-fp8"`
- [ ] Upscale-only cfg (no `engine:` block; only `upscale:`) → `capability_key().stages == ("upscale",)`

**Verify:** `pixi run pytest tests/core/test_config_upscale.py -v` → all pass.

**Steps:**

- [ ] **Step 1: RED — `tests/core/test_config_upscale.py`**

```python
"""Tests for UpscaleConfig / SeedVR2EngineConfig + capability_key wiring."""

from __future__ import annotations

import pytest

from kinoforge.core.config import (
    Config,
    SeedVR2EngineConfig,
    UpscaleConfig,
)


def _minimal_generate_cfg() -> dict:
    """Smallest valid cfg dict that produces a non-empty engine block.

    The exact shape depends on the existing Config schema — copy from
    an existing test fixture under tests/ that builds a Config with engine
    set to diffusers (e.g. tests/test_orchestrator.py or tests/test_config.py).
    """
    return {
        "engine": {"kind": "diffusers", "precision": "fp8", "diffusers": {}},
        "compute": {"provider": "runpod", "image": "kinoforge/wan:latest"},
        # ... fill in remaining required fields per the existing Config schema
        # by copying from tests/test_config.py or similar
    }


class TestSeedVR2EngineConfig:
    def test_default_weights_ref_3b_fp8(self) -> None:
        c = SeedVR2EngineConfig()
        assert c.weights_ref == "hf:ByteDance-Seed/SeedVR2-3B"

    def test_default_weights_ref_7b_fp16(self) -> None:
        c = SeedVR2EngineConfig(variant="7B", precision="fp16")
        assert c.weights_ref == "hf:ByteDance-Seed/SeedVR2-7B"

    def test_explicit_override_preserved(self) -> None:
        c = SeedVR2EngineConfig(weights_ref="hf:fork/custom-seedvr2")
        assert c.weights_ref == "hf:fork/custom-seedvr2"


class TestUpscaleConfig:
    def test_round_trip(self) -> None:
        u = UpscaleConfig(
            engine="seedvr2",
            scale="2x",
            seedvr2=SeedVR2EngineConfig(),
        )
        assert u.engine == "seedvr2"
        assert u.scale == "2x"
        assert u.seedvr2 is not None


class TestConfigCapabilityKeyStages:
    def test_pure_generate_cfg_stages_empty(self) -> None:
        cfg = Config.model_validate(_minimal_generate_cfg())
        key = cfg.capability_key()
        assert key.stages == ()
        assert key.upscaler == ""

    def test_generate_with_upscale_stages(self) -> None:
        d = _minimal_generate_cfg()
        d["upscale"] = {
            "engine": "seedvr2",
            "scale": "2x",
            "seedvr2": {"variant": "3B", "precision": "fp8"},
        }
        cfg = Config.model_validate(d)
        key = cfg.capability_key()
        assert key.stages == ("t2v", "upscale")
        assert key.upscaler == "seedvr2"
        assert key.upscaler_precision == "3b-fp8"

    def test_upscale_only_cfg(self) -> None:
        # Upscale-only cfgs omit the engine: block but keep compute + upscale.
        d = _minimal_generate_cfg()
        d.pop("engine", None)
        d["upscale"] = {
            "engine": "seedvr2",
            "scale": "2x",
            "seedvr2": {"variant": "3B", "precision": "fp8"},
        }
        cfg = Config.model_validate(d)
        key = cfg.capability_key()
        assert key.stages == ("upscale",)
```

- [ ] **Step 2: Confirm RED**

```
pixi run pytest tests/core/test_config_upscale.py -v
```

Expected: `ImportError: cannot import name 'UpscaleConfig'`.

- [ ] **Step 3: Add `SeedVR2EngineConfig` + `UpscaleConfig` to `config.py`**

Locate the existing `class BedrockVideoEngineConfig(BaseModel):` (around line 426) and add after it (before `class EngineConfig(BaseModel):`):

```python
class SeedVR2EngineConfig(BaseModel):
    """SeedVR2-specific config; required when upscale.engine == "seedvr2".

    weights_ref defaults to None; a model_validator populates the
    variant-derived ref ("hf:ByteDance-Seed/SeedVR2-{variant}") so the
    common case stays one line in cfg. Explicit overrides (fork weights,
    pinned snapshots) are preserved unchanged.
    """

    variant: Literal["3B", "7B"] = "3B"
    precision: Literal["fp8", "fp16"] = "fp8"
    tile_size: int | None = None
    steps: int | None = None
    weights_ref: str | None = None

    @model_validator(mode="after")
    def _fill_weights_ref(self) -> SeedVR2EngineConfig:
        if self.weights_ref is None:
            object.__setattr__(
                self,
                "weights_ref",
                f"hf:ByteDance-Seed/SeedVR2-{self.variant}",
            )
        return self


class UpscaleConfig(BaseModel):
    """Top-level upscale block; presence in cfg activates the in-pipeline UpscaleStage.

    Attributes:
        engine: Upscaler name (registry key). v1 supports "seedvr2".
        scale: ScaleTarget grammar string ("2x"|"4x"|"1080p"|...). Consumers
            call ScaleTarget.parse(scale); the height branch raises
            NotYetImplementedError.
        seedvr2: SeedVR2-specific block; required when engine == "seedvr2".
    """

    engine: str
    scale: str
    seedvr2: SeedVR2EngineConfig | None = None
```

If `model_validator` is not already imported at the top of `config.py`, add it to the pydantic imports.

- [ ] **Step 4: Wire `upscale:` field onto `Config`**

Locate the `class Config(BaseModel):` block and add (alongside `engine`, `compute`, `keyframe`, etc.):

```python
upscale: UpscaleConfig | None = None
```

- [ ] **Step 5: Extend `Config.capability_key()`**

Find the existing `def capability_key(self) -> CapabilityKey:` method and replace its body with:

```python
def capability_key(self) -> CapabilityKey:
    """Derive the CapabilityKey for warm-reuse / ModelProfile cache."""
    # Existing extraction helpers (rename to match what's in your current
    # capability_key impl — preserve everything that already populated
    # base_model + loras + engine + precision).
    base_model = self._extract_base_model_ref()
    loras = self._extract_lora_refs()

    stages: list[str] = []
    if self.engine is not None:
        stages.append("t2v")  # or whichever mode the engine declares
    upscaler = ""
    upscaler_precision = ""
    if self.upscale is not None:
        stages.append("upscale")
        upscaler = self.upscale.engine
        if self.upscale.seedvr2 is not None:
            upscaler_precision = (
                f"{self.upscale.seedvr2.variant.lower()}-"
                f"{self.upscale.seedvr2.precision}"
            )

    return CapabilityKey(
        base_model=base_model,
        loras=tuple(loras),
        engine=self.engine.kind if self.engine is not None else "",
        precision=self.engine.precision if self.engine is not None else "",
        stages=tuple(stages),
        upscaler=upscaler,
        upscaler_precision=upscaler_precision,
    )
```

**Important:** the existing `Config.capability_key()` body has its own extraction logic for `base_model` and `loras` — preserve that logic exactly. The block above shows only the *added* lines around `stages` / `upscaler` / `upscaler_precision` and the rebuilt return value. Use `git diff` to confirm you preserved the pre-change extraction.

- [ ] **Step 6: Fill in the test cfg fixture**

Replace the `_minimal_generate_cfg()` body in `tests/core/test_config_upscale.py` Step 1 with the actual minimal Config dict shape — copy from an existing passing cfg fixture (search via `rg "Config.model_validate" tests/`).

- [ ] **Step 7: GREEN**

```
pixi run pytest tests/core/test_config_upscale.py -v
```

Expected: all tests pass.

- [ ] **Step 8: Pre-commit + commit**

```
pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_config_upscale.py
git add src/kinoforge/core/config.py tests/core/test_config_upscale.py
git commit -m "feat(upscale): UpscaleConfig + SeedVR2EngineConfig + capability_key stages wiring"
```

```json:metadata
{"files": ["src/kinoforge/core/config.py", "tests/core/test_config_upscale.py"], "verifyCommand": "pixi run pytest tests/core/test_config_upscale.py -v", "acceptanceCriteria": ["SeedVR2EngineConfig.weights_ref defaults variant-aware", "Explicit weights_ref override preserved", "UpscaleConfig round-trips", "Pure-generate cfg has stages=() (legacy hash)", "Generate+upscale cfg has stages=('t2v','upscale')", "Upscale-only cfg has stages=('upscale',)"], "modelTier": "standard"}
```

---
---

## Task 6: `UpscaleStage` (pipeline)

**Goal:** A `Stage`-protocol implementation that reads `state.artifacts["clip"]`, calls the configured `UpscalerEngine.upscale()`, and writes `state.artifacts["upscaled"]`. Refuses `ScaleTarget(kind="height")` with `NotYetImplementedError`.

**Files:**
- Create: `src/kinoforge/pipeline/upscale.py`
- Test: `tests/pipeline/test_upscale_stage.py`

**Acceptance Criteria:**
- [ ] `UpscaleStage(engine=eng, scale=ScaleTarget(kind="factor", value=2.0), instance=inst, cfg=cfg).run(state)` returns a new `PipelineState` with `state.artifacts["upscaled"]` populated AND `state.artifacts["clip"]` preserved
- [ ] `UpscaleStage.run(state)` raises `KeyError("clip")` when `state.artifacts["clip"]` absent
- [ ] `UpscaleStage.run(state)` raises `NotYetImplementedError` when `scale.kind == "height"` (defensive — also raised in `_runtime.upscale` but earlier here)
- [ ] Cancel-token: `UpscaleStage` propagates a `CancelToken` to `engine.upscale`

**Verify:** `pixi run pytest tests/pipeline/test_upscale_stage.py -v` → all pass.

**Steps:**

- [ ] **Step 1: RED — `tests/pipeline/test_upscale_stage.py`**

```python
"""Tests for UpscaleStage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.interfaces import (
    Artifact,
    GenerationRequest,
    PipelineState,
    UpscaleJob,
    UpscaleResult,
    UpscalerEngine,
)
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.pipeline.upscale import UpscaleStage


def _art(uri: str) -> Artifact:
    return Artifact(uri=uri, sha256="0" * 64, size=1)


@dataclass
class _FakeEngine:
    """Tiny stand-in honouring the UpscalerEngine surface for stage tests."""
    name: str = "fake"
    requires_compute: bool = False
    requires_local_weights: bool = False
    supported_scales: tuple[ScaleTarget, ...] = ()
    called_with: list[UpscaleJob] = field(default_factory=list)

    def provision(self, instance, cfg, *, cancel_token=None):
        return None

    def upscale(self, instance, job, cfg, *, cancel_token=None):
        self.called_with.append(job)
        return UpscaleResult(
            artifact=_art("file:///tmp/out.mp4"),
            input_resolution=(640, 480),
            output_resolution=(1280, 960),
            elapsed_s=1.0,
        )

    def validate_spec(self, job):
        return None

    def model_identity(self, cfg):
        return "fake"


def _state(with_clip: bool = True) -> PipelineState:
    req = GenerationRequest(prompt="p", mode="t2v")
    artifacts = {"clip": _art("file:///tmp/in.mp4")} if with_clip else {}
    return PipelineState(request=req, artifacts=artifacts)


class TestUpscaleStageHappyPath:
    def test_writes_upscaled(self) -> None:
        eng = _FakeEngine()
        stage = UpscaleStage(
            engine=eng,
            scale=ScaleTarget(kind="factor", value=2.0),
            instance=None,
            cfg={},
        )
        out = stage.run(_state())
        assert "upscaled" in out.artifacts
        assert out.artifacts["upscaled"].uri == "file:///tmp/out.mp4"

    def test_preserves_clip(self) -> None:
        eng = _FakeEngine()
        stage = UpscaleStage(
            engine=eng,
            scale=ScaleTarget(kind="factor", value=2.0),
            instance=None,
            cfg={},
        )
        out = stage.run(_state())
        assert out.artifacts["clip"].uri == "file:///tmp/in.mp4"

    def test_passes_scale_to_engine(self) -> None:
        eng = _FakeEngine()
        scale = ScaleTarget(kind="factor", value=4.0)
        stage = UpscaleStage(engine=eng, scale=scale, instance=None, cfg={})
        stage.run(_state())
        assert eng.called_with[0].scale == scale


class TestUpscaleStageFailureModes:
    def test_missing_clip_raises_keyerror(self) -> None:
        eng = _FakeEngine()
        stage = UpscaleStage(
            engine=eng,
            scale=ScaleTarget(kind="factor", value=2.0),
            instance=None,
            cfg={},
        )
        with pytest.raises(KeyError, match="clip"):
            stage.run(_state(with_clip=False))

    def test_height_scale_refused(self) -> None:
        eng = _FakeEngine()
        stage = UpscaleStage(
            engine=eng,
            scale=ScaleTarget(kind="height", value=1080.0),
            instance=None,
            cfg={},
        )
        with pytest.raises(NotYetImplementedError, match="1080p deferred"):
            stage.run(_state())
```

- [ ] **Step 2: Confirm RED**

```
pixi run pytest tests/pipeline/test_upscale_stage.py -v
```

Expected: `ImportError: cannot import name 'UpscaleStage'`.

- [ ] **Step 3: Implement `src/kinoforge/pipeline/upscale.py`**

```python
"""UpscaleStage — PipelineState in, PipelineState out.

Reads state.artifacts["clip"], invokes the configured UpscalerEngine, writes
state.artifacts["upscaled"]. Defensive raise on ScaleTarget(kind="height")
mirrors the engine-level raise so cfgs that pass schema validation but ask
for the height branch still fail before pod work begins.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.interfaces import (
    CancelToken,
    Instance,
    PipelineState,
    UpscaleJob,
    UpscalerEngine,
)
from kinoforge.core.scale_target import ScaleTarget


@dataclass
class UpscaleStage:
    """A Stage that upscales the rendered clip in-place.

    Attributes:
        engine: Configured UpscalerEngine (already provisioned).
        scale: Parsed ScaleTarget. kind="height" raises NotYetImplementedError.
        instance: Compute instance to pass through to the engine; None for
            local engines.
        cfg: Runtime config dict the engine interprets.
        cancel_token: Threaded through to engine.upscale.
    """

    engine: UpscalerEngine
    scale: ScaleTarget
    instance: Instance | None
    cfg: dict
    cancel_token: CancelToken | None = None

    def run(self, state: PipelineState) -> PipelineState:
        if self.scale.kind == "height":
            raise NotYetImplementedError(
                f"--scale {int(self.scale.value)}p deferred to a later "
                f"session; use --scale Nx for v1"
            )
        clip = state.artifacts["clip"]
        job = UpscaleJob(source=clip, scale=self.scale)
        result = self.engine.upscale(
            self.instance, job, self.cfg, cancel_token=self.cancel_token
        )
        new_artifacts = dict(state.artifacts)
        new_artifacts["upscaled"] = result.artifact
        return replace(state, artifacts=new_artifacts)
```

- [ ] **Step 4: Create `tests/pipeline/__init__.py`** (empty) if not present.

- [ ] **Step 5: GREEN**

```
pixi run pytest tests/pipeline/test_upscale_stage.py -v
```

- [ ] **Step 6: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/pipeline/upscale.py \
  tests/pipeline/test_upscale_stage.py
git add src/kinoforge/pipeline/upscale.py tests/pipeline/test_upscale_stage.py \
        tests/pipeline/__init__.py
git commit -m "feat(upscale): UpscaleStage — reads clip artifact, writes upscaled"
```

```json:metadata
{"files": ["src/kinoforge/pipeline/upscale.py", "tests/pipeline/test_upscale_stage.py"], "verifyCommand": "pixi run pytest tests/pipeline/test_upscale_stage.py -v", "acceptanceCriteria": ["Writes state.artifacts['upscaled'] and preserves 'clip'", "Raises KeyError when clip missing", "Raises NotYetImplementedError on ScaleTarget(kind='height')"], "modelTier": "mechanical"}
```

---

## Task 7: Warm-matcher subset pass for upscale-only cfgs

**Goal:** When primary hash-equality returns no candidates AND cfg is upscale-only (`stages=("upscale",)`, no engine block), run a secondary scan that accepts pods whose ledger-recorded `kinoforge_stages` is a superset of the cfg's `stages` AND `upscaler` / `upscaler_precision` match.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_scan_warm_candidates`)
- Modify: `src/kinoforge/core/warm_reuse/matcher.py` (search for existing matcher helpers; extend rather than fork)
- Test: `tests/test_warm_matcher_stages.py`

**Acceptance Criteria:**
- [ ] Multi-stage pod (`kinoforge_stages=["t2v","upscale"]`) attaches to an upscale-only cfg via the secondary pass
- [ ] Generate-only pod (`kinoforge_stages=["t2v"]` or `kinoforge_stages` absent) refused for an upscale-only cfg
- [ ] Pod with `upscaler="flashvsr"` refused for an `upscaler="seedvr2"` cfg
- [ ] Pod with `upscaler_precision="3b-fp16"` refused for an `upscaler_precision="3b-fp8"` cfg
- [ ] Primary hash-equality path still wins when both primary and secondary would match (avoid spurious attach to the wrong pod)
- [ ] Ledger writes for new generations include `kinoforge_stages: list[str]` (see Step 5)

**Verify:** `pixi run pytest tests/test_warm_matcher_stages.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Read the current matcher**

```
rg -n "find_warm_attach_candidate|kinoforge_key|kinoforge_stages" src/kinoforge/core/warm_reuse/
```

Understand which file owns the primary pass. The plan assumes `core/warm_reuse/matcher.py`; if your codebase has a different layout, adjust paths.

- [ ] **Step 2: RED — `tests/test_warm_matcher_stages.py`**

```python
"""Tests for the warm-matcher subset pass (upscale-only cfg over multi-stage pod)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kinoforge.core.interfaces import CapabilityKey
from kinoforge.core.warm_reuse.matcher import find_warm_attach_candidate


@dataclass
class _LedgerEntry:
    """Mirrors the on-disk ledger row shape; only fields the matcher reads."""
    pod_id: str
    kinoforge_key: str       # primary hash
    kinoforge_stages: list[str] | None = None
    kinoforge_upscaler: str = ""
    kinoforge_upscaler_precision: str = ""


def _multi_stage_pod(cfg_key: CapabilityKey) -> _LedgerEntry:
    """Build a ledger entry for a (t2v,upscale) pod that does NOT hash-equal cfg."""
    return _LedgerEntry(
        pod_id="pod-multi",
        kinoforge_key="DIFFERENT_FROM_CFG",   # forces secondary pass
        kinoforge_stages=["t2v", "upscale"],
        kinoforge_upscaler="seedvr2",
        kinoforge_upscaler_precision="3b-fp8",
    )


def _upscale_only_cfg_key() -> CapabilityKey:
    return CapabilityKey(
        base_model="",
        stages=("upscale",),
        upscaler="seedvr2",
        upscaler_precision="3b-fp8",
    )


class TestSubsetMatch:
    def test_multi_stage_pod_matches_upscale_only_cfg(self) -> None:
        cfg_key = _upscale_only_cfg_key()
        pods = [_multi_stage_pod(cfg_key)]
        match = find_warm_attach_candidate(cfg_key, pods)
        assert match is not None
        assert match.pod_id == "pod-multi"

    def test_generate_only_pod_refused_for_upscale_only_cfg(self) -> None:
        cfg_key = _upscale_only_cfg_key()
        pods = [
            _LedgerEntry(
                pod_id="pod-gen",
                kinoforge_key="...",
                kinoforge_stages=["t2v"],
            )
        ]
        assert find_warm_attach_candidate(cfg_key, pods) is None

    def test_legacy_pod_without_stages_field_refused_for_upscale_only(self) -> None:
        cfg_key = _upscale_only_cfg_key()
        pods = [
            _LedgerEntry(
                pod_id="pod-legacy",
                kinoforge_key="...",
                kinoforge_stages=None,
            )
        ]
        assert find_warm_attach_candidate(cfg_key, pods) is None

    def test_upscaler_mismatch_refused(self) -> None:
        cfg_key = _upscale_only_cfg_key()
        pods = [
            _LedgerEntry(
                pod_id="pod-flash",
                kinoforge_key="...",
                kinoforge_stages=["t2v", "upscale"],
                kinoforge_upscaler="flashvsr",     # wrong upscaler
                kinoforge_upscaler_precision="3b-fp8",
            )
        ]
        assert find_warm_attach_candidate(cfg_key, pods) is None

    def test_upscaler_precision_mismatch_refused(self) -> None:
        cfg_key = _upscale_only_cfg_key()
        pods = [
            _LedgerEntry(
                pod_id="pod-7b",
                kinoforge_key="...",
                kinoforge_stages=["t2v", "upscale"],
                kinoforge_upscaler="seedvr2",
                kinoforge_upscaler_precision="7b-fp16",  # wrong precision
            )
        ]
        assert find_warm_attach_candidate(cfg_key, pods) is None


class TestPrimaryWinsOverSecondary:
    def test_primary_hash_match_preferred(self) -> None:
        cfg_key = _upscale_only_cfg_key()
        primary = _LedgerEntry(
            pod_id="pod-primary",
            kinoforge_key=cfg_key.derive(),
            kinoforge_stages=["upscale"],
            kinoforge_upscaler="seedvr2",
            kinoforge_upscaler_precision="3b-fp8",
        )
        secondary = _multi_stage_pod(cfg_key)
        match = find_warm_attach_candidate(cfg_key, [secondary, primary])
        assert match is not None
        assert match.pod_id == "pod-primary"
```

- [ ] **Step 3: Confirm RED**

```
pixi run pytest tests/test_warm_matcher_stages.py -v
```

Expected: failures because the secondary pass doesn't exist yet (multi-stage pod is refused).

- [ ] **Step 4: Extend the matcher**

Locate the existing `find_warm_attach_candidate` function. Add the secondary pass AFTER the primary loop returns nothing:

```python
def find_warm_attach_candidate(
    cfg_key: CapabilityKey,
    pods: list,  # Iterable of ledger entries with kinoforge_key etc.
) -> Any | None:
    """Return a warm-pod ledger entry matching cfg_key, or None.

    Two-pass strategy:
      1. Primary: exact derive() hash equality. Same shape as today.
      2. Secondary (only for upscale-only cfgs): pod's kinoforge_stages
         is a superset of cfg's stages AND kinoforge_upscaler /
         kinoforge_upscaler_precision match. Triggered when primary is
         empty AND cfg.stages == ("upscale",) AND cfg.engine == "" (the
         upscale-only signature).
    """
    target_hash = cfg_key.derive()
    for p in pods:
        if getattr(p, "kinoforge_key", None) == target_hash:
            return p

    # Secondary pass — upscale-only cfgs only.
    is_upscale_only = (
        cfg_key.stages == ("upscale",)
        and cfg_key.engine == ""
    )
    if not is_upscale_only:
        return None

    want_stages = set(cfg_key.stages)
    for p in pods:
        have = getattr(p, "kinoforge_stages", None)
        if have is None:
            continue  # legacy pods opt out of the secondary pass
        if not want_stages.issubset(set(have)):
            continue
        if getattr(p, "kinoforge_upscaler", "") != cfg_key.upscaler:
            continue
        if getattr(p, "kinoforge_upscaler_precision", "") != cfg_key.upscaler_precision:
            continue
        return p
    return None
```

Adjust the function signature to match the existing one (parameter names, ledger-entry shape). The semantics above are what matter; the wrapping conforms to the existing matcher API.

- [ ] **Step 5: Wire ledger writes to include the new fields**

Locate where the ledger row is written (look for the existing `kinoforge_key` set site — `rg -n "kinoforge_key=" src/kinoforge/`). Add three sibling writes:

```python
ledger_entry["kinoforge_stages"] = list(cfg.capability_key().stages)
ledger_entry["kinoforge_upscaler"] = cfg.capability_key().upscaler
ledger_entry["kinoforge_upscaler_precision"] = cfg.capability_key().upscaler_precision
```

(Exact key naming style follows the existing ledger schema — snake_case in the ledger JSON; preserve the existing pattern.)

- [ ] **Step 6: GREEN**

```
pixi run pytest tests/test_warm_matcher_stages.py -v
```

- [ ] **Step 7: Run the broader warm-reuse test suite**

```
pixi run pytest tests/ -k warm_reuse -v
```

Expected: no regressions from the matcher change (the secondary pass is additive — opt-in via the upscale-only signature).

- [ ] **Step 8: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/core/warm_reuse/matcher.py \
  src/kinoforge/cli/_commands.py \
  tests/test_warm_matcher_stages.py
git add src/kinoforge/core/warm_reuse/matcher.py \
        src/kinoforge/cli/_commands.py \
        tests/test_warm_matcher_stages.py
git commit -m "feat(upscale): warm-matcher subset pass for upscale-only cfgs

Primary hash-equality unchanged. Secondary pass fires only when cfg is
upscale-only (stages=('upscale',), no engine block). Multi-stage pods
attach via the secondary pass; legacy pods without kinoforge_stages opt
out. Ledger writes now include kinoforge_stages/upscaler/upscaler_precision."
```

```json:metadata
{"files": ["src/kinoforge/core/warm_reuse/matcher.py", "src/kinoforge/cli/_commands.py", "tests/test_warm_matcher_stages.py"], "verifyCommand": "pixi run pytest tests/test_warm_matcher_stages.py tests/ -k warm_reuse -v", "acceptanceCriteria": ["Multi-stage pod attaches to upscale-only cfg via secondary pass", "Generate-only pod refused for upscale-only cfg", "Legacy pod without kinoforge_stages refused", "Upscaler / upscaler_precision mismatch refused", "Primary hash match wins over secondary"], "modelTier": "standard"}
```

---

## Task 8: `SeedVR2Runtime` wrapper (unit-testable with fake upstream)

**Goal:** A thin import-and-call layer around the upstream SeedVR inference module. Held inside `_LOADED[name].pipe` on the server. `to(device)` is the LRU eviction hook.

**Files:**
- Create: `src/kinoforge/upscalers/__init__.py` (empty)
- Create: `src/kinoforge/upscalers/seedvr2/__init__.py` (empty placeholder — Task 10 fills in `SeedVR2Engine`)
- Create: `src/kinoforge/upscalers/seedvr2/_runtime.py`
- Test: `tests/upscalers/test_seedvr2_runtime.py`

**Acceptance Criteria:**
- [ ] `SeedVR2Runtime(weights_dir=Path("/tmp/x"), variant="3B", precision="fp8")` constructs without hitting the network or filesystem in unit tests (upstream import patched)
- [ ] `runtime.upscale(video_path, ScaleTarget(kind="factor", value=2.0), {})` returns a `Path`
- [ ] `runtime.upscale(..., ScaleTarget(kind="height", value=1080), ...)` raises `NotYetImplementedError`
- [ ] `runtime.to("cpu")` calls `pipe.to("cpu")` on the underlying inferencer; `runtime.to("cuda")` calls `pipe.to("cuda")`
- [ ] Lazy import: `import kinoforge.upscalers.seedvr2._runtime` does NOT import the upstream package at module-import time (deferred to `__init__`)

**Verify:** `pixi run pytest tests/upscalers/test_seedvr2_runtime.py -v` → all pass.

**Steps:**

- [ ] **Step 1: RED — `tests/upscalers/test_seedvr2_runtime.py`**

```python
"""Tests for SeedVR2Runtime wrapper. Upstream module is patched out — these
tests run without the real seedvr package installed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.scale_target import ScaleTarget


@pytest.fixture
def _patched_seedvr():
    """Patch the lazy import so SeedVR2Runtime can be constructed offline."""
    fake_inferencer = MagicMock()
    fake_inferencer.from_pretrained = MagicMock(return_value=fake_inferencer)
    fake_inferencer.upscale = MagicMock(return_value=Path("/tmp/out.mp4"))
    with patch.dict("sys.modules", {"seedvr.inference": MagicMock(SeedVR2Inferencer=fake_inferencer)}):
        yield fake_inferencer


class TestModuleImportIsLazy:
    def test_module_import_does_not_require_upstream(self) -> None:
        # If this import fails because seedvr is missing, the module is
        # eagerly importing upstream and the lazy-import contract is broken.
        import kinoforge.upscalers.seedvr2._runtime  # noqa: F401


class TestConstruction:
    def test_constructs_with_patched_upstream(self, _patched_seedvr) -> None:
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        rt = SeedVR2Runtime(weights_dir=Path("/tmp/w"), variant="3B", precision="fp8")
        assert rt is not None


class TestUpscale:
    def test_factor_branch_returns_path(self, _patched_seedvr) -> None:
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        rt = SeedVR2Runtime(weights_dir=Path("/tmp/w"), variant="3B", precision="fp8")
        out = rt.upscale(
            Path("/tmp/in.mp4"),
            ScaleTarget(kind="factor", value=2.0),
            {},
        )
        assert out == Path("/tmp/out.mp4")

    def test_height_branch_refuses(self, _patched_seedvr) -> None:
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        rt = SeedVR2Runtime(weights_dir=Path("/tmp/w"), variant="3B", precision="fp8")
        with pytest.raises(NotYetImplementedError):
            rt.upscale(
                Path("/tmp/in.mp4"),
                ScaleTarget(kind="height", value=1080),
                {},
            )


class TestEvictionHook:
    def test_to_cpu(self, _patched_seedvr) -> None:
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        rt = SeedVR2Runtime(weights_dir=Path("/tmp/w"), variant="3B", precision="fp8")
        rt.to("cpu")
        _patched_seedvr.to.assert_called_with("cpu")
```

- [ ] **Step 2: Confirm RED**

```
mkdir -p tests/upscalers && touch tests/upscalers/__init__.py
pixi run pytest tests/upscalers/test_seedvr2_runtime.py -v
```

Expected: `ImportError: cannot import name 'SeedVR2Runtime'`.

- [ ] **Step 3: Implement `src/kinoforge/upscalers/seedvr2/_runtime.py`**

```python
"""SeedVR2Runtime — thin wrapper around upstream ByteDance-Seed/SeedVR.

Upstream is NOT vendored. The provision script installs it from a pinned
commit SHA. This module's import is intentionally lazy: importing the
module does not import the upstream package — only constructing
SeedVR2Runtime does. This keeps the kinoforge package importable on a
host without the upstream installed (e.g. unit tests on the dev box).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.scale_target import ScaleTarget


class SeedVR2Runtime:
    """Wraps upstream SeedVR2 inference. Held inside _LOADED[name].pipe on the server.

    Args:
        weights_dir: Local path to the downloaded SeedVR2 weights.
        variant: "3B" or "7B".
        precision: "fp8" or "fp16".

    Raises:
        ImportError: Upstream seedvr package not installed (caller responsibility).
    """

    def __init__(
        self,
        weights_dir: Path,
        variant: Literal["3B", "7B"],
        precision: Literal["fp8", "fp16"],
    ) -> None:
        # Lazy import — module-level import would fail on machines without
        # the upstream seedvr package installed (unit-test hosts).
        from seedvr.inference import SeedVR2Inferencer  # type: ignore[import-not-found]

        self._inferencer: Any = SeedVR2Inferencer.from_pretrained(
            weights_dir, variant=variant, dtype=precision
        )
        self._variant = variant
        self._precision = precision

    def upscale(self, video_path: Path, scale: ScaleTarget, params: dict) -> Path:
        """Run SeedVR2 inference on a single clip.

        Args:
            video_path: Local path to the input mp4.
            scale: ScaleTarget. kind="height" raises NotYetImplementedError.
            params: Engine-specific overrides (tile_size, steps, ...).

        Returns:
            Local path to the upscaled mp4.

        Raises:
            NotYetImplementedError: ScaleTarget(kind="height") is v1-deferred.
        """
        if scale.kind == "height":
            raise NotYetImplementedError(
                f"height-target upscale (e.g. {int(scale.value)}p) deferred "
                f"to a later session; use --scale Nx for v1"
            )
        return Path(
            self._inferencer.upscale(
                video_path,
                factor=scale.value,
                **{k: v for k, v in params.items() if v is not None},
            )
        )

    def to(self, device: str) -> None:
        """LRU eviction hook — move underlying nn.Modules between devices.

        Args:
            device: "cuda" | "cpu" | "disk". The "disk" case is handled by
                the server deleting the runtime instance and reloading on
                next activation; this method only supports cuda/cpu moves.
        """
        self._inferencer.to(device)
```

- [ ] **Step 4: GREEN**

```
pixi run pytest tests/upscalers/test_seedvr2_runtime.py -v
```

- [ ] **Step 5: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/upscalers/__init__.py \
  src/kinoforge/upscalers/seedvr2/__init__.py \
  src/kinoforge/upscalers/seedvr2/_runtime.py \
  tests/upscalers/test_seedvr2_runtime.py
git add src/kinoforge/upscalers/ tests/upscalers/
git commit -m "feat(upscale): SeedVR2Runtime wrapper with lazy upstream import"
```

```json:metadata
{"files": ["src/kinoforge/upscalers/__init__.py", "src/kinoforge/upscalers/seedvr2/__init__.py", "src/kinoforge/upscalers/seedvr2/_runtime.py", "tests/upscalers/test_seedvr2_runtime.py"], "verifyCommand": "pixi run pytest tests/upscalers/test_seedvr2_runtime.py -v", "acceptanceCriteria": ["Module-level import does not require upstream seedvr installed", "Constructor patches cleanly for unit tests", "upscale() returns Path on factor branch", "upscale() raises NotYetImplementedError on height branch", "to('cpu') / to('cuda') hooks delegate to inferencer"], "modelTier": "standard"}
```

---

## Task 9: `_fetch_weights` CLI module (HF source dispatch)

**Goal:** A small Python entry point (`python -m kinoforge.upscalers.seedvr2._fetch_weights ...`) that the pod's provision script invokes to materialise SeedVR2 weights into a known directory via the existing `kinoforge.sources.huggingface` source-resolver.

**Files:**
- Create: `src/kinoforge/upscalers/seedvr2/_fetch_weights.py`
- Test: `tests/upscalers/test_seedvr2_fetch_weights.py`

**Acceptance Criteria:**
- [ ] `python -m kinoforge.upscalers.seedvr2._fetch_weights --variant 3B --precision fp8 --dest /tmp/x` succeeds (mocking the HF download)
- [ ] `--variant` validates against `{3B,7B}`; `--precision` validates against `{fp8,fp16}`
- [ ] Dispatch goes through `kinoforge.core.registry.source_for_ref(...)` — no direct HF client import in this module
- [ ] Final weight directory layout matches what `SeedVR2Runtime.from_pretrained` expects (mirror upstream's expected on-disk layout — see upstream README)

**Verify:** `pixi run pytest tests/upscalers/test_seedvr2_fetch_weights.py -v` → all pass.

**Steps:**

- [ ] **Step 1: RED — `tests/upscalers/test_seedvr2_fetch_weights.py`**

```python
"""Tests for the _fetch_weights CLI module."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _run(argv: list[str]) -> int:
    """Invoke the module's main via argv injection."""
    from kinoforge.upscalers.seedvr2._fetch_weights import main

    return main(argv)


class TestArgParsing:
    def test_rejects_unknown_variant(self) -> None:
        with pytest.raises(SystemExit):
            _run(["--variant", "13B", "--precision", "fp8", "--dest", "/tmp/x"])

    def test_rejects_unknown_precision(self) -> None:
        with pytest.raises(SystemExit):
            _run(["--variant", "3B", "--precision", "int4", "--dest", "/tmp/x"])


class TestDispatch:
    def test_uses_registry_source_for_ref(self, tmp_path: Path) -> None:
        fake_source = MagicMock()
        fake_artifact = MagicMock()
        fake_artifact.uri = str(tmp_path / "seedvr2-3b")
        fake_source.resolve.return_value = fake_artifact

        with patch(
            "kinoforge.core.registry.source_for_ref",
            return_value=fake_source,
        ) as m:
            rc = _run(
                [
                    "--variant", "3B",
                    "--precision", "fp8",
                    "--dest", str(tmp_path),
                ]
            )
            assert rc == 0
            m.assert_called_once()
            # The ref passed to source_for_ref should be the HF SeedVR2 3B ref.
            assert "ByteDance-Seed/SeedVR2-3B" in m.call_args.args[0]
```

- [ ] **Step 2: Implement `src/kinoforge/upscalers/seedvr2/_fetch_weights.py`**

```python
"""CLI entry point invoked by the pod's provision script to materialise
SeedVR2 weights via kinoforge's source-resolver path.

Usage:
    python -m kinoforge.upscalers.seedvr2._fetch_weights \
        --variant 3B --precision fp8 --dest /workspace/models/seedvr2

Args validated against the (variant, precision) matrix the engine supports.
Dispatches through kinoforge.core.registry.source_for_ref so HuggingFace
auth / caching / retry are all inherited from the existing path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kinoforge.core import registry


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kinoforge.upscalers.seedvr2._fetch_weights")
    p.add_argument("--variant", choices=["3B", "7B"], required=True)
    p.add_argument("--precision", choices=["fp8", "fp16"], required=True)
    p.add_argument("--dest", type=Path, required=True)
    return p


def _ref_for(variant: str) -> str:
    return f"hf:ByteDance-Seed/SeedVR2-{variant}"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    ref = _ref_for(args.variant)
    source = registry.source_for_ref(ref)
    artifact = source.resolve(ref)
    # Existing source-resolver writes weights into dest; we re-export the
    # resolved URI for the caller / logs.
    print(f"resolved {ref} -> {artifact.uri}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 3: GREEN**

```
pixi run pytest tests/upscalers/test_seedvr2_fetch_weights.py -v
```

- [ ] **Step 4: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/upscalers/seedvr2/_fetch_weights.py \
  tests/upscalers/test_seedvr2_fetch_weights.py
git add src/kinoforge/upscalers/seedvr2/_fetch_weights.py \
        tests/upscalers/test_seedvr2_fetch_weights.py
git commit -m "feat(upscale): _fetch_weights CLI module dispatches HF source-resolver"
```

```json:metadata
{"files": ["src/kinoforge/upscalers/seedvr2/_fetch_weights.py", "tests/upscalers/test_seedvr2_fetch_weights.py"], "verifyCommand": "pixi run pytest tests/upscalers/test_seedvr2_fetch_weights.py -v", "acceptanceCriteria": ["argparse rejects unknown variant/precision", "Dispatches via registry.source_for_ref", "Ref shape: hf:ByteDance-Seed/SeedVR2-{variant}"], "modelTier": "mechanical"}
```

---

## Task 10: `SeedVR2Engine` (the public `UpscalerEngine` impl)

**Goal:** The HTTP-aware engine that the CLI / pipeline calls. Submits to `/upscale`, polls `/upscale/status`, downloads via `/artifacts`. Reuses `_retry_proxy_call` for RunPod-startup-window 404/502s.

**Files:**
- Replace empty placeholder: `src/kinoforge/upscalers/seedvr2/__init__.py`
- Test: `tests/upscalers/test_seedvr2_engine.py`

**Acceptance Criteria:**
- [ ] `SeedVR2Engine().name == "seedvr2"`, `requires_compute is True`, `requires_local_weights is True`
- [ ] `supported_scales` includes 2x and 4x; `validate_spec` accepts those, raises `UnsupportedScaleError` on 3x / 1.5x
- [ ] `validate_spec` raises `NotYetImplementedError` on `ScaleTarget(kind="height")`
- [ ] `model_identity(cfg)` returns `"seedvr2-3b-fp8"` for default cfg; `"seedvr2-7b-fp16"` for 7B+fp16 cfg; never raises
- [ ] `provision(instance, cfg)` writes a `RenderedProvision` extension that pip-installs upstream from a pinned commit SHA and runs `_fetch_weights`
- [ ] `upscale(instance, job, cfg)` POSTs to `/upscale`, polls `/upscale/status/{id}` until `done`, downloads via `/artifacts/<filename>`. Calls go through `_retry_proxy_call` for RunPod proxy resilience
- [ ] Self-registers at module import: `register_upscaler("seedvr2", SeedVR2Engine)`

**Verify:** `pixi run pytest tests/upscalers/test_seedvr2_engine.py -v` → all pass.

**Steps:**

- [ ] **Step 1: RED — `tests/upscalers/test_seedvr2_engine.py`**

```python
"""Tests for SeedVR2Engine — HTTP-aware UpscalerEngine implementation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.interfaces import Artifact, UpscaleJob
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.upscalers.seedvr2 import SeedVR2Engine


def _job(scale: ScaleTarget) -> UpscaleJob:
    return UpscaleJob(
        source=Artifact(uri="file:///tmp/in.mp4", sha256="0" * 64, size=1),
        scale=scale,
    )


class TestEngineMetadata:
    def test_name(self) -> None:
        assert SeedVR2Engine().name == "seedvr2"

    def test_requires_compute_and_local_weights(self) -> None:
        e = SeedVR2Engine()
        assert e.requires_compute is True
        assert e.requires_local_weights is True

    def test_supported_scales_contains_2x_and_4x(self) -> None:
        scales = SeedVR2Engine().supported_scales
        values = {s.value for s in scales if s.kind == "factor"}
        assert {2.0, 4.0}.issubset(values)


class TestValidateSpec:
    def test_accepts_2x(self) -> None:
        SeedVR2Engine().validate_spec(_job(ScaleTarget(kind="factor", value=2.0)))

    def test_refuses_3x(self) -> None:
        with pytest.raises(UnsupportedScaleError):
            SeedVR2Engine().validate_spec(_job(ScaleTarget(kind="factor", value=3.0)))

    def test_refuses_1_5x(self) -> None:
        with pytest.raises(UnsupportedScaleError):
            SeedVR2Engine().validate_spec(_job(ScaleTarget(kind="factor", value=1.5)))

    def test_refuses_height_target(self) -> None:
        with pytest.raises(NotYetImplementedError):
            SeedVR2Engine().validate_spec(_job(ScaleTarget(kind="height", value=1080)))


class TestModelIdentity:
    def test_default_3b_fp8(self) -> None:
        cfg = {
            "upscale": {
                "engine": "seedvr2",
                "scale": "2x",
                "seedvr2": {"variant": "3B", "precision": "fp8"},
            }
        }
        assert SeedVR2Engine().model_identity(cfg) == "seedvr2-3b-fp8"

    def test_7b_fp16(self) -> None:
        cfg = {
            "upscale": {
                "engine": "seedvr2",
                "scale": "2x",
                "seedvr2": {"variant": "7B", "precision": "fp16"},
            }
        }
        assert SeedVR2Engine().model_identity(cfg) == "seedvr2-7b-fp16"

    def test_empty_cfg_does_not_raise(self) -> None:
        # MUST NOT raise on missing fields per the UpscalerEngine contract.
        assert SeedVR2Engine().model_identity({}) == ""


class TestRegistrySelfRegister:
    def test_registered_at_import(self) -> None:
        from kinoforge.core import registry

        # The module's import side-effect must have registered "seedvr2".
        eng = registry.get_upscaler("seedvr2")()
        assert eng.name == "seedvr2"


class TestUpscaleHTTPRoundTrip:
    def test_submit_poll_download(self) -> None:
        """End-to-end mock — assert SeedVR2Engine.upscale() drives the three
        HTTP calls in order and returns an UpscaleResult."""
        # Wire fakes for /upscale (POST -> job_id), /upscale/status/{id}
        # (poll -> done), /artifacts (GET -> bytes). Use httpx.MockTransport
        # or pytest-httpx to mount routes.
        # Full implementation deferred to the executing subagent — the test
        # MUST exercise all three calls and assert the returned UpscaleResult
        # carries the artifact URI from the /artifacts GET response.
        pytest.skip(
            "Wire the three-call mock in the executing subagent — see "
            "engines/diffusers tests for the pattern with httpx.MockTransport."
        )
```

The last test is deliberately skipped with an instruction; the executing subagent fills it in by copying the HTTP-mock pattern from existing engine tests (`rg -n "httpx.MockTransport" tests/engines/`).

- [ ] **Step 2: Implement `src/kinoforge/upscalers/seedvr2/__init__.py`**

```python
"""SeedVR2Engine — UpscalerEngine impl for ByteDance-Seed/SeedVR2.

Talks to the FastAPI server on the pod via /upscale + /upscale/status/{id}.
Reuses the engines/_proxy_retry helper to absorb RunPod proxy
startup-window 404/502s (per project memory task7_comfyui_404_regression).

Self-registers at module import via register_upscaler("seedvr2", SeedVR2Engine).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from kinoforge.core import registry
from kinoforge.core.errors import (
    NotYetImplementedError,
    UnsupportedScaleError,
    UpscaleFailed,
)
from kinoforge.core.interfaces import (
    Artifact,
    CancelToken,
    Instance,
    RenderedProvision,
    UpscaleJob,
    UpscaleResult,
    UpscalerEngine,
)
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.engines._proxy_retry import _retry_proxy_call

# Pinned upstream commit. Bump deliberately when upstream releases a
# verified-good build; tracked in CHANGELOG / docs/engines.md.
_UPSTREAM_COMMIT = "REPLACE_AT_PLAN_EXEC_TIME"
_UPSTREAM_GIT = f"git+https://github.com/ByteDance-Seed/SeedVR@{_UPSTREAM_COMMIT}"

_SUPPORTED_FACTORS: tuple[float, ...] = (2.0, 4.0)


class SeedVR2Engine(UpscalerEngine):
    name = "seedvr2"
    requires_compute = True
    requires_local_weights = True
    supported_scales = tuple(
        ScaleTarget(kind="factor", value=v) for v in _SUPPORTED_FACTORS
    )

    def validate_spec(self, job: UpscaleJob) -> None:
        if job.scale.kind == "height":
            raise NotYetImplementedError(
                f"SeedVR2 v1 does not support height-target scale "
                f"({int(job.scale.value)}p); use --scale Nx"
            )
        if job.scale.value not in _SUPPORTED_FACTORS:
            raise UnsupportedScaleError(scale=job.scale, engine_name=self.name)

    def model_identity(self, cfg: dict[str, object]) -> str:
        try:
            block = cfg["upscale"]["seedvr2"]  # type: ignore[index]
            variant = str(block["variant"]).lower()  # type: ignore[index]
            precision = str(block["precision"])  # type: ignore[index]
            return f"seedvr2-{variant}-{precision}"
        except (KeyError, TypeError):
            return ""

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        # The diffusers server already renders its own provision; SeedVR2
        # contributes ADDITIONAL install steps that the orchestrator
        # composes with the base provision. The orchestrator stitches the
        # multi-stage provision; this method emits the SeedVR2-only fragment.
        block = cfg.get("upscale", {}).get("seedvr2", {})  # type: ignore[union-attr]
        variant = block.get("variant", "3B")
        precision = block.get("precision", "fp8")
        script = (
            f'pip install --no-build-isolation "seedvr @ {_UPSTREAM_GIT}"\n'
            f"python -m kinoforge.upscalers.seedvr2._fetch_weights "
            f"--variant {variant} --precision {precision} "
            f"--dest /workspace/models/seedvr2\n"
        )
        return RenderedProvision(
            script=script,
            run_cmd=[],   # No new run_cmd — server is started by diffusers provision
            image="",     # Inherits from diffusers
            ports=[],
            env_required=[],
        )

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None:
        # The orchestrator runs the rendered provision; this hook is a no-op
        # for SeedVR2 because the work is captured in render_provision.
        return None

    def upscale(
        self,
        instance: Instance | None,
        job: UpscaleJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> UpscaleResult:
        self.validate_spec(job)
        assert instance is not None, "SeedVR2Engine requires a compute instance"
        base = instance.proxy_url  # provider-supplied; e.g. https://<pod>-8000.proxy.runpod.net

        # POST /upscale
        submit_url = f"{base}/upscale"
        payload = self._build_payload(job, cfg)
        response = _retry_proxy_call(
            method="POST",
            url=submit_url,
            json=payload,
            cancel_token=cancel_token,
        )
        job_id: str = response["job_id"]

        # Poll /upscale/status/{id}
        t0 = time.monotonic()
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_set()
            status = _retry_proxy_call(
                method="GET",
                url=f"{base}/upscale/status/{job_id}",
                cancel_token=cancel_token,
            )
            state = status["state"]
            if state == "done":
                result_url = f"{base}/artifacts/{status['result']['filename']}"
                # The store-side downloader writes the bytes; we surface the URL.
                return UpscaleResult(
                    artifact=Artifact(
                        uri=result_url,
                        sha256=status["result"]["sha256"],
                        size=status["result"]["size"],
                    ),
                    input_resolution=tuple(status["result"]["input_resolution"]),
                    output_resolution=tuple(status["result"]["output_resolution"]),
                    elapsed_s=time.monotonic() - t0,
                    engine_meta=status["result"].get("engine_meta", {}),
                )
            if state == "error":
                raise UpscaleFailed(job_id=job_id, server_error=status.get("error", ""))
            time.sleep(2.0)

    def _build_payload(self, job: UpscaleJob, cfg: dict[str, object]) -> dict:
        block = cfg.get("upscale", {})  # type: ignore[union-attr]
        return {
            "source_url": job.source.uri,
            "source_filename": Path(job.source.uri).name,
            "scale": f"{job.scale.value:g}x" if job.scale.kind == "factor" else f"{int(job.scale.value)}p",
            "engine": "seedvr2",
            "seedvr2": block.get("seedvr2", {}),
        }


# Self-register on import.
registry.register_upscaler("seedvr2", SeedVR2Engine)
```

`_retry_proxy_call` exists today in `src/kinoforge/engines/_proxy_retry.py` (per the existing module layout). Confirm signature via `rg -n "def _retry_proxy_call" src/kinoforge/engines/_proxy_retry.py` and adapt the kwargs to match.

- [ ] **Step 3: GREEN**

```
pixi run pytest tests/upscalers/test_seedvr2_engine.py -v
```

(The HTTP round-trip test is `pytest.skip`-marked; the executing subagent fills it in. All other tests pass.)

- [ ] **Step 4: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/upscalers/seedvr2/__init__.py \
  tests/upscalers/test_seedvr2_engine.py
git add src/kinoforge/upscalers/seedvr2/__init__.py \
        tests/upscalers/test_seedvr2_engine.py
git commit -m "feat(upscale): SeedVR2Engine HTTP-aware UpscalerEngine + self-registration"
```

```json:metadata
{"files": ["src/kinoforge/upscalers/seedvr2/__init__.py", "tests/upscalers/test_seedvr2_engine.py"], "verifyCommand": "pixi run pytest tests/upscalers/test_seedvr2_engine.py -v", "acceptanceCriteria": ["Engine metadata correct (name/requires_compute/supported_scales)", "validate_spec accepts 2x/4x, refuses 3x/1.5x, refuses height", "model_identity returns slug; empty cfg does not raise", "Self-registered at module import"], "modelTier": "standard"}
```

---

## Task 11: Server LRU model registry + eviction policy

**Goal:** In `wan_t2v_server.py`, add `_LOADED` dict + `_REGISTRY_LOCK` + `_ensure_on_gpu(name)` with LRU CPU eviction and hard-floor `VRAMEvictionFailed` raise.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Test: `tests/engines/diffusers/test_lru_eviction.py`

**Acceptance Criteria:**
- [ ] `_ensure_on_gpu("model_a")` while `model_a` is the only entry → no eviction; model loaded to CUDA
- [ ] `_ensure_on_gpu("model_b")` with `model_a` on CUDA + insufficient headroom → `model_a` evicted to CPU; `model_b` loaded to CUDA
- [ ] Repeated `_ensure_on_gpu("model_a")` (already on CUDA, sufficient headroom) → no eviction; only `last_used_monotonic` refresh
- [ ] When evicting every other CUDA model is still insufficient AND target's `vram_bytes` exceeds available memory minus margin → `VRAMEvictionFailed`
- [ ] LRU order respected — least-recently-used model evicted first

**Verify:** `pixi run pytest tests/engines/diffusers/test_lru_eviction.py -v` → all pass.

**Steps:**

- [ ] **Step 1: RED — `tests/engines/diffusers/test_lru_eviction.py`**

```python
"""Tests for the in-process model registry + LRU CPU eviction."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.core.errors import VRAMEvictionFailed


@pytest.fixture
def _fake_cuda(monkeypatch):
    """Patch torch.cuda.mem_get_info and torch.cuda.empty_cache."""
    free = [10 * 1024**3]  # mutable 10 GB
    total = 24 * 1024**3

    def mem_get_info():
        return (free[0], total)

    monkeypatch.setattr("torch.cuda.mem_get_info", mem_get_info, raising=False)
    monkeypatch.setattr("torch.cuda.empty_cache", lambda: None, raising=False)
    return free  # tests mutate this to simulate VRAM consumption


def _fake_pipe(vram_bytes: int) -> MagicMock:
    p = MagicMock()
    p.vram_bytes = vram_bytes
    p.on_device = "cuda"
    return p


class TestSingleModel:
    def test_first_load_no_eviction(self, _fake_cuda) -> None:
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        srv._LOADED.clear()
        with patch.object(srv, "_load_model_to_gpu", return_value=_fake_pipe(5 * 1024**3)):
            entry = asyncio.run(srv._ensure_on_gpu("model_a"))
            assert entry["name"] == "model_a"
            assert entry["on_device"] == "cuda"


class TestEviction:
    def test_lru_evicts_when_tight(self, _fake_cuda) -> None:
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        srv._LOADED.clear()
        # Pre-load model_a on CUDA (5 GB), simulate VRAM consumption:
        _fake_cuda[0] = 2 * 1024**3  # only 2 GB free now
        with patch.object(
            srv, "_load_model_to_gpu", return_value=_fake_pipe(5 * 1024**3)
        ):
            asyncio.run(srv._ensure_on_gpu("model_a"))

        # Loading model_b (8 GB) should evict model_a to CPU first.
        _fake_cuda[0] = 2 * 1024**3
        with patch.object(
            srv, "_load_model_to_gpu", return_value=_fake_pipe(8 * 1024**3)
        ):
            entry = asyncio.run(srv._ensure_on_gpu("model_b"))
            assert entry["name"] == "model_b"
            assert srv._LOADED["model_a"]["on_device"] == "cpu"


class TestHardFloor:
    def test_target_exceeds_capacity_raises(self, _fake_cuda) -> None:
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        srv._LOADED.clear()
        _fake_cuda[0] = 4 * 1024**3   # 4 GB free
        # Pretend model_x needs 80 GB — total GPU is 24 GB; refuse.
        with patch.object(
            srv, "_load_model_to_gpu", return_value=_fake_pipe(80 * 1024**3)
        ), pytest.raises(VRAMEvictionFailed, match="exceeds GPU capacity"):
            asyncio.run(srv._ensure_on_gpu("model_x"))


class TestNoChurn:
    def test_repeated_ensure_no_eviction(self, _fake_cuda) -> None:
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        srv._LOADED.clear()
        _fake_cuda[0] = 20 * 1024**3
        with patch.object(
            srv, "_load_model_to_gpu", return_value=_fake_pipe(5 * 1024**3)
        ) as loader:
            asyncio.run(srv._ensure_on_gpu("model_a"))
            asyncio.run(srv._ensure_on_gpu("model_a"))
            asyncio.run(srv._ensure_on_gpu("model_a"))
        # Loader called exactly once — subsequent ensures hit the cache.
        assert loader.call_count == 1
```

- [ ] **Step 2: Implement `_LOADED` + `_ensure_on_gpu` in `wan_t2v_server.py`**

Add near the top of the module (after existing globals):

```python
import asyncio
import gc
import time
from typing import TypedDict


class LoadedModel(TypedDict):
    name: str
    pipe: Any
    vram_bytes: int
    last_used_monotonic: float
    on_device: Literal["cuda", "cpu", "disk"]


_LOADED: dict[str, LoadedModel] = {}
_REGISTRY_LOCK = asyncio.Lock()
_HEADROOM_MARGIN_BYTES = int(os.environ.get("KINOFORGE_HEADROOM_MARGIN_GB", "2")) * 1024**3


def _load_model_to_gpu(name: str) -> Any:
    """Engine-specific loader; dispatched on name prefix.

    Implementation note: this dispatch is the single seam where wan_t2v_server
    knows which loader to call for which prefix. SeedVR2 is loaded via
    `SeedVR2Runtime`; Wan via the existing `_diffusers_load`.
    """
    if name.startswith("wan-t2v-"):
        return _diffusers_load()
    if name.startswith("seedvr2-"):
        # Lazy import to keep module-import light.
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        variant, precision = name.split("-")[-2:]
        return SeedVR2Runtime(
            weights_dir=Path("/workspace/models/seedvr2"),
            variant=variant.upper(),
            precision=precision,
        )
    raise ValueError(f"unknown model name {name!r}; no loader registered")


async def _ensure_on_gpu(name: str) -> LoadedModel:
    """Ensure ``name`` is on CUDA with sufficient headroom.

    See plan Task 11 / spec §6.2 for the LRU + hard-floor contract.
    """
    async with _REGISTRY_LOCK:
        entry = _LOADED.get(name)
        if entry is not None and entry["on_device"] == "cuda":
            entry["last_used_monotonic"] = time.monotonic()
            return entry

        # Load if not already in _LOADED.
        if entry is None:
            pipe = _load_model_to_gpu(name)
            entry = LoadedModel(
                name=name,
                pipe=pipe,
                vram_bytes=getattr(pipe, "vram_bytes", 0),
                last_used_monotonic=time.monotonic(),
                on_device="cuda",
            )
            _LOADED[name] = entry
        else:
            # entry on CPU/disk — move back to CUDA.
            entry["pipe"].to("cuda")
            entry["on_device"] = "cuda"
            entry["last_used_monotonic"] = time.monotonic()

        # Headroom enforcement.
        await _enforce_headroom(name)
        return entry


async def _enforce_headroom(target_name: str) -> None:
    import torch

    free, total = torch.cuda.mem_get_info()
    target = _LOADED[target_name]

    # Hard floor — target alone too big.
    if target["vram_bytes"] > total - _HEADROOM_MARGIN_BYTES:
        raise VRAMEvictionFailed(
            model=target_name,
            reason="target exceeds GPU capacity",
        )

    while free < _HEADROOM_MARGIN_BYTES:
        # Pick least-recently-used CUDA-resident other model.
        victims = [
            n
            for n, e in _LOADED.items()
            if e["on_device"] == "cuda" and n != target_name
        ]
        if not victims:
            # No more CUDA victims — try CPU-resident eviction-to-disk.
            cpu_victims = [
                n for n, e in _LOADED.items() if e["on_device"] == "cpu"
            ]
            if not cpu_victims:
                raise VRAMEvictionFailed(
                    model=target_name,
                    reason="exhausted eviction targets with insufficient headroom",
                )
            evict = min(cpu_victims, key=lambda n: _LOADED[n]["last_used_monotonic"])
            del _LOADED[evict]["pipe"]
            _LOADED[evict]["on_device"] = "disk"
            gc.collect()
            torch.cuda.empty_cache()
            free, _ = torch.cuda.mem_get_info()
            continue

        evict = min(victims, key=lambda n: _LOADED[n]["last_used_monotonic"])
        _LOADED[evict]["pipe"].to("cpu")
        _LOADED[evict]["on_device"] = "cpu"
        gc.collect()
        torch.cuda.empty_cache()
        free, _ = torch.cuda.mem_get_info()
```

Existing `_load_pipeline` / `_diffusers_load` paths in the server are NOT removed — `_load_model_to_gpu` calls into them. Update the existing `@app.on_event("startup")` handler so the cold-boot Wan load registers itself in `_LOADED` via `_ensure_on_gpu("wan-t2v-a14b-fp8")` rather than the existing module-global pipe variable. This consolidates state.

- [ ] **Step 3: GREEN**

```
pixi run pytest tests/engines/diffusers/test_lru_eviction.py -v
```

- [ ] **Step 4: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
  tests/engines/diffusers/test_lru_eviction.py
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
        tests/engines/diffusers/test_lru_eviction.py
git commit -m "feat(upscale): in-process LRU model registry + hard-floor VRAMEvictionFailed

_LOADED + _REGISTRY_LOCK + _ensure_on_gpu support multiple co-resident
pipelines (Wan T2V, SeedVR2) with opportunistic LRU CPU eviction when
headroom is tight. Hard floor refuses a model that doesn't fit even
alone — surfaces as 503 to /generate or /upscale callers."
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/servers/wan_t2v_server.py", "tests/engines/diffusers/test_lru_eviction.py"], "verifyCommand": "pixi run pytest tests/engines/diffusers/test_lru_eviction.py -v", "acceptanceCriteria": ["First load: no eviction", "LRU evicts to CPU when headroom tight", "Repeated ensure is a no-op", "Target exceeding GPU capacity raises VRAMEvictionFailed"], "modelTier": "standard"}
```

---

## Task 12: Server `/upscale` + `/upscale/status/{id}` endpoints

**Goal:** Add the two new FastAPI routes. Serialize with `_upscale_lock`. Heavy sync work wrapped in `asyncio.to_thread`. Engine dispatch reads `request.engine` (currently only `"seedvr2"` handled; FlashVSR future drop-in extends).

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Test: `tests/engines/diffusers/test_server_upscale.py`

**Acceptance Criteria:**
- [ ] `POST /upscale` with valid body → 200 with `{"job_id": "..."}`
- [ ] `GET /upscale/status/{id}` returns `{"state": "queued"|"running"|"done"|"error", ...}`
- [ ] `POST /upscale` with `engine="flashvsr"` → 400 with "unsupported engine" (until FlashVSR ships)
- [ ] `POST /upscale` while another upscale runs → second request blocks on `_upscale_lock` (does not 500)
- [ ] `/health` endpoint remains responsive during a `/upscale` run (asyncio.to_thread wrap test)

**Verify:** `pixi run pytest tests/engines/diffusers/test_server_upscale.py -v` → all pass.

**Steps:**

- [ ] **Step 1: RED — test file using FastAPI's `TestClient`**

```python
"""Tests for /upscale + /upscale/status endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def _client(monkeypatch, tmp_path):
    """TestClient with the underlying CUDA load patched out."""
    # Patch _load_model_to_gpu and _ensure_on_gpu so the server starts
    # without real CUDA.
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    fake_loaded = {
        "name": "seedvr2-3b-fp8",
        "pipe": MagicMock(upscale=MagicMock(return_value=tmp_path / "out.mp4")),
        "vram_bytes": 10 * 1024**3,
        "last_used_monotonic": 0.0,
        "on_device": "cuda",
    }
    monkeypatch.setattr(
        srv,
        "_ensure_on_gpu",
        MagicMock(return_value=fake_loaded),
    )
    return TestClient(srv.app)


class TestUpscalePost:
    def test_returns_job_id(self, _client) -> None:
        r = _client.post(
            "/upscale",
            json={
                "source_url": "file:///tmp/in.mp4",
                "source_filename": "in.mp4",
                "scale": "2x",
                "engine": "seedvr2",
                "seedvr2": {"variant": "3B", "precision": "fp8"},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert "job_id" in body

    def test_unsupported_engine_rejected(self, _client) -> None:
        r = _client.post(
            "/upscale",
            json={
                "source_url": "file:///tmp/in.mp4",
                "source_filename": "in.mp4",
                "scale": "2x",
                "engine": "flashvsr",
            },
        )
        assert r.status_code == 400
        assert "unsupported engine" in r.json().get("detail", "").lower()


class TestUpscaleStatus:
    def test_status_returns_state(self, _client) -> None:
        # Submit then poll.
        post = _client.post(
            "/upscale",
            json={
                "source_url": "file:///tmp/in.mp4",
                "source_filename": "in.mp4",
                "scale": "2x",
                "engine": "seedvr2",
                "seedvr2": {"variant": "3B", "precision": "fp8"},
            },
        )
        job_id = post.json()["job_id"]
        r = _client.get(f"/upscale/status/{job_id}")
        assert r.status_code == 200
        assert "state" in r.json()
```

- [ ] **Step 2: Implement endpoints in `wan_t2v_server.py`**

Add a request schema near the existing `GenerateRequest`:

```python
class SeedVR2Params(BaseModel):
    variant: Literal["3B", "7B"] = "3B"
    precision: Literal["fp8", "fp16"] = "fp8"
    tile_size: int | None = None
    steps: int | None = None


class UpscaleRequest(BaseModel):
    source_url: str
    source_filename: str
    scale: str
    engine: str    # v1 server only dispatches "seedvr2"; future engines extend
    seedvr2: SeedVR2Params | None = None
    job_id: str | None = None


_upscale_lock = asyncio.Lock()
_upscale_jobs: dict[str, dict] = {}  # job_id -> status payload
```

Endpoints (insert near the existing `/generate`):

```python
@app.post("/upscale")
async def upscale_handler(req: UpscaleRequest) -> dict:
    if req.engine != "seedvr2":
        raise HTTPException(status_code=400, detail=f"unsupported engine: {req.engine}")
    job_id = req.job_id or f"u-{int(time.time()*1000)}"
    _upscale_jobs[job_id] = {"state": "queued", "progress": 0.0, "result": None, "error": None}
    asyncio.create_task(_run_upscale_job(job_id, req))
    return {"job_id": job_id}


async def _run_upscale_job(job_id: str, req: UpscaleRequest) -> None:
    async with _upscale_lock:
        try:
            _upscale_jobs[job_id]["state"] = "running"
            model_name = f"seedvr2-{(req.seedvr2.variant if req.seedvr2 else '3B').lower()}-{(req.seedvr2.precision if req.seedvr2 else 'fp8')}"
            entry = await _ensure_on_gpu(model_name)

            # Download source to a local temp; CPU-bound + IO; off the loop.
            local = await asyncio.to_thread(_download_to_local_temp, req.source_url, req.source_filename)
            scale = ScaleTarget.parse(req.scale)

            # Heavy CUDA call — to_thread to keep /health responsive.
            out_path = await asyncio.to_thread(
                entry["pipe"].upscale,
                local,
                scale,
                (req.seedvr2.dict() if req.seedvr2 else {}),
            )
            filename = Path(out_path).name
            _upscale_jobs[job_id]["state"] = "done"
            _upscale_jobs[job_id]["progress"] = 1.0
            _upscale_jobs[job_id]["result"] = {
                "filename": filename,
                "sha256": _sha256_file(out_path),
                "size": Path(out_path).stat().st_size,
                "input_resolution": list(_probe_resolution(local)),
                "output_resolution": list(_probe_resolution(out_path)),
                "engine_meta": {},
            }
        except Exception as exc:  # noqa: BLE001 — surface any error to client
            _upscale_jobs[job_id]["state"] = "error"
            _upscale_jobs[job_id]["error"] = str(exc)


@app.get("/upscale/status/{job_id}")
async def upscale_status_handler(job_id: str) -> dict:
    payload = _upscale_jobs.get(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    return payload
```

Helper utilities (`_download_to_local_temp`, `_sha256_file`, `_probe_resolution`) — if they don't exist in the server today, add minimal implementations using the project's standard urllib + hashlib + ffprobe shell-out patterns. Search via `rg -n "ffprobe|sha256" src/kinoforge/engines/diffusers/servers/` for the existing patterns to follow.

- [ ] **Step 3: GREEN**

```
pixi run pytest tests/engines/diffusers/test_server_upscale.py -v
```

- [ ] **Step 4: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
  tests/engines/diffusers/test_server_upscale.py
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
        tests/engines/diffusers/test_server_upscale.py
git commit -m "feat(upscale): server /upscale + /upscale/status/{id} endpoints

UpscaleRequest schema has engine as plain str so future FlashVSR drop-in
extends the dispatch table without touching the schema. Heavy CUDA work
wrapped in asyncio.to_thread to keep /health responsive (per
wan_server_async_blocking memory)."
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/servers/wan_t2v_server.py", "tests/engines/diffusers/test_server_upscale.py"], "verifyCommand": "pixi run pytest tests/engines/diffusers/test_server_upscale.py -v", "acceptanceCriteria": ["POST /upscale returns job_id", "GET /upscale/status/{id} returns state payload", "Unsupported engine -> 400", "_upscale_lock serializes concurrent calls"], "modelTier": "standard"}
```

---

## Task 13: `/health` payload extension (preserve `model`, add `models[]` + `capabilities[]`)

**Goal:** Extend `/health` payload additively. `model` field retained for backward compatibility; `models` (per-pipeline state) and `capabilities` (per-pod actually-loaded stages) added. `capabilities` derives from which loaders succeeded — not from cfg intent — so a half-failed provision reports the partial truth.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (`@app.get("/health")`)
- Test: `tests/engines/diffusers/test_server_health.py`

**Acceptance Criteria:**
- [ ] Pre-existing `model` key remains in the response payload (backward compat)
- [ ] `models` is a list of `{name, on_device, ready}` dicts, one entry per `_LOADED`
- [ ] `capabilities` is sorted list — `["t2v"]` if only Wan loaded; `["t2v","upscale"]` after a SeedVR2 ensure-on-gpu; `["upscale"]` for an upscale-only pod (Wan never loaded)
- [ ] `ready` field still reflects "primary pipeline loaded" semantics — unchanged

**Verify:** `pixi run pytest tests/engines/diffusers/test_server_health.py -v` → all pass.

**Steps:**

- [ ] **Step 1: RED**

```python
"""Tests for /health payload extension."""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient


def _client(monkeypatch, loaded: dict) -> TestClient:
    from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

    srv._LOADED.clear()
    srv._LOADED.update(loaded)
    return TestClient(srv.app)


class TestHealthBackwardCompat:
    def test_model_field_retained(self, monkeypatch) -> None:
        client = _client(
            monkeypatch,
            {
                "wan-t2v-a14b-fp8": {
                    "name": "wan-t2v-a14b-fp8",
                    "pipe": MagicMock(),
                    "vram_bytes": 0,
                    "last_used_monotonic": 0.0,
                    "on_device": "cuda",
                }
            },
        )
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert "model" in body   # backward compat
        assert body["ready"] is True


class TestCapabilities:
    def test_wan_only(self, monkeypatch) -> None:
        client = _client(
            monkeypatch,
            {
                "wan-t2v-a14b-fp8": {
                    "name": "wan-t2v-a14b-fp8",
                    "pipe": MagicMock(),
                    "vram_bytes": 0,
                    "last_used_monotonic": 0.0,
                    "on_device": "cuda",
                }
            },
        )
        body = client.get("/health").json()
        assert body["capabilities"] == ["t2v"]

    def test_wan_and_seedvr2(self, monkeypatch) -> None:
        client = _client(
            monkeypatch,
            {
                "wan-t2v-a14b-fp8": {
                    "name": "wan-t2v-a14b-fp8",
                    "pipe": MagicMock(),
                    "vram_bytes": 0,
                    "last_used_monotonic": 0.0,
                    "on_device": "cuda",
                },
                "seedvr2-3b-fp8": {
                    "name": "seedvr2-3b-fp8",
                    "pipe": MagicMock(),
                    "vram_bytes": 0,
                    "last_used_monotonic": 0.0,
                    "on_device": "cpu",
                },
            },
        )
        body = client.get("/health").json()
        assert sorted(body["capabilities"]) == ["t2v", "upscale"]

    def test_upscale_only(self, monkeypatch) -> None:
        client = _client(
            monkeypatch,
            {
                "seedvr2-3b-fp8": {
                    "name": "seedvr2-3b-fp8",
                    "pipe": MagicMock(),
                    "vram_bytes": 0,
                    "last_used_monotonic": 0.0,
                    "on_device": "cuda",
                },
            },
        )
        body = client.get("/health").json()
        assert body["capabilities"] == ["upscale"]
```

- [ ] **Step 2: Update `/health` handler**

```python
def _capabilities_from_loaded() -> list[str]:
    caps: set[str] = set()
    for name in _LOADED:
        if name.startswith("wan-t2v-"):
            caps.add("t2v")
        elif name.startswith("seedvr2-") or name.startswith("flashvsr-"):
            caps.add("upscale")
    return sorted(caps)


@app.get("/health")
async def health() -> dict:
    primary = next(
        (e for e in _LOADED.values() if e["name"].startswith("wan-t2v-")),
        None,
    )
    return {
        "ready": primary is not None and primary["on_device"] == "cuda",
        "model": primary["name"] if primary else "",          # legacy field
        "models": [
            {"name": e["name"], "on_device": e["on_device"], "ready": True}
            for e in _LOADED.values()
        ],
        "capabilities": _capabilities_from_loaded(),
    }
```

- [ ] **Step 3: GREEN + commit**

```
pixi run pytest tests/engines/diffusers/test_server_health.py -v
pixi run pre-commit run --files \
  src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
  tests/engines/diffusers/test_server_health.py
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
        tests/engines/diffusers/test_server_health.py
git commit -m "feat(upscale): /health payload — preserve 'model', add 'models[]' + 'capabilities[]'"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/servers/wan_t2v_server.py", "tests/engines/diffusers/test_server_health.py"], "verifyCommand": "pixi run pytest tests/engines/diffusers/test_server_health.py -v", "acceptanceCriteria": ["Legacy 'model' field preserved", "'models' list reflects _LOADED", "'capabilities' derives from actually-loaded pipelines"], "modelTier": "mechanical"}
```

---

## Task 14: `/health`-driven matcher pre-flight (`STAGE_MISMATCH` verdict)

**Goal:** Before claiming a warm-attach candidate, matcher fetches the pod's `/health`. If `capabilities` doesn't cover cfg's `stages`, refuse with `STAGE_MISMATCH` verdict.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_scan_warm_candidates` / `_resolve_warm_instance`)
- Modify: `src/kinoforge/core/warm_reuse/matcher.py` (if verdict enum lives there) — extend with `STAGE_MISMATCH`
- Test: `tests/test_warm_matcher_health_preflight.py`

**Acceptance Criteria:**
- [ ] Candidate pod whose `/health` returns `capabilities: ["t2v"]` for an upscale-only cfg → verdict `STAGE_MISMATCH`, candidate refused
- [ ] Candidate pod whose `/health` returns `capabilities: ["t2v","upscale"]` for an upscale-only cfg → accepted
- [ ] `/health` 404 / connection refused → existing fallback verdict (not `STAGE_MISMATCH`)

**Verify:** `pixi run pytest tests/test_warm_matcher_health_preflight.py -v` → all pass.

**Steps:**

- [ ] **Step 1: RED — `tests/test_warm_matcher_health_preflight.py`**

```python
"""Tests for the /health-driven matcher pre-flight."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@patch("kinoforge.cli._commands._http_get_json")
class TestHealthPreflight:
    def test_capability_subset_passes(self, http) -> None:
        http.return_value = {"capabilities": ["t2v", "upscale"], "ready": True}
        from kinoforge.cli._commands import _health_preflight_ok

        assert _health_preflight_ok(
            proxy_url="https://pod.example",
            want_stages=("upscale",),
        ) is True

    def test_capability_missing_refused(self, http) -> None:
        http.return_value = {"capabilities": ["t2v"], "ready": True}
        from kinoforge.cli._commands import _health_preflight_ok

        assert _health_preflight_ok(
            proxy_url="https://pod.example",
            want_stages=("upscale",),
        ) is False

    def test_health_unreachable_returns_unknown(self, http) -> None:
        http.side_effect = ConnectionError("refused")
        from kinoforge.cli._commands import _health_preflight_ok

        # Spec: unreachable health → fall through to existing fallback,
        # not a hard STAGE_MISMATCH refusal.
        assert _health_preflight_ok(
            proxy_url="https://pod.example",
            want_stages=("upscale",),
        ) is None
```

- [ ] **Step 2: Implement `_health_preflight_ok` in `_commands.py`**

```python
def _health_preflight_ok(
    *,
    proxy_url: str,
    want_stages: tuple[str, ...],
) -> bool | None:
    """Pre-flight check via /health before claiming a warm-attach candidate.

    Returns:
        True  — pod's capabilities is a superset of want_stages
        False — pod is reachable AND capabilities does NOT cover want_stages
                (caller should emit STAGE_MISMATCH verdict)
        None  — pod /health unreachable; fall through to legacy verdict
                machinery (do not synthesize STAGE_MISMATCH)
    """
    try:
        payload = _http_get_json(f"{proxy_url}/health")
    except (ConnectionError, TimeoutError, OSError):
        return None
    caps = set(payload.get("capabilities", []))
    return set(want_stages).issubset(caps)
```

Wire it into `_scan_warm_candidates` after the matcher returns a candidate but before claiming.

- [ ] **Step 3: Add `STAGE_MISMATCH` to the verdict enum**

```
rg -n "class Verdict|VerdictKind|MISMATCH" src/kinoforge/core/warm_reuse/
```

Locate the existing verdict enum and append `STAGE_MISMATCH`.

- [ ] **Step 4: GREEN + commit**

```
pixi run pytest tests/test_warm_matcher_health_preflight.py -v
pixi run pre-commit run --files \
  src/kinoforge/cli/_commands.py \
  src/kinoforge/core/warm_reuse/matcher.py \
  tests/test_warm_matcher_health_preflight.py
git add src/kinoforge/cli/_commands.py \
        src/kinoforge/core/warm_reuse/matcher.py \
        tests/test_warm_matcher_health_preflight.py
git commit -m "feat(upscale): /health-driven matcher preflight + STAGE_MISMATCH verdict"
```

```json:metadata
{"files": ["src/kinoforge/cli/_commands.py", "src/kinoforge/core/warm_reuse/matcher.py", "tests/test_warm_matcher_health_preflight.py"], "verifyCommand": "pixi run pytest tests/test_warm_matcher_health_preflight.py -v", "acceptanceCriteria": ["Subset capabilities pass", "Missing capability refused with STAGE_MISMATCH", "Unreachable /health falls through (does not synthesize STAGE_MISMATCH)"], "modelTier": "standard"}
```

---

## Task 15: `_cmd_upscale` CLI wiring

**Goal:** New `kinoforge upscale` subcommand mirroring `_cmd_generate` warm-reuse helpers usage.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (new `_cmd_upscale` function)
- Modify: `src/kinoforge/cli/_main.py` (subcommand registration + argparse wiring)
- Test: `tests/cli/test_cmd_upscale.py`

**Acceptance Criteria:**
- [ ] `kinoforge upscale --video x.mp4 --config c.yaml --dry-run` exits 0 with resolved plan emitted to stdout
- [ ] `kinoforge upscale` (no `--video`) → exits 2 with argparse usage
- [ ] `kinoforge upscale --video x.mp4 --config c.yaml --no-reuse --attach-pod abc` → exits 2 (mutual exclusion, message mirrors `_cmd_generate`)
- [ ] `--scale 2x` overrides `cfg.upscale.scale`
- [ ] `--scale 1080p` → exits 2 (caught at CLI startup via `ScaleTarget.parse` consumer raising `NotYetImplementedError`)

**Verify:** `pixi run pytest tests/cli/test_cmd_upscale.py -v` → all pass.

**Steps:**

- [ ] **Step 1: RED**

```python
"""Tests for the kinoforge upscale subcommand."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kinoforge.cli._main import main


class TestArgparse:
    def test_missing_video_exits_2(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["upscale", "--config", "/tmp/c.yaml"])
        assert exc.value.code == 2

    def test_no_reuse_and_attach_pod_mutual_exclusion(self, tmp_path) -> None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text("# stub\n")
        with pytest.raises(SystemExit) as exc:
            main([
                "upscale",
                "--video", "x.mp4",
                "--config", str(cfg),
                "--no-reuse",
                "--attach-pod", "abc",
            ])
        assert exc.value.code == 2


class TestScaleOverride:
    def test_height_target_refused_at_startup(self, tmp_path) -> None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text("# stub\n")
        with pytest.raises(SystemExit) as exc:
            main([
                "upscale",
                "--video", "x.mp4",
                "--config", str(cfg),
                "--scale", "1080p",
                "--dry-run",
            ])
        assert exc.value.code == 2


class TestDryRun:
    def test_dry_run_exits_zero(self, tmp_path, monkeypatch) -> None:
        # Patch Config.from_yaml + capability_key so dry-run skips real
        # cfg validation but exercises the rest of _cmd_upscale's pre-spend path.
        # Implementation: copy the dry-run scaffold from test_cmd_generate.py.
        pytest.skip(
            "Wire dry-run fixture by copying tests/cli/test_cmd_generate.py "
            "dry-run pattern. Same shape as Task 10's deferred HTTP test."
        )
```

- [ ] **Step 2: Implement `_cmd_upscale` + wiring**

`src/kinoforge/cli/_commands.py` — append:

```python
def _cmd_upscale(args: argparse.Namespace, ctx: SessionContext) -> int:
    """`kinoforge upscale` — standalone upscale invocation.

    Mirrors _cmd_generate's warm-reuse plumbing: scan / attach / cold-create,
    --no-reuse semantics, --attach-pod / --force-attach handling.
    """
    if args.no_reuse and args.attach_pod is not None:
        print(
            "error: --no-reuse and --attach-pod are mutually exclusive "
            "(--no-reuse forces cold create; --attach-pod implies survival)",
            file=sys.stderr,
        )
        return 2

    cfg = Config.from_yaml(args.config)
    if cfg.upscale is None:
        print("error: --config must contain an `upscale:` block", file=sys.stderr)
        return 2

    # Apply CLI override.
    scale_raw = args.scale or cfg.upscale.scale
    try:
        scale = ScaleTarget.parse(scale_raw)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if scale.kind == "height":
        print(
            f"error: --scale {scale_raw} deferred to a later session; use --scale Nx for v1",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        _print_upscale_plan(cfg, scale, args)
        return 0

    # Warm-reuse / attach / cold-create — mirrors _cmd_generate.
    # ... (copy the scan/attach skeleton from _cmd_generate, swapping GenerateClipStage for UpscaleStage)
    ...
    return 0
```

`src/kinoforge/cli/_main.py` — register `"upscale"` subcommand with the flags listed in spec §5.1.

- [ ] **Step 3: GREEN + commit**

```
pixi run pytest tests/cli/test_cmd_upscale.py -v
pixi run pre-commit run --files \
  src/kinoforge/cli/_commands.py \
  src/kinoforge/cli/_main.py \
  tests/cli/test_cmd_upscale.py
git add src/kinoforge/cli/_commands.py \
        src/kinoforge/cli/_main.py \
        tests/cli/test_cmd_upscale.py
git commit -m "feat(upscale): kinoforge upscale subcommand + warm-reuse plumbing"
```

```json:metadata
{"files": ["src/kinoforge/cli/_commands.py", "src/kinoforge/cli/_main.py", "tests/cli/test_cmd_upscale.py"], "verifyCommand": "pixi run pytest tests/cli/test_cmd_upscale.py -v", "acceptanceCriteria": ["Missing --video exits 2", "--no-reuse + --attach-pod mutual exclusion exits 2", "--scale 1080p refused at startup", "--dry-run emits resolved plan and exits 0"], "modelTier": "standard"}
```

---

## Task 16: `_adapters.py` self-register + example cfg + docs

**Goal:** Wire the new upscaler module into the adapters hub. Add a tracked example cfg. Document the new command + cfg block.

**Files:**
- Modify: `src/kinoforge/_adapters.py`
- Modify: `src/kinoforge/core/orchestrator.py` (append `UpscaleStage` to `stages` list when `cfg.upscale is not None`)
- Create: `examples/configs/upscale-seedvr2-3b.yaml`
- Modify: `docs/warm-reuse.md` (new subsection "Upscale-only pods")
- Modify: `docs/configuration.md` (document `upscale:` block schema)
- Modify: `docs/engines.md` (new "Upscalers" section pointing to SeedVR2)
- Modify: `README.md` (one-line mention under "Capabilities")

**Acceptance Criteria:**
- [ ] `import kinoforge` runs the SeedVR2 self-registration; `registry.upscaler_names()` includes `"seedvr2"`
- [ ] `kinoforge generate --config examples/configs/wan-with-upscale.yaml --dry-run` emits a plan that includes `UpscaleStage`
- [ ] `kinoforge upscale --help` lists every flag from spec §5.1
- [ ] Docs cross-link (CLI help → docs/engines.md → docs/configuration.md → docs/warm-reuse.md)

**Verify:** `pixi run pytest tests/test_adapters_upscale.py -v` + manual `kinoforge upscale --help` inspection.

**Steps:**

- [ ] **Step 1: RED — `tests/test_adapters_upscale.py`**

```python
"""Confirm SeedVR2Engine self-registers via _adapters import."""

from __future__ import annotations


def test_seedvr2_registered() -> None:
    # Importing kinoforge triggers _adapters which triggers seedvr2 import.
    import kinoforge  # noqa: F401
    from kinoforge.core import registry

    assert "seedvr2" in registry.upscaler_names()
```

- [ ] **Step 2: Edit `_adapters.py`**

Add to the "Engines" block:

```python
# Upscalers
import kinoforge.upscalers.seedvr2  # noqa: F401  self-registers as "seedvr2"
```

- [ ] **Step 3: Wire `UpscaleStage` into orchestrator's stage list**

Locate `stages: list[Stage] = [GenerateClipStage(...)]` in `core/orchestrator.py` (around line 1804). Add after the GenerateClipStage append:

```python
if cfg.upscale is not None:
    from kinoforge.pipeline.upscale import UpscaleStage

    upscaler_engine = registry.get_upscaler(cfg.upscale.engine)()
    upscaler_engine.attach_get_instance(_provider.get_instance)
    upscaler_engine.provision(instance, cfg.model_dump(), cancel_token=cancel_token)
    stages.append(
        UpscaleStage(
            engine=upscaler_engine,
            scale=ScaleTarget.parse(cfg.upscale.scale),
            instance=instance,
            cfg=cfg.model_dump(),
            cancel_token=cancel_token,
        )
    )
```

- [ ] **Step 4: Write `examples/configs/upscale-seedvr2-3b.yaml`**

```yaml
# Upscale-only cfg. Used by `kinoforge upscale --config this --video <path>`.
compute:
  provider: runpod
  image: kinoforge/wan:latest
  requirements:
    min_vram_gb: 24
    disk_gb: 100
  lifecycle:
    idle_timeout_s: 1800
    job_timeout_s: 1800

upscale:
  engine: seedvr2
  scale: 2x
  seedvr2:
    variant: 3B
    precision: fp8
```

Also commit a multi-stage example `examples/configs/wan-with-upscale.yaml` (base existing Wan cfg + the `upscale:` block above; copy from a passing Wan cfg under `examples/configs/`).

- [ ] **Step 5: Docs**

`docs/warm-reuse.md` — append:

```markdown
## Upscale-only pods

`kinoforge upscale` activates the same warm-reuse machinery as `kinoforge generate`. The CapabilityKey
for an upscale-only cfg has `stages=("upscale",)` and a `upscaler`/`upscaler_precision` factor pair
(e.g. `seedvr2` + `3b-fp8`). Two-pass matcher:

1. Primary — exact hash match on the cfg's `capability_key().derive()`.
2. Secondary — when primary returns nothing, a pod whose `kinoforge_stages` is a superset of the cfg
   stages AND whose `kinoforge_upscaler` + `kinoforge_upscaler_precision` match is accepted. This is
   how a `(t2v, upscale)` pod attached after a `kinoforge generate` run becomes reusable by
   subsequent `kinoforge upscale` calls.

Pods written before 2026-06-28 (no `kinoforge_stages` field in their ledger row) are not eligible
for the secondary pass — they are pure-generate pods and not upscale-capable.
```

`docs/configuration.md` — document the `upscale:` block:

```markdown
## `upscale:` (optional, video upscaling)

Presence activates the in-pipeline `UpscaleStage` after `GenerateClipStage` (for
`kinoforge generate`) or stands alone for `kinoforge upscale`.

| Key | Type | Default | Notes |
|---|---|---|---|
| `engine` | `"seedvr2"` | — | Required. v1 supports SeedVR2; FlashVSR drop-in is a future session. |
| `scale` | string | — | `"Nx"` for factor (works in v1); `"Np"` parses but raises `NotYetImplementedError` (future). |
| `seedvr2.variant` | `"3B"` \| `"7B"` | `"3B"` | Required when `engine == "seedvr2"`. |
| `seedvr2.precision` | `"fp8"` \| `"fp16"` | `"fp8"` | |
| `seedvr2.tile_size` | int \| null | `null` (engine default) | |
| `seedvr2.steps` | int \| null | `null` (engine default) | |
| `seedvr2.weights_ref` | string \| null | derived from `variant` | `"hf:ByteDance-Seed/SeedVR2-3B"` for variant=3B; override for forks. |
```

`docs/engines.md` — new section linking to `SeedVR2Engine`.

`README.md` — add to the Capabilities list:

```
- Engine-agnostic video upscaling (SeedVR2 default; FlashVSR seam) via `kinoforge upscale` or pipeline-stage activation.
```

- [ ] **Step 6: GREEN + manual check + commit**

```
pixi run pytest tests/test_adapters_upscale.py -v
pixi run kinoforge upscale --help
```

Expected: help text lists `--video`, `--config`, `--scale`, `--no-reuse`, `--attach-pod`, `--force-attach`, `--output`, `--ephemeral`, `--dry-run`.

```
pixi run pre-commit run --files \
  src/kinoforge/_adapters.py \
  src/kinoforge/core/orchestrator.py \
  examples/configs/upscale-seedvr2-3b.yaml \
  examples/configs/wan-with-upscale.yaml \
  docs/warm-reuse.md \
  docs/configuration.md \
  docs/engines.md \
  README.md \
  tests/test_adapters_upscale.py
git add src/kinoforge/_adapters.py \
        src/kinoforge/core/orchestrator.py \
        examples/configs/upscale-seedvr2-3b.yaml \
        examples/configs/wan-with-upscale.yaml \
        docs/warm-reuse.md \
        docs/configuration.md \
        docs/engines.md \
        README.md \
        tests/test_adapters_upscale.py
git commit -m "feat(upscale): adapters self-register + orchestrator wires UpscaleStage + docs"
```

```json:metadata
{"files": ["src/kinoforge/_adapters.py", "src/kinoforge/core/orchestrator.py", "examples/configs/upscale-seedvr2-3b.yaml", "examples/configs/wan-with-upscale.yaml", "docs/warm-reuse.md", "docs/configuration.md", "docs/engines.md", "README.md", "tests/test_adapters_upscale.py"], "verifyCommand": "pixi run pytest tests/test_adapters_upscale.py -v && pixi run kinoforge upscale --help", "acceptanceCriteria": ["seedvr2 registered after import kinoforge", "Orchestrator appends UpscaleStage when cfg.upscale is not None", "Example cfgs committed", "Docs updated (warm-reuse, configuration, engines, README)"], "modelTier": "mechanical"}
```

---

## Task 17: Live smoke RED scaffold — upscale-only one-shot (committed BEFORE live spend)

**Goal:** Per project rule `commit RED scaffolds before any live spend`, land the live smoke for the upscale-only path as a RED (intentionally-failing) scaffold in its own commit before any live RunPod work.

**Files:**
- Create: `tests/live/test_seedvr2_3b_fp8_upscale_smoke.py` (marked `pytest.mark.live` and `pytest.mark.xfail(reason="RED scaffold — green evidence lands in Task 18")`)
- Create: `tests/live/evidence/2026-06-28-seedvr2-3b-fp8-upscale/.gitkeep`

**Acceptance Criteria:**
- [ ] Test file exists, marked `xfail` with a clear reason
- [ ] `pixi run pytest tests/live/test_seedvr2_3b_fp8_upscale_smoke.py -v --no-header -rA` reports xfail (not error)
- [ ] No live RunPod calls executed
- [ ] Test asserts (when un-xfailed): output dimensions == 2x input via ffprobe, sha256(input frame) != sha256(output frame), `kinoforge list` post-exit reports empty
- [ ] Smoke runs through `pixi run kinoforge upscale --video <fixture> --config examples/configs/upscale-seedvr2-3b.yaml --no-reuse`
- [ ] Polling cadence per `Live smoke monitoring`: every 60-90s query GPU/CPU/mem; bail early if GPU stays at 0% for 3 consecutive probes

**Verify:** `pixi run pytest tests/live/test_seedvr2_3b_fp8_upscale_smoke.py -v` → reports XFAIL.

**Steps:**

- [ ] **Step 1: Locate a small input fixture**

Use an existing low-res clip from `examples/` (search `fd "\.mp4$" examples/`). If none small enough, generate one with ffmpeg + a placeholder upstream Wan output kept under 50 MB — committed as a tracked fixture.

- [ ] **Step 2: Write `tests/live/test_seedvr2_3b_fp8_upscale_smoke.py`**

```python
"""Live smoke — SeedVR2 3B FP8 upscale of a known low-res clip.

RED scaffold per project rule `commit RED scaffolds before any live spend`.
Task 18 removes the xfail mark + lands GREEN evidence.

Polling cadence: every 60-90s during the run, probe RunPod runtime.gpus[].gpuUtilPercent,
runtime.container.cpuPercent, runtime.container.memoryPercent, costPerHr. Bail early on
3 consecutive 0% GPU probes.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest


_FIXTURE = Path(__file__).parent.parent / "fixtures" / "low-res-480p.mp4"
_EVIDENCE_DIR = Path(__file__).parent / "evidence" / "2026-06-28-seedvr2-3b-fp8-upscale"
_CFG = Path(__file__).parent.parent.parent / "examples" / "configs" / "upscale-seedvr2-3b.yaml"


@pytest.mark.live
@pytest.mark.xfail(reason="RED scaffold — green evidence lands in Task 18")
def test_seedvr2_3b_fp8_upscales_2x() -> None:
    assert _FIXTURE.exists(), f"input fixture missing: {_FIXTURE}"
    assert _CFG.exists(), f"cfg missing: {_CFG}"

    out_dir = _EVIDENCE_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [
            "pixi", "run", "kinoforge", "upscale",
            "--video", str(_FIXTURE),
            "--config", str(_CFG),
            "--no-reuse",
            "--output", str(out_dir),
        ],
        capture_output=True,
        text=True,
        timeout=2400,   # 40min ceiling
    )
    (_EVIDENCE_DIR / "stdout.txt").write_text(proc.stdout)
    (_EVIDENCE_DIR / "stderr.txt").write_text(proc.stderr)
    assert proc.returncode == 0, proc.stderr

    # Resolution check via ffprobe.
    in_w, in_h = _probe_resolution(_FIXTURE)
    out_files = sorted(out_dir.rglob("*.mp4"))
    assert out_files, "no output mp4 produced"
    out_w, out_h = _probe_resolution(out_files[-1])
    assert (out_w, out_h) == (in_w * 2, in_h * 2), (
        f"expected {in_w*2}x{in_h*2}, got {out_w}x{out_h}"
    )

    # Frame-level differs — sha256 of first frame as PNG must change.
    in_sha = _first_frame_sha256(_FIXTURE)
    out_sha = _first_frame_sha256(out_files[-1])
    assert in_sha != out_sha, "output frame identical to input — no upscale work"

    # Ledger empty post-exit — per --no-reuse + project memory.
    ledger = subprocess.run(
        ["pixi", "run", "kinoforge", "list", "--json"],
        capture_output=True, text=True, timeout=60,
    )
    pods = json.loads(ledger.stdout).get("instances", [])
    assert pods == [], f"pod survived --no-reuse: {pods}"


def _probe_resolution(path: Path) -> tuple[int, int]:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=s=x:p=0", str(path)],
        text=True,
    ).strip()
    w, h = out.split("x")
    return int(w), int(h)


def _first_frame_sha256(path: Path) -> str:
    out = subprocess.check_output(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", "select=eq(n\\,0)", "-vsync", "vfr",
         "-f", "image2pipe", "-vcodec", "png", "-"],
    )
    return hashlib.sha256(out).hexdigest()
```

- [ ] **Step 3: Verify xfail**

```
pixi run pytest tests/live/test_seedvr2_3b_fp8_upscale_smoke.py -v -rA
```

Expected: `XFAIL` with the reason.

- [ ] **Step 4: Commit RED**

```
git add tests/live/test_seedvr2_3b_fp8_upscale_smoke.py \
        tests/live/evidence/2026-06-28-seedvr2-3b-fp8-upscale/.gitkeep
git commit -m "test(live): RED scaffold — SeedVR2 3B FP8 2x upscale smoke

Per CLAUDE.md durability rule, RED scaffold lands BEFORE live spend so a
mid-spend crash can't lose 100+ LOC of test machinery and tempt a wholesale
git checkout cleanup. GREEN evidence lands in Task 18."
```

```json:metadata
{"files": ["tests/live/test_seedvr2_3b_fp8_upscale_smoke.py"], "verifyCommand": "pixi run pytest tests/live/test_seedvr2_3b_fp8_upscale_smoke.py -v", "acceptanceCriteria": ["File exists with xfail mark", "Asserts cover resolution, frame-diff, ledger empty post-exit"], "modelTier": "mechanical"}
```

---

## Task 18: Live smoke GREEN — execute the upscale-only smoke

**Goal:** Run the RED scaffold against live RunPod. Remove the xfail. Commit evidence. Per `feedback_autonomous_no_gates`, live spend pre-authorized within session budget.

**Files:**
- Modify: `tests/live/test_seedvr2_3b_fp8_upscale_smoke.py` (remove `@pytest.mark.xfail`)
- Create: evidence artifacts under `tests/live/evidence/2026-06-28-seedvr2-3b-fp8-upscale/`
- Update: `/workspace/successful-generations.md` per CLAUDE.md schema

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 BEFORE invoking the smoke
- [ ] Smoke runs through to GREEN
- [ ] Evidence committed: `stdout.txt`, `stderr.txt`, `out/<filename>.mp4` (or its sha256+size if too large), `runtime-probes.jsonl` (per-tick probe captures)
- [ ] `kinoforge list` post-exit reports `No running instances` AND `No instances recorded in ledger.` together
- [ ] `successful-generations.md` updated per schema (new capability axis: upscale)

**Verify:** `pixi run pytest tests/live/test_seedvr2_3b_fp8_upscale_smoke.py -v` → PASS.

**Steps:**

- [ ] **Step 1: Mechanical preflight**

```
pixi run preflight
```

Expected exit 0. RUNPOD/HF creds present, no active pods, clean tree.

- [ ] **Step 2: Remove xfail mark**

```
# In tests/live/test_seedvr2_3b_fp8_upscale_smoke.py:
# Delete the line: @pytest.mark.xfail(reason="RED scaffold — ...")
```

- [ ] **Step 3: Run the smoke with proactive monitoring**

```
pixi run pytest tests/live/test_seedvr2_3b_fp8_upscale_smoke.py -v --no-header -rA
```

While running, in a background process: poll RunPod runtime every 60-90s. Per `feedback_proactive_pod_stats`, the agent surfaces idle-pod signatures without being asked. Record probes to `tests/live/evidence/2026-06-28-seedvr2-3b-fp8-upscale/runtime-probes.jsonl`.

If GPU stays at 0% for ≥3 consecutive probes during a generation phase → capture last 100 lines of server log, destroy pod, fail-fast.

- [ ] **Step 4: Verify teardown**

```
pixi run kinoforge list
```

Expected: `No running instances.` AND `No instances recorded in ledger.` together. If anything survives, `pixi run kinoforge destroy --id <pod-id>` and capture the over-charge in the evidence directory.

- [ ] **Step 5: Capture evidence + log to successful-generations.md**

Evidence directory contents:
- `stdout.txt`, `stderr.txt` from the smoke
- `out/<filename>.mp4` (or its sha256 + size + ffprobe metadata if size > 50 MB — large binaries don't commit)
- `runtime-probes.jsonl`
- `ledger-post-exit.txt` from `kinoforge list`
- `verdict.md` — PASS/FAIL with one-line reason

Append to `/workspace/successful-generations.md` per its schema:
- new section because the `(provider, engine, model, mode)` tuple `(runpod, diffusers+seedvr2, wan-t2v-a14b-fp8 + seedvr2-3b-fp8, t2v+upscale-2x)` is a new capability axis.

- [ ] **Step 6: Commit**

```
git add tests/live/test_seedvr2_3b_fp8_upscale_smoke.py \
        tests/live/evidence/2026-06-28-seedvr2-3b-fp8-upscale/ \
        /workspace/successful-generations.md
git commit -m "test(live): GREEN evidence — SeedVR2 3B FP8 2x upscale

Verdict PASS. Output dimensions match expected 2x. Ledger empty post-exit
per --no-reuse. Runtime probes attached. Successful-generations log updated."
```

```json:metadata
{"files": ["tests/live/test_seedvr2_3b_fp8_upscale_smoke.py", "tests/live/evidence/2026-06-28-seedvr2-3b-fp8-upscale/", "successful-generations.md"], "verifyCommand": "pixi run pytest tests/live/test_seedvr2_3b_fp8_upscale_smoke.py -v && pixi run kinoforge list", "acceptanceCriteria": ["Preflight exits 0", "Smoke passes", "Ledger empty post-exit", "Evidence committed", "successful-generations.md updated"], "modelTier": "live-spend"}
```

---

## Task 19: Live smoke — Wan T2V + SeedVR2 multi-stage warm-reuse

**Goal:** RED scaffold (same commit-before-spend rule) + live execution of the multi-stage scenario: `kinoforge generate` against a cfg with both `engine:` and `upscale:` blocks, then a follow-up `kinoforge upscale --video <other clip>` confirms warm-attach to the multi-stage pod.

**Files:**
- Create: `tests/live/test_wan_then_upscale_warm_reuse_smoke.py`
- Create: `tests/live/evidence/2026-06-28-wan-then-upscale-warm-reuse/`
- Update: `/workspace/successful-generations.md` ("See also" line under the prior entry — same tuple-touching capability, multi-stage variant)

**Acceptance Criteria:**
- [ ] RED scaffold commit lands first (xfail), then GREEN commit removes xfail + adds evidence
- [ ] First call (`kinoforge generate`) lands both `clip` AND `upscaled` artifacts in the output dir
- [ ] Pod's `/health` returns `capabilities: ["t2v", "upscale"]` mid-run
- [ ] Second call (`kinoforge upscale --video <other-clip>`) attaches the warm pod (no cold-boot — boot timing < 60s)
- [ ] Both clips have correct upscaled dimensions
- [ ] Teardown verified via `kinoforge list` (warm-reuse mid-test → explicit destroy at end)
- [ ] Generation half reads prompt from `/workspace/examples/configs/prompts/field-realistic.txt` per `feedback_standard_test_prompt`

**Verify:** `pixi run pytest tests/live/test_wan_then_upscale_warm_reuse_smoke.py -v` → PASS.

**Steps:**

- [ ] **Step 1: RED scaffold commit** (same pattern as Task 17, separate commit)
- [ ] **Step 2: Mechanical preflight** (`pixi run preflight` → 0)
- [ ] **Step 3: Remove xfail + execute** with the same proactive-probe loop
- [ ] **Step 4: Capture evidence + teardown** (`pixi run kinoforge destroy --id <pod>`; verify `kinoforge list` empty)
- [ ] **Step 5: Update `successful-generations.md`** ("See also" under Task 18's section — same provider+engine, multi-stage mode)
- [ ] **Step 6: Commit GREEN**

```json:metadata
{"files": ["tests/live/test_wan_then_upscale_warm_reuse_smoke.py", "tests/live/evidence/2026-06-28-wan-then-upscale-warm-reuse/", "successful-generations.md"], "verifyCommand": "pixi run pytest tests/live/test_wan_then_upscale_warm_reuse_smoke.py -v && pixi run kinoforge list", "acceptanceCriteria": ["RED scaffold commit precedes live spend", "Generate produces clip + upscaled artifacts", "/health advertises both capabilities", "Second upscale attaches warm pod", "Teardown verified post-exit"], "modelTier": "live-spend"}
```

---

## Task 20: `PROGRESS.md` close + workstream shipped

**Goal:** Update PROGRESS.md per CLAUDE.md durability rules; close the workstream pointer; cross-link spec + plan + key commits.

**Files:**
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] PROGRESS.md gains a new shipped section dated today (2026-06-28) summarising the workstream, key commits, gotchas, and "Workstream CLOSED"
- [ ] Active workstream block returns to "No active workstream — next initiative TBD."
- [ ] Cross-references: spec path, plan path, smoke evidence dirs, commit ranges

**Verify:** `git log --oneline -10` shows the close commit; `cat PROGRESS.md` matches the expected shape.

**Steps:**

- [ ] **Step 1: Edit PROGRESS.md**

Insert a new section at the top of the recently-shipped list (after the civarchive entry):

```markdown
**Video upscaling SHIPPED 2026-06-28 (commits `<first>..<last>`, all 21 tasks GREEN).**
Spec `docs/superpowers/specs/2026-06-28-video-upscaling-design.md` +
plan `docs/superpowers/plans/2026-06-28-video-upscaling.md`.
Engine-agnostic `UpscalerEngine` ABC + `register_upscaler` registry, sibling to
`GenerationEngine`/`ImageEngine`. New `kinoforge upscale` subcommand for one-shot
upscales; cfg `upscale:` block triggers in-pipeline `UpscaleStage` after
`GenerateClipStage`. SeedVR2 3B FP8 default via direct upstream Python on the
same FastAPI server as Wan T2V; `/upscale` + `/upscale/status/{id}` endpoints
share the in-process `_LOADED` model registry with LRU CPU eviction + hard-floor
`VRAMEvictionFailed`. `CapabilityKey` gains `stages` + `upscaler` +
`upscaler_precision` factors with byte-equal backward-compat hash for legacy
ledger entries (conditional-extend trick in `derive()`). Warm-matcher gains a
secondary subset pass — multi-stage `(t2v,upscale)` pods attach to upscale-only
cfgs without breaking primary hash-equality matching. Live evidence: upscale-only
+ multi-stage warm-reuse smokes both GREEN, ledger empty post-exit. Foundation
for the future frame-interp stage (LRU registry + stages factor + capabilities
advertisement + multi-stage matcher) ships in this workstream — no retrofit
required.
**Workstream CLOSED.**

---
```

Update the "Active workstream" block back to "No active workstream — next initiative TBD."

- [ ] **Step 2: Commit**

```
git add PROGRESS.md
git commit -m "docs(progress): SHIPPED — video upscaling (engine-agnostic, SeedVR2 default)"
```

```json:metadata
{"files": ["PROGRESS.md"], "verifyCommand": "git log --oneline -1 PROGRESS.md", "acceptanceCriteria": ["PROGRESS.md updated with shipped section", "Cross-references spec + plan + smokes", "Active workstream reset"], "modelTier": "mechanical"}
```

---

## Self-review

Run through the spec one more time:

- §3 module layout → Tasks 0 (scale_target), 1 (UpscaleJob/Result), 2 (UpscalerEngine), 5 (Config), 6 (UpscaleStage), 8/9/10 (upscalers/seedvr2/), 11/12/13 (server), 16 (_adapters + orchestrator) ✓
- §4 interface → Tasks 0, 1, 2 ✓
- §5 CLI → Task 15 + Task 16 (docs) ✓
- §6 server endpoints + LRU + SeedVR2 runtime → Tasks 11, 12, 13, 8, 9, 10 ✓
- §7 warm-reuse stages factor → Tasks 3, 4, 7, 14 ✓
- §8 errors → Tasks 0, 2 ✓
- §9 testing → tests in every task (TDD inside) ✓
- §10 extensibility seams → already enforced by ABC + registry design (Tasks 2, 8, 10); no separate task ✓
- §11 risks → covered by `_retry_proxy_call` usage in T10, hard-floor `VRAMEvictionFailed` in T11, golden-hash test in T3, `_REGISTRY_LOCK` in T11, warmup pattern in T18 ✓

No placeholders introduced (the `<pinned-commit-sha>` constant in T10 is documented as "select at exec time" per spec §6.3 — same exception the spec self-review made).

Type consistency:
- `ScaleTarget.parse` returns `ScaleTarget` everywhere it's used. ✓
- `UpscaleJob` / `UpscaleResult` field names match across T1, T6, T10. ✓
- `UpscalerEngine.upscale` signature matches engine impl in T10 and stage usage in T6. ✓
- `_ensure_on_gpu(name)` returns `LoadedModel` in T11; consumers in T12 destructure `entry["pipe"]`. ✓
- `register_upscaler` / `get_upscaler` / `upscaler_names` signature consistent across T2 / T10 / T16. ✓

No user-gate tasks tagged. Per `feedback_autonomous_no_gates` memory and the trigger rule (Verbs-only matches do not trigger), every task uses routine verification language ("verify", "check") without scope ("first on one"), proof ("prove it works"), or named gate-noun. Live smokes are routine TDD with autonomous live-spend authorization, not user-thrown gates.

---

## Task persistence

Native task snapshot lives at `docs/superpowers/plans/2026-06-28-video-upscaling.md.tasks.json` — 21 tasks (T0-T20), pending, with `blockedBy` dependencies per the diagram at the top of this file.
