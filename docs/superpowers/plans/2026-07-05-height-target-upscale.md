# Height-target upscaling (1080p / 720p) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `--scale`/`cfg.upscale.scale` accept a vertical-resolution target (`1080p`, `720p`) that resolves to an engine factor plus a smooth downscale, capping deliverable size.

**Architecture:** A pure `resolve_height_target` resolver (core) picks the smallest sufficient factor from an engine's declared `supported_scales`; `UpscaleStage` runs the engine at that factor and stashes a `downscale_to` on the upscaled artifact's `.meta`; the orchestrator's post-stage materialize block lanczos-downscales the (now-local) bytes to the requested height before publishing. Engine-agnostic; only FlashVSR is live-validated this pass.

**Tech Stack:** Python 3.12, pydantic configs, ffmpeg/ffprobe (injectable subprocess seams), pytest, RunPod (live smoke only).

**User decisions (already made):**
- Cover all three engines via an engine-agnostic resolver (no per-engine special-casing).
- Downscale runs locally at the materialize boundary (not pod-side, not a mid-walk stage — bytes are pod-side until materialize).
- Delivered artifact = downscaled only; large intermediate discarded (no keep flag).
- Source already ≥ target → downscale-only, skip the GPU upscaler.
- Largest factor still short of target → raise a clear error; no under-target delivery.
- Smallest-sufficient factor selection (least overshoot).
- Live-smoke FlashVSR only; spandrel/seedvr2 offline-only this pass.

Spec: `docs/superpowers/specs/2026-07-05-height-target-upscale-design.md` (incl. §5 planning-time correction: downscale moved from a mid-walk stage to the materialize boundary).

---

## File Structure

- `src/kinoforge/core/errors.py` — **modify**: add `ScaleUnsatisfiableError`.
- `src/kinoforge/core/scale_resolver.py` — **create**: pure `HeightPlan` + `resolve_height_target`.
- `src/kinoforge/core/frames.py` — **modify**: add `_default_probe_run` + `ffprobe_dims`.
- `src/kinoforge/pipeline/downscale.py` — **create**: `downscale_video_bytes`.
- `src/kinoforge/pipeline/materialize.py` — **create**: `finalize_upscaled_bytes` (tested seam for the orchestrator).
- `src/kinoforge/core/config.py` — **modify**: relax `UpscaleConfig._validate_flashvsr_wiring` to accept height targets.
- `src/kinoforge/pipeline/upscale.py` — **modify**: `UpscaleStage` height-awareness (replace the height raise with resolution + meta stash).
- `src/kinoforge/core/orchestrator.py` — **modify**: materialize block calls `finalize_upscaled_bytes`; broaden to file:// when a downscale is pending.
- `examples/configs/upscale-flashvsr-1080p.yaml` — **create**: live-smoke cfg (height target).
- `tests/` — new unit tests per task; `tests/live/` — env-gated live smoke.

Engine/runtime `kind=="height"` guards (`seedvr2/_runtime.py`, `spandrel/_runtime.py`, `flashvsr/_runtime.py`, both engine `validate_spec`s) are **kept** as upstream-resolution invariants — `UpscaleStage` never passes height to an engine now.

---

### Task 0: Resolver + ScaleUnsatisfiableError

**Goal:** A pure, fully unit-tested height→factor resolver plus the undershoot error it raises.

**Files:**
- Modify: `src/kinoforge/core/errors.py` (append after `UnsupportedScaleError`, ~line 420)
- Create: `src/kinoforge/core/scale_resolver.py`
- Test: `tests/core/test_scale_resolver.py`

**Acceptance Criteria:**
- [ ] `resolve_height_target` returns the smallest sufficient factor and correct `downscale_to`.
- [ ] Source ≥ target → `upscale_factor=None`; exact-equal → `downscale_to=None`.
- [ ] Undershoot raises `ScaleUnsatisfiableError` carrying source/factor/reached/requested.
- [ ] Empty `supported_factors` raises `ValueError`.

**Verify:** `pixi run test tests/core/test_scale_resolver.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_scale_resolver.py
"""Tests for the pure height-target upscale resolver."""
import pytest

from kinoforge.core.errors import ScaleUnsatisfiableError
from kinoforge.core.scale_resolver import HeightPlan, resolve_height_target


def test_overshoot_picks_smallest_sufficient_factor():
    # Behaviour: 480p → 1080p with a (2x,4x) menu. 2x=960<1080 insufficient,
    # 4x=1920>=1080 sufficient. Bug caught: picking 2x (undersized) or picking
    # the largest factor blindly (needless overshoot when a smaller one fits).
    assert resolve_height_target(480, (2.0, 4.0), 1080) == HeightPlan(
        upscale_factor=4.0, downscale_to=1080
    )


def test_exact_hit_sets_no_downscale():
    # Behaviour: 540p × 2 == 1080p exactly. Bug caught: emitting a needless
    # downscale (re-encode) when the factor already lands on the target.
    assert resolve_height_target(540, (2.0, 4.0), 1080) == HeightPlan(
        upscale_factor=2.0, downscale_to=None
    )


def test_single_factor_menu_flashvsr():
    # Behaviour: FlashVSR (4x only) 480p → 1080p. Bug caught: resolver assuming
    # a multi-entry menu and IndexError-ing on a one-factor engine.
    assert resolve_height_target(480, (4.0,), 1080) == HeightPlan(
        upscale_factor=4.0, downscale_to=1080
    )


def test_source_taller_than_target_is_downscale_only():
    # Behaviour: 1080p source, 720p requested → skip GPU, downscale to 720.
    # Bug caught: forcing an upscale (or erroring) when the source is already big.
    assert resolve_height_target(1080, (2.0, 4.0), 720) == HeightPlan(
        upscale_factor=None, downscale_to=720
    )


def test_source_equals_target_is_passthrough():
    # Behaviour: source == target → no upscale, no downscale. Bug caught: a
    # no-op re-encode when nothing needs to change.
    assert resolve_height_target(720, (2.0, 4.0), 720) == HeightPlan(
        upscale_factor=None, downscale_to=None
    )


def test_undershoot_raises_with_context():
    # Behaviour: 240p, 4x-only, want 1080p → 960p < 1080p, unsatisfiable.
    # Bug caught: silently delivering a below-target result.
    with pytest.raises(ScaleUnsatisfiableError) as ei:
        resolve_height_target(240, (4.0,), 1080)
    err = ei.value
    assert err.source_h == 240
    assert err.largest_factor == 4.0
    assert err.reached_h == 960
    assert err.requested_h == 1080


def test_empty_factors_raises_valueerror():
    # Behaviour: an engine with no declared factors can't serve a height target.
    with pytest.raises(ValueError, match="non-empty"):
        resolve_height_target(480, (), 1080)
```

