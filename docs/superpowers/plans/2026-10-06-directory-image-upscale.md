# Directory Image Upscaling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `kinoforge upscale --image-dir DIR` upscales every image under `DIR` (any format Pillow opens, recursive) on ONE spandrel pod and writes lossless PNGs into a sibling `DIR_upscaled` that mirrors the tree.

**Architecture:** A pure planner (`core/image_dir.py`) walks the tree, names outputs, applies the megapixel guard and converts inputs on the controller to the PNG/JPEG bytes the pod already accepts. A session runner (`core/upscale_dir.py`, shaped like `core/batch.py`) opens `deploy_session` once and runs `UpscaleStage` per image inside it; `--no-reuse` destroys the pod once at session exit because that destroy already lives in `deploy_session`'s `finally`. The CLI gains one flag in the existing `upscale` source group and keeps its warm-reuse / attach / launch-row chain unchanged. Zero pod-side changes.

**Tech Stack:** Python 3.12+, pydantic config, Pillow 12 + pillow-heif (conda-forge), the existing `deploy_session` / `UpscaleStage` / `_pod_http` seams, pytest with Pillow-written fixtures.

**Spec:** `docs/superpowers/specs/2026-10-06-directory-image-upscale-design.md`

## Global Constraints

- **Zero pod-side changes.** Nothing under `src/kinoforge/engines/diffusers/servers/`, `src/kinoforge/upscalers/spandrel/_runtime.py`, `_engine.py` or any embedded module is edited. `tests/providers/test_pod_embed_closure.py` and `tests/providers/test_env_payload_ceiling.py` must stay green and NO launch golden or `_BASELINE_BYTES` entry may move (Task 8 asserts it). A moved golden is a defect, not a regeneration.
- **Controller-side imports only.** `core/image_dir.py` and `core/upscale_dir.py` are never imported by pod-side code. Pillow is imported lazily inside functions, never at module level in `core/media.py` (that module is imported by `pipeline/upscale.py`, which pods do not embed, but keep it cheap anyway).
- **Exit codes:** 2 = config/precondition (before any pod work), 1 = operational (any item failed or aborted, pod dead, batch-fatal), 0 = success, dry run, nothing-to-do.
- **Output is always `.png`**, lossless, written atomically (temp + `os.replace`), never overwriting an existing output.
- **Every stdout/stderr message names the offending thing** (the path, the cap, the flag) — same convention as the existing `--image` refusals.
- **Local timezone everywhere** (`datetime.now()`), no UTC.
- **Google-style docstrings and full type hints** on every function (ruff `D`, `ANN` are enforced on `src/`; relaxed under `tests/`).
- **Each test states the behaviour under test and the concrete bug it catches** (a `# Bug caught:` comment), per the `test-design` skill. Guard-the-guard: a test that iterates a discovered set asserts a plausible count.
- **Commit after every task** with Conventional Commits; run `pixi run pre-commit run --all-files` first (it ignores untracked files — `git add` new files before trusting it). Never `--no-verify`.
- **Live spend only in Task 9**, only after its RED scaffold is committed and `pixi run preflight` exits 0, with `--no-reuse`, utilisation polled every 60-90 s, and `kinoforge list` verified clean afterwards.

**User decisions (already made):**
- "I prefer your recommendation, the new flag" — `--image-dir` on `upscale`, not a new subcommand.
- "recursive, and mirror the tree into photos_upscaled".
- Naming: `<stem>.png`; same-folder stem collisions keep the full original name + `.png`; existing outputs are skipped (no overwrite flag). "sounds good".
- Failures: "log it, keep going as you suggest" — per-item failures continue; exit 1 at the end with a summary.
- "Keep them separate, add the megapixel guard" — the video and image tilers are NOT unified; `upscale.max_output_megapixels` (default 256) bounds output pixels on the controller.
- "B" — one `deploy_session`, many `UpscaleStage` runs (not a loop over `generate()`).

**Planning-time findings (verified in this environment, 2026-10-06):**
- imageio's Pillow plugin does NOT apply EXIF orientation (a 4×2 JPEG with orientation 6 reads back as 2×4×3, not transposed) and returns `uint16` for a 16-bit PNG, which the pod's `_to_rgb` refuses. Both justify §3.4's controller-side re-encode.
- imageio DOES expand a palette (`P`) PNG to `(h, w, 3) uint8`, so spec §9.3's worry was unfounded. The passthrough rule stays exactly as specified (`RGB` / `RGBA` / `L` only) — re-encoding a `P` PNG costs nothing and keeps one rule.
- The pod unlinks each upload after its job (`_maybe_cleanup_upload` runs in `_run_upscale_job`), so uploads do NOT accumulate (spec §9.2). Outputs under `/tmp/kf-artifacts` DO persist for the pod's lifetime (spec §9.1 — retention is fine for the fetch); the example config's `disk_gb: 40` bounds a run at roughly 1,000 4-MP 2x outputs. Task 8 documents that ceiling.
- `Image.convert("RGB")` from `I;16`, `P`, `LA`, `RGBA` and `CMYK` all succeed on Pillow 12.2; AVIF and WebP save support is present, so the live fixture (Task 9) can be built with Pillow alone.
- `pillow-heif` is NOT currently installed (Task 1 adds it). `pillow` is present only transitively.
- The runner's injection seams are `provider` (passthrough to `deploy_session`), `engine` (the GENERATION engine `deploy_session` resolves from `cfg.engine`, passthrough) and `upscaler` (the `UpscalerEngine` the stage calls). The spec's §4 signature named only `engine`; three seams are needed because the upscaler is not what `deploy_session` resolves.

---

## File Structure

| file | responsibility | task |
|---|---|---|
| `pixi.toml` | declare `pillow` + `pillow-heif` | 1 |
| `src/kinoforge/core/media.py` | `DIR_IMAGE_SUFFIXES`; `local_artifact(path, media)` | 1 |
| `src/kinoforge/cli/_commands.py` (`_resolve_input_as_artifact`) | delegate the local branch to `local_artifact` | 1 |
| `src/kinoforge/core/config.py` (`UpscaleConfig`) | `max_output_megapixels: int = 256`, validated | 2 |
| `src/kinoforge/core/image_dir.py` | **new** — `output_dir_for`, `read_image_header`, `output_megapixels`, `walk_images`, `plan_image_dir`, the dataclasses | 3 |
| `src/kinoforge/core/image_dir.py` | `prepare_upload` | 4 |
| `src/kinoforge/core/orchestrator.py` | `fetch_artifact_bytes` extracted from the materialize block | 5 |
| `src/kinoforge/core/upscale_dir.py` | **new** — `upscale_image_dir`, `ImageDirResult`, `ItemOutcome`, `PodDead` | 6 |
| `src/kinoforge/cli/_main.py` | `--image-dir` in the `upscale` source group | 7 |
| `src/kinoforge/cli/_commands.py` | `_cmd_upscale_image_dir`, `_finish_launch_row`, `_image_megapixel_error`, the `--image-dir` dispatch | 7 |
| `docs/configuration.md`, `docs/engines.md`, `README.md`, `PROGRESS.md` | docs | 8 |
| `tests/live/build_image_dir_fixture.py`, `tests/live/test_image_dir_upscale_smoke.py` | fixture builder + RED live scaffold | 9 |

---

### Task 1: Declare Pillow, add `DIR_IMAGE_SUFFIXES` and `local_artifact`

**Goal:** Pillow and pillow-heif become declared dependencies; `core/media.py` owns the directory suffix set and the local-file artifact builder the CLI already implements inline.

**Files:**
- Modify: `pixi.toml` (the `[dependencies]` table, after `python-dotenv`)
- Modify: `src/kinoforge/core/media.py`
- Modify: `src/kinoforge/cli/_commands.py:1477-1505` (`_resolve_input_as_artifact`)
- Test: `tests/core/test_media.py`

**Acceptance Criteria:**
- [ ] `pixi run python -c "import pillow_heif, PIL; print(PIL.__version__)"` prints a version; `pixi.toml` and `pixi.lock` both change and are committed together.
- [ ] `DIR_IMAGE_SUFFIXES` holds exactly the 25 suffixes of spec §3.1, lower-cased with leading dots; `IMAGE_SUFFIXES` is a subset of it; `.mpg`, `.pdf`, `.ps`, `.h5` are absent.
- [ ] Every suffix in `DIR_IMAGE_SUFFIXES` has a registered Pillow opener once `pillow_heif.register_heif_opener()` has run.
- [ ] `local_artifact(path, "image")` returns an `Artifact` whose `uri` is `file://<absolute path>`, whose `sha256` equals the file's sha256 and whose `size` equals `st_size`, with `meta == {"media": "image"}`.
- [ ] `_resolve_input_as_artifact("<local>", media)` returns a value equal to `local_artifact(Path("<local>"), media)`; the http(s) branch is unchanged.

**Verify:** `pixi run pytest tests/core/test_media.py tests/cli/test_cmd_upscale_image.py tests/cli/test_cmd_upscale_full.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Add the dependencies**

```bash
pixi add pillow pillow-heif
```

Confirm with `rg -n 'pillow' pixi.toml` that both landed under `[dependencies]` (conda-forge). `pixi.lock` regenerates — stage BOTH files at commit time (memory: `pre-commit` stages `pixi.lock`; a hook-stash conflict aborts the commit if only one is staged).

- [ ] **Step 2: Write the failing tests**

Append to `tests/core/test_media.py`:

```python
class TestDirImageSuffixes:
    def test_every_suffix_has_a_pillow_opener(self) -> None:
        # Bug caught: a typo'd suffix (".jpge") that no file ever matches,
        # or a suffix Pillow cannot open, silently never reaches the pod.
        from PIL import Image

        import pillow_heif

        from kinoforge.core.media import DIR_IMAGE_SUFFIXES

        pillow_heif.register_heif_opener()
        registered = Image.registered_extensions()
        openable = {ext for ext, fmt in registered.items() if fmt in Image.OPEN}
        missing = sorted(s for s in DIR_IMAGE_SUFFIXES if s not in openable)
        assert missing == []
        # Guard the guard: a sweep over an empty set passes everything.
        assert len(DIR_IMAGE_SUFFIXES) >= 20

    def test_non_image_suffixes_are_excluded(self) -> None:
        # Bug caught: "everything Pillow registers" would classify .mpg,
        # .pdf and .h5 as images and feed them to the planner.
        from kinoforge.core.media import DIR_IMAGE_SUFFIXES

        for bad in (".mpg", ".mpeg", ".pdf", ".ps", ".eps", ".h5", ".hdf", ".bufr"):
            assert bad not in DIR_IMAGE_SUFFIXES

    def test_pod_suffixes_are_a_subset(self) -> None:
        # Bug caught: a PNG in the directory counted as "non-image".
        from kinoforge.core.media import DIR_IMAGE_SUFFIXES, IMAGE_SUFFIXES

        assert IMAGE_SUFFIXES <= DIR_IMAGE_SUFFIXES
        assert all(s == s.lower() and s.startswith(".") for s in DIR_IMAGE_SUFFIXES)


class TestLocalArtifact:
    def test_stamps_uri_sha_size_and_media(self, tmp_path: Path) -> None:
        # Bug caught: a relative uri, a sha of the wrong bytes, or a missing
        # media stamp means the pod receives a "video".
        import hashlib

        from kinoforge.core.media import local_artifact

        p = tmp_path / "in.png"
        p.write_bytes(b"\x89PNG not really")
        art = local_artifact(p, "image")
        assert art.uri == f"file://{p.resolve()}"
        assert art.sha256 == hashlib.sha256(b"\x89PNG not really").hexdigest()
        assert art.size == p.stat().st_size
        assert art.meta == {"media": "image"}

    def test_cli_resolver_delegates(self, tmp_path: Path) -> None:
        # Bug caught: the CLI keeps its own copy and the two drift.
        from kinoforge.cli._commands import _resolve_input_as_artifact
        from kinoforge.core.media import local_artifact

        p = tmp_path / "in.mp4"
        p.write_bytes(b"mp4")
        assert _resolve_input_as_artifact(str(p), "video") == local_artifact(p, "video")
```

`tests/core/test_media.py` already imports `Path`? Check the top of the file; add `from pathlib import Path` if absent.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pixi run pytest tests/core/test_media.py -q`
Expected: FAIL — `ImportError: cannot import name 'DIR_IMAGE_SUFFIXES'` / `'local_artifact'`.

- [ ] **Step 4: Implement**

In `src/kinoforge/core/media.py`, after `IMAGE_SUFFIXES`:

```python
DIR_IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".jfif", ".webp", ".avif", ".gif", ".bmp", ".dib",
        ".tif", ".tiff", ".tga", ".heic", ".heif", ".jp2", ".j2k", ".psd", ".ico",
        ".pcx", ".pbm", ".pgm", ".ppm", ".pnm", ".qoi", ".dds",
    }
)
"""Suffixes ``kinoforge upscale --image-dir`` treats as images (lower-cased).

A curated set, deliberately NOT everything Pillow registers — that list
includes ``.mpg``, ``.pdf``, ``.h5`` and ``.ps``. Files with other suffixes are
counted and skipped, never refused. Anything outside :data:`IMAGE_SUFFIXES`
is re-encoded to PNG on the controller before upload.
"""
```

and at the end of the module:

```python
def local_artifact(path: Path, media: Media) -> Artifact:
    """Materialise a local file as an input ``Artifact`` stamped with *media*.

    ``file://`` absolute uri, sha256 from disk, size from ``stat``. Shared by
    the CLI's ``--video`` / ``--image`` resolver and the directory runner so
    neither re-implements the stamp.

    Args:
        path: Local file; resolved to an absolute path.
        media: ``"video"`` or ``"image"``.

    Returns:
        The artifact to seed into ``state.artifacts["clip"]``.
    """
    import hashlib

    from kinoforge.core.interfaces import Artifact

    p = Path(path).resolve()
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return Artifact(
        uri=f"file://{p}", sha256=h.hexdigest(), size=p.stat().st_size, meta={MEDIA_KEY: media}
    )
```

Add `from pathlib import Path` to the module's imports (runtime import — it is used in the signature under `from __future__ import annotations`, so a `TYPE_CHECKING` import would also do; a plain import is simpler and `pathlib` is cheap).

In `src/kinoforge/cli/_commands.py`, replace the body of `_resolve_input_as_artifact` after the docstring with:

```python
    if path_or_url.startswith(("http://", "https://")):
        return Artifact(uri=path_or_url, sha256="", size=0, meta={MEDIA_KEY: media})
    return local_artifact(Path(path_or_url), media)
```

