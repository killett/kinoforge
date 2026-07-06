# Frame Interpolation Stage (RIFE v4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a RIFE-v4 frame-interpolation capability that raises a video's frame rate to a floating-point `--fps` target, on a freshly-generated clip or a standalone uploaded `--video`, mirroring the upscale stage.

**Architecture:** An engine-agnostic pure `fps_resolver` (capability = arbitrary-timestep vs recursive-2×) drives an `InterpolatorEngine` interface; `RifeEngine` is the first (only) implementation, running pod-side on its own RunPod instance via the embedded server's new `/interpolate` endpoints. A local ffmpeg decimate path handles `target_fps ≤ source_fps` with no GPU. Delivery mirrors `kinoforge upscale`: a standalone `kinoforge interpolate --video` subcommand reusing `generate(skip_clip_stage=True, initial_clip=…)`.

**Tech Stack:** Python 3.11, pydantic v2 (config), argparse (CLI), ffmpeg/ffprobe (frames), Practical-RIFE (pod inference), RunPod (compute), pytest (TDD), pixi (tasks).

**User decisions (already made):**
- Engine: RIFE v4 (arbitrary-timestep) shipped alone through an engine-agnostic interface + resolver; FILM/GMFSS/GIMM are future plug-ins.
- Ordering when both interp + upscale wanted: **upscale → interpolate**.
- `target_fps ≤ source_fps`: decimate via ffmpeg, skip GPU.
- Compute: independent stage/pod for v1 (own engine + own server, mirrors FlashVSR).

---

## ⚠ Planning-time correction (read before Task 7)

The spec (§5.5) assumed a **single-pass** `kinoforge generate` that walks
render → upscale → interpolate and materializes between compute stages. Reading
the orchestrator (`core/orchestrator.py:1820-1974`) shows `generate` deploys **one**
compute instance per session (`session.instance`). Render (Wan), upscale (FlashVSR)
and interp (RIFE) are three different pod images; running interp on its **own** pod
inside one `generate` pass would need multi-instance orchestration — which is
exactly the **co-resident case the spec put out of scope**, and contradicts the
"independent pod for v1" decision.

**Correction (honors every locked decision):** v1 ships interpolation as a
**standalone `kinoforge interpolate --video` command** — its own RIFE session/pod,
structurally identical to `kinoforge upscale --video`
(`cli/_commands.py:646-782`). The **upscale → interpolate ordering** is realized by
**chaining commands**: `kinoforge upscale …` produces a file, then
`kinoforge interpolate --video <that file> --fps 60`. Each runs on its own pod =
"independent pod" honored, ordering preserved. This **drops the risky orchestrator
inter-stage materialize refactor** entirely — interp's input is always a
local/`http` `--video` artifact, materialized by the existing
`_resolve_input_video_as_artifact`.

Single-pass generate+interp and co-resident interp+upscale stay **out of scope**
(unchanged from spec §8). Task 7 therefore adds only the minimal orchestrator
wiring the standalone path needs (append `InterpolateStage`; materialize the
`interpolated` artifact post-walk) — NOT a general inter-stage boundary.

---

## File Structure

**New files:**
- `src/kinoforge/core/fps_resolver.py` — pure `resolve_fps_target` + `FpsPlan` + `InterpCapability`. No I/O.
- `src/kinoforge/pipeline/decimate.py` — `decimate_video_fps` (ffmpeg `fps` filter, temp-file input).
- `src/kinoforge/pipeline/interpolate.py` — `InterpolateStage`.
- `src/kinoforge/interpolators/__init__.py` — package marker.
- `src/kinoforge/interpolators/rife/__init__.py` — self-registers `RifeEngine`.
- `src/kinoforge/interpolators/rife/_engine.py` — client `InterpolatorEngine` impl (HTTP to pod).
- `src/kinoforge/interpolators/rife/_runtime.py` — on-pod RIFE inference wrapper.
- `examples/configs/interpolate-rife-60fps.yaml` — interpolate-only cfg for the live smoke.
- Tests: `tests/test_fps_resolver.py`, `tests/test_frames_ffprobe_fps.py`, `tests/pipeline/test_decimate.py`, `tests/pipeline/test_interpolate_stage.py`, `tests/interpolators/test_rife_engine.py`, `tests/test_config_interpolate.py`, `tests/core/test_orchestrator_interpolate.py`, `tests/cli/test_cmd_interpolate.py`, `tests/live/test_rife_interpolate_live.py`.

**Modified files:**
- `src/kinoforge/core/errors.py` — add `InterpolationError`.
- `src/kinoforge/core/frames.py` — add `ffprobe_fps`.
- `src/kinoforge/core/interfaces.py` — add `InterpolateJob`, `InterpolateResult`, `InterpolatorEngine`.
- `src/kinoforge/core/registry.py` — add `register_interpolator` / `get_interpolator` / `interpolator_names`.
- `src/kinoforge/core/config.py` — add `RifeEngineConfig`, `InterpolateConfig`, `Config.interpolate`, capability-key wiring.
- `src/kinoforge/core/orchestrator.py` — append `InterpolateStage`; materialize `interpolated`.
- `src/kinoforge/cli/_main.py` — `interpolate` subparser.
- `src/kinoforge/cli/_commands.py` — `_cmd_interpolate` + dispatch.
- `src/kinoforge/engines/diffusers/servers/ln.py` (a.k.a. `wan_t2v_server.py`) — `/interpolate` + `/interpolate/status/{id}` endpoints.

---

### Task 0: `InterpolationError` exception

**Goal:** Add the interp error type so engine/pod failures have a typed home, mirroring `UpscaleFailed`.

**Files:**
- Modify: `src/kinoforge/core/errors.py` (after `UpscaleFailed`, ~line 454)
- Test: `tests/test_errors_interpolation.py`

**Acceptance Criteria:**
- [ ] `InterpolationError` subclasses `KinoforgeError`, carries `job_id` + `server_error`.
- [ ] Message names the failed job id and server error.

**Verify:** `pixi run pytest tests/test_errors_interpolation.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/test_errors_interpolation.py
"""InterpolationError shape — typed home for a failed pod interp job."""
from kinoforge.core.errors import InterpolationError, KinoforgeError


def test_interpolation_error_is_kinoforge_error_with_fields():
    # Bug caught: a bare Exception would lose job_id/server_error and break
    # `except KinoforgeError` handlers in the CLI.
    err = InterpolationError(job_id="rife-abc", server_error="cuda oom")
    assert isinstance(err, KinoforgeError)
    assert err.job_id == "rife-abc"
    assert err.server_error == "cuda oom"
    assert "rife-abc" in str(err)
    assert "cuda oom" in str(err)
```

- [ ] **Step 2: Run — expect FAIL** `pixi run pytest tests/test_errors_interpolation.py -v` → ImportError: cannot import name 'InterpolationError'

- [ ] **Step 3: Implement** (in `src/kinoforge/core/errors.py`, after `UpscaleFailed`):

```python
class InterpolationError(KinoforgeError):
    """Server-side frame-interpolation job entered an error state."""

    def __init__(self, job_id: str, server_error: str) -> None:
        """Record the failed job_id and server-supplied error description."""
        super().__init__(
            f"interpolate job {job_id} failed on server: {server_error}"
        )
        self.job_id = job_id
        self.server_error = server_error
```

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add tests/test_errors_interpolation.py src/kinoforge/core/errors.py
git commit -m "feat(errors): add InterpolationError for pod interp failures"
```

---

### Task 1: `ffprobe_fps` frame-rate probe

**Goal:** Probe a video's real frame rate (rational `r_frame_rate` like `16/1`, `30000/1001`) as a float, sibling to `ffprobe_dims`.

**Files:**
- Modify: `src/kinoforge/core/frames.py` (after `ffprobe_dims`, ~line 233)
- Test: `tests/test_frames_ffprobe_fps.py`

**Acceptance Criteria:**
- [ ] Parses `"16/1"` → `16.0`, `"30000/1001"` → `29.97002…`, `"30/1"` → `30.0`.
- [ ] Uses injectable `run` seam (no real ffprobe in unit tests).
- [ ] Raises `FrameExtractionError` on unparseable output and zero denominator.

**Verify:** `pixi run pytest tests/test_frames_ffprobe_fps.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_frames_ffprobe_fps.py
"""ffprobe_fps parses ffprobe's rational r_frame_rate into a float."""
import math

import pytest

from kinoforge.core.errors import FrameExtractionError
from kinoforge.core.frames import ffprobe_fps


def _seam(out: bytes):
    return lambda argv: out


def test_integer_rate():
    # Bug caught: naive float("16/1") raises; must divide the rational.
    assert ffprobe_fps("x.mp4", run=_seam(b"16/1\n")) == 16.0


def test_ntsc_rational_rate():
    # Bug caught: dropping the denominator would report 30000.0 fps.
    got = ffprobe_fps("x.mp4", run=_seam(b"30000/1001\n"))
    assert math.isclose(got, 29.97002997, rel_tol=1e-6)