- [ ] **Step 2: Run — confirm fail**

Run: `pixi run test tests/core/test_scale_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: kinoforge.core.scale_resolver` / `ImportError: ScaleUnsatisfiableError`.

- [ ] **Step 3: Add the error** — append to `src/kinoforge/core/errors.py` after `UnsupportedScaleError` (before `UpscaleFailed`):

```python
class ScaleUnsatisfiableError(KinoforgeError):
    """No supported upscale factor can reach the requested height target.

    Raised by :func:`kinoforge.core.scale_resolver.resolve_height_target` when
    even the largest declared factor leaves the output below the requested
    vertical resolution. Carries full context for post-mortem without session
    memory.
    """

    def __init__(
        self, source_h: int, largest_factor: float, reached_h: int, requested_h: int
    ) -> None:
        """Record source height, largest factor, reached height, and target."""
        super().__init__(
            f"no supported factor reaches {requested_h}p: source {source_h}p x "
            f"largest factor {largest_factor:g} = {reached_h}p (< {requested_h}p); "
            f"use a larger-factor engine"
        )
        self.source_h = source_h
        self.largest_factor = largest_factor
        self.reached_h = reached_h
        self.requested_h = requested_h
```

- [ ] **Step 4: Create the resolver** — `src/kinoforge/core/scale_resolver.py`:

```python
"""Pure height-target -> factor resolver for the upscale pipeline.

Engine-agnostic: consumes an engine's declared factor menu and decides which
multiplier (if any) to run and whether a post-upscale downscale is needed to hit
the requested vertical resolution. No I/O, no torch -- a pure function so the
whole decision table is unit-tested with zero cloud spend.
"""

from __future__ import annotations

from dataclasses import dataclass

from kinoforge.core.errors import ScaleUnsatisfiableError


@dataclass(frozen=True)
class HeightPlan:
    """Resolved plan for a height-target upscale.

    Attributes:
        upscale_factor: Multiplier to run the engine at, or ``None`` when the
            source already meets/exceeds the target (skip the GPU upscale).
        downscale_to: Vertical resolution to downscale to after upscaling, or
            ``None`` when nothing needs shrinking (exact hit or passthrough).
    """

    upscale_factor: float | None
    downscale_to: int | None


def resolve_height_target(
    source_h: int,
    supported_factors: tuple[float, ...],
    requested_h: int,
) -> HeightPlan:
    """Resolve a requested vertical resolution against an engine's factor menu.

    Picks the smallest supported factor whose result meets or exceeds
    ``requested_h`` (least overshoot -> smallest intermediate + least downscale
    loss), and reports whether a post-upscale downscale is needed.

    Args:
        source_h: Source clip vertical resolution in pixels.
        supported_factors: The engine's declared upscale factors (e.g.
            ``(4.0,)`` for FlashVSR, ``(2.0, 4.0)`` for SeedVR2). Non-empty.
        requested_h: Requested output vertical resolution in pixels.

    Returns:
        A :class:`HeightPlan`.

    Raises:
        ValueError: ``supported_factors`` is empty.
        ScaleUnsatisfiableError: Even the largest factor cannot reach the target.
    """
    if not supported_factors:
        raise ValueError("supported_factors must be non-empty for a height target")
    if source_h >= requested_h:
        downscale_to = None if source_h == requested_h else requested_h
        return HeightPlan(upscale_factor=None, downscale_to=downscale_to)
    candidates = sorted(f for f in supported_factors if source_h * f >= requested_h)
    if not candidates:
        largest = max(supported_factors)
        raise ScaleUnsatisfiableError(
            source_h=source_h,
            largest_factor=largest,
            reached_h=int(source_h * largest),
            requested_h=requested_h,
        )
    factor = candidates[0]
    downscale_to = None if source_h * factor == requested_h else requested_h
    return HeightPlan(upscale_factor=factor, downscale_to=downscale_to)
```

- [ ] **Step 5: Run — confirm pass**

Run: `pixi run test tests/core/test_scale_resolver.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/errors.py src/kinoforge/core/scale_resolver.py tests/core/test_scale_resolver.py
git commit -m "feat(upscale): pure height-target resolver + ScaleUnsatisfiableError"
```

---

### Task 1: `ffprobe_dims` helper

**Goal:** Probe a video's (width, height) via ffprobe with an injectable seam.

**Files:**
- Modify: `src/kinoforge/core/frames.py` (add after `_default_probe_duration`, ~line 171)
- Test: `tests/core/test_frames_dims.py`

**Acceptance Criteria:**
- [ ] `ffprobe_dims` returns `(w, h)` parsed from `WxH` ffprobe output.
- [ ] Injectable `run` seam so the unit test spawns no binary.
- [ ] Unparseable output raises `FrameExtractionError`.

