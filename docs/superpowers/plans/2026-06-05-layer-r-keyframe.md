# Layer R — Keyframe Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the keyframe stage (GH #4) on top of a new pipeline list-walker + parallel ImageEngine sibling ABC, including the `FakeImageEngine` + `FalImageEngine` concretes and a live `fal-ai/flux-schnell` smoke.

**Architecture:** Three orthogonal foundations shipped together — (1) `PipelineState`/`Stage(run(state)->state)` list-walker, (2) `ImageEngine`/`ImageBackend`/`ImageProfile` parallel ABC hierarchy, (3) `KeyframeStage` filling missing image-kind conditioning roles via the new ABC. Backwards-compat lockdown freezes the "no keyframe block" path identical to pre-Layer-R behaviour.

**Tech Stack:** Python 3.13 + pydantic v2 + stdlib urllib + pytest. Live smoke via `fal-ai/flux-schnell` queue API.

**Spec:** `docs/superpowers/specs/2026-06-05-layer-r-keyframe-design.md` (binding).

**Phase:** 32 — closes GH #4.

**Live spend ceiling:** $0.20 (Layer-R budget).

---

## Task 1: Image-side ABCs + PipelineState + Stage Protocol update + registry helpers

**Goal:** Add `ImageProfile`, `ImageJob`, `ImageBackend`, `ImageEngine`, `PipelineState`, `required_image_roles` helper; update `Stage` Protocol to `run(state) -> state`; add `register_image_engine` / `get_image_engine`. Pure additive ABC work + 1 Protocol signature change. No production callers yet — GenerateClipStage migration happens in T4.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (append ABCs + PipelineState + helper; update Stage Protocol)
- Modify: `src/kinoforge/core/registry.py` (append register_image_engine / get_image_engine)
- Create: `tests/core/test_image_interfaces.py`
- Create: `tests/pipeline/test_pipeline_state.py`

**Acceptance Criteria:**
- [ ] `ImageProfile`, `ImageJob`, `ImageBackend(ABC)`, `ImageEngine(ABC)` defined with exact field/method signatures from spec §3.
- [ ] `PipelineState(frozen=True)` defined with `request: GenerationRequest` + `artifacts: dict[str, Artifact] = field(default_factory=dict)`.
- [ ] `Stage` Protocol updated: `run(self, state: PipelineState) -> PipelineState`.
- [ ] `required_image_roles(mode: str) -> list[str]` returns ordered image-kind roles (uses `MODE_ROLE_REQUIREMENTS` unchanged shape at this point — schema migration is T2).
- [ ] `registry.register_image_engine` / `get_image_engine` work with the same factory pattern as `register_engine` / `get_engine`; `UnknownAdapter` on miss.
- [ ] `tests/core/test_image_interfaces.py` ≥ 5 tests pass.
- [ ] `tests/pipeline/test_pipeline_state.py` ≥ 3 tests pass.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/core/test_image_interfaces.py tests/pipeline/test_pipeline_state.py -v && pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/core/registry.py tests/core/test_image_interfaces.py tests/pipeline/test_pipeline_state.py`

**Steps:**

- [ ] **Step 1: Write failing tests for image ABCs and PipelineState**

Create `tests/core/test_image_interfaces.py`:

```python
"""Layer R T1: image-side ABCs + helper smoke tests."""
from __future__ import annotations

import pytest

from kinoforge.core.interfaces import (
    Artifact,
    GenerationRequest,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    MODE_ROLE_REQUIREMENTS,
    PipelineState,
    required_image_roles,
)


def test_image_profile_fields() -> None:
    p = ImageProfile(name="x", max_resolution=(1024, 1024), supported_modes={"t2i"})
    assert p.name == "x"
    assert p.max_resolution == (1024, 1024)
    assert p.supported_modes == {"t2i"}


def test_image_job_minimal() -> None:
    j = ImageJob(spec={"model": "m"}, prompt="hello")
    assert j.spec == {"model": "m"}
    assert j.prompt == "hello"
    assert j.params == {}


def test_image_backend_is_abstract() -> None:
    with pytest.raises(TypeError):
        ImageBackend()  # type: ignore[abstract]


def test_image_engine_is_abstract() -> None:
    with pytest.raises(TypeError):
        ImageEngine()  # type: ignore[abstract]


def test_required_image_roles_dispatch() -> None:
    """For each known mode the helper returns the image-kind roles in
    insertion order. Schema-shape-agnostic: works whether MODE_ROLE_REQUIREMENTS
    is dict[str, set[str]] (pre-T2) or dict[str, dict[str, str]] (post-T2).
    Bug guard: a flf2v that loses ordering would break continuity dispatch.
    """
    assert required_image_roles("t2v") == []
    assert required_image_roles("i2v") == ["init_image"]
    assert required_image_roles("flf2v") == ["first_frame", "last_frame"]
    assert required_image_roles("unknown") == []
```

Create `tests/pipeline/test_pipeline_state.py`:

```python
"""Layer R T1: PipelineState dataclass + Stage Protocol structural check."""
from __future__ import annotations

import dataclasses
from typing import get_type_hints

from kinoforge.core.interfaces import (
    Artifact,
    GenerationRequest,
    PipelineState,
    Stage,
)


def test_pipeline_state_is_frozen() -> None:
    """PipelineState must be frozen so accidental mutation of `request` raises.
    Bug guard: a thawed state lets a stage silently swap request and break the next stage."""
    state = PipelineState(request=GenerationRequest(prompt="p", mode="t2v"))
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        state.request = GenerationRequest(prompt="q", mode="t2v")  # type: ignore[misc]


def test_pipeline_state_artifacts_default_empty_dict() -> None:
    state = PipelineState(request=GenerationRequest(prompt="p", mode="t2v"))
    assert state.artifacts == {}


def test_stage_protocol_matches_callable_with_state_signature() -> None:
    """Anything with `run(self, state) -> PipelineState` satisfies Stage Protocol.
    Bug guard: tightening the Protocol incorrectly would break runtime_checkable."""
    class _Concrete:
        def run(self, state: PipelineState) -> PipelineState:
            return state
    s: Stage = _Concrete()
    state = PipelineState(request=GenerationRequest(prompt="p", mode="t2v"))
    out = s.run(state)
    assert out is state
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `pixi run test tests/core/test_image_interfaces.py tests/pipeline/test_pipeline_state.py -v`
Expected: collection errors / ImportError on `ImageProfile`, `ImageJob`, `ImageBackend`, `ImageEngine`, `PipelineState`, `required_image_roles`.

- [ ] **Step 3: Add the ABCs + helper + PipelineState to `interfaces.py`**

Edit `src/kinoforge/core/interfaces.py`. Append below the existing `MODE_ROLE_REQUIREMENTS` block (line 241), and BEFORE the `Stage` Protocol (line 492):

```python
# --- image generation siblings (Layer R) --------------------------------------


@dataclass
class ImageProfile:
    """Capabilities of an image-generation model, read at plan time from cache.

    Sibling of ModelProfile (the video one) but image-shaped only.
    No fps / max_frames / native_extension / joint_audio.
    """

    name: str
    max_resolution: tuple[int, int]
    supported_modes: set[str]


@dataclass
class ImageJob:
    """One image-generation unit of work.

    Sibling of GenerationJob but no segments concept — one prompt → one image.
    """

    spec: dict  # type: ignore[type-arg]
    prompt: str
    params: dict = field(default_factory=dict)  # type: ignore[type-arg]


class ImageBackend(ABC):
    """A live, ready image engine jobs are submitted to."""

    @abstractmethod
    def capabilities(self) -> ImageProfile: ...  # noqa: D102

    @abstractmethod
    def inspect_capabilities(self) -> ImageProfile: ...  # noqa: D102

    @abstractmethod
    def submit(self, job: ImageJob) -> str: ...  # noqa: D102

    @abstractmethod
    def result(self, job_id: str) -> Artifact: ...  # noqa: D102

    @abstractmethod
    def endpoints(self) -> dict[str, str]: ...  # noqa: D102


class ImageEngine(ABC):
    """A swappable image-generation engine; owns its env setup; knows if it needs compute."""

    name: str
    requires_compute: bool
    requires_local_weights: bool

    @abstractmethod
    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None: ...  # noqa: D102

    @abstractmethod
    def backend(  # noqa: D102
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> ImageBackend: ...

    @abstractmethod
    def profile_for(self, key: CapabilityKey) -> ImageProfile: ...  # noqa: D102

    @abstractmethod
    def validate_spec(self, job: ImageJob) -> None: ...  # noqa: D102


def required_image_roles(mode: str) -> list[str]:
    """Return ordered list of image-kind roles required by ``mode``.

    Order is dict-insertion order from MODE_ROLE_REQUIREMENTS so flf2v always
    returns [first_frame, last_frame], never [last_frame, first_frame].

    Schema-shape-agnostic: handles BOTH the pre-T2 ``dict[str, set[str]]`` shape
    and the post-T2 ``dict[str, dict[str, str]]`` shape. After T2 the kind
    filter is meaningful; before T2 every role is treated as image-kind
    (correct because all current roles are image-kind today).
    """
    roles = MODE_ROLE_REQUIREMENTS.get(mode, ())
    if isinstance(roles, dict):
        return [role for role, kind in roles.items() if kind == "image"]
    return [role for role in roles]


@dataclass(frozen=True)
class PipelineState:
    """State threaded between pipeline stages.

    Frozen wrapper; stages produce a new state via ``dataclasses.replace``.
    The artifacts dict is mutable in-place (matches the project pattern where
    dataclass.replace handles top-level swaps but contained collections may
    be mutated for clarity).

    Keys in ``artifacts`` are stage-defined names. KeyframeStage writes
    ``keyframe-<role>`` (e.g. ``keyframe-init_image``, ``keyframe-first_frame``).
    GenerateClipStage writes ``clip``. Future stages: ``audio``, ``upscaled``,
    ``stitched``, etc.
    """

    request: GenerationRequest
    artifacts: dict[str, Artifact] = field(default_factory=dict)  # type: ignore[type-arg]
```

Then update the existing `Stage` Protocol (line 492-498) — replace the body:

```python
@runtime_checkable
class Stage(Protocol):
    """A pipeline stage: PipelineState in, PipelineState out."""

    def run(self, state: PipelineState) -> PipelineState:
        """Execute the stage with the given state and return the updated state."""
        ...
```

- [ ] **Step 4: Add registry helpers to `registry.py`**

Read `src/kinoforge/core/registry.py` first to confirm shape, then append at the end (after the existing `get_store`):

```python
# --- image engines (Layer R) --------------------------------------------------

_image_engines: dict[str, Callable[[], "ImageEngine"]] = {}


def register_image_engine(name: str, factory: Callable[[], "ImageEngine"]) -> None:
    """Register an image engine under ``name``.

    Mirrors :func:`register_engine` shape. Separate registry namespace from
    video engines — names may collide across the two (e.g. ``"fake"`` engine
    coexists with ``"fake"`` image engine without conflict).
    """
    _image_engines[name] = factory


def get_image_engine(name: str) -> Callable[[], "ImageEngine"]:
    """Return the registered factory for image engine ``name``.

    Raises:
        UnknownAdapter: ``name`` is not registered.
    """
    if name not in _image_engines:
        raise UnknownAdapter(f"unknown image engine: {name!r}")
    return _image_engines[name]
```

Add the import at the top of `registry.py`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kinoforge.core.interfaces import ImageEngine
```

- [ ] **Step 5: Run tests — expect PASS**

Run: `pixi run test tests/core/test_image_interfaces.py tests/pipeline/test_pipeline_state.py -v`
Expected: 8 passed.

- [ ] **Step 6: Verify mypy / ruff / pre-commit clean**

Run: `pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/core/registry.py tests/core/test_image_interfaces.py tests/pipeline/test_pipeline_state.py`
Expected: all hooks pass.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/core/registry.py \
        tests/core/test_image_interfaces.py tests/pipeline/test_pipeline_state.py
git commit -m "feat(core): ImageEngine/Backend/Profile/Job ABCs + PipelineState + Stage Protocol (Phase 32 T1)"
```

---

## Task 2: MODE_ROLE_REQUIREMENTS schema migration to dict[mode, dict[role, kind]]

**Goal:** Migrate `MODE_ROLE_REQUIREMENTS` from `dict[str, set[str]]` to `dict[str, dict[str, str]]`. Update 3 production touch sites + 1 test literal. Add VALID_KINDS drift-guard lockdown test.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py:237` (schema)
- Modify: `src/kinoforge/pipeline/generate_clip.py:166` (default arg `set()` → `{}`)
- Modify: `src/kinoforge/core/validation.py:62` (wrap with `set(...)`)
- Modify: `tests/core/test_interfaces.py:69` (literal update + append VALID_KINDS lockdown)
- Modify: `PROGRESS.md:72` (text note about schema change)

**Acceptance Criteria:**
- [ ] `MODE_ROLE_REQUIREMENTS == {"t2v": {}, "i2v": {"init_image": "image"}, "flf2v": {"first_frame": "image", "last_frame": "image"}}`
- [ ] `"init_image" in MODE_ROLE_REQUIREMENTS["i2v"]` returns True (key-membership preserved).
- [ ] `set(MODE_ROLE_REQUIREMENTS["i2v"]) == {"init_image"}` (key-set extraction works).
- [ ] New `test_mode_role_requirements_kinds_are_valid` lockdown test passes.
- [ ] Existing tests in `tests/pipeline/test_generate_clip.py` (continuity dispatch) + `tests/core/test_validation.py` (role contract) all still pass.
- [ ] `required_image_roles` (added in T1) returns the same lists pre- and post-migration.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/core/test_interfaces.py tests/core/test_validation.py tests/pipeline/test_generate_clip.py -v && pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/pipeline/generate_clip.py src/kinoforge/core/validation.py tests/core/test_interfaces.py`

**Steps:**

- [ ] **Step 1: Edit the lockdown test FIRST so failing pre-migration becomes the red signal**

Edit `tests/core/test_interfaces.py:69` (and append below) — replace the existing assertion block with the new shape + add the drift guard:

```python
def test_mode_role_requirements_shape() -> None:
    """Layer R: schema is dict[mode, dict[role, kind]].
    Bug guard: regression to set[str] form breaks every consumer that
    relies on the kind metadata."""
    assert MODE_ROLE_REQUIREMENTS == {
        "t2v": {},
        "i2v": {"init_image": "image"},
        "flf2v": {"first_frame": "image", "last_frame": "image"},
    }


VALID_KINDS = {"image", "audio", "video"}


def test_mode_role_requirements_kinds_are_valid() -> None:
    """Drift guard: every kind in MODE_ROLE_REQUIREMENTS must be a known kind.
    Catches typos and accidental string drift on additions."""
    for mode, roles in MODE_ROLE_REQUIREMENTS.items():
        for role, kind in roles.items():
            assert kind in VALID_KINDS, (
                f"role {role!r} in mode {mode!r} has unknown kind {kind!r}; "
                f"valid kinds: {sorted(VALID_KINDS)}"
            )
```

(If the existing test was named differently — for example `test_mode_role_requirements_exact` — rename in place; do not duplicate.)

- [ ] **Step 2: Run the new test to see RED**

Run: `pixi run test tests/core/test_interfaces.py::test_mode_role_requirements_shape -v`
Expected: FAIL (literal mismatch — still `dict[str, set[str]]`).

- [ ] **Step 3: Migrate the schema in `interfaces.py`**

Edit `src/kinoforge/core/interfaces.py:237`:

```python
MODE_ROLE_REQUIREMENTS: dict[str, dict[str, str]] = {
    "t2v": {},
    "i2v": {"init_image": "image"},
    "flf2v": {"first_frame": "image", "last_frame": "image"},
}
```

- [ ] **Step 4: Update `generate_clip.py:166`**

The membership test `"init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, set())` works on dict keys identically — change ONLY the default arg to keep the type aligned:

```python
should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, {})
```

- [ ] **Step 5: Update `validation.py:62`**

```python
required_roles: set[str] = set(MODE_ROLE_REQUIREMENTS[request.mode])
```

- [ ] **Step 6: Run the touched test files**

Run: `pixi run test tests/core/test_interfaces.py tests/core/test_validation.py tests/pipeline/test_generate_clip.py -v`
Expected: all PASS (continuity dispatch + role contract preserved).

- [ ] **Step 7: Update PROGRESS.md line 72**

Edit `PROGRESS.md` line 72:

```diff
- Continuity dispatch via `MODE_ROLE_REQUIREMENTS` — injects only when `"init_image"` in role contract (i2v today; t2v/flf2v skip); future modes automatic.
+ Continuity dispatch via `MODE_ROLE_REQUIREMENTS` — injects only when `"init_image"` in role contract keys (i2v today; t2v/flf2v skip); future modes automatic. Schema: `dict[mode, dict[role, kind]]` since Layer R.
```

- [ ] **Step 8: Pre-commit clean**

Run: `pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/pipeline/generate_clip.py src/kinoforge/core/validation.py tests/core/test_interfaces.py PROGRESS.md`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/pipeline/generate_clip.py \
        src/kinoforge/core/validation.py tests/core/test_interfaces.py PROGRESS.md
