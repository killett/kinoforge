# Standalone Image Upscaling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `kinoforge upscale --image photo.png -c <spandrel cfg>` upscales one still image on a pod with the spandrel engine and publishes a PNG.

**Architecture:** One explicit media value (`"video"` | `"image"`) is threaded as data through the three seams that already exist — `Artifact.meta` → `UpscaleJob.media` → the pod `/upscale` request — instead of being inferred from filenames. The pod runtime gains one method, `SpandrelRuntime.upscale_image`, which tiles the still by the already-configured `tile_size` and writes `<stem>.upscaled.png`. The orchestrator's publish step picks the extension from the media. No config schema, embed set or launch golden changes.

**Tech Stack:** Python 3.13, pydantic, FastAPI (pod server), imageio + Pillow (image I/O), torch + spandrel (pod only), pytest. All dev commands through `pixi run`.

**Spec:** `docs/superpowers/specs/2026-10-03-standalone-image-upscaling-design.md`

## Global Constraints

- The media literal is exactly `"video"` or `"image"`; the `Artifact.meta` key is exactly `"media"`; absent means `"video"`. Both live in `src/kinoforge/core/media.py` and nowhere else.
- Published extension: image → `.png`, video → `.mp4`. Pod output filename for an image: `<stem>.upscaled.png`.
- Tile overlap on the still-image path is a fixed 32 px. The video path of `SpandrelRuntime.upscale` stays untiled and behaviourally identical (existing `tests/upscalers/test_spandrel_runtime.py` must stay green untouched).
- Every `--image` refusal exits 2 BEFORE any store, registry engine, provider or HTTP construction, and the config-fact refusals (height scale, engine, chunk/tile) fire BEFORE `--dry-run` prints.
- No edits to `UpscaleConfig` / `SpandrelEngineConfig` fields, to any `embed_modules` / `embed_files` list, and launch goldens / `_BASELINE_BYTES` are regenerated only in Task 8, deliberately, after the last pod-side edit. `tests/providers/test_pod_embed_closure.py` and `tests/providers/test_env_payload_ceiling.py` must stay green (Task 8 runs them explicitly).
- Pod-side modules (`wan_t2v_server.py`, `spandrel/_runtime.py`) must NOT gain any `kinoforge.*` import outside their current embed set (`kinoforge.core.errors`, `kinoforge.core.scale_target`, the `servers/_*` helpers). The media string is used raw on the pod.
- TDD per task: failing test → run → implement → run → commit. `pixi run pre-commit run --all-files` before every commit (stage new files first — `--all-files` ignores untracked). Conventional Commits with the session's `Co-Authored-By` + `Claude-Session` trailers.
- Live spend only in Task 9, only after Task 9's RED scaffold is committed and `pixi run preflight` exits 0, always `--no-reuse`, utilisation polled during the run, `kinoforge list` verified after.

**User decisions (already made):**
- Pod-side spandrel, not a hosted (fal/Replicate) upscaler — hosted is a separate later decision.
- Surface is `kinoforge upscale --image PATH`, mutually exclusive with `--video`; no new subcommand, no suffix sniffing.
- v1 rules: factor scales only for images; `chunk_frames` / `tile_grid` refused with `--image`; FlashVSR and SeedVR2 refuse `--image` at preflight via a default-false engine attribute; output is always PNG.

---

## File structure

| file | responsibility | task |
|---|---|---|
| `src/kinoforge/core/media.py` (new) | the media convention: `Media`, `MEDIA_KEY`, `IMAGE_SUFFIXES`, `media_of()`, `extension_for()` | 1 |
| `src/kinoforge/core/interfaces.py` | `UpscaleJob.media` default `"video"`; `UpscalerEngine.supports_image_input = False` | 1 |
| `src/kinoforge/upscalers/spandrel/_engine.py` | `supports_image_input = True`; forward `job.media` to upload + payload; stamp result meta | 1, 4 |
| `src/kinoforge/pipeline/upscale.py` | `_engine_call` reads `media_of(clip)` into the job | 2 |
| `src/kinoforge/cli/_main.py` | `--video` / `--image` required mutex group | 3 |
| `src/kinoforge/cli/_commands.py` | `_image_arg_error`, `_image_preflight_error`, `_resolve_input_as_artifact(path, media)`, dry-run `media:` line | 3 |
| `src/kinoforge/engines/_pod_http.py` | `_upload_source(..., media=)` content type + suffix | 4 |
| `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` | `/upload` accepts `image/png`, `image/jpeg`; `UpscaleRequest.media`; submit refusal; dispatch to `upscale_image` | 5 |
| `src/kinoforge/upscalers/spandrel/_runtime.py` | pure-numpy `tile_upscale()` + `_to_rgb()`; `_check_scale`, `_place_model`, `_infer` extraction; `upscale_image`; docstrings | 6 |
| `src/kinoforge/core/config.py` | `SpandrelEngineConfig.tile_size` docstring only | 6 |
| `src/kinoforge/core/orchestrator.py` | publish extension from media; defensive raise on image + `downscale_to` | 7 |
| `docs/engines.md`, `docs/configuration.md`, `README.md`, `PROGRESS.md` | docs | 8, 9 |
| `tests/live/test_spandrel_image_upscale_smoke.py` (new) | RED scaffold, then the live proof | 9 |
| `successful-generations.md` | new §36 after the live proof | 9 |

Dependencies: Task 1 first; Tasks 2–7 each depend only on Task 1 and can run in any order; Task 8 after 1–7; Task 9 after 8.

---

### Task 1: The media convention and the two interface fields

**Goal:** `core/media.py` exists, `UpscaleJob` carries `media` defaulting to `"video"`, `UpscalerEngine.supports_image_input` defaults to `False` and is `True` only on spandrel.

**Files:**
- Create: `src/kinoforge/core/media.py`
- Modify: `src/kinoforge/core/interfaces.py:989-1004` (`UpscaleJob`), `:1366-1386` (`UpscalerEngine` attributes)
- Modify: `src/kinoforge/upscalers/spandrel/_engine.py:38-48` (class attributes)
- Test: `tests/core/test_media.py` (new)

**Acceptance Criteria:**
- [ ] `media_of(Artifact())` returns `"video"`; `media_of(Artifact(meta={"media": "image"}))` returns `"image"`; a garbage value raises `ValueError`.
- [ ] `extension_for("image") == ".png"`, `extension_for("video") == ".mp4"`.
- [ ] `UpscaleJob(source=..., scale=...).media == "video"` — no existing construction breaks.
- [ ] Iterating `registry.upscaler_names()` finds ≥ 3 engines and exactly `{"spandrel"}` reports `supports_image_input` true.
- [ ] A minimal `UpscalerEngine` subclass that does not declare the attribute reports `False`.

**Verify:** `pixi run python -m pytest tests/core/test_media.py tests/core/test_upscaler_registry.py tests/pipeline/test_upscale_stage.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_media.py
"""The media convention: one definition of "video" | "image" and its default."""

from __future__ import annotations

import pytest

import kinoforge._adapters  # noqa: F401 — self-register every upscaler
from kinoforge.core import registry
from kinoforge.core.interfaces import Artifact, UpscaleJob, UpscalerEngine
from kinoforge.core.scale_target import ScaleTarget


class TestMediaOf:
    def test_absent_meta_means_video(self) -> None:
        # Bug caught: a default of None/"" leaks into extension_for and the
        # orchestrator publishes an upscaled clip with no extension.
        from kinoforge.core.media import media_of

        assert media_of(Artifact(uri="file:///x.mp4")) == "video"

    def test_image_meta_is_read(self) -> None:
        from kinoforge.core.media import media_of

        art = Artifact(uri="file:///x.png", meta={"media": "image"})
        assert media_of(art) == "image"

    def test_garbage_meta_raises(self) -> None:
        # Bug caught: a typo'd meta value silently falls back to video and
        # a PNG is published as .mp4 — the exact defect this module exists
        # to make impossible.
        from kinoforge.core.media import media_of

        with pytest.raises(ValueError, match="media"):
            media_of(Artifact(uri="file:///x", meta={"media": "still"}))


class TestExtensionFor:
    def test_mapping(self) -> None:
        from kinoforge.core.media import extension_for

        assert extension_for("image") == ".png"
        assert extension_for("video") == ".mp4"


class TestUpscaleJobMedia:
    def test_default_is_video(self) -> None:
        # Bug caught: a REQUIRED media field breaks every existing
        # UpscaleJob(...) construction in src/ and tests/.
        job = UpscaleJob(
            source=Artifact(uri="file:///x.mp4"),
            scale=ScaleTarget(kind="factor", value=2.0),
        )
        assert job.media == "video"

    def test_image_is_accepted(self) -> None:
        job = UpscaleJob(
            source=Artifact(uri="file:///x.png"),
            scale=ScaleTarget(kind="factor", value=2.0),
            media="image",
        )
        assert job.media == "image"


class _Bare(UpscalerEngine):
    name = "_bare"
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
        return "_bare"


class TestSupportsImageInput:
    def test_default_is_false(self) -> None:
        # Bug caught: a future engine inherits "yes" by accident and the CLI
        # boots a 10-minute pod for an engine that cannot read a PNG.
        assert _Bare().supports_image_input is False

    def test_only_spandrel_declares_support(self) -> None:
        names = registry.upscaler_names()
        # Guard the guard: a sweep over nothing passes everything.
        assert len(names) >= 3, names
        supporting: set[str] = set()
        for name in names:
            factory = registry.get_upscaler(name)
            flag = getattr(factory, "supports_image_input", None)
            if flag is None:
                flag = factory().supports_image_input
            if flag:
                supporting.add(name)
        assert supporting == {"spandrel"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pixi run python -m pytest tests/core/test_media.py -q`
Expected: FAIL — `ModuleNotFoundError: kinoforge.core.media`, then `TypeError: unexpected keyword 'media'`, then `AttributeError: supports_image_input`.

- [ ] **Step 3: Create `core/media.py`**

```python
# src/kinoforge/core/media.py
"""Media kind of an artifact: ``"video"`` or ``"image"``.

The upscale path carries the input kind as DATA rather than inferring it from
a filename at every hop. The convention lives here so the CLI (which stamps
it onto the input ``Artifact``), ``UpscaleStage`` (which reads it into the
``UpscaleJob``), the engine (which forwards it to the pod) and the
orchestrator (which picks the published extension from it) share one
definition and one default. Pod-side code never imports this module — the pod
receives the string in the ``/upscale`` request body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from kinoforge.core.interfaces import Artifact

Media = Literal["video", "image"]
"""The two media kinds the upscale path understands."""

MEDIA_KEY = "media"
"""The ``Artifact.meta`` key the kind travels under."""

IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg"})
"""Suffixes ``kinoforge upscale --image`` accepts (lower-cased)."""

_EXTENSIONS: dict[str, str] = {"video": ".mp4", "image": ".png"}


def media_of(artifact: Artifact) -> Media:
    """Return the artifact's media kind; an absent key means ``"video"``.

    Args:
        artifact: Any artifact; only ``.meta`` is consulted.

    Returns:
        ``"video"`` or ``"image"``.

    Raises:
        ValueError: ``meta["media"]`` is present but not one of the two kinds.
    """
    raw = artifact.meta.get(MEDIA_KEY, "video")
    if raw not in get_args(Media):
        raise ValueError(
            f"artifact meta[{MEDIA_KEY!r}] must be one of {get_args(Media)}, "
            f"got {raw!r}"
        )
    return raw  # type: ignore[return-value]


def extension_for(media: Media) -> str:
    """Return the published file extension for *media* (``.png`` / ``.mp4``).

    Args:
        media: ``"video"`` or ``"image"``.

    Returns:
        The extension including the leading dot.
    """
    return _EXTENSIONS[media]
```

- [ ] **Step 4: Add the two interface fields**

In `src/kinoforge/core/interfaces.py`, add the import near the other `kinoforge.core` imports at the top of the module:

```python
from kinoforge.core.media import Media
```

Replace the `UpscaleJob` dataclass body (currently ending at `params: dict = field(default_factory=dict)  # type: ignore[type-arg]`):