**Verify:** `pixi run test tests/core/test_frames_dims.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_frames_dims.py
"""Tests for ffprobe_dims."""
import pytest

from kinoforge.core.errors import FrameExtractionError
from kinoforge.core.frames import ffprobe_dims


def test_parses_width_x_height():
    # Behaviour: ffprobe csv "1920x1080" -> (1920, 1080). Bug caught: swapping
    # width/height or off-by-one CSV parsing.
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return b"1920x1080\n"

    assert ffprobe_dims("clip.mp4", run=fake_run) == (1920, 1080)
    # argv targets the first video stream and requests width,height as WxH.
    assert "v:0" in calls[0]
    assert "stream=width,height" in calls[0]


def test_unparseable_output_raises():
    # Behaviour: "N/A" (no video stream) -> FrameExtractionError, not ValueError.
    with pytest.raises(FrameExtractionError, match="unparseable"):
        ffprobe_dims("clip.mp4", run=lambda argv: b"N/A\n")
```

- [ ] **Step 2: Run — confirm fail**

Run: `pixi run test tests/core/test_frames_dims.py -v`
Expected: FAIL — `ImportError: cannot import name 'ffprobe_dims'`.

- [ ] **Step 3: Add the helper** — insert into `src/kinoforge/core/frames.py` after `_default_probe_duration` (line ~171):

```python
def _default_probe_run(argv: list[str]) -> bytes:
    """Run an ffprobe *argv*; return stdout; raise on missing binary / non-zero.

    Args:
        argv: The ffprobe command line.

    Returns:
        The subprocess's stdout bytes.

    Raises:
        FrameExtractionError: ffprobe missing from PATH or non-zero exit.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, check=False)  # noqa: S603
    except FileNotFoundError as exc:
        raise FrameExtractionError(f"ffprobe not found on PATH: {exc}") from exc
    if proc.returncode != 0:
        stderr_snip = proc.stderr.decode(errors="replace")[:512]
        raise FrameExtractionError(f"ffprobe exit {proc.returncode}: {stderr_snip}")
    return proc.stdout


def ffprobe_dims(
    video_path: str | Path,
    *,
    run: Callable[[list[str]], bytes] = _default_probe_run,
) -> tuple[int, int]:
    """Probe ``(width, height)`` of the first video stream via ffprobe.

    Args:
        video_path: Path to the video file on disk.
        run: Injectable seam ``(argv) -> stdout`` so tests spawn no binary.

    Returns:
        ``(width, height)`` in pixels.

    Raises:
        FrameExtractionError: ffprobe missing / non-zero exit, or output that
            does not parse as ``WxH``.
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=s=x:p=0",
        str(video_path),
    ]
    raw = run(argv).decode(errors="replace").strip()
    try:
        w_str, h_str = raw.split("x")
        return int(w_str), int(h_str)
    except ValueError as exc:
        raise FrameExtractionError(
            f"unparseable ffprobe dims {raw!r} for {video_path}"
        ) from exc
```

- [ ] **Step 4: Run — confirm pass**

Run: `pixi run test tests/core/test_frames_dims.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/frames.py tests/core/test_frames_dims.py
git commit -m "feat(frames): ffprobe_dims (width,height) probe with injectable seam"
```

---

### Task 2: `downscale_video_bytes` helper

**Goal:** A tested ffmpeg lanczos downscale over stdin→stdout, aspect preserved, even width.

**Files:**
- Create: `src/kinoforge/pipeline/downscale.py`
- Test: `tests/pipeline/test_downscale.py`

**Acceptance Criteria:**
- [ ] Builds ffmpeg argv containing `scale=-2:{target}:flags=lanczos`.
- [ ] Odd or non-positive `target_h` raises `ValueError`.
- [ ] Real-ffmpeg fixture: output height == target, width even.

**Verify:** `pixi run test tests/pipeline/test_downscale.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_downscale.py
"""Tests for downscale_video_bytes."""
import shutil
import subprocess

import pytest

from kinoforge.core.frames import _default_run, ffprobe_dims
from kinoforge.pipeline.downscale import downscale_video_bytes


def test_argv_uses_lanczos_and_even_width():
    # Behaviour: the ffmpeg filter is scale=-2:{h}:flags=lanczos. Bug caught:
    # dropping -2 (odd width -> h264 reject) or using a blurrier default filter.
    seen = {}

    def fake_run(argv, stdin):
        seen["argv"] = argv
        seen["stdin"] = stdin
        return b"OUT"

    out = downscale_video_bytes(b"IN", 1080, run=fake_run)
    assert out == b"OUT"
    assert "scale=-2:1080:flags=lanczos" in seen["argv"]
    assert seen["stdin"] == b"IN"


@pytest.mark.parametrize("bad", [0, -2, 1081])
def test_rejects_non_positive_or_odd_height(bad):
    # Behaviour: target must be a positive even int (h264). Bug caught: passing
    # an odd height straight to ffmpeg and getting a cryptic encoder failure.
    with pytest.raises(ValueError, match="positive even"):
        downscale_video_bytes(b"IN", bad, run=lambda a, s: b"")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_downscales_to_target(tmp_path):
    # Behaviour: a real 256x256 clip downscaled to 128 -> (128,128), width even.
    # Bug caught: aspect distortion or wrong output height end-to-end.
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "testsrc=size=256x256:duration=1:rate=8",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    out_bytes = downscale_video_bytes(src.read_bytes(), 128, run=_default_run)
    out = tmp_path / "out.mp4"
    out.write_bytes(out_bytes)
    w, h = ffprobe_dims(out)
    assert h == 128
    assert w % 2 == 0
```

- [ ] **Step 2: Run — confirm fail**

Run: `pixi run test tests/pipeline/test_downscale.py -v`
Expected: FAIL — `ModuleNotFoundError: kinoforge.pipeline.downscale`.