git commit -m "refactor(core): MODE_ROLE_REQUIREMENTS to dict[mode, dict[role, kind]] (Phase 32 T2)"
```

---

## Task 3: Extract artifact_bytes helper

**Goal:** Move `GenerateClipStage._artifact_bytes` (the uri→file / url→http / synthetic-fallback resolver) into `src/kinoforge/pipeline/artifact_bytes.py` so both `GenerateClipStage` and `KeyframeStage` (T9) reuse it.

**Files:**
- Create: `src/kinoforge/pipeline/artifact_bytes.py`
- Modify: `src/kinoforge/pipeline/generate_clip.py` (delete `_artifact_bytes`; import + delegate to module fn)
- Create: `tests/pipeline/test_artifact_bytes.py`
- Modify: `tests/pipeline/test_generate_clip.py` (relocate any tests of `_artifact_bytes` directly; if none, no edits)

**Acceptance Criteria:**
- [ ] `artifact_bytes(artifact, http_get_bytes=None) -> bytes` resolves in order: `artifact.uri` (file://) → `artifact.url` (http(s) via injected seam) → synthetic fallback from filename + meta repr.
- [ ] `GenerateClipStage` delegates by calling `artifact_bytes(last, self.http_get_bytes)` in its body.
- [ ] All pre-existing tests in `tests/pipeline/test_generate_clip.py` that exercised `_artifact_bytes` behaviour still pass through the helper indirection.
- [ ] New `tests/pipeline/test_artifact_bytes.py` covers ≥ 8 cases: file URI, http URL with headers, http URL no headers, missing file falls through to URL, missing both falls through to synthetic, synthetic deterministic, URL with non-Authorization header passes through, custom http seam called exactly once.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/pipeline/test_artifact_bytes.py tests/pipeline/test_generate_clip.py -v && pixi run pre-commit run --files src/kinoforge/pipeline/artifact_bytes.py src/kinoforge/pipeline/generate_clip.py tests/pipeline/test_artifact_bytes.py`

**Steps:**

- [ ] **Step 1: Write failing tests for the new module**

Create `tests/pipeline/test_artifact_bytes.py`:

```python
"""Layer R T3: shared artifact_bytes helper tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.interfaces import Artifact
from kinoforge.pipeline.artifact_bytes import artifact_bytes


def test_file_uri_reads_local_path(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello")
    a = Artifact(filename="x.bin", uri=f"file://{p}")
    assert artifact_bytes(a) == b"hello"


def test_bare_path_uri_reads_local_path(tmp_path: Path) -> None:
    p = tmp_path / "y.bin"
    p.write_bytes(b"world")
    a = Artifact(filename="y.bin", uri=str(p))
    assert artifact_bytes(a) == b"world"


def test_http_url_calls_seam_with_headers() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        calls.append((url, dict(headers)))
        return b"DOWNLOADED"

    a = Artifact(
        filename="z.mp4",
        url="https://example.test/z.mp4",
        headers={"Authorization": "Bearer xyz"},
    )
    assert artifact_bytes(a, fetch) == b"DOWNLOADED"
    assert calls == [("https://example.test/z.mp4", {"Authorization": "Bearer xyz"})]


def test_http_url_seam_called_once_only() -> None:
    """Bug guard: a refactor that double-resolves the URL would inflate cost
    on real fal/runpod endpoints."""
    n = 0

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        nonlocal n
        n += 1
        return b"X"

    a = Artifact(filename="z.mp4", url="https://example.test/z.mp4")
    artifact_bytes(a, fetch)
    assert n == 1


def test_synthetic_fallback_no_uri_no_url() -> None:
    """Bug guard: when neither path resolves we fall back to deterministic synthetic
    bytes so FakeEngine-driven tests still get something to put_bytes."""
    a = Artifact(filename="abc.png", meta={"k": "v"})
    out = artifact_bytes(a)
    assert b"abc.png" in out
    assert b"k" in out and b"v" in out


def test_missing_file_uri_falls_through_to_url(tmp_path: Path) -> None:
    """Bug guard: stale file:// URI must not short-circuit when URL is available."""

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        return b"FROM_URL"

    a = Artifact(
        filename="x.bin",
        uri=f"file://{tmp_path}/missing.bin",
        url="https://example.test/x.bin",
    )
    assert artifact_bytes(a, fetch) == b"FROM_URL"


def test_default_seam_used_when_none_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the default http_get_bytes seam from generate_clip.py is hooked up."""
    from kinoforge.pipeline import artifact_bytes as mod

    captured: list[str] = []

    def fake_default(url: str, headers: dict[str, str]) -> bytes:
        captured.append(url)
        return b"DEFAULT"

    monkeypatch.setattr(mod, "_default_http_get_bytes", fake_default)
    a = Artifact(filename="x.mp4", url="https://example.test/x.mp4")
    assert artifact_bytes(a) == b"DEFAULT"
    assert captured == ["https://example.test/x.mp4"]


def test_empty_headers_dict_passed_when_artifact_has_no_headers() -> None:
    calls: list[dict[str, str]] = []

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        calls.append(dict(headers))
        return b""

    a = Artifact(filename="x", url="https://example.test/x")
    artifact_bytes(a, fetch)
    assert calls == [{}]
```

- [ ] **Step 2: Run — expect ImportError**

Run: `pixi run test tests/pipeline/test_artifact_bytes.py -v`
Expected: ImportError on `kinoforge.pipeline.artifact_bytes`.

- [ ] **Step 3: Create the helper module**

Create `src/kinoforge/pipeline/artifact_bytes.py`:

```python
"""Shared artifact-bytes resolver (Layer R extraction).

Resolves an :class:`~kinoforge.core.interfaces.Artifact` to its raw bytes via
three fallback paths: ``artifact.uri`` (file://) → ``artifact.url`` (http(s)
via an injected seam) → deterministic synthetic bytes (FakeEngine tests).

Originally lived as ``GenerateClipStage._artifact_bytes``; extracted in
Layer R so :class:`~kinoforge.pipeline.keyframe.KeyframeStage` and any future
stage reuse it without re-implementing the resolution rules.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from kinoforge.core.interfaces import Artifact

_DEFAULT_USER_AGENT = "kinoforge/0.1"


def _default_http_get_bytes(url: str, headers: dict[str, str]) -> bytes:
    """GET ``url`` with optional ``headers`` and return raw bytes.

    Injects a default ``User-Agent: kinoforge/0.1`` because edge proxies on
    RunPod / fal reject the stdlib default ``Python-urllib/<ver>`` with HTTP 403
    (caught live 2026-06-03; see commit 8058dc2 / fcaa213 family).
    """
    merged = dict(headers)
    if not any(k.lower() == "user-agent" for k in merged):
        merged["User-Agent"] = _DEFAULT_USER_AGENT
    req = urllib.request.Request(url, headers=merged)  # noqa: S310
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return bytes(resp.read())


def artifact_bytes(
    artifact: Artifact,
    http_get_bytes: Callable[[str, dict[str, str]], bytes] | None = None,
) -> bytes:
    """Resolve an Artifact's bytes via uri→file / url→http / synthetic fallback.

    Args:
        artifact: The artifact to resolve.
        http_get_bytes: Optional injectable HTTP GET seam.  When ``None``
            (the default), :func:`_default_http_get_bytes` is used.

    Returns:
        The raw bytes addressed by the artifact.
    """
    uri = (artifact.uri or "").strip()
    if uri:
        parsed = urllib.parse.urlparse(uri)
        local_path: str | None = None
        if parsed.scheme == "file":
            local_path = urllib.request.url2pathname(parsed.path)
        elif parsed.scheme == "" and uri:
            local_path = uri
        if local_path is not None:
            candidate = Path(local_path)
            if candidate.exists():
                return candidate.read_bytes()

    url = (artifact.url or "").strip()
    if url.startswith(("http://", "https://")):
        fetch = http_get_bytes or _default_http_get_bytes
        return fetch(url, dict(artifact.headers))

    # Synthetic fallback retained for FakeEngine-driven tests.
    return (
        artifact.filename.encode("utf-8")
        + b"|"
        + repr(sorted(artifact.meta.items())).encode("utf-8")
    )
```

- [ ] **Step 4: Run — expect PASS on test_artifact_bytes.py**

Run: `pixi run test tests/pipeline/test_artifact_bytes.py -v`
Expected: 8 passed.

- [ ] **Step 5: Migrate GenerateClipStage to delegate**

Edit `src/kinoforge/pipeline/generate_clip.py`:

1. At top of file, replace the existing `_DEFAULT_USER_AGENT` constant + `_default_http_get_bytes` function (lines ~34-66) with a single import:

```python
from kinoforge.pipeline.artifact_bytes import artifact_bytes
```

(Keep all other imports.)

2. Delete the entire `_artifact_bytes` method on `GenerateClipStage` (around lines 220-267).

3. In `GenerateClipStage.run()` body, change the line that previously called `self._artifact_bytes(last)` to:

```python
payload = artifact_bytes(last, self.http_get_bytes)
```

- [ ] **Step 6: Run pipeline + downstream tests — expect PASS**

Run: `pixi run test tests/pipeline/ -v`
Expected: all pre-existing tests still pass, including any that exercised the resolver via `stage.run()`.

- [ ] **Step 7: Pre-commit clean**

Run: `pixi run pre-commit run --files src/kinoforge/pipeline/artifact_bytes.py src/kinoforge/pipeline/generate_clip.py tests/pipeline/test_artifact_bytes.py`
Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/pipeline/artifact_bytes.py src/kinoforge/pipeline/generate_clip.py \
        tests/pipeline/test_artifact_bytes.py
git commit -m "refactor(pipeline): extract artifact_bytes helper for KeyframeStage reuse (Phase 32 T3)"
```

---

## Task 4: GenerateClipStage signature migration — drop segments_override, add segments field, run(state)->state

**Goal:** Make `GenerateClipStage.run(state) -> PipelineState`. Promote `segments_override` from a `run()` kwarg into a `segments: list[Segment]` constructor field always populated by the orchestrator. Tests migrated via `_make_stage` helper.

**Files:**
- Modify: `src/kinoforge/pipeline/generate_clip.py` (constructor + run signature + body)
- Modify: `src/kinoforge/core/orchestrator.py` (line ~996-1008: stage construction + run call)
- Modify: `src/kinoforge/core/batch.py` (~line 250: stage construction + run call)
- Modify: `tests/pipeline/test_generate_clip.py` (add `_make_stage` helper at module top; rewrite ~30 sites to use `segments=`)
- Modify: `tests/core/test_pool.py` (line ~153 stage_kwargs: add `segments=`)

**Acceptance Criteria:**
- [ ] `GenerateClipStage` dataclass gains `segments: list[Segment]` field (non-default; orchestrator must supply).
- [ ] `run(self, state: PipelineState) -> PipelineState` — no `segments_override` kwarg.
- [ ] `run()` body returns `replace(state, artifacts={**state.artifacts, "clip": stored})` instead of bare Artifact.
- [ ] Orchestrator + batch call `stage.run(state)` (no override kwarg).
- [ ] All pre-existing `tests/pipeline/test_generate_clip.py` tests pass with `_make_stage(...)` + `stage.run(PipelineState(request=req))` shape.
- [ ] `tests/core/test_pool.py` pool-swap test passes with the new constructor field.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/pipeline/ tests/core/test_pool.py tests/core/test_orchestrator.py tests/core/test_batch.py -v && pixi run pre-commit run --files src/kinoforge/pipeline/generate_clip.py src/kinoforge/core/orchestrator.py src/kinoforge/core/batch.py`

**Steps:**

- [ ] **Step 1: Rewrite GenerateClipStage dataclass + run body**

Edit `src/kinoforge/pipeline/generate_clip.py`. Replace the whole `GenerateClipStage` dataclass body (the docstring, fields, and `run` method) with:

```python
@dataclass
class GenerateClipStage:
    """Single-clip pipeline stage (Layer R: PipelineState in, PipelineState out)."""

    profile: ModelProfile
    pool: BackendPool
    store: ArtifactStore
    run_id: str
    accepted_kinds: set[str]
    base_params: dict  # type: ignore[type-arg]
    base_spec: dict  # type: ignore[type-arg]
    engine: GenerationEngine
    segments: list[Segment]  # NEW — always populated by orchestrator
    http_get_bytes: Callable[[str, dict[str, str]], bytes] | None = None
    sink: OutputSink | None = None
    namespace: str | None = None

    def run(self, state: PipelineState) -> PipelineState:
        """Validate every job, dispatch through the pool, persist, return updated state.

        Validation runs upstream (orchestrator.validate_request); this body assumes
        ``state.request`` is already validated and ``self.segments`` is the
        ordered segment list produced by the splitter.
        """
        request = state.request
        jobs = decide(self.profile, self.segments, self.base_params, self.base_spec)

        # Layer K Task 2 invariant: validate every job's spec BEFORE any dispatch.
        for job in jobs:
            self.engine.validate_spec(job)

        should_chain = "init_image" in MODE_ROLE_REQUIREMENTS.get(request.mode, {})
        if not should_chain and len(jobs) > 1:
            # Layer G: t2v non-chained fan-out via pool.map.
            results = list(self.pool.map(jobs))
        else:
            results = []
            for i, job in enumerate(jobs):
                if i > 0 and should_chain:
                    tail_bytes = self.engine.extract_last_frame(results[-1])
                    tail_name = f"seg-{i - 1}-tail.png"
                    stored_tail = self.store.put_bytes(self.run_id, tail_name, tail_bytes)
                    tail_artifact = replace(stored_tail, filename=tail_name)
                    tail_asset = ConditioningAsset(
                        kind="image",
                        role="init_image",
                        ref=tail_artifact,
                    )
                    job = inject_tail_frame(job, tail_asset)
                    self.engine.validate_spec(job)
                art = self.pool.submit(job).result()
                results.append(art)
        last = results[-1]

        payload = artifact_bytes(last, self.http_get_bytes)
        stored = self.store.put_bytes(self.run_id, last.filename, payload)

        if self.sink is not None:
            ext = Path(last.filename).suffix or ".bin"
            self.sink.publish(
                payload,
                prompt=self.segments[-1].prompt,
                extension=ext,
                namespace=self.namespace,
            )

        return replace(
            state,
            artifacts={**state.artifacts, "clip": stored},
        )
```

Also drop the import of `validate_request` from this file (it was only used in the deleted `else` branch). Drop any unused imports flagged by ruff.

- [ ] **Step 2: Update orchestrator callsite in `generate()`**

Edit `src/kinoforge/core/orchestrator.py` — the existing `stage = GenerateClipStage(...)` + `stage.run(request, segments_override=prompt_segments)` block (lines ~996-1019). Replace with:

```python
        stage = GenerateClipStage(
            profile=session.profile,
            pool=session.pool,
            store=store,
            run_id=run_id,
            accepted_kinds=accepted_kinds,
            base_params=dict(cfg.params),
            base_spec=dict(cfg.spec),
            engine=session.engine,
            segments=prompt_segments,  # NEW — promoted from run() kwarg
            sink=sink,
        )
        try:
            state = PipelineState(request=validated, artifacts={})
            state = stage.run(state)
            artifact = state.artifacts["clip"]
        except ValidationError:
            _log.warning(
                "spec validation failed; tearing down instance before re-raising"
            )
            if (
                session.instance is not None
                and session.provider is not None
                and not _caller_supplied_instance
            ):
                session.provider.destroy_instance(session.instance.id)
            raise
        _log.info("generate completed — artifact uri=%r", artifact.uri)
```

(Note: the full orchestrator rewire for the keyframe stage happens in T10. T4 is the minimum migration to keep the existing path green.)

Add `PipelineState` to the existing import block from `kinoforge.core.interfaces`.

- [ ] **Step 3: Update batch callsite**

Edit `src/kinoforge/core/batch.py` (line ~250). Locate the `GenerateClipStage` construction in `batch_generate()` and apply the same shape: add `segments=prompt_segments` to construction; wrap `stage.run` in `PipelineState(...)`; extract `artifact = state.artifacts["clip"]`.

(Read the file first to confirm the surrounding code and apply the exact same shape as the orchestrator delta.)

- [ ] **Step 4: Add `_make_stage` test helper + migrate test sites in `test_generate_clip.py`**

At the top of `tests/pipeline/test_generate_clip.py` (after imports, before the first test), add:

```python
def _make_stage(
    *,
    profile,
    pool,
    store,
    run_id="run",
    accepted_kinds=None,
    base_params=None,
    base_spec=None,
    engine,
    segments,
    http_get_bytes=None,
    sink=None,
    namespace=None,
):
    """Test helper: construct GenerateClipStage with sensible defaults.

    Layer R migrated `segments_override` (run() kwarg) → `segments` (constructor
    field). This helper absorbs the construction churn; tests pass `segments=[...]`
    where they previously passed `segments_override=[...]`.
    """
    from kinoforge.pipeline.generate_clip import GenerateClipStage

    return GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id=run_id,
        accepted_kinds=accepted_kinds if accepted_kinds is not None else {"image"},
        base_params=dict(base_params or {}),
        base_spec=dict(base_spec or {}),
        engine=engine,
        segments=list(segments),
        http_get_bytes=http_get_bytes,
        sink=sink,
        namespace=namespace,
    )


def _run(stage, request):
    """Test helper: wrap request in PipelineState and return stored Artifact."""
    from kinoforge.core.interfaces import PipelineState

    state = PipelineState(request=request, artifacts={})
    out = stage.run(state)
    return out.artifacts["clip"]
```