```python
@dataclass(frozen=True)
class UpscaleJob:
    """One unit of upscale work — engine-agnostic.

    No prompt, no segments, no LoRA stack. The input is a video by default;
    ``media="image"`` marks a still (PNG/JPEG) for engines that declare
    ``supports_image_input``.

    Attributes:
        source: Input Artifact (uri set by ArtifactStore or pointing at a
            local path readable by the engine).
        scale: ScaleTarget. v1 engines MUST raise NotYetImplementedError on
            ``kind="height"``.
        params: Engine-specific overrides (e.g. tile_size, steps, denoise);
            engines validate via ``validate_spec``.
        media: ``"video"`` (default) or ``"image"``. Mirrors the
            ``Artifact.meta["media"]`` convention in :mod:`kinoforge.core.media`.
    """

    source: Artifact
    scale: ScaleTarget
    params: dict = field(default_factory=dict)  # type: ignore[type-arg]
    media: Media = "video"
```

In `UpscalerEngine`, change the docstring's first lines and add the attribute after `supported_scales`:

```python
class UpscalerEngine(ABC):
    """A swappable upscaler; owns env setup; declares supported scales.

    No prompt, no segments, no LoRA stack. Video-in/video-out by default; an
    engine that can also take a still image sets ``supports_image_input``.
    Separate from GenerationEngine because the surfaces don't overlap.

    Attributes:
        name: Registry key (e.g. ``"seedvr2"``).
        requires_compute: True when this engine needs a remote pod.
        requires_local_weights: True when the engine downloads weights into
            the pod's weight directory.
        supported_scales: Declared support; matcher pre-flight + ``validate_spec``
            consult this. Empty tuple means "engine claims to accept any
            ScaleTarget" (use sparingly).
        supports_image_input: True when ``UpscaleJob.media == "image"`` is
            honoured end to end (upload, pod request, runtime). Defaults to
            False so a new engine refuses stills until it is proven on them.
    """

    name: str
    requires_compute: bool
    requires_local_weights: bool
    supported_scales: tuple[ScaleTarget, ...]
    supports_image_input: bool = False
```

In `src/kinoforge/upscalers/spandrel/_engine.py`, inside `class SpandrelEngine`, directly under `_pod_user_agent = _USER_AGENT`:

```python
    # Still-image input (`kinoforge upscale --image`) — the model IS a
    # still-image SR model; the pod runtime's upscale_image() is the consumer.
    supports_image_input = True
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pixi run python -m pytest tests/core/test_media.py tests/core/test_upscaler_registry.py tests/pipeline/test_upscale_stage.py tests/upscalers -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/core/media.py src/kinoforge/core/interfaces.py \
  src/kinoforge/upscalers/spandrel/_engine.py tests/core/test_media.py
pixi run pre-commit run --all-files
git commit -m "feat(core): media kind for upscale inputs — UpscaleJob.media, supports_image_input"
```

---

### Task 2: `UpscaleStage` threads the media into the job

**Goal:** The engine receives `UpscaleJob.media == "image"` when the seeded clip carries `meta["media"] = "image"`, and `"video"` otherwise.

**Files:**
- Modify: `src/kinoforge/pipeline/upscale.py:232-236` (`_engine_call`)
- Test: `tests/pipeline/test_upscale_stage.py` (append a class)

**Acceptance Criteria:**
- [ ] With `artifacts["clip"].meta == {"media": "image"}` the engine's recorded job has `media == "image"`.
- [ ] With no meta the recorded job has `media == "video"`.
- [ ] All existing tests in the four `test_upscale_stage*.py` files stay green.

**Verify:** `pixi run python -m pytest tests/pipeline -q -k upscale_stage` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests** — append to `tests/pipeline/test_upscale_stage.py`:

```python
class TestMediaThreading:
    def test_image_meta_reaches_the_job(self) -> None:
        # Bug caught: the CLI stamps meta["media"]="image" and the stage
        # builds UpscaleJob without it, so the pod receives a "video" job
        # and feeds PNG bytes to the FFMPEG frame reader.
        eng = _FakeEngine()
        stage = UpscaleStage(
            engine=eng, scale=ScaleTarget(kind="factor", value=2.0), instance=None, cfg={}
        )
        clip = Artifact(uri="file:///tmp/in.png", sha256="0" * 64, size=1, meta={"media": "image"})
        stage.run(PipelineState(request=GenerationRequest(prompt="p", mode="t2v"), artifacts={"clip": clip}))
        assert eng.called_with[0].media == "image"

    def test_no_meta_is_video(self) -> None:
        eng = _FakeEngine()
        stage = UpscaleStage(
            engine=eng, scale=ScaleTarget(kind="factor", value=2.0), instance=None, cfg={}
        )
        stage.run(_state())
        assert eng.called_with[0].media == "video"
```

- [ ] **Step 2: Run to verify the first test fails**

Run: `pixi run python -m pytest tests/pipeline/test_upscale_stage.py -q -k MediaThreading`
Expected: `test_image_meta_reaches_the_job` FAILS (`'video' == 'image'`); the second passes already.

- [ ] **Step 3: Implement** — in `src/kinoforge/pipeline/upscale.py` add the import next to the other `kinoforge.core` imports:

```python
from kinoforge.core.media import media_of
```

and change `_engine_call`:

```python
    def _engine_call(self, clip: Artifact, scale: ScaleTarget) -> UpscaleResult:
        # The media kind rides Artifact.meta from the CLI; the engine needs it
        # on the job to pick the upload content type and the pod method.
        job = UpscaleJob(source=clip, scale=scale, media=media_of(clip))
        return self.engine.upscale(
            self.instance, job, self.cfg, cancel_token=self.cancel_token
        )
```

- [ ] **Step 4: Run to verify they pass**

Run: `pixi run python -m pytest tests/pipeline -q -k upscale_stage`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/pipeline/upscale.py tests/pipeline/test_upscale_stage.py
pixi run pre-commit run --all-files
git commit -m "feat(pipeline): UpscaleStage passes the clip's media kind into the job"
```

---

### Task 3: CLI — `--image`, preflight refusals, dry run, media stamp

**Goal:** `kinoforge upscale --image PATH` parses, refuses every §2.1 case with exit 2 before any pod work, prints `media:` in `--dry-run`, and seeds the orchestrator with an `Artifact` whose `meta["media"] == "image"`.

**Files:**
- Modify: `src/kinoforge/cli/_main.py:706-714` (the `upscale` parser's `--video`)
- Modify: `src/kinoforge/cli/_commands.py` — `_cmd_upscale` (1092-1177), `_cmd_interpolate` call sites (1278, 1290), `_video_arg_error` neighbourhood (1347-1385); new helpers `_image_arg_error`, `_image_preflight_error`; rename `_resolve_input_video_as_artifact` → `_resolve_input_as_artifact`
- Test: `tests/cli/test_cmd_upscale_image.py` (new)

**Acceptance Criteria:**
- [ ] `--video` and `--image` together, or neither → argparse `SystemExit(2)`.
- [ ] `--image` with a seedvr2 cfg → rc 2, stderr names `seedvr2` and `--image`.
- [ ] `--image` with `chunk_frames` → rc 2, stderr contains `chunk_frames`; with `tile_grid` → contains `tile_grid`; with cfg `scale: 1080p` → contains `1080p`.
- [ ] `--image` with a `.gif`, a missing file, a directory, or an `https://` source → rc 2 with a message naming the problem.
- [ ] `--image --dry-run` prints `media: image` and `source: <path>` and never reaches `orchestrator.generate`.
- [ ] Non-dry-run `--image` seeds `initial_clip.meta["media"] == "image"`; `--video` seeds `"video"`.
- [ ] `tests/cli/test_cmd_upscale.py`, `test_cmd_upscale_full.py`, `test_cmd_interpolate.py` stay green.

**Verify:** `pixi run python -m pytest tests/cli/test_cmd_upscale_image.py tests/cli/test_cmd_upscale.py tests/cli/test_cmd_upscale_full.py tests/cli/test_cmd_interpolate.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/cli/test_cmd_upscale_image.py
"""`kinoforge upscale --image`: surface, preflight refusals, media stamp.

Every refusal here exits 2 BEFORE any pod work. The non-dry-run tests patch
``kinoforge.core.orchestrator.generate`` so nothing is provisioned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

import kinoforge._adapters  # noqa: F401 — self-register engines + upscalers
from kinoforge.cli._main import main
from kinoforge.core.interfaces import Artifact

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


def _spandrel_cfg(tmp_path: Path, *, scale: str = "2x", extra: str = "") -> Path:
    cfg = tmp_path / "spandrel.yaml"
    cfg.write_text(
        _HEAD
        + "upscale:\n"
        + "  engine: spandrel\n"
        + f"  scale: {scale}\n"
        + extra
        + "  spandrel:\n"
        + "    model_url: hf:foo/bar.pth\n"
        + "    arch: realesrgan\n"
        + "    precision: fp16\n"
        + "    tile_size: 512\n"
        + "    batch_size: 4\n"
    )
    return cfg


def _seedvr2_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "seedvr2.yaml"
    cfg.write_text(
        _HEAD
        + "upscale:\n"
        + "  engine: seedvr2\n"
        + "  scale: 2x\n"
        + "  seedvr2:\n"
        + "    variant: 3B\n"
        + "    precision: fp8\n"
    )
    return cfg


def _png(tmp_path: Path, name: str = "in.png") -> Path:
    import imageio.v3 as iio

    p = tmp_path / name
    iio.imwrite(p, np.zeros((8, 8, 3), dtype=np.uint8))
    return p


@pytest.fixture
def no_generate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any reach into the orchestrator is a test failure."""

    def boom(*a: Any, **kw: Any) -> Any:
        raise AssertionError("orchestrator.generate must not be reached")

    monkeypatch.setattr("kinoforge.core.orchestrator.generate", boom)


class TestArgparse:
    def test_video_and_image_are_mutually_exclusive(self, tmp_path: Path) -> None:
        # Bug caught: both accepted → the handler has two sources and picks
        # one silently.
        cfg = _spandrel_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["upscale", "--video", "a.mp4", "--image", "b.png", "-c", str(cfg)])
        assert exc.value.code == 2

    def test_one_source_is_required(self, tmp_path: Path) -> None:
        cfg = _spandrel_cfg(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["upscale", "-c", str(cfg)])
        assert exc.value.code == 2


class TestConfigRefusals:
    def test_engine_without_image_support_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: a FlashVSR/SeedVR2 cfg boots a 10-minute pod and the
        # server then 400s the image request.
        cfg = _seedvr2_cfg(tmp_path)
        rc = main(["upscale", "--image", str(_png(tmp_path)), "-c", str(cfg), "--dry-run"])
        err = capsys.readouterr().err
        assert rc == 2
        assert "seedvr2" in err and "--image" in err

    def test_chunk_frames_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _spandrel_cfg(tmp_path, extra="  chunk_frames: 16\n")
        rc = main(["upscale", "--image", str(_png(tmp_path)), "-c", str(cfg), "--dry-run"])
        assert rc == 2
        assert "chunk_frames" in capsys.readouterr().err

    def test_tile_grid_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = _spandrel_cfg(tmp_path, extra="  tile_grid: [2, 1]\n")
        rc = main(["upscale", "--image", str(_png(tmp_path)), "-c", str(cfg), "--dry-run"])
        assert rc == 2
        assert "tile_grid" in capsys.readouterr().err

    def test_height_scale_in_cfg_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: only the --scale flag is height-checked; a cfg
        # `scale: 1080p` reaches spandrel's validate_spec after the boot.
        cfg = _spandrel_cfg(tmp_path, scale="1080p")
        rc = main(["upscale", "--image", str(_png(tmp_path)), "-c", str(cfg), "--dry-run"])
        assert rc == 2
        assert "1080p" in capsys.readouterr().err

    def test_video_with_height_scale_is_still_allowed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: the new height refusal leaks onto --video, breaking
        # FlashVSR 1080p dry runs.
        cfg = _spandrel_cfg(tmp_path, scale="1080p")
        rc = main(["upscale", "--video", "x.mp4", "-c", str(cfg), "--dry-run"])
        assert rc == 0
        assert "media: video" in capsys.readouterr().out


class TestPathRefusals:
    def test_bad_suffix_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad = tmp_path / "in.gif"
        bad.write_bytes(b"GIF89a")
        rc = main(["upscale", "--image", str(bad), "-c", str(_spandrel_cfg(tmp_path))])
        assert rc == 2
        assert ".gif" in capsys.readouterr().err

    def test_missing_file_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["upscale", "--image", str(tmp_path / "nope.png"), "-c", str(_spandrel_cfg(tmp_path))])
        assert rc == 2
        assert "does not exist" in capsys.readouterr().err

    def test_directory_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = main(["upscale", "--image", str(tmp_path), "-c", str(_spandrel_cfg(tmp_path))])
        assert rc == 2
        assert "not a file" in capsys.readouterr().err

    def test_url_exits_2(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Bug caught: --video's http(s) passthrough is copied to --image and
        # the pod infers the kind from a URL it has not fetched.
        rc = main(["upscale", "--image", "https://x/y.png", "-c", str(_spandrel_cfg(tmp_path))])
        assert rc == 2
        assert "local file" in capsys.readouterr().err


class TestDryRun:
    def test_prints_media_and_source(
        self, tmp_path: Path, no_generate: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        png = _png(tmp_path)
        rc = main(["upscale", "--image", str(png), "-c", str(_spandrel_cfg(tmp_path)), "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "media: image" in out
        assert f"source: {png}" in out


class TestMediaStamp:
    def _run(self, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_generate(cfg: Any, request: Any, **kw: Any) -> Any:
            captured.update(kw)
            return (Artifact(uri="file:///out", sha256="x", size=1), None)

        monkeypatch.setattr("kinoforge.core.orchestrator.generate", fake_generate)
        assert main(argv) == 0
        return captured

    def test_image_seeds_image_media(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bug caught: the flag parses, the preflight passes, and the seeded
        # artifact still says video — the pod gets a "video" job.
        png = _png(tmp_path)
        captured = self._run(
            ["upscale", "--image", str(png), "-c", str(_spandrel_cfg(tmp_path)), "--no-reuse"],
            monkeypatch,
        )
        assert captured["initial_clip"].meta["media"] == "image"
        assert captured["initial_clip"].uri == f"file://{png.resolve()}"

    def test_video_seeds_video_media(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        mp4 = tmp_path / "in.mp4"
        mp4.write_bytes(b"x")
        captured = self._run(
            ["upscale", "--video", str(mp4), "-c", str(_spandrel_cfg(tmp_path)), "--no-reuse"],
            monkeypatch,
        )
        assert captured["initial_clip"].meta["media"] == "video"
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/cli/test_cmd_upscale_image.py -q`
Expected: argparse tests fail with `unrecognized arguments: --image`; the rest fail on exit code / missing text.