def test_argv_targets_r_frame_rate():
    # Bug caught: probing avg_frame_rate returns 0/0 for VFR/streamed inputs.
    captured = {}

    def run(argv):
        captured["argv"] = argv
        return b"24/1\n"

    ffprobe_fps("clip.mp4", run=run)
    assert "r_frame_rate" in " ".join(captured["argv"])
    assert captured["argv"][-1] == "clip.mp4"


def test_unparseable_raises():
    with pytest.raises(FrameExtractionError):
        ffprobe_fps("x.mp4", run=_seam(b"N/A\n"))


def test_zero_denominator_raises():
    # Bug caught: "0/0" (no timing) must error, not ZeroDivisionError-crash.
    with pytest.raises(FrameExtractionError):
        ffprobe_fps("x.mp4", run=_seam(b"0/0\n"))
```

- [ ] **Step 2: Run — expect FAIL** (ImportError: `ffprobe_fps`)

- [ ] **Step 3: Implement** (in `src/kinoforge/core/frames.py`, after `ffprobe_dims`):

```python
def ffprobe_fps(
    video_path: str | Path,
    *,
    run: Callable[[list[str]], bytes] = _default_probe_run,
) -> float:
    """Probe the frame rate of the first video stream via ffprobe.

    Reads ``r_frame_rate`` (the base frame rate, a rational such as ``16/1``
    or ``30000/1001``) rather than ``avg_frame_rate`` — the latter is ``0/0``
    for streamed / variable-frame-rate inputs.

    Args:
        video_path: Path to the video file on disk.
        run: Injectable seam ``(argv) -> stdout`` so tests spawn no binary.

    Returns:
        Frame rate in frames per second.

    Raises:
        FrameExtractionError: ffprobe missing / non-zero exit, output that is
            not a ``num/den`` rational, or a zero denominator.
    """
    argv = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    raw = run(argv).decode(errors="replace").strip()
    try:
        num_str, den_str = raw.split("/")
        num, den = float(num_str), float(den_str)
        if den == 0.0:
            raise ValueError("zero denominator")
        return num / den
    except ValueError as exc:
        raise FrameExtractionError(
            f"unparseable ffprobe frame rate {raw!r} for {video_path}"
        ) from exc
```

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add tests/test_frames_ffprobe_fps.py src/kinoforge/core/frames.py
git commit -m "feat(frames): add ffprobe_fps rational frame-rate probe"
```

---

### Task 2: `fps_resolver` — the engine-agnostic foundation

**Goal:** Pure function mapping `(source_fps, target_fps, capability)` → an `FpsPlan` describing whether to skip the GPU (decimate), the arbitrary-timestep synthesis schedule, or the recursive-2× depth, plus any exact-fps decimation. Zero I/O.

**Files:**
- Create: `src/kinoforge/core/fps_resolver.py`
- Test: `tests/test_fps_resolver.py`

**Acceptance Criteria:**
- [ ] `target == source` → `skip_gpu=True`, no schedule, no decimate (passthrough).
- [ ] `target < source` → `skip_gpu=True`, `decimate_to=target`, no schedule.
- [ ] arbitrary-timestep, exact multiple (`16→32`, count 3) → schedule length 5, all timesteps in {0.0, 0.5}, `decimate_to=None`.
- [ ] arbitrary-timestep, non-multiple (`16→24`) → output count == `round(duration*24)`, fractional timesteps present, `decimate_to=None`.
- [ ] recursive-2×, `16→60` → `recursion_depth=2` (k=4 → 64 fps), `decimate_to=60.0`; `16→64` → depth 3? No: `next_pow2(ceil(64/16)=4)=4` → depth 2, `decimate_to=None`.
- [ ] `target_fps <= 0` → `ValueError`.

**Verify:** `pixi run pytest tests/test_fps_resolver.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fps_resolver.py
"""Exhaustive table for the pure fps resolver — zero cloud spend."""
import math

import pytest

from kinoforge.core.fps_resolver import (
    FpsPlan,
    InterpCapability,
    resolve_fps_target,
)

ARB = InterpCapability.ARBITRARY_TIMESTEP
REC = InterpCapability.RECURSIVE_2X


def test_passthrough_when_equal():
    # Bug caught: booting a GPU pod to "interpolate" 30->30 is pure waste.
    plan = resolve_fps_target(30.0, 30.0, ARB, source_frame_count=90)
    assert plan == FpsPlan(
        schedule=None, recursion_depth=None, decimate_to=None, skip_gpu=True
    )


def test_decimate_only_when_target_below_source():
    # Bug caught: 30->24 is frame *removal*, must skip GPU and ffmpeg-decimate.
    plan = resolve_fps_target(30.0, 24.0, ARB, source_frame_count=90)
    assert plan.skip_gpu is True
    assert plan.schedule is None
    assert plan.decimate_to == 24.0


def test_arbitrary_exact_double():
    # 3 source frames at 16fps -> 32fps: insert one midpoint between each pair.
    # Output frames at times j/32 for j in 0..(round(3/16*32)-1)=0..5 -> 6 frames.
    plan = resolve_fps_target(16.0, 32.0, ARB, source_frame_count=3)
    assert plan.decimate_to is None
    assert plan.recursion_depth is None
    assert len(plan.schedule) == 6
    # timesteps land on 0.0 (copy) and 0.5 (midpoint) only for an exact 2x.
    assert {round(t, 3) for _, t in plan.schedule} == {0.0, 0.5}
    # first output copies source frame 0 at t=0.0
    assert plan.schedule[0] == (0, 0.0)


def test_arbitrary_non_multiple_hits_exact_count():
    # 16 -> 24 over 2s (32 source frames): output count == round(2*24)=48.
    plan = resolve_fps_target(16.0, 24.0, ARB, source_frame_count=32)
    assert plan.decimate_to is None
    assert len(plan.schedule) == 48
    # Non-multiple => at least one fractional (non-0, non-0.5-only) timestep.
    fracs = {round(t, 4) for _, t in plan.schedule}
    assert any(f not in (0.0, 0.5) for f in fracs)
    # Every source index referenced is in range.
    assert all(0 <= i < 32 for i, _ in plan.schedule)


def test_recursive_overshoot_then_decimate():
    # 16 -> 60: recursive engine can only do powers of two. ceil(60/16)=4 ->
    # next_pow2(4)=4 -> depth 2 -> 64fps, then decimate to exact 60.
    plan = resolve_fps_target(16.0, 60.0, REC, source_frame_count=16)
    assert plan.schedule is None
    assert plan.recursion_depth == 2
    assert plan.decimate_to == 60.0


def test_recursive_exact_power_of_two_no_decimate():
    # 16 -> 64: ceil(64/16)=4 -> next_pow2=4 -> depth 2 -> exactly 64, no trim.
    plan = resolve_fps_target(16.0, 64.0, REC, source_frame_count=16)
    assert plan.recursion_depth == 2
    assert plan.decimate_to is None


def test_non_positive_target_raises():
    with pytest.raises(ValueError):
        resolve_fps_target(16.0, 0.0, ARB, source_frame_count=16)
```

- [ ] **Step 2: Run — expect FAIL** (module missing)

- [ ] **Step 3: Implement** `src/kinoforge/core/fps_resolver.py`:

```python
"""Pure fps-target resolver for the frame-interpolation pipeline.

Engine-agnostic: consumes an engine's timestep capability and decides whether
to skip the GPU (decimate a downshift), synthesize an exact arbitrary-timestep
schedule, or drive a recursive-2x engine and then decimate to the exact target.
No I/O, no torch — a pure function so the whole decision table is unit-tested
with zero cloud spend.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class InterpCapability(Enum):
    """How an interpolation engine reaches an intermediate time.

    ARBITRARY_TIMESTEP: give it t in (0,1) between two frames (RIFE v4,
    GIMM-VFI) -> hit any target fps in one pass.
    RECURSIVE_2X: only halves intervals (FILM, GMFSS-classic) -> overshoot to
    a power-of-two multiple, then decimate to the exact target.
    """

    ARBITRARY_TIMESTEP = "arbitrary_timestep"
    RECURSIVE_2X = "recursive_2x"


@dataclass(frozen=True)
class FpsPlan:
    """Resolved plan for an fps-target interpolation.

    Attributes:
        schedule: For arbitrary-timestep engines, one ``(source_index,
            timestep)`` per OUTPUT frame; ``timestep == 0.0`` copies the source
            frame, else synthesize between ``source_index`` and the next frame.
            ``None`` for recursive/decimate/passthrough plans.
        recursion_depth: For recursive-2x engines, ``log2`` of the insertion
            factor (depth 2 -> x4). ``None`` otherwise.
        decimate_to: Exact fps to ffmpeg-decimate to after synthesis (recursive
            overshoot) or instead of it (downshift). ``None`` when the synthesis
            already lands exactly on target or on passthrough.
        skip_gpu: ``True`` when no GPU pod is needed (passthrough or pure
            decimation).
    """

    schedule: tuple[tuple[int, float], ...] | None
    recursion_depth: int | None
    decimate_to: float | None
    skip_gpu: bool


def _next_pow2(n: int) -> int:
    """Smallest power of two >= ``n`` (n >= 1)."""
    return 1 << (n - 1).bit_length()


def resolve_fps_target(
    source_fps: float,
    target_fps: float,
    cap: InterpCapability,
    *,
    source_frame_count: int,
) -> FpsPlan:
    """Resolve a target frame rate against an engine's timestep capability.

    Args:
        source_fps: Source frame rate (fps).
        target_fps: Requested output frame rate (fps); must be > 0.
        cap: The engine's :class:`InterpCapability`.
        source_frame_count: Number of frames in the source clip; sets the
            arbitrary-timestep output length.

    Returns:
        An :class:`FpsPlan`.

    Raises:
        ValueError: ``target_fps`` is not positive.
    """
    if target_fps <= 0:
        raise ValueError(f"target_fps must be > 0, got {target_fps}")

    if target_fps == source_fps:
        return FpsPlan(
            schedule=None, recursion_depth=None, decimate_to=None, skip_gpu=True
        )
    if target_fps < source_fps:
        return FpsPlan(
            schedule=None,
            recursion_depth=None,
            decimate_to=target_fps,
            skip_gpu=True,
        )

    if cap is InterpCapability.RECURSIVE_2X:
        ratio = math.ceil(target_fps / source_fps)
        k = _next_pow2(ratio)
        depth = k.bit_length() - 1
        reached = source_fps * k
        decimate_to = None if reached == target_fps else target_fps
        return FpsPlan(
            schedule=None,
            recursion_depth=depth,
            decimate_to=decimate_to,
            skip_gpu=False,
        )

    # ARBITRARY_TIMESTEP: exact constant-output-rate placement.
    duration = source_frame_count / source_fps
    out_count = round(duration * target_fps)
    last_src = source_frame_count - 1
    schedule: list[tuple[int, float]] = []
    for j in range(out_count):
        pos = (j / target_fps) * source_fps  # source-frame units
        i = min(int(pos), last_src)
        f = 0.0 if i >= last_src else pos - i
        schedule.append((i, round(f, 6)))
    return FpsPlan(
        schedule=tuple(schedule),
        recursion_depth=None,
        decimate_to=None,
        skip_gpu=False,
    )
```

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/fps_resolver.py tests/test_fps_resolver.py
git commit -m "feat(core): pure fps_resolver — the engine-agnostic interp foundation"
```

---

### Task 3: `decimate_video_fps` — ffmpeg frame re-timing

**Goal:** Re-time encoded video bytes to a target fps with ffmpeg's `fps` filter, reading from a **temp file** (not stdin) to dodge the large-mp4 moov-atom pipe-seek trap. Handles the `target ≤ source` path and the recursive-overshoot trim.

**Files:**
- Create: `src/kinoforge/pipeline/decimate.py`
- Test: `tests/pipeline/test_decimate.py`

**Acceptance Criteria:**
- [ ] argv contains `-vf fps=<target>` and reads a file path (`-i <tmp>`), NOT `pipe:0`.
- [ ] Accepts fractional targets (29.97) — emitted as `fps=30000/1001` or `fps=29.97` (see impl).
- [ ] Raises `ValueError` on `target_fps <= 0`.
- [ ] Real-ffmpeg fixture: output `r_frame_rate` == target (integration test, `@pytest.mark.slow` if the suite marks ffmpeg tests).

**Verify:** `pixi run pytest tests/pipeline/test_decimate.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_decimate.py
"""decimate_video_fps: ffmpeg fps re-timing over a seekable temp file."""
import pytest

from kinoforge.pipeline.decimate import _decimate_argv, decimate_video_fps


def test_argv_uses_fps_filter_and_file_input():
    # Bug caught: reading pipe:0 fails on large mp4 (moov seek, exit 183).
    argv = _decimate_argv("/tmp/x.mp4", 24.0)
    joined = " ".join(argv)
    assert "fps=24" in joined
    assert "pipe:0" not in argv
    assert "-i" in argv and "/tmp/x.mp4" in argv


def test_ntsc_target_serialized_exactly():
    # Bug caught: str(29.97) rounding drifts; keep NTSC exact.
    argv = _decimate_argv("/tmp/x.mp4", 29.97)
    assert "fps=30000/1001" in " ".join(argv)


def test_seam_receives_argv_and_empty_stdin():
    seen = {}

    def run(argv, stdin):
        seen["argv"] = argv
        seen["stdin"] = stdin
        return b"OUT"

    out = decimate_video_fps(b"INPUT", 24.0, run=run)
    assert out == b"OUT"
    assert seen["stdin"] == b""  # bytes go via temp file, not stdin


def test_non_positive_target_raises():
    with pytest.raises(ValueError):
        decimate_video_fps(b"x", 0.0, run=lambda a, s: b"")
```

Plus an integration test guarded like the existing real-ffmpeg tests (grep the
repo for how `test_downscale` marks its real-ffmpeg case and copy that marker):

```python
def test_real_ffmpeg_hits_target_fps(tmp_path):
    # Generate a 1s 30fps clip, decimate to 15, assert probed rate.
    import subprocess

    from kinoforge.core.frames import ffprobe_fps

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=30:duration=1",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    out = decimate_video_fps(src.read_bytes(), 15.0)
    dst = tmp_path / "out.mp4"
    dst.write_bytes(out)
    assert round(ffprobe_fps(dst)) == 15