and change the import line `from kinoforge.core.media import IMAGE_SUFFIXES, MEDIA_KEY` to `from kinoforge.core.media import IMAGE_SUFFIXES, MEDIA_KEY, local_artifact`. Delete the now-unused `import hashlib as _hashlib` inside the function.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pixi run pytest tests/core/test_media.py tests/cli/test_cmd_upscale_image.py tests/cli/test_cmd_upscale_full.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
pixi run pre-commit run --all-files
git add pixi.toml pixi.lock src/kinoforge/core/media.py src/kinoforge/cli/_commands.py tests/core/test_media.py
git commit -m "feat(media): declare pillow + pillow-heif; DIR_IMAGE_SUFFIXES and local_artifact"
```

---

### Task 2: `upscale.max_output_megapixels` config field

**Goal:** `UpscaleConfig` carries the output-pixel cap with a positive-integer validator and a default of 256.

**Files:**
- Modify: `src/kinoforge/core/config.py:838-846` (the `UpscaleConfig` fields and docstring)
- Test: `tests/core/test_config_upscale_megapixels.py` (new)

**Acceptance Criteria:**
- [ ] `Config.model_validate(<spandrel cfg without the key>).upscale.max_output_megapixels == 256`.
- [ ] `max_output_megapixels: 64` round-trips; `0` and `-5` raise `ConfigError` whose message contains `upscale.max_output_megapixels`.
- [ ] Every existing config under `examples/configs/` still loads (`tests/` already sweeps them — stays green).

**Verify:** `pixi run pytest tests/core/test_config_upscale_megapixels.py tests/core/test_config.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_config_upscale_megapixels.py`:

```python
"""`upscale.max_output_megapixels` — the controller-side output-pixel cap."""

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
            {"kind": "base", "ref": "hf:Wan-AI/Wan2.2-T2V", "target": "diffusion_models"}
        ],
        "compute": {"provider": "fake", "image": "fake:latest"},
        "upscale": upscale,
    }


def test_default_is_256() -> None:
    # Bug caught: no default → every existing config fails to load.
    cfg = Config.model_validate(_cfg())
    assert cfg.upscale is not None
    assert cfg.upscale.max_output_megapixels == 256


def test_explicit_value_round_trips() -> None:
    cfg = Config.model_validate(_cfg({"max_output_megapixels": 64}))
    assert cfg.upscale is not None
    assert cfg.upscale.max_output_megapixels == 64


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_is_refused(bad: int) -> None:
    # Bug caught: a 0 cap refuses every image; a negative one is nonsense
    # that would surface only as a confusing "oversize" on the first file.
    with pytest.raises(ConfigError, match="upscale.max_output_megapixels"):
        Config.model_validate(_cfg({"max_output_megapixels": bad}))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pixi run pytest tests/core/test_config_upscale_megapixels.py -q`
Expected: `test_default_is_256` FAILS with `AttributeError: 'UpscaleConfig' object has no attribute 'max_output_megapixels'`; the refusal cases fail because no error is raised.

- [ ] **Step 3: Implement**

In `UpscaleConfig` (`src/kinoforge/core/config.py`), add to the Attributes docstring:

```
        max_output_megapixels: Controller-side cap on ``width × height ×
            scale²`` of a STILL-IMAGE output (``--image`` / ``--image-dir``).
            An oversize still is refused (``--image``) or recorded as a
            per-file failure (``--image-dir``) before any upload. Bounds the
            pod's host-RAM output canvas and PNG encode — a 50 MP photo at 4x
            is an 800 MP canvas, ~2.4 GB raw — which the pod-side tiler does
            not bound. Ignored for video.
```

add the field after `tile_overlap`:

```python
    max_output_megapixels: int = 256
```

and a validator beside `_validate_tiling`:

```python
    @model_validator(mode="after")
    def _validate_megapixels(self) -> Self:
        if self.max_output_megapixels <= 0:
            raise ConfigError(
                "upscale.max_output_megapixels must be positive, "
                f"got {self.max_output_megapixels}"
            )
        return self
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/core/test_config_upscale_megapixels.py tests/core/test_config.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/config.py tests/core/test_config_upscale_megapixels.py
git commit -m "feat(config): upscale.max_output_megapixels — still-image output-pixel cap"
```

---

### Task 3: `core/image_dir.py` — the planner

**Goal:** A pure module that walks a directory recursively, names every output, reads image headers (size, mode, orientation) without decoding pixels, applies the megapixel guard and returns an `ImageDirPlan` the CLI and the runner both consume.

**Files:**
- Create: `src/kinoforge/core/image_dir.py`
- Test: `tests/core/test_image_dir_plan.py` (new)

**Acceptance Criteria:**
- [ ] `output_dir_for(Path("photos"))`, `output_dir_for(Path("photos/"))` and the resolved absolute path all return `<cwd-resolved>/photos_upscaled`; a filesystem root raises `ValueError`.
- [ ] The walk is recursive, sorted by name at every level, skips dot-prefixed names and symlinks, mirrors relative paths with suffix `.png`, and counts non-image files in `skipped_non_image` without creating items.
- [ ] Same-folder stem collisions get `<full name>.png` with `renamed=True`; a lone file gets `<stem>.png`; a same-stem pair in different folders is not renamed.
- [ ] Dispositions: an unopenable file is `unreadable` (reason names the exception); an existing output is `exists`; `width × height × scale²` over the cap is `oversize` (reason names the cap); else `pending`. `plan.pending`, `plan.exists`, `plan.failed_at_plan` partition the items accordingly.
- [ ] A JPEG with EXIF orientation 6 reports swapped width/height.
- [ ] No pixel data is decoded during planning (a `MagicMock`-free check: planning a 1-px-wide, 60000-px-tall PNG header finishes in under a second).

**Verify:** `pixi run pytest tests/core/test_image_dir_plan.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_image_dir_plan.py`:

```python
"""`core/image_dir.py` planning: walk, naming, header read, megapixel guard."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from kinoforge.core.image_dir import (
    ImageDirPlan,
    output_dir_for,
    output_megapixels,
    plan_image_dir,
    read_image_header,
)


def _png(path: Path, size: tuple[int, int] = (8, 6), mode: str = "RGB") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new(mode, size, 0).save(path)
    return path


def _jpeg_oriented(path: Path, size: tuple[int, int], orientation: int) -> Path:
    im = Image.new("RGB", size, (1, 2, 3))
    exif = im.getexif()
    exif[0x0112] = orientation
    im.save(path, exif=exif)
    return path


def _plan(src: Path, *, scale: int = 2, cap: int = 256) -> ImageDirPlan:
    return plan_image_dir(src, scale=scale, max_output_megapixels=cap)


class TestOutputDirFor:
    def test_sibling_with_suffix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bug caught: a trailing slash yields "photos/_upscaled" or an empty
        # name; a relative path resolves somewhere other than the cwd.
        (tmp_path / "photos").mkdir()
        monkeypatch.chdir(tmp_path)
        expected = tmp_path.resolve() / "photos_upscaled"
        assert output_dir_for(Path("photos")) == expected
        assert output_dir_for(Path("photos/")) == expected
        assert output_dir_for(tmp_path / "photos") == expected

    def test_root_is_refused(self) -> None:
        with pytest.raises(ValueError, match="root"):
            output_dir_for(Path("/"))


class TestWalk:
    def test_recursive_sorted_mirrored(self, tmp_path: Path) -> None:
        # Bug caught: subdirectories dropped, or non-deterministic order.
        src = tmp_path / "photos"
        _png(src / "b.png")
        _png(src / "a.webp")
        _png(src / "sub" / "deep" / "c.bmp")
        plan = _plan(src)
        rel_in = [i.source.relative_to(src).as_posix() for i in plan.items]
        rel_out = [i.output.relative_to(plan.output_dir).as_posix() for i in plan.items]
        assert rel_in == ["a.webp", "b.png", "sub/deep/c.bmp"]
        assert rel_out == ["a.png", "b.png", "sub/deep/c.png"]
        assert plan.output_dir == tmp_path.resolve() / "photos_upscaled"
        assert _plan(src).items == plan.items  # stable across calls

    def test_dotfiles_and_symlinks_are_skipped(self, tmp_path: Path) -> None:
        # Bug caught: following a symlinked directory loops or escapes the
        # tree; .DS_Store-style files become "unreadable" failures.
        src = tmp_path / "photos"
        _png(src / "a.png")
        _png(src / ".hidden.png")
        (src / ".cache").mkdir()
        _png(src / ".cache" / "x.png")
        outside = _png(tmp_path / "outside" / "o.png")
        os.symlink(outside.parent, src / "linked_dir")
        os.symlink(outside, src / "linked.png")
        plan = _plan(src)
        assert [i.source.name for i in plan.items] == ["a.png"]
        assert plan.skipped_non_image == 0

    def test_non_image_files_are_counted_not_items(self, tmp_path: Path) -> None:
        # Bug caught: a refusal or an unreadable item for notes.txt.
        src = tmp_path / "photos"
        _png(src / "a.png")
        (src / "notes.txt").write_text("hi")
        (src / "clip.mp4").write_bytes(b"\x00")
        plan = _plan(src)
        assert [i.source.name for i in plan.items] == ["a.png"]
        assert plan.skipped_non_image == 2

    def test_suffix_match_is_case_insensitive(self, tmp_path: Path) -> None:
        src = tmp_path / "photos"
        _png(src / "A.PNG")
        assert [i.source.name for i in _plan(src).items] == ["A.PNG"]


class TestNaming:
    def test_same_folder_collision_keeps_full_name(self, tmp_path: Path) -> None:
        # Bug caught: a.webp and a.png both map to a.png and one output
        # silently overwrites the other.
        src = tmp_path / "photos"
        _png(src / "a.png")
        _png(src / "a.webp")
        _png(src / "b.webp")
        plan = _plan(src)
        by_name = {i.source.name: i for i in plan.items}
        assert by_name["a.png"].output.name == "a.png.png" and by_name["a.png"].renamed
        assert by_name["a.webp"].output.name == "a.webp.png" and by_name["a.webp"].renamed
        assert by_name["b.webp"].output.name == "b.png" and not by_name["b.webp"].renamed

    def test_cross_folder_same_stem_is_not_a_collision(self, tmp_path: Path) -> None:
        src = tmp_path / "photos"
        _png(src / "a.png")
        _png(src / "sub" / "a.webp")
        plan = _plan(src)
        assert sorted(i.output.name for i in plan.items) == ["a.png", "a.png"]
        assert not any(i.renamed for i in plan.items)


class TestDispositions:
    def test_existing_output_is_skipped(self, tmp_path: Path) -> None:
        # Bug caught: every re-run re-upscales and overwrites.
        src = tmp_path / "photos"
        _png(src / "a.png")
        _png(src / "b.png")
        out = output_dir_for(src)
        _png(out / "a.png")
        plan = _plan(src)
        assert [i.source.name for i in plan.exists] == ["a.png"]
        assert [i.source.name for i in plan.pending] == ["b.png"]

    def test_unreadable_is_a_plan_failure(self, tmp_path: Path) -> None:
        # Bug caught: a corrupt .jpg crashes planning (so nothing runs) or
        # is uploaded and fails on the pod after the boot.
        src = tmp_path / "photos"
        _png(src / "ok.png")
        (src / "bad.jpg").write_bytes(b"not a jpeg")
        plan = _plan(src)
        bad = next(i for i in plan.items if i.source.name == "bad.jpg")
        assert bad.disposition == "unreadable"
        assert bad.reason is not None and "UnidentifiedImageError" in bad.reason
        assert [i.source.name for i in plan.failed_at_plan] == ["bad.jpg"]
        assert [i.source.name for i in plan.pending] == ["ok.png"]

    def test_oversize_boundary(self, tmp_path: Path) -> None:
        # Bug caught: an off-by-one at the cap, or a guard on INPUT pixels.
        src = tmp_path / "photos"
        # cap = 1 MP, scale 2 → input limit is 250,000 px.
        _png(src / "under.png", (500, 500))  # 250,000 × 4 = 1,000,000 → not over
        _png(src / "over.png", (501, 500))  # 250,500 × 4 = 1,002,000 → over
        plan = _plan(src, scale=2, cap=1)
        by = {i.source.name: i for i in plan.items}
        assert by["under.png"].disposition == "pending"
        assert by["over.png"].disposition == "oversize"
        assert "max_output_megapixels=1" in (by["over.png"].reason or "")
        assert "501x500" in (by["over.png"].reason or "")

    def test_output_megapixels_formula(self) -> None:
        assert output_megapixels(1000, 1000, 2) == 4.0
        assert output_megapixels(2672, 1504, 2) == pytest.approx(16.07, abs=0.01)


class TestHeader:
    def test_orientation_swaps_dimensions(self, tmp_path: Path) -> None:
        # Bug caught: the guard and the dry run report a portrait phone
        # photo as landscape; the output dims line is wrong.
        p = _jpeg_oriented(tmp_path / "o.jpg", (40, 20), orientation=6)
        hdr = read_image_header(p)
        assert (hdr.width, hdr.height) == (20, 40)
        assert hdr.orientation == 6
        plain = _jpeg_oriented(tmp_path / "p.jpg", (40, 20), orientation=1)
        assert (read_image_header(plain).width, read_image_header(plain).height) == (40, 20)

    def test_header_reports_mode_and_format(self, tmp_path: Path) -> None:
        p = _png(tmp_path / "x.png", mode="RGBA")
        hdr = read_image_header(p)
        assert hdr.mode == "RGBA" and hdr.format == "PNG" and hdr.orientation == 1

    def test_planning_does_not_decode_pixels(self, tmp_path: Path) -> None:
        # Bug caught: a planner that calls im.load() decodes every file;
        # a 1×60000 PNG header must plan in well under a second.
        import time

        src = tmp_path / "photos"
        _png(src / "tall.png", (1, 60000))
        t0 = time.perf_counter()
        plan = _plan(src, cap=1000)
        assert plan.items[0].height == 60000
        assert time.perf_counter() - t0 < 1.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/core/test_image_dir_plan.py -q`
Expected: FAIL at import — `ModuleNotFoundError: No module named 'kinoforge.core.image_dir'`.

- [ ] **Step 3: Implement**

Create `src/kinoforge/core/image_dir.py`:

```python
"""Plan a directory of stills for ``kinoforge upscale --image-dir``.

Pure planning: walk the tree, name every output, read image HEADERS (size,
mode, EXIF orientation — never pixels), apply the output-megapixel guard, and
hand the CLI and the session runner one finished :class:`ImageDirPlan` so
neither re-derives a rule. :func:`prepare_upload` (same module) converts one
planned input into the PNG/JPEG bytes the pod accepts.

Controller-side only. Pillow is imported lazily inside functions.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from kinoforge.core.media import DIR_IMAGE_SUFFIXES

_log = logging.getLogger("kinoforge.core.image_dir")

OUTPUT_DIR_SUFFIX = "_upscaled"
"""Appended to the source directory's last component to name the output dir."""

_ORIENTATION_TAG = 0x0112
_ROTATED_ORIENTATIONS = frozenset({5, 6, 7, 8})
"""EXIF orientations that swap width and height as viewed."""

Disposition = Literal["pending", "exists", "oversize", "unreadable"]