- [ ] **Step 3: Implement the parser** — in `src/kinoforge/cli/_main.py` replace the `upscale` parser's `help` and `--video` declaration (lines 707-714):

```python
    # upscale (T15) — standalone upscale subcommand: a video clip, or a
    # still image for engines declaring supports_image_input (spandrel).
    p_upscale = sub.add_parser("upscale", help="upscale a video clip or a still image")
    p_upscale.add_argument("-c", "--config", required=True, metavar="PATH")
    p_upscale_src = p_upscale.add_mutually_exclusive_group(required=True)
    p_upscale_src.add_argument(
        "--video",
        metavar="PATH_OR_URL",
        help="source mp4 (file path or http(s)://... URL)",
    )
    p_upscale_src.add_argument(
        "--image",
        metavar="PATH",
        help=(
            "source still image (.png/.jpg/.jpeg, local file only). Engine must "
            "support image input (spandrel); output is always PNG."
        ),
    )
```

- [ ] **Step 4: Implement the handler changes** — in `src/kinoforge/cli/_commands.py`:

Add to the runtime imports near `from kinoforge.core.lifecycle import destroy_confirmed`:

```python
from kinoforge.core.media import IMAGE_SUFFIXES, MEDIA_KEY
```

and under the existing `if TYPE_CHECKING:` block:

```python
    from kinoforge.core.media import Media
```

Replace the body of `_cmd_upscale` from `if ctx.cfg is None:` through `input_artifact = _resolve_input_video_as_artifact(args.video)` with:

```python
    if ctx.cfg is None:
        print("error: --config required for upscale", file=sys.stderr)
        return 2
    cfg = ctx.cfg
    if cfg.upscale is None:
        print(
            "error: --config must contain an `upscale:` block; "
            "see examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml",
            file=sys.stderr,
        )
        return 2

    # CLI override takes precedence over cfg.upscale.scale.
    if scale is None:
        try:
            scale = ScaleTarget.parse(cfg.upscale.scale)
        except ValueError as exc:
            print(f"error: invalid cfg.upscale.scale: {exc}", file=sys.stderr)
            return 2

    # --video | --image is a required argparse mutex group, so exactly one
    # is set. The kind travels as DATA from here on (core/media.py).
    media: Media = "image" if getattr(args, "image", None) else "video"
    source: str = args.image if media == "image" else args.video

    # Config-fact refusals fire BEFORE --dry-run prints so a dry run surfaces
    # them too, and long before any pod work.
    if media == "image" and (pre_err := _image_preflight_error(cfg, scale)) is not None:
        print(pre_err, file=sys.stderr)
        return 2

    if getattr(args, "dry_run", False):
        print("upscale plan:")
        print(f"  source: {source}")
        print(f"  media: {media}")
        print(f"  scale: {raw_scale or cfg.upscale.scale}")
        print(f"  engine: {cfg.upscale.engine}")
        if cfg.upscale.seedvr2 is not None:
            print(
                f"  seedvr2: variant={cfg.upscale.seedvr2.variant} "
                f"precision={cfg.upscale.seedvr2.precision}"
            )
        print(f"  no_reuse: {bool(getattr(args, 'no_reuse', False))}")
        print(f"  attach_pod: {getattr(args, 'attach_pod', None)}")
        return 0

    if media == "image":
        if (img_err := _image_arg_error(source)) is not None:
            print(img_err, file=sys.stderr)
            return 2
    elif (video_err := _video_arg_error(source)) is not None:
        print(video_err, file=sys.stderr)
        return 2

    # T11 — non-dry-run wiring. Reuses generate()'s machinery via the
    # skip_clip_stage flag (T10). Mirrors _cmd_generate's warm-reuse /
    # attach / cold-create precedence chain.
    from kinoforge.core import orchestrator as _orchestrator

    del scale  # ScaleTarget recomputed inside UpscaleStage via cfg.upscale.scale

    input_artifact = _resolve_input_as_artifact(source, media)
```

In `_cmd_interpolate` change line 1290 to:

```python
    input_artifact = _resolve_input_as_artifact(args.video, "video")
```

Add the two new helpers directly after `_video_arg_error` and rename/extend the resolver:

```python
def _image_arg_error(image: str) -> str | None:
    """Return a CLI error message for a bad ``--image`` arg, else ``None``.

    Mirrors :func:`_video_arg_error` with two differences: a URL is refused
    (the pod would have to infer the kind from a path it has not fetched)
    and the suffix must be one of :data:`kinoforge.core.media.IMAGE_SUFFIXES`.

    Args:
        image: The raw ``--image`` value.

    Returns:
        An ``error: ...`` line, or ``None`` when the path is usable.
    """
    if not image:
        return "error: --image is empty (no input path)"
    if image.startswith(("http://", "https://")):
        return (
            "error: --image must be a local file; http(s):// sources are not "
            "supported for still images"
        )
    p = Path(image)
    if not p.exists():
        return f"error: --image path does not exist: {image}"
    if not p.is_file():
        return f"error: --image is not a file: {image}"
    if p.suffix.lower() not in IMAGE_SUFFIXES:
        return (
            f"error: --image suffix {p.suffix!r} is not accepted; "
            f"use one of {sorted(IMAGE_SUFFIXES)}"
        )
    return None


def _image_preflight_error(cfg: Config, scale: ScaleTarget) -> str | None:
    """Return the exit-2 message for an ``--image`` run this config cannot serve.

    Spec §2.1 items 2-4, in order: a height-target scale, an engine without
    ``supports_image_input``, then ``chunk_frames`` / ``tile_grid``. All are
    config facts, so they fire before ``--dry-run`` prints and before any
    pod work.

    Args:
        cfg: Loaded config; ``cfg.upscale`` must be present (caller checked).
        scale: The effective scale (CLI override or ``cfg.upscale.scale``).

    Returns:
        An ``error: ...`` line, or ``None`` when the config can serve a still.
    """
    from kinoforge.core import registry

    block = cfg.upscale
    assert block is not None  # noqa: S101 — _cmd_upscale checked before calling
    if scale.kind == "height":
        return (
            f"error: --image cannot use a height-target scale "
            f"({int(scale.value)}p); use --scale Nx (height targets for stills "
            "are deferred)"
        )
    try:
        factory = registry.get_upscaler(block.engine)
    except UnknownAdapter as exc:
        return f"error: {exc}"
    supports = getattr(factory, "supports_image_input", None)
    if supports is None:
        supports = factory().supports_image_input
    if not supports:
        return (
            f"error: upscale engine {block.engine!r} does not support --image "
            "(still-image input); use an engine that does, e.g. spandrel"
        )
    if block.chunk_frames is not None:
        return (
            "error: --image cannot be combined with upscale.chunk_frames "
            "(temporal chunking is video-only; a still is tiled on the pod via "
            "spandrel.tile_size)"
        )
    if block.tile_grid is not None:
        return (
            "error: --image cannot be combined with upscale.tile_grid "
            "(controller-side video tiling; a still is tiled on the pod via "
            "spandrel.tile_size)"
        )
    return None


def _resolve_input_as_artifact(path_or_url: str, media: Media) -> Artifact:
    """Materialise a ``--video`` / ``--image`` arg as a kinoforge Artifact.

    Local file path → ``file://`` URL + sha256 from disk + size from stat.
    ``http(s)://`` URL → passthrough; sha256/size deferred to the pod-side
    fetch. Either way ``meta["media"]`` carries *media* so every later layer
    (stage, engine, orchestrator publish) reads the kind rather than guessing
    it from the filename.

    Args:
        path_or_url: The raw CLI source value.
        media: ``"video"`` or ``"image"``.

    Returns:
        The input artifact seeded into ``state.artifacts["clip"]``.
    """
    import hashlib as _hashlib

    meta = {MEDIA_KEY: media}
    if path_or_url.startswith(("http://", "https://")):
        return Artifact(uri=path_or_url, sha256="", size=0, meta=meta)
    p = Path(path_or_url).resolve()
    h = _hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return Artifact(
        uri=f"file://{p}", sha256=h.hexdigest(), size=p.stat().st_size, meta=meta
    )
```

Delete the old `_resolve_input_video_as_artifact` (its body is now the function above). Update the two comment references in `tests/live/_u13_hang_probe.py:101` and `tests/cli/test_cmd_interpolate.py:105` to the new name.

- [ ] **Step 5: Run to verify they pass**

Run: `pixi run python -m pytest tests/cli/test_cmd_upscale_image.py tests/cli/test_cmd_upscale.py tests/cli/test_cmd_upscale_full.py tests/cli/test_cmd_interpolate.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/cli/_main.py src/kinoforge/cli/_commands.py \
  tests/cli/test_cmd_upscale_image.py tests/cli/test_cmd_interpolate.py tests/live/_u13_hang_probe.py
pixi run pre-commit run --all-files
git commit -m "feat(cli): kinoforge upscale --image — preflight refusals, dry run, media stamp"
```

---

### Task 4: Client side — upload content type and the `/upscale` payload

**Goal:** `SpandrelEngine.upscale` uploads a still with `image/png` or `image/jpeg` and a matching suffix, sends `"media": "image"` in the submit payload, and stamps the result artifact's meta.

**Files:**
- Modify: `src/kinoforge/engines/_pod_http.py:191-240` (`_upload_source`)
- Modify: `src/kinoforge/upscalers/spandrel/_engine.py:155-205` (`upscale`)
- Test: `tests/upscalers/test_spandrel_image_upload.py` (new)

**Acceptance Criteria:**
- [ ] `_upload_source(instance, x.png, media="image")` sends `Content-Type: image/png` and `X-Filename: <sha8>.png`; `.jpg` → `image/jpeg` / `.jpg`.
- [ ] `_upload_source(instance, x.mp4)` (no `media`) still sends `video/mp4` / `<sha8>.mp4`.
- [ ] `_upload_source(..., media="image")` on a `.gif` raises `ValueError` before any HTTP call.
- [ ] `engine.upscale(..., UpscaleJob(media="image"))` passes `media="image"` to the upload, puts `"media": "image"` in the POST payload, and returns `UpscaleResult.artifact.meta["media"] == "image"`.
- [ ] A default job sends `"media": "video"`.
- [ ] `tests/upscalers/test_spandrel_upload.py` and `test_spandrel_engine.py` stay green.

