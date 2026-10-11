# Animated Inputs in `kinoforge upscale --image-dir` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An animated input under `--image-dir` (animated WebP, GIF, APNG, multi-page TIFF) is upscaled frame by frame on the pod the run already holds and written back as an animation in its own container, with the original per-frame durations, loop count and alpha; a plan-time frame cap bounds spend; the latent APNG passthrough is closed.

**Architecture:** The planner (`core/image_dir.py`) reads `n_frames` from the header, refuses files above `upscale.max_animation_frames` as `oversize`, and names animated outputs with the input's suffix. A generator `prepare_frames` decodes one frame at a time to scratch PNGs; the runner (`core/upscale_dir.py`) sends each through the same `UpscaleStage` it already holds and keeps the upscaled PNGs in scratch; a new `core/animation.py` reassembles them with Pillow's `save_all` writers, resizing alpha masks on the controller. One item stays one item in the plan, progress, summary and exit code. Zero pod-side changes.

**Tech Stack:** Python 3.12+, pydantic config, Pillow 12 (conda-forge) + pillow-heif + pillow-avif-plugin, the existing `deploy_session` / `UpscaleStage` / `fetch_artifact_bytes` seams, pytest with Pillow-written fixtures.

**Spec:** `docs/superpowers/specs/2026-10-11-animated-image-dir-upscale-design.md` (addendum to `docs/superpowers/specs/2026-10-06-directory-image-upscale-design.md`).

## Global Constraints

- **Zero pod-side changes.** Nothing under `src/kinoforge/engines/diffusers/servers/`, `src/kinoforge/upscalers/`, or any embedded module is edited. NO launch golden, `_BASELINE_BYTES` entry or `_golden_provision.json` may move; Task 7 asserts it. A moved golden is a defect, not a regeneration.
- **Controller-side imports only.** `core/image_dir.py`, `core/animation.py` and `core/upscale_dir.py` import Pillow lazily inside functions, never at module level. `core/` never imports an adapter namespace (`kinoforge.engines.*`, `kinoforge.providers.*`, `kinoforge.cli.*`); `tests/test_core_invariant.py` enforces it.
- **Whole-tree invariants in EVERY task's Verify:** `pixi run python -m pytest -q tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers` must pass before each commit. The directory plan omitted this and paid for it in `de669716`.
- **Direct file writes** outside an `ArtifactStore` / `OutputSink` go through the existing tagged `_write_atomic` in `core/upscale_dir.py` (carries `# kinoforge:public-write`). No new `Path.write_bytes` / `open(..., "wb")` call sites anywhere in `src/` without that tag and a justification comment. Pillow's `Image.save(path)` is not a tracked write.
- **Exit codes unchanged:** 2 = precondition, 1 = any item failed/aborted/pod dead/batch-fatal, 0 = clean, dry run, nothing-to-do.
- **Nothing partial reaches the mirrored tree.** An animated output is written only after every frame succeeded, via `_write_atomic`.
- **Existing exact-string assertions stay green.** The plan line `found N: pending P, exists E, oversize O, unreadable U; non-image skipped S` and the summary line are extended ONLY when at least one animated item is present (the parenthetical is omitted at zero), so `tests/cli/test_cmd_upscale_image_dir.py` keeps passing without edits to its still-only cases.
- **`ImageHeader.frames` and `ImageDirItem.frames` default to `1`** so every existing positional constructor call in tests stays valid.
- **Local timezone everywhere** (`datetime.now()`), no UTC.
- **Google-style docstrings and full type hints** on every function in `src/` (ruff `D`, `ANN` enforced there; relaxed under `tests/`).
- **Each test states the behaviour under test and the concrete bug it catches** (a `# Bug caught:` comment), per the `test-design` skill. Guard-the-guard: a test that iterates a discovered set asserts a plausible count.
- **Commit after every task** with Conventional Commits; run `pixi run pre-commit run --all-files` first (it ignores untracked files — `git add` new files before trusting it). Never `--no-verify`.
- **Live spend only in Task 8**, only after its RED scaffold is committed and `pixi run preflight` exits 0, with `--no-reuse`, utilisation polled every 60 s by the harness thread (never `est_spend`), frame QA on every output before reporting green, and `pixi run kinoforge list` verified clean afterwards.

**User decisions (already made):**
- "Upscale all frames, capped" — every frame of an animated input is upscaled by default; `upscale.max_animation_frames` (default 300) refuses larger files at plan time.
- "Same container as the input" — WebP→WebP, GIF→GIF, APNG→APNG, TIFF→TIFF, knowing that GIF output is re-quantised to 256 colours.
- "a" — the per-frame loop lives in the runner (approach A), not in N plan items and not on the pod.
- Four design sections approved verbatim ("y" ×4); spec approved with "no changes".

**Planning-time findings (verified in this environment, 2026-10-11):**
- Pillow 12.3.0 (conda-forge) reports `n_frames` and `info["loop"]` for animated WebP, GIF, APNG and multi-page TIFF (`loop` is `None` for TIFF).
- **WebP per-frame `duration` is populated only after `im.load()`** following `im.seek(i)`; `seek` alone leaves it `None`. GIF and APNG populate it on `seek` (APNG as a float). TIFF has no duration. `prepare_frames` therefore calls `load()` after every `seek()`.
- **The APNG writer does NOT accept a generator for `append_images`** (a generator yields a one-frame file). WebP, GIF and TIFF writers do. A list of lazily `Image.open`ed frames works for APNG, so the assembler materialises a list ONLY for PNG output; memory for APNG peaks at all decoded upscaled frames, and the docs say so.
- **Saving RGBA frames straight to GIF drops transparency entirely** (frame 0 becomes `P` with `transparency=None`). The recipe that preserves 1-bit transparency: quantise each RGBA frame to 255 colours, paste palette index 255 where alpha < 128, save with `transparency=255, disposal=2`. Verified: frame 0's transparent pixel reads back `(0, 0, 0, 0)`, opaque frames read back their colour.
- Lossy animated WebP at `quality=90, method=4` preserves alpha (frame 0 alpha 128 reads back 128).
- **Pillow's `n_frames` is NOT a reliable "animated" signal for every container**: a PSD reports its layer count, an ICO its size count. Only `.webp`, `.gif`, `.png`, `.tif`, `.tiff` are consulted for frames; everything else is a still exactly as today.
- The live fixture (`tests/live/build_image_dir_fixture.py`) ALREADY contains a two-frame `sub/anim.gif` (320×200, 200 ms). Under this plan its output becomes `sub/anim.gif` (640×400, 2 frames) instead of `sub/anim.png`. The spec's §7.1 example counts (`pending 8 (2 animated, 7 frames)`) assumed both animated fixtures were new; the real numbers after Task 8 adds one WebP are **`found 9: pending 7 (2 animated, 6 frames), exists 1, oversize 1, unreadable 0; non-image skipped 1`**. This plan's numbers win.

---

## File Structure

| file | responsibility |
|---|---|
| `src/kinoforge/core/config.py` (modify) | `UpscaleConfig.max_animation_frames` + validator |
| `src/kinoforge/core/image_dir.py` (modify) | `ImageHeader.frames`, `ImageDirItem.frames`, `_ANIMATED_SUFFIXES`, cap in `plan_image_dir`, container-keeping names, `Frame`, `prepare_frames`, `read_loop` |
| `src/kinoforge/core/animation.py` (create) | `assemble_animation` — per-container Pillow writers, alpha resize, GIF transparency recipe |
| `src/kinoforge/core/upscale_dir.py` (modify) | `FrameFailed`, `_upscale_animated`, the animated branch, per-item reason text |
| `src/kinoforge/cli/_commands.py` (modify) | cap passed to the planner; plan line, dry-run item tag, progress line, summary parenthetical |
| `docs/configuration.md`, `README.md`, the 2026-10-06 spec, `PROGRESS.md`, `successful-generations.md` | docs |
| `tests/core/test_config_upscale_animation_frames.py` (create) | config cap |
| `tests/core/test_image_dir_plan.py` (modify) | frames, cap, naming, exists |
| `tests/core/test_image_dir_prepare.py` (modify) | `prepare_frames`, `read_loop` |
| `tests/core/test_animation.py` (create) | assembler round trips |
| `tests/core/test_upscale_dir.py` (modify) | runner animated branch |
| `tests/cli/test_cmd_upscale_image_dir.py` (modify) | plan line, progress, summary |
| `tests/live/build_image_dir_fixture.py`, `tests/live/test_image_dir_upscale_smoke.py` (modify) | live fire |

---

### Task 1: `upscale.max_animation_frames`

**Goal:** `UpscaleConfig` gains `max_animation_frames: int = 300`, refused below 1, documented beside `max_output_megapixels`.

**Files:**
- Modify: `src/kinoforge/core/config.py` (the `UpscaleConfig` attributes docstring, the field list after `max_output_megapixels: int = 256`, and a validator after `_validate_megapixels`)
- Modify: `docs/configuration.md` (the `upscale:` table row after `max_output_megapixels`)
- Test: `tests/core/test_config_upscale_animation_frames.py`

**Acceptance Criteria:**
- [ ] A config without the key loads with `max_animation_frames == 300`.
- [ ] `max_animation_frames: 12` round-trips as `12`.
- [ ] `0` and `-3` raise `ConfigError` whose message contains `upscale.max_animation_frames`.
- [ ] `docs/configuration.md` has the row.
- [ ] Whole-tree invariants and `tests/providers` green; no golden moved.

**Verify:** `pixi run python -m pytest -q tests/core/test_config_upscale_animation_frames.py tests/core/test_config_upscale_megapixels.py tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers` → all passed.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_config_upscale_animation_frames.py`:

```python
"""`upscale.max_animation_frames` — the plan-time cap on frames per animated input."""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ConfigError


def _cfg(extra: dict[str, object] | None = None) -> dict[str, object]:
    upscale: dict[str, object] = {
        "engine": "spandrel",
        "scale": "2x",
        "spandrel": {
            "model_url": "hf:foo/bar.pth",
            "arch": "realesrgan",
            "precision": "fp16",
            "tile_size": 512,
            "batch_size": 4,
        },
    }
    upscale.update(extra or {})
    return {
        "engine": {"kind": "diffusers", "precision": "fp8"},
        "models": [
            {
                "kind": "base",
                "ref": "hf:Wan-AI/Wan2.2-T2V",
                "target": "diffusion_models",
            }
        ],
        "compute": {"provider": "fake", "image": "fake:latest"},
        "upscale": upscale,
    }


def test_default_is_300() -> None:
    # Bug caught: no default → every existing config fails to load, or a
    # default of 0 refuses every animation.
    cfg = Config.model_validate(_cfg())
    assert cfg.upscale is not None
    assert cfg.upscale.max_animation_frames == 300


def test_explicit_value_round_trips() -> None:
    # Bug caught: a validator that ignores the configured value.
    cfg = Config.model_validate(_cfg({"max_animation_frames": 12}))
    assert cfg.upscale is not None
    assert cfg.upscale.max_animation_frames == 12


@pytest.mark.parametrize("bad", [0, -3])
def test_below_one_is_refused(bad: int) -> None:
    # Bug caught: a 0 cap silently refuses every animated file as oversize.
    with pytest.raises(ConfigError, match="upscale.max_animation_frames"):
        Config.model_validate(_cfg({"max_animation_frames": bad}))
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest -q tests/core/test_config_upscale_animation_frames.py`
Expected: 4 failures — `AttributeError: 'UpscaleConfig' object has no attribute 'max_animation_frames'` for the first two, and the parametrised pair fail with `DID NOT RAISE` (pydantic ignores the unknown key).

- [ ] **Step 3: Add the field, docstring line and validator**

In `src/kinoforge/core/config.py`, in the `UpscaleConfig` attributes docstring directly after the `max_output_megapixels` paragraph, add:

```
        max_animation_frames: Plan-time cap on frames per animated input
            under ``--image-dir`` (animated WebP, GIF, APNG, multi-page
            TIFF). An animation of F frames costs F stage calls; a file
            above the cap is refused as ``oversize`` before any pod work.