- [ ] **Step 3: Create the helper** — `src/kinoforge/pipeline/downscale.py`:

```python
"""Smooth video downscale to a target vertical resolution.

Used at the orchestrator materialize boundary to shrink an overshooting upscale
(e.g. 1920p -> 1080p) after a height-target upscale. Engine-agnostic: operates on
encoded video bytes, aspect preserved, width kept even for h264. The injectable
``run`` seam is shared with core.frames so tests never spawn ffmpeg.
"""

from __future__ import annotations

from collections.abc import Callable

from kinoforge.core.frames import _default_run


def _downscale_argv(target_h: int) -> list[str]:
    """Build the ffmpeg argv lanczos-downscaling stdin video to ``target_h``."""
    return [
        "ffmpeg",
        "-i",
        "pipe:0",
        "-vf",
        f"scale=-2:{target_h}:flags=lanczos",
        "-c:a",
        "copy",
        "-f",
        "mp4",
        "-movflags",
        "frag_keyframe+empty_moov",
        "pipe:1",
    ]


def downscale_video_bytes(
    video_bytes: bytes,
    target_h: int,
    *,
    run: Callable[[list[str], bytes], bytes] = _default_run,
) -> bytes:
    """Lanczos-downscale *video_bytes* so its height becomes *target_h*.

    Width is auto-computed to preserve aspect ratio and kept even (``-2``) so the
    result is h264-safe. Audio is stream-copied.

    Args:
        video_bytes: Encoded input video bytes (the overshooting upscale).
        target_h: Desired output vertical resolution; positive even integer.
        run: Injectable subprocess seam ``(argv, stdin) -> stdout`` shared with
            :mod:`kinoforge.core.frames`.

    Returns:
        Encoded MP4 bytes at the requested vertical resolution.

    Raises:
        ValueError: ``target_h`` is not a positive even integer.
        FrameExtractionError: The default seam hits a missing ffmpeg or non-zero
            exit.
    """
    if target_h <= 0 or target_h % 2 != 0:
        raise ValueError(f"target_h must be a positive even int, got {target_h}")
    return run(_downscale_argv(target_h), video_bytes)
```

- [ ] **Step 4: Run — confirm pass**

Run: `pixi run test tests/pipeline/test_downscale.py -v` → PASS (or the real-ffmpeg case skips if ffmpeg absent — it is present in this env).

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/pipeline/downscale.py tests/pipeline/test_downscale.py
git commit -m "feat(upscale): downscale_video_bytes lanczos helper"
```

---

### Task 3: Relax `UpscaleConfig` to accept height targets

**Goal:** Height-target `scale` (`1080p`) passes cfg validation for flashvsr; a non-4× *factor* is still refused.

**Files:**
- Modify: `src/kinoforge/core/config.py` (`_validate_flashvsr_wiring`, ~lines 673-693; docstring ~658-661)
- Test: `tests/core/test_upscale_config_height.py`

**Acceptance Criteria:**
- [ ] `UpscaleConfig(engine="flashvsr", scale="1080p", flashvsr=<block>)` validates without raising.
- [ ] `scale="720p"` validates; `scale="4x"` validates.
- [ ] `scale="3x"` still raises `ConfigError` (non-4× factor).

**Verify:** `pixi run test tests/core/test_upscale_config_height.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_upscale_config_height.py
"""UpscaleConfig now accepts a height-target scale for flashvsr."""
import pytest

from kinoforge.core.config import FlashVSREngineConfig, UpscaleConfig
from kinoforge.core.errors import ConfigError


def _cfg(scale: str) -> UpscaleConfig:
    return UpscaleConfig(
        engine="flashvsr", scale=scale, flashvsr=FlashVSREngineConfig()
    )


@pytest.mark.parametrize("scale", ["1080p", "720p", "4x"])
def test_height_and_4x_accepted(scale):
    # Behaviour: height targets + the native 4x factor pass. Bug caught: the old
    # cfg-time refusal of the height branch still firing.
    assert _cfg(scale).scale == scale


def test_non_4x_factor_still_refused():
    # Behaviour: flashvsr is 4x-native; a 3x factor is nonsense. Bug caught:
    # accidentally widening the relax to allow arbitrary factors.
    with pytest.raises(ConfigError, match="4x"):
        _cfg("3x")
```

> If `FlashVSREngineConfig()` requires fields, construct it exactly as existing tests do — grep `rg -n "FlashVSREngineConfig(" tests/` for the canonical constructor and mirror it here.

- [ ] **Step 2: Run — confirm fail**

Run: `pixi run test tests/core/test_upscale_config_height.py -v`
Expected: FAIL — `1080p`/`720p` raise `ConfigError` ("height-target ... not yet wired").

- [ ] **Step 3: Edit the validator** — in `src/kinoforge/core/config.py`, replace the height-refusal + factor check inside `_validate_flashvsr_wiring`:

Replace:
```python
        parsed = ScaleTarget.parse(self.scale)
        if parsed.kind == "height":
            raise ConfigError(
                f"engine=flashvsr: height-target scale ({self.scale!r}) "
                "not yet wired; use --scale Nx (factor form)"
            )
        if parsed.value != 4.0:
            raise ConfigError(
                f"engine=flashvsr fixed at native 4x upscale; got {self.scale!r}. "
                "Use engine=spandrel for other factors."
            )
        return self
```
With:
```python
        parsed = ScaleTarget.parse(self.scale)
        # Height targets are now supported: UpscaleStage resolves them to the
        # native 4x factor plus a post-upscale downscale. Only a non-4x FACTOR
        # form is still nonsense for flashvsr's fixed native scale.
        if parsed.kind == "factor" and parsed.value != 4.0:
            raise ConfigError(
                f"engine=flashvsr fixed at native 4x upscale; got {self.scale!r}. "
                "Use engine=spandrel for other factors."
            )
        return self