**Verify:** `pixi run python -m pytest tests/upscalers -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/upscalers/test_spandrel_image_upload.py
"""Still-image input through SpandrelEngine: upload headers + /upscale payload."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from kinoforge.core.interfaces import Artifact, Instance, UpscaleJob
from kinoforge.core.scale_target import ScaleTarget


def _instance() -> Instance:
    return Instance(
        id="pod-fake", provider="fake", status="ready", created_at=0.0,
        endpoints={"8000": "https://pod.example/proxy"}, tags={},
    )


def _cfg() -> dict[str, object]:
    return {
        "upscale": {
            "engine": "spandrel", "scale": "2x",
            "spandrel": {
                "model_url": "hf:ai-forever/Real-ESRGAN/RealESRGAN_x2.pth",
                "arch": "realesrgan", "precision": "fp16", "tile_size": 512, "batch_size": 4,
            },
        },
    }


def _job(uri: str, media: str = "video") -> UpscaleJob:
    return UpscaleJob(
        source=Artifact(uri=uri, sha256="0" * 64, size=1),
        scale=ScaleTarget(kind="factor", value=2.0),
        media=media,  # type: ignore[arg-type]
    )


def _file(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(bytes(i % 256 for i in range(4096)))
    return p


def _capture_put(engine: Any) -> tuple[Any, dict[str, Any]]:
    seen: dict[str, Any] = {}

    def fake_put(url: str, data: Any, headers: dict[str, str], timeout: int) -> dict[str, Any]:
        seen["headers"] = dict(headers)
        body = data.read()
        return {"path": f"/tmp/kf-uploads/{headers['X-Filename']}", "size": len(body),
                "sha256": hashlib.sha256(body).hexdigest()}

    return patch.object(engine, "_put_upload", side_effect=fake_put), seen


class TestUploadHeaders:
    @pytest.mark.parametrize(
        ("name", "ctype", "suffix"),
        [("in.png", "image/png", ".png"), ("in.jpg", "image/jpeg", ".jpg"), ("in.JPEG", "image/jpeg", ".jpeg")],
    )
    def test_image_headers_follow_the_suffix(self, tmp_path: Path, name: str, ctype: str, suffix: str) -> None:
        # Bug caught: the pod's /upload sees video/mp4 for a PNG body and
        # stores it under <sha8>.mp4 — the runtime then FFMPEG-decodes a PNG.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        engine = SpandrelEngine()
        src = _file(tmp_path, name)
        patcher, seen = _capture_put(engine)
        with patcher:
            url = engine._upload_source(_instance(), src, media="image")
        sha8 = hashlib.sha256(src.read_bytes()).hexdigest()[:8]
        assert seen["headers"]["Content-Type"] == ctype
        assert seen["headers"]["X-Filename"] == f"{sha8}{suffix}"
        assert url.endswith(suffix)

    def test_video_headers_unchanged(self, tmp_path: Path) -> None:
        # Bug caught: the media branch changes the proven video header pair.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        engine = SpandrelEngine()
        src = _file(tmp_path, "in.mp4")
        patcher, seen = _capture_put(engine)
        with patcher:
            engine._upload_source(_instance(), src)
        sha8 = hashlib.sha256(src.read_bytes()).hexdigest()[:8]
        assert seen["headers"]["Content-Type"] == "video/mp4"
        assert seen["headers"]["X-Filename"] == f"{sha8}.mp4"

    def test_image_with_unknown_suffix_raises_before_http(self, tmp_path: Path) -> None:
        from kinoforge.upscalers.spandrel import SpandrelEngine

        engine = SpandrelEngine()
        src = _file(tmp_path, "in.gif")
        patcher, seen = _capture_put(engine)
        with patcher, pytest.raises(ValueError, match="png"):
            engine._upload_source(_instance(), src, media="image")
        assert "headers" not in seen


class TestUpscalePayload:
    def _drive(self, media: str, src: Path) -> tuple[dict[str, Any], Any, Any]:
        from kinoforge.upscalers.spandrel import SpandrelEngine
        from kinoforge.upscalers.spandrel import _engine as spandrel_mod

        engine = SpandrelEngine()
        captured: dict[str, Any] = {}

        def fake_http(*, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
            if method == "POST":
                assert payload is not None
                captured.update(payload)
                return {"job_id": "j-img"}
            return {"state": "done", "progress": 1.0, "error": None,
                    "result": {"filename": "out.upscaled.png", "sha256": "z", "size": 1,
                               "input_resolution": [8, 8], "output_resolution": [16, 16], "engine_meta": {}}}

        with (
            patch.object(engine, "_upload_source", return_value="file:///tmp/kf-uploads/up.bin") as upl,
            patch.object(spandrel_mod, "_http_json", side_effect=fake_http),
        ):
            result = engine.upscale(_instance(), _job(f"file://{src}", media), _cfg())
        return captured, upl, result

    def test_image_job_forwards_media_everywhere(self, tmp_path: Path) -> None:
        # Bug caught: media reaches the upload but not the payload (or vice
        # versa), or the result artifact loses it so the orchestrator
        # publishes the PNG as .mp4.
        captured, upl, result = self._drive("image", _file(tmp_path, "in.png"))
        assert upl.call_args.kwargs["media"] == "image"
        assert captured["media"] == "image"
        assert result.artifact.meta["media"] == "image"

    def test_video_job_sends_media_video(self, tmp_path: Path) -> None:
        captured, upl, result = self._drive("video", _file(tmp_path, "in.mp4"))
        assert upl.call_args.kwargs["media"] == "video"
        assert captured["media"] == "video"
        assert result.artifact.meta["media"] == "video"
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/upscalers/test_spandrel_image_upload.py -q`
Expected: FAIL — `TypeError: _upload_source() got an unexpected keyword argument 'media'`, `KeyError: 'media'`.

- [ ] **Step 3: Implement the upload seam** — in `src/kinoforge/engines/_pod_http.py` add a module constant near `_UPLOAD_TIMEOUT_S`:

```python
_IMAGE_CONTENT_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
```

and change `_upload_source`'s signature, docstring and header block:

```python
    def _upload_source(
        self, instance: Instance, local_path: Path, *, media: str = "video"
    ) -> str:
        """Upload ``local_path`` to the pod via PUT /upload; return its file:// URL.

        Computes sha256 locally, streams the file body as the PUT payload,
        and cross-checks the server's reported sha256 before returning.
        Recovers once from a proxy cold-warmup 502; subsequent failures
        bubble. For ``media="video"`` the body is sent as ``video/mp4`` under
        ``<sha8>.mp4``; for ``media="image"`` the content type and suffix
        follow the local file's suffix (``.png`` / ``.jpg`` / ``.jpeg``).

        Args:
            instance: Compute instance exposing the pod server endpoint.
            local_path: Local file to upload.
            media: ``"video"`` (default) or ``"image"``.

        Returns:
            ``file://`` URL of the uploaded file on the pod.

        Raises:
            ValueError: ``media="image"`` with a suffix outside png/jpg/jpeg.
            UploadIntegrityError: Server-reported sha256 does not match
                the locally computed one.
        """
        if media == "image":
            suffix = local_path.suffix.lower()
            content_type = _IMAGE_CONTENT_TYPES.get(suffix)
            if content_type is None:
                raise ValueError(
                    f"image upload needs a .png/.jpg/.jpeg source, got {local_path.name!r}"
                )
        else:
            suffix, content_type = ".mp4", "video/mp4"
        body = local_path.read_bytes()
        local_sha = hashlib.sha256(body).hexdigest()
        short = local_sha[:8]
        url = f"{self._base_url(instance)}/upload"
        headers = {
            "Content-Type": content_type,
            "X-Filename": f"{short}{suffix}",
            "Content-Length": str(len(body)),
            "User-Agent": self._pod_user_agent,
        }
```

(the retry loop and integrity check below are unchanged.)

- [ ] **Step 4: Implement the engine threading** — in `src/kinoforge/upscalers/spandrel/_engine.py`, inside `upscale`:

```python
        if source_uri.startswith("file://") or source_uri.startswith("/"):
            local_path = Path(source_uri.removeprefix("file://"))
            source_uri = self._upload_source(instance, local_path, media=job.media)
```

add `"media": job.media,` to `submit_payload` after `"engine": "spandrel",`, and stamp the result:

```python
        return UpscaleResult(
            artifact=Artifact(
                uri=f"{base}/artifacts/{result['filename']}",
                sha256=result["sha256"],
                size=result["size"],
                # The kind rides the artifact forward so the orchestrator's
                # publish step picks .png for a still (core/media.py).
                meta={"media": job.media},
            ),
```

Update the class docstring to `"""spandrel-based image super-resolution: per-frame video upscaler, and still-image upscaler via ``UpscaleJob.media="image"``."""`.

- [ ] **Step 5: Run to verify they pass**

Run: `pixi run python -m pytest tests/upscalers -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/engines/_pod_http.py src/kinoforge/upscalers/spandrel/_engine.py \
  tests/upscalers/test_spandrel_image_upload.py
pixi run pre-commit run --all-files
git commit -m "feat(upscalers): spandrel forwards the media kind — image upload headers and payload"
```

---

### Task 5: Pod server — `/upload` content types, `UpscaleRequest.media`, dispatch

**Goal:** The pod accepts PNG/JPEG uploads, refuses `media="image"` for non-spandrel engines at submit, and dispatches image jobs to `pipe.upscale_image`.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` — `UpscaleRequest` (~2364), `upscale_handler` (~2448), `_run_upscale_job` (~2531-2535), `upload_handler` (~2692-2708)
- Test: `tests/engines/diffusers/test_server_upscale_image.py` (new)

**Acceptance Criteria:**
- [ ] `POST /upscale` with `media="image"`, `engine="flashvsr"` → 400 and `_ensure_on_gpu` was called zero times.
- [ ] `media="image"`, `engine="spandrel"` → job reaches `done`, `pipe.upscale_image` called once, `pipe.upscale` never.
- [ ] Default request (no `media`) → `pipe.upscale` called, `pipe.upscale_image` never.
- [ ] `PUT /upload` with `image/png` → 200, stored path ends `.png`, sha256 matches; `video/mp4` still 200; `text/plain` → 415.
- [ ] `tests/engines/diffusers/test_server_upscale.py` and `test_server_upload_cleanup.py` stay green.
- [ ] No new `kinoforge.*` import in the server module.

**Verify:** `pixi run python -m pytest tests/engines/diffusers/test_server_upscale_image.py tests/engines/diffusers/test_server_upscale.py tests/engines/diffusers/test_server_upload_cleanup.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/engines/diffusers/test_server_upscale_image.py
"""Still-image input on the pod: /upload content types, /upscale media dispatch."""

from __future__ import annotations