```

- [ ] **Step 2: Run — expect FAIL** (module missing)

- [ ] **Step 3: Implement** `src/kinoforge/pipeline/decimate.py`:

```python
"""Re-time encoded video to a target fps via ffmpeg's fps filter.

Used for the ``target_fps <= source_fps`` downshift (no GPU) and to trim a
recursive-2x engine's power-of-two overshoot to the exact requested rate.
Reads input from a SEEKABLE temp file, not stdin: an mp4's moov atom lives at
the container tail and ffmpeg cannot seek back to it over pipe:0 (exit 183 on
large inputs). Mirrors :mod:`kinoforge.pipeline.downscale`.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from fractions import Fraction

from kinoforge.core.frames import _default_run


def _fps_arg(target_fps: float) -> str:
    """Serialize *target_fps* for ffmpeg, keeping NTSC rationals exact."""
    frac = Fraction(target_fps).limit_denominator(1001)
    if frac.denominator == 1:
        return f"fps={frac.numerator}"
    return f"fps={frac.numerator}/{frac.denominator}"


def _decimate_argv(src_path: str, target_fps: float) -> list[str]:
    """Build the ffmpeg argv re-timing *src_path* to ``target_fps``."""
    return [
        "ffmpeg",
        "-i",
        src_path,
        "-vf",
        _fps_arg(target_fps),
        "-c:a",
        "copy",
        "-f",
        "mp4",
        "-movflags",
        "frag_keyframe+empty_moov",
        "pipe:1",
    ]


def decimate_video_fps(
    video_bytes: bytes,
    target_fps: float,
    *,
    run: Callable[[list[str], bytes], bytes] = _default_run,
) -> bytes:
    """Re-time *video_bytes* to *target_fps* using ffmpeg's fps filter.

    Args:
        video_bytes: Encoded input video bytes.
        target_fps: Desired output frame rate; must be > 0.
        run: Injectable subprocess seam ``(argv, stdin) -> stdout`` shared with
            :mod:`kinoforge.core.frames`.

    Returns:
        Encoded MP4 bytes at the requested frame rate.

    Raises:
        ValueError: ``target_fps`` is not positive.
        FrameExtractionError: The default seam hits a missing ffmpeg / non-zero
            exit.
    """
    if target_fps <= 0:
        raise ValueError(f"target_fps must be > 0, got {target_fps}")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        tf.write(video_bytes)
        src_path = tf.name
    try:
        return run(_decimate_argv(src_path, target_fps), b"")
    finally:
        os.unlink(src_path)
```

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/pipeline/decimate.py tests/pipeline/test_decimate.py
git commit -m "feat(pipeline): decimate_video_fps — ffmpeg fps re-timing (temp-file input)"
```

---

### Task 4: `InterpolatorEngine` interface + registry

**Goal:** Add the job/result dataclasses, the `InterpolatorEngine` ABC (parallel to `UpscalerEngine`), and registry functions, so a stage can drive any interp engine by capability.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (after `UpscaleResult`, ~line 640; ABC after `UpscalerEngine`, ~line 963)
- Modify: `src/kinoforge/core/registry.py` (mirror `register_upscaler`, ~line 235; add `_interpolators` dict ~line 37)
- Test: `tests/core/test_interpolator_registry.py`

**Acceptance Criteria:**
- [ ] `InterpolateJob(source, target_fps, params)` and `InterpolateResult(artifact, input_fps, output_fps, input_frame_count, output_frame_count, elapsed_s, engine_meta)` are frozen dataclasses.
- [ ] `InterpolatorEngine` declares `name`, `requires_compute`, `requires_local_weights`, `capability: InterpCapability`, abstract `provision`, `interpolate`, `validate_spec`, `model_identity`, default-raising `render_provision`, `attach_get_instance`.
- [ ] `register_interpolator` rejects duplicates (`UnknownAdapter`); `get_interpolator` raises `UnknownAdapter` on miss; `interpolator_names` sorted.

**Verify:** `pixi run pytest tests/core/test_interpolator_registry.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/core/test_interpolator_registry.py
"""Interpolator registry + interface shape."""
import pytest

from kinoforge.core import registry
from kinoforge.core.errors import UnknownAdapter
from kinoforge.core.fps_resolver import InterpCapability
from kinoforge.core.interfaces import (
    InterpolateJob,
    InterpolateResult,
    InterpolatorEngine,
)


class _FakeRife(InterpolatorEngine):
    name = "fake-rife"
    requires_compute = True
    requires_local_weights = True
    capability = InterpCapability.ARBITRARY_TIMESTEP

    def provision(self, instance, cfg, *, cancel_token=None): ...
    def interpolate(self, instance, job, cfg, *, cancel_token=None):
        raise NotImplementedError
    def validate_spec(self, job): ...
    def model_identity(self, cfg): return "fake-rife"


def test_register_and_get_roundtrip():
    name = "fake-rife-rt"
    registry.register_interpolator(name, _FakeRife)
    assert registry.get_interpolator(name)() .name == "fake-rife"
    assert name in registry.interpolator_names()


def test_duplicate_registration_rejected():
    registry.register_interpolator("dup-rife", _FakeRife)
    with pytest.raises(UnknownAdapter):
        registry.register_interpolator("dup-rife", _FakeRife)


def test_unknown_get_raises():
    with pytest.raises(UnknownAdapter):
        registry.get_interpolator("nope-rife")


def test_job_and_result_fields():
    job = InterpolateJob(source=object(), target_fps=60.0)
    assert job.target_fps == 60.0
    res = InterpolateResult(
        artifact=object(), input_fps=16.0, output_fps=60.0,
        input_frame_count=16, output_frame_count=60, elapsed_s=1.0,
    )
    assert res.output_fps == 60.0
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement**

In `core/interfaces.py`, after `UpscaleResult` (import `InterpCapability` at top: `from kinoforge.core.fps_resolver import InterpCapability`):

```python
@dataclass(frozen=True)
class InterpolateJob:
    """One unit of frame-interpolation work — engine-agnostic.

    Attributes:
        source: Input video Artifact (uri local or pod-fetchable).
        target_fps: Requested output frame rate.
        params: Engine-specific overrides (model tag, precision); engines
            validate via ``validate_spec``.
    """

    source: Artifact
    target_fps: float
    params: dict = field(default_factory=dict)  # type: ignore[type-arg]


@dataclass(frozen=True)
class InterpolateResult:
    """Output of one interpolation job.

    Attributes:
        artifact: Rendered interpolated video.
        input_fps: Probed source frame rate.
        output_fps: Delivered frame rate.
        input_frame_count: Source frame count.
        output_frame_count: Delivered frame count.
        elapsed_s: Wall-clock seconds inside the engine.
        engine_meta: Free-form engine telemetry.
    """

    artifact: Artifact
    input_fps: float
    output_fps: float
    input_frame_count: int
    output_frame_count: int
    elapsed_s: float
    engine_meta: dict = field(default_factory=dict)  # type: ignore[type-arg]
```

`InterpolatorEngine` ABC after `UpscalerEngine` (mirror it exactly, swapping method names/types and adding `capability`):

```python
class InterpolatorEngine(ABC):
    """A swappable frame interpolator; owns env setup; declares capability.

    Video-in / video-out at a higher frame rate. Separate from UpscalerEngine
    because the surfaces don't overlap.

    Attributes:
        name: Registry key (e.g. ``"rife"``).
        requires_compute: True when this engine needs a remote pod.
        requires_local_weights: True when it downloads weights on the pod.
        capability: How it reaches an intermediate time (arbitrary vs
            recursive-2x); the stage's fps resolver consults this.
    """

    name: str
    requires_compute: bool
    requires_local_weights: bool
    capability: InterpCapability

    @abstractmethod
    def provision(  # noqa: D102
        self, instance: "Instance | None", cfg: dict[str, object],
        *, cancel_token: "CancelToken | None" = None,
    ) -> None: ...

    @abstractmethod
    def interpolate(  # noqa: D102
        self, instance: "Instance | None", job: InterpolateJob,
        cfg: dict[str, object], *, cancel_token: "CancelToken | None" = None,
    ) -> InterpolateResult: ...

    @abstractmethod
    def validate_spec(self, job: InterpolateJob) -> None:
        """Raise on an engine-unsupportable job."""
        ...

    @abstractmethod
    def model_identity(self, cfg: dict[str, object]) -> str:
        """Sink-filename slug (e.g. ``'rife-v4.9'``). MUST NOT raise."""
        ...

    def render_provision(self, cfg: dict[str, object]) -> "RenderedProvision":
        """Emit boot payload. Default raises; remote engines override."""
        del cfg
        raise NotImplementedError(
            f"{type(self).__name__} does not support remote provisioning"
        )

    def attach_get_instance(
        self, get_instance: "Callable[[str], Instance]",
    ) -> None:
        """Wire provider lookup; mirrors UpscalerEngine.attach_get_instance."""
        self._get_instance = get_instance  # noqa: SLF001
```

In `core/registry.py` (add near `_upscalers`, line 37, and after `upscaler_names`):

```python
_interpolators: dict[str, Callable[[], "InterpolatorEngine"]] = {}


def register_interpolator(
    name: str, factory: Callable[[], "InterpolatorEngine"]
) -> None:
    """Register an interpolator factory under ``name`` (duplicates rejected)."""
    if name in _interpolators:
        raise UnknownAdapter(f"interpolator {name!r} already registered")
    _interpolators[name] = factory


def get_interpolator(name: str) -> Callable[[], "InterpolatorEngine"]:
    """Return the factory for ``name`` or raise UnknownAdapter."""
    try:
        return _interpolators[name]
    except KeyError:
        raise UnknownAdapter(
            f"no interpolator registered as {name!r}; "
            f"known: {sorted(_interpolators)}"
        ) from None


def interpolator_names() -> list[str]:
    """Return all registered interpolator names, sorted."""
    return sorted(_interpolators)
```

Add `InterpolatorEngine` to the `TYPE_CHECKING` import block in `registry.py` (mirror how `UpscalerEngine` is imported there).

- [ ] **Step 4: Run — expect PASS**; also `pixi run typecheck` clean.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/core/registry.py tests/core/test_interpolator_registry.py
git commit -m "feat(core): InterpolatorEngine interface + registry (capability-driven)"
```

---

### Task 5: `InterpolateStage`

**Goal:** The pipeline stage: read the input clip, probe its fps + frame count locally when readable, resolve the plan; on decimate/passthrough do it locally (no engine), else call the engine; always write `state.artifacts["interpolated"]`.

**Files:**
- Create: `src/kinoforge/pipeline/interpolate.py`
- Test: `tests/pipeline/test_interpolate_stage.py`

**Acceptance Criteria:**
- [ ] Reads `state.artifacts["clip"]`, writes `state.artifacts["interpolated"]`.
- [ ] Local source + `target > source` (arbitrary) → calls `engine.interpolate`, passes through the result artifact.
- [ ] Local source + `target < source` → does NOT call the engine; runs `decimate_video_fps`, publishes a local artifact.
- [ ] Local source + `target == source` → neither engine nor decimate; passes the clip through unchanged as `interpolated`.
- [ ] Remote (`http`) source → always calls the engine (server probes + plans); no local probe required.

**Verify:** `pixi run pytest tests/pipeline/test_interpolate_stage.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing tests** (mock engine + injected probe/decimate seams)

```python
# tests/pipeline/test_interpolate_stage.py
"""InterpolateStage routing: engine vs local-decimate vs passthrough."""
import dataclasses

from kinoforge.core.fps_resolver import InterpCapability
from kinoforge.core.interfaces import (
    Artifact,
    GenerationRequest,
    InterpolateResult,
    PipelineState,
)
from kinoforge.pipeline.interpolate import InterpolateStage


class _Engine:
    name = "rife"
    capability = InterpCapability.ARBITRARY_TIMESTEP

    def __init__(self):
        self.calls = 0

    def interpolate(self, instance, job, cfg, *, cancel_token=None):
        self.calls += 1
        return InterpolateResult(
            artifact=Artifact(uri="http://pod/artifacts/out.mp4"),
            input_fps=16.0, output_fps=job.target_fps,
            input_frame_count=16, output_frame_count=60, elapsed_s=1.0,
        )


def _state(uri: str) -> PipelineState:
    return PipelineState(
        request=GenerationRequest(prompt="", mode="upscale"),
        artifacts={"clip": Artifact(uri=uri)},
    )


def _stage(engine, target_fps, **kw):
    return InterpolateStage(
        engine=engine, target_fps=target_fps, instance=object(), cfg={},
        probe_fps=kw.get("probe_fps", lambda p: 16.0),
        probe_count=kw.get("probe_count", lambda p: 16),
        decimate=kw.get("decimate", lambda b, f: b"DECIMATED"),
        read_bytes=kw.get("read_bytes", lambda p: b"CLIP"),
        publish=kw.get("publish", lambda b: "file:///out/deci.mp4"),
    )


def test_arbitrary_upshift_calls_engine():
    eng = _Engine()
    out = _stage(eng, 60.0).run(_state("file:///in.mp4"))
    assert eng.calls == 1
    assert out.artifacts["interpolated"].uri == "http://pod/artifacts/out.mp4"


def test_downshift_decimates_locally_no_engine():
    eng = _Engine()
    out = _stage(eng, 12.0).run(_state("file:///in.mp4"))
    assert eng.calls == 0
    assert out.artifacts["interpolated"].uri == "file:///out/deci.mp4"


def test_equal_fps_passthrough():
    eng = _Engine()
    st = _state("file:///in.mp4")
    out = _stage(eng, 16.0).run(st)
    assert eng.calls == 0
    assert out.artifacts["interpolated"].uri == "file:///in.mp4"


def test_remote_source_always_calls_engine():
    eng = _Engine()
    # probe_fps must NOT be consulted for http; force it to blow up if used.
    def boom(p):
        raise AssertionError("probed a remote source locally")
    out = _stage(eng, 60.0, probe_fps=boom, probe_count=boom).run(
        _state("http://pod/in.mp4")
    )
    assert eng.calls == 1
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement** `src/kinoforge/pipeline/interpolate.py`:

```python
"""InterpolateStage — PipelineState in, PipelineState out.