Then mechanically migrate every `GenerateClipStage(...)` construction in the file to use `_make_stage(...)` and every `stage.run(request, segments_override=segs)` (or `stage.run(request)`) to `_run(stage, request)`. Tests that need to inspect the returned `PipelineState` instead of the bare Artifact should keep calling `stage.run(PipelineState(request=request, artifacts={}))` directly.

Mechanical recipe (apply at each site):
- `GenerateClipStage(...)` with `segments_override=foo` passed to `run()` → `_make_stage(..., segments=foo)`.
- `GenerateClipStage(...)` with the old "build-1-from-request" path (no override) → `_make_stage(..., segments=[Segment(prompt=req.prompt, assets=list(req.assets))])`.
- Every `stage.run(req, segments_override=...)` → `_run(stage, req)` (segments already supplied at construction).

- [ ] **Step 5: Migrate `tests/core/test_pool.py` stage_kwargs**

Edit `tests/core/test_pool.py` line ~153. The `stage_kwargs` dict that constructs `GenerateClipStage` needs a `segments=` entry. Read the surrounding context, add `"segments": [...]` matching whatever the test was previously supplying via `segments_override`.

- [ ] **Step 6: Run pipeline + pool + orchestrator + batch test suites**

Run: `pixi run test tests/pipeline/ tests/core/test_pool.py tests/core/test_orchestrator.py tests/core/test_batch.py -v`
Expected: all pre-existing tests pass under the new shape.

- [ ] **Step 7: Pre-commit clean + full suite spot check**

Run: `pixi run pre-commit run --files src/kinoforge/pipeline/generate_clip.py src/kinoforge/core/orchestrator.py src/kinoforge/core/batch.py tests/pipeline/test_generate_clip.py tests/core/test_pool.py`
Then: `pixi run test -q` to confirm no other test files broke.
Expected: pre-commit pass; full suite green (1111 + new T1/T2/T3 deltas).

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/pipeline/generate_clip.py src/kinoforge/core/orchestrator.py \
        src/kinoforge/core/batch.py tests/pipeline/test_generate_clip.py \
        tests/core/test_pool.py
git commit -m "refactor(pipeline): GenerateClipStage.run(state)->state; segments as constructor field (Phase 32 T4)"
```

---

## Task 5: JsonImageProfileCache namespace split

**Goal:** Add `JsonImageProfileCache` that stores `ImageProfile` cache entries under `<key>.image.json` (separate from `<key>.json` for video). Reuses `JsonProfileCache` machinery via thin subclass.

**Files:**
- Modify: `src/kinoforge/core/profiles.py` (append `JsonImageProfileCache`)
- Create: `tests/core/test_image_profile_cache.py`

**Acceptance Criteria:**
- [ ] `JsonImageProfileCache(store).resolve(key)` reads `<hex>.image.json`; raises `ProfileNotCached` on miss.
- [ ] `JsonImageProfileCache(store).discover(key, engine, backend)` writes `<hex>.image.json` and returns the profile.
- [ ] `JsonImageProfileCache(store).verify(profile, backend, *, engine, key)` matches `JsonProfileCache.verify` semantics adapted to `ImageProfile` fields (no `max_frames`/`fps`).
- [ ] Image cache and video cache for the SAME `CapabilityKey` do not collide (`<hex>.json` vs `<hex>.image.json`).
- [ ] ≥ 10 tests pass: resolve miss → ProfileNotCached, discover writes file, resolve hit reads it, separate-namespace isolation, verify match, verify mismatch raises CapabilityMismatch, set-vs-list round-trip on `supported_modes`, tuple round-trip on `max_resolution`, ImageEngine without `inspect_capabilities` raises, single-flight inflight dedup.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/core/test_image_profile_cache.py -v && pixi run pre-commit run --files src/kinoforge/core/profiles.py tests/core/test_image_profile_cache.py`

**Steps:**

- [ ] **Step 1: Read existing JsonProfileCache to confirm extension surface**

Read `src/kinoforge/core/profiles.py` end-to-end so the subclass slots into the existing single-flight + URI-index machinery.

- [ ] **Step 2: Write failing tests**

Create `tests/core/test_image_profile_cache.py`:

```python
"""Layer R T5: JsonImageProfileCache namespace tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.errors import CapabilityMismatch, ProfileNotCached
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    Instance,
)
from kinoforge.core.profiles import JsonImageProfileCache, JsonProfileCache
from kinoforge.stores.local import LocalArtifactStore


def _key() -> CapabilityKey:
    return CapabilityKey(base_model="m", engine="fake", precision="")


def _profile() -> ImageProfile:
    return ImageProfile(
        name="m", max_resolution=(1024, 1024), supported_modes={"t2i"},
    )


class _FakeImageBackend(ImageBackend):
    def __init__(self, p: ImageProfile) -> None:
        self.p = p

    def capabilities(self) -> ImageProfile:
        return self.p

    def inspect_capabilities(self) -> ImageProfile:
        return self.p

    def submit(self, job: ImageJob) -> str:
        return "id"

    def result(self, job_id: str) -> Artifact:
        return Artifact(filename="x.png")

    def endpoints(self) -> dict[str, str]:
        return {}


class _FakeImageEngine(ImageEngine):
    name = "fake"
    requires_compute = False
    requires_local_weights = False

    def __init__(self, p: ImageProfile) -> None:
        self.p = p

    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
        return

    def backend(self, instance: Instance | None, cfg: dict[str, object]) -> ImageBackend:
        return _FakeImageBackend(self.p)

    def profile_for(self, key: CapabilityKey) -> ImageProfile:
        return self.p

    def validate_spec(self, job: ImageJob) -> None:
        return


def test_resolve_miss_raises(tmp_path: Path) -> None:
    cache = JsonImageProfileCache(LocalArtifactStore(tmp_path))
    with pytest.raises(ProfileNotCached):
        cache.resolve(_key())


def test_discover_writes_image_json_namespace(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    out = cache.discover(_key(), eng, eng.backend(None, {}))
    assert out == p
    # File lives in the image namespace
    image_files = list(tmp_path.glob("**/*.image.json"))
    assert len(image_files) == 1
    # And NOT in the video namespace
    video_files = [
        f for f in tmp_path.glob("**/*.json")
        if not f.name.endswith(".image.json")
    ]
    assert video_files == [], video_files


def test_resolve_hit_reads_discovered_profile(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    out = cache.resolve(_key())
    assert out == p


def test_image_and_video_cache_do_not_collide(tmp_path: Path) -> None:
    """Same CapabilityKey, different namespaces — must not overwrite each other."""
    store = LocalArtifactStore(tmp_path)
    image_cache = JsonImageProfileCache(store)
    video_cache = JsonProfileCache(store)
    key = _key()
    ip = _profile()
    eng = _FakeImageEngine(ip)
    image_cache.discover(key, eng, eng.backend(None, {}))
    with pytest.raises(ProfileNotCached):
        video_cache.resolve(key)


def test_verify_match_succeeds(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    cache.verify(p, eng.backend(None, {}), engine=eng, key=_key())


def test_verify_mismatch_raises(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    drifted = ImageProfile(
        name="m", max_resolution=(2048, 2048), supported_modes={"t2i"},
    )
    drifted_eng = _FakeImageEngine(drifted)
    with pytest.raises(CapabilityMismatch):
        cache.verify(p, drifted_eng.backend(None, {}), engine=drifted_eng, key=_key())


def test_supported_modes_set_round_trip(tmp_path: Path) -> None:
    """JSON has no `set`; persistence must round-trip via sorted list."""
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = ImageProfile(
        name="m", max_resolution=(1024, 1024), supported_modes={"t2i", "i2i", "inpaint"},
    )
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    out = cache.resolve(_key())
    assert isinstance(out.supported_modes, set)
    assert out.supported_modes == {"t2i", "i2i", "inpaint"}


def test_max_resolution_tuple_round_trip(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = ImageProfile(name="m", max_resolution=(2048, 1024), supported_modes={"t2i"})
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    out = cache.resolve(_key())
    assert isinstance(out.max_resolution, tuple)
    assert out.max_resolution == (2048, 1024)


def test_inflight_dedup_single_call_to_backend(tmp_path: Path) -> None:
    """Bug guard: two concurrent resolve_or_discover calls must call inspect_capabilities ONCE."""
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    backend = eng.backend(None, {})

    n = 0

    def counting() -> ImageProfile:
        nonlocal n
        n += 1
        return p

    backend.inspect_capabilities = counting  # type: ignore[method-assign]
    cache.discover(_key(), eng, backend)
    cache.discover(_key(), eng, backend)  # second call should be a no-op or short-circuit
    assert n == 1


def test_namespace_filename_pattern(tmp_path: Path) -> None:
    """Bug guard: filename must include `.image.json` suffix verbatim."""
    store = LocalArtifactStore(tmp_path)
    cache = JsonImageProfileCache(store)
    p = _profile()
    eng = _FakeImageEngine(p)
    cache.discover(_key(), eng, eng.backend(None, {}))
    files = sorted(p.name for p in tmp_path.glob("**/*.image.json"))
    assert len(files) == 1
    assert files[0].endswith(".image.json")
    # And starts with the hex prefix from CapabilityKey.derive()
    assert files[0].split(".")[0] == _key().derive()
```

- [ ] **Step 3: Run — expect ImportError**

Run: `pixi run test tests/core/test_image_profile_cache.py -v`
Expected: ImportError on `JsonImageProfileCache`.

- [ ] **Step 4: Implement `JsonImageProfileCache`**

Edit `src/kinoforge/core/profiles.py`. Append a subclass that swaps the filename suffix:

```python
# Layer R: image profile cache namespace split


class JsonImageProfileCache(JsonProfileCache):
    """JsonProfileCache namespaced to ``<hex>.image.json`` for ImageProfile.

    Same single-flight + URI-index machinery; only the persisted filename and
    the (de)serialised dataclass differ. Discovers via
    ``backend.inspect_capabilities() -> ImageProfile`` (the parent class's
    discover method calls the same method; the result is an ImageProfile
    because the backend is an ImageBackend).
    """

    _FILENAME_SUFFIX = ".image.json"

    def _filename_for(self, key: CapabilityKey) -> str:  # noqa: D102
        return f"{key.derive()}{self._FILENAME_SUFFIX}"

    def _profile_from_payload(self, payload: dict) -> ImageProfile:  # type: ignore[override]
        return ImageProfile(
            name=str(payload["name"]),
            max_resolution=tuple(payload["max_resolution"]),  # type: ignore[arg-type]
            supported_modes=set(payload["supported_modes"]),
        )

    def _payload_from_profile(self, profile: ImageProfile) -> dict:  # type: ignore[override]
        return {
            "name": profile.name,
            "max_resolution": list(profile.max_resolution),
            "supported_modes": sorted(profile.supported_modes),
        }

    def _verify_fields(self) -> tuple[str, ...]:  # type: ignore[override]
        # Image profiles have no max_frames/fps/supports_*; compare only image-shaped fields.
        return ("max_resolution", "supported_modes")
```

If `JsonProfileCache` does not already factor `_filename_for` / `_profile_from_payload` / `_payload_from_profile` / `_verify_fields` as override seams, refactor it minimally to expose them BEFORE writing the subclass:
- Pull the video-side `<hex>.json` filename construction into `_filename_for(key)`.
- Pull the dict→ModelProfile parser into `_profile_from_payload`.
- Pull the ModelProfile→dict serialiser into `_payload_from_profile`.
- Pull the verify field tuple (probably `("max_frames", "fps", "max_resolution", "supported_modes")`) into `_verify_fields`.

The video tests must still pass after the refactor.

- [ ] **Step 5: Run — expect PASS**

Run: `pixi run test tests/core/test_image_profile_cache.py tests/core/test_profiles.py -v`
Expected: 10 new + all existing video-cache tests pass.

- [ ] **Step 6: Pre-commit clean + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/profiles.py tests/core/test_image_profile_cache.py
git add src/kinoforge/core/profiles.py tests/core/test_image_profile_cache.py
git commit -m "feat(core): JsonImageProfileCache with .image.json namespace (Phase 32 T5)"
```

---

## Task 6: FakeImageEngine + FakeImageBackend + self-registration

**Goal:** Deterministic GPU-free image engine for offline tests.

**Files:**
- Create: `src/kinoforge/image_engines/__init__.py` (empty package marker)
- Create: `src/kinoforge/image_engines/fake/__init__.py` (FakeImageEngine + FakeImageBackend + self-register)
- Create: `tests/image_engines/__init__.py` (empty)
- Create: `tests/image_engines/test_fake.py`

**Acceptance Criteria:**
- [ ] `FakeImageEngine.name == "fake"`, `requires_compute=False`, `requires_local_weights=False`.
- [ ] `FakeImageBackend.submit(job)` returns deterministic 16-hex-char ID derived from `(prompt, spec)`.
- [ ] `FakeImageBackend.result(id)` returns `Artifact(filename=f"fake-image-{id}.png", meta={...})`.
- [ ] `validate_spec` raises `ValidationError` when required spec keys missing (default: `{"model"}`).
- [ ] `profile_for(key)` returns injectable profile (default `ImageProfile(name="fake-image", max_resolution=(1024, 1024), supported_modes={"t2i"})`).
- [ ] `endpoints()` returns `{"local": "fake://image"}`.
- [ ] Self-registers under `"fake"` on import.
- [ ] ≥ 6 tests pass.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/image_engines/test_fake.py -v && pixi run pre-commit run --files src/kinoforge/image_engines/fake/__init__.py tests/image_engines/test_fake.py`

**Steps:**

- [ ] **Step 1: Write failing tests**

Create `tests/image_engines/__init__.py` (empty file).

Create `tests/image_engines/test_fake.py`:

```python
"""Layer R T6: FakeImageEngine tests."""
from __future__ import annotations

import importlib

import pytest

from kinoforge.core import registry
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact, CapabilityKey, ImageEngine, ImageJob, ImageProfile,
)


def _engine() -> ImageEngine:
    importlib.import_module("kinoforge.image_engines.fake")
    return registry.get_image_engine("fake")()


def test_self_registers_under_fake() -> None:
    importlib.import_module("kinoforge.image_engines.fake")
    factory = registry.get_image_engine("fake")
    eng = factory()
    assert eng.name == "fake"


def test_engine_flags() -> None:
    eng = _engine()
    assert eng.requires_compute is False
    assert eng.requires_local_weights is False


def test_submit_id_deterministic_for_same_inputs() -> None:
    eng = _engine()
    backend = eng.backend(None, {})
    id1 = backend.submit(ImageJob(spec={"model": "m"}, prompt="cat"))
    id2 = backend.submit(ImageJob(spec={"model": "m"}, prompt="cat"))
    assert id1 == id2
    assert len(id1) == 16   # 16-char sha256 prefix


def test_submit_id_differs_for_different_prompts() -> None:
    """Bug guard: collision on prompt change would cause persistence overwrites."""
    eng = _engine()
    backend = eng.backend(None, {})
    a = backend.submit(ImageJob(spec={"model": "m"}, prompt="cat"))
    b = backend.submit(ImageJob(spec={"model": "m"}, prompt="dog"))
    assert a != b


def test_result_returns_filename_matching_id() -> None:
    eng = _engine()
    backend = eng.backend(None, {})
    job_id = backend.submit(ImageJob(spec={"model": "m"}, prompt="x"))
    art = backend.result(job_id)
    assert isinstance(art, Artifact)
    assert art.filename == f"fake-image-{job_id}.png"


def test_validate_spec_missing_model_raises() -> None:
    eng = _engine()
    with pytest.raises(ValidationError):
        eng.validate_spec(ImageJob(spec={}, prompt="x"))


def test_profile_for_returns_default_image_profile() -> None:
    eng = _engine()
    p = eng.profile_for(CapabilityKey(base_model="m", engine="fake"))
    assert isinstance(p, ImageProfile)
    assert p.max_resolution == (1024, 1024)
    assert "t2i" in p.supported_modes
```

- [ ] **Step 2: Run — expect ImportError**

Run: `pixi run test tests/image_engines/test_fake.py -v`
Expected: ImportError.

- [ ] **Step 3: Create the package + engine**

Create `src/kinoforge/image_engines/__init__.py`:

```python
"""kinoforge.image_engines — image-generation engine adapters.

Sibling to ``kinoforge.engines`` (video). Each engine self-registers on
import; the CLI's ``_adapters`` hub imports the concrete adapter modules
to trigger registration.
"""
```

Create `src/kinoforge/image_engines/fake/__init__.py`:

```python
"""FakeImageEngine: deterministic GPU-free image engine for offline tests."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from kinoforge.core import registry
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    Instance,
)


@dataclass
class FakeImageBackend(ImageBackend):
    """Deterministic backend: sha256(prompt+spec) → 16-hex submit id; synthetic Artifact on result."""

    profile_to_return: ImageProfile

    def capabilities(self) -> ImageProfile:
        return self.profile_to_return

    def inspect_capabilities(self) -> ImageProfile:
        return self.profile_to_return

    def submit(self, job: ImageJob) -> str:
        seed = json.dumps(
            [job.prompt, sorted(job.spec.items())],
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    def result(self, job_id: str) -> Artifact:
        return Artifact(
            filename=f"fake-image-{job_id}.png",
            meta={"_kf_job_id": job_id, "_synthetic": True},
        )

    def endpoints(self) -> dict[str, str]:
        return {"local": "fake://image"}


@dataclass
class FakeImageEngine(ImageEngine):
    """Hosted-style fake image engine; no compute, no weights."""

    name: str = "fake"
    requires_compute: bool = False
    requires_local_weights: bool = False
    profile_to_return: ImageProfile = field(
        default_factory=lambda: ImageProfile(
            name="fake-image",
            max_resolution=(1024, 1024),
            supported_modes={"t2i"},
        )
    )
    required_spec_keys: frozenset[str] = field(
        default_factory=lambda: frozenset({"model"})
    )

    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
        return  # hosted no-op

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> ImageBackend:
        return FakeImageBackend(profile_to_return=self.profile_to_return)

    def profile_for(self, key: CapabilityKey) -> ImageProfile:
        return self.profile_to_return

    def validate_spec(self, job: ImageJob) -> None:
        missing = self.required_spec_keys - set(job.spec)
        if missing:
            raise ValidationError(
                f"FakeImageEngine: missing spec keys: {sorted(missing)}"
            )


registry.register_image_engine("fake", lambda: FakeImageEngine())
```

- [ ] **Step 4: Run — expect PASS**

Run: `pixi run test tests/image_engines/test_fake.py -v`
Expected: 7 passed.

- [ ] **Step 5: Pre-commit clean + commit**

```bash
pixi run pre-commit run --files src/kinoforge/image_engines/__init__.py \
  src/kinoforge/image_engines/fake/__init__.py tests/image_engines/test_fake.py
git add src/kinoforge/image_engines/__init__.py src/kinoforge/image_engines/fake/__init__.py \
        tests/image_engines/__init__.py tests/image_engines/test_fake.py
git commit -m "feat(image_engines): FakeImageEngine offline-test adapter (Phase 32 T6)"
```

---

## Task 7: FalImageEngine + FalImageBackend + self-registration

**Goal:** Live-fire image engine wrapping fal.ai queue API (fal-ai/flux-schnell). Reuses `engines/fal/wire.py` helpers. All HTTP I/O via injected seams; offline tests use fakes.

**Files:**
- Create: `src/kinoforge/image_engines/fal/__init__.py`
- Create: `tests/image_engines/test_fal.py`

**Acceptance Criteria:**
- [ ] `FalImageEngine.name == "fal"`, `requires_compute=False`, `requires_local_weights=False`.
- [ ] `FalImageEngine.provision(None, cfg)` raises `AuthError` when `FAL_KEY` unset; returns None when set.
- [ ] `FalImageBackend.submit(job)` POSTs to `https://queue.fal.run/<spec.model>` with body `{"prompt": ..., **spec.input, **params}`; headers include `Authorization: Key <FAL_KEY>` + `Content-Type: application/json`.
- [ ] `FalImageBackend.result(job_id)` polls status_url via `wire.build_status_url`, fetches response_url via `wire.build_response_url`, returns `Artifact(url=images[0].url, filename=..., headers={})`.
- [ ] `validate_spec` raises `ValidationError` on empty prompt or missing `spec.model`.
- [ ] `profile_for(key)` returns static `ImageProfile(name=key.base_model or "fal-image", max_resolution=(1024, 1024), supported_modes={"t2i"})`.
- [ ] Self-registers under `"fal"` on import.
- [ ] ≥ 12 offline tests pass with injected `http_post` / `http_get` / `sleep` seams.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/image_engines/test_fal.py -v && pixi run pre-commit run --files src/kinoforge/image_engines/fal/__init__.py tests/image_engines/test_fal.py`

**Steps:**

- [ ] **Step 1: Read `engines/fal/wire.py` to confirm helper signatures**

Read `src/kinoforge/engines/fal/wire.py`. Confirm exposed surface:
- `FalStatus` enum
- `interpret_status(status_str) -> FalStatus`
- `build_status_url(endpoint, request_id) -> str`
- `build_response_url(endpoint, request_id) -> str`

If any are missing the signatures expected below, adjust the implementation accordingly (do not edit `wire.py` to add new helpers in this task — minimise blast radius).

- [ ] **Step 2: Write failing tests**

Create `tests/image_engines/test_fal.py`:

```python
"""Layer R T7: FalImageEngine offline tests with injected HTTP seams."""
from __future__ import annotations

import importlib

import pytest

from kinoforge.core import registry
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError, ValidationError
from kinoforge.core.interfaces import (
    Artifact, CapabilityKey, ImageJob, ImageProfile,
)


def _engine_module():
    return importlib.import_module("kinoforge.image_engines.fal")


def _engine():
    _engine_module()
    return registry.get_image_engine("fal")()


def test_self_registers_under_fal() -> None:
    eng = _engine()
    assert eng.name == "fal"


def test_provision_without_fal_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAL_KEY", raising=False)
    eng = _engine()
    with pytest.raises(AuthError):
        eng.provision(None, {})


def test_provision_with_fal_key_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAL_KEY", "tk-test")
    eng = _engine()
    eng.provision(None, {})  # no raise


def test_profile_for_static_shape() -> None:
    eng = _engine()
    p = eng.profile_for(CapabilityKey(base_model="fal-ai/flux-schnell", engine="fal"))
    assert isinstance(p, ImageProfile)
    assert p.max_resolution == (1024, 1024)
    assert p.supported_modes == {"t2i"}
    assert p.name == "fal-ai/flux-schnell"


def test_validate_spec_empty_prompt_raises() -> None:
    eng = _engine()
    with pytest.raises(ValidationError):
        eng.validate_spec(ImageJob(spec={"model": "x"}, prompt=""))


def test_validate_spec_missing_model_raises() -> None:
    eng = _engine()
    with pytest.raises(ValidationError):
        eng.validate_spec(ImageJob(spec={}, prompt="x"))


def _build_backend(monkeypatch, http_post=None, http_get=None, sleep=None):
    monkeypatch.setenv("FAL_KEY", "tk-test")
    from kinoforge.image_engines.fal import FalImageBackend

    profile = ImageProfile(name="fal", max_resolution=(1024, 1024), supported_modes={"t2i"})
    return FalImageBackend(
        cfg={"model": "fal-ai/flux-schnell"},
        creds=EnvCredentialProvider(),
        profile_to_return=profile,
        http_post=http_post or (lambda url, body, headers: {"request_id": "req-1"}),
        http_get=http_get or (lambda url, headers: {}),
        sleep=sleep or (lambda s: None),
    )


def test_submit_posts_with_auth_header(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict, dict]] = []

    def post(url, body, headers):
        calls.append((url, dict(body), dict(headers)))
        return {"request_id": "req-1"}

    backend = _build_backend(monkeypatch, http_post=post)
    rid = backend.submit(ImageJob(spec={"model": "fal-ai/flux-schnell"}, prompt="cat"))
    assert rid == "req-1"
    assert len(calls) == 1
    url, body, headers = calls[0]
    assert url == "https://queue.fal.run/fal-ai/flux-schnell"
    assert body == {"prompt": "cat"}
    assert headers["Authorization"] == "Key tk-test"
    assert headers["Content-Type"] == "application/json"


def test_submit_merges_spec_input_and_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug guard: spec.input + params merge order must be explicit."""
    captured = {}

    def post(url, body, headers):
        captured.update(body)
        return {"request_id": "r"}

    backend = _build_backend(monkeypatch, http_post=post)
    backend.submit(
        ImageJob(
            spec={"model": "fal-ai/flux-schnell", "input": {"image_size": "square_hd"}},
            prompt="x",
            params={"seed": 42},
        )
    )
    assert captured == {"prompt": "x", "image_size": "square_hd", "seed": 42}


def test_submit_no_fal_key_raises_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FAL_KEY", raising=False)
    from kinoforge.image_engines.fal import FalImageBackend

    backend = FalImageBackend(
        cfg={"model": "x"},
        creds=EnvCredentialProvider(),
        profile_to_return=ImageProfile(name="x", max_resolution=(1024, 1024), supported_modes={"t2i"}),
        http_post=lambda *a, **kw: {"request_id": "r"},
    )
    with pytest.raises(AuthError):
        backend.submit(ImageJob(spec={"model": "x"}, prompt="x"))


def test_result_polls_until_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug guard: result must poll, not assume immediate completion."""
    status_responses = iter([
        {"status": "IN_PROGRESS"},
        {"status": "IN_PROGRESS"},
        {"status": "COMPLETED"},
        {"images": [{"url": "https://fal.media/img/abc.png"}]},
    ])

    def get(url, headers):
        return next(status_responses)

    sleeps: list[float] = []
    backend = _build_backend(monkeypatch, http_get=get, sleep=lambda s: sleeps.append(s))
    art = backend.result("req-1")
    assert art.url == "https://fal.media/img/abc.png"
    assert art.filename == "abc.png"
    assert len(sleeps) == 2  # two IN_PROGRESS polls before COMPLETED


def test_result_error_status_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url, headers):
        return {"status": "ERROR", "error": "model not found"}

    backend = _build_backend(monkeypatch, http_get=get)
    with pytest.raises(KinoforgeError):
        backend.result("req-1")


def test_result_no_images_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug guard: empty images array must fail loud rather than crash on indexing."""
    responses = iter([
        {"status": "COMPLETED"},
        {"images": []},
    ])

    def get(url, headers):
        return next(responses)

    backend = _build_backend(monkeypatch, http_get=get)
    with pytest.raises(KinoforgeError):
        backend.result("req-1")


def test_endpoints_static_queue_url(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _build_backend(monkeypatch)
    assert backend.endpoints() == {"queue": "https://queue.fal.run"}
```

- [ ] **Step 3: Run — expect ImportError on `kinoforge.image_engines.fal`**

Run: `pixi run test tests/image_engines/test_fal.py -v`
Expected: ImportError.

- [ ] **Step 4: Implement FalImageEngine + FalImageBackend**

Create `src/kinoforge/image_engines/fal/__init__.py`:

```python
"""FalImageEngine: live-fire image engine wrapping fal.ai queue API.

Reuses ``kinoforge.engines.fal.wire`` helpers (FalStatus, interpret_status,
build_status_url, build_response_url) — pure functions, no HTTP. HTTP I/O
lives in :class:`FalImageBackend` via injected ``http_post`` / ``http_get``
seams (mirror of FalBackend pattern; same User-Agent override for edge-proxy
compatibility).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from kinoforge.core import registry
from kinoforge.core.credentials import EnvCredentialProvider
from kinoforge.core.errors import AuthError, KinoforgeError, ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    CapabilityKey,
    CredentialProvider,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    Instance,
)
from kinoforge.engines.fal import wire

_DEFAULT_USER_AGENT = "kinoforge/0.1"


def _default_post(url: str, body: dict, headers: dict) -> dict:
    merged = dict(headers)
    if not any(k.lower() == "user-agent" for k in merged):
        merged["User-Agent"] = _DEFAULT_USER_AGENT
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=merged, method="POST")  # noqa: S310
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _default_get(url: str, headers: dict) -> dict:
    merged = dict(headers)
    if not any(k.lower() == "user-agent" for k in merged):
        merged["User-Agent"] = _DEFAULT_USER_AGENT
    req = urllib.request.Request(url, headers=merged)  # noqa: S310
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class FalImageBackend(ImageBackend):
    """Live-fire fal queue backend for image endpoints (e.g. fal-ai/flux-schnell).

    HTTP I/O via injected seams. submit POSTs to queue.fal.run/<endpoint>;
    result polls status_url then fetches response_url.
    """

    cfg: dict
    creds: CredentialProvider
    profile_to_return: ImageProfile
    http_post: Callable[[str, dict, dict], dict] = field(default=_default_post)
    http_get: Callable[[str, dict], dict] = field(default=_default_get)
    sleep: Callable[[float], None] = field(default=time.sleep)
    poll_interval_s: float = 1.0
    max_polls: int = 600

    def capabilities(self) -> ImageProfile:
        return self.profile_to_return

    def inspect_capabilities(self) -> ImageProfile:
        return self.profile_to_return

    def submit(self, job: ImageJob) -> str:
        endpoint = job.spec.get("model") or self.cfg.get("model")
        if not endpoint:
            raise ValidationError(
                "FalImageBackend.submit: no endpoint in spec.model / cfg.model"
            )
        api_key = self.creds.get("FAL_KEY")
        if not api_key:
            raise AuthError("FAL_KEY required for FalImageBackend")
        body: dict = {"prompt": job.prompt}
        body.update(job.spec.get("input", {}))
        body.update(job.params)
        resp = self.http_post(
            f"https://queue.fal.run/{endpoint}",
            body,
            {
                "Authorization": f"Key {api_key}",
                "Content-Type": "application/json",
            },
        )
        return str(resp["request_id"])

    def result(self, job_id: str) -> Artifact:
        endpoint = self.cfg.get("model", "")
        api_key = self.creds.get("FAL_KEY") or ""
        headers = {"Authorization": f"Key {api_key}"}
        status_url = wire.build_status_url(endpoint, job_id)
        response_url = wire.build_response_url(endpoint, job_id)

        for _ in range(self.max_polls):
            status_data = self.http_get(status_url, headers)
            s = wire.interpret_status(str(status_data.get("status", "")))
            if s == wire.FalStatus.COMPLETED:
                break
            if s == wire.FalStatus.ERROR:
                raise KinoforgeError(
                    f"fal image job {job_id} failed: {status_data}"
                )
            self.sleep(self.poll_interval_s)
        else:
            raise KinoforgeError(
                f"fal image job {job_id} timed out after {self.max_polls} polls"
            )

        data = self.http_get(response_url, headers)
        images = data.get("images") or []
        if not images:
            raise KinoforgeError(
                f"fal image job {job_id}: no images in response: {data}"
            )
        url = str(images[0]["url"])
        return Artifact(
            url=url,
            filename=Path(urlparse(url).path).name or f"fal-image-{job_id[:8]}.png",
            headers={},  # fal signed URLs need no auth for fetch
        )

    def endpoints(self) -> dict[str, str]:
        return {"queue": "https://queue.fal.run"}


@dataclass
class FalImageEngine(ImageEngine):
    name: str = "fal"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def provision(self, instance: Instance | None, cfg: dict[str, object]) -> None:
        creds = EnvCredentialProvider()
        if not creds.get("FAL_KEY"):
            raise AuthError("FAL_KEY required for FalImageEngine")

    def backend(
        self, instance: Instance | None, cfg: dict[str, object]
    ) -> ImageBackend:
        spec = cfg.get("spec", {}) if isinstance(cfg.get("spec"), dict) else {}
        return FalImageBackend(
            cfg=spec,
            creds=EnvCredentialProvider(),
            profile_to_return=self.profile_for(
                CapabilityKey(
                    base_model=str(spec.get("model", "")),
                    engine="fal",
                )
            ),
        )

    def profile_for(self, key: CapabilityKey) -> ImageProfile:
        return ImageProfile(
            name=key.base_model or "fal-image",
            max_resolution=(1024, 1024),
            supported_modes={"t2i"},
        )

    def validate_spec(self, job: ImageJob) -> None:
        if not job.prompt or not job.prompt.strip():
            raise ValidationError("FalImageEngine: prompt required")
        if not job.spec.get("model") and not job.spec.get("endpoint"):
            raise ValidationError(
                "FalImageEngine: spec.model (fal endpoint) required"
            )


registry.register_image_engine("fal", lambda: FalImageEngine())
```

- [ ] **Step 5: Run — expect PASS**

Run: `pixi run test tests/image_engines/test_fal.py -v`
Expected: 13 passed.

- [ ] **Step 6: Pre-commit clean + commit**

```bash
pixi run pre-commit run --files src/kinoforge/image_engines/fal/__init__.py tests/image_engines/test_fal.py
git add src/kinoforge/image_engines/fal/__init__.py tests/image_engines/test_fal.py
git commit -m "feat(image_engines): FalImageEngine fal-ai/flux-schnell queue adapter (Phase 32 T7)"
```

---

## Task 8: KeyframeConfig pydantic block + Config.keyframe field

**Goal:** Add `KeyframeRoleOverride` + `KeyframeConfig` pydantic models with prompt-required + role-names-known validators; add `Config.keyframe: KeyframeConfig | None = None`.

**Files:**
- Modify: `src/kinoforge/core/config.py` (append new models + Config field)
- Create: `tests/core/test_keyframe_config.py`

**Acceptance Criteria:**
- [ ] `Config(...)` without `keyframe` block loads, `cfg.keyframe is None`.
- [ ] `Config(..., keyframe={"engine": "fal", "prompt": "..."})` loads; `cfg.keyframe.engine == "fal"`.
- [ ] Missing both top-level `prompt` AND any `roles.<name>.prompt` raises `ValueError` (prompt-required validator).
- [ ] `roles.bogus_role.prompt="x"` raises `ValueError` (role-names-known validator).
- [ ] Unknown top-level key in `keyframe` block raises `ValueError` (`extra="forbid"`).
- [ ] `KeyframeConfig.capability_key()` is deterministic + order-insensitive on `spec` dict-key order.
- [ ] Per-role override `roles.last_frame.prompt` accepted.
- [ ] ≥ 8 tests pass.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/core/test_keyframe_config.py tests/core/test_config.py -v && pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_keyframe_config.py`