import hashlib
import importlib
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def srv_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Fresh server with CUDA bypassed; yields (srv, client, fake_pipe, gpu_calls, out_png)."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

    importlib.reload(srv)

    import imageio.v3 as iio

    out_png = tmp_path / "out.upscaled.png"
    iio.imwrite(out_png, np.zeros((16, 16, 3), dtype=np.uint8))
    out_mp4 = tmp_path / "out.mp4"
    out_mp4.write_bytes(b"\x00" * 64)

    fake_pipe = MagicMock(name="SpandrelPipe")
    fake_pipe.upscale = MagicMock(return_value=out_mp4)
    fake_pipe.upscale_image = MagicMock(return_value=out_png)
    fake_loaded = {"name": "spandrel-realesrgan-fp16", "pipe": fake_pipe, "vram_bytes": 1,
                   "last_used_monotonic": 0.0, "on_device": "cuda"}
    gpu_calls: list[str] = []

    async def _fake_ensure_on_gpu(name: str) -> dict[str, Any]:
        gpu_calls.append(name)
        return fake_loaded

    monkeypatch.setattr(srv, "_ensure_on_gpu", _fake_ensure_on_gpu)
    monkeypatch.setattr(srv, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(srv, "_UPLOAD_DIR", tmp_path / "kf-uploads")
    monkeypatch.setattr(srv, "LORAS_DIR", tmp_path / "loras")
    monkeypatch.setattr(srv, "_load_pipeline", lambda **_kw: MagicMock())
    monkeypatch.setattr(srv, "_pipe_arity", 1)
    with TestClient(srv.app) as client:
        yield srv, client, fake_pipe, gpu_calls, out_png


def _wait(client: TestClient, job_id: str, timeout_s: float = 3.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        r = client.get(f"/upscale/status/{job_id}")
        if r.status_code == 200:
            last = r.json()
            if last.get("state") in {"done", "error"}:
                return last
        time.sleep(0.01)
    raise AssertionError(f"job never finished: {last}")


def _src_png(tmp_path: Path) -> Path:
    import imageio.v3 as iio

    p = tmp_path / "src.png"
    iio.imwrite(p, np.full((8, 8, 3), 7, dtype=np.uint8))
    return p


def _body(src: Path, media: str | None, engine: str = "spandrel") -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_url": f"file://{src}", "source_filename": src.name, "scale": "2x",
        "engine": engine, "spandrel": {"arch": "realesrgan", "precision": "fp16"},
        "flashvsr": {"debug_stats": False} if engine == "flashvsr" else None,
    }
    if media is not None:
        body["media"] = media
    return body


class TestSubmitRefusal:
    def test_image_with_non_spandrel_engine_is_400_before_any_load(self, srv_env: Any, tmp_path: Path) -> None:
        # Bug caught: the refusal happens inside _run_upscale_job AFTER
        # _ensure_on_gpu — a FlashVSR load (minutes) for a request that was
        # always going to fail.
        srv, client, pipe, gpu_calls, _ = srv_env
        r = client.post("/upscale", json=_body(_src_png(tmp_path), "image", engine="flashvsr"))
        assert r.status_code == 400
        assert "image" in r.json()["detail"]
        assert gpu_calls == []


class TestDispatch:
    def test_image_dispatches_to_upscale_image(self, srv_env: Any, tmp_path: Path) -> None:
        # Bug caught: media is accepted and ignored — PNG bytes go through
        # pipe.upscale and the FFMPEG reader.
        srv, client, pipe, gpu_calls, out_png = srv_env
        r = client.post("/upscale", json=_body(_src_png(tmp_path), "image"))
        assert r.status_code == 200
        st = _wait(client, r.json()["job_id"])
        assert st["state"] == "done", st
        pipe.upscale_image.assert_called_once()
        pipe.upscale.assert_not_called()
        assert st["result"]["filename"] == out_png.name

    def test_default_media_dispatches_to_upscale(self, srv_env: Any, tmp_path: Path) -> None:
        srv, client, pipe, gpu_calls, _ = srv_env
        r = client.post("/upscale", json=_body(_src_png(tmp_path), None))
        assert r.status_code == 200
        st = _wait(client, r.json()["job_id"])
        assert st["state"] == "done", st
        pipe.upscale.assert_called_once()
        pipe.upscale_image.assert_not_called()


class TestUploadContentTypes:
    @pytest.mark.parametrize(("ctype", "name"), [("image/png", "a1b2c3d4.png"), ("image/jpeg", "a1b2c3d4.jpg"), ("video/mp4", "a1b2c3d4.mp4")])
    def test_accepted_types_store_under_their_name(self, srv_env: Any, ctype: str, name: str) -> None:
        # Bug caught: /upload keeps the video/mp4-only gate and every image
        # upload is a 415 after the pod has booted.
        srv, client, *_ = srv_env
        body = b"\x89PNG\r\n" + bytes(range(64))
        r = client.put("/upload", content=body, headers={"Content-Type": ctype, "X-Filename": name})
        assert r.status_code == 200, r.text
        assert r.json()["path"].endswith(name)
        assert r.json()["sha256"] == hashlib.sha256(body).hexdigest()

    def test_other_types_are_still_415(self, srv_env: Any) -> None:
        # Bug caught: the gate was widened to "anything".
        srv, client, *_ = srv_env
        r = client.put("/upload", content=b"hello", headers={"Content-Type": "text/plain", "X-Filename": "x.txt"})
        assert r.status_code == 415
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/engines/diffusers/test_server_upscale_image.py -q`
Expected: refusal test fails (200 instead of 400), dispatch test fails (`upscale` called), PNG upload fails with 415.

- [ ] **Step 3: Implement** — in `wan_t2v_server.py`:

`UpscaleRequest`: add after `engine: str`:

```python
    # "video" (default) or "image" — a still is dispatched to the runtime's
    # upscale_image(); only engines in _IMAGE_CAPABLE_ENGINES accept it.
    media: Literal["video", "image"] = "video"
```

Above `upscale_handler` add:

```python
_IMAGE_CAPABLE_ENGINES: frozenset[str] = frozenset({"spandrel"})
```

In `upscale_handler`, after the unsupported-engine check:

```python
    if req.media == "image" and req.engine not in _IMAGE_CAPABLE_ENGINES:
        # Refuse at SUBMIT so the client sees it on its first poll instead of
        # after a multi-minute model load inside _run_upscale_job.
        raise HTTPException(
            status_code=400,
            detail=(
                f"engine {req.engine!r} does not support image input; "
                f"image-capable engines: {sorted(_IMAGE_CAPABLE_ENGINES)}"
            ),
        )
```

In `_run_upscale_job`, replace

```python
            out_path = await asyncio.to_thread(
                entry["pipe"].upscale, local, scale, params
            )
```

with

```python
            # A still goes to upscale_image (tiled, writes <stem>.upscaled.png);
            # a clip keeps the proven frame-loop path.
            method = (
                entry["pipe"].upscale_image
                if req.media == "image"
                else entry["pipe"].upscale
            )
            out_path = await asyncio.to_thread(method, local, scale, params)
```

Above `upload_handler` add:

```python
_UPLOAD_CONTENT_TYPES: frozenset[str] = frozenset(
    {"video/mp4", "image/png", "image/jpeg"}
)
```

and replace its content-type gate:

```python
    ct = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ct not in _UPLOAD_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Content-Type must be one of {sorted(_UPLOAD_CONTENT_TYPES)}, "
                f"got {ct!r}"
            ),
        )
```

Update the `upload_handler` docstring's first two lines to "Stream-write an mp4 / PNG / JPEG body into ``_UPLOAD_DIR``; return path + size + sha256. Content-Type must be in ``_UPLOAD_CONTENT_TYPES``." and `_sanitize_upload_filename`'s fallback comment stays (`<hex8>.mp4` — only reached when the client sent no usable name, which the kinoforge client never does).

- [ ] **Step 4: Run to verify they pass**

Run: `pixi run python -m pytest tests/engines/diffusers/test_server_upscale_image.py tests/engines/diffusers/test_server_upscale.py tests/engines/diffusers/test_server_upload_cleanup.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
  tests/engines/diffusers/test_server_upscale_image.py