```

After `max_output_megapixels: int = 256` add:

```python
    max_animation_frames: int = 300
```

After the `_validate_megapixels` validator add:

```python
    @model_validator(mode="after")
    def _validate_animation_frames(self) -> Self:
        """Validate max_animation_frames is at least 1."""
        if self.max_animation_frames < 1:
            raise ConfigError(
                "upscale.max_animation_frames must be at least 1, "
                f"got {self.max_animation_frames}"
            )
        return self
```

- [ ] **Step 4: Run to verify it passes**

Run: `pixi run python -m pytest -q tests/core/test_config_upscale_animation_frames.py tests/core/test_config_upscale_megapixels.py`
Expected: 7 passed.

- [ ] **Step 5: Document the field**

In `docs/configuration.md`, directly after the `max_output_megapixels` row of the `upscale:` table, add:

```
| `max_animation_frames` | int | `300` | `--image-dir` only: an animated input (animated WebP, GIF, APNG, multi-page TIFF) with more frames than this is refused as `oversize` at plan time. Every frame is one upscale request, so this is the spend cap for animations; the dry run's `(N animated, F frames)` count is the preview. |
```

- [ ] **Step 6: Whole-tree invariants and golden check**

Run: `pixi run python -m pytest -q tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers`
Expected: all passed.
Run: `pixi run python tools/snapshot_launch_payloads.py && git status --porcelain`
Expected: only `src/kinoforge/core/config.py`, `docs/configuration.md` and the new test file listed; NO golden, baseline or `_golden_provision.json` changed. If a golden moved, STOP — the `upscale:` block is not part of any launch payload, so a moved golden means something else was touched.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/config.py docs/configuration.md tests/core/test_config_upscale_animation_frames.py
pixi run pre-commit run --all-files
git commit -m "feat(config): upscale.max_animation_frames — plan-time cap on frames per animated input"
```

---

### Task 2: Planner — `frames`, the cap, container-keeping names

**Goal:** `read_image_header` reports `frames`; `plan_image_dir` refuses animations above the cap as `oversize`, names animated outputs with the input's suffix, and the collision rule and `exists` check follow.

**Files:**
- Modify: `src/kinoforge/core/image_dir.py` (`ImageHeader`, `ImageDirItem`, a new `_ANIMATED_SUFFIXES` constant, `read_image_header`, `_output_names`, `plan_image_dir`)
- Test: `tests/core/test_image_dir_plan.py` (new class `TestFrames`, extensions to the naming tests)

**Acceptance Criteria:**
- [ ] `read_image_header(...).frames` is `4` for a four-frame WebP, GIF and APNG, `3` for a three-page TIFF, `1` for a still PNG/JPEG/WebP.
- [ ] A `.psd`-style false positive cannot happen: frames are read only for `.webp/.gif/.png/.tif/.tiff`; a multi-frame file with another suffix reports `1`.
- [ ] `plan_image_dir(..., max_animation_frames=3)` marks a 4-frame WebP `oversize` with reason `4 frames exceeds upscale.max_animation_frames=3`; a 3-frame one is `pending` with `frames == 3`.
- [ ] Outputs: `anim.webp`→`anim.webp`, `anim.gif`→`anim.gif`, APNG `anim.png`→`anim.png`, `pages.tif`→`pages.tif`, `pages.tiff`→`pages.tiff`; a still `b.webp`→`b.png` unchanged.
- [ ] Same-folder collision: animated `a.webp` + still `a.png` → `a.webp.webp` and `a.png.png`, both `renamed=True`.
- [ ] A pre-existing `anim.webp` output → `exists`.
- [ ] Every existing test in `tests/core/test_image_dir_plan.py` and `tests/core/test_image_dir_prepare.py` still passes unchanged.
- [ ] Whole-tree invariants and `tests/providers` green.

**Verify:** `pixi run python -m pytest -q tests/core/test_image_dir_plan.py tests/core/test_image_dir_prepare.py tests/core/test_media.py tests/core/test_upscale_dir.py tests/cli/test_cmd_upscale_image_dir.py tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers` → all passed.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_image_dir_plan.py` (the module already imports `Path`, `pytest`, `Image`, `plan_image_dir`, `read_image_header`, and defines `_png` and `_plan`):

```python
def _anim(path: Path, n: int = 4, size: tuple[int, int] = (8, 6), mode: str = "RGBA") -> Path:
    """Write an n-frame animation (or multi-page TIFF) in the container of *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = [Image.new(mode, size, (10 * i, 0, 0, 255)[: len(mode)]) for i in range(n)]
    kw: dict[str, object] = {}
    if path.suffix.lower() in (".webp", ".gif", ".png"):
        kw = {"duration": [100] * n, "loop": 0}
    frames[0].save(path, save_all=True, append_images=frames[1:], **kw)
    return path


class TestFrames:
    @pytest.mark.parametrize(
        ("name", "n"),
        [("a.webp", 4), ("a.gif", 4), ("a.png", 4), ("a.tif", 3), ("a.tiff", 3)],
    )
    def test_header_reports_frames(self, tmp_path: Path, name: str, n: int) -> None:
        # Bug caught: a two-frame APNG reports one frame and passes through
        # byte for byte; a multi-page TIFF is treated as its first page.
        mode = "RGB" if name.endswith(("tif", "tiff")) else "RGBA"
        hdr = read_image_header(_anim(tmp_path / name, n, mode=mode))
        assert hdr.frames == n

    @pytest.mark.parametrize("name", ["s.png", "s.jpg", "s.webp"])
    def test_still_reports_one_frame(self, tmp_path: Path, name: str) -> None:
        # Bug caught: a still is routed through the animated branch.
        assert read_image_header(_png(tmp_path / name)).frames == 1

    def test_frames_only_consulted_for_animated_containers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: a PSD (n_frames == layer count) or ICO (n_frames ==
        # size count) is classified as an animation and fed frame by frame.
        from PIL import Image as _Image

        src = _png(tmp_path / "layers.psd")  # a PNG under a .psd name
        real_open = _Image.open

        class _Multi:
            def __init__(self, im: object) -> None:
                self._im = im
                self.n_frames = 7

            def __getattr__(self, name: str) -> object:
                return getattr(self._im, name)

            def __enter__(self) -> _Multi:
                return self

            def __exit__(self, *a: object) -> None:
                self._im.close()  # type: ignore[attr-defined]

        monkeypatch.setattr(_Image, "open", lambda p, *a, **k: _Multi(real_open(p)))
        assert read_image_header(src).frames == 1

    def test_above_cap_is_oversize_naming_the_cap(self, tmp_path: Path) -> None:
        # Bug caught: a 2,000-frame file enters the pod loop; or the reason
        # does not say which knob to raise.
        src = tmp_path / "photos"
        _anim(src / "big.webp", 4)
        _anim(src / "ok.webp", 3)
        plan = plan_image_dir(src, scale=2, max_output_megapixels=256, max_animation_frames=3)
        by = {i.source.name: i for i in plan.items}
        assert by["big.webp"].disposition == "oversize"
        assert by["big.webp"].reason == "4 frames exceeds upscale.max_animation_frames=3"
        assert by["ok.webp"].disposition == "pending"
        assert by["ok.webp"].frames == 3

    def test_default_cap_is_300(self, tmp_path: Path) -> None:
        # Bug caught: the keyword default drifts from the config default.
        src = tmp_path / "photos"
        _anim(src / "a.gif", 4, mode="RGB")
        plan = plan_image_dir(src, scale=2, max_output_megapixels=256)
        assert plan.items[0].disposition == "pending"

    def test_megapixel_guard_applies_per_frame(self, tmp_path: Path) -> None:
        # Bug caught: the guard multiplies by the frame count and refuses
        # a 4-frame 8x6 animation at 2x as "oversize" on pixels.
        src = tmp_path / "photos"
        _anim(src / "a.webp", 4, size=(100, 100))
        # 100x100 at 2x = 0.04 MP per frame; a cap of 1 MP must pass
        plan = plan_image_dir(src, scale=2, max_output_megapixels=1)
        assert plan.items[0].disposition == "pending"


class TestAnimatedNaming:
    @pytest.mark.parametrize(
        ("name", "out"),
        [
            ("anim.webp", "anim.webp"),
            ("anim.gif", "anim.gif"),
            ("anim.png", "anim.png"),
            ("pages.tif", "pages.tif"),
            ("pages.tiff", "pages.tiff"),
        ],
    )
    def test_animated_keeps_its_container(self, tmp_path: Path, name: str, out: str) -> None:
        # Bug caught: an animated WebP is written as a still <stem>.png.
        src = tmp_path / "photos"
        mode = "RGB" if name.endswith(("tif", "tiff")) else "RGBA"
        _anim(src / name, 3, mode=mode)
        plan = _plan(src)
        assert plan.items[0].output == plan.output_dir / out

    def test_still_webp_still_becomes_png(self, tmp_path: Path) -> None:
        # Bug caught: the suffix rule keys on the container, not on frames.
        src = tmp_path / "photos"
        _png(src / "b.webp")
        assert _plan(src).items[0].output.name == "b.png"

    def test_collision_between_animated_and_still_keeps_full_names(
        self, tmp_path: Path
    ) -> None:
        # Bug caught: different output suffixes exempt the pair from the rule,
        # so a later-added `a.webp` still silently shadows `a.png`'s output.
        src = tmp_path / "photos"
        _anim(src / "a.webp", 3)
        _png(src / "a.png")
        plan = _plan(src)
        names = {i.source.name: (i.output.name, i.renamed) for i in plan.items}
        assert names == {"a.webp": ("a.webp.webp", True), "a.png": ("a.png.png", True)}

    def test_existing_animated_output_is_exists(self, tmp_path: Path) -> None:
        # Bug caught: a finished animation is paid for again on rerun.
        src = tmp_path / "photos"
        _anim(src / "anim.webp", 3)
        out = tmp_path / "photos_upscaled"
        _anim(out / "anim.webp", 3)
        assert _plan(src).items[0].disposition == "exists"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest -q tests/core/test_image_dir_plan.py -k "TestFrames or TestAnimatedNaming"`
Expected: failures — `AttributeError: 'ImageHeader' object has no attribute 'frames'`, `TypeError: plan_image_dir() got an unexpected keyword argument 'max_animation_frames'`, and the naming tests asserting `anim.webp` / `a.webp.webp` fail against `.png` outputs.

- [ ] **Step 3: Implement**

In `src/kinoforge/core/image_dir.py`:

After `_ROTATED_ORIENTATIONS` add:

```python
_ANIMATED_SUFFIXES = frozenset({".webp", ".gif", ".png", ".tif", ".tiff"})
"""Containers whose Pillow ``n_frames`` means frames (or pages) of ONE image.