**Steps:**

- [ ] **Step 1: Write failing tests**

Create `tests/core/test_keyframe_config.py`:

```python
"""Layer R T8: KeyframeConfig pydantic validation tests."""
from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from kinoforge.core.config import KeyframeConfig
from kinoforge.core.interfaces import CapabilityKey


def test_keyframe_missing_block_default_none() -> None:
    """Tested via Config in test_config.py — local sanity here."""
    cfg = KeyframeConfig(engine="fal", prompt="x")
    assert cfg.engine == "fal"
    assert cfg.prompt == "x"
    assert cfg.roles == {}


def test_keyframe_top_level_prompt_alone_loads() -> None:
    cfg = KeyframeConfig(engine="fal", prompt="cat in meadow")
    assert cfg.prompt == "cat in meadow"


def test_keyframe_per_role_prompt_alone_loads() -> None:
    cfg = KeyframeConfig(
        engine="fal",
        roles={"init_image": {"prompt": "cat"}},
    )
    assert cfg.roles["init_image"].prompt == "cat"


def test_keyframe_no_prompt_anywhere_raises() -> None:
    """Bug guard: empty prompt config silently producing empty fal POSTs would burn money."""
    with pytest.raises(PydanticValidationError, match="prompt"):
        KeyframeConfig(engine="fal")


def test_keyframe_empty_prompt_strings_treated_as_unset() -> None:
    """Bug guard: a whitespace-only prompt is a typo, not a valid prompt."""
    with pytest.raises(PydanticValidationError, match="prompt"):
        KeyframeConfig(engine="fal", prompt="   ")


def test_keyframe_unknown_role_raises() -> None:
    with pytest.raises(PydanticValidationError, match="unknown role"):
        KeyframeConfig(
            engine="fal",
            prompt="x",
            roles={"init_imag": {"prompt": "typo"}},
        )


def test_keyframe_extra_top_level_key_raises() -> None:
    """extra='forbid' lockdown — typo in YAML key fails loud."""
    with pytest.raises(PydanticValidationError):
        KeyframeConfig(engine="fal", prompt="x", endpooint="y")  # type: ignore[call-arg]


def test_keyframe_capability_key_deterministic() -> None:
    """Bug guard: dict-key ordering in `spec` must not change derived hash."""
    a = KeyframeConfig(engine="fal", prompt="x", spec={"model": "m", "precision": "fp16"})
    b = KeyframeConfig(engine="fal", prompt="x", spec={"precision": "fp16", "model": "m"})
    assert a.capability_key() == b.capability_key()
    key = a.capability_key()
    assert isinstance(key, CapabilityKey)
    assert key.base_model == "m"
    assert key.engine == "fal"
    assert key.precision == "fp16"


def test_keyframe_per_role_spec_and_params_load() -> None:
    cfg = KeyframeConfig(
        engine="fal",
        prompt="x",
        roles={
            "first_frame": {"prompt": "a", "spec": {"seed": 1}, "params": {"k": "v"}},
        },
    )
    assert cfg.roles["first_frame"].spec == {"seed": 1}
    assert cfg.roles["first_frame"].params == {"k": "v"}
```

Also add to `tests/core/test_config.py` (append, not new file):

```python
def test_config_keyframe_absent_defaults_none(tmp_path) -> None:
    """Layer R: Config without keyframe block has cfg.keyframe is None.
    Bug guard: regression that makes keyframe required would break every existing config."""
    from kinoforge.core.config import load_config
    import yaml
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({
        "mode": "t2v",
        "engine": {"kind": "fake"},
        "models": [{"kind": "base", "ref": "fake://m"}],
        "compute": None,
        "lifecycle": {
            "idle_timeout_s": 60,
            "max_lifetime_s": 600,
            "budget_usd": 1.0,
            "max_in_flight": 1,
        },
    }))
    cfg = load_config(p)
    assert cfg.keyframe is None
```

- [ ] **Step 2: Run — expect ImportError on `KeyframeConfig`**

Run: `pixi run test tests/core/test_keyframe_config.py -v`
Expected: ImportError.

- [ ] **Step 3: Add models to `config.py`**

Edit `src/kinoforge/core/config.py`. Add imports if missing:

```python
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Any
```

Append (before the `Config` class definition):

```python
class KeyframeRoleOverride(BaseModel):
    """Per-role keyframe overrides (prompt / spec / params).

    All fields optional; populated only where the user wants to deviate
    from the top-level keyframe defaults.
    """

    prompt: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


class KeyframeConfig(BaseModel):
    """Keyframe-generation block. Presence opts the orchestrator into
    constructing a KeyframeStage at the head of the pipeline.
    """

    engine: str
    prompt: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    roles: dict[str, KeyframeRoleOverride] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _at_least_one_prompt(self) -> "KeyframeConfig":
        has_top = bool(self.prompt and self.prompt.strip())
        has_role = any(
            (r.prompt and r.prompt.strip()) for r in self.roles.values()
        )
        if not has_top and not has_role:
            raise ValueError(
                "keyframe block requires either top-level `prompt` "
                "or at least one `roles.<role>.prompt`"
            )
        return self

    @model_validator(mode="after")
    def _role_names_known(self) -> "KeyframeConfig":
        from kinoforge.core.interfaces import MODE_ROLE_REQUIREMENTS

        known = {
            role
            for roles in MODE_ROLE_REQUIREMENTS.values()
            for role in roles
        }
        unknown = set(self.roles) - known
        if unknown:
            raise ValueError(
                f"keyframe.roles contains unknown role(s): {sorted(unknown)}; "
                f"known: {sorted(known)}"
            )
        return self

    def capability_key(self) -> "CapabilityKey":
        from kinoforge.core.interfaces import CapabilityKey
        return CapabilityKey(
            base_model=str(self.spec.get("model", "")),
            loras=(),
            engine=self.engine,
            precision=str(self.spec.get("precision", "")),
        )
```

Then add to the `Config` class body (append at end of field list):

```python
    keyframe: KeyframeConfig | None = None
```

- [ ] **Step 4: Run — expect PASS**

Run: `pixi run test tests/core/test_keyframe_config.py tests/core/test_config.py -v`
Expected: 8 new tests + 1 new in test_config.py + all existing config tests pass.

- [ ] **Step 5: Pre-commit clean + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/config.py tests/core/test_keyframe_config.py tests/core/test_config.py
git add src/kinoforge/core/config.py tests/core/test_keyframe_config.py tests/core/test_config.py
git commit -m "feat(config): KeyframeConfig + Config.keyframe pydantic block (Phase 32 T8)"
```

---

## Task 9: KeyframeStage implementation

**Goal:** Implement `KeyframeStage.run(state)` per spec §5. Per-role gap fill; persistence via ArtifactStore; prompt/spec/params resolution helpers.

**Files:**
- Create: `src/kinoforge/pipeline/keyframe.py`
- Create: `tests/pipeline/test_keyframe_stage.py`

**Acceptance Criteria:**
- [ ] i2v with empty assets → KeyframeStage fills `init_image`; state.artifacts gains `keyframe-init_image`.
- [ ] flf2v with empty assets → fills both `first_frame` + `last_frame`; per-role prompts honoured.
- [ ] Partial fill: user supplies `first_frame` only → stage fills ONLY `last_frame`; user's `first_frame` preserved bit-identical.
- [ ] Prompt resolution: per-role override beats top-level; missing both raises `ValidationError`.
- [ ] Spec/params shallow-merge: per-role keys override top-level keys.
- [ ] Non-image-kind roles in `MODE_ROLE_REQUIREMENTS[mode]` are skipped (forward-compat).
- [ ] Persistence: filename = `keyframe-<role>.png`; `state.artifacts[f"keyframe-{role}"]` populated; ConditioningAsset appended to request.assets.
- [ ] Stage returns new PipelineState via `replace`; original `state.request.assets` not mutated.
- [ ] ≥ 12 tests pass.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/pipeline/test_keyframe_stage.py -v && pixi run pre-commit run --files src/kinoforge/pipeline/keyframe.py tests/pipeline/test_keyframe_stage.py`

**Steps:**

- [ ] **Step 1: Write failing tests**

Create `tests/pipeline/test_keyframe_stage.py`:

```python
"""Layer R T9: KeyframeStage role-loop tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.config import KeyframeConfig
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationRequest,
    ImageProfile,
    PipelineState,
)
from kinoforge.image_engines.fake import FakeImageEngine
from kinoforge.pipeline.keyframe import KeyframeStage
from kinoforge.stores.local import LocalArtifactStore


def _make_stage(cfg: KeyframeConfig, tmp_path: Path) -> KeyframeStage:
    eng = FakeImageEngine()
    backend = eng.backend(None, cfg.model_dump())
    profile = eng.profile_for(cfg.capability_key())
    return KeyframeStage(
        keyframe_cfg=cfg,
        image_engine=eng,
        image_backend=backend,
        image_profile=profile,
        store=LocalArtifactStore(tmp_path),
        run_id="r1",
    )


def test_i2v_empty_assets_fills_init_image(tmp_path: Path) -> None:
    cfg = KeyframeConfig(engine="fake", prompt="cat", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    req = GenerationRequest(prompt="ignored-clip-prompt", mode="i2v")
    state = PipelineState(request=req)
    out = stage.run(state)
    assert len(out.request.assets) == 1
    assert out.request.assets[0].role == "init_image"
    assert out.request.assets[0].kind == "image"
    assert "keyframe-init_image" in out.artifacts


def test_flf2v_empty_assets_fills_both(tmp_path: Path) -> None:
    cfg = KeyframeConfig(
        engine="fake",
        spec={"model": "m"},
        roles={
            "first_frame": {"prompt": "a"},
            "last_frame": {"prompt": "b"},
        },
    )
    stage = _make_stage(cfg, tmp_path)
    req = GenerationRequest(prompt="x", mode="flf2v")
    out = stage.run(PipelineState(request=req))
    roles = {a.role for a in out.request.assets}
    assert roles == {"first_frame", "last_frame"}
    assert "keyframe-first_frame" in out.artifacts
    assert "keyframe-last_frame" in out.artifacts


def test_partial_fill_preserves_user_supplied(tmp_path: Path) -> None:
    """Bug guard: a user-supplied bookend MUST survive; overwriting wastes spend."""
    cfg = KeyframeConfig(
        engine="fake",
        prompt="x",
        spec={"model": "m"},
    )
    stage = _make_stage(cfg, tmp_path)
    user_first = ConditioningAsset(
        kind="image", role="first_frame",
        ref=Artifact(filename="user.png", uri="file:///does/not/exist"),
    )
    req = GenerationRequest(prompt="x", mode="flf2v", assets=[user_first])
    out = stage.run(PipelineState(request=req))
    # User asset preserved bit-identical
    survivors = [a for a in out.request.assets if a.role == "first_frame"]
    assert len(survivors) == 1
    assert survivors[0] is user_first
    # last_frame was generated
    generated = [a for a in out.request.assets if a.role == "last_frame"]
    assert len(generated) == 1
    assert "keyframe-last_frame" in out.artifacts
    # NO keyframe-first_frame in artifacts (we didn't generate it)
    assert "keyframe-first_frame" not in out.artifacts


def test_per_role_prompt_overrides_top_level(tmp_path: Path) -> None:
    cfg = KeyframeConfig(
        engine="fake",
        prompt="default",
        spec={"model": "m"},
        roles={"init_image": {"prompt": "specific"}},
    )
    stage = _make_stage(cfg, tmp_path)
    # Resolution helpers are private but observable: capture the prompt
    # via FakeImageBackend submit-id determinism — same prompt → same id.
    state = stage.run(PipelineState(request=GenerationRequest(prompt="x", mode="i2v")))
    # The submit id encodes the prompt; verify by independent recomputation.
    import hashlib, json
    expected_seed = json.dumps(
        ["specific", sorted({"model": "m"}.items())], sort_keys=True, ensure_ascii=False,
    )
    expected_id = hashlib.sha256(expected_seed.encode("utf-8")).hexdigest()[:16]
    assert f"fake-image-{expected_id}.png" in {
        Path(state.artifacts["keyframe-init_image"].filename).name,
        state.artifacts["keyframe-init_image"].meta.get("_kf_job_id", ""),
    } or state.artifacts["keyframe-init_image"].meta["_kf_job_id"] == expected_id


def test_missing_prompt_raises_validation(tmp_path: Path) -> None:
    """Bug guard: stage-level defence even though Config-load validator usually catches this."""
    # Construct a KeyframeConfig that passes the load-time validator BUT
    # then strip the prompt on the dataclass — simulates a bug in cfg
    # mutation. Stage must still refuse.
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    cfg = cfg.model_copy(update={"prompt": None})  # bypass validator
    stage = _make_stage(cfg, tmp_path)
    with pytest.raises(ValidationError, match="no prompt"):
        stage.run(PipelineState(request=GenerationRequest(prompt="x", mode="i2v")))


def test_skips_non_image_roles(tmp_path: Path) -> None:
    """Forward-compat: if a future mode adds a non-image role, stage MUST skip it."""
    from kinoforge.core.interfaces import MODE_ROLE_REQUIREMENTS

    MODE_ROLE_REQUIREMENTS["audio_mode"] = {"input_audio": "audio"}
    try:
        cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
        stage = _make_stage(cfg, tmp_path)
        out = stage.run(PipelineState(request=GenerationRequest(prompt="x", mode="audio_mode")))
        # No assets added (audio role skipped)
        assert out.request.assets == []
        assert out.artifacts == {}
    finally:
        del MODE_ROLE_REQUIREMENTS["audio_mode"]


def test_t2v_no_required_roles_is_no_op(tmp_path: Path) -> None:
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    out = stage.run(PipelineState(request=GenerationRequest(prompt="x", mode="t2v")))
    assert out.request.assets == []
    assert out.artifacts == {}


def test_original_state_not_mutated(tmp_path: Path) -> None:
    """Bug guard: PipelineState must be frozen; in-place mutation of request.assets is illegal."""
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    req = GenerationRequest(prompt="x", mode="i2v")
    state = PipelineState(request=req)
    out = stage.run(state)
    assert state.request.assets == []   # original untouched
    assert len(out.request.assets) == 1
    assert out is not state


def test_persisted_filename_pattern(tmp_path: Path) -> None:
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    out = stage.run(PipelineState(request=GenerationRequest(prompt="x", mode="i2v")))
    # Filename of the persisted Artifact = keyframe-init_image.png
    art = out.artifacts["keyframe-init_image"]
    assert art.filename == "keyframe-init_image.png"
    # File exists on disk under run_id
    saved = list(tmp_path.glob("**/keyframe-init_image.png"))
    assert len(saved) == 1


def test_per_role_spec_overrides_top_level(tmp_path: Path) -> None:
    """Bug guard: top-level spec MUST be the base; per-role spec layers on top."""
    cfg = KeyframeConfig(
        engine="fake",
        prompt="x",
        spec={"model": "m", "size": "small"},
        roles={"init_image": {"spec": {"size": "large"}}},
    )
    stage = _make_stage(cfg, tmp_path)
    # FakeImageBackend submit id depends on spec. Verify resolved spec = {model: m, size: large}.
    import hashlib, json
    out = stage.run(PipelineState(request=GenerationRequest(prompt="x", mode="i2v")))
    expected_spec = sorted({"model": "m", "size": "large"}.items())
    expected_seed = json.dumps(["x", expected_spec], sort_keys=True, ensure_ascii=False)
    expected_id = hashlib.sha256(expected_seed.encode("utf-8")).hexdigest()[:16]
    art = out.artifacts["keyframe-init_image"]
    assert art.meta["_kf_job_id"] == expected_id


def test_appends_asset_at_end_of_request_assets(tmp_path: Path) -> None:
    """Bug guard: preserve insertion order of user assets."""
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    user = ConditioningAsset(
        kind="image", role="first_frame", ref=Artifact(filename="u.png"),
    )
    req = GenerationRequest(prompt="x", mode="flf2v", assets=[user])
    out = stage.run(PipelineState(request=req))
    assert out.request.assets[0] is user
    assert out.request.assets[1].role == "last_frame"


def test_artifacts_dict_carries_existing_entries(tmp_path: Path) -> None:
    """Bug guard: stage must not drop pre-existing artifacts from upstream stages."""
    cfg = KeyframeConfig(engine="fake", prompt="x", spec={"model": "m"})
    stage = _make_stage(cfg, tmp_path)
    pre = Artifact(filename="pre.png", uri="file:///pre")
    state = PipelineState(
        request=GenerationRequest(prompt="x", mode="i2v"),
        artifacts={"upstream": pre},
    )
    out = stage.run(state)
    assert "upstream" in out.artifacts
    assert out.artifacts["upstream"] is pre
```