pixi run pre-commit run --all-files
git commit -m "feat(server): /upload takes PNG/JPEG; /upscale media=image dispatches to upscale_image"
```

---

### Task 6: `SpandrelRuntime.upscale_image` — tiled still-image inference

**Goal:** The pod runtime upscales a still to `<stem>.upscaled.png`, honouring `tile_size` with a 32 px overlap through a pure-numpy tiler, while the video path is unchanged.

**Torch is not installed in any pixi env here** (the three inference tests in `tests/upscalers/test_spandrel_runtime.py` already skip). So the tiler is a module-level pure function taking an `infer` callable, and the two torch seams on the runtime (`_place_model`, `_infer`) are instance methods a test can monkeypatch with numpy fakes. Every test below runs without torch; the live pod in Task 9 is the torch proof.

**Files:**
- Modify: `src/kinoforge/upscalers/spandrel/_runtime.py` (whole class + two module functions)
- Modify: `src/kinoforge/core/config.py:682` (`SpandrelEngineConfig.tile_size` docstring line)
- Test: `tests/upscalers/test_spandrel_runtime_image.py` (new)

**Acceptance Criteria:**
- [ ] `tile_upscale` on a 900×1100 RGB array with `tile=512, overlap=32, scale=2` equals the whole-image result pixel-for-pixel under a nearest-neighbour `infer`, and `infer` was called exactly 6 times.
- [ ] `tile_upscale` with `tile=0`, or an image no larger than one tile, calls `infer` exactly once.
- [ ] Every `infer` call receives a batch whose spatial dims are ≤ `tile + 2*overlap` on each side (the VRAM bound the tiler exists for).
- [ ] `_to_rgb`: RGBA → RGB (alpha dropped), 2-D → 3-channel, RGB unchanged, 2-channel raises `ValueError`.
- [ ] `upscale_image` on a 70×100 RGBA PNG (torch seams faked) writes `in.upscaled.png` of shape `(140, 200, 3)`; `.jpg` input → `.png` output.
- [ ] Height target → `NotYetImplementedError`; 4x on 2x weights → `UnsupportedScaleError`, both before `_place_model` is touched.
- [ ] `upscale` (video, torch seams faked) with `tile_size=16` on a 4-frame 64×48 clip, `batch_size=4` → `_infer` called once with a `(4, 48, 64, 3)` batch — the video path is untiled.
- [ ] `tests/upscalers/test_spandrel_runtime.py` stays green/skipped exactly as before (5 passed, 3 skipped).

**Verify:** `pixi run python -m pytest tests/upscalers/test_spandrel_runtime_image.py tests/upscalers/test_spandrel_runtime.py -q -rs` → new file all PASS (no skips); old file `5 passed, 3 skipped`.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/upscalers/test_spandrel_runtime_image.py
"""SpandrelRuntime.upscale_image — still in, PNG out, tiled by tile_size.

No torch here: the tiler is pure numpy and the runtime's two torch seams
(``_place_model``, ``_infer``) are monkeypatched with numpy fakes. The real
model runs only on the Task 9 pod.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget

_2X = ScaleTarget(kind="factor", value=2.0)


def _nn2x(batch: np.ndarray) -> np.ndarray:
    """Nearest-neighbour 2x on a uint8 NHWC batch — content-dependent on purpose.

    A zeros fake would make "tiled == untiled" vacuously true and hide seam
    and offset bugs.
    """
    return np.repeat(np.repeat(batch, 2, axis=1), 2, axis=2)


class _Counter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, ...]] = []

    def __call__(self, batch: np.ndarray) -> np.ndarray:
        self.calls.append(batch.shape)
        return _nn2x(batch)


@pytest.fixture
def fake_spandrel(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Inject a fake `spandrel` module so construction needs no weights."""
    model = MagicMock(name="SpandrelModel")
    model.scale = 2
    loader = MagicMock()
    loader.return_value.load_from_file = MagicMock(return_value=model)
    monkeypatch.setitem(sys.modules, "spandrel", types.SimpleNamespace(ModelLoader=loader))
    return model


def _rt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, tile_size: int, batch_size: int = 4) -> tuple[Any, _Counter]:
    """Runtime with both torch seams replaced by numpy fakes; returns (rt, infer_counter)."""
    from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

    weights = tmp_path / "fake.pth"
    weights.write_bytes(b"")
    rt = SpandrelRuntime(weights_path=weights, precision="fp32", tile_size=tile_size, batch_size=batch_size)
    counter = _Counter()
    monkeypatch.setattr(rt, "_place_model", lambda: ("cpu", None))
    monkeypatch.setattr(rt, "_infer", lambda batch, device, dtype: counter(batch))
    return rt, counter


def _write(path: Path, arr: np.ndarray) -> Path:
    import imageio.v3 as iio

    iio.imwrite(path, arr)
    return path


def _read(path: Path) -> np.ndarray:
    import imageio.v3 as iio

    return np.asarray(iio.imread(path))


class TestTileUpscale:
    def test_tiled_equals_untiled_and_actually_tiles(self) -> None:
        # Bug caught: seams, off-by-one at the right/bottom remainder tiles,
        # overlap interiors copied from the wrong offset.
        from kinoforge.upscalers.spandrel._runtime import tile_upscale

        rng = np.random.default_rng(1)
        img = rng.integers(0, 255, (900, 1100, 3), dtype=np.uint8)
        whole = tile_upscale(img, scale=2, tile=0, overlap=32, infer=_nn2x)
        counter = _Counter()
        tiled = tile_upscale(img, scale=2, tile=512, overlap=32, infer=counter)
        assert whole.shape == (1800, 2200, 3)
        assert np.array_equal(whole, tiled)
        # 1100/512 -> 3 columns, 900/512 -> 2 rows: six tiles, six calls.
        assert len(counter.calls) == 6
        # The VRAM bound the tiler exists for: no patch wider than tile+2*overlap.
        assert all(h <= 512 + 64 and w <= 512 + 64 for (_, h, w, _) in counter.calls)

    def test_exactly_divisible_edges(self) -> None:
        from kinoforge.upscalers.spandrel._runtime import tile_upscale

        rng = np.random.default_rng(2)
        img = rng.integers(0, 255, (512, 1024, 3), dtype=np.uint8)
        counter = _Counter()
        tiled = tile_upscale(img, scale=2, tile=512, overlap=32, infer=counter)
        assert np.array_equal(tiled, _nn2x(img[None])[0])
        assert len(counter.calls) == 2

    def test_small_image_is_one_call(self) -> None:
        # Bug caught: a 100x70 still is needlessly cut into tiles, or tile=0
        # divides by zero.
        from kinoforge.upscalers.spandrel._runtime import tile_upscale

        img = np.full((70, 100, 3), 9, dtype=np.uint8)
        for tile in (0, 512):
            counter = _Counter()
            out = tile_upscale(img, scale=2, tile=tile, overlap=32, infer=counter)
            assert out.shape == (140, 200, 3)
            assert len(counter.calls) == 1


class TestToRgb:
    def test_alpha_is_dropped(self) -> None:
        # Bug caught: a 4-channel tensor reaches a 3-channel conv on the pod.
        from kinoforge.upscalers.spandrel._runtime import _to_rgb

        rgba = np.random.default_rng(0).integers(0, 255, (5, 6, 4), dtype=np.uint8)
        out = _to_rgb(rgba, Path("x.png"))
        assert out.shape == (5, 6, 3)
        assert np.array_equal(out, rgba[:, :, :3])

    def test_grey_is_broadcast(self) -> None:
        from kinoforge.upscalers.spandrel._runtime import _to_rgb

        out = _to_rgb(np.full((5, 6), 3, dtype=np.uint8), Path("g.png"))
        assert out.shape == (5, 6, 3)
        assert (out == 3).all()

    def test_rgb_passes_through(self) -> None:
        from kinoforge.upscalers.spandrel._runtime import _to_rgb

        rgb = np.zeros((5, 6, 3), dtype=np.uint8)
        assert _to_rgb(rgb, Path("c.png")) is rgb

    def test_two_channels_raise(self) -> None:
        from kinoforge.upscalers.spandrel._runtime import _to_rgb

        with pytest.raises(ValueError, match="shape"):
            _to_rgb(np.zeros((5, 6, 2), dtype=np.uint8), Path("bad.png"))


class TestUpscaleImage:
    def test_rgba_png_becomes_rgb_png_at_2x(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock) -> None:
        # Bug caught: output keeps the input suffix, is written as mp4, or
        # carries alpha.
        rng = np.random.default_rng(0)
        src = _write(tmp_path / "in.png", rng.integers(0, 255, (70, 100, 4), dtype=np.uint8))
        rt, counter = _rt(tmp_path, monkeypatch, tile_size=512)
        out = rt.upscale_image(src, _2X, params={})
        assert out.name == "in.upscaled.png"
        assert _read(out).shape == (140, 200, 3)
        assert counter.calls == [(1, 70, 100, 3)]

    def test_jpeg_in_png_out(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock) -> None:
        src = _write(tmp_path / "in.jpg", np.full((30, 40, 3), 90, dtype=np.uint8))
        rt, _ = _rt(tmp_path, monkeypatch, tile_size=512)
        out = rt.upscale_image(src, _2X, params={})
        assert out.suffix == ".png"
        assert _read(out).shape == (60, 80, 3)

    def test_large_image_is_tiled_through_infer(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock) -> None:
        # Bug caught: upscale_image bypasses tile_upscale and sends the whole
        # still through the model (the OOM this feature exists to avoid).
        rng = np.random.default_rng(3)
        src = _write(tmp_path / "big.png", rng.integers(0, 255, (600, 1100, 3), dtype=np.uint8))
        rt, counter = _rt(tmp_path, monkeypatch, tile_size=512)
        out = rt.upscale_image(src, _2X, params={})
        assert _read(out).shape == (1200, 2200, 3)
        assert len(counter.calls) == 6  # 3 columns x 2 rows


class TestRefusals:
    def test_height_target_refused_before_model_placement(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock) -> None:
        src = _write(tmp_path / "in.png", np.zeros((8, 8, 3), dtype=np.uint8))
        rt, counter = _rt(tmp_path, monkeypatch, tile_size=512)
        placed: list[bool] = []
        monkeypatch.setattr(rt, "_place_model", lambda: placed.append(True) or ("cpu", None))
        with pytest.raises(NotYetImplementedError, match="height"):
            rt.upscale_image(src, ScaleTarget(kind="height", value=1080.0), params={})
        assert placed == [] and counter.calls == []

    def test_scale_mismatch_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock) -> None:
        src = _write(tmp_path / "in.png", np.zeros((8, 8, 3), dtype=np.uint8))
        rt, _ = _rt(tmp_path, monkeypatch, tile_size=512)
        with pytest.raises(UnsupportedScaleError):
            rt.upscale_image(src, ScaleTarget(kind="factor", value=4.0), params={})


class TestVideoPathUnchanged:
    def test_video_is_batched_not_tiled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_spandrel: MagicMock) -> None:
        # Bug caught: the image tiler is wired into the proven video path, or
        # the _infer extraction changed the batching.
        import imageio.v3 as iio

        frames = np.zeros((4, 48, 64, 3), dtype=np.uint8)
        src = tmp_path / "in.mp4"
        iio.imwrite(src, frames, fps=8, codec="libx264", macro_block_size=1)
        rt, counter = _rt(tmp_path, monkeypatch, tile_size=16, batch_size=4)
        out = rt.upscale(src, _2X, params={})
        assert out.name == "in.upscaled.mp4"
        assert counter.calls == [(4, 48, 64, 3)]
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/upscalers/test_spandrel_runtime_image.py -q`
Expected: FAIL — `ImportError: cannot import name 'tile_upscale'`, `AttributeError: ... has no attribute 'upscale_image'`, `has no attribute '_infer'`.

- [ ] **Step 3: Rewrite the runtime** — replace everything in `src/kinoforge/upscalers/spandrel/_runtime.py` below the imports with:

```python
_log = logging.getLogger("kinoforge.upscalers.spandrel.runtime")

_TILE_OVERLAP = 32
"""Pixels of context around each still-image tile; discarded on assembly."""


def tile_upscale(
    img: np.ndarray,
    *,
    scale: int,
    tile: int,
    overlap: int,
    infer: Callable[[np.ndarray], np.ndarray],
) -> np.ndarray:
    """Upscale a uint8 HxWx3 still by *scale*, tile by tile, through *infer*.

    Each tile is padded by *overlap* on every interior edge (clamped at the
    image border, never zero-padded), inferred alone as a 1-image NHWC batch,
    and only the tile's own area — at ``scale`` times its geometry — is copied
    into the output canvas. ``tile <= 0`` or an image no larger than one tile
    goes through in a single call.

    Pure numpy, so it is testable without torch; the runtime passes a bound
    ``_infer`` as *infer*.

    Args:
        img: Input image, ``(H, W, 3)`` uint8.
        scale: Integer factor the model multiplies each dimension by.
        tile: Tile edge in input pixels; ``0`` means whole image.
        overlap: Context pixels around each tile.
        infer: ``(N, h, w, 3) uint8 -> (N, h*scale, w*scale, 3) uint8``.

    Returns:
        The upscaled image, ``(H*scale, W*scale, 3)`` uint8.
    """
    h, w, _ = img.shape
    if tile <= 0 or (h <= tile and w <= tile):
        return infer(img[None])[0]
    out = np.zeros((h * scale, w * scale, 3), dtype=np.uint8)
    for y0 in range(0, h, tile):
        for x0 in range(0, w, tile):
            y1, x1 = min(y0 + tile, h), min(x0 + tile, w)
            py0, px0 = max(y0 - overlap, 0), max(x0 - overlap, 0)
            py1, px1 = min(y1 + overlap, h), min(x1 + overlap, w)
            patch = infer(np.ascontiguousarray(img[py0:py1, px0:px1])[None])[0]
            iy0, ix0 = (y0 - py0) * scale, (x0 - px0) * scale
            out[y0 * scale : y1 * scale, x0 * scale : x1 * scale] = patch[
                iy0 : iy0 + (y1 - y0) * scale, ix0 : ix0 + (x1 - x0) * scale
            ]
    return out


def _to_rgb(img: np.ndarray, source: Path) -> np.ndarray:
    """Return *img* as uint8 HxWx3: broadcast greyscale, drop alpha (logged).

    Raises:
        ValueError: Channel count other than 1 (2-D), 3 or 4.
    """
    if img.ndim == 2:
        return np.repeat(img[:, :, None], 3, axis=2)
    if img.ndim == 3 and img.shape[2] == 4:
        _log.info("dropping alpha channel of %s before upscale", source.name)
        return np.ascontiguousarray(img[:, :, :3])
    if img.ndim == 3 and img.shape[2] == 3:
        return img
    raise ValueError(
        f"{source.name}: expected a greyscale, RGB or RGBA image, got shape {img.shape}"
    )


class SpandrelRuntime:
    """Loads a spandrel model; upscales video per frame or a still image tiled.

    Two public entry points share one inference core (``_infer``):

    * :meth:`upscale` — the live-proven video path. Frames are batched
      through the model whole (NOT tiled) and re-encoded to mp4.
    * :meth:`upscale_image` — a still image, cut by :func:`tile_upscale` into
      ``tile_size`` squares with :data:`_TILE_OVERLAP` px of context and
      written as ``<stem>.upscaled.png``.

    Args:
        weights_path: Local path to the weights file (.pth / .safetensors).
        precision: ``"fp16"`` or ``"fp32"``. fp16 halves VRAM on consumer GPUs
            but some architectures emit subtle artifacts; fp32 is the safe default.
        tile_size: Tile edge in pixels for the STILL-IMAGE path; ``0`` means
            whole-image. The video path ignores it (frames are small).
        batch_size: Frames per CUDA batch on the video path.

    Raises:
        ImportError: ``spandrel`` package not installed.
    """

    def __init__(
        self,
        weights_path: Path,
        precision: Literal["fp16", "fp32"],
        tile_size: int,
        batch_size: int,
    ) -> None:
        """Lazy-import spandrel and load the weights from disk."""
        from spandrel import ModelLoader  # type: ignore[import-not-found]

        self._model = ModelLoader().load_from_file(str(weights_path))
        self._scale: float = float(self._model.scale)
        self._tile = tile_size
        self._batch = batch_size
        self._precision: Literal["fp16", "fp32"] = precision

    # ------------------------------------------------------------------ video

    def upscale(
        self, video_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        """Decode, batch frames through model, re-encode mp4.

        Args:
            video_path: Local mp4 to upscale.
            scale: ``ScaleTarget``. Only ``kind="factor"`` supported in v1;
                ``"height"`` raises ``NotYetImplementedError``. ``scale.value``
                MUST match ``self._scale`` (declared by the weights).
            params: Reserved for engine overrides; ignored in v1.

        Returns:
            Path to the upscaled mp4 (sibling of input, ``<stem>.upscaled.mp4``).

        Raises:
            NotYetImplementedError: ``scale.kind == "height"``.
            UnsupportedScaleError: ``scale.value != self._scale``.
        """
        del params  # reserved for future engine overrides
        self._check_scale(scale)

        import imageio.v3 as iio

        frames_in = iio.imread(video_path, plugin="FFMPEG")
        try:
            metadata = iio.immeta(video_path, plugin="FFMPEG")
            fps = float(metadata.get("fps", 16))
        except Exception:  # noqa: BLE001 — fall back to a sane default
            fps = 16.0

        device, dtype = self._place_model()
        out_frames: list[np.ndarray] = []
        for i in range(0, len(frames_in), self._batch):
            out_frames.extend(
                list(self._infer(frames_in[i : i + self._batch], device, dtype))
            )

        out_path = video_path.with_suffix(".upscaled.mp4")
        iio.imwrite(
            out_path,
            np.stack(out_frames),
            fps=fps,
            codec="libx264",
            macro_block_size=1,
        )
        return out_path

    # ------------------------------------------------------------------ image

    def upscale_image(
        self, image_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        """Upscale one still image; write ``<stem>.upscaled.png`` beside it.

        Args:
            image_path: Local PNG or JPEG.
            scale: Same contract as :meth:`upscale` — factor only, must match
                the weights' declared scale.
            params: Reserved; ignored in v1.

        Returns:
            Path to the upscaled PNG (always RGB, lossless).

        Raises:
            NotYetImplementedError: ``scale.kind == "height"``.
            UnsupportedScaleError: ``scale.value != self._scale``.
            ValueError: The image has a channel count other than 1, 3 or 4.
        """
        del params
        self._check_scale(scale)

        import imageio.v3 as iio

        img = _to_rgb(np.asarray(iio.imread(image_path)), image_path)
        device, dtype = self._place_model()
        out = tile_upscale(
            img,
            scale=int(round(self._scale)),
            tile=self._tile,
            overlap=_TILE_OVERLAP,
            infer=lambda batch: self._infer(batch, device, dtype),
        )
        out_path = image_path.with_suffix(".upscaled.png")
        iio.imwrite(out_path, out)
        return out_path

    # ------------------------------------------------------------------ shared

    def _check_scale(self, scale: ScaleTarget) -> None:
        """Refuse height targets and factors the loaded weights do not provide."""
        if scale.kind == "height":
            raise NotYetImplementedError(
                f"height-target upscale (e.g. {int(scale.value)}p) deferred; "
                "use --scale Nx for v1"
            )
        if scale.value != self._scale:
            raise UnsupportedScaleError(scale=scale, engine_name="spandrel")

    def _place_model(self) -> tuple[Any, Any]:
        """Move the raw nn.Module to (device, dtype) atomically; return both.

        Spandrel's ``ModelLoader().load_from_file`` returns an
        ``ImageModelDescriptor`` whose ``.to()`` is ambiguous — a dtype-only
        cast can silently drop the device back to CPU on some versions,
        producing "Expected all tensors on same device, but found cpu and
        cuda:0" at the first conv2d. Unwrap to the raw nn.Module and issue a
        single combined ``.to(device=..., dtype=...)`` so both settings land
        atomically without descriptor round-tripping.
        """
        import torch

        raw_model = getattr(self._model, "model", self._model)
        device = (
            next(raw_model.parameters()).device
            if hasattr(raw_model, "parameters")
            else "cpu"
        )
        # Prefer cuda if available and model was ever moved off cpu; otherwise
        # honor the parameter-inferred device. This defends against a
        # descriptor.to("cuda") that didn't actually move the wrapped module.
        if torch.cuda.is_available() and str(device) == "cpu":
            device = torch.device("cuda")
        dtype = torch.float16 if self._precision == "fp16" else torch.float32
        raw_model.to(device=device, dtype=dtype)
        return device, dtype

    def _infer(self, batch_np: np.ndarray, device: Any, dtype: Any) -> np.ndarray:
        """Run one uint8 NHWC batch through the model; return uint8 NHWC."""
        import torch

        batch_t = (
            torch.from_numpy(np.ascontiguousarray(batch_np))
            .permute(0, 3, 1, 2)
            .to(device=device, dtype=dtype)
            / 255.0
        )
        with torch.no_grad():
            out_t = self._model(batch_t)
        return (
            (out_t.clamp(0.0, 1.0) * 255.0)
            .to(torch.uint8)
            .permute(0, 2, 3, 1)
            .cpu()
            .numpy()
        )

    def to(self, device: str) -> None:
        """LRU eviction hook — move underlying nn.Modules between cuda/cpu."""
        self._model.to(device)
```