Reads ``state.artifacts["clip"]``, raises its frame rate to ``target_fps`` and
writes ``state.artifacts["interpolated"]``. For a locally-readable source it
probes the fps + frame count and routes via the pure fps resolver: an upshift
calls the engine, a downshift decimates locally (no GPU), an equal rate passes
through. A remote (http) source always calls the engine (the server probes and
plans). Mirrors :mod:`kinoforge.pipeline.upscale`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from kinoforge.core.cancel import CancelToken
from kinoforge.core.frames import ffprobe_fps
from kinoforge.core.fps_resolver import resolve_fps_target
from kinoforge.core.interfaces import (
    Artifact,
    Instance,
    InterpolateJob,
    InterpolatorEngine,
    PipelineState,
)
from kinoforge.pipeline.decimate import decimate_video_fps


def _default_count(path: str | Path) -> int:
    """Probe the video's frame count via ffprobe (nb_read_packets)."""
    import subprocess

    argv = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-count_packets", "-show_entries", "stream=nb_read_packets",
        "-of", "csv=p=0", str(path),
    ]
    out = subprocess.run(argv, capture_output=True, check=True)  # noqa: S603
    return int(out.stdout.decode().strip())


@dataclass
class InterpolateStage:
    """A Stage that raises the clip's frame rate to ``target_fps``."""

    engine: InterpolatorEngine
    target_fps: float
    instance: Instance | None
    cfg: dict[str, Any]
    cancel_token: CancelToken | None = None
    probe_fps: Callable[[str | Path], float] = ffprobe_fps
    probe_count: Callable[[str | Path], int] = _default_count
    decimate: Callable[[bytes, float], bytes] = decimate_video_fps
    read_bytes: Callable[[str | Path], bytes] = lambda p: Path(p).read_bytes()
    publish: Callable[[bytes], str] | None = None

    def run(self, state: PipelineState) -> PipelineState:
        """Run interpolation, returning a new state with ``interpolated`` set."""
        clip = state.artifacts["clip"]
        local = self._local_path(clip)
        if local is not None:
            interpolated = self._run_local(clip, local)
        else:
            interpolated = self._run_engine(clip)
        new_artifacts = dict(state.artifacts)
        new_artifacts["interpolated"] = interpolated
        return replace(state, artifacts=new_artifacts)

    def _local_path(self, clip: Artifact) -> str | None:
        uri = clip.uri
        if uri.startswith("file://"):
            return uri.removeprefix("file://")
        if uri.startswith("/"):
            return uri
        return None

    def _run_local(self, clip: Artifact, path: str) -> Artifact:
        source_fps = self.probe_fps(path)
        count = self.probe_count(path)
        plan = resolve_fps_target(
            source_fps, self.target_fps, self.engine.capability,
            source_frame_count=count,
        )
        if plan.skip_gpu:
            if plan.decimate_to is None:
                return clip  # passthrough (equal fps)
            out = self.decimate(self.read_bytes(path), plan.decimate_to)
            if self.publish is None:
                raise ValueError("InterpolateStage needs a publish seam to decimate")
            return replace(clip, uri=self.publish(out))
        return self._run_engine(clip)

    def _run_engine(self, clip: Artifact) -> Artifact:
        job = InterpolateJob(source=clip, target_fps=self.target_fps)
        result = self.engine.interpolate(
            self.instance, job, self.cfg, cancel_token=self.cancel_token
        )
        return result.artifact
```

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/pipeline/interpolate.py tests/pipeline/test_interpolate_stage.py
git commit -m "feat(pipeline): InterpolateStage — resolver-routed engine/decimate/passthrough"
```

---

### Task 6: Config surface — `interpolate:` block

**Goal:** Add `RifeEngineConfig` + `InterpolateConfig` + `Config.interpolate`, with `fps > 0` validation, an engine-block presence check, and capability-key wiring.

**Files:**
- Modify: `src/kinoforge/core/config.py` (new models near `UpscaleConfig` ~651; `Config.interpolate` field ~1080; capability_key ~1272)
- Test: `tests/test_config_interpolate.py`

**Acceptance Criteria:**
- [ ] `InterpolateConfig(engine, fps, rife)` — `fps: float` validated `> 0`.
- [ ] `engine == "rife"` requires a `rife:` block (else `ConfigError`).
- [ ] `RifeEngineConfig(weights_ref, model="rife49", precision="fp16")`; precision in `{"fp16","fp32"}`.
- [ ] `capability_key()` appends `"interpolate"` to `stages` and sets an `interpolator` factor when `cfg.interpolate` present. (Add `interpolator` + `interpolator_fps` to `CapabilityKey` — grep `class CapabilityKey` and extend it, defaulting to `""`/`0.0` so existing hashes for non-interp cfgs are unchanged.)

**Verify:** `pixi run pytest tests/test_config_interpolate.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/test_config_interpolate.py
"""interpolate: config block validation + capability-key wiring."""
import pytest

from kinoforge.core.config import InterpolateConfig, RifeEngineConfig
from kinoforge.core.errors import ConfigError


def test_fps_must_be_positive():
    with pytest.raises((ConfigError, ValueError)):
        InterpolateConfig(engine="rife", fps=0.0, rife=RifeEngineConfig(weights_ref="hf:x"))


def test_rife_engine_requires_block():
    with pytest.raises(ConfigError):
        InterpolateConfig(engine="rife", fps=60.0, rife=None)


def test_valid_rife_config():
    c = InterpolateConfig(
        engine="rife", fps=59.94,
        rife=RifeEngineConfig(weights_ref="hf:kinoforge/rife", model="rife49"),
    )
    assert c.fps == 59.94
    assert c.rife.model == "rife49"


def test_precision_allowlist():
    with pytest.raises(ConfigError):
        RifeEngineConfig(weights_ref="hf:x", precision="int4")
```

Add a capability-key test mirroring the repo's existing capability-key tests
(grep `capability_key` in `tests/` to match the constructor/fixture style):

```python
def test_capability_key_includes_interpolate_stage():
    # Build a minimal Config with an interpolate block (copy the smallest
    # existing Config fixture from tests/test_config*.py and add interpolate=).
    # Assert "interpolate" in cfg.capability_key().stages and
    # cfg.capability_key().interpolator == "rife".
    ...
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement** in `core/config.py`:

```python
class RifeEngineConfig(BaseModel):
    """RIFE v4 engine params, validated at cfg-load time.

    Attributes:
        weights_ref: Source ref for the RIFE model weights (``hf:`` or
            ``http(s)``), fetched during pod provision.
        model: RIFE model tag (e.g. ``"rife49"``); selects the arch on the pod.
        precision: ``"fp16"`` (default) or ``"fp32"``.
    """

    weights_ref: str
    model: str = "rife49"
    precision: str = "fp16"

    @field_validator("precision")
    @classmethod
    def _validate_precision(cls, v: str) -> str:
        if v not in ("fp16", "fp32"):
            raise ConfigError(f"rife precision {v!r} not in ('fp16', 'fp32')")
        return v


class InterpolateConfig(BaseModel):
    """Top-level ``interpolate:`` block; presence activates InterpolateStage.

    Attributes:
        engine: Interpolator name (registry key). v1 supports ``"rife"``.
        fps: Target output frame rate (float, > 0).
        rife: RIFE-specific block; required when ``engine == "rife"``.
    """

    engine: str
    fps: float
    rife: RifeEngineConfig | None = None

    @field_validator("fps")
    @classmethod
    def _validate_fps(cls, v: float) -> float:
        if v <= 0:
            raise ConfigError(f"interpolate fps must be > 0, got {v}")
        return v

    @model_validator(mode="after")
    def _validate_rife_wiring(self) -> Self:
        if self.engine == "rife" and self.rife is None:
            raise ConfigError("engine=rife requires a cfg.interpolate.rife block")
        return self