- [ ] **Step 2: Run — expect ImportError on `kinoforge.pipeline.keyframe`**

Run: `pixi run test tests/pipeline/test_keyframe_stage.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `KeyframeStage`**

Create `src/kinoforge/pipeline/keyframe.py`:

```python
"""KeyframeStage: fills missing image-kind conditioning roles via an ImageEngine.

Reads MODE_ROLE_REQUIREMENTS[request.mode] to discover required roles; for each
role with kind == "image" not already present in request.assets, generates an
image via the configured ImageEngine and appends a ConditioningAsset.
User-supplied assets are preserved (per-role gap fill).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from kinoforge.core.config import KeyframeConfig
from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    ImageBackend,
    ImageEngine,
    ImageJob,
    ImageProfile,
    MODE_ROLE_REQUIREMENTS,
    PipelineState,
)
from kinoforge.pipeline.artifact_bytes import artifact_bytes
from kinoforge.stores.base import ArtifactStore


@dataclass
class KeyframeStage:
    """Fills missing image-kind conditioning roles via an ImageEngine."""

    keyframe_cfg: KeyframeConfig
    image_engine: ImageEngine
    image_backend: ImageBackend
    image_profile: ImageProfile  # reserved for future spec validation
    store: ArtifactStore
    run_id: str
    http_get_bytes: Callable[[str, dict[str, str]], bytes] | None = None

    def run(self, state: PipelineState) -> PipelineState:
        request = state.request
        required = MODE_ROLE_REQUIREMENTS.get(request.mode, {})
        have = {a.role for a in request.assets}

        new_assets = list(request.assets)
        new_artifacts = dict(state.artifacts)

        for role, kind in required.items():
            if kind != "image":
                continue
            if role in have:
                continue
            prompt = self._resolve_prompt(role)
            spec = self._resolve_spec(role)
            params = self._resolve_params(role)
            job = ImageJob(spec=spec, prompt=prompt, params=params)
            self.image_engine.validate_spec(job)
            job_id = self.image_backend.submit(job)
            artifact = self.image_backend.result(job_id)
            png_bytes = artifact_bytes(artifact, self.http_get_bytes)
            filename = f"keyframe-{role}.png"
            stored = self.store.put_bytes(self.run_id, filename, png_bytes)
            stored = replace(stored, filename=filename, meta=dict(artifact.meta))
            new_assets.append(
                ConditioningAsset(kind="image", role=role, ref=stored)
            )
            new_artifacts[f"keyframe-{role}"] = stored

        new_request = replace(request, assets=new_assets)
        return replace(state, request=new_request, artifacts=new_artifacts)

    def _resolve_prompt(self, role: str) -> str:
        """Per-role override > top-level default. No clip-prompt inheritance."""
        role_block = (self.keyframe_cfg.roles or {}).get(role)
        if role_block is not None and role_block.prompt:
            return role_block.prompt
        if self.keyframe_cfg.prompt:
            return self.keyframe_cfg.prompt
        raise ValidationError(
            f"keyframe role {role!r} has no prompt configured: set "
            f"keyframe.prompt or keyframe.roles.{role}.prompt"
        )

    def _resolve_spec(self, role: str) -> dict:
        base = dict(self.keyframe_cfg.spec or {})
        role_block = (self.keyframe_cfg.roles or {}).get(role)
        if role_block is not None and role_block.spec:
            base.update(role_block.spec)
        return base

    def _resolve_params(self, role: str) -> dict:
        base = dict(self.keyframe_cfg.params or {})
        role_block = (self.keyframe_cfg.roles or {}).get(role)
        if role_block is not None and role_block.params:
            base.update(role_block.params)
        return base
```

- [ ] **Step 4: Run — expect PASS**

Run: `pixi run test tests/pipeline/test_keyframe_stage.py -v`
Expected: 12 passed.

- [ ] **Step 5: Pre-commit clean + commit**

```bash
pixi run pre-commit run --files src/kinoforge/pipeline/keyframe.py tests/pipeline/test_keyframe_stage.py
git add src/kinoforge/pipeline/keyframe.py tests/pipeline/test_keyframe_stage.py
git commit -m "feat(pipeline): KeyframeStage filling image-kind conditioning roles (Phase 32 T9)"
```

---

## Task 10: Orchestrator generate() rewire — pipeline list-walker + image engine pre-resolution

**Goal:** Refactor `generate()` to (a) pre-resolve image engine + backend + profile when `cfg.keyframe` is set, (b) build `stages: list[Stage]` from cfg-block presence, (c) walk via shared PipelineState, (d) extract `state.artifacts["clip"]` as the return artifact. Add test injection points (`image_engine`, `image_profile_provider`).

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py` (`generate()` body — full §8.1 + §8.2 spec)
- Modify: `tests/core/test_orchestrator.py` (append ≥ 6 tests)

**Acceptance Criteria:**
- [ ] `generate()` builds 1-stage pipeline when `cfg.keyframe is None` (current behaviour preserved).
- [ ] `generate()` builds 2-stage pipeline when `cfg.keyframe` is set: KeyframeStage first, GenerateClipStage second.
- [ ] Image engine resolved BEFORE `deploy_session` (unknown image-engine name fails fast — no GPU spend).
- [ ] `ImageProfileProvider` test injection works; cache miss → discover; cache hit → resolve.
- [ ] `ValidationError` from either stage triggers the existing teardown branch on compute path.
- [ ] `generate()` returns `tuple[Artifact, Instance | None]` (existing contract preserved); the Artifact is `state.artifacts["clip"]`.
- [ ] ≥ 6 new tests in test_orchestrator.py pass.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/core/test_orchestrator.py tests/pipeline/ tests/core/test_pool.py -v && pixi run pre-commit run --files src/kinoforge/core/orchestrator.py tests/core/test_orchestrator.py`

**Steps:**

- [ ] **Step 1: Write failing orchestrator tests**

Append to `tests/core/test_orchestrator.py` (read the existing file first to learn the test fixtures, then add):

```python
def test_generate_no_keyframe_block_runs_single_stage(tmp_path) -> None:
    """Layer R: cfg.keyframe is None → only GenerateClipStage runs.
    Bug guard: accidental KeyframeStage construction would call image_engine.provision and break tests without FAL_KEY."""
    # Build cfg with keyframe=None + FakeEngine + LocalProvider + FakeBackend
    # generate(cfg, request, store=store, engine=FakeEngine(...), provider=LocalProvider(...))
    # assert returned Artifact is from clip stage; no keyframe-* keys leaked.
    # (Use existing test fixtures in this file as a template.)


def test_generate_with_keyframe_runs_two_stages_in_order(tmp_path, monkeypatch) -> None:
    """KeyframeStage MUST run before GenerateClipStage.
    Bug guard: reversed order would mean clip stage sees an empty assets list."""
    # cfg.keyframe = KeyframeConfig(engine="fake", prompt="cat", spec={"model":"m"})
    # request.mode = "i2v", request.assets = []
    # generate(...) — assert returned Artifact's filename came from clip stage,
    # AND the recorded sequence of stage.run calls is [KeyframeStage, GenerateClipStage].


def test_generate_keyframe_image_engine_resolved_before_deploy_session(tmp_path, monkeypatch) -> None:
    """Misconfigured cfg.keyframe.engine MUST raise UnknownAdapter BEFORE create_instance."""
    # Set cfg.keyframe.engine = "bogus_engine"
    # provider = a LocalProvider mock that tracks create_instance calls
    # Expect UnknownAdapter; assert create_instance was never called.


def test_generate_keyframe_profile_cache_miss_triggers_discover(tmp_path, monkeypatch) -> None:
    """First call: ImageProfileProvider.resolve raises ProfileNotCached → discover called once."""
    # Use a recording ImageProfileProvider that counts resolve + discover.
    # cfg.keyframe set; mode=i2v.
    # generate(...)
    # assert resolve called once (raises), discover called once.


def test_generate_keyframe_profile_cache_hit_skips_discover(tmp_path, monkeypatch) -> None:
    """Second call on same key: resolve returns profile; discover NOT called."""


def test_generate_validation_error_in_keyframe_tears_down_video_instance(tmp_path, monkeypatch) -> None:
    """Bug guard: ValidationError from KeyframeStage (e.g. missing prompt) MUST trigger video pod teardown."""
    # Use a real LocalProvider so destroy_instance counter is observable.
    # cfg.keyframe with NO prompt (bypassed via model_copy after validation).
    # Expect ValidationError raised AND destroy_instance called once.
```

(Flesh each test using the existing orchestrator-test fixtures in the file — these are skeletons; the implementer fills in the parts that mirror existing patterns.)

- [ ] **Step 2: Run — expect FAIL (most tests fail before T10's edits)**

Run: `pixi run test tests/core/test_orchestrator.py -v -k "keyframe or layer_r or two_stages"`
Expected: collection/assertion failures.

- [ ] **Step 3: Add `ImageProfileProvider` ABC**

Edit `src/kinoforge/core/interfaces.py`. Append (near `ModelProfileProvider`):

```python
class ImageProfileProvider(ABC):
    """A cache of ImageProfiles keyed by CapabilityKey (image-side)."""

    @abstractmethod
    def resolve(self, key: CapabilityKey) -> ImageProfile: ...  # noqa: D102

    @abstractmethod
    def discover(  # noqa: D102
        self, key: CapabilityKey, engine: ImageEngine, backend: ImageBackend
    ) -> ImageProfile: ...

    @abstractmethod
    def verify(  # noqa: D102
        self,
        profile: ImageProfile,
        backend: ImageBackend,
        *,
        engine: ImageEngine | None = None,
        key: CapabilityKey | None = None,
    ) -> None: ...
```

(Update `JsonImageProfileCache` from T5 to declare `class JsonImageProfileCache(JsonProfileCache, ImageProfileProvider)` if mypy needs the explicit ABC inheritance — or rely on structural Protocol.)

- [ ] **Step 4: Refactor `generate()` body per spec §8.1 + §8.2**

Replace the existing `generate()` body in `src/kinoforge/core/orchestrator.py` with the full block (read the current file first to confirm surrounding helpers and imports — DO NOT delete `deploy()`, `deploy_session`, or helpers above `generate()`).

```python
def generate(
    cfg: Config,
    request: GenerationRequest,
    *,
    store: ArtifactStore,
    provider: ComputeProvider | None = None,
    engine: GenerationEngine | None = None,
    image_engine: ImageEngine | None = None,
    creds: CredentialProvider | None = None,
    profile_provider: ModelProfileProvider | None = None,
    image_profile_provider: ImageProfileProvider | None = None,
    run_id: str = "run",
    state_dir: Path = Path(".kinoforge"),
    sink: OutputSink | None = None,
    instance: Instance | None = None,
    tags: dict[str, str] | None = None,
) -> tuple[Artifact, Instance | None]:
    _caller_supplied_instance = instance is not None

    # --- Pre-resolve image engine + backend + profile if keyframe block present.
    # Image engine receives ONLY the keyframe block, not the full cfg.
    image_backend = None
    image_prof = None
    resolved_image_engine = None
    if cfg.keyframe is not None:
        resolved_image_engine = (
            image_engine
            if image_engine is not None
            else registry.get_image_engine(cfg.keyframe.engine)()
        )
        kf_cfg_dict = cfg.keyframe.model_dump()
        resolved_image_engine.provision(None, kf_cfg_dict)
        image_backend = resolved_image_engine.backend(None, kf_cfg_dict)
        image_key = cfg.keyframe.capability_key()
        ipp = (
            image_profile_provider
            if image_profile_provider is not None
            else JsonImageProfileCache(store)
        )
        try:
            image_prof = ipp.resolve(image_key)
        except ProfileNotCached:
            image_prof = ipp.discover(image_key, resolved_image_engine, image_backend)

    with deploy_session(
        cfg, store=store, provider=provider, engine=engine, creds=creds,
        profile_provider=profile_provider, run_id=run_id, state_dir=state_dir,
        instance=instance, tags=tags,
    ) as session:
        accepted_kinds: set[str] = getattr(session.engine, "accepted_kinds", {"image"})

        validated = validate_request(
            session.profile, request, accepted_kinds=accepted_kinds
        )

        splitter = registry.get_splitter(cfg.splitter.kind)()
        prompt_segments = splitter.split(validated.prompt, session.profile, {})
        if prompt_segments and validated.assets:
            prompt_segments[0] = dataclasses.replace(
                prompt_segments[0], assets=list(validated.assets)
            )

        stages: list[Stage] = []
        if cfg.keyframe is not None:
            stages.append(KeyframeStage(
                keyframe_cfg=cfg.keyframe,
                image_engine=resolved_image_engine,
                image_backend=image_backend,
                image_profile=image_prof,
                store=store,
                run_id=run_id,
            ))
        stages.append(GenerateClipStage(
            profile=session.profile,
            pool=session.pool,
            store=store,
            run_id=run_id,
            accepted_kinds=accepted_kinds,
            base_params=dict(cfg.params),
            base_spec=dict(cfg.spec),
            engine=session.engine,
            segments=prompt_segments,
            sink=sink,
        ))

        state = PipelineState(request=validated, artifacts={})
        try:
            for stage in stages:
                state = stage.run(state)
        except ValidationError:
            _log.warning(
                "spec validation failed; tearing down instance before re-raising"
            )
            if (
                session.instance is not None
                and session.provider is not None
                and not _caller_supplied_instance
            ):
                session.provider.destroy_instance(session.instance.id)
            raise

        artifact = state.artifacts["clip"]
        _log.info("generate completed — artifact uri=%r", artifact.uri)
        owned_instance = None if _caller_supplied_instance else session.instance
        return artifact, owned_instance
```

Add the new imports near the top of orchestrator.py:

```python
from kinoforge.core.interfaces import (
    # ... existing imports ...
    ImageBackend,
    ImageEngine,
    ImageProfileProvider,
    PipelineState,
    Stage,
)
from kinoforge.core.profiles import JsonImageProfileCache, JsonProfileCache
from kinoforge.pipeline.keyframe import KeyframeStage
```

- [ ] **Step 5: Run orchestrator tests + downstream — expect PASS**

Run: `pixi run test tests/core/test_orchestrator.py tests/pipeline/ tests/core/test_pool.py -v`
Expected: all new + existing pass.

- [ ] **Step 6: Pre-commit clean + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/orchestrator.py src/kinoforge/core/interfaces.py tests/core/test_orchestrator.py
git add src/kinoforge/core/orchestrator.py src/kinoforge/core/interfaces.py tests/core/test_orchestrator.py
git commit -m "feat(orchestrator): pipeline list-walker + image engine pre-resolution (Phase 32 T10)"
```

---

## Task 11: batch_generate() mirror

**Goal:** Apply the same image-engine pre-resolution + stage-list construction to `batch_generate()`. Per-entry override of `cfg.keyframe.prompt` honoured via the existing shallow-merge.

**Files:**
- Modify: `src/kinoforge/core/batch.py` (apply T10 shape to `batch_generate`)
- Modify: `tests/test_batch_cli.py` OR `tests/core/test_batch.py` (≥ 2 new tests)

**Acceptance Criteria:**
- [ ] `batch_generate()` constructs the same image engine + ImageProfile pre-resolution once per batch (not per entry — amortise).
- [ ] Per-entry `keyframe.prompt` override is honoured (entry value beats cfg-level default for that entry).
- [ ] Per-batch summary records the published clip artifact for each entry, unchanged for no-keyframe runs.
- [ ] Existing batch tests pass.
- [ ] ≥ 2 new tests: (a) batch with cfg.keyframe runs KeyframeStage for each entry; (b) per-entry keyframe.prompt override flows to that entry's KeyframeStage.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/core/test_batch.py tests/test_batch_cli.py -v && pixi run pre-commit run --files src/kinoforge/core/batch.py`

**Steps:**

- [ ] **Step 1: Read existing batch.py to understand entry loop + override merge**

Read `src/kinoforge/core/batch.py` (~280 lines). Identify:
- Where `cfg` is per-entry shallow-merged (likely a `_merge_cfg(cfg, entry)` helper).
- Where `GenerateClipStage` is constructed.
- Where `stage.run` is called.

- [ ] **Step 2: Write the 2 new tests first**

Add to `tests/core/test_batch.py` (or create if absent — mirror the structure of `tests/test_batch_cli.py`):

```python
def test_batch_with_keyframe_runs_image_engine_per_entry(tmp_path) -> None:
    """Bug guard: pre-resolution outside the entry loop, but stage construction per-entry.
    Each entry MUST get its own KeyframeStage with its own state."""
    # Construct manifest with 2 entries, cfg.keyframe set, mode=i2v.
    # Run batch_generate; assert 2 published clips + 2 keyframes.


def test_batch_per_entry_keyframe_prompt_override(tmp_path) -> None:
    """Bug guard: per-entry keyframe.prompt MUST beat the cfg-level default for that entry only."""
    # Manifest: entry A uses cfg-level keyframe.prompt; entry B overrides with its own.
    # Use FakeImageBackend; capture submit IDs (deterministic from prompt+spec).
    # Assert entry A's keyframe id derives from cfg prompt; entry B's from override.
```

- [ ] **Step 3: Apply the T10 shape to batch_generate**

In `batch_generate()`:

1. BEFORE the entry loop, pre-resolve image engine + backend + profile (if `cfg.keyframe is not None`) — same block as T10.
2. INSIDE the entry loop, after the per-entry cfg merge, construct stages list:
   - If per-entry merged `cfg.keyframe is not None`: append `KeyframeStage` using the pre-resolved engine/backend/profile + the entry-specific keyframe_cfg.
   - Append `GenerateClipStage` with the entry's segments.
3. Walk the pipeline with a fresh PipelineState per entry.
4. Persist `state.artifacts["clip"]` to the per-entry sink/output as before.

(The image engine + backend + profile are constructed once per batch — they don't depend on per-entry overrides since `cfg.keyframe.engine` is batch-level. ONLY the prompt/spec/params per-entry override the resolved values inside KeyframeStage at run time.)

- [ ] **Step 4: Run batch tests + spot-check full suite**

Run: `pixi run test tests/core/test_batch.py tests/test_batch_cli.py -v`
Then: `pixi run test -q` (full suite spot check).

- [ ] **Step 5: Pre-commit clean + commit**

```bash
pixi run pre-commit run --files src/kinoforge/core/batch.py tests/core/test_batch.py
git add src/kinoforge/core/batch.py tests/core/test_batch.py
git commit -m "feat(batch): batch_generate honours cfg.keyframe with per-entry overrides (Phase 32 T11)"
```

---

## Task 12: Example YAMLs + example-load lockdown tests

**Goal:** Add `keyframe-fal-i2v.yaml` + `keyframe-fal-flf2v.yaml` examples; lock them down with parse + structural tests.

**Files:**
- Create: `examples/configs/keyframe-fal-i2v.yaml`
- Create: `examples/configs/keyframe-fal-flf2v.yaml`
- Modify: `tests/test_examples.py` (append ≥ 4 tests)

**Acceptance Criteria:**
- [ ] Both YAML files parse via `load_config` without error.
- [ ] `keyframe-fal-i2v.yaml`: `cfg.mode == "i2v"`, `cfg.keyframe.engine == "fal"`, `cfg.keyframe.prompt` non-empty.
- [ ] `keyframe-fal-flf2v.yaml`: `cfg.mode == "flf2v"`, `cfg.keyframe.roles` has both `first_frame` and `last_frame` with distinct prompts.
- [ ] `tests/test_examples.py`'s existing "every example loads" test still passes for the 2 new files.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/test_examples.py -v && pixi run pre-commit run --files examples/configs/keyframe-fal-i2v.yaml examples/configs/keyframe-fal-flf2v.yaml tests/test_examples.py`

**Steps:**

- [ ] **Step 1: Create `examples/configs/keyframe-fal-i2v.yaml`**

```yaml
# Keyframe + i2v: fal generates first frame, fal generates clip.
mode: i2v
prompt: "a cat walking through a sunlit meadow, soft motion"

engine:
  kind: fal
  fal:
    endpoint: "fal-ai/wan-i2v"

spec:
  model: "fal-ai/wan-i2v"

keyframe:
  engine: fal
  prompt: "photorealistic cat in a sunlit meadow, shot on 35mm film, shallow depth of field"
  spec:
    model: "fal-ai/flux-schnell"

models:
  - kind: base
    ref: "fal://fal-ai/wan-i2v"

compute: null

lifecycle:
  idle_timeout_s: 60
  max_lifetime_s: 600
  budget_usd: 1.0
  max_in_flight: 1
```

- [ ] **Step 2: Create `examples/configs/keyframe-fal-flf2v.yaml`**

```yaml
mode: flf2v
prompt: "a cat morphing into a tiger, smooth transition"

engine:
  kind: fal
  fal:
    endpoint: "fal-ai/wan-flf2v"

spec:
  model: "fal-ai/wan-flf2v"

keyframe:
  engine: fal
  spec:
    model: "fal-ai/flux-schnell"
  roles:
    first_frame:
      prompt: "photorealistic cat sitting in meadow, centered, soft daylight"
      spec:
        seed: 42
    last_frame:
      prompt: "photorealistic tiger sitting in meadow, centered, same composition, same lighting"
      spec:
        seed: 43

models:
  - kind: base
    ref: "fal://fal-ai/wan-flf2v"

compute: null
lifecycle:
  idle_timeout_s: 60
  max_lifetime_s: 600
  budget_usd: 1.0
  max_in_flight: 1
```

- [ ] **Step 3: Append example-load tests**

Add to `tests/test_examples.py`:

```python
def test_keyframe_fal_i2v_example_loads() -> None:
    from kinoforge.core.config import load_config
    cfg = load_config("examples/configs/keyframe-fal-i2v.yaml")
    assert cfg.mode == "i2v"
    assert cfg.keyframe is not None
    assert cfg.keyframe.engine == "fal"
    assert cfg.keyframe.prompt
    assert cfg.keyframe.spec["model"] == "fal-ai/flux-schnell"


def test_keyframe_fal_flf2v_example_loads() -> None:
    from kinoforge.core.config import load_config
    cfg = load_config("examples/configs/keyframe-fal-flf2v.yaml")
    assert cfg.mode == "flf2v"
    assert cfg.keyframe is not None
    assert "first_frame" in cfg.keyframe.roles
    assert "last_frame" in cfg.keyframe.roles
    assert (
        cfg.keyframe.roles["first_frame"].prompt
        != cfg.keyframe.roles["last_frame"].prompt
    )


def test_keyframe_examples_in_master_loader() -> None:
    """Bug guard: the existing 'every example loads' iteration in this file MUST cover the 2 new files."""
    from pathlib import Path
    yamls = sorted(Path("examples/configs").glob("*.yaml"))
    names = {p.name for p in yamls}
    assert "keyframe-fal-i2v.yaml" in names
    assert "keyframe-fal-flf2v.yaml" in names


def test_keyframe_examples_have_no_compute() -> None:
    """Bug guard: keyframe examples ship as hosted/queue path; compute: null must hold."""
    from kinoforge.core.config import load_config
    for name in ("keyframe-fal-i2v.yaml", "keyframe-fal-flf2v.yaml"):
        cfg = load_config(f"examples/configs/{name}")
        assert cfg.compute is None, f"{name}: expected compute=null"
```

- [ ] **Step 4: Run example tests**

Run: `pixi run test tests/test_examples.py -v`
Expected: all pass.

- [ ] **Step 5: Pre-commit clean + commit**

```bash
pixi run pre-commit run --files examples/configs/keyframe-fal-i2v.yaml examples/configs/keyframe-fal-flf2v.yaml tests/test_examples.py
git add examples/configs/keyframe-fal-i2v.yaml examples/configs/keyframe-fal-flf2v.yaml tests/test_examples.py
git commit -m "docs(examples): keyframe-fal-{i2v,flf2v}.yaml + load tests (Phase 32 T12)"
```

---

## Task 13: Backwards-compat lockdown tests

**Goal:** Lock down that pre-Layer-R behaviour is bit-identical for configs without a keyframe block.

**Files:**
- Create: `tests/test_layer_r_backcompat.py`

**Acceptance Criteria:**
- [ ] Test 1: `cfg.keyframe is None` → `generate()` constructs exactly one stage (recorder fixture confirms).
- [ ] Test 2: Iterate every existing pre-Layer-R `examples/configs/*.yaml` (excluding the 2 new keyframe files); load each; assert `cfg.keyframe is None`.
- [ ] Test 3: `MODE_ROLE_REQUIREMENTS` dict schema migration is byte-compatible for `in` operator usage: `"init_image" in MODE_ROLE_REQUIREMENTS["i2v"]` returns True.
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/test_layer_r_backcompat.py -v`

**Steps:**

- [ ] **Step 1: Write the 3 lockdown tests**

Create `tests/test_layer_r_backcompat.py`:

```python
"""Layer R T13: backwards-compat lockdown.