```

- [ ] **Step 4: Update the `scale` docstring** — in `UpscaleConfig`, replace the `scale:` attribute lines (~658-661):

Replace:
```python
        scale: ScaleTarget grammar string (``"2x"`` | ``"4x"`` | ``"1080p"`` ...).
            Consumers call ``ScaleTarget.parse(scale)``; the height branch
            raises ``NotYetImplementedError`` in v1. For ``engine=flashvsr``,
            height-target is refused at cfg-time.
```
With:
```python
        scale: ScaleTarget grammar string (``"2x"`` | ``"4x"`` | ``"1080p"`` ...).
            Factor targets multiply the source; height targets (``"1080p"``,
            ``"720p"``) resolve to a factor + smooth downscale in UpscaleStage.
            For ``engine=flashvsr`` a non-4x *factor* form is still refused
            (native scale is fixed at 4x).
```

- [ ] **Step 5: Run — confirm pass**

Run: `pixi run test tests/core/test_upscale_config_height.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/config.py tests/core/test_upscale_config_height.py
git commit -m "feat(config): accept height-target scale for flashvsr (4x factor still pinned)"
```

---

### Task 4: `UpscaleStage` height-awareness

**Goal:** `UpscaleStage` resolves a height target to a factor, runs the engine (or skips for downscale-only), and stashes `downscale_to` on the upscaled artifact's `.meta`.

**Files:**
- Modify: `src/kinoforge/pipeline/upscale.py` (full `run` rewrite + new helpers + a `probe_dims` seam field)
- Test: `tests/pipeline/test_upscale_stage_height.py`

**Acceptance Criteria:**
- [ ] Factor-target path is unchanged (no meta, engine called with the given scale).
- [ ] Single-factor height, overshoot: engine called with the sole factor; `meta["downscale_to"]` == requested.
- [ ] Single-factor height, exact output: no `downscale_to` in meta.
- [ ] Single-factor height, output below target: raises `ScaleUnsatisfiableError`.
- [ ] Downscale-only (local source ≥ target): engine NOT called; `meta["downscale_to"]` == requested.

**Verify:** `pixi run test tests/pipeline/test_upscale_stage_height.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_upscale_stage_height.py
"""UpscaleStage height-target behaviour."""
import dataclasses

import pytest

from kinoforge.core.errors import ScaleUnsatisfiableError
from kinoforge.core.interfaces import (
    Artifact,
    GenerationRequest,
    PipelineState,
    UpscaleResult,
)
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.pipeline.upscale import UpscaleStage


class FakeEngine:
    """Records the scale it was asked to run and returns a canned result."""

    def __init__(self, factors, out_res):
        self.supported_scales = tuple(
            ScaleTarget(kind="factor", value=f) for f in factors
        )
        self._out_res = out_res
        self.calls = []

    def upscale(self, instance, job, cfg, *, cancel_token=None):
        self.calls.append(job.scale)
        return UpscaleResult(
            artifact=Artifact(uri="https://pod-8000.proxy.runpod.net/artifacts/x"),
            input_resolution=(480, 480),
            output_resolution=self._out_res,
            elapsed_s=1.0,
        )


def _state(uri="https://pod/clip.mp4"):
    return PipelineState(
        request=GenerationRequest(prompt="", mode="upscale"),
        artifacts={"clip": Artifact(uri=uri)},
    )


def _stage(engine, scale, *, probe_dims=None):
    return UpscaleStage(
        engine=engine,
        scale=scale,
        instance=None,
        cfg={},
        probe_dims=probe_dims or (lambda p: (480, 480)),
    )


def test_factor_target_unchanged():
    # Behaviour: a plain 4x factor still runs the engine with that scale and sets
    # no downscale meta. Bug caught: height logic leaking into the factor path.
    eng = FakeEngine((4.0,), (1920, 1920))
    out = _stage(eng, ScaleTarget(kind="factor", value=4.0)).run(_state())
    assert eng.calls == [ScaleTarget(kind="factor", value=4.0)]
    assert "downscale_to" not in out.artifacts["upscaled"].meta


def test_single_factor_overshoot_stashes_downscale():
    # Behaviour: 1080p on a 4x engine -> run 4x, output 1920 > 1080 -> stash 1080.
    eng = FakeEngine((4.0,), (1920, 1920))
    out = _stage(eng, ScaleTarget(kind="height", value=1080)).run(_state())
    assert eng.calls == [ScaleTarget(kind="factor", value=4.0)]
    assert out.artifacts["upscaled"].meta["downscale_to"] == 1080


def test_single_factor_exact_output_no_downscale():
    # Behaviour: output height already == target -> no downscale meta.
    eng = FakeEngine((4.0,), (1920, 1080))
    out = _stage(eng, ScaleTarget(kind="height", value=1080)).run(_state())
    assert "downscale_to" not in out.artifacts["upscaled"].meta


def test_single_factor_undershoot_raises():
    # Behaviour: 4x output still below target -> ScaleUnsatisfiableError.
    eng = FakeEngine((4.0,), (960, 960))
    with pytest.raises(ScaleUnsatisfiableError):
        _stage(eng, ScaleTarget(kind="height", value=1080)).run(_state())


def test_downscale_only_skips_engine():
    # Behaviour: local source 1080p, want 720p -> engine untouched, stash 720.
    eng = FakeEngine((2.0, 4.0), (0, 0))
    stage = _stage(
        eng,
        ScaleTarget(kind="height", value=720),
        probe_dims=lambda p: (1920, 1080),
    )
    out = stage.run(_state(uri="file:///tmp/clip.mp4"))
    assert eng.calls == []
    assert out.artifacts["upscaled"].meta["downscale_to"] == 720