@dataclass(frozen=True)
class ImageHeader:
    """What planning reads from a file without decoding pixels.

    Attributes:
        width: Width as VIEWED (EXIF orientation applied).
        height: Height as viewed.
        mode: Pillow mode string (``"RGB"``, ``"I;16"``, ``"P"``, ...).
        format: Pillow format name (``"PNG"``, ``"JPEG"``, ``"WEBP"``, ...).
        orientation: EXIF orientation 1-8; 1 is the identity.
    """

    width: int
    height: int
    mode: str
    format: str | None
    orientation: int


@dataclass(frozen=True)
class ImageDirItem:
    """One discovered image and what will happen to it.

    Attributes:
        source: Absolute input path.
        output: Absolute output path, always ``.png``.
        width: Viewed width (0 when ``unreadable``).
        height: Viewed height (0 when ``unreadable``).
        disposition: ``pending`` (will run), ``exists`` (output present,
            skipped), ``oversize`` / ``unreadable`` (plan-time failures).
        reason: Set for the two failure dispositions.
        renamed: The same-folder stem-collision rule applied (§3.2).
    """

    source: Path
    output: Path
    width: int
    height: int
    disposition: Disposition
    reason: str | None = None
    renamed: bool = False


@dataclass(frozen=True)
class ImageDirPlan:
    """The finished plan for one directory.

    Attributes:
        source_dir: Resolved input directory.
        output_dir: Resolved sibling ``<name>_upscaled`` directory.
        items: Every recognised image in sorted walk order.
        skipped_non_image: Files whose suffix is outside
            :data:`~kinoforge.core.media.DIR_IMAGE_SUFFIXES`.
    """

    source_dir: Path
    output_dir: Path
    items: tuple[ImageDirItem, ...]
    skipped_non_image: int

    @property
    def pending(self) -> tuple[ImageDirItem, ...]:
        """Items the runner will upscale."""
        return tuple(i for i in self.items if i.disposition == "pending")

    @property
    def exists(self) -> tuple[ImageDirItem, ...]:
        """Items whose output already exists."""
        return tuple(i for i in self.items if i.disposition == "exists")

    @property
    def failed_at_plan(self) -> tuple[ImageDirItem, ...]:
        """Items refused before any pod work (``oversize`` / ``unreadable``)."""
        return tuple(i for i in self.items if i.disposition in ("oversize", "unreadable"))


def output_dir_for(source_dir: Path) -> Path:
    """Return the sibling output directory for *source_dir*.

    ``photos`` and ``photos/`` both give ``<parent>/photos_upscaled``.

    Args:
        source_dir: The input directory (relative paths resolve from cwd).

    Returns:
        The resolved output directory; it may not exist yet.

    Raises:
        ValueError: *source_dir* is a filesystem root.
    """
    src = Path(source_dir).resolve()
    if src.parent == src:
        raise ValueError(f"cannot upscale a filesystem root: {src}")
    return src.parent / f"{src.name}{OUTPUT_DIR_SUFFIX}"


def register_heif() -> bool:
    """Register Pillow's HEIF/HEIC opener if ``pillow-heif`` is importable.

    Returns:
        ``True`` when registered, ``False`` when the package is absent (the
        ``.heic`` / ``.heif`` suffixes stay recognised and such files become
        ``unreadable`` items).
    """
    try:
        import pillow_heif
    except ImportError:
        return False
    pillow_heif.register_heif_opener()
    return True


def read_image_header(path: Path) -> ImageHeader:
    """Read size, mode, format and EXIF orientation without decoding pixels.

    Args:
        path: An image file.

    Returns:
        The header, with width/height swapped for the 90° orientations.

    Raises:
        OSError: Pillow cannot open the file (``UnidentifiedImageError`` is
            an ``OSError``); other Pillow failures propagate as raised.
    """
    from PIL import Image

    register_heif()
    with Image.open(path) as im:
        orientation = int(im.getexif().get(_ORIENTATION_TAG, 1) or 1)
        width, height = im.size
        if orientation in _ROTATED_ORIENTATIONS:
            width, height = height, width
        return ImageHeader(
            width=width, height=height, mode=im.mode, format=im.format, orientation=orientation
        )


def output_megapixels(width: int, height: int, scale: int) -> float:
    """Return ``width × height × scale²`` in megapixels."""
    return width * height * scale * scale / 1e6


def walk_images(source_dir: Path) -> tuple[list[Path], int]:
    """Recursively list image files under *source_dir* in sorted order.

    Dot-prefixed names are skipped at every level, symlinks are never
    followed, and files outside :data:`DIR_IMAGE_SUFFIXES` are counted.

    Args:
        source_dir: Directory to walk.

    Returns:
        ``(image paths in walk order, non-image file count)``.
    """
    images: list[Path] = []
    skipped = 0

    def _walk(directory: Path) -> None:
        nonlocal skipped
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            if entry.is_dir():
                _walk(entry)
            elif entry.is_file():
                if entry.suffix.lower() in DIR_IMAGE_SUFFIXES:
                    images.append(entry)
                else:
                    skipped += 1

    _walk(source_dir)
    return images, skipped


def _output_names(paths: list[Path]) -> dict[Path, tuple[str, bool]]:
    """Map each source to ``(output filename, renamed)`` per the §3.2 rule."""
    by_folder: dict[Path, Counter[str]] = {}
    for p in paths:
        by_folder.setdefault(p.parent, Counter())[p.stem] += 1
    names: dict[Path, tuple[str, bool]] = {}
    for p in paths:
        if by_folder[p.parent][p.stem] > 1:
            names[p] = (f"{p.name}.png", True)
        else:
            names[p] = (f"{p.stem}.png", False)
    return names


def plan_image_dir(
    source_dir: Path, *, scale: int, max_output_megapixels: int
) -> ImageDirPlan:
    """Build the plan for *source_dir*.

    Per file, in order: header read (``unreadable`` on failure), existing
    output (``exists``), the megapixel guard (``oversize``), else ``pending``.

    Args:
        source_dir: The input directory.
        scale: Integer upscale factor (the guard bounds OUTPUT pixels).
        max_output_megapixels: ``cfg.upscale.max_output_megapixels``.

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
    names = _output_names(paths)
    renamed_folders: set[Path] = set()
    items: list[ImageDirItem] = []
    for p in paths:
        name, renamed = names[p]
        if renamed and p.parent not in renamed_folders:
            renamed_folders.add(p.parent)
            _log.warning("%s: stem collision; colliding files keep their full name + .png", p.parent)
        output = out_dir / p.parent.relative_to(src) / name
        try:
            hdr = read_image_header(p)
        except Exception as exc:  # noqa: BLE001 — any Pillow failure is "unreadable"
            items.append(
                ImageDirItem(p, output, 0, 0, "unreadable", f"{type(exc).__name__}: {exc}", renamed)
            )
            continue
        if output.exists():
            disposition: Disposition = "exists"
            reason: str | None = None
        elif output_megapixels(hdr.width, hdr.height, scale) > max_output_megapixels:
            disposition = "oversize"
            reason = (
                f"{hdr.width}x{hdr.height} at {scale}x -> "
                f"{output_megapixels(hdr.width, hdr.height, scale):.1f} MP exceeds "
                f"upscale.max_output_megapixels={max_output_megapixels}"
            )
        else:
            disposition, reason = "pending", None
        items.append(ImageDirItem(p, output, hdr.width, hdr.height, disposition, reason, renamed))
    return ImageDirPlan(src, out_dir, tuple(items), skipped)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/core/test_image_dir_plan.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/image_dir.py tests/core/test_image_dir_plan.py
git commit -m "feat(image-dir): planner — recursive walk, mirrored naming, header read, megapixel guard"
```

---

### Task 4: `prepare_upload` — controller-side conversion

**Goal:** Convert one planned input into bytes the pod accepts: passthrough for identity-oriented 8-bit PNG/JPEG, otherwise an EXIF-transposed 8-bit RGB PNG in a scratch directory.

**Files:**
- Modify: `src/kinoforge/core/image_dir.py` (append)
- Test: `tests/core/test_image_dir_prepare.py` (new)

**Acceptance Criteria:**
- [ ] An `RGB` PNG with orientation 1 returns its own path (byte-identical, nothing written to scratch); same for `RGBA` PNG, `L` PNG and `RGB` JPEG.
- [ ] A JPEG with orientation 6 returns a new PNG in scratch whose pixels equal the fixture rotated as viewed (`ImageOps.exif_transpose`), size swapped.
- [ ] A 16-bit (`I;16`) PNG, a `P` PNG, an RGBA WebP, an AVIF and a BMP all return new 8-bit `RGB` PNGs in scratch.
- [ ] A two-frame GIF returns a PNG equal to frame 0.
- [ ] The scratch file is named `<first 8 hex of the source sha256>.png`.

**Verify:** `pixi run pytest tests/core/test_image_dir_prepare.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_image_dir_prepare.py`:

```python
"""`prepare_upload`: passthrough vs controller-side re-encode to 8-bit RGB PNG."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageOps

from kinoforge.core.image_dir import ImageDirItem, prepare_upload


def _item(src: Path) -> ImageDirItem:
    return ImageDirItem(src, src.parent / "out.png", 1, 1, "pending")


def _rgb(size: tuple[int, int] = (6, 4)) -> Image.Image:
    rng = np.random.default_rng(0)
    return Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8))


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    d = tmp_path / "scratch"
    d.mkdir()
    return d


class TestPassthrough:
    @pytest.mark.parametrize(
        ("name", "mode"), [("a.png", "RGB"), ("b.png", "RGBA"), ("c.png", "L"), ("d.jpg", "RGB")]
    )
    def test_identity_oriented_8bit_png_jpeg_is_untouched(
        self, tmp_path: Path, scratch: Path, name: str, mode: str
    ) -> None:
        # Bug caught: re-encoding everything changes the sha the pod
        # cross-checks and costs a decode per file for nothing.
        src = tmp_path / name
        _rgb().convert(mode).save(src)
        before = src.read_bytes()
        assert prepare_upload(_item(src), scratch) == src
        assert src.read_bytes() == before
        assert list(scratch.iterdir()) == []


class TestReencode:
    def test_orientation_is_applied(self, tmp_path: Path, scratch: Path) -> None:
        # Bug caught: a phone portrait is upscaled lying on its side, because
        # the pod's imageio read ignores EXIF orientation.
        src = tmp_path / "o.jpg"
        im = _rgb((40, 20))
        exif = im.getexif()
        exif[0x0112] = 6
        im.save(src, exif=exif, quality=100, subsampling=0)
        out = prepare_upload(_item(src), scratch)
        assert out != src and out.parent == scratch and out.suffix == ".png"
        with Image.open(out) as got, Image.open(src) as orig:
            expected = ImageOps.exif_transpose(orig).convert("RGB")
            assert got.size == (20, 40)
            assert got.mode == "RGB"
            assert np.array_equal(np.asarray(got), np.asarray(expected))

    @pytest.mark.parametrize(
        ("name", "build"),
        [
            ("sixteen.png", lambda: Image.new("I;16", (6, 4), 1000)),
            ("pal.png", lambda: _rgb().convert("P", palette=Image.Palette.ADAPTIVE)),
            ("alpha.webp", lambda: _rgb().convert("RGBA")),
            ("x.avif", _rgb),
            ("x.bmp", _rgb),
        ],
    )
    def test_other_formats_become_8bit_rgb_png(
        self, tmp_path: Path, scratch: Path, name: str, build: object
    ) -> None:
        # Bug caught: 16-bit bytes reach the pod (its _to_rgb raises), or
        # a WebP is uploaded under a content type /upload rejects with 415.
        src = tmp_path / name
        build().save(src)  # type: ignore[operator]
        out = prepare_upload(_item(src), scratch)
        assert out.parent == scratch and out.suffix == ".png"
        with Image.open(out) as got:
            assert got.format == "PNG" and got.mode == "RGB" and got.size == (6, 4)

    def test_animated_gif_uses_frame_zero(self, tmp_path: Path, scratch: Path) -> None:
        # Bug caught: the last frame, or a Pillow error on seek.
        src = tmp_path / "anim.gif"
        f0 = Image.new("RGB", (4, 4), (200, 0, 0))
        f1 = Image.new("RGB", (4, 4), (0, 200, 0))
        f0.save(src, save_all=True, append_images=[f1])
        out = prepare_upload(_item(src), scratch)
        with Image.open(out) as got:
            r, g, b = got.getpixel((0, 0))  # type: ignore[misc]
            assert r > 150 and g < 50

    def test_scratch_name_is_sha8(self, tmp_path: Path, scratch: Path) -> None:
        # Bug caught: two converted inputs with the same stem overwrite each
        # other inside one run.
        src = tmp_path / "x.webp"
        _rgb().save(src)
        out = prepare_upload(_item(src), scratch)
        assert out.name == hashlib.sha256(src.read_bytes()).hexdigest()[:8] + ".png"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/core/test_image_dir_prepare.py -q`
Expected: FAIL — `ImportError: cannot import name 'prepare_upload'`.

- [ ] **Step 3: Implement**

Append to `src/kinoforge/core/image_dir.py`:

```python
_PASSTHROUGH_SUFFIXES = frozenset({".png", ".jpg", ".jpeg"})
_PASSTHROUGH_MODES = frozenset({"RGB", "RGBA", "L"})


def _sha8(path: Path) -> str:
    """First 8 hex digits of the file's sha256."""
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def prepare_upload(item: ImageDirItem, scratch: Path) -> Path:
    """Return the path to upload for *item*: the source itself, or a converted PNG.

    The pod accepts only PNG/JPEG, 8-bit, and reads them without applying
    EXIF orientation. A PNG or JPEG whose mode is ``RGB`` / ``RGBA`` / ``L``
    and whose orientation is the identity is uploaded byte for byte.
    Everything else is opened, EXIF-transposed, converted to 8-bit ``RGB``
    and written as ``<sha8>.png`` under *scratch*. Multi-frame inputs
    contribute frame 0.

    Args:
        item: A planned item (``pending``).
        scratch: Per-run scratch directory; the caller removes it.

    Returns:
        The path whose bytes go to the pod.
    """
    from PIL import Image, ImageOps

    register_heif()
    with Image.open(item.source) as im:
        orientation = int(im.getexif().get(_ORIENTATION_TAG, 1) or 1)
        if (
            item.source.suffix.lower() in _PASSTHROUGH_SUFFIXES
            and im.mode in _PASSTHROUGH_MODES
            and orientation == 1
        ):
            return item.source
        src_mode = im.mode
        n_frames = int(getattr(im, "n_frames", 1))
        if n_frames > 1:
            _log.info("%s: %d frames; upscaling frame 0 only", item.source.name, n_frames)
            im.seek(0)
        transposed = ImageOps.exif_transpose(im)
        rgb = (transposed if transposed is not None else im).convert("RGB")
    out = scratch / f"{_sha8(item.source)}.png"
    rgb.save(out, format="PNG")
    _log.info("%s: re-encoded to %s (%s -> RGB PNG)", item.source.name, out.name, src_mode)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/core/test_image_dir_prepare.py tests/core/test_image_dir_plan.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/image_dir.py tests/core/test_image_dir_prepare.py