Deliberately not every container that reports ``n_frames``: a PSD reports
its layer count and an ICO its size count. Anything else is a still.
"""
```

In `ImageHeader`, add after `orientation`:

```python
        frames: Frame count; ``1`` for a still. Read only for
            :data:`_ANIMATED_SUFFIXES`; other containers always report ``1``.
```
and the field `frames: int = 1`.

In `ImageDirItem`, add to the docstring after `renamed`:

```python
        frames: Frame count from the header; ``> 1`` takes the animated path
            and keeps the input container's suffix on ``output``.
```
and the field `frames: int = 1` (after `renamed: bool = False`). Update the `output` line of the docstring to: `output: Absolute output path — ``.png`` for a still, the input's own suffix for an animation.`

In `read_image_header`, inside the `with Image.open(path) as im:` block, before the `return`:

```python
        frames = 1
        if path.suffix.lower() in _ANIMATED_SUFFIXES:
            frames = max(1, int(getattr(im, "n_frames", 1)))
```
and pass `frames=frames` to the `ImageHeader(...)` constructor. Update the docstring's first line to "Read size, mode, format, EXIF orientation and frame count without decoding pixels."

Replace `_output_names` with a version that knows the per-file output suffix:

```python
def _output_suffix(path: Path, frames: int) -> str:
    """``.png`` for a still; the input's own (lower-cased) suffix for an animation."""
    return ".png" if frames <= 1 else path.suffix.lower()


def _output_names(
    paths: list[Path], frames: dict[Path, int]
) -> dict[Path, tuple[str, bool]]:
    """Map each source to ``(output filename, renamed)`` per the §3.2 rule.

    Stems are compared within one folder regardless of suffix: an animated
    ``a.webp`` and a still ``a.png`` collide even though their outputs would
    not share a name — predictability beats a special case.
    """
    by_folder: dict[Path, Counter[str]] = {}
    for p in paths:
        by_folder.setdefault(p.parent, Counter())[p.stem] += 1
    names: dict[Path, tuple[str, bool]] = {}
    for p in paths:
        suffix = _output_suffix(p, frames.get(p, 1))
        if by_folder[p.parent][p.stem] > 1:
            names[p] = (f"{p.name}{suffix}", True)
        else:
            names[p] = (f"{p.stem}{suffix}", False)
    return names
```

Rewrite `plan_image_dir` so headers are read BEFORE names (the name depends on the frame count), the cap sits between `exists` and the megapixel guard, and `frames` reaches the item:

```python
def plan_image_dir(
    source_dir: Path,
    *,
    scale: int,
    max_output_megapixels: int,
    max_animation_frames: int = 300,
) -> ImageDirPlan:
    """Build the plan for *source_dir*.

    Per file, in order: header read (``unreadable`` on failure), existing
    output (``exists``), the frame cap (``oversize``), the megapixel guard
    (``oversize``), else ``pending``.

    Args:
        source_dir: The input directory.
        scale: Integer upscale factor (the guard bounds OUTPUT pixels).
        max_output_megapixels: ``cfg.upscale.max_output_megapixels``.
        max_animation_frames: ``cfg.upscale.max_animation_frames``; an
            animation with more frames is refused before any pod work.

    Returns:
        The plan.

    Raises:
        NotADirectoryError: *source_dir* is not a directory.
        ValueError: *source_dir* is a filesystem root.
    """
    src = Path(source_dir).resolve()
    if not src.is_dir():
        raise NotADirectoryError(str(src))
    out_dir = output_dir_for(src)
    paths, skipped = walk_images(src)
    headers: dict[Path, ImageHeader] = {}
    errors: dict[Path, str] = {}
    for p in paths:
        try:
            headers[p] = read_image_header(p)
        except Exception as exc:  # noqa: BLE001 — any Pillow failure is "unreadable"
            errors[p] = f"{type(exc).__name__}: {exc}"
    names = _output_names(paths, {p: h.frames for p, h in headers.items()})
    renamed_folders: set[Path] = set()
    items: list[ImageDirItem] = []
    for p in paths:
        name, renamed = names[p]
        if renamed and p.parent not in renamed_folders:
            renamed_folders.add(p.parent)
            _log.warning(
                "%s: stem collision; colliding files keep their full name + suffix",
                p.parent,
            )
        output = out_dir / p.parent.relative_to(src) / name
        if p in errors:
            items.append(ImageDirItem(p, output, 0, 0, "unreadable", errors[p], renamed))
            continue
        hdr = headers[p]
        if output.exists():
            disposition: Disposition = "exists"
            reason: str | None = None
        elif hdr.frames > max_animation_frames:
            disposition = "oversize"
            reason = (
                f"{hdr.frames} frames exceeds "
                f"upscale.max_animation_frames={max_animation_frames}"
            )
        elif output_megapixels(hdr.width, hdr.height, scale) > max_output_megapixels:
            disposition = "oversize"
            reason = (
                f"{hdr.width}x{hdr.height} at {scale}x -> "
                f"{output_megapixels(hdr.width, hdr.height, scale):.1f} MP exceeds "
                f"upscale.max_output_megapixels={max_output_megapixels}"
            )
        else:
            disposition, reason = "pending", None
        items.append(
            ImageDirItem(
                p, output, hdr.width, hdr.height, disposition, reason, renamed, hdr.frames
            )
        )
    return ImageDirPlan(src, out_dir, tuple(items), skipped)
```

Also update the `ImageDirItem` docstring `output` line and the §3.2 reference in `_output_names` as shown. The module docstring's first paragraph gains: "An animated input (`frames > 1`) keeps its own container on output and is refused above `max_animation_frames`."

- [ ] **Step 4: Run to verify it passes**

Run: `pixi run python -m pytest -q tests/core/test_image_dir_plan.py tests/core/test_image_dir_prepare.py tests/core/test_media.py`
Expected: all passed, including every pre-existing test (the still naming, header and guard tests must be untouched).

- [ ] **Step 5: Downstream callers still green**

Run: `pixi run python -m pytest -q tests/core/test_upscale_dir.py tests/cli/test_cmd_upscale_image_dir.py tests/cli/test_cmd_upscale_image.py`
Expected: all passed (the CLI still calls `plan_image_dir` without the new keyword; the default keeps it working until Task 6 wires the config value).

- [ ] **Step 6: Whole-tree invariants**

Run: `pixi run python -m pytest -q tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers`
Expected: all passed.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/image_dir.py tests/core/test_image_dir_plan.py
pixi run pre-commit run --all-files
git commit -m "feat(image-dir): planner reads frames, caps animations, keeps the input container on output"
```

---

### Task 3: `prepare_frames` and `read_loop`

**Goal:** A generator decodes an animated input one frame at a time into scratch RGB PNGs with the frame's duration and alpha mask; a helper reads the loop count. The still path (`prepare_upload`) is untouched.

**Files:**
- Modify: `src/kinoforge/core/image_dir.py` (new `Frame` dataclass, `prepare_frames`, `read_loop`, a `_has_alpha` helper; `from collections.abc import Iterator` and `from typing import TYPE_CHECKING, Any` at the top)
- Test: `tests/core/test_image_dir_prepare.py` (new class `TestPrepareFrames`)

**Acceptance Criteria:**
- [ ] A 4-frame RGBA WebP with durations `[100, 200, 100, 300]` yields 4 `Frame`s in order with `duration_ms == [100, 200, 100, 300]`, each `path` an 8-bit RGB PNG under scratch named `<sha8>-f0001.png` … `-f0004.png`, each `alpha` an `L` image of the frame's size whose pixel values equal the source alpha.
- [ ] A 3-frame RGB GIF yields `alpha is None` for every frame and the GIF's durations.
- [ ] A 4-frame APNG yields integer durations (Pillow reports floats) and alpha masks.
- [ ] A 2-page RGB TIFF yields `duration_ms == 0` for both pages and `alpha is None`.
- [ ] An animated WebP whose EXIF orientation is 6 yields transposed frames (a 40×20 source yields 20×40 PNGs).
- [ ] Frames are yielded lazily: after consuming the first frame only, exactly one `-f` file exists in scratch.
- [ ] `read_loop` returns `0` for WebP `loop=0`, `2` for GIF `loop=2`, `3` for APNG `loop=3`, `0` for TIFF.
- [ ] Whole-tree invariants and `tests/providers` green.

**Verify:** `pixi run python -m pytest -q tests/core/test_image_dir_prepare.py tests/core/test_image_dir_plan.py tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers` → all passed.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_image_dir_prepare.py` (the module already has `_rgb`, `_item`, the `scratch` fixture, the autouse `_openers` fixture, `np`, `Image`, `ImageOps`, `hashlib`, `Path`):

```python
def _rgba_frames(n: int = 4, size: tuple[int, int] = (6, 4)) -> list[Image.Image]:
    rng = np.random.default_rng(1)
    out = []
    for i in range(n):
        arr = rng.integers(0, 255, (size[1], size[0], 4), dtype=np.uint8)
        arr[..., 3] = 40 * i + 15  # distinct alpha per frame
        out.append(Image.fromarray(arr, "RGBA"))
    return out


def _save_anim(path: Path, frames: list[Image.Image], **kw: object) -> Path:
    frames[0].save(path, save_all=True, append_images=frames[1:], **kw)
    return path


class TestPrepareFrames:
    def test_webp_frames_in_order_with_durations_and_alpha(
        self, tmp_path: Path, scratch: Path
    ) -> None:
        # Bug caught: WebP durations read as None (Pillow fills them only
        # after load()), frames yielded out of order, or alpha dropped.
        from kinoforge.core.image_dir import prepare_frames

        frames = _rgba_frames()
        src = _save_anim(tmp_path / "a.webp", frames, duration=[100, 200, 100, 300], loop=0, lossless=True)
        got = list(prepare_frames(_item(src), scratch))
        assert [f.index for f in got] == [0, 1, 2, 3]
        assert [f.duration_ms for f in got] == [100, 200, 100, 300]
        sha8 = hashlib.sha256(src.read_bytes()).hexdigest()[:8]
        assert [f.path.name for f in got] == [f"{sha8}-f{i:04d}.png" for i in (1, 2, 3, 4)]
        for f, orig in zip(got, frames, strict=True):
            with Image.open(f.path) as im:
                assert im.format == "PNG" and im.mode == "RGB" and im.size == (6, 4)
                assert np.array_equal(np.asarray(im), np.asarray(orig.convert("RGB")))
            assert f.alpha is not None and f.alpha.mode == "L" and f.alpha.size == (6, 4)
            assert np.array_equal(np.asarray(f.alpha), np.asarray(orig.getchannel("A")))

    def test_gif_without_alpha_yields_none_masks(self, tmp_path: Path, scratch: Path) -> None:
        # Bug caught: an opaque GIF gets a synthetic all-255 mask and the
        # assembler takes the transparency path for nothing.
        from kinoforge.core.image_dir import prepare_frames

        frames = [f.convert("RGB") for f in _rgba_frames(3)]
        src = _save_anim(tmp_path / "a.gif", frames, duration=[50, 60, 70], loop=0)
        got = list(prepare_frames(_item(src), scratch))
        assert [f.alpha for f in got] == [None, None, None]
        assert [f.duration_ms for f in got] == [50, 60, 70]

    def test_apng_durations_are_ints(self, tmp_path: Path, scratch: Path) -> None:
        # Bug caught: Pillow reports APNG durations as floats; a float
        # leaks into the WebP writer's duration list and raises.
        from kinoforge.core.image_dir import prepare_frames

        src = _save_anim(tmp_path / "a.png", _rgba_frames(), duration=[100, 200, 100, 300], loop=3)
        got = list(prepare_frames(_item(src), scratch))
        assert [f.duration_ms for f in got] == [100, 200, 100, 300]
        assert all(isinstance(f.duration_ms, int) for f in got)
        assert all(f.alpha is not None for f in got)

    def test_tiff_pages_have_no_duration(self, tmp_path: Path, scratch: Path) -> None:
        # Bug caught: a missing duration becomes None and the writer raises.
        from kinoforge.core.image_dir import prepare_frames

        frames = [f.convert("RGB") for f in _rgba_frames(2)]
        src = _save_anim(tmp_path / "p.tif", frames)
        got = list(prepare_frames(_item(src), scratch))
        assert [f.duration_ms for f in got] == [0, 0]
        assert [f.alpha for f in got] == [None, None]

    def test_exif_orientation_applies_to_every_frame(
        self, tmp_path: Path, scratch: Path
    ) -> None:
        # Bug caught: frame 0 is transposed and the rest lie on their side.
        from kinoforge.core.image_dir import prepare_frames

        frames = [Image.new("RGB", (40, 20), (i * 60, 0, 0)) for i in range(3)]
        exif = frames[0].getexif()
        exif[0x0112] = 6
        src = _save_anim(tmp_path / "o.webp", frames, duration=[100] * 3, loop=0, exif=exif)
        sizes = []
        for f in prepare_frames(_item(src), scratch):
            with Image.open(f.path) as im:
                sizes.append(im.size)
        assert sizes == [(20, 40)] * 3

    def test_frames_are_yielded_lazily(self, tmp_path: Path, scratch: Path) -> None:
        # Bug caught: all frames decoded to scratch up front — the
        # one-decoded-frame-at-a-time memory/disk promise is broken.
        from kinoforge.core.image_dir import prepare_frames

        src = _save_anim(tmp_path / "a.webp", _rgba_frames(4), duration=[100] * 4, loop=0)
        gen = prepare_frames(_item(src), scratch)
        first = next(gen)
        assert first.path.exists()
        assert len(list(scratch.glob("*-f*.png"))) == 1
        gen.close()

    @pytest.mark.parametrize(
        ("name", "kw", "expected"),
        [
            ("l.webp", {"duration": [100] * 2, "loop": 0}, 0),
            ("l.gif", {"duration": [100] * 2, "loop": 2}, 2),
            ("l.png", {"duration": [100] * 2, "loop": 3}, 3),
            ("l.tif", {}, 0),
        ],
    )
    def test_read_loop(self, tmp_path: Path, name: str, kw: dict[str, object], expected: int) -> None:
        # Bug caught: loop count dropped (a looping sticker plays once).
        from kinoforge.core.image_dir import read_loop

        frames = _rgba_frames(2)
        if name.endswith("tif"):
            frames = [f.convert("RGB") for f in frames]
        assert read_loop(_save_anim(tmp_path / name, frames, **kw)) == expected
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest -q tests/core/test_image_dir_prepare.py -k TestPrepareFrames`
Expected: every test fails with `ImportError: cannot import name 'prepare_frames'` / `'read_loop'`.

- [ ] **Step 3: Implement**

In `src/kinoforge/core/image_dir.py`, change the imports at the top to:

```python
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from kinoforge.core.media import DIR_IMAGE_SUFFIXES