```

- [ ] **Step 2: Run — confirm fail**

Run: `pixi run test tests/pipeline/test_upscale_stage_height.py -v`
Expected: FAIL — `UpscaleStage.__init__` has no `probe_dims`; height raises `NotYetImplementedError`.

- [ ] **Step 3: Rewrite `src/kinoforge/pipeline/upscale.py`**:

```python
"""UpscaleStage — PipelineState in, PipelineState out.

Reads ``state.artifacts["clip"]``, invokes the configured ``UpscalerEngine``,
writes ``state.artifacts["upscaled"]``. A height target (``ScaleTarget(kind=
"height")``) is resolved here to a concrete factor plus an optional
``downscale_to`` stashed on the upscaled artifact's ``.meta`` for the orchestrator
materialize boundary to apply. Engines only ever receive ``kind="factor"``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import ScaleUnsatisfiableError
from kinoforge.core.frames import ffprobe_dims
from kinoforge.core.interfaces import (
    Artifact,
    Instance,
    PipelineState,
    UpscaleJob,
    UpscalerEngine,
    UpscaleResult,
)
from kinoforge.core.scale_resolver import resolve_height_target
from kinoforge.core.scale_target import ScaleTarget


@dataclass
class UpscaleStage:
    """A Stage that upscales the rendered clip in-place.

    Attributes:
        engine: Configured UpscalerEngine (already provisioned).
        scale: Parsed ScaleTarget. ``kind="height"`` is resolved here.
        instance: Compute instance passed to the engine; None for local engines.
        cfg: Runtime config dict the engine interprets.
        cancel_token: Threaded through to ``engine.upscale``.
        probe_dims: Injectable ``(path) -> (w, h)`` seam (tests override).
    """

    engine: UpscalerEngine
    scale: ScaleTarget
    instance: Instance | None
    cfg: dict[str, Any]
    cancel_token: CancelToken | None = None
    probe_dims: Callable[[str | Path], tuple[int, int]] = ffprobe_dims

    def run(self, state: PipelineState) -> PipelineState:
        """Run the upscale, returning a new state with ``upscaled`` populated."""
        clip = state.artifacts["clip"]
        if self.scale.kind == "factor":
            upscaled = self._run_engine(clip, self.scale).artifact
        else:
            upscaled = self._run_height(clip)
        new_artifacts = dict(state.artifacts)
        new_artifacts["upscaled"] = upscaled
        return replace(state, artifacts=new_artifacts)

    def _run_engine(self, clip: Artifact, scale: ScaleTarget) -> UpscaleResult:
        """Invoke the engine at a concrete factor scale."""
        job = UpscaleJob(source=clip, scale=scale)
        return self.engine.upscale(
            self.instance, job, self.cfg, cancel_token=self.cancel_token
        )

    def _run_height(self, clip: Artifact) -> Artifact:
        """Resolve a height target to a factor + optional downscale meta."""
        requested_h = int(self.scale.value)
        factors = tuple(
            s.value for s in self.engine.supported_scales if s.kind == "factor"
        )
        source_h = self._source_h(clip)

        if source_h is not None:
            plan = resolve_height_target(source_h, factors, requested_h)
            if plan.upscale_factor is None:
                return self._stash(clip, plan.downscale_to)
            result = self._run_engine(
                clip, ScaleTarget(kind="factor", value=plan.upscale_factor)
            )
            return self._stash(result.artifact, plan.downscale_to)

        # Remote source: dims unknown pre-run. Single-factor engines run their
        # sole factor and decide from the reported output_resolution; multi-factor
        # engines cannot pick a factor blind.
        if len(factors) != 1:
            raise ScaleUnsatisfiableError(
                source_h=-1,
                largest_factor=max(factors) if factors else 0.0,
                reached_h=-1,
                requested_h=requested_h,
            )
        result = self._run_engine(clip, ScaleTarget(kind="factor", value=factors[0]))
        output_h = int(result.output_resolution[1])
        if output_h < requested_h:
            raise ScaleUnsatisfiableError(
                source_h=int(result.input_resolution[1]),
                largest_factor=factors[0],
                reached_h=output_h,
                requested_h=requested_h,
            )
        downscale_to = None if output_h == requested_h else requested_h
        return self._stash(result.artifact, downscale_to)

    def _source_h(self, clip: Artifact) -> int | None:
        """Vertical resolution of a locally-readable source, else None."""
        uri = clip.uri
        if uri.startswith("file://"):
            return self.probe_dims(uri.removeprefix("file://"))[1]
        if uri.startswith("/"):
            return self.probe_dims(uri)[1]
        return None

    def _stash(self, artifact: Artifact, downscale_to: int | None) -> Artifact:
        """Attach ``downscale_to`` to the artifact meta (omit when None)."""
        if downscale_to is None:
            return artifact
        return replace(artifact, meta={**artifact.meta, "downscale_to": downscale_to})
```

- [ ] **Step 4: Run — confirm pass**

Run: `pixi run test tests/pipeline/test_upscale_stage_height.py -v` → PASS.
Also run the existing upscale-stage suite so the factor path didn't regress:
`pixi run test tests/pipeline/ -k upscale -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/pipeline/upscale.py tests/pipeline/test_upscale_stage_height.py
git commit -m "feat(upscale): UpscaleStage resolves height targets to factor + downscale meta"
```

---

### Task 5: Materialize-boundary downscale wiring

**Goal:** The orchestrator applies the stashed `downscale_to` to the (now-local) upscaled bytes before publishing, via a tested `finalize_upscaled_bytes` seam.

**Files:**
- Create: `src/kinoforge/pipeline/materialize.py`
- Modify: `src/kinoforge/core/orchestrator.py` (materialize block ~1926-1958)
- Test: `tests/pipeline/test_materialize.py`

**Acceptance Criteria:**
- [ ] `finalize_upscaled_bytes` downscales iff `downscale_to` is not None.
- [ ] Orchestrator materialize block calls it after obtaining local bytes, before `sink.publish`.
- [ ] Block widens to read `file://` bytes when a downscale is pending (downscale-only / local-engine path).

**Verify:** `pixi run test tests/pipeline/test_materialize.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/pipeline/test_materialize.py
"""Tests for finalize_upscaled_bytes."""
from kinoforge.pipeline.materialize import finalize_upscaled_bytes


def test_downscales_when_target_set():
    # Behaviour: a downscale_to triggers the downscale seam with those args.
    seen = {}

    def fake_downscale(body, target_h):
        seen["args"] = (body, target_h)
        return b"SMALL"

    out = finalize_upscaled_bytes(b"BIG", 1080, downscale=fake_downscale)
    assert out == b"SMALL"
    assert seen["args"] == (b"BIG", 1080)


def test_passthrough_when_no_target():
    # Behaviour: no downscale_to -> bytes returned untouched, seam not called.
    called = False

    def fake_downscale(body, target_h):
        nonlocal called
        called = True
        return b"X"

    assert finalize_upscaled_bytes(b"BIG", None, downscale=fake_downscale) == b"BIG"
    assert called is False
```

- [ ] **Step 2: Run — confirm fail**

Run: `pixi run test tests/pipeline/test_materialize.py -v`
Expected: FAIL — `ModuleNotFoundError: kinoforge.pipeline.materialize`.

- [ ] **Step 3: Create `src/kinoforge/pipeline/materialize.py`**:

```python
"""Finalize upscaled bytes at the orchestrator materialize boundary.