git commit -m "feat(image-dir): prepare_upload — passthrough or EXIF-transposed 8-bit RGB PNG"
```

---

### Task 5: Extract `fetch_artifact_bytes` from the orchestrator's materialize block

**Goal:** The "read an upscaled artifact's bytes from a pod proxy URL or a `file://` uri" logic becomes one public function both `generate` and the directory runner call.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:3085-3097` (inside the `_needs_materialize` block) and a new module-level function near `_cfg_dict` (~line 230)
- Test: `tests/core/test_orchestrator_fetch_artifact_bytes.py` (new); `tests/core/test_orchestrator_publish_media.py` and `tests/core/test_orchestrator_upscale_chunk.py` must stay green

**Acceptance Criteria:**
- [ ] `fetch_artifact_bytes(Artifact(uri="file:///…"))` returns the file's bytes.
- [ ] `fetch_artifact_bytes(Artifact(uri="https://…"))` sends a GET with `User-Agent: kinoforge-orchestrator/0.1` and a 600 s timeout through `urllib.request.urlopen` and returns the body.
- [ ] `generate`'s materialize block calls it (the inline `_urequest` block is gone) and every existing orchestrator test passes.

**Verify:** `pixi run pytest tests/core/test_orchestrator_fetch_artifact_bytes.py tests/core/test_orchestrator_publish_media.py tests/core/test_orchestrator_upscale_chunk.py tests/core/test_orchestrator.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_orchestrator_fetch_artifact_bytes.py`:

```python
"""`fetch_artifact_bytes`: one reader for pod-proxy URLs and file:// uris."""

from __future__ import annotations

import io
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from kinoforge.core.interfaces import Artifact
from kinoforge.core.orchestrator import fetch_artifact_bytes


def test_file_uri_reads_bytes(tmp_path: Path) -> None:
    p = tmp_path / "up.png"
    p.write_bytes(b"png-bytes")
    assert fetch_artifact_bytes(Artifact(uri=f"file://{p}")) == b"png-bytes"


def test_http_uri_gets_with_kinoforge_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bug caught: the default Python-urllib UA is 403'd by RunPod's
    # Cloudflare edge; a missing timeout hangs on a dead pod forever.
    seen: dict[str, Any] = {}

    class _Resp(io.BytesIO):
        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *a: Any) -> None:
            pass

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> _Resp:
        seen["url"] = req.full_url
        seen["ua"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        return _Resp(b"body")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = fetch_artifact_bytes(Artifact(uri="https://pod-8000.proxy.runpod.net/artifacts/x.png"))
    assert out == b"body"
    assert seen["url"].endswith("/artifacts/x.png")
    assert seen["ua"] == "kinoforge-orchestrator/0.1"
    assert seen["timeout"] == 600
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pixi run pytest tests/core/test_orchestrator_fetch_artifact_bytes.py -q`
Expected: FAIL — `ImportError: cannot import name 'fetch_artifact_bytes'`.

- [ ] **Step 3: Implement**

In `src/kinoforge/core/orchestrator.py`, add after `_cfg_dict`:

```python
def fetch_artifact_bytes(artifact: Artifact) -> bytes:
    """Read an artifact's bytes from a pod proxy URL or a ``file://`` uri.

    Used by :func:`generate`'s materialize step and by
    :func:`kinoforge.core.upscale_dir.upscale_image_dir`, so a pod-served
    result is fetched the same way — same User-Agent (RunPod's Cloudflare
    edge 403s the default urllib one) and the same 600 s timeout — wherever
    it is consumed.

    Args:
        artifact: Its ``uri`` is ``http(s)://…`` or ``file://…``.

    Returns:
        The raw bytes.
    """
    if artifact.uri.startswith(("http://", "https://")):
        import urllib.request as _urequest  # orchestrator stays urllib-free at module level

        _log.info("materializing artifact from %s", artifact.uri)
        req = _urequest.Request(  # noqa: S310 — pod proxy URL only
            artifact.uri,
            headers={"User-Agent": "kinoforge-orchestrator/0.1"},
        )
        with _urequest.urlopen(req, timeout=600) as resp:  # noqa: S310
            return bytes(resp.read())
    return Path(artifact.uri.removeprefix("file://")).read_bytes()
```

Then in the materialize block (~line 3085), replace

```python
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
```

with

```python
            body: bytes = fetch_artifact_bytes(upscaled)
```

Leave the interpolated-artifact fetch further down untouched (it has its own log wording and tests; not this feature).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/core/test_orchestrator_fetch_artifact_bytes.py tests/core/test_orchestrator_publish_media.py tests/core/test_orchestrator_upscale_chunk.py tests/core/test_orchestrator.py -q`
Expected: all PASS. If an existing test monkeypatched `urllib.request.urlopen` expecting the inline block, it still works — the helper goes through the same `urllib.request.urlopen` name.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_fetch_artifact_bytes.py
git commit -m "refactor(orchestrator): extract fetch_artifact_bytes from the materialize block"
```

---

### Task 6: `core/upscale_dir.py` — the session runner

**Goal:** `upscale_image_dir()` opens `deploy_session` once, runs `UpscaleStage` per pending item, writes each output atomically, continues past per-item failures, probes `/health` after a failure, aborts the rest when the pod is gone, and lets `single=True` destroy the pod once at session exit.

**Files:**
- Create: `src/kinoforge/core/upscale_dir.py`
- Test: `tests/core/test_upscale_dir.py` (new)

**Acceptance Criteria:**
- [ ] With three pending items and a stubbed `deploy_session`, the session is entered exactly once, `on_item` fires exactly once per pending item in order, and three outputs exist with the stub's bytes under created parent directories; the scratch directory is gone afterwards.
- [ ] Item 2 raising `RuntimeError` → items 1 and 3 written, outcome `failed` with the exception text as reason, `/health` probed exactly once, result counts `written=2 failed=1 aborted=0`.
- [ ] Health probe raising after a failure → remaining items reported `aborted` through `on_item`, `PodDead` raised naming the pod id, no further `stage.run` calls.
- [ ] `single=True` is forwarded to `deploy_session` unchanged (the destroy is its job) and the session is entered once even when an item failed.
- [ ] `Cancelled` from the stage → the rest reported `aborted`, `Cancelled` re-raised; `BudgetExceeded` / `CapabilityMismatch` / `TeardownError` likewise re-raised with the rest `aborted`.
- [ ] With zero pending items, `deploy_session` is never entered and an empty result is returned.
- [ ] `EphemeralSession.register_store` is called when an ephemeral session is active (mirror `generate`).

**Verify:** `pixi run pytest tests/core/test_upscale_dir.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_upscale_dir.py`:

```python
"""`upscale_image_dir`: one deploy_session, one UpscaleStage run per image."""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

import kinoforge._adapters  # noqa: F401 — self-register upscalers
from kinoforge.core import upscale_dir as mod
from kinoforge.core.config import Config
from kinoforge.core.errors import BudgetExceeded, Cancelled, KinoforgeError
from kinoforge.core.image_dir import plan_image_dir
from kinoforge.core.interfaces import Artifact, Instance, PipelineState, UpscaleJob, UpscaleResult
from kinoforge.core.orchestrator import DeploySession
from kinoforge.core.upscale_dir import PodDead, upscale_image_dir


def _cfg() -> Config:
    return Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp8"},
            "models": [
                {"kind": "base", "ref": "hf:Wan-AI/Wan2.2-T2V", "target": "diffusion_models"}
            ],
            "compute": {"provider": "fake", "image": "fake:latest"},
            "upscale": {
                "engine": "spandrel",
                "scale": "2x",
                "spandrel": {
                    "model_url": "hf:foo/bar.pth",
                    "arch": "realesrgan",
                    "precision": "fp16",
                    "tile_size": 512,
                    "batch_size": 4,
                },
            },
        }
    )


def _instance() -> Instance:
    return Instance(
        id="pod-1",
        provider="fake",
        status="ready",
        created_at=0.0,
        endpoints={"8000": "https://pod-1-8000.proxy.runpod.net"},
    )


class _Upscaler:
    """Fake UpscalerEngine: returns a file:// artifact of fixed bytes per call."""

    supports_image_input = True

    def __init__(self, tmp: Path, fail_on: set[int] = frozenset(), raise_cls: type[BaseException] = RuntimeError) -> None:
        self.tmp = tmp
        self.calls: list[UpscaleJob] = []
        self.fail_on = fail_on
        self.raise_cls = raise_cls

    def upscale(self, instance: Any, job: UpscaleJob, cfg: Any, *, cancel_token: Any = None) -> UpscaleResult:
        self.calls.append(job)
        n = len(self.calls)
        if n in self.fail_on:
            raise self.raise_cls(f"boom on call {n}")
        out = self.tmp / f"result{n}.png"
        out.write_bytes(f"upscaled-{n}".encode())
        return UpscaleResult(
            artifact=Artifact(uri=f"file://{out}", meta={"media": "image"}),
            input_resolution=(4, 4),
            output_resolution=(8, 8),
            elapsed_s=0.0,
        )


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub deploy_session; record entries and the kwargs it received."""
    rec: dict[str, Any] = {"entries": 0, "kwargs": None}
    sess = DeploySession(
        backend=MagicMock(), profile=MagicMock(), pool=MagicMock(),
        instance=_instance(), engine=MagicMock(name="GenerationEngine"), provider=MagicMock(),
    )

    @contextmanager
    def fake_deploy(cfg: Any, **kwargs: Any) -> Any:
        rec["entries"] += 1
        rec["kwargs"] = kwargs
        yield sess

    from kinoforge.core import orchestrator

    monkeypatch.setattr(orchestrator, "deploy_session", fake_deploy)
    return rec


def _src(tmp_path: Path, names: list[str]) -> Path:
    src = tmp_path / "photos"
    for n in names:
        p = src / n
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), 0).save(p)
    return src


def _run(tmp_path: Path, src: Path, upscaler: _Upscaler, *, probe: Any = None, single: bool = False) -> tuple[Any, list[tuple[str, str, str | None]]]:
    plan = plan_image_dir(src, scale=2, max_output_megapixels=256)
    seen: list[tuple[str, str, str | None]] = []
    result = upscale_image_dir(
        _cfg(), plan, store=MagicMock(), run_id="r", state_dir=tmp_path / ".kf",
        upscaler=upscaler, single=single,
        on_item=lambda item, outcome, reason: seen.append((item.source.name, outcome, reason)),
        health_probe=probe if probe is not None else (lambda url: {"ok": True}),
    )
    return result, seen


def test_one_session_three_items_written(tmp_path: Path, session: dict[str, Any]) -> None:
    # Bug caught: one deploy_session per image (the loop-over-generate
    # shape), outputs not mirrored, scratch dir leaked.
    src = _src(tmp_path, ["a.png", "sub/b.webp", "c.png"])
    up = _Upscaler(tmp_path)
    (result, _), seen = _run(tmp_path, src, up)
    assert session["entries"] == 1
    assert [s[1] for s in seen] == ["written", "written", "written"]
    out = tmp_path / "photos_upscaled"
    assert (out / "a.png").read_bytes() == b"upscaled-1"
    assert (out / "sub" / "b.png").read_bytes() == b"upscaled-2"
    assert (out / "c.png").read_bytes() == b"upscaled-3"
    assert result.written == 3 and result.failed == 0 and result.aborted == 0
    assert all(j.media == "image" for j in up.calls)
    import tempfile

    assert not any(p.name.startswith("kf-image-dir-") for p in Path(tempfile.gettempdir()).iterdir())


def test_per_item_failure_continues_and_probes_once(tmp_path: Path, session: dict[str, Any]) -> None:
    # Bug caught: stop on first failure; or no health probe after one.
    src = _src(tmp_path, ["a.png", "b.png", "c.png"])
    probes: list[str] = []

    def probe(url: str) -> dict[str, bool]:
        probes.append(url)
        return {"ok": True}

    up = _Upscaler(tmp_path, fail_on={2})
    (result, _), seen = _run(tmp_path, src, up, probe=probe)
    assert [s[1] for s in seen] == ["written", "failed", "written"]
    assert "boom on call 2" in (seen[1][2] or "")
    assert probes == ["https://pod-1-8000.proxy.runpod.net/health"]
    assert result.written == 2 and result.failed == 1
    assert not (tmp_path / "photos_upscaled" / "b.png").exists()


def test_dead_pod_aborts_the_rest(tmp_path: Path, session: dict[str, Any]) -> None:
    # Bug caught: paying an upload timeout per remaining file against a
    # pod that is gone.
    src = _src(tmp_path, ["a.png", "b.png", "c.png", "d.png"])

    def probe(url: str) -> dict[str, bool]:
        raise OSError("connection refused")

    up = _Upscaler(tmp_path, fail_on={2})
    with pytest.raises(PodDead, match="pod-1"):
        _run(tmp_path, src, up, probe=probe)
    assert len(up.calls) == 2


def test_dead_pod_reports_aborted_items_via_on_item(tmp_path: Path, session: dict[str, Any]) -> None:
    src = _src(tmp_path, ["a.png", "b.png", "c.png", "d.png"])
    seen: list[tuple[str, str]] = []
    plan = plan_image_dir(src, scale=2, max_output_megapixels=256)

    def probe(url: str) -> dict[str, bool]:
        raise OSError("down")

    with pytest.raises(PodDead):
        upscale_image_dir(
            _cfg(), plan, store=MagicMock(), run_id="r", state_dir=tmp_path / ".kf",
            upscaler=_Upscaler(tmp_path, fail_on={2}),
            on_item=lambda item, outcome, reason: seen.append((item.source.name, outcome)),
            health_probe=probe,
        )
    assert seen == [("a.png", "written"), ("b.png", "failed"), ("c.png", "aborted"), ("d.png", "aborted")]


@pytest.mark.parametrize("exc", [Cancelled("stop"), BudgetExceeded("over")])
def test_cancel_and_fatal_abort_the_rest_and_reraise(tmp_path: Path, session: dict[str, Any], exc: KinoforgeError) -> None:
    # Bug caught: a cancel swallowed as a per-item failure, so the run keeps
    # uploading after Ctrl-C.
    src = _src(tmp_path, ["a.png", "b.png", "c.png"])
    seen: list[tuple[str, str]] = []
    plan = plan_image_dir(src, scale=2, max_output_megapixels=256)
    up = _Upscaler(tmp_path, fail_on={2}, raise_cls=type(exc))
    with pytest.raises(type(exc)):
        upscale_image_dir(
            _cfg(), plan, store=MagicMock(), run_id="r", state_dir=tmp_path / ".kf",
            upscaler=up, on_item=lambda item, outcome, reason: seen.append((item.source.name, outcome)),
            health_probe=lambda url: {"ok": True},
        )
    assert seen == [("a.png", "written"), ("b.png", "aborted"), ("c.png", "aborted")]
    assert len(up.calls) == 2


def test_single_is_forwarded_and_session_entered_once_despite_failure(tmp_path: Path, session: dict[str, Any]) -> None:
    # Bug caught: --no-reuse destroying per item, or not at all after a
    # failure. The destroy itself is deploy_session's (tested there).
    src = _src(tmp_path, ["a.png", "b.png"])
    _run(tmp_path, src, _Upscaler(tmp_path, fail_on={1}), single=True)
    assert session["entries"] == 1
    assert session["kwargs"]["single"] is True


def test_no_pending_items_never_opens_a_session(tmp_path: Path, session: dict[str, Any]) -> None:
    src = _src(tmp_path, ["a.png"])
    (tmp_path / "photos_upscaled").mkdir()
    Image.new("RGB", (4, 4), 0).save(tmp_path / "photos_upscaled" / "a.png")
    (result, inst), seen = _run(tmp_path, src, _Upscaler(tmp_path))
    assert session["entries"] == 0 and seen == [] and result.written == 0


def test_stage_receives_image_media_and_no_tiling(tmp_path: Path, session: dict[str, Any]) -> None:
    # Bug caught: the stage built with tile_grid/chunk_frames from cfg
    # would try to ffprobe a PNG.
    src = _src(tmp_path, ["a.png"])
    up = _Upscaler(tmp_path)
    _run(tmp_path, src, up)
    assert up.calls[0].media == "image" and up.calls[0].scale.value == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/core/test_upscale_dir.py -q`