```

Add the field to `Config` (after `upscale`, line 1080):

```python
    interpolate: InterpolateConfig | None = None
```

Extend `CapabilityKey` (grep `class CapabilityKey`; add fields with defaults):

```python
    interpolator: str = ""
    interpolator_fps: float = 0.0
```

And in `capability_key()` after the upscale block (~1285):

```python
        interpolator = ""
        interpolator_fps = 0.0
        if self.interpolate is not None:
            stages.append("interpolate")
            interpolator = self.interpolate.engine
            interpolator_fps = self.interpolate.fps
```

then pass `interpolator=interpolator, interpolator_fps=interpolator_fps` into the
`CapabilityKey(...)` constructor.

- [ ] **Step 4: Run — expect PASS**; `pixi run typecheck` clean.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/config.py tests/test_config_interpolate.py
git commit -m "feat(config): interpolate: block (RifeEngineConfig + capability-key wiring)"
```

---

### Task 7: Orchestrator wiring (minimal — standalone path only)

**Goal:** Append `InterpolateStage` when `cfg.interpolate` is set; after the walk, materialize the `interpolated` artifact (fetch pod bytes → publish) and return it for the standalone entry. NO inter-stage refactor (see the Planning-time correction).

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` (append near the upscale append ~1858-1872; materialize + artifact_key near ~1911-1974)
- Test: `tests/core/test_orchestrator_interpolate.py`

**Acceptance Criteria:**
- [ ] `cfg.interpolate is not None` → an `InterpolateStage` is appended, built from `registry.get_interpolator(cfg.interpolate.engine)` with `target_fps=cfg.interpolate.fps`.
- [ ] Post-walk: when `state.artifacts` has `"interpolated"` with an `http(s)` uri and a sink, the bytes are fetched and `sink.publish(..., kind="interpolated")` is called; the artifact uri is replaced with the local `file://` path.
- [ ] Standalone entry (`skip_clip_stage and cfg.interpolate is not None`) returns the `interpolated` artifact.
- [ ] A cfg WITHOUT `interpolate` appends no interp stage and is byte-for-byte unaffected (regression assert).

**Verify:** `pixi run pytest tests/core/test_orchestrator_interpolate.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write failing tests.** Use the repo's existing orchestrator test
harness (grep `tests/core/test_orchestrator*` for the fake sink / fake registry /
monkeypatch style used by the upscale materialize tests — reuse it verbatim). Two
tests: (a) interp stage appended + interpolated materialized + published with
`kind="interpolated"`; (b) no-interpolate cfg leaves stages unchanged.

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement.** Append block, mirroring the upscale append (after line 1872):

```python
        # Append InterpolateStage when cfg.interpolate is set. Standalone path
        # only (see plan Planning-time correction): interp runs on its own pod
        # via a separate `kinoforge interpolate` invocation, so its input clip
        # is always the seeded initial_clip (local/http), never a mid-walk
        # pod-side upscaled artifact.
        if cfg.interpolate is not None:
            from kinoforge.core import registry as _registry
            from kinoforge.pipeline.interpolate import InterpolateStage

            interp_engine = _registry.get_interpolator(cfg.interpolate.engine)()
            stages.append(
                InterpolateStage(
                    engine=interp_engine,
                    target_fps=cfg.interpolate.fps,
                    instance=session.instance,
                    cfg=cfg_dict,
                    cancel_token=cancel_token,
                    publish=(
                        (lambda b: f"file://{sink.publish(b, prompt='interpolate', extension='.mp4', provider=cfg.interpolate.engine, model='interp', kind='interpolated')}")
                        if sink is not None else None
                    ),
                )
            )
```

Extend `artifact_key` (line 1913):

```python
        if skip_clip_stage and cfg.interpolate is not None:
            artifact_key = "interpolated"
        elif skip_clip_stage and cfg.upscale is not None:
            artifact_key = "upscaled"
        else:
            artifact_key = "clip"
```

Add an interpolated-materialize block mirroring the upscaled one (after the
upscaled materialize, before `artifact = state.artifacts[artifact_key]`, ~1970).
Interp has no downscale, so it is the simpler fetch+publish half:

```python
        interpolated = state.artifacts.get("interpolated")
        if (
            interpolated is not None
            and sink is not None
            and interpolated.uri.startswith(("http://", "https://"))
        ):
            import urllib.request as _urequest

            _log.info("materializing interpolated artifact from %s", interpolated.uri)
            req = _urequest.Request(  # noqa: S310 — pod proxy URL only
                interpolated.uri,
                headers={"User-Agent": "kinoforge-orchestrator/0.1"},
            )
            with _urequest.urlopen(req, timeout=600) as resp:  # noqa: S310
                body = resp.read()
            provider_tag = cfg.interpolate.engine
            local_path = sink.publish(
                body, prompt="interpolate", extension=".mp4",
                provider=provider_tag, model="interp", kind="interpolated",
            )
            state.artifacts["interpolated"] = dataclasses.replace(
                interpolated, uri=f"file://{local_path}"
            )
```

(Note: the `publish` seam passed to the stage covers the local-decimate branch;
this post-walk block covers the engine branch whose artifact uri is the pod proxy.
Both end with a published `file://` artifact.)

- [ ] **Step 4: Run — expect PASS**; run the FULL suite `pixi run test` to confirm no upscale regression.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_interpolate.py
git commit -m "feat(orchestrator): append InterpolateStage + materialize interpolated artifact"
```

---

### Task 8: CLI — `kinoforge interpolate` subcommand

**Goal:** Add the `interpolate` subparser + `_cmd_interpolate`, mirroring `upscale`: `--video`, `--fps`, `--config`, `--no-reuse`, `--attach-pod`, `--dry-run`; drives `generate(skip_clip_stage=True, initial_clip=…)`.

**Files:**
- Modify: `src/kinoforge/cli/_main.py` (subparser near the `upscale` parser ~565-598; dispatch table)
- Modify: `src/kinoforge/cli/_commands.py` (`_cmd_interpolate` near `_cmd_upscale` ~646)
- Test: `tests/cli/test_cmd_interpolate.py`

**Acceptance Criteria:**
- [ ] `interpolate` parser accepts `--video` (required), `--fps` (float), `--config`, `--no-reuse`, `--attach-pod`, `--dry-run`.
- [ ] `--no-reuse` + `--attach-pod` → exit 2 (mutually exclusive), no cfg load.
- [ ] Missing `--config` → exit 2; cfg without `interpolate:` block → exit 2.
- [ ] `--fps` overrides `cfg.interpolate.fps`; `--dry-run` prints the plan + exits 0.
- [ ] Non-dry-run calls `orchestrator.generate(..., skip_clip_stage=True, initial_clip=_resolve_input_video_as_artifact(args.video))`.

**Verify:** `pixi run pytest tests/cli/test_cmd_interpolate.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write failing tests** (mirror `tests/cli/test_cmd_upscale*`; grep for it and copy the arg-namespace + monkeypatched-orchestrator style). Cover: mutual-exclusion exit 2; missing config exit 2; no-interpolate-block exit 2; dry-run prints fps+engine and exits 0; non-dry-run passes `skip_clip_stage=True` + the resolved fps to a captured fake `generate`.

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement.** Subparser in `_main.py` (after the `upscale` parser):

```python
    p_interp = sub.add_parser(
        "interpolate", help="raise a video's frame rate (RIFE)"
    )
    p_interp.add_argument(
        "--video", required=True,
        help="input video: local path or http(s):// URL",
    )
    p_interp.add_argument(
        "--fps", type=float, default=None,
        help="target output fps (overrides cfg.interpolate.fps)",
    )
    p_interp.add_argument("--no-reuse", action="store_true")
    p_interp.add_argument("--attach-pod", default=None)
    p_interp.add_argument("--dry-run", action="store_true")
```

Register `interpolate -> _cmd_interpolate` in the dispatch table (grep how
`upscale` maps to `_cmd_upscale`).

`_cmd_interpolate` in `_commands.py` (mirror `_cmd_upscale`, swapping upscale→interpolate;
`--fps` override replaces the `--scale` override):