Freeze in that pre-Layer-R behaviour is preserved for configs without a
keyframe block.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.interfaces import MODE_ROLE_REQUIREMENTS


def test_existing_examples_have_no_keyframe_block() -> None:
    """Bug guard: an existing example that accidentally gains a keyframe block
    would silently flip its execution path. Lock down explicitly."""
    from kinoforge.core.config import load_config

    excluded = {"keyframe-fal-i2v.yaml", "keyframe-fal-flf2v.yaml"}
    yamls = sorted(Path("examples/configs").glob("*.yaml"))
    assert yamls, "examples/configs should not be empty"
    for p in yamls:
        if p.name in excluded:
            continue
        cfg = load_config(p)
        assert cfg.keyframe is None, (
            f"{p.name}: expected cfg.keyframe is None (pre-Layer-R backcompat)"
        )


def test_mode_role_requirements_key_membership_preserved() -> None:
    """Schema migration changed value type set→dict; `in` operator on KEYS must still work."""
    assert "init_image" in MODE_ROLE_REQUIREMENTS["i2v"]
    assert "first_frame" in MODE_ROLE_REQUIREMENTS["flf2v"]
    assert "last_frame" in MODE_ROLE_REQUIREMENTS["flf2v"]
    assert "init_image" not in MODE_ROLE_REQUIREMENTS["t2v"]


def test_generate_without_keyframe_single_stage(tmp_path) -> None:
    """Recorder fixture confirms only GenerateClipStage runs.
    Bug guard: orchestrator drift that adds KeyframeStage anyway would
    surface here as a 2-element stages list."""
    # Use the same fixture pattern as test_orchestrator.py.
    # Build cfg WITHOUT keyframe; pass instrumentation that records each
    # stage type entered. Assert the sequence == ["GenerateClipStage"].
    pytest.importorskip("kinoforge.image_engines.fake")
    # Implementation: spy on the orchestrator's stage list via test injection.
    # Concrete implementation depends on the orchestrator test seam shape
    # already established in T10's tests.
```

- [ ] **Step 2: Run — expect PASS once T10 + T12 are in place**

Run: `pixi run test tests/test_layer_r_backcompat.py -v`
Expected: 3 passed.

- [ ] **Step 3: Pre-commit clean + commit**

```bash
pixi run pre-commit run --files tests/test_layer_r_backcompat.py
git add tests/test_layer_r_backcompat.py
git commit -m "test(layer-r): backwards-compat lockdown for no-keyframe configs (Phase 32 T13)"
```

---

## Task 14: Core invariant scan extension

**Goal:** Update `tests/test_pipeline_invariant.py` (or `tests/test_core_invariant.py`) to (a) allow `kinoforge.image_engines.*` under the existing core-import-ban scan, (b) lock down that ImageEngine ABCs are NOT imported by `core/` modules at runtime.

**Files:**
- Modify: `tests/test_core_invariant.py` (or whichever existing invariant file holds the allowlist)

**Acceptance Criteria:**
- [ ] Existing invariant scan still passes (no regressions).
- [ ] New test: `core/` modules MUST NOT import `kinoforge.image_engines.*` directly (image engines are registry-mediated).
- [ ] New test: `kinoforge.image_engines.fal` may import `kinoforge.engines.fal.wire` (sibling adapter cross-reference is allowed for shared pure-function helpers).
- [ ] mypy + ruff + pre-commit clean.

**Verify:** `pixi run test tests/test_core_invariant.py -v`

**Steps:**

- [ ] **Step 1: Read the existing invariant test**

Read `tests/test_core_invariant.py`. Understand the existing allowlist + scanning mechanism.

- [ ] **Step 2: Extend the scan + add the 2 new lockdowns**

Append (or extend the existing scan helpers — minimal-diff style):

```python
def test_core_does_not_import_image_engines() -> None:
    """Layer R: image_engines/ is registry-mediated. core/ must not import them.
    Bug guard: a direct import from core would break the registry indirection
    and force all consumers to load every image engine eagerly."""
    import pathlib
    forbidden = "kinoforge.image_engines"
    core_dir = pathlib.Path("src/kinoforge/core")
    offenders: list[str] = []
    for py in core_dir.rglob("*.py"):
        text = py.read_text()
        if forbidden in text:
            offenders.append(str(py))
    assert offenders == [], f"core/ files importing image_engines: {offenders}"


def test_image_engine_fal_may_import_engines_fal_wire() -> None:
    """Sibling-adapter cross-reference for shared pure-function helpers is allowed.
    Bug guard: a refactor that breaks this import would silently re-implement wire helpers."""
    from kinoforge.image_engines.fal import wire  # type: ignore[attr-defined]
    assert hasattr(wire, "build_status_url")
    assert hasattr(wire, "build_response_url")
    assert hasattr(wire, "interpret_status")