Expected: FAIL at import — `ModuleNotFoundError: No module named 'kinoforge.core.upscale_dir'`.

- [ ] **Step 3: Implement**

Create `src/kinoforge/core/upscale_dir.py`:

```python
"""Upscale every planned still in a directory on ONE pod.

The shape is :mod:`kinoforge.core.batch`: open :func:`deploy_session` once
and do per-item work inside it, so the boot, the profile verify and the
heartbeat are paid once per directory rather than once per image. ``single``
(``--no-reuse``) is forwarded to the session, whose ``finally`` destroys the
pod exactly once at exit — whatever happened in between.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from kinoforge.core.cancel import CancelToken
from kinoforge.core.config import Config
from kinoforge.core.errors import (
    BudgetExceeded,
    Cancelled,
    CapabilityMismatch,
    KinoforgeError,
    TeardownError,
)
from kinoforge.core.image_dir import ImageDirItem, ImageDirPlan, prepare_upload
from kinoforge.core.interfaces import (
    ComputeProvider,
    GenerationEngine,
    GenerationRequest,
    Instance,
    PipelineState,
    UpscalerEngine,
)
from kinoforge.core.media import local_artifact
from kinoforge.stores.base import ArtifactStore

_log = logging.getLogger("kinoforge.core.upscale_dir")

ItemOutcome = Literal["written", "failed", "aborted"]
"""Terminal state of one pending item."""

ItemCallback = Callable[[ImageDirItem, ItemOutcome, str | None], None]
"""``on_item(item, outcome, reason)`` — fires exactly once per pending item, in order."""

_FATAL: tuple[type[BaseException], ...] = (
    KeyboardInterrupt,
    Cancelled,
    BudgetExceeded,
    CapabilityMismatch,
    TeardownError,
)
_HEALTH_USER_AGENT = "kinoforge-upscale-dir/0.1"


class PodDead(KinoforgeError):
    """The pod stopped answering ``/health`` after an item failed; the rest were aborted."""


@dataclass(frozen=True)
class ImageDirResult:
    """Per-item outcomes for one directory run.

    Attributes:
        plan: The plan that was executed.
        outcomes: ``(item, outcome, reason)`` for every pending item, in order.
    """

    plan: ImageDirPlan
    outcomes: tuple[tuple[ImageDirItem, ItemOutcome, str | None], ...]

    def _count(self, outcome: ItemOutcome) -> int:
        return sum(1 for _, o, _ in self.outcomes if o == outcome)

    @property
    def written(self) -> int:
        """Items whose output was written."""
        return self._count("written")

    @property
    def failed(self) -> int:
        """Items that raised and were skipped."""
        return self._count("failed")

    @property
    def aborted(self) -> int:
        """Items never attempted because the run stopped."""
        return self._count("aborted")


def _default_health_probe(url: str) -> dict[str, Any]:
    from kinoforge.engines._pod_http import http_json

    return http_json(method="GET", url=url, payload=None, user_agent=_HEALTH_USER_AGENT)


def _health_url(instance: Instance | None) -> str | None:
    if instance is None:
        return None
    endpoints = instance.endpoints or {}
    base = endpoints.get("8000") or next(iter(endpoints.values()), "")
    return f"{base.rstrip('/')}/health" if base else None


def _write_atomic(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def upscale_image_dir(
    cfg: Config,
    plan: ImageDirPlan,
    *,
    store: ArtifactStore,
    run_id: str,
    state_dir: Path,
    instance: Instance | None = None,
    cancel_token: CancelToken | None = None,
    single: bool = False,
    on_instance_created: Callable[[Instance], None] | None = None,
    on_item: ItemCallback | None = None,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    upscaler: UpscalerEngine | None = None,
    health_probe: Callable[[str], Any] = _default_health_probe,
) -> tuple[ImageDirResult, Instance | None]:
    """Upscale every ``pending`` item of *plan* inside one deploy session.

    Per item: :func:`prepare_upload`, seed a ``PipelineState`` with the
    input artifact (``meta["media"] = "image"``), run the shared
    ``UpscaleStage``, fetch the result bytes, write them atomically to
    ``item.output``. A per-item exception is recorded as ``failed`` and the
    run continues; after any failure the pod's ``/health`` is probed once and,
    if it does not answer, the rest are ``aborted`` and :class:`PodDead` is
    raised. ``KeyboardInterrupt``, ``Cancelled`` and the batch-fatal errors
    abort the rest and re-raise.

    Args:
        cfg: Loaded config; ``cfg.upscale`` must be present.
        plan: From :func:`kinoforge.core.image_dir.plan_image_dir`.
        store: Artifact store (profile cache, ephemeral registration).
        run_id: Namespace tag forwarded to the session.
        state_dir: kinoforge state root.
        instance: Pre-resolved warm pod, or ``None`` to cold-create.
        cancel_token: Cooperative cancellation (the CLI's SIGINT token).
        single: ``--no-reuse`` — the session destroys the pod once at exit.
        on_instance_created: Forwarded to the session (launch-row upgrade).
        on_item: Fires once per pending item, in order, including the
            ``aborted`` ones before an exception propagates.
        provider: Test-injection ``ComputeProvider`` for the session.
        engine: Test-injection ``GenerationEngine`` for the session.
        upscaler: Test-injection ``UpscalerEngine``; defaults to the
            registry's ``cfg.upscale.engine``.
        health_probe: ``(url) -> json``; raises when the pod is unreachable.

    Returns:
        ``(result, instance)`` — the session's instance so the CLI can stamp
        the ledger exactly as it does for a single image.

    Raises:
        PodDead: The pod stopped answering after an item failed.
        Cancelled: The cancel token fired mid-run.
        BudgetExceeded, CapabilityMismatch, TeardownError: Batch-fatal.
    """
    from kinoforge.core import orchestrator as _orch
    from kinoforge.core import registry as _registry
    from kinoforge.core.ephemeral import EphemeralSession
    from kinoforge.core.scale_target import ScaleTarget
    from kinoforge.pipeline.upscale import UpscaleStage

    if cfg.upscale is None:
        raise ValueError("upscale_image_dir needs an `upscale:` block")
    pending = plan.pending
    outcomes: list[tuple[ImageDirItem, ItemOutcome, str | None]] = []

    def _emit(item: ImageDirItem, outcome: ItemOutcome, reason: str | None) -> None:
        outcomes.append((item, outcome, reason))
        if on_item is not None:
            on_item(item, outcome, reason)

    def _abort_rest(items: tuple[ImageDirItem, ...], reason: str) -> None:
        for it in items:
            _emit(it, "aborted", reason)

    if not pending:
        return ImageDirResult(plan, ()), instance

    with _orch.deploy_session(
        cfg,
        store=store,
        provider=provider,
        engine=engine,
        run_id=run_id,
        state_dir=state_dir,
        instance=instance,
        cancel_token=cancel_token,
        single=single,
        on_instance_created=on_instance_created,
    ) as session:
        eph = EphemeralSession.current()
        if eph is not None:
            eph.register_store(store, run_id)
        up = upscaler if upscaler is not None else _registry.get_upscaler(cfg.upscale.engine)()
        stage = UpscaleStage(
            engine=up,
            scale=ScaleTarget.parse(cfg.upscale.scale),
            instance=session.instance,
            cfg=_orch._cfg_dict(cfg),  # noqa: SLF001 — same dict generate() hands its stages
            cancel_token=cancel_token,
        )
        with tempfile.TemporaryDirectory(prefix="kf-image-dir-") as scratch_str:
            scratch = Path(scratch_str)
            for idx, item in enumerate(pending):
                try:
                    upload = prepare_upload(item, scratch)
                    state = PipelineState(
                        request=GenerationRequest(prompt="", mode="upscale"),
                        artifacts={"clip": local_artifact(upload, "image")},
                    )
                    state = stage.run(state)
                    body = _orch.fetch_artifact_bytes(state.artifacts["upscaled"])
                    _write_atomic(item.output, body)
                except _FATAL as exc:
                    _abort_rest(pending[idx:], type(exc).__name__)
                    raise
                except Exception as exc:  # noqa: BLE001 — one bad file must not end the run
                    reason = f"{type(exc).__name__}: {exc}"
                    _log.warning("%s failed: %s", item.source, reason)
                    _emit(item, "failed", reason)
                    url = _health_url(session.instance)
                    if url is not None:
                        try:
                            health_probe(url)
                        except Exception as probe_exc:  # noqa: BLE001 — unreachable is the signal
                            rest = pending[idx + 1 :]
                            _abort_rest(rest, "pod stopped answering /health")
                            pod_id = session.instance.id if session.instance else "<none>"
                            raise PodDead(
                                f"pod {pod_id} stopped answering /health after "
                                f"{item.source.name} failed ({type(probe_exc).__name__}); "
                                f"{len(rest)} item(s) aborted"
                            ) from exc
                    continue
                _emit(item, "written", None)
        return ImageDirResult(plan, tuple(outcomes)), session.instance
```