if TYPE_CHECKING:
    from PIL import Image as _PILImage
```

After `prepare_upload` add:

```python
@dataclass(frozen=True)
class Frame:
    """One decoded frame of an animated input, ready to upload.

    Attributes:
        index: 0-based frame index.
        path: 8-bit RGB PNG under the run's scratch directory
            (``<sha8>-f0001.png`` …); the caller unlinks it after upload.
        duration_ms: The frame's own duration; ``0`` when the container has
            no timing (multi-page TIFF).
        alpha: The frame's alpha channel as an 8-bit ``L`` image, or
            ``None`` when the frame has none.
    """

    index: int
    path: Path
    duration_ms: int
    alpha: _PILImage.Image | None


def _has_alpha(im: Any) -> bool:
    """True when the current frame carries alpha or palette transparency."""
    return "A" in im.getbands() or "transparency" in im.info


def read_loop(path: Path) -> int:
    """Return the animation's loop count (``0`` = forever; ``0`` when absent).

    Args:
        path: An animated input.

    Returns:
        Pillow's ``info["loop"]`` as an int, ``0`` when the container has none.
    """
    from PIL import Image

    register_openers()
    with Image.open(path) as im:
        return int(im.info.get("loop") or 0)


def prepare_frames(item: ImageDirItem, scratch: Path) -> Iterator[Frame]:
    """Yield every frame of an animated *item* as an RGB PNG under *scratch*.

    One frame is decoded at a time: the generator writes frame *i* only when
    the consumer asks for it, so scratch holds at most one decoded frame plus
    whatever the consumer has not yet unlinked. Each frame is EXIF-transposed
    like :func:`prepare_upload`. WebP fills a frame's ``duration`` only on
    ``load()``, so every frame is loaded after ``seek``.

    Args:
        item: A planned animated item (``frames > 1``).
        scratch: Per-run scratch directory; the consumer unlinks each path.

    Yields:
        :class:`Frame` in frame order.
    """
    from PIL import Image, ImageOps

    register_openers()
    tag = _sha8(item.source)
    with Image.open(item.source) as im:
        n = max(1, int(getattr(im, "n_frames", 1)))
        for i in range(n):
            im.seek(i)
            im.load()
            duration = int(round(float(im.info.get("duration") or 0)))
            alpha: _PILImage.Image | None = None
            rgba = im.convert("RGBA")
            transposed = ImageOps.exif_transpose(rgba)
            frame = transposed if transposed is not None else rgba
            if _has_alpha(im):
                alpha = frame.getchannel("A")
            path = scratch / f"{tag}-f{i + 1:04d}.png"
            frame.convert("RGB").save(path, format="PNG")
            yield Frame(i, path, duration, alpha)
```

Note on the EXIF transpose: `exif_transpose` reads the orientation from the image's `info["exif"]`, which `convert` copies onto the RGBA copy, so each frame is transposed by the file-level orientation. If the orientation test in Step 1 fails because a container does not carry `exif` through `convert`, read `orientation` once from `im.getexif()` before the loop and apply `ImageOps.exif_transpose` on the ORIGINAL `im` (returns a transposed copy of the current frame) before `convert("RGBA")`; keep whichever variant passes the test.

- [ ] **Step 4: Run to verify it passes**

Run: `pixi run python -m pytest -q tests/core/test_image_dir_prepare.py`
Expected: all passed (the existing still tests included).

- [ ] **Step 5: Whole-tree invariants**

Run: `pixi run python -m pytest -q tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers`
Expected: all passed. (`Image.save(path)` is not a tracked write; there must be no `write_bytes` in the new code.)

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/image_dir.py tests/core/test_image_dir_prepare.py
pixi run pre-commit run --all-files
git commit -m "feat(image-dir): prepare_frames yields one RGB PNG per frame with duration and alpha; read_loop"
```

---

### Task 4: `core/animation.py` — the assembler

**Goal:** `assemble_animation` encodes upscaled frame PNGs into an animated WebP, GIF, APNG or multi-page TIFF (chosen by output suffix) with durations, loop and controller-resized alpha, returning the encoded bytes.

**Files:**
- Create: `src/kinoforge/core/animation.py`
- Test: `tests/core/test_animation.py`

**Acceptance Criteria:**
- [ ] WebP: 4 frames, durations `[100, 200, 100, 300]`, loop 0, alpha masks → read back `n_frames == 4`, durations (after `load()`) equal, loop 0, frame 0 alpha at (0,0) within ±8 of the mask, frame 3 RGB at (0,0) within ±12 of the source (lossy).
- [ ] WebP without alpha → read-back frames are opaque RGB-equivalent (alpha 255) and pixel-exact RGB within ±12.
- [ ] GIF with alpha: frame 0's masked pixel reads back `(0, 0, 0, 0)`; an opaque frame reads back its colour within ±16 (quantised); durations and loop preserved; `n_frames == 4`.
- [ ] GIF without alpha: no `transparency` key on frame 0; durations preserved.
- [ ] APNG: lossless — frames pixel-exact, alpha exact, durations and loop preserved.
- [ ] TIFF: `n_frames == 2`, pixel-exact RGB.
- [ ] A mask of a different size than the frame is resized to the frame's size (a 4×4 mask on an 8×8 frame yields an 8×8 alpha).
- [ ] `assemble_animation` with an empty frame list raises `ValueError("no frames")`; an unknown suffix (`.jpg`) raises `ValueError` naming the suffix.
- [ ] WebP, GIF and TIFF writers receive a generator (memory bound to one frame); only the PNG writer materialises a list — asserted by a test that counts simultaneously-open frames via a monkeypatched `Image.open`.
- [ ] Whole-tree invariants and `tests/providers` green; `core/animation.py` imports nothing from an adapter namespace.