```python
def _cmd_interpolate(args: argparse.Namespace, ctx: SessionContext) -> int:
    """Handle ``interpolate`` subcommand — standalone frame interpolation."""
    if getattr(args, "no_reuse", False) and getattr(args, "attach_pod", None):
        print(
            "error: --no-reuse and --attach-pod are mutually exclusive",
            file=sys.stderr,
        )
        return 2
    if ctx.cfg is None:
        print("error: --config required for interpolate", file=sys.stderr)
        return 2
    cfg = ctx.cfg
    if cfg.interpolate is None:
        print(
            "error: --config must contain an `interpolate:` block; "
            "see examples/configs/interpolate-rife-60fps.yaml",
            file=sys.stderr,
        )
        return 2

    fps = args.fps if args.fps is not None else cfg.interpolate.fps
    if fps <= 0:
        print(f"error: invalid --fps {fps}", file=sys.stderr)
        return 2

    if getattr(args, "dry_run", False):
        print("interpolate plan:")
        print(f"  source: {args.video}")
        print(f"  fps: {fps}")
        print(f"  engine: {cfg.interpolate.engine}")
        print(f"  no_reuse: {bool(getattr(args, 'no_reuse', False))}")
        print(f"  attach_pod: {getattr(args, 'attach_pod', None)}")
        return 0

    from kinoforge.core import orchestrator as _orchestrator

    # CLI --fps override reaches the stage via cfg.interpolate.fps.
    if args.fps is not None:
        cfg = cfg.model_copy(
            update={"interpolate": cfg.interpolate.model_copy(update={"fps": fps})}
        )

    input_artifact = _resolve_input_video_as_artifact(args.video)
    store = ctx.store()
    sink_local = _build_sink(cfg, args)
    run_id = (
        args.run_id if getattr(args, "run_id", None) is not None
        else f"interpolate-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )

    instance: Instance | None = None
    attach_pod_id = getattr(args, "attach_pod", None)
    if attach_pod_id:
        instance, rc = _resolve_attach_pod(ctx, cfg, attach_pod_id)
        if rc is not None:
            return rc
    elif not args.no_reuse:
        instance, report = _scan_warm_candidates(ctx, cfg)
        summary = report.summarize()
        if summary:
            logger.info(summary)

    artifact, returned_instance = _orchestrator.generate(
        cfg, request=None, store=store, sink=sink_local, run_id=run_id,
        state_dir=ctx.state_dir, cancel_token=ctx.cancel_token, instance=instance,
        single=bool(args.no_reuse), skip_clip_stage=True, initial_clip=input_artifact,
    )
    del returned_instance
    print(f"interpolated: uri={artifact.uri!r}")
    return 0
```

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/cli/test_cmd_interpolate.py
git commit -m "feat(cli): kinoforge interpolate subcommand (standalone --video/--fps)"
```

---

### Task 9: `RifeEngine` client (HTTP to pod)

**Goal:** The `InterpolatorEngine` implementation: `interpolate()` uploads a local source, POSTs `/interpolate`, polls `/interpolate/status/{id}`, returns an `InterpolateResult`; plus `render_provision` (Practical-RIFE install + weights fetch), `validate_spec`, `model_identity`. Self-registers as `"rife"`.

**Files:**
- Create: `src/kinoforge/interpolators/__init__.py` (empty package marker)
- Create: `src/kinoforge/interpolators/rife/__init__.py` (registers `RifeEngine`, ImportError-guarded like `upscalers/flashvsr/__init__.py`)
- Create: `src/kinoforge/interpolators/rife/_engine.py`
- Test: `tests/interpolators/test_rife_engine.py`

**Acceptance Criteria:**
- [ ] `interpolate()` on a `file://` source calls `_upload_source` (PUT /upload with sha verify), POSTs `/interpolate` with `source_url`, `target_fps`, `engine="rife"`, `rife` block; polls to `state=="done"`; returns `InterpolateResult` with fps + counts from the server payload.
- [ ] `state=="error"` → raises `InterpolationError`.
- [ ] `validate_spec` rejects `target_fps <= 0`.
- [ ] `model_identity` returns `rife-<model>` (never raises on missing fields).
- [ ] `capability == InterpCapability.ARBITRARY_TIMESTEP`.
- [ ] Registered: `registry.get_interpolator("rife")` resolves after import.

**Verify:** `pixi run pytest tests/interpolators/test_rife_engine.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write failing tests** (mock HTTP seams — mirror `tests/upscalers/test_flashvsr_engine*` for the `_http_json` / `retry_proxy_call` / fake-instance patterns). Cover the done-poll happy path, the error-state raise, `validate_spec`, `model_identity` missing-field safety, and registration.

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement.** Model `_engine.py` directly on `upscalers/flashvsr/_engine.py`
(lines 200-339): reuse `retry_proxy_call`, `_http_json`, `_upload_source`, `_base_url`
verbatim in shape, swapping `/upscale`→`/interpolate`, `UpscaleResult`→`InterpolateResult`,
`UpscaleFailed`→`InterpolationError`. Class skeleton:

```python
class RifeEngine(InterpolatorEngine):
    """RIFE v4 arbitrary-timestep frame interpolator (pod-side)."""

    name = "rife"
    requires_compute = True
    requires_local_weights = True
    capability = InterpCapability.ARBITRARY_TIMESTEP

    def validate_spec(self, job: InterpolateJob) -> None:
        if job.target_fps <= 0:
            raise ValueError(f"rife: target_fps must be > 0, got {job.target_fps}")

    def model_identity(self, cfg: dict[str, object]) -> str:
        try:
            block = cast(dict[str, Any],
                         cast(dict[str, Any], cfg["interpolate"])["rife"])
            return f"rife-{block['model']}"
        except (KeyError, TypeError):
            return ""

    def provision(self, instance, cfg, *, cancel_token=None):
        del instance, cfg, cancel_token  # captured in render_provision

    def interpolate(self, instance, job, cfg, *, cancel_token=None):
        self.validate_spec(job)
        if instance is None:
            raise ValueError("RifeEngine requires a compute instance")
        base = self._base_url(instance)
        source_uri = job.source.uri
        if source_uri.startswith(("file://", "/")):
            source_uri = self._upload_source(instance, Path(source_uri.removeprefix("file://")))
        block = cast(dict[str, Any],
                     cast(dict[str, Any], cfg.get("interpolate", {})).get("rife", {}))
        submit_payload = {
            "source_url": source_uri,
            "source_filename": source_uri.rsplit("/", 1)[-1] or "in.mp4",
            "target_fps": job.target_fps,
            "engine": "rife",
            "rife": block,
        }
        # ... retry_proxy_call POST /interpolate -> job_id
        # ... poll retry_proxy_call GET /interpolate/status/{job_id}
        #     state=="done" -> InterpolateResult(
        #         artifact=Artifact(uri=f"{base}/artifacts/{r['filename']}",
        #                           sha256=r["sha256"], size=r["size"]),
        #         input_fps=r["input_fps"], output_fps=r["output_fps"],
        #         input_frame_count=r["input_frame_count"],
        #         output_frame_count=r["output_frame_count"],
        #         elapsed_s=time.monotonic()-t0, engine_meta=r.get("engine_meta", {}))
        #     state=="error" -> raise InterpolationError(job_id, status.get("error",""))
```

`render_provision`: emit a bootstrap that installs Practical-RIFE and fetches
weights. RIFE is light — no BSA wheel, no diffsynth. Concrete script body:

```python
    def render_provision(self, cfg):
        block = cast(dict[str, Any],
                     cast(dict[str, Any], cfg["interpolate"])["rife"])
        weights_ref = str(block["weights_ref"])
        model = str(block.get("model", "rife49"))
        script = "".join([
            "set -euo pipefail\n",
            # Practical-RIFE: arbitrary-timestep v4 inference repo. Pin a commit.
            "pip install --no-deps "
            '"git+https://github.com/hzwer/Practical-RIFE@<PIN_SHA>"\n',
            'pip install "torch" "numpy" "opencv-python-headless" '
            '"imageio[ffmpeg]"\n',
            # Fetch RIFE weights (train_log/*.pkl) to a stable pod path.
            f"python -m kinoforge.interpolators.rife._fetch_weights "
            f"--ref {weights_ref} --model {model} "
            f"--dest /workspace/models/rife\n",
        ])
        return RenderedProvision(
            script=script, run_cmd=[], image="", ports=[], env_required=["HF_TOKEN"],
        )
```

> **Execution-time TODO (unblock before Task 10/11):** replace `<PIN_SHA>` with a
> real Practical-RIFE commit, and confirm the weights layout (`train_log/flownet.pkl`)
> for the chosen `model`. Add `_fetch_weights.py` (mirror
> `upscalers/flashvsr/_fetch_weights.py`) if not shipping weights in the image.

- [ ] **Step 4: Run — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/interpolators tests/interpolators/test_rife_engine.py
git commit -m "feat(interpolators): RifeEngine client + self-registration"
```

---

### Task 10: On-pod RIFE runtime + server `/interpolate` endpoints

**Goal:** Make the pod actually interpolate: a `_runtime.py` that loads RIFE and renders the arbitrary-timestep schedule to video, and `/interpolate` + `/interpolate/status/{id}` endpoints on the embedded server, mirroring the `/upscale` endpoints.

**Files:**
- Create: `src/kinoforge/interpolators/rife/_runtime.py`
- Modify: `src/kinoforge/engines/diffusers/servers/ln.py` (a.k.a. `wan_t2v_server.py`) — add endpoints mirroring `/upscale` (grep `@app.post("/upscale")`, `_run_upscale_job`, `@app.get("/upscale/status/`)
- Test: `tests/interpolators/test_rife_runtime.py` (offline: schedule → frame-op list, mocked model)

