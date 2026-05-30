# `extract_last_frame` for real engines — design spec

**Date:** 2026-05-30
**Author:** brainstorm session (Dr. Twinklebrane + Claude)
**Issue:** none yet (per-engine follow-up; recommend opening one before plan phase)
**Status:** validated, awaiting user spec review before plan phase

---

## 1. Motivation

Layer B (continuity fallback, commits `b9cb44b` + `270accd`, closed GH #1) added
the seam for non-native multi-segment video runs: `GenerateClipStage` interleaves
`render → extract_last_frame → inject_tail_frame → render` between segments when
the engine's profile reports `supports_native_extension=False`.

The `extract_last_frame` ABC was added with a raising default; only `FakeEngine`
overrode it. The three real engines (`ComfyUIEngine`, `DiffusersEngine`,
`HostedAPIEngine`) inherit the raising default. The first real-cloud user
configuring a non-native engine with `max_segment_seconds < total_duration`
trips `NotImplementedError` at the chain step.

This layer ships per-engine `extract_last_frame` so the chain step survives.
Downstream wiring of the resulting `init_image` asset into each engine's
`submit()` body remains a separate (larger) hole — Layer F, deferred.

---

## 2. Scope

### In scope

- New shared helper `core/frames.py::ffmpeg_last_frame(bytes) -> bytes` —
  ffmpeg subprocess, injectable for tests.
- New error `FrameExtractionError(KinoforgeError)` in `core/errors.py`.
- ABC contract change: `GenerationEngine.extract_last_frame(artifact) -> bytes`
  (was `-> ConditioningAsset`). Stage takes over persistence.
- `core/continuity.py::inject_tail_frame` simplified to a pure asset-injection
  helper; engine + extract logic moves to the stage.
- `GenerateClipStage` non-native branch rewired: engine → bytes → store →
  `ConditioningAsset` → `inject_tail_frame`.
- Per-engine `result()` populates `Artifact.url` so `extract_last_frame` can
  fetch video bytes uniformly.
- Per-engine `extract_last_frame` (5 identical lines copied into each — no
  mixin) with two new injected seams: `http_get_bytes`, `ffmpeg_run`.
- Hosted-specific config key `cfg["engine"]["hosted"]["url_path"]` plus a
  dot-path walker (`"video.url"` → nested lookup) so Hosted can locate the
  artifact URL in arbitrary provider response shapes.
- Test additions and updates across `tests/core/`, `tests/engines/`,
  `tests/pipeline/` (~30 new/updated tests total).

### Out of scope (explicitly deferred)

- **Layer F: engine asset-wiring.** Each engine's `submit()` body still
  reads only `job.spec` — it ignores `job.segments[0].assets`. Until this
  follow-up lands, multi-segment non-native runs will persist tail PNGs to the
  artifact store, but the next render will not actually consume them as
  conditioning. Filing as a new "Known limitations" entry in PROGRESS.
- ComfyUI `/upload/image` flow for cross-host tail upload (only matters once
  non-native runs hit a remote ComfyUI; today ComfyUI runs on the same disk as
  the orchestrator).
- Hosted `url_path` with array indexing (`results[0].url`). Dot-walker
  handles dicts only; arrays are out of scope. If needed later, swap the
  walker for a JSONPath dep.
- Cleanup of intermediate tail PNGs before clip is delivered (they live in
  the same `run_id` namespace as the final clip and are reaped by the existing
  `kinoforge gc --run <id>` flow).

---

## 3. Locked design decisions

| Topic | Decision | Rejected alternative |
|---|---|---|
| **Scope** | Extraction only; engine submit() spec-wiring deferred as Layer F | Full continuity now (6–8 tasks, brittle Hosted spec wiring per provider) |
| **Decoder** | ffmpeg subprocess via injected `run` seam | imageio-ffmpeg (~30 MB pip dep); PyAV (heavy native build) |
| **Persistence** | Stage persists bytes via `ArtifactStore.put_bytes(run_id, name, bytes)` | Engine persists (ABC grows store + run_id kwargs; couples engine to store API); engine ctor takes store (per-run mutable run_id awkward on registry singletons) |
| **Fetch** | Backfill `Artifact.url` in each engine's `result()`; uniform `extract_last_frame` body | Per-engine URL construction inside `extract_last_frame` |
| **Hosted URL location** | Config-driven dot-path `cfg["engine"]["hosted"]["url_path"]` | Heuristic "first http-shaped string" (fragile); defer Hosted entirely (leaves the most-used engine raising) |
| **Shared body placement** | Copy 5 lines into each engine (no mixin) | `BaseEngine` mixin (~30 lines of indirection for 15 lines saved; project pattern is self-contained engines) |

---

## 4. Architecture

### 4.1 New module: `src/kinoforge/core/frames.py`

```python
from __future__ import annotations
import subprocess
from collections.abc import Callable

from kinoforge.core.errors import FrameExtractionError

_FFMPEG_ARGV: list[str] = [
    "ffmpeg",
    "-sseof", "-1",        # seek to 1 second before end-of-file
    "-i", "pipe:0",        # read input from stdin
    "-frames:v", "1",      # one video frame out
    "-f", "image2pipe",
    "-vcodec", "png",
    "pipe:1",              # write PNG to stdout
]


def _default_run(argv: list[str], stdin: bytes) -> bytes:
    """Run ffmpeg with stdin pipe; return stdout bytes; raise on non-zero."""
    proc = subprocess.run(  # noqa: S603
        argv, input=stdin, capture_output=True, check=False
    )
    if proc.returncode != 0:
        raise FrameExtractionError(
            f"ffmpeg exit {proc.returncode}: {proc.stderr.decode(errors='replace')[:512]}"
        )
    return proc.stdout


def ffmpeg_last_frame(
    video_bytes: bytes,
    *,
    run: Callable[[list[str], bytes], bytes] = _default_run,
) -> bytes:
    """Decode the last frame of *video_bytes* as a PNG byte string.

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

### 4.2 New error in `src/kinoforge/core/errors.py`

```python
class FrameExtractionError(KinoforgeError):
    """Raised when a frame cannot be decoded from an Artifact's video bytes."""
```

### 4.3 ABC contract change in `src/kinoforge/core/interfaces.py`

```python
def extract_last_frame(self, artifact: Artifact) -> bytes:
    """Decode the last frame of a rendered clip as PNG bytes.

    Default raises; subclass to enable continuity for this engine.

    Args:
        artifact: A clip Artifact returned by backend.result() with .url
            populated to a fetchable location.

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

### 4.4 `inject_tail_frame` simplified in `src/kinoforge/core/continuity.py`

```python
def inject_tail_frame(
    next_job: GenerationJob,
    tail_asset: ConditioningAsset,
) -> GenerationJob:
    """Return a copy of next_job with seg-0 assets replaced by [tail_asset].

    Pure dataclass-replace helper. Engine + extract logic is the stage's job.
    """
    new_seg_0 = replace(next_job.segments[0], assets=[tail_asset])
    return replace(next_job, segments=[new_seg_0, *next_job.segments[1:]])
```

### 4.5 Stage rewiring — `GenerateClipStage` non-native branch

The existing call at `pipeline/generate_clip.py:105`:
```python
job = inject_tail_frame(job, results[-1], self.engine)
```
becomes:
```python
tail_bytes = self.engine.extract_last_frame(results[-1])
tail_name = f"seg-{i-1}-tail.png"
tail_uri = self.store.put_bytes(self.run_id, tail_name, tail_bytes)
tail_asset = ConditioningAsset(
    kind="image",
    role="init_image",
    ref=Artifact(filename=tail_name, uri=tail_uri),
)
job = inject_tail_frame(job, tail_asset)
```

`self.store` and `self.run_id` already exist on the stage (used for final
clip persistence). The tail PNG persists into the same `run_id` namespace.

### 4.6 Per-engine `result()` URL backfill

**ComfyUI** (`engines/comfyui/__init__.py`, near line 251):
```python
view_url = f"{self._base_url}/view?filename={filename}&type=output"
return Artifact(filename=filename, url=view_url, meta={"prompt_id": job_id})
```
ComfyUI server already serves `/view?filename=<name>&type=output`. No
server-side change. URL works for same-host extraction (today's only
ComfyUI deployment shape).

**Diffusers** (`engines/diffusers/__init__.py`, near line 198):
```python
url = str(data.get("url", ""))
return Artifact(filename=filename, url=url, meta={"job_id": job_id})
```
New server contract documented in README + example cfg: the
`/status/{job_id}` response must include a `"url"` field pointing at a
HTTP-fetchable location of the rendered video (e.g.
`"http://127.0.0.1:8000/file/<filename>"`). Servers that omit it cause
`extract_last_frame` to raise `FrameExtractionError` with a clear message.

**Hosted** (`engines/hosted/__init__.py`, near line 225):

Backend constructor gains `url_path: str = ""`. Engine threads it through
`backend()` from `cfg["engine"]["hosted"]["url_path"]`. `result()` body:
```python
url = _walk_dot_path(data, self._url_path) if self._url_path else ""
return Artifact(filename=filename, url=url, meta={"job_id": job_id})
```
where:
```python
def _walk_dot_path(data: dict[str, Any], path: str) -> str:
    """Walk dot-separated keys; return empty string if any step missing.

    Examples:
        _walk_dot_path({"video": {"url": "X"}}, "video.url") -> "X"
        _walk_dot_path({}, "video.url") -> ""
    """
    node: Any = data
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return ""
        node = node[key]
    return str(node) if isinstance(node, str) else ""
```
Cfg example for fal.ai:
```yaml
engine:
  hosted:
    provider: fal
    endpoint: https://fal.run/fal-ai/ltx-video
    model: ltx-2
    api_key_env: FAL_KEY
    health_url: https://fal.run/health
    url_path: video.url        # NEW
```

### 4.7 Per-engine `extract_last_frame` — copied 5 lines per engine

Each of the three engines gains two new constructor seams:
```python
http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
ffmpeg_run: Callable[[list[str], bytes], bytes] = frames._default_run,
```
where `_urllib_get_bytes(url) -> bytes` is a thin
`urllib.request.urlopen(url).read()` helper defined in each engine module
(each gets its own copy; no cross-engine shared utility module — engines
do not import each other) and `frames._default_run` is imported from
`kinoforge.core.frames` (engine → core import is allowed; only the reverse
is banned).

The method body is identical in all three engines:
```python
def extract_last_frame(self, artifact: Artifact) -> bytes:
    if not artifact.url:
        raise FrameExtractionError(
            f"{type(self).__name__}: artifact.url is empty; cannot fetch video bytes"
        )
    video_bytes = self._http_get_bytes(artifact.url)
    return ffmpeg_last_frame(video_bytes, run=self._ffmpeg_run)
```

### 4.8 `FakeEngine.extract_last_frame` — return type change

```python
def extract_last_frame(self, artifact: Artifact) -> bytes:
    """Deterministic byte string for tests; not a real PNG."""
    return f"FAKE_TAIL:{artifact.filename}".encode()
```

---

## 5. Test surface (red-first, per `test-design` skill)

### 5.1 New tests

**`tests/core/test_frames.py`** (~4 tests):
1. `ffmpeg_last_frame` calls injected `run` with exact `_FFMPEG_ARGV` argv.
2. Returns bytes from injected `run` unchanged (no post-processing).
3. Subprocess non-zero exit → `FrameExtractionError` with stderr substring.
4. Empty input bytes propagate to `run` (no special-casing).

### 5.2 Updated tests

**`tests/core/test_continuity.py`** (5 → ~3 tests):
- Keep: replace-seg-0, preserve-others, no-mutation.
- Drop: engine-extract-called, raises-propagation (no longer the helper's
  concern; the equivalent moves to stage tests).
- Signatures lose the engine arg; tests build a `ConditioningAsset`
  directly.

**`tests/core/test_interfaces.py`** (1 test touched):
- `test_extract_last_frame_default_raises_with_engine_name` — assertion
  content unchanged; the test's local subclass return-type annotation
  becomes `bytes`.

**`tests/engines/test_fake.py`** (1 test renamed/rewritten):
- `..._returns_init_image_asset` → `..._returns_deterministic_bytes`;
  asserts byte content and that bytes derive from `artifact.filename`.

**`tests/engines/test_comfyui.py`** (~4 new tests):
1. `result()` populates `url=f"{base_url}/view?filename=<fn>&type=output"`.
2. `extract_last_frame` calls `http_get_bytes(artifact.url)` exactly once.
3. `extract_last_frame` passes returned video bytes through to `ffmpeg_run`
   via `ffmpeg_last_frame`; returns the PNG bytes from `ffmpeg_run`.
4. `extract_last_frame` raises `FrameExtractionError` when
   `artifact.url == ""`.

**`tests/engines/test_diffusers.py`** (~4 new tests, same shape as ComfyUI
but URL is read from response body's `"url"` field, not constructed).

**`tests/engines/test_hosted.py`** (~6 new tests):
- 4 mirror ComfyUI/Diffusers shape.
- `_walk_dot_path` resolves `"video.url"` against `{"video": {"url": "X"}}`
  → `"X"`.
- `_walk_dot_path` returns `""` for missing intermediate key.

**`tests/pipeline/test_generate_clip.py`** (~3 new tests, 1 touched):
1. Non-native N=3 chain: stage calls `store.put_bytes(run_id,
   "seg-0-tail.png", FAKE_BYTES)` once between segs 0 and 1.
2. Tail asset injected into seg 1 has `ref.uri == store.put_bytes return`.
3. `run_id` flows from stage construction into the put_bytes call.
4. Existing `_FakeExtractor` (lines 296–302) deleted. Its purpose was to
   spy that `extract_last_frame` is NOT called on the native (N=1) path;
   the equivalent now lives on the bytes-returning `FakeEngine` and is
   covered by the run_id/put_bytes assertions above.

---

## 6. Task slicing

| # | Task | Files | Tests |
|---|---|---|---|
| 1 | `FrameExtractionError` + `core/frames.py` shared ffmpeg helper | `core/errors.py`, `core/frames.py` (new), `tests/core/test_frames.py` (new) | 4 |
| 2 | ABC contract change + helper simplification + `FakeEngine` bytes return | `core/interfaces.py`, `core/continuity.py`, `engines/fake/__init__.py`, `tests/core/test_continuity.py`, `tests/core/test_interfaces.py`, `tests/engines/test_fake.py` | ~5 updated |
| 3 | `GenerateClipStage` non-native rewiring | `pipeline/generate_clip.py`, `tests/pipeline/test_generate_clip.py` | ~3 new + 1 touched |
| 4 | ComfyUI `result()` URL backfill + `extract_last_frame` + 2 seams | `engines/comfyui/__init__.py`, `tests/engines/test_comfyui.py` | ~4 new |
| 5 | Diffusers `result()` URL passthrough + `extract_last_frame` + 2 seams + server contract doc | `engines/diffusers/__init__.py`, `tests/engines/test_diffusers.py`, README | ~4 new |
| 6 | Hosted `url_path` cfg + dot-walker + `result()` backfill + `extract_last_frame` + 2 seams | `engines/hosted/__init__.py`, `tests/engines/test_hosted.py`, README, `examples/configs/hosted.yaml` | ~6 new |
| 7 | PROGRESS update + Layer F entry in "Known limitations" | `PROGRESS.md` | — |

Dependencies: 1 → 2 → 3 → {4, 5, 6} (parallelisable) → 7.

---

## 7. Risks + carry-forward

**Layer F gap (largest):** Non-native multi-segment runs ship with tail PNGs
persisted but unconsumed. A user expecting "continuous output" gets
"discontinuous output without crash". Must be loudly documented in
README + PROGRESS so users aren't surprised.

**ffmpeg runtime requirement:** Layer assumes `ffmpeg` is on `PATH` on
the GPU instance. Documented in README under engine prerequisites.
ComfyUI/Diffusers Docker images typically include ffmpeg already.

**Diffusers server contract drift:** New `"url"` field in `/status/{job_id}`
response is a forward-incompatible expectation. Servers that don't ship it
fail at extract time with a clear `FrameExtractionError`. Documented as a
server-implementation requirement in README.

**ComfyUI `/view` URL only works same-host (today):** Once a real-cloud
ComfyUI deployment ships (remote box), the URL needs to be a public/proxied
endpoint. Today ComfyUI runs on the same disk as the orchestrator so this
is fine. Filed as part of Layer F follow-up.

**Hosted `url_path` is dict-only:** Providers that return `{"results": [{"url": "X"}]}`
need a one-line `[0]` extension or a JSONPath swap. Out of scope; documented.

---

## 8. References

- `DESIGN.md` §3 (interfaces), §4 (data flows / discovery ordering)
- `PROGRESS.md` "Known limitations & follow-ups" — per-engine
  `extract_last_frame` follow-up (currently first bullet)
- Layer B commits: `b9cb44b` (ABC + FakeEngine + continuity helper),
  `270accd` (stage wiring) — closed GH #1
- Layer C commits: `424c7c9`, `057caaf` (`ArtifactStore.put_bytes`
  contract used by the stage persistence step)