Replace the import block at the top of the module with:

```python
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import numpy as np

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget
```

and change the module docstring's first line to `"""SpandrelRuntime — video (per-frame) and still-image upscale around the spandrel library.` (keep the rest). No new `kinoforge.*` import is introduced — the embed set is unchanged.

In `src/kinoforge/core/config.py` change the `SpandrelEngineConfig` docstring lines for `tile_size` / `batch_size` to:

```python
        tile_size: Tile edge in pixels for STILL-IMAGE upscale
            (``kinoforge upscale --image``); ``0`` = whole image. The video
            path is untiled and ignores it.
        batch_size: Frames per CUDA batch on the video path.
```

- [ ] **Step 4: Run to verify they pass**

Run: `pixi run python -m pytest tests/upscalers/test_spandrel_runtime_image.py tests/upscalers/test_spandrel_runtime.py -q -rs`
Expected: new file all PASS with zero skips; old file `5 passed, 3 skipped` (unchanged — that is the "video path untouched" guard, together with `TestVideoPathUnchanged`).

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/upscalers/spandrel/_runtime.py src/kinoforge/core/config.py \
  tests/upscalers/test_spandrel_runtime_image.py
pixi run pre-commit run --all-files
git commit -m "feat(spandrel): upscale_image — pure-numpy tiler, PNG out; video path untouched"
```

---

### Task 7: Orchestrator publish picks the extension from the media

**Goal:** An upscaled image publishes as `.png`, a video as `.mp4`, and an image carrying `downscale_to` raises instead of going through the mp4 downscaler.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py:3030-3100` (the materialize block)
- Test: `tests/core/test_orchestrator_publish_media.py` (new)

**Acceptance Criteria:**
- [ ] Image artifact (`meta={"media": "image", "materialize": True}`) → one `sink.publish` with `extension == ".png"`, `kind == "upscaled"`.
- [ ] Video artifact (`meta={"materialize": True}`) → `extension == ".mp4"`.
- [ ] Image + `downscale_to` → `RuntimeError` mentioning "image" and `sink.publish` never called.
- [ ] `tests/core/test_orchestrator_fullres.py`, `test_orchestrator_skip_clip_stage.py`, `test_orchestrator_upscale_chunk.py` stay green.

**Verify:** `pixi run python -m pytest tests/core/test_orchestrator_publish_media.py tests/core/test_orchestrator_fullres.py tests/core/test_orchestrator_skip_clip_stage.py tests/core/test_orchestrator_upscale_chunk.py -q` → all pass.

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_orchestrator_publish_media.py
"""The materialize boundary publishes an upscaled still as .png, a clip as .mp4."""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import kinoforge._adapters  # noqa: F401 — self-register every engine + upscaler
from kinoforge.core import orchestrator
from kinoforge.core.config import Config
from kinoforge.core.interfaces import Artifact, PipelineState
from kinoforge.core.orchestrator import DeploySession, generate


def _cfg() -> Config:
    return Config.model_validate(
        {
            "engine": {"kind": "diffusers", "precision": "fp8"},
            "models": [{"kind": "base", "ref": "hf:Wan-AI/Wan2.2-T2V", "target": "diffusion_models"}],
            "compute": {"provider": "fake", "image": "fake:latest"},
            "upscale": {
                "engine": "spandrel", "scale": "2x",
                "spandrel": {"model_url": "hf:foo/bar.pth", "arch": "realesrgan",
                             "precision": "fp16", "tile_size": 512, "batch_size": 4},
            },
        }
    )


@pytest.fixture
def _fake_session(monkeypatch: pytest.MonkeyPatch) -> DeploySession:
    fake_engine = MagicMock(name="GenerationEngine")
    fake_engine.name = "diffusers"
    fake_engine.model_identity = MagicMock(return_value="fake-model")
    fake_engine.accepted_kinds = {"image"}
    session = DeploySession(backend=MagicMock(), profile=MagicMock(), pool=MagicMock(),
                            instance=None, engine=fake_engine, provider=None)

    @contextmanager
    def fake_deploy(*args: Any, **kwargs: Any) -> Any:
        yield session

    monkeypatch.setattr(orchestrator, "deploy_session", fake_deploy)
    return session


def _stub_stage(monkeypatch: pytest.MonkeyPatch, upscaled: Artifact) -> None:
    class _Stub:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, state: PipelineState) -> PipelineState:
            return dataclasses.replace(state, artifacts={**state.artifacts, "upscaled": upscaled})

    import kinoforge.pipeline.upscale as upscale_mod

    monkeypatch.setattr(upscale_mod, "UpscaleStage", _Stub)


def _sink(tmp_path: Path) -> MagicMock:
    sink = MagicMock(name="sink")
    sink.publish = MagicMock(side_effect=lambda data, **kw: tmp_path / f"published{kw['extension']}")
    return sink


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, meta: dict[str, Any]) -> MagicMock:
    body = tmp_path / "up.bin"
    body.write_bytes(b"bytes")
    _stub_stage(monkeypatch, Artifact(uri=f"file://{body}", sha256="s", size=5, meta=meta))
    sink = _sink(tmp_path)
    generate(_cfg(), request=None, store=MagicMock(), sink=sink, run_id="r",
             skip_clip_stage=True, initial_clip=Artifact(uri="file:///tmp/in", sha256="i", size=1))
    return sink