If ruff does not enable `SLF`, drop that `noqa` comment (ruff will flag an unused one under `RUF100` only if that rule is on; the repo's select list is `F E W B B9 UP I ANN D S`, so neither applies — the comment is documentation and may stay, or be removed).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pixi run pytest tests/core/test_upscale_dir.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/core/upscale_dir.py tests/core/test_upscale_dir.py
git commit -m "feat(upscale-dir): one-session runner — per-item continue, health probe, abort on dead pod"
```

---

### Task 7: CLI — `--image-dir`, the directory branch, `_finish_launch_row`, the single-`--image` guard

**Goal:** `kinoforge upscale --image-dir DIR` is wired end to end: argparse, preflight refusals, dry run, nothing-to-do, the warm-reuse / attach / launch-row chain (shared through one `_finish_launch_row` helper), progress lines, the closing summary and exit codes. Single `--image` gains the megapixel refusal.

**Files:**
- Modify: `src/kinoforge/cli/_main.py:709-724` (the `upscale` source group)
- Modify: `src/kinoforge/cli/_commands.py` — `_cmd_upscale` (lines 1095-1240), new `_cmd_upscale_image_dir`, `_print_image_dir_plan`, `_print_image_dir_summary`, `_finish_launch_row`, `_image_megapixel_error`
- Test: `tests/cli/test_cmd_upscale_image_dir.py` (new); `tests/cli/test_cmd_upscale_image.py` (two added cases); existing `tests/cli/test_cmd_upscale*.py` stay green

**Acceptance Criteria:**
- [ ] `--image-dir` is in the required mutex group with `--video` and `--image` (`upscale --video a.mp4 --image-dir d` exits 2 from argparse).
- [ ] Refusals exit 2 with stderr naming the thing: empty value, missing path, a file instead of a directory, an engine without image support, `chunk_frames` / `tile_grid`, a height scale, a directory with no recognised images. All fire before `upscale_image_dir` is reached.
- [ ] `--dry-run` prints `source_dir`, `output_dir`, `media: image`, one line per item with its disposition, a counts line, exits 0, and never reaches `upscale_image_dir`.
- [ ] Every output present → `nothing to do` on stdout, exit 0, `upscale_image_dir` not reached; with plan-time failures present the summary prints and the exit is 1.
- [ ] `--output-dir X` with `--image-dir` prints the stderr note and nothing is written under `X`.
- [ ] A run where the stub reports one `failed` item prints `[n/N] <rel> FAILED: <reason>`, the summary line `upscaled 2, skipped 0 existing, failed 1, aborted 0 -> <out>`, the failed file's name, and exits 1; a clean run exits 0 with `failed 0`.
- [ ] `PodDead` from the runner → summary printed, `error: PodDead: …` on stderr, exit 1. `Cancelled` → summary, `upscale: cancelled` on stderr, exit 1. Neither settles the launch row (ruling C1).
- [ ] `_finish_launch_row` is called by both the single path (`--video` / `--image`) and the directory path; it stamps the ledger on a cold create and settles otherwise (asserted with patched `_stamp_cold_created_instance` / `_settle_unused_launch_row`).
- [ ] Single `--image` with an oversize file exits 2 naming `upscale.max_output_megapixels`; an unreadable `--image` exits 2 naming the file.

**Verify:** `pixi run pytest tests/cli/ -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

Create `tests/cli/test_cmd_upscale_image_dir.py`:

```python
"""`kinoforge upscale --image-dir`: surface, refusals, dry run, summary, exit codes.

Everything below the preflight patches ``kinoforge.core.upscale_dir.upscale_image_dir``
so no pod is ever provisioned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PIL import Image

import kinoforge._adapters  # noqa: F401 — self-register engines + upscalers
from kinoforge.cli._main import main
from kinoforge.core.image_dir import ImageDirPlan
from kinoforge.core.interfaces import Instance
from kinoforge.core.upscale_dir import ImageDirResult, PodDead

_HEAD = (
    "engine:\n"
    "  kind: diffusers\n"
    "  precision: fp8\n"
    "models:\n"
    "  - kind: base\n"
    "    ref: hf:Wan-AI/Wan2.2-T2V\n"
    "    target: diffusion_models\n"
    "compute:\n"
    "  provider: fake\n"
    "  image: fake:latest\n"
)


def _cfg(tmp_path: Path, *, scale: str = "2x", extra: str = "", engine: str = "spandrel") -> Path:
    cfg = tmp_path / "cfg.yaml"
    block = (
        "  spandrel:\n    model_url: hf:foo/bar.pth\n    arch: realesrgan\n"
        "    precision: fp16\n    tile_size: 512\n    batch_size: 4\n"
        if engine == "spandrel"
        else "  seedvr2:\n    variant: 3B\n    precision: fp8\n"
    )
    cfg.write_text(_HEAD + f"upscale:\n  engine: {engine}\n  scale: {scale}\n" + extra + block)
    return cfg


def _photos(tmp_path: Path, names: tuple[str, ...] = ("a.png", "sub/b.webp", "c.jpg")) -> Path:
    src = tmp_path / "photos"
    for n in names:
        p = src / n
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 6), 0).save(p)
    return src


def _argv(tmp_path: Path, src: Path, *more: str) -> list[str]:
    return [
        "upscale", "--image-dir", str(src), "-c", str(_cfg(tmp_path)),
        "--state-dir", str(tmp_path / ".kf"), *more,
    ]


@pytest.fixture
def no_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("upscale_image_dir must not be reached")

    monkeypatch.setattr("kinoforge.core.upscale_dir.upscale_image_dir", boom)


def _fake_runner(monkeypatch: pytest.MonkeyPatch, *, fail_names: set[str] = frozenset(), raise_exc: BaseException | None = None, instance: Instance | None = None) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake(cfg: Any, plan: ImageDirPlan, **kw: Any) -> tuple[ImageDirResult, Instance | None]:
        captured["plan"] = plan
        captured.update(kw)
        outcomes = []
        for item in plan.pending:
            if item.source.name in fail_names:
                outcome, reason = "failed", "RuntimeError: pod said no"
            else:
                outcome, reason = "written", None
            kw["on_item"](item, outcome, reason)
            outcomes.append((item, outcome, reason))
        if raise_exc is not None:
            raise raise_exc
        return ImageDirResult(plan, tuple(outcomes)), instance

    monkeypatch.setattr("kinoforge.core.upscale_dir.upscale_image_dir", fake)
    return captured


class TestArgparse:
    def test_mutually_exclusive_with_video_and_image(self, tmp_path: Path) -> None:
        # Bug caught: two sources accepted; the handler picks one silently.
        with pytest.raises(SystemExit) as exc:
            main(["upscale", "--video", "a.mp4", "--image-dir", "d", "-c", str(_cfg(tmp_path))])
        assert exc.value.code == 2


class TestRefusals:
    @pytest.mark.parametrize(
        ("value", "needle"),
        [("", "empty"), ("/nonexistent/dir", "does not exist")],
    )
    def test_bad_path_exits_2(self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str], value: str, needle: str) -> None:
        rc = main(["upscale", "--image-dir", value, "-c", str(_cfg(tmp_path)), "--state-dir", str(tmp_path / ".kf")])
        assert rc == 2
        assert needle in capsys.readouterr().err

    def test_file_instead_of_dir_exits_2(self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]) -> None:
        f = tmp_path / "x.png"
        Image.new("RGB", (4, 4)).save(f)
        rc = main(_argv(tmp_path, f))
        assert rc == 2 and "not a directory" in capsys.readouterr().err

    def test_engine_without_image_support_exits_2(self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]) -> None:
        # Bug caught: a SeedVR2 cfg boots a pod and the server 400s image jobs.
        src = _photos(tmp_path)
        rc = main(["upscale", "--image-dir", str(src), "-c", str(_cfg(tmp_path, engine="seedvr2")), "--state-dir", str(tmp_path / ".kf")])
        assert rc == 2 and "seedvr2" in capsys.readouterr().err

    @pytest.mark.parametrize("extra", ["  chunk_frames: 16\n", "  tile_grid: [2, 1]\n"])
    def test_video_splits_exit_2(self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str], extra: str) -> None:
        src = _photos(tmp_path)
        rc = main(["upscale", "--image-dir", str(src), "-c", str(_cfg(tmp_path, extra=extra)), "--state-dir", str(tmp_path / ".kf")])
        assert rc == 2 and extra.split(":")[0].strip() in capsys.readouterr().err

    def test_height_scale_exits_2(self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]) -> None:
        src = _photos(tmp_path)
        rc = main(_argv(tmp_path, src, "--scale", "1080p"))
        assert rc == 2 and "1080p" in capsys.readouterr().err

    def test_no_images_exits_2(self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]) -> None:
        # Bug caught: an empty (wrong) directory boots a pod for nothing.
        src = tmp_path / "photos"
        src.mkdir()
        (src / "notes.txt").write_text("x")
        rc = main(_argv(tmp_path, src))
        err = capsys.readouterr().err
        assert rc == 2 and "no images" in err and str(src.resolve()) in err


class TestDryRun:
    def test_prints_plan_and_never_runs(self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]) -> None:
        # Bug caught: a dry run that boots a pod.
        src = _photos(tmp_path)
        out = src.parent / "photos_upscaled"
        out.mkdir()
        Image.new("RGB", (4, 4)).save(out / "a.png")
        rc = main(_argv(tmp_path, src, "--dry-run"))
        o = capsys.readouterr().out
        assert rc == 0
        assert f"source_dir: {src.resolve()}" in o and f"output_dir: {out.resolve()}" in o
        assert "media: image" in o
        assert "a.png -> a.png [exists]" in o
        assert "sub/b.webp -> sub/b.png [pending]" in o
        assert "found 3: pending 2, exists 1, oversize 0, unreadable 0; non-image skipped 0" in o
        assert not (out / "sub").exists()


class TestNothingToDo:
    def test_all_outputs_present_exits_0(self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]) -> None:
        src = _photos(tmp_path, ["a.png"])
        out = src.parent / "photos_upscaled"
        out.mkdir()
        Image.new("RGB", (4, 4)).save(out / "a.png")
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        assert rc == 0 and "nothing to do" in capsys.readouterr().out
        assert not (tmp_path / ".kf" / "ledger.json").exists()

    def test_plan_failures_only_exit_1(self, tmp_path: Path, no_runner: None, capsys: pytest.CaptureFixture[str]) -> None:
        # Bug caught: a directory of nothing but oversize files reports success.
        src = tmp_path / "photos"
        src.mkdir()
        Image.new("RGB", (600, 600), 0).save(src / "a.png")  # 600·600·4 = 1.44 MP > cap 1
        cfg = _cfg(tmp_path, extra="  max_output_megapixels: 1\n")
        rc = main(["upscale", "--image-dir", str(src), "-c", str(cfg), "--state-dir", str(tmp_path / ".kf")])
        o = capsys.readouterr().out
        assert rc == 1
        assert "nothing to do" in o
        assert "failed 1" in o and "failed: a.png" in o and "max_output_megapixels=1" in o


class TestRun:
    def test_clean_run_prints_progress_and_exits_0(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        src = _photos(tmp_path)
        cap = _fake_runner(monkeypatch)
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        o = capsys.readouterr().out
        assert rc == 0
        assert "[1/3] a.png -> a.png (8x6 -> 16x12)" in o
        assert "[2/3] sub/b.webp -> sub/b.png (8x6 -> 16x12)" in o
        assert f"upscaled 3, skipped 0 existing, failed 0, aborted 0 -> {src.resolve()}_upscaled" in o
        assert cap["single"] is True and cap["instance"] is None

    def test_one_failure_exits_1_and_names_it(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        # Bug caught: a failure reported as success.
        src = _photos(tmp_path)
        _fake_runner(monkeypatch, fail_names={"b.webp"})
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        o = capsys.readouterr().out
        assert rc == 1
        assert "[2/3] sub/b.webp FAILED: RuntimeError: pod said no" in o
        assert "upscaled 2, skipped 0 existing, failed 1, aborted 0" in o
        assert "failed: sub/b.webp" in o

    def test_pod_dead_prints_summary_then_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        src = _photos(tmp_path)
        _fake_runner(monkeypatch, raise_exc=PodDead("pod pod-1 stopped answering /health"))
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        cap = capsys.readouterr()
        assert rc == 1
        assert "upscaled 3," in cap.out  # the fake emitted all three before raising
        assert "error: PodDead: pod pod-1" in cap.err

    def test_cancelled_prints_and_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        from kinoforge.core.errors import Cancelled

        src = _photos(tmp_path)
        _fake_runner(monkeypatch, raise_exc=Cancelled("stop"))
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        assert rc == 1 and "upscale: cancelled" in capsys.readouterr().err

    def test_output_dir_flag_is_ignored_with_note(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        # Bug caught: files silently landing in output/.
        src = _photos(tmp_path)
        _fake_runner(monkeypatch)
        other = tmp_path / "elsewhere"
        rc = main(_argv(tmp_path, src, "--no-reuse", "--output-dir", str(other)))
        cap = capsys.readouterr()
        assert rc == 0
        assert "--output-dir" in cap.err and "ignored" in cap.err
        assert not other.exists()


class TestLaunchRow:
    def test_cold_create_stamps_once(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bug caught: the directory path forgets to record the pod, so the
        # next invocation cannot warm-attach to it.
        src = _photos(tmp_path)
        pod = Instance(id="pod-9", provider="fake", status="ready", created_at=0.0)
        _fake_runner(monkeypatch, instance=pod)
        stamped: list[Any] = []
        settled: list[Any] = []
        monkeypatch.setattr("kinoforge.cli._commands._stamp_cold_created_instance", lambda ctx, cfg, inst, **kw: stamped.append(inst) or "pod-9")
        monkeypatch.setattr("kinoforge.cli._commands._settle_unused_launch_row", lambda *a: settled.append(a))
        monkeypatch.setattr("kinoforge.cli._commands._scan_warm_candidates", lambda ctx, cfg: (None, type("R", (), {"summarize": lambda self: "none"})()))
        rc = main(_argv(tmp_path, src))  # warm-reuse default: no --no-reuse
        assert rc == 0 and stamped == [pod] and settled == []

    def test_no_reuse_settles_not_stamps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        src = _photos(tmp_path)
        _fake_runner(monkeypatch, instance=None)
        stamped: list[Any] = []
        settled: list[Any] = []
        monkeypatch.setattr("kinoforge.cli._commands._stamp_cold_created_instance", lambda *a, **kw: stamped.append(a))
        monkeypatch.setattr("kinoforge.cli._commands._settle_unused_launch_row", lambda *a: settled.append(a))
        rc = main(_argv(tmp_path, src, "--no-reuse"))
        assert rc == 0 and stamped == [] and len(settled) == 1

    def test_single_image_path_uses_the_shared_helper(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bug caught: a fourth copy of the stamp/settle block drifting.
        from kinoforge.core.interfaces import Artifact

        png = tmp_path / "in.png"
        Image.new("RGB", (8, 6)).save(png)
        calls: list[Any] = []
        monkeypatch.setattr("kinoforge.core.orchestrator.generate", lambda *a, **kw: (Artifact(uri="file:///out"), None))
        monkeypatch.setattr("kinoforge.cli._commands._finish_launch_row", lambda *a, **kw: calls.append((a, kw)))
        rc = main(["upscale", "--image", str(png), "-c", str(_cfg(tmp_path)), "--state-dir", str(tmp_path / ".kf"), "--no-reuse"])
        assert rc == 0 and len(calls) == 1 and calls[0][1]["no_reuse"] is True
```

Append to `tests/cli/test_cmd_upscale_image.py` (inside `TestConfigRefusals` or a new class):

```python
class TestMegapixelGuard:
    def test_oversize_image_exits_2(self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]) -> None:
        # Bug caught: --image and --image-dir disagree on the cap; a 50 MP
        # still at 4x reaches the pod and kills it on host RAM.
        import imageio.v3 as iio

        big = tmp_path / "big.png"
        iio.imwrite(big, np.zeros((600, 600, 3), dtype=np.uint8))
        cfg = _spandrel_cfg(tmp_path, extra="  max_output_megapixels: 1\n")
        rc = main(["upscale", "--image", str(big), "-c", str(cfg), "--no-reuse"])
        err = capsys.readouterr().err
        assert rc == 2
        assert "max_output_megapixels=1" in err and "600x600" in err

    def test_unreadable_image_exits_2(self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]) -> None:
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not a png")
        rc = main(["upscale", "--image", str(bad), "-c", str(_spandrel_cfg(tmp_path)), "--no-reuse"])
        assert rc == 2 and "bad.png" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run pytest tests/cli/test_cmd_upscale_image_dir.py tests/cli/test_cmd_upscale_image.py -q`
Expected: argparse cases exit 2 for the wrong reason (`unrecognized arguments: --image-dir`), `TestMegapixelGuard` fails (rc 0 — `generate` is patched to raise `AssertionError`, so actually the oversize case raises). Confirm every new test fails before implementing.

- [ ] **Step 3: Implement — argparse**

In `src/kinoforge/cli/_main.py`, after the `--image` argument of `p_upscale_src`:

```python
    p_upscale_src.add_argument(
        "--image-dir",
        metavar="DIR",
        dest="image_dir",
        help=(
            "upscale every image under DIR (recursive; any format Pillow opens) "
            "on one pod into a sibling DIR_upscaled that mirrors the tree. Output "
            "is always PNG; existing outputs are skipped. Engine must support "
            "image input (spandrel)."
        ),
    )
```

- [ ] **Step 4: Implement — `_cmd_upscale` dispatch and the shared helper**

In `src/kinoforge/cli/_commands.py`, replace the `media` / `source` derivation in `_cmd_upscale` with:

```python
    # --video | --image | --image-dir is a required argparse mutex group, so
    # exactly one is set. The kind travels as DATA from here on (core/media.py).
    image_dir: str | None = getattr(args, "image_dir", None)
    media: Media = "video" if getattr(args, "video", None) is not None else "image"
    source: str = (
        args.video
        if media == "video"
        else (image_dir if image_dir is not None else args.image)
    )
```

Change the empty-`--image` guard to `if media == "image" and image_dir is None and not source:`.

Immediately BEFORE the `if getattr(args, "dry_run", False):` block, add:

```python
    if image_dir is not None:
        return _cmd_upscale_image_dir(args, ctx, cfg, scale, raw_scale, image_dir)
```

(`scale` is a parsed `ScaleTarget` at that point: CLI override or `cfg.upscale.scale`.)

After the existing `_image_arg_error` check on the non-dry-run path, add the single-image guard:

```python
    if media == "image":
        if (img_err := _image_arg_error(source)) is not None:
            print(img_err, file=sys.stderr)
            return 2
        if (mp_err := _image_megapixel_error(source, cfg, scale)) is not None:
            print(mp_err, file=sys.stderr)
            return 2
```

Replace the tail of `_cmd_upscale` (from the `# T11 — symmetric ledger stamp` comment to the `else:` branch inclusive) with:

```python
    _finish_launch_row(
        ctx, cfg, launch, instance, returned_instance, no_reuse=bool(args.no_reuse)
    )
```

Add the helpers near `_image_preflight_error`:

```python
def _finish_launch_row(
    ctx: SessionContext,
    cfg: Config,
    launch: _LaunchRow | None,
    instance: Instance | None,
    returned_instance: Instance | None,
    *,
    no_reuse: bool,
) -> None:
    """Stamp a cold-created pod into the ledger, or settle the launch row.

    The one copy of the block ``--video``, ``--image`` and ``--image-dir``
    share. A cold create that will be warm-reused is recorded so the next
    invocation can find it; every other outcome (attach, ``--no-reuse``, no
    pod) releases the pre-create row when that is actually safe
    (:func:`_settle_unused_launch_row`). Never called on a raise — ruling C1
    keeps the row because the pod may still be alive.

    Args:
        ctx: Session context (ledger access).
        cfg: Loaded config.
        launch: The reserved launch row, or ``None`` when a pod was supplied.
        instance: The pre-resolved instance the run was handed, if any.
        returned_instance: The instance the run reports it used.
        no_reuse: ``--no-reuse`` — the pod was destroyed, never stamp it.
    """
    if returned_instance is not None and instance is None and not no_reuse:
        _stamp_cold_created_instance(
            ctx,
            cfg,
            returned_instance,
            created_at_local=launch.created_at_local if launch else None,
            supersedes=launch.id if launch else None,
        )
    else:
        _settle_unused_launch_row(ctx, cfg, launch, returned_instance)


def _image_megapixel_error(source: str, cfg: Config, scale: ScaleTarget) -> str | None:
    """Return the exit-2 message when a ``--image`` output would exceed the cap.

    Args:
        source: The ``--image`` path (already known to exist).
        cfg: Loaded config; ``cfg.upscale`` present.
        scale: The effective factor scale.

    Returns:
        An ``error: ...`` line naming the file, its size and the cap, or
        ``None``.
    """
    from kinoforge.core.image_dir import output_megapixels, read_image_header

    block = cfg.upscale
    assert block is not None  # noqa: S101 — _cmd_upscale checked before calling
    try:
        hdr = read_image_header(Path(source))
    except Exception as exc:  # noqa: BLE001 — any Pillow failure is "unreadable"
        return f"error: --image cannot be read: {source}: {type(exc).__name__}: {exc}"
    factor = int(scale.value)
    mp = output_megapixels(hdr.width, hdr.height, factor)
    if mp > block.max_output_megapixels:
        return (
            f"error: --image {source} is {hdr.width}x{hdr.height}; at {factor}x the "
            f"output is {mp:.1f} MP, over upscale.max_output_megapixels="
            f"{block.max_output_megapixels}"
        )
    return None
```

`ScaleTarget` is imported inside `_cmd_upscale`; for the annotation, add `from kinoforge.core.scale_target import ScaleTarget` to the `TYPE_CHECKING` block at the top of `_commands.py` (the module already uses `from __future__ import annotations`).

- [ ] **Step 5: Implement — the directory branch**

Add after `_image_megapixel_error`:

```python
def _print_image_dir_plan(
    plan: ImageDirPlan, cfg: Config, raw_scale: str | None, args: argparse.Namespace, *, dry_run: bool
) -> None:
    """Print the plan header and counts; per-item lines on a dry run."""
    assert cfg.upscale is not None  # noqa: S101 — caller checked
    print("upscale plan:")
    print(f"  source_dir: {plan.source_dir}")
    print(f"  output_dir: {plan.output_dir}")
    print("  media: image")
    print(f"  scale: {raw_scale or cfg.upscale.scale}")
    print(f"  engine: {cfg.upscale.engine}")
    print(f"  no_reuse: {bool(getattr(args, 'no_reuse', False))}")
    print(f"  attach_pod: {getattr(args, 'attach_pod', None)}")
    for item in plan.items:
        if not dry_run and item.disposition not in ("oversize", "unreadable"):
            continue
        tag = item.disposition + (" renamed" if item.renamed else "")
        line = (
            f"  {item.source.relative_to(plan.source_dir).as_posix()} -> "
            f"{item.output.relative_to(plan.output_dir).as_posix()} [{tag}]"
        )
        print(line + (f": {item.reason}" if item.reason else ""))
    n_over = sum(1 for i in plan.items if i.disposition == "oversize")
    n_unr = sum(1 for i in plan.items if i.disposition == "unreadable")
    print(
        f"  found {len(plan.items)}: pending {len(plan.pending)}, exists {len(plan.exists)}, "
        f"oversize {n_over}, unreadable {n_unr}; non-image skipped {plan.skipped_non_image}"
    )


def _print_image_dir_summary(
    plan: ImageDirPlan, seen: Mapping[Path, tuple[str, str | None]]
) -> bool:
    """Print the closing summary; return ``True`` when anything failed or aborted."""
    written = sum(1 for o, _ in seen.values() if o == "written")
    run_failed = [(p, r) for p, (o, r) in seen.items() if o == "failed"]
    aborted = [(p, r) for p, (o, r) in seen.items() if o == "aborted"]
    plan_failed = [(i.source, i.reason) for i in plan.failed_at_plan]
    print(
        f"upscaled {written}, skipped {len(plan.exists)} existing, "
        f"failed {len(run_failed) + len(plan_failed)}, aborted {len(aborted)} -> {plan.output_dir}"
    )
    for p, r in [*plan_failed, *run_failed]:
        print(f"  failed: {p.relative_to(plan.source_dir).as_posix()}: {r}")
    for p, r in aborted:
        print(f"  aborted: {p.relative_to(plan.source_dir).as_posix()}: {r}")
    return bool(run_failed or plan_failed or aborted)


def _cmd_upscale_image_dir(
    args: argparse.Namespace,
    ctx: SessionContext,
    cfg: Config,
    scale: ScaleTarget,
    raw_scale: str | None,
    image_dir: str,
) -> int:
    """Handle ``upscale --image-dir`` after the shared preflight has passed.

    Args:
        args: Parsed CLI arguments.
        ctx: Session context.
        cfg: Loaded config with an ``upscale:`` block the engine can serve.
        scale: Effective factor scale.
        raw_scale: The ``--scale`` token, for the plan header.
        image_dir: The ``--image-dir`` value.

    Returns:
        Exit code: 2 precondition, 1 any failure or abort, 0 otherwise.
    """
    from kinoforge.core.errors import Cancelled, KinoforgeError
    from kinoforge.core.image_dir import ImageDirItem, plan_image_dir
    from kinoforge.core.upscale_dir import upscale_image_dir

    assert cfg.upscale is not None  # noqa: S101 — _cmd_upscale checked
    if not image_dir:
        print("error: --image-dir is empty (no input path)", file=sys.stderr)
        return 2
    src = Path(image_dir)
    if not src.exists():
        print(f"error: --image-dir path does not exist: {image_dir}", file=sys.stderr)
        return 2
    if not src.is_dir():
        print(f"error: --image-dir is not a directory: {image_dir}", file=sys.stderr)
        return 2
    factor = int(scale.value)
    try:
        plan = plan_image_dir(
            src, scale=factor, max_output_megapixels=cfg.upscale.max_output_megapixels
        )
    except ValueError as exc:
        print(f"error: --image-dir: {exc}", file=sys.stderr)
        return 2
    if not plan.items:
        print(
            f"error: --image-dir contains no images: {plan.source_dir} "
            f"({plan.skipped_non_image} non-image file(s) skipped)",
            file=sys.stderr,
        )
        return 2

    dry_run = bool(getattr(args, "dry_run", False))
    _print_image_dir_plan(plan, cfg, raw_scale, args, dry_run=dry_run)
    if dry_run:
        return 0
    if getattr(args, "output_dir", None) is not None or getattr(args, "no_output_dir", False):
        print(
            "note: --output-dir / --no-output-dir are ignored with --image-dir; "
            f"outputs go to {plan.output_dir}",
            file=sys.stderr,
        )

    seen: dict[Path, tuple[str, str | None]] = {}
    pending = plan.pending
    if not pending:
        print(f"nothing to do: every image already has an output under {plan.output_dir}")
        return 1 if _print_image_dir_summary(plan, seen) else 0

    store = ctx.store()
    run_id = _resolve_run_id(args, "upscale")
    instance: Instance | None = None
    attach_pod_id = getattr(args, "attach_pod", None)
    if attach_pod_id:
        instance, rc = _resolve_attach_pod(ctx, cfg, attach_pod_id)
        if rc is not None:
            return rc
    elif not args.no_reuse:
        instance, report = _scan_warm_candidates(ctx, cfg)
        logger.info(report.summarize())
    launch = _ephemeral_launch_row_reserve(ctx, cfg, run_id) if instance is None else None

    total = len(pending)

    def _on_item(item: ImageDirItem, outcome: str, reason: str | None) -> None:
        seen[item.source] = (outcome, reason)
        n = len(seen)
        rel_in = item.source.relative_to(plan.source_dir).as_posix()
        if outcome == "written":
            rel_out = item.output.relative_to(plan.output_dir).as_posix()
            print(
                f"[{n}/{total}] {rel_in} -> {rel_out} "
                f"({item.width}x{item.height} -> {item.width * factor}x{item.height * factor})"
            )
        else:
            print(f"[{n}/{total}] {rel_in} {outcome.upper()}: {reason}")

    returned_instance: Instance | None = None
    try:
        _result, returned_instance = upscale_image_dir(
            cfg,
            plan,
            store=store,
            run_id=run_id,
            state_dir=ctx.state_dir,
            instance=instance,
            cancel_token=ctx.cancel_token,
            single=bool(args.no_reuse),
            on_instance_created=_ephemeral_row_upgrade_hook(ctx, cfg, launch),
            on_item=_on_item,
        )
    # None of these rungs settle the launch row — a raise keeps it (ruling C1):
    # the pod may still be alive and billing, and the row is its only handle.
    except Cancelled:
        _print_image_dir_summary(plan, seen)
        print("upscale: cancelled", file=sys.stderr)
        return 1
    except KinoforgeError as exc:
        _print_image_dir_summary(plan, seen)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    any_failed = _print_image_dir_summary(plan, seen)
    _finish_launch_row(
        ctx, cfg, launch, instance, returned_instance, no_reuse=bool(args.no_reuse)
    )
    return 1 if any_failed else 0
```

Add `ImageDirPlan` to the `TYPE_CHECKING` imports (`from kinoforge.core.image_dir import ImageDirPlan`). `Mapping` is already imported from `collections.abc`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `pixi run pytest tests/cli/ -q`
Expected: all PASS, including the pre-existing `test_cmd_upscale*.py` files. If `test_cmd_upscale_full.py::test_non_dry_run_invokes_generate_with_skip_flag` (video path) breaks, the cause is the `_finish_launch_row` extraction — the behaviour must be identical to the removed block; fix the helper, not the test.

- [ ] **Step 7: Commit**

```bash
pixi run pre-commit run --all-files
git add src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py tests/cli/test_cmd_upscale_image_dir.py tests/cli/test_cmd_upscale_image.py
git commit -m "feat(cli): kinoforge upscale --image-dir — directory branch, shared launch-row finish, --image megapixel guard"
```

---

### Task 8: Docs, PROGRESS.md, and the no-golden-moved guard

**Goal:** Operators can find the feature in the docs; the resume protocol knows where this build is; the "zero pod-side changes" constraint is proven with a snapshot diff and the full suite.

**Files:**
- Modify: `docs/configuration.md` (the `upscale:` section, ~line 140-160)
- Modify: `docs/engines.md` (the spandrel "Image input" paragraph, ~line 605-620)
- Modify: `README.md:116`
- Modify: `PROGRESS.md` (the `## Pointers` block and the `## RESUME SNAPSHOT`)

**Acceptance Criteria:**
- [ ] `tools/snapshot_launch_payloads.py` followed by `git status --porcelain` shows NO changed golden, baseline or `_golden_provision.json` file (run it and paste the empty output into the commit message body).
- [ ] `pixi run pytest -q` is fully green (count the total; it must be ≥ the pre-feature 6365 passed plus every new test).
- [ ] `docs/configuration.md` documents `max_output_megapixels` in the `upscale:` key table and has a `--image-dir` paragraph covering: recursion + mirrored tree, `<stem>.png` naming and the collision rule, existing outputs skipped, the accepted suffix list, the conversion rule (passthrough vs re-encode, EXIF applied, frame 0), the ignored output flags, exit codes, and the pod-disk ceiling (`disk_gb: 40` ≈ 1,000 4-MP 2x outputs per pod life).
- [ ] `docs/engines.md` spandrel section gains two sentences on the directory mode and the tiler comparison from spec §3.3.
- [ ] README command table reads "Upscale a video clip, a still image, or a directory of images".
- [ ] `PROGRESS.md` Pointers names the spec + plan; the RESUME SNAPSHOT says Tasks 1-8 done, Task 9 (live) next, with the exact live command.

**Verify:** `pixi run pytest -q && pixi run pre-commit run --all-files` → green; `git status --porcelain` after the snapshot tool → empty.

**Steps:**

- [ ] **Step 1: Prove no golden moved**

```bash
pixi run python tools/snapshot_launch_payloads.py
git status --porcelain
```

Expected: empty. If anything under `tests/providers/` or a `*_golden*` / `_BASELINE_BYTES` moved, STOP — a controller-side import has leaked onto the pod (Global Constraint 1). Find it with `rg -n 'image_dir|upscale_dir' src/kinoforge/engines src/kinoforge/upscalers` and remove it; never regenerate.

- [ ] **Step 2: `docs/configuration.md`**

In the `upscale:` key table add a row:

```
| `max_output_megapixels` | int | `256` | Still images only (`--image` / `--image-dir`): refuses an output larger than `width × height × scale²` megapixels before any upload. Bounds the pod's host-RAM output canvas, which the pod-side tiler does not. |
```

After the existing `--image` paragraph add:

```markdown
**Directories.** `kinoforge upscale --image-dir photos/` upscales every image
under `photos/` (recursive) on ONE pod and writes lossless PNGs into a sibling
`photos_upscaled/` that mirrors the tree: `photos/sub/b.webp` →
`photos_upscaled/sub/b.png`. Two files in the same folder with the same stem
(`a.webp` + `a.png`) keep their full name plus `.png` (`a.webp.png`,
`a.png.png`). An output that already exists is skipped, so re-running the same
command after a crash pays only for what is missing. Recognised suffixes:
png jpg jpeg jfif webp avif gif bmp dib tif tiff tga heic heif jp2 j2k psd ico
pcx pbm pgm ppm pnm qoi dds; other files are counted and skipped, dot-files and
symlinks ignored. An 8-bit RGB/RGBA/greyscale PNG or JPEG with no EXIF rotation
is uploaded unchanged; anything else is converted on the controller to an
8-bit RGB PNG with EXIF orientation applied (animated GIFs and multi-page
TIFFs contribute their first frame). `--output-dir` / `--no-output-dir` are
ignored for a directory run. One line per image is printed as it finishes; the
closing line is `upscaled N, skipped N existing, failed N, aborted N -> <dir>`.
Exit 0 when nothing failed, 1 when any image failed (oversize, unreadable, or a
pod error — the rest still run; after a failure the pod's `/health` is probed
and a dead pod aborts the remainder), 2 for a precondition. Each output stays
on the pod's disk until the pod dies; the example config's `disk_gb: 40` is
roughly 1,000 4-MP 2x outputs per pod lifetime. `--no-reuse` destroys the pod
once, after the last image.
```

- [ ] **Step 3: `docs/engines.md`**

After the spandrel "Image input" paragraph add:

```markdown
**Directory input.** `--image-dir DIR` runs the same path once per file on one
pod (`core/upscale_dir.py`, one `deploy_session`, one `UpscaleStage`). Inputs
are normalised on the controller (`core/image_dir.py`): the pod never sees
anything but 8-bit PNG/JPEG. Two tilers exist by decision and stay separate:
the video path's `tile_grid` crops mp4 frames on the controller and
feather-blends them (FlashVSR is generative, tiles can disagree); the still
path's `tile_size` slices the array on the pod with 32 px of context and
discards the overlap (RealESRGAN is deterministic, so the tile interior equals
the whole-image result — asserted pixel-for-pixel in
`tests/upscalers/test_spandrel_runtime_image.py`).
```

- [ ] **Step 4: README**

Change the `upscale` row's description to `Upscale a video clip (FlashVSR default; spandrel 2x), a still image (`--image`), or a directory of images (`--image-dir`, one pod, mirrored `<dir>_upscaled`)`.

- [ ] **Step 5: PROGRESS.md**

In `## Pointers`, add a first bullet:

```markdown
- **IN FLIGHT — directory image upscaling (`kinoforge upscale --image-dir`):** design
  `docs/superpowers/specs/2026-10-06-directory-image-upscale-design.md` (approved 2026-10-06,
  committed `7916b91d`), plan `docs/superpowers/plans/2026-10-06-directory-image-upscale.md`
  (+ `.tasks.json`, 9 tasks). One flag on `upscale`; pure planner `core/image_dir.py`;
  one-session runner `core/upscale_dir.py`; `upscale.max_output_megapixels` (default 256);
  tilers deliberately NOT unified (spec §3.3). Zero pod-side changes.
```

Under `## RESUME SNAPSHOT`, prepend a dated section:

```markdown
### SESSION 2026-10-06 — directory image upscaling, Tasks 1-8 done, Task 9 (live) next

Offline work complete and green; no launch golden moved (snapshot diff empty). **Single next
action:** Task 9 — commit the RED live scaffold, `pixi run preflight`, then exactly one run:

    pixi run kinoforge upscale \
      -c examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml \
      --image-dir /workspace/output/dir-smoke --no-reuse

Expected six written / one existing / one oversize / exit 0; poll utilisation every 60-90 s;
`kinoforge list` must show both clean lines afterwards; frame-QA every output; new
`successful-generations.md` section.
```

- [ ] **Step 6: Full suite and commit**

```bash
pixi run pytest -q
pixi run pre-commit run --all-files
git add docs/configuration.md docs/engines.md README.md PROGRESS.md
git commit -m "docs: kinoforge upscale --image-dir — configuration, engines, README, PROGRESS"
```

Paste the suite totals and the empty `git status --porcelain` from Step 1 into the commit body.

---

### Task 9: Live proof — fixture builder, RED scaffold, one `--no-reuse` run, frame QA, log entry

**Goal:** One real RunPod run proves the directory mode end to end on the live-proven spandrel x2 config, with every branch (passthrough, conversion, rotation, nested, GIF, RGBA, oversize, exists, non-image) exercised in one pod's life, visually QA'd and recorded.

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Create: `tests/live/build_image_dir_fixture.py`
- Create: `tests/live/test_image_dir_upscale_smoke.py`
- Create: `tests/live/evidence/2026-10-06-image-dir-upscale/` (stdout, stderr, `kinoforge list` output, contact sheets)
- Modify: `successful-generations.md` (new section + "See also" under §36), `PROGRESS.md`

**Acceptance Criteria:**
- [ ] The RED scaffold (fixture builder + live test) is committed BEFORE `pixi run preflight` is run; `pixi run preflight` exits 0 before the live command.
- [ ] The run's stdout, captured to `tests/live/evidence/2026-10-06-image-dir-upscale/stdout.txt`, contains six `[n/6] … -> …` written lines, the line `upscaled 6, skipped 1 existing, failed 1, aborted 0 -> /workspace/output/dir-smoke_upscaled`, exactly one `failed:` line naming `huge.webp` with `max_output_megapixels=256`, and the process exit code is 1 (one plan-time failure is DESIGNED into the fixture — the exit code proves the oversize guard is reported, not hidden).
- [ ] `dir-smoke_upscaled/` holds exactly: `luma.png`, `luma-webp.png`, `luma-avif.png`, `rotated.png`, `sub/anim.png`, `sub/alpha.png`, plus the pre-placed `existing.png` unchanged (sha256 before == after); `rotated.png` is 5344×3008 (upright), the four Luma-derived outputs are 5344×3008, `sub/anim.png` and `sub/alpha.png` are 2× their inputs, and `huge.png` does NOT exist.
- [ ] GPU utilisation was polled every 60-90 s during boot and run (the probe output is in `evidence/util.txt`) — never inferred from `est_spend`.
- [ ] After the process exits, `pixi run kinoforge list` output (saved to `evidence/list.txt`) contains both `No running instances.` and `No instances recorded in ledger.`
- [ ] A contact sheet per output beside its input (`evidence/qa-*.png`) was READ by the controller and the verdict recorded: detail preserved, no tile seams, no colour shift, `rotated.png` upright, `sub/anim.png` is frame 0 (red, not green), `sub/alpha.png` has no alpha artefacts. Anything not clearly high quality gets an explicit ⚠️.
- [ ] `successful-generations.md` has a new numbered section for mode axis `image-dir-upscale` per the preamble schema, a "See also" line under §36, and `PROGRESS.md`'s snapshot records the result and spend.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_image_dir_upscale_smoke.py -q -s` → PASS; `pixi run kinoforge list` → both clean lines.

**Steps:**

- [ ] **Step 1: Write the fixture builder**

Create `tests/live/build_image_dir_fixture.py`:

```python
"""Build the `--image-dir` live-smoke fixture directory (design §6.1).

Usage: ``pixi run python tests/live/build_image_dir_fixture.py [DEST]``
(default ``output/dir-smoke``). Idempotent: DEST is recreated from scratch.
Every branch of the feature is represented once; see the design for why each
file is there.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image

_ROOT = Path(__file__).parent.parent.parent
_LUMA = _ROOT / "output" / "20261003-174427_image_luma_agents_uni-1_Photorealistic-cinem.png"


def build(dest: Path) -> None:
    """Create the fixture tree under *dest* and the pre-placed existing output."""
    if not _LUMA.exists():
        raise SystemExit(f"missing §35 input {_LUMA.name}; regenerate with kinoforge image")
    shutil.rmtree(dest, ignore_errors=True)
    shutil.rmtree(dest.parent / f"{dest.name}_upscaled", ignore_errors=True)
    (dest / "sub").mkdir(parents=True)
    with Image.open(_LUMA) as luma:
        luma = luma.convert("RGB")
        luma.save(dest / "luma.png")                       # passthrough
        luma.save(dest / "luma-webp.webp", quality=95)      # conversion
        luma.save(dest / "luma-avif.avif", quality=80)      # conversion
        rotated = luma.transpose(Image.Transpose.ROTATE_90)  # stored on its side …
        exif = rotated.getexif()
        exif[0x0112] = 6  # … orientation 6 (exif_transpose applies ROTATE_270) restores it
        rotated.save(dest / "rotated.jpg", exif=exif, quality=95)
    f0 = Image.new("RGB", (320, 200), (200, 30, 30))
    f1 = Image.new("RGB", (320, 200), (30, 200, 30))
    f0.save(dest / "sub" / "anim.gif", save_all=True, append_images=[f1], duration=200)
    alpha = Image.new("RGBA", (300, 180), (40, 120, 220, 255))
    alpha.putalpha(Image.linear_gradient("L").resize((300, 180)))
    alpha.save(dest / "sub" / "alpha.png")
    Image.new("RGB", (12000, 12000), (90, 90, 90)).save(dest / "huge.webp", quality=50)
    (dest / "notes.txt").write_text("not an image\n")
    Image.new("RGB", (64, 64), (0, 0, 0)).save(dest / "existing.png")
    out = dest.parent / f"{dest.name}_upscaled"
    out.mkdir()
    Image.new("RGB", (128, 128), (255, 0, 255)).save(out / "existing.png")  # pre-placed → skipped
    print(f"fixture at {dest}; pre-placed {out / 'existing.png'}")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else _ROOT / "output" / "dir-smoke")
```

Note `rotated.jpg`: the Luma still is 2672×1504 landscape; `ROTATE_90` stores it as 1504×2672 with orientation 6, and `ImageOps.exif_transpose` maps 6 to `ROTATE_270`, so a viewer (and `prepare_upload`) rotate it back to the ORIGINAL 2672×1504 upright → output 5344×3008 identical in orientation to `luma.png`'s. The 12000² WebP at 2x is 576 MP > 256 → `oversize`.

- [ ] **Step 2: Write the RED live test**

Create `tests/live/test_image_dir_upscale_smoke.py`, following `tests/live/test_spandrel_image_upscale_smoke.py`'s shape:

```python
"""Live smoke — `kinoforge upscale --image-dir` on the spandrel x2 config (design §6.1).

RED scaffold committed BEFORE the live spend per CLAUDE.md. One pod, eight
files, every branch of the feature. Evidence lands under
``tests/live/evidence/2026-10-06-image-dir-upscale/`` BEFORE any assertion.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="live smoke: set KINOFORGE_LIVE_TESTS=1 (spends RunPod money)",
)

_ROOT = Path(__file__).parent.parent.parent
_CFG = _ROOT / "examples" / "configs" / "runpod-diffusers-spandrel-x2-upscale.yaml"
_SRC = _ROOT / "output" / "dir-smoke"
_OUT = _ROOT / "output" / "dir-smoke_upscaled"
_EVIDENCE = Path(__file__).parent / "evidence" / "2026-10-06-image-dir-upscale"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _size(p: Path) -> tuple[int, int]:
    with Image.open(p) as im:
        return im.size


@pytest.mark.live
def test_directory_upscale_on_one_pod() -> None:
    from tests.live.build_image_dir_fixture import build

    build(_SRC)
    _EVIDENCE.mkdir(parents=True, exist_ok=True)
    existing_before = _sha(_OUT / "existing.png")

    proc = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "upscale", "-c", str(_CFG), "--image-dir", str(_SRC), "--no-reuse"],
        capture_output=True, text=True, timeout=3600, check=False,
    )
    (_EVIDENCE / "stdout.txt").write_text(proc.stdout)
    (_EVIDENCE / "stderr.txt").write_text(proc.stderr)
    ledger = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "list"], capture_output=True, text=True, check=False
    )
    (_EVIDENCE / "list.txt").write_text(ledger.stdout + ledger.stderr)

    # One plan-time failure (huge.webp) is designed in → exit 1, not 0.
    assert proc.returncode == 1, proc.stderr
    assert "upscaled 6, skipped 1 existing, failed 1, aborted 0" in proc.stdout
    assert re.search(r"failed: huge\.webp: .*max_output_megapixels=256", proc.stdout)
    assert len(re.findall(r"^\[\d/6\] .* -> ", proc.stdout, flags=re.M)) == 6

    expected = {
        "luma.png": (5344, 3008), "luma-webp.png": (5344, 3008), "luma-avif.png": (5344, 3008),
        "rotated.png": (5344, 3008), "sub/anim.png": (640, 400), "sub/alpha.png": (600, 360),
    }
    for rel, size in expected.items():
        p = _OUT / rel
        assert p.exists(), rel
        assert _size(p) == size, (rel, _size(p))
        shutil.copy2(p, _EVIDENCE / rel.replace("/", "__"))
    assert not (_OUT / "huge.png").exists()
    assert _sha(_OUT / "existing.png") == existing_before
    assert sorted(p.relative_to(_OUT).as_posix() for p in _OUT.rglob("*.png")) == sorted([*expected, "existing.png"])

    assert "No running instances." in ledger.stdout
    assert "No instances recorded in ledger." in ledger.stdout
```

`tests/live/__init__.py` exists, so the `from tests.live...` import resolves under pytest's rootdir.

- [ ] **Step 3: Commit the RED scaffold (before any spend)**

```bash
pixi run pre-commit run --all-files
git add tests/live/build_image_dir_fixture.py tests/live/test_image_dir_upscale_smoke.py
git commit -m "test(live): RED scaffold for the kinoforge upscale --image-dir smoke"
```

- [ ] **Step 4: Preflight, then fire — with utilisation polling**

```bash
pixi run preflight
```

Must exit 0 (creds present, zero pods, clean tree). Then start the run in the background and poll:

```bash
KINOFORGE_LIVE_TESTS=1 pixi run pytest tests/live/test_image_dir_upscale_smoke.py -q -s \
  > /tmp/claude-1000/-workspace/*/scratchpad/dir-smoke-run.log 2>&1 &
```

Every 60-90 s until the run exits, get the pod id from `pixi run kinoforge list` and run the util probe from CLAUDE.md (`RunPodGraphQLUtilEndpoint(...).probe(pod_id)`), appending each snapshot with a local timestamp to `tests/live/evidence/2026-10-06-image-dir-upscale/util.txt`. Expected shape: GPU 0% during the ~3-10 min boot and weight fetch with memory rising, then bursts of GPU during the six inferences (each only seconds — the probe caches 15-30 s, so a quiet reading between images is normal). GPU 0% for ≥3 probes WITH memory flat during boot → pull `https://<pod>-8001.proxy.runpod.net/bootstrap.log`, destroy, fail fast.

- [ ] **Step 5: Verify teardown independently**

```bash
pixi run kinoforge list
```

Both `No running instances.` and `No instances recorded in ledger.` must appear. A `⚠ launching — pod not confirmed` row is NOT a leak (it ages out after 1800 s); any other row → `pixi run kinoforge destroy --id <id>`.

- [ ] **Step 6: Frame QA (mandatory before reporting green)**

For each of the six outputs, build a side-by-side contact sheet (input, nearest-neighbour-upscaled to output size, beside the output; crop a 512² detail from the centre of each) with Pillow into `evidence/qa-<name>.png`, then READ each sheet. Record per file: detail preserved / tile seams / colour shift, plus `rotated.png` upright, `sub/anim.png` red (frame 0), `sub/alpha.png` clean. Anything not clearly high quality gets ⚠️.

- [ ] **Step 7: Record**

Add section 39 (or the next number) to `successful-generations.md` for mode axis `image-dir-upscale` per the preamble schema — stack triple `runpod / SpandrelEngine / RealESRGAN_x2.pth`, the exact command, pod id, costPerHr × life ≈ spend, the per-file table (input → output dims, passthrough/converted), the QA verdicts, and the evidence path — plus a "See also" line under §36. Update `PROGRESS.md`'s snapshot: Task 9 done, spend, verdicts, and the next action (merge the branch). Commit:

```bash
pixi run pre-commit run --all-files
git add successful-generations.md PROGRESS.md tests/live/evidence/2026-10-06-image-dir-upscale
git commit -m "test(live): kinoforge upscale --image-dir live-proven on RunPod spandrel x2 (§39)"
```

(Do not commit the multi-MB output PNGs themselves if `check-added-large-files` refuses them; the contact sheets and text evidence are enough, and the outputs stay under `output/`, which is the operator's.)