**Verify:** `pixi run python -m pytest -q tests/core/test_animation.py tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers` → all passed.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_animation.py`:

```python
"""`core/animation.py`: reassemble upscaled frames into the input's container."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from kinoforge.core.animation import assemble_animation

DUR = [100, 200, 100, 300]


def _frames(n: int = 4, size: tuple[int, int] = (8, 8)) -> list[Image.Image]:
    rng = np.random.default_rng(2)
    return [
        Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8))
        for _ in range(n)
    ]


def _masks(n: int = 4, size: tuple[int, int] = (8, 8)) -> list[Image.Image]:
    out = []
    for i in range(n):
        m = Image.new("L", size, 255)
        if i == 0:
            m.putpixel((0, 0), 0)  # one transparent pixel on frame 0
        out.append(m)
    return out


def _write(tmp: Path, frames: list[Image.Image]) -> list[Path]:
    paths = []
    for i, f in enumerate(frames):
        p = tmp / f"u{i:04d}.png"
        f.save(p)
        paths.append(p)
    return paths


def _read(body: bytes) -> tuple[int, list[int | None], int | None, list[Image.Image]]:
    with Image.open(io.BytesIO(body)) as im:
        n = getattr(im, "n_frames", 1)
        durs, pix = [], []
        for i in range(n):
            im.seek(i)
            im.load()
            d = im.info.get("duration")
            durs.append(int(d) if d is not None else None)
            pix.append(im.convert("RGBA").copy())
        return n, durs, im.info.get("loop"), pix


def _close(a: tuple[int, ...], b: tuple[int, ...], tol: int) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


class TestWebP:
    def test_round_trip_with_alpha(self, tmp_path: Path) -> None:
        # Bug caught: durations written as one value, loop dropped, alpha
        # lost on the lossy path, frames reordered.
        frames, masks = _frames(), _masks()
        body = assemble_animation(
            _write(tmp_path, frames), durations_ms=DUR, loop=0, alphas=masks,
            output_suffix=".webp",
        )
        n, durs, loop, pix = _read(body)
        assert n == 4 and durs == DUR and loop == 0
        assert pix[0].getpixel((0, 0))[3] <= 8  # masked pixel transparent
        assert pix[1].getpixel((0, 0))[3] >= 247
        assert _close(pix[3].getpixel((0, 0))[:3], frames[3].getpixel((0, 0)), 12)

    def test_round_trip_without_alpha(self, tmp_path: Path) -> None:
        # Bug caught: a None mask crashes the writer or yields alpha 0.
        frames = _frames()
        body = assemble_animation(
            _write(tmp_path, frames), durations_ms=DUR, loop=0, alphas=[None] * 4,
            output_suffix=".webp",
        )
        n, _durs, _loop, pix = _read(body)
        assert n == 4
        assert all(p.getpixel((3, 3))[3] == 255 for p in pix)


class TestGif:
    def test_alpha_becomes_one_bit_transparency(self, tmp_path: Path) -> None:
        # Bug caught: RGBA frames saved straight to GIF lose transparency
        # entirely (verified on Pillow 12.3: frame 0 transparency=None).
        frames, masks = _frames(), _masks()
        body = assemble_animation(
            _write(tmp_path, frames), durations_ms=DUR, loop=2, alphas=masks,
            output_suffix=".gif",
        )
        n, durs, loop, pix = _read(body)
        assert n == 4 and durs == DUR and loop == 2
        assert pix[0].getpixel((0, 0)) == (0, 0, 0, 0)
        assert pix[0].getpixel((5, 5))[3] == 255
        assert _close(pix[2].getpixel((2, 2))[:3], frames[2].getpixel((2, 2)), 16)

    def test_opaque_gif_has_no_transparency_index(self, tmp_path: Path) -> None:
        # Bug caught: index 255 reserved on an opaque animation steals a
        # colour and marks a real pixel transparent.
        frames = _frames()
        body = assemble_animation(
            _write(tmp_path, frames), durations_ms=DUR, loop=0, alphas=[None] * 4,
            output_suffix=".gif",
        )
        with Image.open(io.BytesIO(body)) as im:
            assert "transparency" not in im.info
            assert im.n_frames == 4


class TestApng:
    def test_lossless_round_trip(self, tmp_path: Path) -> None:
        # Bug caught: a generator handed to the PNG writer yields ONE frame
        # (verified on Pillow 12.3); durations/loop dropped.
        frames, masks = _frames(), _masks()
        body = assemble_animation(
            _write(tmp_path, frames), durations_ms=DUR, loop=3, alphas=masks,
            output_suffix=".png",
        )
        n, durs, loop, pix = _read(body)
        assert n == 4 and durs == DUR and loop == 3
        for got, f, m in zip(pix, frames, masks, strict=True):
            assert np.array_equal(np.asarray(got)[..., :3], np.asarray(f))
            assert np.array_equal(np.asarray(got)[..., 3], np.asarray(m))


class TestTiff:
    def test_multi_page(self, tmp_path: Path) -> None:
        # Bug caught: only the first page written.
        frames = _frames(2)
        body = assemble_animation(
            _write(tmp_path, frames), durations_ms=[0, 0], loop=0, alphas=[None, None],
            output_suffix=".tif",
        )
        n, _d, _l, pix = _read(body)
        assert n == 2
        assert np.array_equal(np.asarray(pix[1])[..., :3], np.asarray(frames[1]))


class TestMaskResize:
    def test_small_mask_is_resized_to_the_frame(self, tmp_path: Path) -> None:
        # Bug caught: putalpha with a source-sized mask on an upscaled
        # frame raises or crops.
        frames = _frames(2, (8, 8))
        small = [Image.new("L", (4, 4), 255) for _ in range(2)]
        small[0].putpixel((0, 0), 0)
        body = assemble_animation(
            _write(tmp_path, frames), durations_ms=[100, 100], loop=0, alphas=small,
            output_suffix=".png",
        )
        _n, _d, _l, pix = _read(body)
        assert pix[0].size == (8, 8)
        assert pix[0].getpixel((0, 0))[3] < 128 and pix[0].getpixel((7, 7))[3] == 255


class TestErrors:
    def test_no_frames(self) -> None:
        # Bug caught: an empty list writes a zero-frame file or crashes deep in Pillow.
        with pytest.raises(ValueError, match="no frames"):
            assemble_animation([], durations_ms=[], loop=0, alphas=[], output_suffix=".webp")

    def test_unknown_suffix(self, tmp_path: Path) -> None:
        # Bug caught: a `.jpg` output silently becomes a single JPEG.
        paths = _write(tmp_path, _frames(2))
        with pytest.raises(ValueError, match=r"\.jpg"):
            assemble_animation(paths, durations_ms=[1, 1], loop=0, alphas=[None, None], output_suffix=".jpg")


class TestLaziness:
    @pytest.mark.parametrize("suffix", [".webp", ".gif", ".tif"])
    def test_streaming_writers_hold_one_frame(
        self, tmp_path: Path, suffix: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bug caught: the assembler materialises every upscaled frame for a
        # writer that would have streamed them (300 frames × 2048² in RAM).
        import kinoforge.core.animation as mod

        paths = _write(tmp_path, _frames(4))
        real_open = Image.open
        live: list[int] = [0]
        peak: list[int] = [0]

        class _Tracked:
            def __init__(self, p: Path) -> None:
                self._im = real_open(p)
                live[0] += 1
                peak[0] = max(peak[0], live[0])

            def __enter__(self) -> Image.Image:
                return self._im

            def __exit__(self, *a: object) -> None:
                self._im.close()
                live[0] -= 1

        monkeypatch.setattr(mod, "_open_frame", lambda p: _Tracked(p))
        assemble_animation(paths, durations_ms=[1] * 4, loop=0, alphas=[None] * 4, output_suffix=suffix)
        assert peak[0] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest -q tests/core/test_animation.py`
Expected: `ModuleNotFoundError: No module named 'kinoforge.core.animation'`.

- [ ] **Step 3: Implement**

Create `src/kinoforge/core/animation.py`:

```python
"""Reassemble upscaled frames into an animation in the input's own container.

Controller-side only; Pillow is imported lazily inside functions. The pod
returns one RGB PNG per frame; this module puts the frames back together
with the durations, loop count and alpha the planner's converter read from
the source. Alpha never visits the pod (it returns RGB), so each mask is
resized on the controller with Lanczos and reattached — cheap, keeps
stickers transparent, and the mask's edges are interpolated rather than
model-upscaled.

Per-container writers, all Pillow ``save_all``:

* WebP — lossy at :data:`WEBP_QUALITY`, durations + loop, alpha kept.
* GIF — Pillow quantises each frame to a palette; alpha becomes 1-bit
  transparency via a reserved palette index (saving RGBA straight to GIF
  drops transparency entirely on Pillow 12).
* APNG — lossless, durations + loop, alpha kept. The PNG writer does NOT
  accept a generator for ``append_images`` (it yields a one-frame file), so
  this is the one container whose frames are materialised in memory.
* TIFF — multi-page, lossless, no timing.
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from PIL import Image as _PILImage

WEBP_QUALITY = 90
"""Lossy animated-WebP quality; a knob waits for someone to need it."""

_GIF_TRANSPARENT_INDEX = 255
"""Palette index reserved for transparent pixels when any frame has alpha."""

_WRITERS: dict[str, str] = {
    ".webp": "WEBP",
    ".gif": "GIF",
    ".png": "PNG",
    ".tif": "TIFF",
    ".tiff": "TIFF",
}


def _open_frame(path: Path) -> Any:
    """Open one upscaled frame (a seam the laziness test replaces)."""
    from PIL import Image

    return Image.open(path)


def _composed(
    paths: Sequence[Path], alphas: Sequence[_PILImage.Image | None]
) -> Iterator[_PILImage.Image]:
    """Yield each upscaled frame with its (resized) alpha reattached.

    Frames are opened one at a time and copied out of the file so the file
    handle closes before the next frame opens.
    """
    from PIL import Image

    for path, alpha in zip(paths, alphas, strict=True):
        with _open_frame(path) as im:
            frame = im.convert("RGB")
        if alpha is not None:
            mask = alpha
            if mask.size != frame.size:
                mask = mask.resize(frame.size, Image.Resampling.LANCZOS)
            frame.putalpha(mask)
        yield frame


def _gif_frame(frame: _PILImage.Image) -> _PILImage.Image:
    """Quantise one frame for GIF, reserving index 255 for alpha < 128."""
    from PIL import Image

    rgba = frame.convert("RGBA")
    pal = rgba.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    mask = rgba.getchannel("A").point(lambda a: 255 if a < 128 else 0)
    pal.paste(_GIF_TRANSPARENT_INDEX, mask=mask)
    return pal


def assemble_animation(
    frame_paths: Sequence[Path],
    *,
    durations_ms: Sequence[int],
    loop: int,
    alphas: Sequence[_PILImage.Image | None],
    output_suffix: str,
) -> bytes:
    """Encode upscaled frames as an animation in the container of *output_suffix*.

    Args:
        frame_paths: Upscaled RGB PNGs in frame order.
        durations_ms: One duration per frame (ignored for TIFF).
        loop: Loop count, ``0`` = forever.
        alphas: One ``L`` mask per frame (any size; resized to the frame), or
            ``None`` for an opaque frame.
        output_suffix: ``.webp`` / ``.gif`` / ``.png`` / ``.tif`` / ``.tiff``.

    Returns:
        The encoded file bytes; the caller writes them atomically.

    Raises:
        ValueError: No frames, mismatched sequence lengths, or a suffix with
            no animated writer.
    """
    if not frame_paths:
        raise ValueError("no frames to assemble")
    if not (len(frame_paths) == len(durations_ms) == len(alphas)):
        raise ValueError(
            f"frames/durations/alphas length mismatch: "
            f"{len(frame_paths)}/{len(durations_ms)}/{len(alphas)}"
        )
    fmt = _WRITERS.get(output_suffix.lower())
    if fmt is None:
        raise ValueError(f"no animated writer for suffix {output_suffix!r}")

    frames = _composed(frame_paths, alphas)
    durations = [int(d) for d in durations_ms]
    buf = io.BytesIO()
    if fmt == "WEBP":
        first = next(frames)
        first.save(
            buf,
            format="WEBP",
            save_all=True,
            append_images=frames,
            duration=durations,
            loop=loop,
            lossless=False,
            quality=WEBP_QUALITY,
            method=4,
        )
    elif fmt == "GIF":
        has_alpha = any(a is not None for a in alphas)
        if has_alpha:
            frames = (_gif_frame(f) for f in frames)
        first = next(frames)
        extra: dict[str, Any] = {"transparency": _GIF_TRANSPARENT_INDEX} if has_alpha else {}
        first.save(
            buf,
            format="GIF",
            save_all=True,
            append_images=frames,
            duration=durations,
            loop=loop,
            disposal=2,
            **extra,
        )
    elif fmt == "PNG":
        materialised = list(frames)  # the PNG writer cannot stream a generator
        materialised[0].save(
            buf,
            format="PNG",
            save_all=True,
            append_images=materialised[1:],
            duration=durations,
            loop=loop,
        )
    else:  # TIFF
        first = next(frames)
        first.save(buf, format="TIFF", save_all=True, append_images=frames)
    return buf.getvalue()
```

- [ ] **Step 4: Run to verify it passes**

Run: `pixi run python -m pytest -q tests/core/test_animation.py`
Expected: all passed. If `TestGif::test_alpha_becomes_one_bit_transparency` fails on the opaque-pixel tolerance, widen `_close`'s tolerance for the GIF case to 24 (quantisation on random noise), never loosen the transparent-pixel assertion.

- [ ] **Step 5: Whole-tree invariants**

Run: `pixi run python -m pytest -q tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers`
Expected: all passed (`io.BytesIO` is not a path write).

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/animation.py tests/core/test_animation.py
pixi run pre-commit run --all-files
git commit -m "feat(animation): assemble upscaled frames into WebP/GIF/APNG/TIFF with durations, loop and resized alpha"
```

---

### Task 5: Runner — the animated branch

**Goal:** `upscale_image_dir` sends every frame of an animated item through the stage, assembles the result, writes it atomically, and turns a frame failure into an item failure that names the frame, with nothing partial on disk and scratch drained.

**Files:**
- Modify: `src/kinoforge/core/upscale_dir.py` (new `FrameFailed` exception, `_upscale_animated` helper, the per-item branch, the reason text, imports of `Frame`, `prepare_frames`, `read_loop`)
- Test: `tests/core/test_upscale_dir.py` (new tests at the end)