**Acceptance Criteria:**
- [ ] `_runtime.py` exposes `interpolate(local_path, target_fps, params) -> {filename, sha256, size, input_fps, output_fps, input_frame_count, output_frame_count, engine_meta}`; it probes source fps + count, calls `resolve_fps_target(...ARBITRARY_TIMESTEP...)`, synthesizes each `(src_idx, t)` frame (copy when `t==0`), muxes at `output_fps`, and applies `decimate_video_fps` only if `plan.decimate_to` is set.
- [ ] Server `/interpolate` enqueues a job (returns `{"job_id"}`), runs it under the same async lock pattern as `/upscale` (`asyncio.to_thread`), and `/interpolate/status/{id}` returns `{"state", "result"|"error"}`.
- [ ] Offline test: with a fake RIFE model (returns a solid frame per call), a 16→32 request produces the right number of frame ops and a non-empty mp4 via real ffmpeg mux.

**Verify:** `pixi run pytest tests/interpolators/test_rife_runtime.py -v` → PASS (offline)

**Steps:**

- [ ] **Step 1: Read `ln.py` first.** Grep `@app.post("/upscale")`, `UpscaleRequest`,
`_run_upscale_job`, `@app.get("/upscale/status/`, `@app.put("/upload")` and read
those ~120 lines. The `/interpolate` endpoints mirror them 1:1.

- [ ] **Step 2: Write the failing offline runtime test** (fake model + real ffmpeg
mux; assert output frame count == resolver's `len(schedule)` and probed
`output_fps == target`). Follow `test-design`: state the behavior + the concrete
bug each test catches (e.g. "off-by-one in schedule → wrong frame count").

- [ ] **Step 3: Implement `_runtime.py`** using `resolve_fps_target` +
`ffprobe_fps` + `_default_count` + an ffmpeg image2→mp4 mux at `output_fps`
(reuse `decimate_video_fps` for the recursive/decimate trim path — RIFE won't hit
it, but keep the code path honest). Then add the server endpoints in `ln.py`
mirroring the `/upscale` block, dispatching `engine == "rife"` to the runtime.

- [ ] **Step 4: Run — expect PASS** (offline). Full pod behavior is proven live in Task 12.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/interpolators/rife/_runtime.py src/kinoforge/engines/diffusers/servers/ln.py tests/interpolators/test_rife_runtime.py
git commit -m "feat(rife): on-pod runtime + server /interpolate endpoints"
```

---

### Task 11: Example config + RED live-smoke scaffold (committed BEFORE any spend)

**Goal:** Ship the interpolate-only example cfg and a live-smoke test that drives `kinoforge interpolate --video <fixture> --fps 60 --no-reuse`. Commit RED (skipped/xfail) BEFORE Task 12 spends, per CLAUDE.md durability rules.

**Files:**
- Create: `examples/configs/interpolate-rife-60fps.yaml`
- Create: `tests/live/test_rife_interpolate_live.py`

**Acceptance Criteria:**
- [ ] Cfg: `interpolate: {engine: rife, fps: 60.0, rife: {weights_ref: hf:…, model: rife49, precision: fp16}}`, `compute` block with `cloud_type: secure` (per the community-pool-deletions gotcha), a minimal `engine`/`models` shell matching how the upscale-only example is structured (grep `examples/configs/upscale-*.yaml`).
- [ ] Test is committed **RED**: marked `@pytest.mark.live` + skipped unless a `KINOFORGE_LIVE=1`-style env gate (grep how `test_flashvsr_height_target_live.py` gates), so CI stays green and the scaffold exists before spend.
- [ ] Test body: run the CLI on the 480² fixture (`output/20260630-221857_..._Photorealistic-cinem.mp4`), assert exit 0, probe the delivered mp4's fps == 60, and assert the ledger is clean afterward (`kinoforge list`).

**Verify:** `pixi run pytest tests/live/test_rife_interpolate_live.py -v` → SKIPPED (gated); `pixi run preflight` → exit 0 before Task 12.

**Steps:**

- [ ] **Step 1: Write the example cfg** (copy the smallest `examples/configs/upscale-*.yaml`, swap the `upscale:` block for `interpolate:`, keep `compute.cloud_type: secure`).

- [ ] **Step 2: Write the gated live test** mirroring `tests/live/test_flashvsr_height_target_live.py` (same env gate, same `--no-reuse` invocation, same post-run `kinoforge list` ledger assert, same ffprobe check — swap dims-check for `round(ffprobe_fps(out)) == 60`).

- [ ] **Step 3: Confirm RED/skipped** `pixi run pytest tests/live/test_rife_interpolate_live.py -v` → SKIPPED.

- [ ] **Step 4: Commit the scaffold (RED) BEFORE any spend**

```bash
git add examples/configs/interpolate-rife-60fps.yaml tests/live/test_rife_interpolate_live.py
git commit -m "test(live): RED scaffold — rife interpolate-only smoke (480->60fps)"
```

---

### Task 12: Live smoke + frame-QA + successful-generations entry

**Goal:** **USER-ORDERED GATE — NON-SKIPPABLE.** Run the RIFE interpolate-only live smoke on a real pod, frame-QA the output before declaring green, verify the pod is destroyed, and log the generation.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation (a live-validated RIFE ship). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `/workspace/successful-generations.md` (new entry per its schema)
- Create: `tests/live/evidence/2026-07-…_rife_interpolate_stdout.txt`

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 (creds present, zero pods, clean tree) immediately before spend.
- [ ] `KINOFORGE_LIVE=1 pixi run pytest tests/live/test_rife_interpolate_live.py -v` → `1 passed`; captured stdout saved under `tests/live/evidence/`.
- [ ] Delivered mp4 probed fps == 60 (`ffprobe_fps`), duration matches the 480² fixture within ±1 frame.
- [ ] **Frame-QA (mandatory, CLAUDE.md):** extract a 5-frame contact sheet with `ffmpeg_frames_by_count`; Claude eyeballs for motion smoothness, warping/ghosting, and temporal coherence vs the source; verdict recorded. Anything not clearly high quality gets an explicit ⚠️.
- [ ] Pod destroyed: `kinoforge list` shows `[instance overview] No running instances.` AND `No instances recorded in ledger.` after the run.
- [ ] `successful-generations.md` entry added (new capability axis: interp mode / rife engine) unless run with `--ephemeral`.

**Verify:** `KINOFORGE_LIVE=1 pixi run pytest tests/live/test_rife_interpolate_live.py -v` → `1 passed`; then `pixi run kinoforge list` → no instances.

**Steps:**

- [ ] **Step 1: Preflight** `pixi run preflight` → exit 0. If non-zero, STOP and resolve.
- [ ] **Step 2: Run the smoke** with live polling (per CLAUDE.md: poll RunPod GPU/CPU/mem + costPerHr every 60-90s; if GPU 0% for ≥3 probes mid-run, capture logs, destroy, fail fast).
- [ ] **Step 3: Frame-QA** the delivered mp4 (5-frame contact sheet) BEFORE reporting green; record the verdict.
- [ ] **Step 4: Verify teardown** `pixi run kinoforge list` → no instances + empty ledger; destroy explicitly if anything survives.
- [ ] **Step 5: Log + commit**

```bash
git add successful-generations.md tests/live/evidence/
git commit -m "docs(gen): rife interpolate-only live-green (480->60fps, frame-QA'd)"
```

---

## Self-Review

- **Spec coverage:** §2 engine RIFE → Tasks 9/10. §3 resolver foundation → Task 2. §4 engine → Task 9. §5.1 resolver → Task 2; §5.2 helpers → Tasks 1+3; §5.3 engine package → Task 9; §5.4 stage → Task 5; §5.5 orchestrator → Task 7 (reclassified per correction); §5.6 config → Task 6; §5.7 CLI → Task 8; §5.8 server → Task 10; §5.9 errors → Task 0. §6 testing → each task's tests + Tasks 11/12 live. §7 gotchas (mp4 pipe-seek) → Task 3; (runtime fps) → Task 5. §8 out-of-scope respected (no FILM/GMFSS, no co-resident, no single-pass). **Gap intentionally reclassified:** single-pass generate+interp (spec §5.5) → out of scope per Planning-time correction; combined workflow = command chaining.
- **Placeholder scan:** one deliberate `<PIN_SHA>` in Task 9 render_provision, flagged with an explicit execution-time TODO (real infra pin that must be chosen against the live repo — not a lazy gap). Tasks 7/9/10/11 reuse existing test harnesses via named greps rather than repeating 200-line fixtures; the behavior + assertions are specified.
- **Type consistency:** `resolve_fps_target(source_fps, target_fps, cap, *, source_frame_count)` and `FpsPlan(schedule, recursion_depth, decimate_to, skip_gpu)` consistent across Tasks 2/5/10. `InterpolateResult(artifact, input_fps, output_fps, input_frame_count, output_frame_count, elapsed_s, engine_meta)` consistent across Tasks 4/9/10. `decimate_video_fps(bytes, target_fps, *, run)` consistent across Tasks 3/5/10. `InterpCapability.ARBITRARY_TIMESTEP` consistent across Tasks 2/4/5/9.

---

## Execution order / dependencies

0 → 1 → 2 → 3 (offline helpers, independent after their predecessors) → 4 → 5 (needs 2,3,4) → 6 → 7 (needs 5,6) → 8 (needs 6,7) → 9 (needs 4) → 10 (needs 2,9) → 11 (needs 6,8,10) → 12 (needs 11, live spend, user-gate).