def test_image_publishes_as_png(_fake_session: DeploySession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Bug caught: the four hardcoded extension=".mp4" calls — a PNG lands on
    # disk named .mp4 and every downstream `find '*.mp4'` picks it up.
    sink = _run(monkeypatch, tmp_path, {"media": "image", "materialize": True})
    assert sink.publish.call_count == 1
    assert sink.publish.call_args.kwargs["extension"] == ".png"
    assert sink.publish.call_args.kwargs["kind"] == "upscaled"


def test_video_publishes_as_mp4(_fake_session: DeploySession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sink = _run(monkeypatch, tmp_path, {"materialize": True})
    assert sink.publish.call_args.kwargs["extension"] == ".mp4"


def test_image_with_downscale_to_raises_before_publish(_fake_session: DeploySession, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Bug caught: PNG bytes fed through finalize_upscaled_bytes, an ffmpeg
    # mp4 pipeline, producing garbage or an opaque ffmpeg error.
    body = tmp_path / "up.bin"
    body.write_bytes(b"bytes")
    _stub_stage(monkeypatch, Artifact(uri=f"file://{body}", sha256="s", size=5,
                                      meta={"media": "image", "downscale_to": 1080, "materialize": True}))
    sink = _sink(tmp_path)
    with pytest.raises(RuntimeError, match="image"):
        generate(_cfg(), request=None, store=MagicMock(), sink=sink, run_id="r",
                 skip_clip_stage=True, initial_clip=Artifact(uri="file:///tmp/in", sha256="i", size=1))
    sink.publish.assert_not_called()
```

- [ ] **Step 2: Run to verify they fail**

Run: `pixi run python -m pytest tests/core/test_orchestrator_publish_media.py -q`
Expected: `test_image_publishes_as_png` fails (`'.mp4' == '.png'`), the downscale test fails (no raise).

- [ ] **Step 3: Implement** — in `src/kinoforge/core/orchestrator.py`, inside `if _needs_materialize and upscaled is not None and sink is not None:` right after the `from kinoforge.pipeline.materialize import finalize_upscaled_bytes` line, add:

```python
            from kinoforge.core.media import extension_for, media_of

            media = media_of(upscaled)
            if media == "image" and _downscale_to is not None:
                # Unreachable by construction — spandrel refuses height
                # targets and the CLI refuses them for --image — but
                # finalize_upscaled_bytes is an ffmpeg mp4 pipeline, so never
                # let PNG bytes near it.
                raise RuntimeError(
                    "upscaled image artifact carries downscale_to="
                    f"{_downscale_to}; height-target downscale is video-only"
                )
            publish_ext = extension_for(media)
```

Then change the two publishes in that block (`kind="fullres"` and `kind="upscaled"`) from `extension=".mp4",` to `extension=publish_ext,`. The `interpolated` publish further down keeps `extension=".mp4"`.

- [ ] **Step 4: Run to verify they pass**

Run: `pixi run python -m pytest tests/core/test_orchestrator_publish_media.py tests/core/test_orchestrator_fullres.py tests/core/test_orchestrator_skip_clip_stage.py tests/core/test_orchestrator_upscale_chunk.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_publish_media.py
pixi run pre-commit run --all-files
git commit -m "feat(orchestrator): publish an upscaled still as .png, from the artifact's media"
```

---

### Task 8: Guards, full suite, docs

**Goal:** The pod embed-closure and env-payload guards are green, the whole offline suite is green, and the operator docs describe `--image`.

**Files:**
- Modify: `docs/engines.md` (§Upscalers → `spandrel` subsection), `docs/configuration.md` (`upscale:` section), `README.md:116`

**Acceptance Criteria:**
- [ ] `tests/providers/test_pod_embed_closure.py` and `tests/providers/test_env_payload_ceiling.py` pass.
- [ ] `pixi run test` → 0 failed.
- [ ] `docs/engines.md` has an "Image input" paragraph under `spandrel` and the corrected `tile_size` story; `docs/configuration.md` names `--image`, the accepted suffixes and the three refusals; README's `upscale` row says "video clip or still image".

**Verify:** `pixi run python -m pytest tests/providers/test_pod_embed_closure.py tests/providers/test_env_payload_ceiling.py -q && pixi run test -q 2>&1 | tail -3` → guards pass, `0 failed`.

**Steps:**

- [ ] **Step 1: Run the two pod guards first** (spec §10 — verify, do not assume)

Run: `pixi run python -m pytest tests/providers/test_pod_embed_closure.py tests/providers/test_env_payload_ceiling.py -q`
Expected: PASS. If the payload guard fails, the server module grew past the ceiling: shorten the new docstrings in `wan_t2v_server.py` (Task 5) rather than touching embed sets.

- [ ] **Step 2: Run the full offline suite**

Run: `pixi run test -q 2>&1 | tail -5`
Expected: `0 failed`. Fix any sweep that assumed `--video` is the only source (e.g. a help-text snapshot) in place.

- [ ] **Step 3: Docs** — `docs/engines.md`, replace the `tile_size` / `batch_size` bullet under `### spandrel (per-frame fallback)` and add the paragraph:

```markdown
- `precision` — `"fp16"` (default) or `"fp32"`.
- `tile_size` — tile edge in pixels for **still-image** upscale; `0` = whole
  image. The video path is untiled (frames are small) and ignores it.
- `batch_size` — frames per CUDA batch on the video path.

**Image input.** `spandrel` is the one upscaler that also takes a still:
`kinoforge upscale --image photo.png -c examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml --no-reuse`
uploads the PNG/JPEG to the same pod, runs the same weights through
`SpandrelRuntime.upscale_image` (tiled by `tile_size` with a 32 px overlap)
and publishes `{ts}_upscaled_spandrel_{model}_upscale.png` — always PNG,
lossless. Factor scales only; `chunk_frames`, `tile_grid` and height targets
are refused at preflight for `--image`. FlashVSR and SeedVR2 refuse `--image`
(`UpscalerEngine.supports_image_input` is false), because a temporal model has
no meaning for one frame.
```

`docs/configuration.md`, after the first paragraph of `## upscale: (optional, video upscaling)` (retitle it `## upscale: (optional, video or still-image upscaling)`), add:

```markdown
`kinoforge upscale` takes exactly one of `--video PATH_OR_URL` or
`--image PATH` (local `.png` / `.jpg` / `.jpeg` only). With `--image` the
engine must declare image support (today: `spandrel`), `scale` must be a
factor (`2x`, `4x`), and `chunk_frames` / `tile_grid` must be unset — each
violation exits 2 before any pod is booted. Output is always PNG.
```

and change the spandrel `tile_size` row to `| `spandrel.tile_size` | int | `512` — still-image tile edge; `0` = whole image; video path ignores it |`.

`README.md:116`:

```markdown
| `upscale` | Upscale a video clip (FlashVSR default; spandrel 2x) or a still image (`--image`, spandrel) | `pixi run kinoforge upscale --config cfg.yaml --video clip.mp4 --no-reuse` |
```

- [ ] **Step 4: Commit**

```bash
git add docs/engines.md docs/configuration.md README.md
pixi run pre-commit run --all-files
git commit -m "docs(upscale): document kinoforge upscale --image and the tile_size story"
```

---

### Task 9: RED scaffold, live proof, generation log, PROGRESS

**Goal:** One live `kinoforge upscale --image` run on RunPod with the spandrel x2 config, frame-QA'd, logged as `successful-generations.md` §36, with PROGRESS.md pointing at this work.

**Files:**
- Create: `tests/live/test_spandrel_image_upscale_smoke.py`
- Create: `tests/live/evidence/2026-10-03-spandrel-image-upscale/` (stdout, stderr, output PNG copy)
- Modify: `successful-generations.md` (TOC + new §36), `PROGRESS.md` (Pointers block)

**Acceptance Criteria:**
- [ ] The smoke file is committed BEFORE the run (`git log -1 -- tests/live/test_spandrel_image_upscale_smoke.py` shows a commit).
- [ ] `pixi run preflight` exits 0 before the run.
- [ ] The run exits 0; the published file is `output/*_upscaled_spandrel_spandrel-realesrgan-fp16_upscale.png` with dims exactly 2× the input (5344×3008 from the 2672×1504 §35 PNG).
- [ ] GPU utilisation was probed non-zero at least once during the run (recorded in the log entry), never inferred from `est_spend`.
- [ ] Visual QA: input and output read side by side; verdict recorded with any ⚠️ flags.
- [ ] `kinoforge list` afterwards prints BOTH `No running instances.` and `No instances recorded in ledger.`
- [ ] §36 added with TOC entry; PROGRESS.md Pointers block updated; both committed.

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run python -m pytest tests/live/test_spandrel_image_upscale_smoke.py -m live -q` → PASS; then `pixi run kinoforge list` → both lines.

**Steps:**

- [ ] **Step 1: Write the RED scaffold and commit it before any spend**

```python
# tests/live/test_spandrel_image_upscale_smoke.py
"""Live smoke — spandrel RealESRGAN-x2 upscale of a still image (`upscale --image`).

RED scaffold committed BEFORE the live spend per CLAUDE.md. Input is the §35
`kinoforge image` PNG, so this run is also the first image -> upscale chain.
Evidence lands under ``tests/live/evidence/2026-10-03-spandrel-image-upscale/``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="live smoke: set KINOFORGE_LIVE_TESTS=1 (spends RunPod money)",
)

_ROOT = Path(__file__).parent.parent.parent
_INPUT = _ROOT / "output" / "20261003-174427_image_luma_agents_uni-1_Photorealistic-cinem.png"
_CFG = _ROOT / "examples" / "configs" / "runpod-diffusers-spandrel-x2-upscale.yaml"
_EVIDENCE_DIR = Path(__file__).parent / "evidence" / "2026-10-03-spandrel-image-upscale"


def _dims(path: Path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as im:
        return im.size


@pytest.mark.live
def test_spandrel_upscales_a_still_image_2x() -> None:
    assert _INPUT.exists(), f"input PNG missing: {_INPUT}"
    assert _CFG.exists(), f"cfg missing: {_CFG}"
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "upscale", "--image", str(_INPUT), "--config", str(_CFG), "--no-reuse"],
        capture_output=True, text=True, timeout=2400, check=False,
    )
    (_EVIDENCE_DIR / "stdout.txt").write_text(proc.stdout)
    (_EVIDENCE_DIR / "stderr.txt").write_text(proc.stderr)
    assert proc.returncode == 0, proc.stderr

    import re

    m = re.search(r"upscaled: uri='([^']+)'", proc.stdout)
    assert m, f"no upscaled uri line in stdout:\n{proc.stdout}"
    out_path = Path(m.group(1).removeprefix("file://"))
    assert out_path.exists(), out_path
    assert out_path.suffix == ".png", out_path
    assert "_upscaled_spandrel_" in out_path.name, out_path

    evidence = _EVIDENCE_DIR / out_path.name
    shutil.copy2(out_path, evidence)

    in_w, in_h = _dims(_INPUT)
    out_w, out_h = _dims(evidence)
    assert (out_w, out_h) == (in_w * 2, in_h * 2), f"expected {in_w * 2}x{in_h * 2}, got {out_w}x{out_h}"
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() != hashlib.sha256(_INPUT.read_bytes()).hexdigest()

    ledger = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "list"], capture_output=True, text=True, timeout=60, check=False
    )
    assert "No running instances." in ledger.stdout, ledger.stdout
    assert "No instances recorded in ledger." in ledger.stdout, ledger.stdout
```

```bash
git add tests/live/test_spandrel_image_upscale_smoke.py
pixi run pre-commit run --all-files
git commit -m "test(live): RED scaffold for the spandrel still-image upscale smoke"
```

- [ ] **Step 2: Preflight** — Run: `pixi run preflight`. Expected: exit 0 (creds present, zero RunPod pods, clean tree). Do not continue on non-zero.

- [ ] **Step 3: Fire the run in the background and poll utilisation** — from the controlling session (not a subagent: a subagent cannot wait on background work):

```bash
KINOFORGE_LIVE_TESTS=1 pixi run python -m pytest \
  tests/live/test_spandrel_image_upscale_smoke.py -m live -q
```

Every 60–90 s while it runs: `pixi run kinoforge list` for the pod id, then the probe from CLAUDE.md (`RunPodGraphQLUtilEndpoint(...).probe("<pod-id>")`), recording `gpu_util_percent` / `cpu_percent` / `memory_percent`. GPU 0 % for ≥ 3 probes after boot → pull `https://<pod-id>-8001.proxy.runpod.net/bootstrap.log`, destroy, investigate. Expect: boot 3–10 min (pip + 64 MB weights), then a short GPU burst (a 4 MP still through RealESRGAN x2, tiled into 512 px squares — ~24 tiles).

- [ ] **Step 4: Visual QA** — read `_INPUT` and the evidence PNG side by side (the output is 5344×3008; make a half-size copy for reading if needed with `pixi run python -c "from PIL import Image; im=Image.open('<out>'); im.resize((im.width//2, im.height//2)).save('/tmp/claude-1000/-workspace/*/scratchpad/qa.png')"`). Judge: detail gain on the meadow/waterfall textures, no tile seams (inspect the 512-px grid lines at 1024, 1536, 2048 … in source coordinates), no colour shift versus the input, RealESRGAN face plasticity noted if present. Record PASS / PASS-with-flags / FAIL.

- [ ] **Step 5: Teardown check** — Run: `pixi run kinoforge list`. Expected: both `No running instances.` and `No instances recorded in ledger.` A `⚠ launching — pod not confirmed` row is acceptable per CLAUDE.md; anything else → `pixi run kinoforge destroy --id <pod-id>`.

- [ ] **Step 6: Log §36** — append to `successful-generations.md` following §35's shape (Field table: stack triple `runpod / SpandrelEngine / RealESRGAN_x2`, mode `image-upscale`, first-success SHA, local date; "The new capability axis" — a kinoforge command that upscales a still, and the first `image` → `upscale` chain; exact command; cfg verbatim pointer; pod id, GPU type, boot time, GPU-util readings, wall clock, spend; dims in → out; the QA verdict; evidence dir). Add the TOC line `36. \`<ts>\` — [kinoforge upscale --image — spandrel RealESRGAN-x2 on a Luma UNI-1 still — image-upscale](#36-...)`.

- [ ] **Step 7: PROGRESS.md** — in the `## Pointers` block add, above the `kinoforge image` entry:

```markdown
- **SHIPPED — standalone image upscaling (`kinoforge upscale --image`):** design
  `docs/superpowers/specs/2026-10-03-standalone-image-upscaling-design.md`, plan
  `docs/superpowers/plans/2026-10-03-standalone-image-upscaling.md` (+ `.tasks.json`),
  all 9 tasks complete, live-proven <date> — `successful-generations.md` §36 (pod
  `<id>`, <in>→<out>, ~$<spend>, frame-QA <verdict>). Closes the §13.1 deferral of
  the image design. Media kind travels as data (`core/media.py`); `tile_size` is
  honoured for the first time, on the still-image path only.
```

and change the `kinoforge image` entry's "Deferred, NOT done" line to drop still-image upscaling.

- [ ] **Step 8: Commit**

```bash
git add tests/live/evidence/2026-10-03-spandrel-image-upscale successful-generations.md PROGRESS.md
pixi run pre-commit run --all-files
git commit -m "docs(log): kinoforge upscale --image live-proven on RunPod spandrel x2 (§36)"
```