**Acceptance Criteria:**
- [ ] A 3-frame WebP item produces exactly 3 stage calls (the fake upscaler's `calls`), one output `anim.webp` with 3 frames, and the item is reported `written` once.
- [ ] After the run the scratch directory holds no `-f*.png` or `-u*.png` files for the item (asserted via a spy on `prepare_frames` capturing the scratch path, checked before the `TemporaryDirectory` exits — same pattern as `test_scratch_cleared_after_each_item`).
- [ ] A fake that fails on its 2nd call: item `failed`, reason starts with `frame 2/3: RuntimeError: boom on call 2`, `anim.webp` does not exist, no `.part` file exists, the health probe ran once, and the NEXT still item is still `written`.
- [ ] A fake that raises `Cancelled` on frame 2: the item and every later item are `aborted` and `Cancelled` propagates (not wrapped in `FrameFailed`).
- [ ] A still item's behaviour and the stage's `media == "image"` request are unchanged (existing tests stay green).
- [ ] `_log.info` emits `anim.webp: frame 2/3` (caplog).
- [ ] Whole-tree invariants and `tests/providers` green.

**Verify:** `pixi run python -m pytest -q tests/core/test_upscale_dir.py tests/core/test_image_dir_prepare.py tests/core/test_animation.py tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers` → all passed.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_upscale_dir.py` (it already defines `_Upscaler`, `session`, `_src`, `_run`, imports `Image`, `Path`, `plan_image_dir`, `upscale_image_dir`, `Cancelled`, `MagicMock`, `pytest`, `Any`):

```python
def _anim_src(tmp_path: Path, extra_stills: tuple[str, ...] = ()) -> Path:
    """`photos/anim.webp` (3 RGBA frames, mixed durations) plus optional stills."""
    src = tmp_path / "photos"
    src.mkdir(parents=True, exist_ok=True)
    frames = [Image.new("RGBA", (4, 4), (60 * i, 0, 0, 255)) for i in range(3)]
    frames[0].save(
        src / "anim.webp", save_all=True, append_images=frames[1:],
        duration=[100, 200, 300], loop=0,
    )
    for n in extra_stills:
        Image.new("RGB", (4, 4), 0).save(src / n)
    return src


class _FrameUpscaler(_Upscaler):
    """Returns a real 8x8 RGB PNG per call so the assembler can read it."""

    def upscale(self, instance: Any, job: Any, cfg: Any, *, cancel_token: Any = None) -> Any:
        self.calls.append(job)
        n = len(self.calls)
        if n in self.fail_on:
            raise self.raise_cls(f"boom on call {n}")
        out = self.tmp / f"result{n}.png"
        Image.new("RGB", (8, 8), (n * 30, 0, 0)).save(out)
        from kinoforge.core.interfaces import Artifact, UpscaleResult

        return UpscaleResult(
            artifact=Artifact(uri=f"file://{out}", meta={"media": "image"}),
            input_resolution=(4, 4),
            output_resolution=(8, 8),
            elapsed_s=0.0,
        )


def test_animated_item_runs_one_call_per_frame_and_writes_one_animation(
    tmp_path: Path, session: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    # Bug caught: the runner uploads the source file once (frame 0 only), or
    # writes one PNG per frame into the mirrored tree.
    import logging

    caplog.set_level(logging.INFO, logger="kinoforge.core.upscale_dir")
    src = _anim_src(tmp_path)
    up = _FrameUpscaler(tmp_path)
    _result, seen = _run(tmp_path, src, up)
    assert len(up.calls) == 3
    assert seen == [("anim.webp", "written", None)]
    out = tmp_path / "photos_upscaled" / "anim.webp"
    assert out.exists() and not list((tmp_path / "photos_upscaled").glob("*.png"))
    with Image.open(out) as im:
        assert im.n_frames == 3
        im.seek(2)
        im.load()
        assert im.info["duration"] == 300
        assert im.size == (8, 8)
    assert "anim.webp: frame 2/3" in caplog.text


def test_animated_scratch_is_drained_before_the_next_item(
    tmp_path: Path, session: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: decoded or upscaled frame files accumulate across items
    # (300 frames × N items of controller /tmp).
    from kinoforge.core import upscale_dir as mod

    src = _anim_src(tmp_path, ("z.png",))
    real = mod.prepare_frames
    scratch_seen: list[Path] = []

    def spy(item: Any, scratch: Path) -> Any:
        scratch_seen.append(scratch)
        return real(item, scratch)

    monkeypatch.setattr(mod, "prepare_frames", spy)
    leftovers: list[list[str]] = []

    def on_item(item: Any, outcome: str, reason: str | None) -> None:
        if scratch_seen:
            leftovers.append(sorted(p.name for p in scratch_seen[0].iterdir()))

    plan = plan_image_dir(src, scale=2, max_output_megapixels=256)
    upscale_image_dir(
        _cfg(), plan, store=MagicMock(), run_id="r", state_dir=tmp_path / ".kf",
        upscaler=_FrameUpscaler(tmp_path),  # type: ignore[arg-type]
        on_item=on_item, health_probe=lambda url, cancel_token: {"ok": True},
    )
    assert leftovers and all(names == [] for names in leftovers)


def test_frame_failure_fails_the_item_names_the_frame_and_continues(
    tmp_path: Path, session: dict[str, Any]
) -> None:
    # Bug caught: a partial animation lands in the tree; the reason hides
    # which frame died; one bad animation ends the run.
    src = _anim_src(tmp_path, ("z.png",))
    probes: list[str] = []

    def probe(url: str, cancel_token: Any) -> dict[str, bool]:
        probes.append(url)
        return {"ok": True}

    up = _FrameUpscaler(tmp_path, fail_on={2})
    _result, seen = _run(tmp_path, src, up, probe=probe)
    assert seen[0][:2] == ("anim.webp", "failed")
    assert seen[0][2] is not None and seen[0][2].startswith(
        "frame 2/3: RuntimeError: boom on call 2"
    )
    assert seen[1] == ("z.png", "written", None)
    out_dir = tmp_path / "photos_upscaled"
    assert not (out_dir / "anim.webp").exists()
    assert not list(out_dir.glob("*.part"))
    assert len(probes) == 1
    assert len(up.calls) == 3  # 2 for anim (second failed), 1 for z.png


def test_fatal_on_a_frame_aborts_the_rest_unwrapped(
    tmp_path: Path, session: dict[str, Any]
) -> None:
    # Bug caught: Cancelled wrapped into FrameFailed becomes a per-item
    # failure and the run keeps spending.
    src = _anim_src(tmp_path, ("z.png",))
    up = _FrameUpscaler(tmp_path, fail_on={2}, raise_cls=Cancelled)
    with pytest.raises(Cancelled):
        _run(tmp_path, src, up)
    assert len(up.calls) == 2
    assert not (tmp_path / "photos_upscaled" / "anim.webp").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest -q tests/core/test_upscale_dir.py -k "animated or frame_failure or fatal_on_a_frame"`
Expected: the first test fails with `len(up.calls) == 1` (the runner still uploads the source once via `prepare_upload`); the scratch test fails with `AttributeError: module ... has no attribute 'prepare_frames'`.

- [ ] **Step 3: Implement**

In `src/kinoforge/core/upscale_dir.py`:

Change the image_dir import to:

```python
from kinoforge.core.image_dir import (
    ImageDirItem,
    ImageDirPlan,
    prepare_frames,
    prepare_upload,
    read_loop,
)
```

After `class PodDead(KinoforgeError)` add:

```python
class FrameFailed(KinoforgeError):
    """One frame of an animated item failed; the item fails as a whole.

    ``str()`` is the per-item reason: ``frame 37/100: RuntimeError: ...``.
    Never raised for :data:`_FATAL` exceptions, which propagate unwrapped.
    """

    def __init__(self, index: int, total: int, cause: BaseException) -> None:
        self.index = index
        self.total = total
        self.cause = cause
        super().__init__(f"frame {index}/{total}: {type(cause).__name__}: {cause}")
```

After `_write_atomic` add:

```python
def _upscale_animated(
    item: ImageDirItem, stage: Any, scratch: Path, fetch: Callable[[Any], bytes]
) -> bytes:
    """Upscale every frame of *item* through *stage* and return the encoded animation.

    Decoded frames are unlinked right after their upload; upscaled frames
    are unlinked once the animation is encoded (or on failure). A frame
    failure raises :class:`FrameFailed`; :data:`_FATAL` exceptions pass
    through unwrapped.

    Args:
        item: A planned animated item (``frames > 1``).
        stage: The run's ``UpscaleStage``.
        scratch: The run's scratch directory.
        fetch: ``fetch_artifact_bytes`` (injected so the orchestrator import
            stays lazy in the caller).

    Returns:
        The encoded animation bytes for :func:`_write_atomic`.
    """
    from kinoforge.core.animation import assemble_animation

    upscaled: list[Path] = []
    durations: list[int] = []
    alphas: list[Any] = []
    try:
        for frame in prepare_frames(item, scratch):
            _log.info("%s: frame %d/%d", item.source.name, frame.index + 1, item.frames)
            try:
                state = PipelineState(
                    request=GenerationRequest(prompt="", mode="upscale"),
                    artifacts={"clip": local_artifact(frame.path, "image")},
                )
                state = stage.run(state)
                body = fetch(state.artifacts["upscaled"])
            except _FATAL:
                raise
            except Exception as exc:
                raise FrameFailed(frame.index + 1, item.frames, exc) from exc
            finally:
                frame.path.unlink(missing_ok=True)
            up = frame.path.with_name(frame.path.name.replace("-f", "-u", 1))
            _write_atomic(up, body)
            upscaled.append(up)
            durations.append(frame.duration_ms)
            alphas.append(frame.alpha)
        return assemble_animation(
            upscaled,
            durations_ms=durations,
            loop=read_loop(item.source),
            alphas=alphas,
            output_suffix=item.output.suffix,
        )
    finally:
        for p in upscaled:
            p.unlink(missing_ok=True)
```

In `upscale_image_dir`'s per-item loop, replace the inner `try:` body (the `prepare_upload … _write_atomic(item.output, body)` block) with:

```python
                    try:
                        if item.frames > 1:
                            body = _upscale_animated(
                                item, stage, scratch, _orch.fetch_artifact_bytes
                            )
                        else:
                            upload = prepare_upload(item, scratch)
                            state = PipelineState(
                                request=GenerationRequest(prompt="", mode="upscale"),
                                artifacts={"clip": local_artifact(upload, "image")},
                            )
                            state = stage.run(state)
                            body = _orch.fetch_artifact_bytes(state.artifacts["upscaled"])
                        _write_atomic(item.output, body)
                    finally:
                        if upload is not None and upload != item.source:
                            upload.unlink(missing_ok=True)
```

and change the per-item reason line to:

```python
                    reason = (
                        str(exc)
                        if isinstance(exc, FrameFailed)
                        else f"{type(exc).__name__}: {exc}"
                    )
```

Add `FrameFailed` to the module docstring's list of exceptions and to `__all__` if the module has one.

- [ ] **Step 4: Run to verify it passes**

Run: `pixi run python -m pytest -q tests/core/test_upscale_dir.py`
Expected: all passed, existing tests included.

- [ ] **Step 5: Whole-tree invariants**

Run: `pixi run python -m pytest -q tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers`
Expected: all passed. The only new write goes through the already-tagged `_write_atomic`; `core/upscale_dir.py` must not import `kinoforge.engines.*` or `kinoforge.cli.*`.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/upscale_dir.py tests/core/test_upscale_dir.py
pixi run pre-commit run --all-files
git commit -m "feat(upscale-dir): animated branch — one stage call per frame, assembled atomically, frame failures fail the item"
```

---

### Task 6: CLI — cap wiring, plan line, progress line, summary

**Goal:** `_cmd_upscale_image_dir` passes `cfg.upscale.max_animation_frames` to the planner; the plan line, dry-run item tag, per-item progress line and closing summary show animated counts when any animated item is present, and are byte-identical to today otherwise.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py` (`_print_image_dir_plan`, `_print_image_dir_summary`, the `plan_image_dir(...)` call and `_on_item` inside `_cmd_upscale_image_dir`)
- Test: `tests/cli/test_cmd_upscale_image_dir.py` (new class `TestAnimated`)

**Acceptance Criteria:**
- [ ] Dry run on a dir with one 4-frame WebP and one still prints `sub/anim.webp -> sub/anim.webp [pending 4 frames]`, the still's line unchanged, and `found 2: pending 2 (1 animated, 4 frames), exists 0, oversize 0, unreadable 0; non-image skipped 0`.
- [ ] Dry run on stills only prints the exact pre-existing line (no parenthetical) — `TestDryRun::test_prints_plan_and_never_runs` stays green unedited.
- [ ] With `max_animation_frames: 3` in the config, the 4-frame item prints `[oversize]: 4 frames exceeds upscale.max_animation_frames=3` and the run exits 1 after the summary with `failed 1`.
- [ ] Progress line for a written animated item ends with `(4 frames)`.
- [ ] Summary with one animated written: `upscaled 2 (1 animated, 4 frames), skipped 0 existing, failed 0, aborted 0 -> <out>`; with none animated the line is unchanged.
- [ ] Whole-tree invariants and `tests/providers` green.

**Verify:** `pixi run python -m pytest -q tests/cli/test_cmd_upscale_image_dir.py tests/cli/test_cmd_upscale_image.py tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers` → all passed.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_cmd_upscale_image_dir.py` (it already has `_cfg`, `_photos`, `_argv`, `no_runner`, `_fake_runner`, `main`, `Image`, `capsys`):

```python
def _photos_with_anim(tmp_path: Path) -> Path:
    src = _photos(tmp_path, ("a.png",))
    (src / "sub").mkdir(exist_ok=True)
    frames = [Image.new("RGBA", (4, 4), (50 * i, 0, 0, 255)) for i in range(4)]
    frames[0].save(
        src / "sub" / "anim.webp", save_all=True, append_images=frames[1:],
        duration=[100] * 4, loop=0,
    )
    return src


class TestAnimated:
    def test_dry_run_shows_frames_and_the_parenthetical(
        self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: the spend preview is silent on animations.
        src = _photos_with_anim(tmp_path)
        rc = main(_argv(tmp_path, src, "--dry-run"))
        o = capsys.readouterr().out
        assert rc == 0
        assert "a.png -> a.png [pending]" in o
        assert "sub/anim.webp -> sub/anim.webp [pending 4 frames]" in o
        assert (
            "found 2: pending 2 (1 animated, 4 frames), exists 0, oversize 0, "
            "unreadable 0; non-image skipped 0"
        ) in o

    def test_cap_from_config_reaches_the_planner(
        self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: the CLI never passes max_animation_frames, so the
        # config knob is dead and the planner's default of 300 always wins.
        src = _photos_with_anim(tmp_path)
        cfg = _cfg(tmp_path, upscale_extra={"max_animation_frames": 3})
        rc = main(_argv(tmp_path, src, "--dry-run", "-c", str(cfg)))
        o = capsys.readouterr().out
        assert rc == 0
        assert (
            "sub/anim.webp -> sub/anim.webp [oversize]: "
            "4 frames exceeds upscale.max_animation_frames=3"
        ) in o
        assert "found 2: pending 1, exists 0, oversize 1" in o

    def test_progress_and_summary_show_frames(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: a 300-frame item runs for minutes with a line that
        # looks like a still; the summary counts it as one plain file.
        src = _photos_with_anim(tmp_path)
        _fake_runner(monkeypatch)
        rc = main(_argv(tmp_path, src))
        o = capsys.readouterr().out
        assert rc == 0
        assert "[2/2] sub/anim.webp -> sub/anim.webp (4x4 -> 8x8) (4 frames)" in o
        assert "[1/2] a.png -> a.png (4x4 -> 8x8)\n" in o
        assert "upscaled 2 (1 animated, 4 frames), skipped 0 existing, failed 0, aborted 0 ->" in o
```

If `_cfg` in this test module does not already accept an `upscale_extra` mapping, extend it: add a keyword-only `upscale_extra: dict[str, object] | None = None` parameter and `upscale.update(upscale_extra or {})` before the YAML is written — read the helper first, keep its existing call sites valid.

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest -q tests/cli/test_cmd_upscale_image_dir.py -k TestAnimated`
Expected: 3 failures on the missing `4 frames` / parenthetical / oversize text.

- [ ] **Step 3: Implement**

In `src/kinoforge/cli/_commands.py`:

In `_cmd_upscale_image_dir`, the `plan_image_dir(` call gains `max_animation_frames=cfg.upscale.max_animation_frames,`.

In `_print_image_dir_plan`, change the item `tag` and the counts line:

```python
        tag = item.disposition + (f" {item.frames} frames" if item.frames > 1 else "")
        tag += " renamed" if item.renamed else ""
```

and

```python
    n_over = sum(1 for i in plan.items if i.disposition == "oversize")
    n_unr = sum(1 for i in plan.items if i.disposition == "unreadable")
    anim = [i for i in plan.pending if i.frames > 1]
    pending = f"pending {len(plan.pending)}"
    if anim:
        pending += f" ({len(anim)} animated, {sum(i.frames for i in anim)} frames)"
    print(
        f"  found {len(plan.items)}: {pending}, exists {len(plan.exists)}, "
        f"oversize {n_over}, unreadable {n_unr}; non-image skipped {plan.skipped_non_image}"
    )
```

In `_print_image_dir_summary`, change the signature to also receive the items so it can count frames: look up `plan.items` by source path:

```python
    by_source = {i.source: i for i in plan.items}
    written_items = [by_source[p] for p, (o, _) in seen.items() if o == "written"]
    written = len(written_items)
    anim = [i for i in written_items if i.frames > 1]
    head = f"upscaled {written}"
    if anim:
        head += f" ({len(anim)} animated, {sum(i.frames for i in anim)} frames)"
    print(
        f"{head}, skipped {len(plan.exists)} existing, "
        f"failed {len(run_failed) + len(plan_failed)}, aborted {len(aborted)} -> {plan.output_dir}"
    )
```

(keep `run_failed`, `aborted`, `plan_failed` exactly as they are).

In `_on_item`, the `written` branch:

```python
            frames = f" ({item.frames} frames)" if item.frames > 1 else ""
            print(
                f"[{n}/{total}] {rel_in} -> {rel_out} "
                f"({item.width}x{item.height} -> {item.width * factor}x{item.height * factor})"
                f"{frames}"
            )
```

- [ ] **Step 4: Run to verify it passes**

Run: `pixi run python -m pytest -q tests/cli/test_cmd_upscale_image_dir.py tests/cli/test_cmd_upscale_image.py`
Expected: all passed, every pre-existing exact-string test included.

- [ ] **Step 5: Whole-tree invariants**

Run: `pixi run python -m pytest -q tests/test_core_invariant.py tests/test_no_unredacted_writes.py tests/test_source_audit.py tests/providers`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/cli/_commands.py tests/cli/test_cmd_upscale_image_dir.py
pixi run pre-commit run --all-files
git commit -m "feat(cli): --image-dir shows animated counts in the plan, progress and summary; wires max_animation_frames"
```

---

### Task 7: Docs, the no-golden-moved guard, full suite

**Goal:** The directory spec points at the addendum, the README row mentions animations, PROGRESS.md records Tasks 1-7, and the full offline suite plus the golden snapshot prove nothing pod-side moved.

**Files:**
- Modify: `docs/superpowers/specs/2026-10-06-directory-image-upscale-design.md` (the §3.4 bullet "Animated GIFs and multi-page TIFFs contribute frame 0, logged.")
- Modify: `README.md` (the `upscale` row of the command table)
- Modify: `docs/configuration.md` (the "Directories." paragraph below the `upscale:` table)
- Modify: `PROGRESS.md` (RESUME SNAPSHOT)

**Acceptance Criteria:**
- [ ] The 2026-10-06 spec's §3.4 bullet reads: "Animated inputs (WebP, GIF, APNG, multi-page TIFF) are upscaled frame by frame and written back in their own container — see `2026-10-11-animated-image-dir-upscale-design.md`. Other multi-frame containers contribute frame 0, logged."
- [ ] README `upscale` row mentions "animations frame by frame, same container out".
- [ ] `docs/configuration.md` "Directories." paragraph gains two sentences: animated inputs are upscaled frame by frame and written back in their own container with durations, loop and alpha (alpha resized on the controller, GIF re-quantised to 256 colours, APNG frames held in memory while encoding); `max_animation_frames` caps them.
- [ ] `tools/snapshot_launch_payloads.py` followed by `git status --porcelain` shows NO changed golden, baseline or `_golden_provision.json`.
- [ ] `pixi run python -m pytest -q -p no:cacheprovider -m 'not live'` → 0 failed.
- [ ] `PROGRESS.md` snapshot lists Tasks 1-7 done and names Task 8 (live) as the single next action with its command.

**Verify:** `pixi run python -m pytest -q -p no:cacheprovider -m 'not live'` → `0 failed`; `pixi run python tools/snapshot_launch_payloads.py && git status --porcelain` → no golden path listed.

**Steps:**

- [ ] **Step 1: Prove no golden moved**

Run: `pixi run python tools/snapshot_launch_payloads.py && git status --porcelain`
Expected: only the docs files you are about to edit (none yet) — an EMPTY porcelain list before docs edits. If any `tests/providers/golden/*`, `_BASELINE_BYTES` or `_golden_provision.json` path appears, STOP: a controller-only change moved a pod payload, which means a module-level import leaked into an embedded set. Find it with `git diff --stat` and fix before continuing.

- [ ] **Step 2: Edit the three docs**

In the 2026-10-06 spec replace the §3.4 bullet exactly as the acceptance criterion states. In `README.md` change the `upscale` row's description to: ``Upscale a video clip (FlashVSR default; spandrel 2x), a still image (`--image`, spandrel), or a directory of images (`--image-dir`, one pod, mirrored `<dir>_upscaled`; animations frame by frame, same container out); `--output-dir`/`--no-output-dir` apply to `--video`/`--image` ``. In `docs/configuration.md`, after the "Directories." paragraph's existing sentences, add:

```
Animated inputs (animated WebP, GIF, APNG, multi-page TIFF) are upscaled frame
by frame on the same pod and written back in their own container with the
original durations, loop count and alpha — the pod returns RGB, so alpha is
resized on the controller (edges interpolated, not model-upscaled); a GIF is
re-quantised to 256 colours on write; an APNG's upscaled frames are held in
memory while encoding (every other container streams one frame at a time).
`max_animation_frames` (default 300) refuses larger animations at plan time;
the dry run's `(N animated, F frames)` is the spend preview. `--image` on a
single animated file keeps today's frame-0 behaviour.
```

- [ ] **Step 3: Full offline suite**

Run: `pixi run python -m pytest -q -p no:cacheprovider -m 'not live' 2>&1 | tail -3`
Expected: `0 failed` (around 6,800 passed). Paste the summary line into the commit body.

- [ ] **Step 4: PROGRESS.md**

Insert a new `### SESSION 2026-10-11 — animated --image-dir inputs, Tasks 1-7 done` block directly under the `## RESUME SNAPSHOT` heading (bump the heading's date), listing: the spec and plan paths, Tasks 1-7 done with their commit SHAs, the planning-time findings table from this plan's header (WebP `load()`, APNG no generator, GIF transparency recipe), and **Single next action:** Task 8 live fire with the exact command from Task 8 Step 4. Keep the 2026-10-07 and 2026-10-06 blocks below it untouched.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-10-06-directory-image-upscale-design.md README.md docs/configuration.md PROGRESS.md
pixi run pre-commit run --all-files
git commit -m "docs: animated --image-dir inputs — configuration, README, spec pointer, PROGRESS (Tasks 1-7)"
```

---

### Task 8: Live fire on RunPod — RED scaffold, fixture, spend, QA, evidence

**Goal:** The §39 live smoke proves an animated WebP with alpha and the existing animated GIF come back as animations with the right frame count, durations, loop and alpha, on one pod, with `--no-reuse` and utilisation polled.

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `tests/live/build_image_dir_fixture.py` (add `sub/sticker.webp`)
- Modify: `tests/live/test_image_dir_upscale_smoke.py` (`_EVIDENCE`, the progress regex, `expected`, animated assertions, the png rglob list)
- Create: `tests/live/evidence/2026-10-11-animated-image-dir/` (stdout, stderr, list, util, the two animated outputs, contact sheets)
- Modify: `successful-generations.md` ("See also" line under §39), `PROGRESS.md`

**Acceptance Criteria:**
- [ ] RED scaffold (fixture + test edits) committed BEFORE `pixi run preflight`; offline, `kinoforge upscale --dry-run` on the fixture prints `found 9: pending 7 (2 animated, 6 frames), exists 1, oversize 1, unreadable 0; non-image skipped 1`.
- [ ] `pixi run preflight` exits 0 immediately before the live run.
- [ ] Live run with `--no-reuse` exits 1 (the designed-in `huge.webp` oversize), seven `[n/7]` progress lines, `sub/anim.gif` and `sub/sticker.webp` written.
- [ ] `sub/sticker.webp`: 4 frames, 320×240, durations `[100, 200, 100, 300]` after `load()`, loop 0, frame 0 alpha at (0,0) ≤ 8 and at (319,239) ≥ 247.
- [ ] `sub/anim.gif`: 2 frames, 640×400, both durations 200.
- [ ] `util.txt` shows at least one probe with `gpu_util` > 0 during the run (never only `est_spend`).
- [ ] After exit, `pixi run kinoforge list` prints both `No running instances.` and `No instances recorded in ledger.`.
- [ ] Frame QA: a contact sheet per animated output read by Claude; verdict recorded in `successful-generations.md` "See also" and in `PROGRESS.md`. Anything not clearly clean gets a ⚠️.
- [ ] Evidence files committed; the two animated outputs committed (both well under the 500 KB hook limit; check with `ls -l`).

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run python -m pytest -q tests/live/test_image_dir_upscale_smoke.py -p no:cacheprovider` → `1 passed`; then `pixi run kinoforge list` → both clean lines.

**Steps:**

- [ ] **Step 1: Extend the fixture builder**

In `tests/live/build_image_dir_fixture.py`, after the `anim.gif` line add:

```python
    # 4-frame RGBA sticker, mixed durations, loop forever: the animated-WebP
    # branch with alpha (resized on the controller, not model-upscaled).
    sticker = []
    for i, colour in enumerate(((220, 40, 40), (40, 220, 40), (40, 40, 220), (220, 220, 40))):
        fr = Image.new("RGBA", (160, 120), (*colour, 255))
        fr.putalpha(Image.linear_gradient("L").resize((160, 120)))  # top transparent
        sticker.append(fr)
    sticker[0].save(
        dest / "sub" / "sticker.webp", save_all=True, append_images=sticker[1:],
        duration=[100, 200, 100, 300], loop=0, lossless=True,
    )
```

- [ ] **Step 2: Extend the live test (RED)**

In `tests/live/test_image_dir_upscale_smoke.py`:
- `_EVIDENCE = Path(__file__).parent / "evidence" / "2026-10-11-animated-image-dir"`.
- The progress regex becomes `r"^\[\d/7\] .* -> "` with `== 7`.
- `expected` becomes:

```python
    expected = {
        "luma.png": (5344, 3008),
        "luma-webp.png": (5344, 3008),
        "luma-avif.png": (5344, 3008),
        "rotated.png": (5344, 3008),
        "sub/anim.gif": (640, 400),
        "sub/sticker.webp": (320, 240),
        "sub/alpha.png": (600, 360),
    }
```
- The rglob assertion becomes `sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == sorted([*expected, "existing.png"])`.
- Add, after the size loop:

```python
    with Image.open(out / "sub" / "sticker.webp") as im:
        assert im.n_frames == 4
        durs = []
        for i in range(4):
            im.seek(i)
            im.load()
            durs.append(im.info["duration"])
        assert durs == [100, 200, 100, 300]
        assert im.info.get("loop", 0) == 0
        im.seek(0)
        rgba = im.convert("RGBA")
        assert rgba.getpixel((0, 0))[3] <= 8 and rgba.getpixel((319, 239))[3] >= 247
    with Image.open(out / "sub" / "anim.gif") as im:
        assert im.n_frames == 2
        im.seek(1)
        assert im.info["duration"] == 200
    assert "pending 7 (2 animated, 6 frames)" in proc.stdout
```
(`from PIL import Image` at the top if absent.) Also save both animated outputs into `_EVIDENCE` via the existing `shutil.copy2` loop (it already copies every `expected` entry).

- [ ] **Step 3: Prove the scaffold offline, then commit it (RED is fine)**

```bash
pixi run python tests/live/build_image_dir_fixture.py /tmp/claude-1000/-workspace/*/scratchpad/dir-smoke
pixi run kinoforge upscale -c examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml \
  --image-dir /tmp/claude-1000/-workspace/*/scratchpad/dir-smoke --dry-run
```
Expected dry-run line: `found 9: pending 7 (2 animated, 6 frames), exists 1, oversize 1, unreadable 0; non-image skipped 1` and `sub/sticker.webp -> sub/sticker.webp [pending 4 frames]`.

```bash
mkdir -p tests/live/evidence/2026-10-11-animated-image-dir
git add tests/live/build_image_dir_fixture.py tests/live/test_image_dir_upscale_smoke.py
pixi run pre-commit run --all-files
git commit -m "test(live): RED scaffold — animated WebP sticker + GIF in the --image-dir smoke"
```

- [ ] **Step 4: Preflight, then fire — in the FOREGROUND**

```bash
pixi run preflight
```
Expected: exit 0 (creds present, zero active pods, clean tree). Do not chain this with the run; read the exit code.

```bash
KINOFORGE_LIVE_TESTS=1 pixi run python -m pytest -q \
  tests/live/test_image_dir_upscale_smoke.py -p no:cacheprovider
```
The harness thread polls RunPod GraphQL utilisation every 60 s into `evidence/util.txt`. If a subagent runs this, it runs it in the foreground (a background wait dies with the subagent's turn). While it runs, the controller tails `util.txt`; GPU 0% for three consecutive probes after the first inference line means a dead pod — pull `bootstrap.log` via the pod's port 8001, `kinoforge destroy --id <pod>`, fail fast.

- [ ] **Step 5: Verify teardown, QA the outputs**

```bash
pixi run kinoforge list
```
Expected: `[instance overview] No running instances.` AND `No instances recorded in ledger.`; a `⚠ launching — pod not confirmed` row is the accepted placeholder (ages out), never `destroy` it.

Build contact sheets of every frame of both animated outputs (Pillow: open, iterate frames, paste side by side, save `evidence/sticker-frames.png` and `evidence/anim-gif-frames.png`) and READ them: sharp edges, no tile seams, colours as in the fixture, transparency gradient on the sticker intact (checkerboard or transparent top in the viewer). Record the verdict.

- [ ] **Step 6: Evidence, generations log, PROGRESS, commit**

```bash
ls -l tests/live/evidence/2026-10-11-animated-image-dir/
```
Confirm every file is under 500 KB. Add to `successful-generations.md` under §39's TOC entry a "See also" line: date, pod id, `pending 7 (2 animated, 6 frames)`, the two animated outputs with frames/durations/loop, the QA verdict, approximate spend from the pod lifetime. Update the `PROGRESS.md` snapshot: Task 8 done, plan 8/8, the follow-ups found during review (if any), single next action.

```bash
git add tests/live/evidence/2026-10-11-animated-image-dir successful-generations.md PROGRESS.md
pixi run pre-commit run --all-files
git commit -m "test(live): animated --image-dir inputs live-proven on RunPod spandrel x2 (§39 see-also)"
```

```json:metadata
{"files": ["tests/live/build_image_dir_fixture.py", "tests/live/test_image_dir_upscale_smoke.py", "successful-generations.md", "PROGRESS.md"], "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run python -m pytest -q tests/live/test_image_dir_upscale_smoke.py -p no:cacheprovider && pixi run kinoforge list", "acceptanceCriteria": ["RED scaffold committed before preflight; dry run prints found 9: pending 7 (2 animated, 6 frames)", "pixi run preflight exit 0 immediately before the run", "live run --no-reuse: seven [n/7] lines, sub/anim.gif and sub/sticker.webp written", "sticker.webp: 4 frames 320x240 durations [100,200,100,300] loop 0 alpha(0,0)<=8 alpha(319,239)>=247", "anim.gif: 2 frames 640x400 duration 200", "util.txt shows gpu_util > 0 during the run", "kinoforge list: No running instances. AND No instances recorded in ledger.", "contact sheets read; verdict recorded in successful-generations.md and PROGRESS.md", "evidence + both animated outputs committed, each under 500 KB"], "modelTier": "frontier", "userGate": true, "tags": ["user-gate", "live-spend"], "gateScope": "task", "failurePolicy": "capture-logs-destroy-pod-fail-fast", "requireEvidenceTokens": [["dry-run", "pending 7 (2 animated, 6 frames)"], ["live", "[7/7]", "gpu_util"]]}
```

---

## Self-review

**Spec coverage.** §2 surface + §2.1 config → Task 1 + Task 6. §2.2 dry run/summary → Task 6. §3.1 header frames + APNG closure → Task 2 (+ Task 5 routes `frames > 1` away from passthrough). §3.2 cap → Task 2. §3.3 naming + exists + GIF decision → Task 2 + Task 7 docs. §4.1 `prepare_frames` → Task 3. §4.2 alpha → Task 4. §4.3 writers, lazy frames, atomic write → Task 4 + Task 5. §5 runner, progress, scratch → Task 5. §6 CLI → Task 6. §7 tests → Tasks 1-6; §7.1 live → Task 8. §8 docs → Task 7 + Task 8. §9 zero pod-side → Task 7 Step 1. §10 open questions → all three resolved in the planning-time findings (Q1 `n_frames` works and Task 2's header tests run in CI; Q2 PNG materialises, others stream — Task 4's laziness test; Q3 GIF recipe verified — Task 4's GIF alpha test).

**Deviation from the spec, recorded:** the live fixture already had an animated GIF, so Task 8 adds one WebP (not two files) and the expected counts are `found 9: pending 7 (2 animated, 6 frames)`, not the spec's illustrative `pending 8 (… 7 frames)`.

**Type consistency.** `Frame(index, path, duration_ms, alpha)` (Task 3) is what Task 5 reads. `prepare_frames(item, scratch)`, `read_loop(path)` (Task 3) are imported by Task 5. `assemble_animation(frame_paths, *, durations_ms, loop, alphas, output_suffix) -> bytes` (Task 4) is called with exactly those keywords in Task 5. `plan_image_dir(..., max_animation_frames=)` (Task 2) is passed in Task 6. `ImageDirItem.frames` (Task 2) is read in Tasks 5 and 6. `_open_frame` (Task 4) is the seam Task 4's laziness test patches. `FrameFailed(index, total, cause)` (Task 5) is only raised and `str()`ed there.

**Placeholders.** None: every step carries its code, command and expected output.