The upscaled artifact's bytes are only local once the orchestrator fetches them
after the stage walk. If UpscaleStage stashed a ``downscale_to`` (height-target
overshoot), shrink here before the sink publishes the deliverable.
"""

from __future__ import annotations

from collections.abc import Callable

from kinoforge.pipeline.downscale import downscale_video_bytes


def finalize_upscaled_bytes(
    body: bytes,
    downscale_to: int | None,
    *,
    downscale: Callable[[bytes, int], bytes] = downscale_video_bytes,
) -> bytes:
    """Return *body*, lanczos-downscaled to ``downscale_to`` when set.

    Args:
        body: Local upscaled video bytes.
        downscale_to: Target vertical resolution, or ``None`` for passthrough.
        downscale: Injectable downscale seam ``(body, target_h) -> bytes``.

    Returns:
        The (possibly downscaled) video bytes.
    """
    if downscale_to is not None:
        return downscale(body, downscale_to)
    return body
```

- [ ] **Step 4: Run — confirm pass**

Run: `pixi run test tests/pipeline/test_materialize.py -v` → PASS.

- [ ] **Step 5: Wire into the orchestrator** — in `src/kinoforge/core/orchestrator.py`, edit the materialize block (~1926-1958). Replace:

```python
        upscaled = state.artifacts.get("upscaled")
        if (
            upscaled is not None
            and sink is not None
            and upscaled.uri.startswith(("http://", "https://"))
        ):
            import urllib.request as _urequest  # local — orchestrator stays urllib-free

            _log.info("materializing upscaled artifact from %s", upscaled.uri)
            req = _urequest.Request(  # noqa: S310 — pod proxy URL only
                upscaled.uri,
                headers={"User-Agent": "kinoforge-orchestrator/0.1"},
            )
            with _urequest.urlopen(req, timeout=600) as resp:  # noqa: S310
                body: bytes = resp.read()
```
With:
```python
        upscaled = state.artifacts.get("upscaled")
        _downscale_to = upscaled.meta.get("downscale_to") if upscaled else None
        _needs_materialize = upscaled is not None and sink is not None and (
            upscaled.uri.startswith(("http://", "https://"))
            or (_downscale_to is not None and upscaled.uri.startswith("file://"))
        )
        if _needs_materialize and upscaled is not None and sink is not None:
            from kinoforge.pipeline.materialize import finalize_upscaled_bytes

            if upscaled.uri.startswith(("http://", "https://")):
                import urllib.request as _urequest  # orchestrator stays urllib-free

                _log.info("materializing upscaled artifact from %s", upscaled.uri)
                req = _urequest.Request(  # noqa: S310 — pod proxy URL only
                    upscaled.uri,
                    headers={"User-Agent": "kinoforge-orchestrator/0.1"},
                )
                with _urequest.urlopen(req, timeout=600) as resp:  # noqa: S310
                    body: bytes = resp.read()
            else:
                body = Path(upscaled.uri.removeprefix("file://")).read_bytes()

            if _downscale_to is not None:
                _log.info("downscaling upscaled artifact to %dp", _downscale_to)
                body = finalize_upscaled_bytes(body, _downscale_to)
```

Then the existing `provider_tag = ...` / `sink.publish(...)` / `state.artifacts["upscaled"] = dataclasses.replace(upscaled, uri=...)` lines stay as-is, now inside the widened `if`. Ensure `from pathlib import Path` is imported at module top (grep `rg -n "^from pathlib import Path" src/kinoforge/core/orchestrator.py`; add if absent).

> Preserve the exact indentation of the trailing publish/replace lines when nesting them under the new `if`. Read the full 1926-1959 block first and re-indent as one unit.

- [ ] **Step 6: Verify orchestrator import + existing upscale suite**

Run: `pixi run test tests/ -k "upscale or orchestrat" -v` → PASS (no regression in existing multi-stage tests).
Run: `pixi run typecheck` → clean.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/pipeline/materialize.py tests/pipeline/test_materialize.py src/kinoforge/core/orchestrator.py
git commit -m "feat(upscale): downscale at materialize boundary for height targets"
```

---

### Task 6: Live FlashVSR height-target smoke + frame-QA

**Goal:** Prove end-to-end that `engine=flashvsr scale=1080p` on a 480² source delivers a coherent 1080p video (4×→1920→lanczos 1080), visually QA'd.

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `examples/configs/upscale-flashvsr-1080p.yaml`
- Create: `tests/live/test_flashvsr_height_target_live.py` (env-gated; commit RED before spend)
- Modify (post-run): `successful-generations.md` (new capability axis: height-target upscale YAML shape)

**Acceptance Criteria:**
- [ ] Scaffold (cfg + env-gated live test) committed RED **before** any spend (CLAUDE.md live-spend rule).
- [ ] `pixi run preflight` exit 0 before spend.
- [ ] One `--no-reuse` run completes; delivered mp4 vertical resolution == 1080 (ffprobe), aspect preserved.
- [ ] 5-frame contact sheet extracted + eyeballed: sharp, color-correct, temporally coherent, faithful to the 1920² sibling (per CLAUDE.md visual-QA rule). Verdict recorded.
- [ ] `kinoforge list` after run shows no running instances AND empty ledger; pod destroyed.
- [ ] `successful-generations.md` entry added with the verdict.

**Verify:** `ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 <out>.mp4` → `1080`; contact-sheet QA verdict = high quality.

**Steps:**

- [ ] **Step 1: Write the cfg** — `examples/configs/upscale-flashvsr-1080p.yaml`, copied from the existing `upscale-flashvsr-x4.yaml` (grep `fd upscale-flashvsr examples/configs`) with `upscale.scale: "1080p"` instead of `"4x"`. Keep all compute/engine blocks identical (incl. `compute.cloud_type: secure`).

- [ ] **Step 2: Write the env-gated live smoke** — `tests/live/test_flashvsr_height_target_live.py`:

```python
"""Live: FlashVSR height-target (1080p) on a 480x480 source. Env-gated."""
import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE") != "1",
    reason="live smoke — set KINOFORGE_LIVE=1 to run (spends money)",
)


def test_flashvsr_1080p_delivers_1080_height(tmp_path):
    # Behaviour: engine=flashvsr scale=1080p on 480p source -> 1080p deliverable.
    # Bug caught: height target not resolving to 4x+downscale end-to-end.
    prompt = open("/workspace/examples/configs/prompts/field-realistic.txt").read()
    # ... invoke `kinoforge generate --config examples/configs/upscale-flashvsr-1080p.yaml
    #     --mode upscale --prompt <prompt> --no-reuse`, capture the output path,
    #     then ffprobe its height and assert == 1080. Mirror the existing FlashVSR
    #     live smoke (grep tests/live for the entry-#13/#14 FlashVSR test) for the
    #     exact CLI invocation + artifact-path capture pattern.
    raise AssertionError("scaffold — fill CLI invocation from existing FlashVSR live test")
```

- [ ] **Step 3: Commit the RED scaffold BEFORE any spend**

```bash
git add examples/configs/upscale-flashvsr-1080p.yaml tests/live/test_flashvsr_height_target_live.py
git commit -m "test(live): RED scaffold for FlashVSR 1080p height-target smoke"
```

- [ ] **Step 4: Flesh out the CLI invocation** in the live test using the existing FlashVSR live smoke as the template (artifact-path capture, `--no-reuse`). Commit.

- [ ] **Step 5: Preflight, then run with polling**

```bash
pixi run preflight
KINOFORGE_LIVE=1 pixi run test tests/live/test_flashvsr_height_target_live.py -v -s
```
Poll pod GPU/CPU/mem + costPerHr every 60-90s during the run (CLAUDE.md live-smoke monitoring). Kill fast if GPU 0% for 3 consecutive probes.

- [ ] **Step 6: Frame-QA the output** — extract 5 frames via `kinoforge.core.frames.ffmpeg_frames_by_count`, read the contact sheet, judge artifacts/coherence/prompt-adherence/fidelity vs the 1920² sibling. Record the verdict.

- [ ] **Step 7: Verify teardown**

```bash
kinoforge list
```
Expected: `No running instances.` AND `No instances recorded in ledger.` If a pod survives: `kinoforge destroy --id <pod-id>`.

- [ ] **Step 8: Log + commit** — add the `successful-generations.md` entry (new YAML-shape capability axis) with dims, generation id, spend, and QA verdict; update `PROGRESS.md`. Commit.

```bash
git add successful-generations.md PROGRESS.md tests/live/test_flashvsr_height_target_live.py
git commit -m "docs: FlashVSR 1080p height-target smoke green + frame-QA verdict"
```

---

## Self-Review

- **Spec coverage:** §5.1 resolver → T0; §5.2 ffprobe_dims → T1; §5.4 downscale helper → T2; §5.6 config relax → T3; §5.3 UpscaleStage → T4; §5.5 materialize wiring → T5; §7 live+QA → T6. Errors (§5.7) → T0. All covered.
- **Placeholder scan:** the only deliberately-deferred detail is the live-test CLI invocation (T6 Step 2/4), which points at the existing FlashVSR live smoke as the concrete template — filled before spend, not a silent gap.
- **Type consistency:** `HeightPlan(upscale_factor, downscale_to)`, `resolve_height_target`, `ffprobe_dims`, `downscale_video_bytes(body, target_h)`, `finalize_upscaled_bytes(body, downscale_to, *, downscale)`, `UpscaleStage(..., probe_dims=)`, `.meta["downscale_to"]` — names consistent across T0-T5.

## Notes for the executor

- `pixi run test` / `lint` / `typecheck` / `pre-commit` per CLAUDE.md. Stage `pixi.lock` with any dep change (none expected here).
- Engine/runtime height guards are intentionally KEPT (defense-in-depth invariants) — do not delete them.
- T6 is the only money-spending task; its RED scaffold MUST be committed before the run.
