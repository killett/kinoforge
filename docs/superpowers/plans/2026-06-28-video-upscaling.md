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

> **Plan continues in `2026-06-28-video-upscaling-part-2.md`** — Tasks 6 through 20 (UpscaleStage, warm-matcher subset pass, SeedVR2 runtime + engine, server endpoints, CLI command, adapters wiring, live smokes, PROGRESS close). The split exists because the per-task TDD detail volume exceeded a single output window; the part-2 file is co-located in `docs/superpowers/plans/` and indexed from `.tasks.json` as the continuation.

---

> **Continuation:** see Tasks 6-20 in the part-2 file written next.