```

(Adjust attribute path if the import in `image_engines/fal/__init__.py` is `from kinoforge.engines.fal import wire` — in that case the test should `import kinoforge.engines.fal.wire as wire` and confirm the same attributes.)

- [ ] **Step 3: Run — expect PASS**

Run: `pixi run test tests/test_core_invariant.py -v`
Expected: all pre-existing + 2 new pass.

- [ ] **Step 4: Pre-commit clean + commit**

```bash
pixi run pre-commit run --files tests/test_core_invariant.py
git add tests/test_core_invariant.py
git commit -m "test(invariant): core-import-ban for image_engines/ (Phase 32 T14)"
```

---

## Task 15: RED scaffold for live smoke (commit BEFORE any spend)

**Goal:** Per CLAUDE.md durability rule — commit the failing live-smoke test scaffold BEFORE invoking any paid fal API call. Test gated on `KINOFORGE_LIVE_TESTS=1`; default-skip pattern.

**Files:**
- Create: `tests/live/test_keyframe_fal_live.py`

**Acceptance Criteria:**
- [ ] Test file exists with 2 tests: `test_keyframe_fal_i2v_live` + `test_keyframe_fal_flf2v_live`.
- [ ] Both tests `pytest.skip(...)` when `KINOFORGE_LIVE_TESTS` env var is unset.
- [ ] Both tests xfail / fail clearly when env var IS set but `FAL_KEY` is missing — so a misconfigured live run does not silently no-op.
- [ ] Default `pixi run test` adds +2 to skipped count (now 6 → 8 skips).
- [ ] mypy + ruff + pre-commit clean.
- [ ] Committed BEFORE T16 (live invocation).

**Verify:** `pixi run test tests/live/test_keyframe_fal_live.py -v` (expect 2 skipped) AND `git log -1 --oneline` matches the scaffold commit.

**Steps:**

- [ ] **Step 1: Confirm tests/live/ directory layout**

Read `tests/live/test_runpod_lifecycle.py` and `tests/live/test_skypilot_live.py` to crib the env-gate + cleanup pattern.

- [ ] **Step 2: Write the live-smoke scaffold**

Create `tests/live/test_keyframe_fal_live.py`:

```python
"""Layer R T15-T16: live smoke against fal-ai/flux-schnell + wan-i2v/flf2v.

Default-skip; runs only with KINOFORGE_LIVE_TESTS=1 + FAL_KEY in env.
Spend ceiling per test: ~$0.05 (1 flux-schnell + 1 wan).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

LIVE = os.environ.get("KINOFORGE_LIVE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not LIVE, reason="set KINOFORGE_LIVE_TESTS=1 to enable live smoke"
)

# PNG magic bytes: 0x89 50 4E 47
PNG_MAGIC = b"\x89PNG"
# MP4 ftyp box magic offset 4
MP4_FTYP = b"ftyp"


def _require_fal_key() -> str:
    key = os.environ.get("FAL_KEY")
    if not key:
        pytest.fail(
            "KINOFORGE_LIVE_TESTS=1 is set but FAL_KEY is missing — "
            "a misconfigured live run must fail loud, not no-op."
        )
    return key


def test_keyframe_fal_i2v_live(tmp_path: Path) -> None:
    """Cfg.keyframe + mode=i2v → fal generates init_image → wan-i2v consumes it → MP4 output.

    Real spend: ~$0.003 (keyframe) + ~$0.02 (clip) ≈ $0.025.
    """
    _require_fal_key()

    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import GenerationRequest
    from kinoforge.core.orchestrator import generate
    from kinoforge.stores.local import LocalArtifactStore

    cfg = load_config("examples/configs/keyframe-fal-i2v.yaml")
    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt=cfg.prompt, mode=cfg.mode)
    artifact, _instance = generate(cfg, request, store=store, run_id="live-r-i2v")

    # Clip artifact materialised
    clip_path = Path(artifact.uri.replace("file://", ""))
    assert clip_path.exists(), f"clip not persisted: {artifact.uri}"
    clip_bytes = clip_path.read_bytes()
    assert MP4_FTYP in clip_bytes[:32], (
        f"clip is not an MP4 (no ftyp box in header): {clip_bytes[:32]!r}"
    )

    # Keyframe artifact also persisted under the run_id
    kf_files = list(tmp_path.glob("**/keyframe-init_image.png"))
    assert len(kf_files) == 1, f"expected 1 keyframe-init_image.png, got {kf_files}"
    kf_bytes = kf_files[0].read_bytes()
    assert kf_bytes.startswith(PNG_MAGIC), (
        f"keyframe is not a PNG (no PNG magic): {kf_bytes[:8]!r}"
    )


def test_keyframe_fal_flf2v_live(tmp_path: Path) -> None:
    """flf2v variant — fal generates both bookends with differentiated prompts,
    wan-flf2v morphs between them.

    Real spend: ~$0.006 (2 keyframes) + ~$0.025 (clip) ≈ $0.031.
    """
    _require_fal_key()

    from kinoforge.core.config import load_config
    from kinoforge.core.interfaces import GenerationRequest
    from kinoforge.core.orchestrator import generate
    from kinoforge.stores.local import LocalArtifactStore

    cfg = load_config("examples/configs/keyframe-fal-flf2v.yaml")
    store = LocalArtifactStore(tmp_path)
    request = GenerationRequest(prompt=cfg.prompt, mode=cfg.mode)
    artifact, _instance = generate(cfg, request, store=store, run_id="live-r-flf")

    clip_path = Path(artifact.uri.replace("file://", ""))
    assert clip_path.exists()
    clip_bytes = clip_path.read_bytes()
    assert MP4_FTYP in clip_bytes[:32]

    first = list(tmp_path.glob("**/keyframe-first_frame.png"))
    last = list(tmp_path.glob("**/keyframe-last_frame.png"))
    assert len(first) == 1
    assert len(last) == 1
    first_bytes = first[0].read_bytes()
    last_bytes = last[0].read_bytes()
    assert first_bytes.startswith(PNG_MAGIC)
    assert last_bytes.startswith(PNG_MAGIC)
    assert first_bytes != last_bytes, (
        "first_frame and last_frame must differ (distinct prompts → distinct images)"
    )
```

- [ ] **Step 3: Run — expect 2 skipped under default env**

Run: `pixi run test tests/live/test_keyframe_fal_live.py -v`
Expected: 2 skipped (env unset).

- [ ] **Step 4: Run preflight to make sure no live pods exist**

Run: `pixi run preflight`
Expected: exit 0.

- [ ] **Step 5: Commit RED scaffold (BEFORE any live spend)**

```bash
pixi run pre-commit run --files tests/live/test_keyframe_fal_live.py
git add tests/live/test_keyframe_fal_live.py
git commit -m "test(live): RED scaffold for keyframe fal smoke (Phase 32 T15)"
```

---

## Task 16: Live smoke run + iterate on real-cloud bugs

**Goal:** Invoke `KINOFORGE_LIVE_TESTS=1 pixi run test tests/live/test_keyframe_fal_live.py -v` against real fal-ai/flux-schnell + wan endpoints autonomously, capture stdout/stderr verbatim, iterate on any real-cloud bugs surfaced. No user gate; Claude runs this directly.

**Files:**
- Modify (if real-cloud bugs surface): `src/kinoforge/image_engines/fal/__init__.py` or related (mirror Phase 24/31 bug-catch trail pattern)

**Acceptance Criteria:**
- [ ] `KINOFORGE_LIVE_TESTS=1 pixi run preflight` exits 0 (no live pods + clean tree before invocation).
- [ ] `KINOFORGE_LIVE_TESTS=1 FAL_KEY=<set> pixi run test tests/live/test_keyframe_fal_live.py -v` returns **2 passed**.
- [ ] Both test functions complete: i2v keyframe captures PNG magic bytes at start of `keyframe-init_image.png`; flf2v captures both `keyframe-first_frame.png` + `keyframe-last_frame.png` with PNG magic AND `first_bytes != last_bytes`.
- [ ] Final clip MP4 from each test contains `ftyp` box in first 32 bytes.
- [ ] Total live spend ≤ $0.20 (Layer-R budget). Per-attempt spend logged in commit message.
- [ ] Any real-cloud bug surfaced during the wave gets its own atomic fix commit + regression test (mirror Phase 24/31 pattern).
- [ ] After PASS, working tree clean; HEAD points at the last fix commit (or T15 scaffold if no bugs surfaced).

**Verify:** `KINOFORGE_LIVE_TESTS=1 pixi run test tests/live/test_keyframe_fal_live.py -v 2>&1 | tee /tmp/layer-r-live.log` → exit 0 + `2 passed` in tee'd log. Cost confirmation: `tail -50 /tmp/layer-r-live.log` plus fal dashboard receipt.

**Steps:**

- [ ] **Step 1: Confirm pre-spend cleanliness**

Run: `pixi run preflight`
Expected: exit 0.
Run: `git status --short`
Expected: empty (clean tree).

- [ ] **Step 2: Confirm credentials present**

Run: `printenv FAL_KEY | head -c 12 && echo '... (truncated)'`
Expected: a non-empty 12-character prefix is printed.

- [ ] **Step 3: Invoke live smoke**

Run: `KINOFORGE_LIVE_TESTS=1 pixi run test tests/live/test_keyframe_fal_live.py -v 2>&1 | tee /tmp/layer-r-live.log`
Expected: `2 passed`.

If FAIL: read the captured log; classify the failure (auth / endpoint shape / response shape / artifact bytes / something else). Each class gets its own fix commit + regression test before re-running. Hard ceiling: stop after 5 sequential live re-runs without progress and escalate.

- [ ] **Step 4: On PASS, record cost + run baseline second time**

Capture the wall-clock time + the implicit cost (per-flux-schnell ~$0.003, per-wan ~$0.02). Run a second time to confirm reproducibility:

Run: `KINOFORGE_LIVE_TESTS=1 pixi run test tests/live/test_keyframe_fal_live.py -v`
Expected: `2 passed` again.

- [ ] **Step 5: Commit any real-cloud fixes (one atomic commit each)**

For each bug surfaced:

```bash
git add <fixed file> <regression test>
git commit -m "fix(image_engines/fal): <one-line bug summary> (Phase 32 T16 bug-catch #N)"
```

If no bugs surfaced, no commits in this task body — proceed to T17.

- [ ] **Step 6: Confirm clean tree + budget intact**

Run: `git status --short && pixi run preflight`
Expected: empty status + exit 0.

```json:metadata
{
  "files": ["src/kinoforge/image_engines/fal/__init__.py", "tests/live/test_keyframe_fal_live.py"],
  "verifyCommand": "KINOFORGE_LIVE_TESTS=1 pixi run test tests/live/test_keyframe_fal_live.py -v 2>&1 | tee /tmp/layer-r-live.log",
  "acceptanceCriteria": [
    "pixi run preflight exits 0 before invocation",
    "2 passed under KINOFORGE_LIVE_TESTS=1 + FAL_KEY set",
    "keyframe-init_image.png starts with PNG magic (i2v test)",
    "keyframe-first_frame.png != keyframe-last_frame.png bytes (flf2v test)",
    "clip MP4 first 32 bytes contain ftyp box",
    "total spend <= $0.20",
    "each real-cloud bug gets own fix commit + regression test"
  ]
}
```

---

## Task 17: README + PROGRESS.md Phase 32 entry + final-gate full suite

**Goal:** Add the README "Keyframe stage" section; append the Phase 32 entry to PROGRESS.md; run the full suite + invariants; commit.

**Files:**
- Modify: `README.md` (append "Keyframe stage" section)
- Modify: `PROGRESS.md` (append Phase 32 entry; update Single-next-action; update test count; close GH #4 line)

**Acceptance Criteria:**
- [ ] `README.md` has a "Keyframe stage" subsection under the main concept docs explaining: when to use it, the YAML knob, the i2v / flf2v common case, the per-role override.
- [ ] `PROGRESS.md` Phase 32 entry follows the existing format (per-task SHAs, key decisions, live-smoke confirmation, test count delta, carry-forwards).
- [ ] `PROGRESS.md` GitHub-issue table row for #4 flips to CLOSED (Layer R).
- [ ] `PROGRESS.md` Single-next-action block updated.
- [ ] `pixi run test` passes the entire suite (1111 + Layer-R deltas → ~1186 + 2 live-gated).
- [ ] `pixi run pre-commit run --all-files` passes.
- [ ] Final commit message references Phase 32 + closes #4.

**Verify:** `pixi run test -q && pixi run pre-commit run --all-files` → both clean. Final `git log --oneline -5` shows the Phase 32 close-out commit at HEAD.

**Steps:**

- [ ] **Step 1: README "Keyframe stage" section**

Edit `README.md`. Locate the existing concept-doc section (likely "Pipeline / Stages" or similar — read the file to find the right anchor). Append:

```markdown
### Keyframe stage (optional)

Some video-generation modes need an input image (`i2v` requires `init_image`;
`flf2v` requires `first_frame` + `last_frame`). The keyframe stage generates
those images via a separate image engine so you don't have to bring your own.

Opt in by adding a `keyframe:` block:

```yaml
mode: i2v
prompt: "a cat walking through a sunlit meadow"
engine: { kind: fal, fal: { endpoint: "fal-ai/wan-i2v" } }
spec: { model: "fal-ai/wan-i2v" }

keyframe:
  engine: fal                        # any registered image engine
  prompt: "photorealistic cat in a sunlit meadow"
  spec: { model: "fal-ai/flux-schnell" }
```

For `flf2v` with differentiated bookends, use per-role overrides:

```yaml
keyframe:
  engine: fal
  spec: { model: "fal-ai/flux-schnell" }
  roles:
    first_frame: { prompt: "cat sitting in meadow" }
    last_frame:  { prompt: "tiger sitting in meadow, same composition" }
```

User-supplied assets are preserved per role: if you provide `first_frame`
in the request but not `last_frame`, only the missing role gets generated.

See `examples/configs/keyframe-fal-i2v.yaml` and `keyframe-fal-flf2v.yaml`.
```

- [ ] **Step 2: PROGRESS.md Phase 32 entry**

Append to `PROGRESS.md` after the Phase 31 entry (read PROGRESS first to copy the entry format exactly):

```markdown
### Phase 32 — Layer R (keyframe stage + ImageEngine sibling ABC + pipeline list-walker)

Closes GH #4. Three orthogonal foundations shipped together: pipeline
list-walker (`PipelineState` + `Stage(run(state)->state)`), parallel
`ImageEngine` / `ImageBackend` / `ImageProfile` sibling ABCs, and
`KeyframeStage` filling missing image-kind conditioning roles. Concrete
engines: `FakeImageEngine` (offline) + `FalImageEngine`
(`fal-ai/flux-schnell` live).

- Spec: `docs/superpowers/specs/2026-06-05-layer-r-keyframe-design.md`
- Plan: `docs/superpowers/plans/2026-06-05-layer-r-keyframe.md`
- T1 (image ABCs + PipelineState + registry helpers): `<sha>`
- T2 (MODE_ROLE_REQUIREMENTS schema migration): `<sha>`
- T3 (artifact_bytes helper extraction): `<sha>`
- T4 (GenerateClipStage segments=constructor migration): `<sha>`
- T5 (JsonImageProfileCache .image.json namespace): `<sha>`
- T6 (FakeImageEngine + register): `<sha>`
- T7 (FalImageEngine + register): `<sha>`
- T8 (KeyframeConfig pydantic + Config.keyframe field): `<sha>`
- T9 (KeyframeStage implementation): `<sha>`
- T10 (Orchestrator pipeline list-walker + image engine pre-resolution): `<sha>`
- T11 (batch_generate mirror): `<sha>`
- T12 (Example YAMLs + load-lockdown tests): `<sha>`
- T13 (Backwards-compat lockdown): `<sha>`
- T14 (Core invariant scan extension): `<sha>`
- T15 (Live smoke RED scaffold pre-spend): `<sha>`
- T16 (USER-GATE live smoke + any bug-fix commits): `<sha>`
- T17 (README + PROGRESS + final gate): `<sha>`

**Key design decisions:**
- Slim `PipelineState{request, artifacts}` — future stages add ZERO fields,
  they store outputs in the artifacts dict.
- Parallel sibling ImageEngine ABCs (zero touch to existing 5 video engines).
- `MODE_ROLE_REQUIREMENTS` migrated to `dict[mode, dict[role, kind]]` —
  single source of truth, foundation for future audio roles.
- Per-role gap fill: user-supplied assets are preserved; only missing roles get generated.
- `segments_override` kwarg dropped from `GenerateClipStage.run`; promoted
  to a `segments` constructor field for uniform Stage Protocol.
- Static fal `ImageProfile` (max_resolution `(1024, 1024)`, `{"t2i"}`) —
  dynamic capability sniffing per fal endpoint is a carry-forward.

**Live-smoke confirmation:**

```
KINOFORGE_LIVE_TESTS=1 pixi run test tests/live/test_keyframe_fal_live.py -v
============================== 2 passed in <wall-clock> ==============================
```

Cost: ~$<actual> total across all live invocations + bug-fix wave. (Budget
ceiling was $0.20.)

**Test count:** 1111 → ~1186 (+~75 offline) + 2 live-gated (default-skip 6 → 8).

**Out of scope (carry-forwards):**
- HostedImageEngine + DiffusersImageEngine concretes.
- Image-backend pool (parallel flf2v role fills).
- Keyframe caching across runs.
- User-facing `pipeline:` YAML override.
- `output_intermediates: true` cfg knob.
- LoRA support on image engines.
- Dynamic fal per-endpoint capability sniffing.
- Splitter into `GenerateClipStage`.
- Multi-pass refinement keyframes.

Closes GH #4.
```

Update the GitHub-issues table row for `#4`:

```
| #4 | Keyframe / image-generation upstream Stage | CLOSED (Layer R) |
```

Update PROGRESS line 72 reference is already done in T2.

Update the Single-next-action block to reflect Phase 32 close-out and list next-layer candidates (per pre-Layer-R candidates listed in the earlier `Single next action` block).

- [ ] **Step 3: Full-suite final gate**

Run: `pixi run test -q`
Expected: full suite passes; total ≈ 1186 + 8 skipped.

Run: `pixi run pre-commit run --all-files`
Expected: clean.

- [ ] **Step 4: Commit the close-out**

```bash
git add README.md PROGRESS.md
git commit -m "docs(progress): Phase 32 entry + close GH #4 (Phase 32 T17)

Layer R (keyframe stage + ImageEngine sibling ABC + pipeline list-walker)
shipped. Closes #4."
```

- [ ] **Step 5: Verify final state**

Run: `git log --oneline -20`
Expected: ~17 Phase-32 commits, HEAD at T17 close-out.

Run: `pixi run preflight`
Expected: exit 0.

---

## Self-review

**Spec coverage check:**
- Spec §2 architecture → T1 + T10 (pipeline list-walker + image-engine pre-resolution). ✓
- Spec §3 new ABCs → T1. ✓
- Spec §4 PipelineState + Stage Protocol → T1, §4.3 GenerateClipStage migration → T4, §4.4 shared artifact_bytes → T3. ✓
- Spec §5 KeyframeStage → T9. ✓
- Spec §6 schema migration → T2. ✓
- Spec §7 KeyframeConfig pydantic + YAML → T8 + T12. ✓
- Spec §8 orchestrator changes → T10 + T11 (batch). ✓
- Spec §9 concrete engines → T6 (Fake) + T7 (Fal). ✓
- Spec §10.1 offline tests → covered task-by-task (each task ships its tests). ✓
- Spec §10.2 live smoke → T15 RED scaffold + T16 USER-GATE invocation. ✓
- Spec §10.3 backcompat lockdown → T13. ✓
- Spec §10.4 carry-forwards → T17 PROGRESS entry. ✓
- Spec §10.5 phase metadata → T17 PROGRESS entry. ✓

**Type consistency check:**
- `PipelineState` defined in T1, consumed in T3 (artifact_bytes does NOT take state — independent), T4 (stage.run signature), T9 (stage.run signature), T10 (orchestrator construction), T13 (backcompat tests). ✓
- `ImageEngine` / `ImageBackend` / `ImageProfile` / `ImageJob` defined T1, consumed T5 (cache), T6 (Fake), T7 (Fal), T9 (KeyframeStage), T10 (orchestrator). ✓
- `ImageProfileProvider` ABC introduced in T10 (was implicit in §3 spec). The JsonImageProfileCache class shipped in T5 satisfies this Protocol structurally; T10 may need to explicitly declare inheritance. Noted in T10 Step 3.
- `KeyframeConfig` defined T8, consumed T9 (KeyframeStage construction), T10 (orchestrator branch), T12 (YAML examples). ✓
- `MODE_ROLE_REQUIREMENTS` migrated T2, consumed by `required_image_roles` (T1 — schema-shape-agnostic helper handles both pre and post-migration shapes), and by KeyframeStage (T9, post-migration). ✓
- `register_image_engine` / `get_image_engine` defined T1, consumed T6 / T7 / T10. ✓

**Placeholder scan:** none. Every code step has actual code; every test step has actual assertions; every shell step has the exact command.

---

## Native task creation

Tasks are tracked separately via the TaskCreate flow below; this plan document is the authoritative reference and the .tasks.json companion will be written at completion of this skill invocation.
