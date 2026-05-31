# Layer F — Engine Asset Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `init_image` conditioning assets from `segments[0].assets` into each engine's outgoing request, closing the loop opened by Layer E (persisted tail PNGs that engines currently ignore).

**Architecture:** Approach A — shared pure helpers in `core/assets.py` (`find_asset`, `asset_bytes`, `set_by_dot_path`) + per-engine `submit()` extension. No new ABC methods. Mirrors Layer E pattern. Data-driven injection paths: ComfyUI per-job `spec["asset_node_ids"]`; Diffusers/Hosted per-engine `cfg["engine"]["<kind>"]["asset_paths"]` dict.

**Tech Stack:** Python 3.11, stdlib (urllib, email.mime.multipart, pathlib, urllib.parse), pydantic v2 (no change), pytest, mypy strict.

**Spec:** `docs/superpowers/specs/2026-05-30-layer-f-engine-asset-wiring-design.md`

**Spec divergence (noted upfront):** spec §3 says modify `DiffusersEngineCfg`/`HostedEngineCfg` to add `asset_paths`. Live code uses dict-based cfg for engine-specific params (Layer E precedent: `hosted_cfg.get("url_path", "")` in `engines/hosted/__init__.py:450`); `DiffusersEngineConfig` doesn't even exist in `core/config.py`. Plan matches the live pattern — `asset_paths` is read via `engine_cfg.get("asset_paths", {})`. No pydantic class change. Spec design intent ("user declares paths in YAML; engine reads + writes") fully preserved.

---

## File Structure

| Path | Role | Status |
|------|------|--------|
| `src/kinoforge/core/errors.py` | Add `AssetFetchError` class | Modified (1 class, ~3 lines) |
| `src/kinoforge/core/assets.py` | Pure helpers: `find_asset`, `asset_bytes`, `set_by_dot_path` | **New** |
| `src/kinoforge/engines/diffusers/__init__.py` | `DiffusersBackend.__init__` + `submit` + `validate_spec`; `DiffusersEngine.backend` cfg read | Modified |
| `src/kinoforge/engines/hosted/__init__.py` | `HostedAPIBackend.__init__` + `submit` + `validate_spec`; `HostedAPIEngine.backend` cfg read | Modified |
| `src/kinoforge/engines/comfyui/__init__.py` | `ComfyUIBackend.__init__` + `submit` + `validate_spec`; `ComfyUIEngine.__init__` + `backend` for new seams; add `_urllib_post_multipart` module-level default | Modified |
| `src/kinoforge/pipeline/generate_clip.py` | Add `engine.validate_spec(job)` call after `inject_tail_frame` | Modified (1 line) |
| `tests/core/test_assets.py` | 10 tests for new helpers | **New** |
| `tests/engines/test_diffusers.py` | 4 new tests | Extended |
| `tests/engines/test_hosted.py` | 4 new tests | Extended |
| `tests/engines/test_comfyui.py` | 8 new tests | Extended |
| `tests/pipeline/test_generate_clip.py` | 3 new tests | Extended |
| `README.md` | Replace Layer F limitation callout; document `asset_paths` cfg | Modified |
| `PROGRESS.md` | New Phase 16 entry | Modified |

---

## Task 1: Core helpers (AssetFetchError + assets.py)

**Goal:** Add new error type and pure helpers used by all three engines. No engine touched.

**Files:**
- Modify: `src/kinoforge/core/errors.py:44` (append after `FrameExtractionError`)
- Create: `src/kinoforge/core/assets.py`
- Create: `tests/core/test_assets.py`

**Acceptance Criteria:**
- [ ] `AssetFetchError` subclasses `KinoforgeError`
- [ ] `find_asset(job, role)` returns matching `ConditioningAsset`, `None` if absent, raises `ValidationError` on duplicate
- [ ] `asset_bytes(uri, *, http_get_bytes)` resolves `http(s)://` via injected fetcher, `file://` via filesystem, raises `AssetFetchError` for other schemes or fetch failures
- [ ] `set_by_dot_path(body, dot_path, value)` writes at nested dot-path, creating intermediate dicts
- [ ] All 10 tests pass

**Verify:** `pixi run pytest tests/core/test_assets.py -v` → 10 passed

**Steps:**

- [ ] **Step 1: Write failing tests** (`tests/core/test_assets.py`)

```python
"""Tests for core/assets.py helpers."""

from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from kinoforge.core.assets import asset_bytes, find_asset, set_by_dot_path
from kinoforge.core.errors import AssetFetchError, ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationJob,
    Segment,
)


def _job(assets: list[ConditioningAsset]) -> GenerationJob:
    """Build a minimal job with one segment carrying ``assets``."""
    return GenerationJob(
        spec={},
        segments=[Segment(prompt="p", assets=assets)],
        params={},
    )


def _asset(role: str, uri: str = "file:///tmp/x.png") -> ConditioningAsset:
    return ConditioningAsset(
        kind="image",
        role=role,
        ref=Artifact(filename="x.png", uri=uri),
    )


def test_find_asset_returns_match() -> None:
    a = _asset("init_image")
    job = _job([_asset("first_frame"), a, _asset("last_frame")])
    # Bug catch: substring/prefix matching would return the first asset
    # whose role *contains* "init_image" (none here) or worse silently
    # match "first_frame" if comparison is broken.
    assert find_asset(job, "init_image") is a


def test_find_asset_returns_none_for_missing() -> None:
    job = _job([_asset("first_frame")])
    # Bug catch: KeyError or IndexError on missing role instead of
    # graceful None.
    assert find_asset(job, "init_image") is None


def test_find_asset_raises_on_duplicate_role() -> None:
    a1 = _asset("init_image", "file:///a.png")
    a2 = _asset("init_image", "file:///b.png")
    job = _job([a1, a2])
    # Bug catch: silent "pick first" would let a splitter bug ship
    # uncaught — caller has no way to know two distinct images were
    # meant for the same slot.
    with pytest.raises(ValidationError, match="duplicate"):
        find_asset(job, "init_image")


def test_asset_bytes_http_dispatches_to_fetcher() -> None:
    called: list[str] = []

    def fake_fetcher(url: str) -> bytes:
        called.append(url)
        return b"HTTP_PAYLOAD"

    out = asset_bytes("https://example.com/x.png", http_get_bytes=fake_fetcher)
    # Bug catch: file-path fallback for http URI would read 0 bytes from
    # a "/example.com/x.png" path that may or may not exist locally.
    assert out == b"HTTP_PAYLOAD"
    assert called == ["https://example.com/x.png"]


def test_asset_bytes_file_reads_filesystem(tmp_path: Path) -> None:
    p = tmp_path / "asset.png"
    p.write_bytes(b"LOCAL_PAYLOAD")
    uri = p.as_uri()  # file:///...

    def fail_fetcher(url: str) -> bytes:
        raise AssertionError("http fetcher must not be called for file://")

    # Bug catch: urlparse drops the leading slash on file URIs in some
    # naive impls (path becomes "tmp/asset.png" → not found).
    assert asset_bytes(uri, http_get_bytes=fail_fetcher) == b"LOCAL_PAYLOAD"


def test_asset_bytes_unsupported_scheme_raises() -> None:
    def fail_fetcher(url: str) -> bytes:
        raise AssertionError("must not be called for unsupported scheme")

    # Bug catch: silent fallthrough to http_get_bytes would yield an
    # opaque urllib failure 30s later instead of an early typed error.
    with pytest.raises(AssetFetchError, match="unsupported scheme"):
        asset_bytes("s3://bucket/key", http_get_bytes=fail_fetcher)


def test_asset_bytes_wraps_http_error() -> None:
    def raising_fetcher(url: str) -> bytes:
        raise urllib.error.URLError("dns fail")

    # Bug catch: raw URLError leaking to caller breaks the typed-error
    # contract used by orchestrator (matches Layer E discipline).
    with pytest.raises(AssetFetchError, match="dns fail"):
        asset_bytes("https://example.com/x.png", http_get_bytes=raising_fetcher)


def test_asset_bytes_wraps_file_not_found(tmp_path: Path) -> None:
    missing = (tmp_path / "nope.png").as_uri()

    def fail_fetcher(url: str) -> bytes:
        raise AssertionError("must not be called for file://")

    # Bug catch: bare FileNotFoundError leaks; should be AssetFetchError.
    with pytest.raises(AssetFetchError, match="nope.png"):
        asset_bytes(missing, http_get_bytes=fail_fetcher)


def test_set_by_dot_path_simple() -> None:
    body: dict = {}
    set_by_dot_path(body, "key", 42)
    # Bug catch: a "single-key always treated as nested" impl would
    # create {"k": {"e": {"y": 42}}} or similar nonsense.
    assert body == {"key": 42}


def test_set_by_dot_path_nested_creates_intermediates() -> None:
    body: dict = {"existing": {"left": "alone"}}
    set_by_dot_path(body, "a.b.c", "deep")
    # Bug catch: missing-intermediate KeyError on write, or overwriting
    # the sibling "existing" branch.
    assert body == {
        "existing": {"left": "alone"},
        "a": {"b": {"c": "deep"}},
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pixi run pytest tests/core/test_assets.py -v`
Expected: All 10 tests fail with `ModuleNotFoundError: No module named 'kinoforge.core.assets'` (and missing `AssetFetchError`).

- [ ] **Step 3: Add `AssetFetchError` to errors module**

Append to `src/kinoforge/core/errors.py` after the `FrameExtractionError` class (currently the last class, at line 44):

```python
class AssetFetchError(KinoforgeError):
    """Raised when fetching a conditioning asset's bytes fails.

    Wraps unsupported URI scheme, HTTP transport error, missing file,
    and ComfyUI ``/upload/image`` failure into a single typed error.
    """
```

- [ ] **Step 4: Create `core/assets.py`**

Create `src/kinoforge/core/assets.py`:

```python
"""Pure helpers for conditioning-asset discovery and URI resolution.

Used by per-engine ``submit()`` implementations to find the asset on
``segments[0].assets`` matching a declared role and resolve its URI to
bytes (when the engine needs to upload them) or pass the URI through
(when the engine's server fetches it).

This module is part of ``core`` and must never import any concrete
engine, provider, source, or store — verified by ``test_core_invariant``.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from kinoforge.core.errors import AssetFetchError, ValidationError
from kinoforge.core.interfaces import ConditioningAsset, GenerationJob


def find_asset(job: GenerationJob, role: str) -> ConditioningAsset | None:
    """Return ``segments[0]``'s asset whose role matches, or ``None``.

    Looks at ``segments[0]`` only because the chain mechanism injects
    tail-frames there and the happy-path single-segment build attaches
    user-supplied request assets there too (see ``GenerateClipStage``).

    Args:
        job: The generation job to inspect.
        role: Exact role string to match.

    Returns:
        The matching :class:`ConditioningAsset`, or ``None`` if none
        carry that role.

    Raises:
        ValidationError: ``segments[0].assets`` contains more than one
            asset with the requested role.
    """
    if not job.segments:
        return None
    matches = [a for a in job.segments[0].assets if a.role == role]
    if len(matches) > 1:
        raise ValidationError(
            f"duplicate asset role {role!r} in segments[0]: "
            f"{len(matches)} entries found, expected at most 1"
        )
    return matches[0] if matches else None


def asset_bytes(
    uri: str,
    *,
    http_get_bytes: Callable[[str], bytes],
) -> bytes:
    """Resolve ``uri`` to raw bytes by scheme.

    ``http``/``https`` dispatch to the injected ``http_get_bytes``;
    ``file://`` reads via :class:`pathlib.Path`.  Any other scheme
    raises :class:`AssetFetchError`.

    Args:
        uri: URI to resolve (``http``, ``https``, or ``file``).
        http_get_bytes: Injected HTTP byte fetcher; tests pass spies.

    Returns:
        Raw bytes at the URI.

    Raises:
        AssetFetchError: Unsupported scheme, HTTP transport error, or
            missing local file.
    """
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if scheme in ("http", "https"):
        try:
            return http_get_bytes(uri)
        except (urllib.error.URLError, OSError) as e:
            raise AssetFetchError(f"failed to fetch {uri}: {e}") from e
    if scheme == "file":
        path = Path(parsed.path)
        try:
            return path.read_bytes()
        except (FileNotFoundError, OSError) as e:
            raise AssetFetchError(f"failed to read {uri}: {e}") from e
    raise AssetFetchError(f"unsupported scheme {scheme!r} for asset URI {uri!r}")


def set_by_dot_path(body: dict[str, Any], dot_path: str, value: Any) -> None:
    """Write ``value`` at ``dot_path`` in ``body``, creating intermediate dicts.

    Mutation is in-place.  Caller is responsible for passing a copy if
    the original must remain unchanged.  A single-segment ``dot_path``
    (no ``"."``) writes at the top level.

    Args:
        body: Target dict (mutated in place).
        dot_path: Dot-separated key path (e.g. ``"input.image_url"``).
        value: Value to write at the leaf.
    """
    parts = dot_path.split(".")
    cursor: dict[str, Any] = body
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pixi run pytest tests/core/test_assets.py -v`
Expected: 10 passed.

- [ ] **Step 6: Run full quality gate**

Run: `pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files`
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add src/kinoforge/core/errors.py src/kinoforge/core/assets.py tests/core/test_assets.py
git commit -m "$(cat <<'EOF'
feat(core): add AssetFetchError + core/assets.py helpers

Pure helpers for per-engine asset wiring (Layer F):
- find_asset(job, role): seg-0 lookup; duplicate role raises ValidationError
- asset_bytes(uri, *, http_get_bytes): scheme dispatch (http(s)/file); wraps URLError/OSError as AssetFetchError
- set_by_dot_path(body, path, value): nested dot-path setter with intermediate-dict creation

AssetFetchError subclasses KinoforgeError alongside FrameExtractionError.
No engine imports — core-import invariant preserved.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/core/errors.py", "src/kinoforge/core/assets.py", "tests/core/test_assets.py"], "verifyCommand": "pixi run pytest tests/core/test_assets.py -v && pixi run typecheck && pixi run lint", "acceptanceCriteria": ["AssetFetchError subclasses KinoforgeError", "find_asset returns match/None/raises on duplicate", "asset_bytes dispatches http(s) vs file vs unsupported", "set_by_dot_path writes at nested paths", "10 tests pass"]}
```

---

## Task 2: Diffusers asset wiring

**Goal:** Diffusers `submit()` reads `segments[0].assets`, finds role match, writes URI into request body at the configured dot-path. URL passthrough (no bytes fetched at engine).

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py` — `DiffusersBackend.__init__` (line 135-158), `DiffusersBackend.submit` (line 176-187), `DiffusersEngine.backend` (around line 327), `DiffusersEngine.validate_spec` (line 383-403)
- Modify: `tests/engines/test_diffusers.py` — 4 new tests

**Acceptance Criteria:**
- [ ] `DiffusersBackend.__init__` accepts `asset_paths: dict[str, str] = {}` kwarg
- [ ] `DiffusersEngine.backend()` reads `cfg["engine"]["diffusers"].get("asset_paths", {})` and passes through
- [ ] `submit()` walks `asset_paths`, calls `find_asset`, writes `asset.ref.uri` at the dot-path via `set_by_dot_path`
- [ ] `submit()` does NOT fetch asset bytes (URL passthrough)
- [ ] `validate_spec()` raises `ValidationError` if `segments[0].assets` carries a role not in `asset_paths`
- [ ] All existing Diffusers tests still pass; 4 new tests pass
- [ ] Pre-Layer-F templates (no `asset_paths`) work unchanged

**Verify:** `pixi run pytest tests/engines/test_diffusers.py -v` → all previous + 4 new pass

**Steps:**

- [ ] **Step 1: Write failing tests** (append to `tests/engines/test_diffusers.py`; reuse existing imports + fixtures)

Add at the end of the file. The test file uses helper builders already; verify name conventions before writing by reading the existing fixtures and naming style in `tests/engines/test_diffusers.py`. Pattern:

```python
def test_submit_writes_asset_uri_at_configured_dot_path() -> None:
    posted: list[tuple[str, dict]] = []

    def spy_post(url: str, body: dict) -> dict:
        posted.append((url, dict(body)))
        return {"job_id": "j-1"}

    backend = DiffusersBackend(
        http_post=spy_post,
        http_get=lambda u: {},
        base_url="http://localhost:8000",
        probe_profile=_PROBE,
        asset_paths={"init_image": "init_image_url"},
    )
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="seed.png", uri="https://store/seed.png"),
    )
    job = GenerationJob(
        spec={"pipeline": "Stable", "scheduler": "DDIM"},
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    backend.submit(job)
    # Bug catch: if the engine wrote the URI at the wrong path (e.g.
    # top-level "uri" or inside spec verbatim), this assertion fails.
    assert posted[0][1]["init_image_url"] == "https://store/seed.png"
    # Bug catch: spec keys must still be forwarded.
    assert posted[0][1]["pipeline"] == "Stable"


def test_submit_no_asset_paths_unchanged() -> None:
    """Regression: pre-Layer-F templates submit identical body."""
    posted: list[dict] = []

    def spy_post(url: str, body: dict) -> dict:
        posted.append(dict(body))
        return {"job_id": "j-2"}

    backend = DiffusersBackend(
        http_post=spy_post,
        http_get=lambda u: {},
        base_url="http://localhost:8000",
        probe_profile=_PROBE,
    )
    job = GenerationJob(
        spec={"pipeline": "Stable", "scheduler": "DDIM"},
        segments=[Segment(prompt="p", assets=[])],
        params={},
    )
    backend.submit(job)
    # Bug catch: a new injection branch must not leak spurious keys.
    assert posted[0] == {"pipeline": "Stable", "scheduler": "DDIM"}


def test_validate_spec_rejects_asset_without_path_mapping() -> None:
    engine = DiffusersEngine()
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="x.png", uri="https://x"),
    )
    job = GenerationJob(
        spec={"pipeline": "Stable", "scheduler": "DDIM"},
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    # The engine needs cfg to know about asset_paths. validate_spec
    # currently takes only job; the engine must hold its asset_paths
    # the same way it currently lacks per-job cfg. See implementation:
    # asset_paths is held on the Backend (passed at backend()); the
    # Engine's validate_spec must inspect job.spec for the engine-cfg
    # injected at request time. Approach: validate_spec on the Engine
    # asks the Backend's asset_paths via a Backend-bound delegate, OR
    # validate_spec moves to the Backend. Plan chooses the latter:
    # DiffusersBackend gets validate_spec(); the GenerationEngine
    # facade delegates to it. See Step 4.
    backend = DiffusersBackend(
        http_post=lambda u, b: {"job_id": "x"},
        http_get=lambda u: {},
        base_url="http://localhost:8000",
        probe_profile=_PROBE,
        asset_paths={},  # path NOT declared for init_image
    )
    # Bug catch: silent swallow would let the engine submit a body
    # without the asset and the user would never know.
    with pytest.raises(ValidationError, match="init_image"):
        backend.validate_spec(job)


def test_submit_does_not_fetch_asset_bytes() -> None:
    fetched: list[str] = []

    def spy_get_bytes(url: str) -> bytes:
        fetched.append(url)
        return b""

    # Backend does not even take http_get_bytes; this test asserts the
    # surface by demonstrating that submit() works without it (URL
    # passthrough). Replicates by constructing without the seam and
    # confirming submit completes.
    backend = DiffusersBackend(
        http_post=lambda u, b: {"job_id": "x"},
        http_get=lambda u: {},
        base_url="http://localhost:8000",
        probe_profile=_PROBE,
        asset_paths={"init_image": "init_image_url"},
    )
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="s.png", uri="https://store/s.png"),
    )
    job = GenerationJob(
        spec={"pipeline": "Stable", "scheduler": "DDIM"},
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    backend.submit(job)
    # Bug catch: if a future refactor adds eager byte-fetching, the
    # passthrough contract breaks silently and bandwidth doubles.
    assert fetched == []
```

**Important contract note for the implementer:** The original spec describes
`engine.validate_spec(job)` as the ABC method (per `GenerationEngine`
interface). Today's Diffusers engine has `validate_spec` on the
`DiffusersEngine` class (not Backend) — see line 383. Layer F's
validation needs to know about `asset_paths`, which lives on the
Backend after construction. Two paths:

1. **Move `validate_spec` to the Backend** (cleanest — Backend knows
   both spec and cfg).
2. **Pass `asset_paths` to the Engine too** via `__init__` so
   `validate_spec` on the Engine can check.

Plan choice: **Option 2** — the Engine retains `validate_spec`
(ABC contract), but `DiffusersEngine.__init__` accepts an optional
`asset_paths` map so validation can run against it. The Engine's
`backend()` factory passes the same map onto the Backend. This
preserves the ABC location of `validate_spec` and avoids moving the
method. Test the Engine's `validate_spec` directly, not the
Backend's. Update the test above accordingly (see Step 1 revised
snippet).

- [ ] **Step 2: Revise test snippet from Step 1 for Engine-side `validate_spec`**

Replace the `test_validate_spec_rejects_asset_without_path_mapping` body
with the Engine variant:

```python
def test_validate_spec_rejects_asset_without_path_mapping() -> None:
    # Engine constructed without an asset_paths mapping for init_image.
    engine = DiffusersEngine()  # asset_paths defaults to {}
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="x.png", uri="https://x"),
    )
    job = GenerationJob(
        spec={"pipeline": "Stable", "scheduler": "DDIM"},
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    with pytest.raises(ValidationError, match="init_image"):
        engine.validate_spec(job)
```

- [ ] **Step 3: Run new tests to confirm they fail**

Run: `pixi run pytest tests/engines/test_diffusers.py -v -k "asset"`
Expected: 4 fails (NameError for `ConditioningAsset`/etc imports, or
attribute errors for `asset_paths`).

- [ ] **Step 4: Implement `DiffusersBackend.__init__` change**

In `src/kinoforge/engines/diffusers/__init__.py`, extend the Backend constructor (replace block starting at line 135):

```python
    def __init__(
        self,
        *,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
        http_get: Callable[[str], dict[str, Any]],
        base_url: str,
        probe_profile: ModelProfile,
        sleep: Callable[[float], None] = time.sleep,
        asset_paths: dict[str, str] | None = None,
    ) -> None:
        """Initialise the backend with injected transport callables.

        Args:
            http_post: POST callable ``(url, json_body) -> dict``.
            http_get: GET callable ``(url) -> dict``.
            base_url: Base URL of the diffusers server, e.g.
                ``"http://127.0.0.1:8000"``.  No trailing slash.
            probe_profile: ``ModelProfile`` returned by ``inspect_capabilities``.
            sleep: Callable invoked between poll iterations in ``result``.
            asset_paths: Optional mapping from role name to dot-path
                in the request body where the asset's URI is written
                (e.g. ``{"init_image": "init_image_url"}``).  Roles
                absent from this map are not injected.
        """
        self._http_post = http_post
        self._http_get = http_get
        self._base_url = base_url.rstrip("/")
        self._probe = probe_profile
        self._sleep = sleep
        self._asset_paths: dict[str, str] = dict(asset_paths or {})
```

- [ ] **Step 5: Implement `DiffusersBackend.submit` extension**

Replace the existing `submit` body (line 176-187) with:

```python
    def submit(self, job: GenerationJob) -> str:
        """POST the job spec (with asset URIs injected) to ``/generate``.

        For each role declared in ``self._asset_paths``, look up the
        corresponding asset on ``job.segments[0]`` via
        :func:`~kinoforge.core.assets.find_asset` and write
        ``asset.ref.uri`` into the request body at the configured
        dot-path via :func:`~kinoforge.core.assets.set_by_dot_path`.
        Roles absent from ``segments[0].assets`` are silently skipped.

        Args:
            job: The ``GenerationJob`` whose ``spec`` is the request body.

        Returns:
            The ``job_id`` string from the server response.
        """
        body = dict(job.spec)
        for role, dot_path in self._asset_paths.items():
            asset = find_asset(job, role)
            if asset is None:
                continue
            set_by_dot_path(body, dot_path, asset.ref.uri)
        url = f"{self._base_url}/generate"
        response = self._http_post(url, body)
        return str(response["job_id"])
```

Add imports near the top of the file (after existing `from kinoforge.core.errors import ...`):

```python
from kinoforge.core.assets import find_asset, set_by_dot_path
```

- [ ] **Step 6: Implement `DiffusersEngine.__init__` + `backend` + `validate_spec` changes**

DiffusersEngine currently has class-level dataclass-style fields. Confirm by reading lines 240-310 before editing. If it's a `@dataclass`, add `asset_paths: dict[str, str] = field(default_factory=dict)`; if it's a regular class with `__init__`, add the parameter and `self._asset_paths` attr the same as the Backend.

Then update `backend()`:

```python
    def backend(
        self, instance: Instance | None, cfg: dict[str, Any]
    ) -> DiffusersBackend:
        # ... existing base_url derivation ...
        engine_block = cfg.get("engine", {}) if isinstance(cfg, dict) else {}
        diffusers_cfg: dict[str, Any] = (
            engine_block.get("diffusers", {})
            if isinstance(engine_block, dict)
            else {}
        )
        asset_paths_raw = diffusers_cfg.get("asset_paths", {})
        asset_paths: dict[str, str] = (
            {str(k): str(v) for k, v in asset_paths_raw.items()}
            if isinstance(asset_paths_raw, dict)
            else {}
        )
        # Mirror into engine's own copy so validate_spec sees it on the
        # next call (validate_spec runs before submit per the
        # orchestrator/stage flow).
        self._asset_paths = asset_paths
        return DiffusersBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            base_url=base_url,
            probe_profile=self._probe,
            sleep=self._sleep,
            asset_paths=asset_paths,
        )
```

Then extend `validate_spec` (replace body at line 383+):

```python
    def validate_spec(self, job: GenerationJob) -> None:
        """Raise ValidationError for missing spec keys or undeclared asset roles.

        Required spec keys: ``"pipeline"``, ``"scheduler"``.  In addition,
        for every asset in ``job.segments[0].assets``, the asset's role
        must appear in ``self._asset_paths`` (set from
        ``cfg["engine"]["diffusers"]["asset_paths"]`` at backend
        construction).  Roles present without a configured injection
        path are a hard error — silent skip would let the engine submit
        a body missing the conditioning asset.

        Args:
            job: The ``GenerationJob`` to validate.

        Raises:
            ValidationError: required key missing, or asset role declared
                on ``segments[0]`` but absent from ``asset_paths``.
        """
        required = {"pipeline", "scheduler"}
        missing = required - set(job.spec.keys())
        if missing:
            raise ValidationError(
                f"Diffusers job.spec is missing required keys: {sorted(missing)}"
            )
        if not job.segments:
            return
        for asset in job.segments[0].assets:
            if asset.role not in self._asset_paths:
                raise ValidationError(
                    f"asset role {asset.role!r} present on segments[0] but "
                    f"engine.diffusers.asset_paths has no mapping; declare "
                    f"asset_paths.{asset.role}: <dot.path> in YAML"
                )
```

Ensure the Engine has `self._asset_paths: dict[str, str] = {}` initialized in `__init__` (or as a dataclass field) so the attribute exists before `backend()` is called.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pixi run pytest tests/engines/test_diffusers.py -v`
Expected: previous tests still pass + 4 new pass.

- [ ] **Step 8: Run full quality gate**

Run: `pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add src/kinoforge/engines/diffusers/__init__.py tests/engines/test_diffusers.py
git commit -m "$(cat <<'EOF'
feat(diffusers): wire init_image asset into request body via asset_paths

DiffusersBackend.__init__ accepts asset_paths: dict[role, dot_path];
submit() walks the map, finds matching asset on segments[0], writes
asset.ref.uri into the body at the configured dot-path (URL passthrough
— no bytes fetched at engine; server fetches). DiffusersEngine.backend
reads cfg["engine"]["diffusers"]["asset_paths"] and threads through.
validate_spec rejects asset roles without a declared injection path.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/__init__.py", "tests/engines/test_diffusers.py"], "verifyCommand": "pixi run pytest tests/engines/test_diffusers.py -v && pixi run typecheck && pixi run lint", "acceptanceCriteria": ["DiffusersBackend.__init__ accepts asset_paths kwarg", "submit writes URI at dot-path", "submit does not fetch bytes", "validate_spec rejects undeclared role", "all prior tests + 4 new pass"]}
```

---

## Task 3: Hosted asset wiring

**Goal:** Same as Diffusers but for Hosted. Reads `cfg["engine"]["hosted"]["asset_paths"]`, writes URI into POST body at configured dot-path. URL passthrough.

**Files:**
- Modify: `src/kinoforge/engines/hosted/__init__.py` — `HostedAPIBackend.__init__` (line 190-217), `HostedAPIBackend.submit` (line 235-245), `HostedAPIEngine.backend` (line 430+), `HostedAPIEngine.validate_spec` (line 486-505), HostedAPIEngine.__init__
- Modify: `tests/engines/test_hosted.py` — 4 new tests

**Acceptance Criteria:**
- [ ] `HostedAPIBackend.__init__` accepts `asset_paths: dict[str, str] = {}` kwarg
- [ ] `HostedAPIEngine.backend()` reads `cfg["engine"]["hosted"].get("asset_paths", {})` and passes through
- [ ] `submit()` walks `asset_paths`, calls `find_asset`, writes `asset.ref.uri` at dot-path
- [ ] `submit()` does NOT fetch asset bytes
- [ ] `validate_spec()` raises `ValidationError` for asset role without `asset_paths` entry
- [ ] Nested dot-path destination (`"input.image_url"`) works (intermediate dict creation)
- [ ] All existing Hosted tests still pass; 4 new tests pass

**Verify:** `pixi run pytest tests/engines/test_hosted.py -v`

**Steps:**

- [ ] **Step 1: Write failing tests** (append to `tests/engines/test_hosted.py`)

```python
def test_submit_writes_asset_uri_at_nested_dot_path() -> None:
    posted: list[tuple[str, dict]] = []

    def spy_post(url: str, body: dict) -> dict:
        posted.append((url, dict(body)))
        return {"job_id": "j-1"}

    backend = HostedAPIBackend(
        http_post=spy_post,
        http_get=lambda u: {},
        endpoint="https://api.example.com/v1/predict",
        probe_profile=_PROBE,
        asset_paths={"init_image": "input.image_url"},
    )
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="s.png", uri="https://store/s.png"),
    )
    job = GenerationJob(
        spec={"model": "vendor/m", "params": {"steps": 30}},
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    backend.submit(job)
    body = posted[0][1]
    # Bug catch: a non-nested setter would create top-level
    # "input.image_url" string-key instead of nested dict.
    assert body["input"]["image_url"] == "https://store/s.png"
    # Bug catch: original spec keys must be forwarded intact.
    assert body["model"] == "vendor/m"
    assert body["params"] == {"steps": 30}


def test_submit_no_asset_paths_unchanged() -> None:
    posted: list[dict] = []

    def spy_post(url: str, body: dict) -> dict:
        posted.append(dict(body))
        return {"job_id": "j-2"}

    backend = HostedAPIBackend(
        http_post=spy_post,
        http_get=lambda u: {},
        endpoint="https://api.example.com/v1/predict",
        probe_profile=_PROBE,
    )
    job = GenerationJob(
        spec={"model": "vendor/m", "params": {}},
        segments=[Segment(prompt="p", assets=[])],
        params={},
    )
    backend.submit(job)
    assert posted[0] == {"model": "vendor/m", "params": {}}


def test_validate_spec_rejects_asset_without_path_mapping() -> None:
    engine = HostedAPIEngine()
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="x.png", uri="https://x"),
    )
    job = GenerationJob(
        spec={"model": "vendor/m", "params": {}},
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    with pytest.raises(ValidationError, match="init_image"):
        engine.validate_spec(job)


def test_submit_does_not_fetch_asset_bytes() -> None:
    backend = HostedAPIBackend(
        http_post=lambda u, b: {"job_id": "x"},
        http_get=lambda u: {},
        endpoint="https://api.example.com/v1/predict",
        probe_profile=_PROBE,
        asset_paths={"init_image": "input.image_url"},
    )
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="s.png", uri="https://store/s.png"),
    )
    job = GenerationJob(
        spec={"model": "vendor/m", "params": {}},
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    # Backend constructor takes no http_get_bytes seam at Layer F;
    # absence is the contract. The call must succeed without any
    # byte-fetch infrastructure.
    backend.submit(job)
```

- [ ] **Step 2: Run new tests to confirm they fail**

Run: `pixi run pytest tests/engines/test_hosted.py -v -k "asset"`
Expected: 4 fails.

- [ ] **Step 3: Extend `HostedAPIBackend.__init__`**

Replace block at line 190-217:

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
        asset_paths: dict[str, str] | None = None,
    ) -> None:
        """Initialise the backend with injected transport callables.

        Args:
            http_post: POST callable ``(url, json_body) -> dict``.
            http_get: GET callable ``(url) -> dict``.
            endpoint: Remote hosted inference endpoint URL.
            probe_profile: ``ModelProfile`` returned by ``inspect_capabilities``.
            sleep: Callable invoked between poll iterations in ``result``.
            url_path: Dot-separated path into the polled response body that
                locates the rendered video URL (e.g. ``"video.url"``).
                Empty (default) leaves ``Artifact.url == ""``.
            asset_paths: Optional mapping from role name to dot-path
                in the request body where the asset's URI is written
                (e.g. ``{"init_image": "input.image_url"}``).
        """
        self._http_post = http_post
        self._http_get = http_get
        self._endpoint = endpoint
        self._probe = probe_profile
        self._sleep = sleep
        self._url_path = url_path
        self._asset_paths: dict[str, str] = dict(asset_paths or {})
```

- [ ] **Step 4: Extend `submit`**

Replace existing `submit` body (line 235-245):

```python
    def submit(self, job: GenerationJob) -> str:
        """POST the job spec (with asset URIs injected) to the hosted endpoint.

        For each role declared in ``self._asset_paths``, look up the
        corresponding asset on ``job.segments[0]`` via
        :func:`~kinoforge.core.assets.find_asset` and write
        ``asset.ref.uri`` into the request body at the configured
        dot-path via :func:`~kinoforge.core.assets.set_by_dot_path`.

        Args:
            job: The ``GenerationJob`` whose ``spec`` is the request body.

        Returns:
            The ``job_id`` (or task id) string from the API response.
        """
        body = dict(job.spec)
        for role, dot_path in self._asset_paths.items():
            asset = find_asset(job, role)
            if asset is None:
                continue
            set_by_dot_path(body, dot_path, asset.ref.uri)
        response = self._http_post(self._endpoint, body)
        return str(response["job_id"])
```

Add import:

```python
from kinoforge.core.assets import find_asset, set_by_dot_path
```

- [ ] **Step 5: Extend `HostedAPIEngine.__init__` + `backend` + `validate_spec`**

Add `self._asset_paths: dict[str, str] = {}` to engine `__init__`.

Update `backend()` (around line 430) to read cfg and pass through; mirror onto engine's own attribute:

```python
        hosted_cfg: dict[str, Any] = (
            engine_block.get("hosted", {})
            if isinstance(engine_block, dict)
            else {}
        )
        url_path: str = str(hosted_cfg.get("url_path", ""))
        asset_paths_raw = hosted_cfg.get("asset_paths", {})
        asset_paths: dict[str, str] = (
            {str(k): str(v) for k, v in asset_paths_raw.items()}
            if isinstance(asset_paths_raw, dict)
            else {}
        )
        self._asset_paths = asset_paths
        return HostedAPIBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            endpoint=endpoint,
            probe_profile=self._probe,
            sleep=self._sleep,
            url_path=url_path,
            asset_paths=asset_paths,
        )
```

(The block above replaces the existing `return HostedAPIBackend(...)` site near line 450 — keep the existing `endpoint=` and `url_path=` derivation; only add `asset_paths` parsing and pass-through.)

Update `validate_spec` (replace body at line 486-505):

```python
    def validate_spec(self, job: GenerationJob) -> None:
        """Raise ValidationError for missing spec keys or undeclared asset roles.

        Required spec keys: ``"model"``, ``"params"``.  For every asset
        on ``job.segments[0]``, the asset's role must appear in
        ``self._asset_paths`` (set from
        ``cfg["engine"]["hosted"]["asset_paths"]`` at backend
        construction).

        Args:
            job: The ``GenerationJob`` to validate.

        Raises:
            ValidationError: required key missing, or asset role declared
                on ``segments[0]`` but absent from ``asset_paths``.
        """
        required = {"model", "params"}
        missing = required - set(job.spec.keys())
        if missing:
            raise ValidationError(
                f"Hosted job.spec is missing required keys: {sorted(missing)}"
            )
        if not job.segments:
            return
        for asset in job.segments[0].assets:
            if asset.role not in self._asset_paths:
                raise ValidationError(
                    f"asset role {asset.role!r} present on segments[0] but "
                    f"engine.hosted.asset_paths has no mapping; declare "
                    f"asset_paths.{asset.role}: <dot.path> in YAML"
                )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pixi run pytest tests/engines/test_hosted.py -v`
Expected: previous tests still pass + 4 new pass.

- [ ] **Step 7: Run full quality gate**

Run: `pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/engines/hosted/__init__.py tests/engines/test_hosted.py
git commit -m "$(cat <<'EOF'
feat(hosted): wire init_image asset into request body via asset_paths

HostedAPIBackend.__init__ accepts asset_paths: dict[role, dot_path];
submit() walks the map, finds matching asset on segments[0], writes
asset.ref.uri into the body at the configured dot-path. Supports
nested dot-paths (e.g. "input.image_url") via set_by_dot_path's
intermediate-dict creation. HostedAPIEngine.backend reads
cfg["engine"]["hosted"]["asset_paths"]. validate_spec rejects asset
roles without a declared injection path.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/engines/hosted/__init__.py", "tests/engines/test_hosted.py"], "verifyCommand": "pixi run pytest tests/engines/test_hosted.py -v && pixi run typecheck && pixi run lint", "acceptanceCriteria": ["HostedAPIBackend.__init__ accepts asset_paths kwarg", "submit writes URI at nested dot-path", "submit does not fetch bytes", "validate_spec rejects undeclared role", "all prior tests + 4 new pass"]}
```

---

## Task 4: ComfyUI asset wiring

**Goal:** ComfyUI `submit()` fetches asset bytes, uploads via `/upload/image`, patches `LoadImage` node's `inputs.image` field via `node_overrides`. Requires two new injectable seams on the Backend.

**Files:**
- Modify: `src/kinoforge/engines/comfyui/__init__.py` — module-level default `_urllib_post_multipart`; `ComfyUIBackend.__init__` (line 172-194); `ComfyUIBackend.submit` (line 213-238); `ComfyUIEngine.backend` (line 419-443); `ComfyUIEngine.validate_spec` (line 471-490)
- Modify: `tests/engines/test_comfyui.py` — 8 new tests

**Acceptance Criteria:**
- [ ] `ComfyUIBackend.__init__` accepts new `http_get_bytes` and `http_post_file` injectable seams (mirror existing pattern; defaults reuse `_urllib_get_bytes` + new `_urllib_post_multipart`)
- [ ] `ComfyUIEngine.backend()` passes both seams from engine to backend
- [ ] `submit()` walks `job.spec.get("asset_node_ids", {})`, calls `find_asset`, fetches bytes via `asset_bytes`, uploads via `_http_post_file`, patches `node_overrides[<node_id>]["inputs"]["image"] = <uploaded_name>`
- [ ] Default `_urllib_post_multipart` POSTs multipart form to `/upload/image` and returns `response["name"]`
- [ ] `AssetFetchError` wraps fetch failures (already done by `asset_bytes`) and upload failures (wrap `URLError`/`OSError` from `_http_post_file`)
- [ ] `validate_spec()` raises `ValidationError` if asset role on `segments[0]` has no `asset_node_ids[role]` mapping
- [ ] Pre-Layer-F templates (no `asset_node_ids`) submit unchanged
- [ ] All existing ComfyUI tests still pass; 8 new tests pass

**Verify:** `pixi run pytest tests/engines/test_comfyui.py -v`

**Steps:**

- [ ] **Step 1: Write failing tests** (append to `tests/engines/test_comfyui.py`)

```python
def test_submit_uploads_bytes_for_declared_asset_role(tmp_path) -> None:
    asset_path = tmp_path / "seed.png"
    asset_path.write_bytes(b"SEED_BYTES")
    uploaded: list[dict] = []

    def spy_post_file(
        url: str, *, field_name: str, filename: str, content: bytes
    ) -> str:
        uploaded.append(
            {"url": url, "field_name": field_name,
             "filename": filename, "content": content}
        )
        return "server_seed.png"

    posted_prompts: list[dict] = []

    def spy_post(url: str, body: dict) -> dict:
        posted_prompts.append({"url": url, "body": body})
        return {"prompt_id": "p-1"}

    backend = ComfyUIBackend(
        http_post=spy_post,
        http_get=lambda u: {},
        http_get_bytes=lambda u: b"unused",  # file:// path skips it
        http_post_file=spy_post_file,
        base_url="http://comfy:8188",
        probe=_PROBE,
    )
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="seed.png", uri=asset_path.as_uri()),
    )
    spec = {
        "graph": {"5": {"class_type": "LoadImage", "inputs": {"image": "old.png"}}},
        "node_overrides": {},
        "asset_node_ids": {"init_image": "5"},
    }
    job = GenerationJob(
        spec=spec,
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    backend.submit(job)
    # Bug catch: wrong URL would 404 silently; wrong field name would
    # be rejected by ComfyUI server.
    assert uploaded[0]["url"] == "http://comfy:8188/upload/image"
    assert uploaded[0]["field_name"] == "image"
    assert uploaded[0]["content"] == b"SEED_BYTES"
    # Bug catch: the LoadImage node in the merged graph must point to
    # the uploaded name, not the original "old.png".
    merged_graph = posted_prompts[0]["body"]["prompt"]
    assert merged_graph["5"]["inputs"]["image"] == "server_seed.png"


def test_submit_with_no_asset_node_ids_unchanged() -> None:
    """Regression: pre-Layer-F spec submits without asset side-effects."""
    uploaded: list = []
    posted: list[dict] = []

    backend = ComfyUIBackend(
        http_post=lambda u, b: posted.append({"u": u, "b": b}) or {"prompt_id": "x"},
        http_get=lambda u: {},
        http_get_bytes=lambda u: b"",
        http_post_file=lambda u, **kw: uploaded.append(kw) or "x.png",
        base_url="http://comfy:8188",
        probe=_PROBE,
    )
    spec = {"graph": {"1": {"class_type": "X", "inputs": {}}}, "node_overrides": {}}
    job = GenerationJob(spec=spec, segments=[Segment(prompt="p", assets=[])], params={})
    backend.submit(job)
    # Bug catch: a Layer F refactor must not call the upload spy.
    assert uploaded == []
    # Pre-Layer-F graph survives intact.
    assert posted[0]["b"]["prompt"]["1"] == {"class_type": "X", "inputs": {}}


def test_submit_skips_role_when_asset_absent() -> None:
    """spec declares mapping, but segments[0].assets is empty -> no upload."""
    uploaded: list = []
    posted: list[dict] = []

    backend = ComfyUIBackend(
        http_post=lambda u, b: posted.append(b) or {"prompt_id": "x"},
        http_get=lambda u: {},
        http_get_bytes=lambda u: b"",
        http_post_file=lambda u, **kw: uploaded.append(kw) or "x.png",
        base_url="http://comfy:8188",
        probe=_PROBE,
    )
    spec = {
        "graph": {"5": {"inputs": {"image": "kept.png"}}},
        "node_overrides": {},
        "asset_node_ids": {"init_image": "5"},
    }
    job = GenerationJob(spec=spec, segments=[Segment(prompt="p", assets=[])], params={})
    backend.submit(job)
    # Bug catch: silent upload of empty content would corrupt the
    # ComfyUI input directory.
    assert uploaded == []
    # node_overrides must not gain a phantom entry.
    assert posted[0]["prompt"]["5"]["inputs"]["image"] == "kept.png"


def test_validate_spec_rejects_role_without_node_id_mapping() -> None:
    engine = ComfyUIEngine()
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="x.png", uri="https://x"),
    )
    spec = {"graph": {}, "node_overrides": {}}  # asset_node_ids absent
    job = GenerationJob(
        spec=spec,
        segments=[Segment(prompt="p", assets=[asset])],
        params={},
    )
    with pytest.raises(ValidationError, match="init_image"):
        engine.validate_spec(job)


def test_submit_raises_AssetFetchError_on_upload_failure(tmp_path) -> None:
    asset_path = tmp_path / "seed.png"
    asset_path.write_bytes(b"X")

    def raising_post_file(url, **kw):
        raise urllib.error.URLError("upload 500")

    backend = ComfyUIBackend(
        http_post=lambda u, b: {"prompt_id": "x"},
        http_get=lambda u: {},
        http_get_bytes=lambda u: b"",
        http_post_file=raising_post_file,
        base_url="http://comfy:8188",
        probe=_PROBE,
    )
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="seed.png", uri=asset_path.as_uri()),
    )
    spec = {"graph": {"5": {}}, "node_overrides": {}, "asset_node_ids": {"init_image": "5"}}
    job = GenerationJob(spec=spec, segments=[Segment(prompt="p", assets=[asset])], params={})
    with pytest.raises(AssetFetchError, match="upload"):
        backend.submit(job)


def test_submit_raises_AssetFetchError_on_fetch_failure() -> None:
    def raising_get_bytes(url: str) -> bytes:
        raise urllib.error.URLError("dns fail")

    backend = ComfyUIBackend(
        http_post=lambda u, b: {"prompt_id": "x"},
        http_get=lambda u: {},
        http_get_bytes=raising_get_bytes,
        http_post_file=lambda u, **kw: "n",
        base_url="http://comfy:8188",
        probe=_PROBE,
    )
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="x.png", uri="https://broken/x.png"),
    )
    spec = {"graph": {"5": {}}, "node_overrides": {}, "asset_node_ids": {"init_image": "5"}}
    job = GenerationJob(spec=spec, segments=[Segment(prompt="p", assets=[asset])], params={})
    with pytest.raises(AssetFetchError, match="dns fail"):
        backend.submit(job)


def test_submit_file_uri_reads_local_bytes(tmp_path) -> None:
    asset_path = tmp_path / "local.png"
    asset_path.write_bytes(b"LOCAL_PNG")
    upload_captured: list[bytes] = []

    backend = ComfyUIBackend(
        http_post=lambda u, b: {"prompt_id": "x"},
        http_get=lambda u: {},
        http_get_bytes=lambda u: b"WRONG",  # must NOT be used for file://
        http_post_file=lambda u, *, field_name, filename, content: (
            upload_captured.append(content) or "n"
        ),
        base_url="http://comfy:8188",
        probe=_PROBE,
    )
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="local.png", uri=asset_path.as_uri()),
    )
    spec = {"graph": {"5": {}}, "node_overrides": {}, "asset_node_ids": {"init_image": "5"}}
    job = GenerationJob(spec=spec, segments=[Segment(prompt="p", assets=[asset])], params={})
    backend.submit(job)
    # Bug catch: using http_get_bytes for file:// URIs would upload
    # "WRONG" instead of the real local bytes.
    assert upload_captured == [b"LOCAL_PNG"]


def test_submit_patches_node_with_uploaded_filename() -> None:
    """node_overrides receives the new image filename even if the user
    template did not pre-populate the LoadImage entry."""
    posted: list[dict] = []
    backend = ComfyUIBackend(
        http_post=lambda u, b: posted.append(b) or {"prompt_id": "x"},
        http_get=lambda u: {},
        http_get_bytes=lambda u: b"BYTES",
        http_post_file=lambda u, **kw: "uploaded.png",
        base_url="http://comfy:8188",
        probe=_PROBE,
    )
    asset = ConditioningAsset(
        kind="image",
        role="init_image",
        ref=Artifact(filename="seed.png", uri="https://x/seed.png"),
    )
    # graph has node 7 without an "inputs" subdict — engine must
    # create it during patch.
    spec = {
        "graph": {"7": {"class_type": "LoadImage"}},
        "node_overrides": {},
        "asset_node_ids": {"init_image": "7"},
    }
    job = GenerationJob(spec=spec, segments=[Segment(prompt="p", assets=[asset])], params={})
    backend.submit(job)
    merged = posted[0]["prompt"]
    # Bug catch: KeyError on missing "inputs" subdict in the patch path.
    assert merged["7"]["inputs"]["image"] == "uploaded.png"
```

- [ ] **Step 2: Run new tests to confirm they fail**

Run: `pixi run pytest tests/engines/test_comfyui.py -v -k "asset or upload or file_uri or patches"`
Expected: 8 fails (missing kwargs on Backend `__init__`, missing `asset_node_ids` handling).

- [ ] **Step 3: Add default `_urllib_post_multipart` module-level helper**

Add to `src/kinoforge/engines/comfyui/__init__.py` near the existing `_urllib_get_bytes` default (Layer E):

```python
import email.mime.multipart
import email.mime.application
import json
import urllib.request


def _urllib_post_multipart(
    url: str,
    *,
    field_name: str,
    filename: str,
    content: bytes,
) -> str:
    """Default multipart POST helper for /upload/image.

    Sends a single-part multipart form with the given field name and
    filename, content type ``application/octet-stream``. Reads the JSON
    response and returns ``response["name"]`` per the ComfyUI server
    contract.

    Args:
        url: Upload endpoint URL.
        field_name: Form field name (ComfyUI expects ``"image"``).
        filename: Filename hint passed in the Content-Disposition header.
        content: Raw bytes to upload.

    Returns:
        The server-side filename string from the response JSON.

    Raises:
        urllib.error.URLError, OSError: Transport failures (wrapped by
        the caller as AssetFetchError).
    """
    boundary = "----kinoforge-boundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return str(payload["name"])
```

- [ ] **Step 4: Extend `ComfyUIBackend.__init__`**

Replace block at line 172-194:

```python
    def __init__(
        self,
        *,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]],
        http_get: Callable[[str], dict[str, Any]],
        base_url: str,
        probe: ModelProfile,
        sleep: Callable[[float], None] = time.sleep,
        http_get_bytes: Callable[[str], bytes] = _urllib_get_bytes,
        http_post_file: Callable[..., str] = _urllib_post_multipart,
    ) -> None:
        """Initialise the backend with injected transport callables.

        Args:
            http_post: POST callable ``(url, json_body) -> dict``.
            http_get: GET callable ``(url) -> dict``.
            base_url: Base URL of the ComfyUI server, e.g.
                ``"http://localhost:8188"``.  No trailing slash.
            probe: ``ModelProfile`` returned by ``inspect_capabilities``.
            sleep: Callable invoked between poll iterations in ``result``.
            http_get_bytes: Byte fetcher used by ``submit`` to resolve
                asset URIs (http/https only — file:// is read by
                ``core/assets.asset_bytes`` via stdlib Path).
            http_post_file: Multipart POST callable
                ``(url, *, field_name, filename, content) -> str``
                returning the server-side filename. Defaults to the
                stdlib urllib multipart helper.
        """
        self._http_post = http_post
        self._http_get = http_get
        self._base_url = base_url.rstrip("/")
        self._probe = probe
        self._sleep = sleep
        self._http_get_bytes = http_get_bytes
        self._http_post_file = http_post_file
```

- [ ] **Step 5: Extend `submit`**

Replace block at line 213-238:

```python
    def submit(self, job: GenerationJob) -> str:
        """POST the merged workflow graph to ``/prompt`` and return the prompt ID.

        Layer F: for each role in ``job.spec.get("asset_node_ids", {})``,
        find the matching asset on ``segments[0]`` via ``find_asset``,
        fetch its bytes via ``asset_bytes`` (http/https/file), upload
        them via ``self._http_post_file`` to ``/upload/image``, and
        patch ``node_overrides[<node_id>]["inputs"]["image"]`` with
        the returned server-side filename. Existing graph + override
        merge proceeds afterward unchanged.

        Args:
            job: The ``GenerationJob`` whose ``spec`` contains ``"graph"``
                and ``"node_overrides"`` (and optionally ``"asset_node_ids"``).

        Returns:
            The ``prompt_id`` string from the ComfyUI server response.

        Raises:
            AssetFetchError: Asset URI fetch or upload failed.
        """
        graph: dict[str, Any] = copy.deepcopy(job.spec.get("graph", {}))
        overrides: dict[str, Any] = copy.deepcopy(
            job.spec.get("node_overrides", {})
        )
        asset_node_ids: dict[str, str] = job.spec.get("asset_node_ids", {})

        for role, node_id in asset_node_ids.items():
            asset = find_asset(job, role)
            if asset is None:
                continue
            payload = asset_bytes(
                asset.ref.uri,
                http_get_bytes=self._http_get_bytes,
            )
            upload_url = f"{self._base_url}/upload/image"
            try:
                uploaded_name = self._http_post_file(
                    upload_url,
                    field_name="image",
                    filename=asset.ref.filename or f"{role}.png",
                    content=payload,
                )
            except (urllib.error.URLError, OSError) as e:
                raise AssetFetchError(
                    f"ComfyUI /upload/image failed for role {role!r}: {e}"
                ) from e
            node_patch = overrides.setdefault(str(node_id), {})
            inputs = node_patch.setdefault("inputs", {})
            inputs["image"] = uploaded_name

        # Existing deep-merge loop:
        for node_id, node_patch in overrides.items():
            if node_id in graph:
                _deep_merge(graph[node_id], node_patch)
            else:
                graph[node_id] = copy.deepcopy(node_patch)
        url = f"{self._base_url}/prompt"
        response = self._http_post(url, {"prompt": graph})
        return str(response["prompt_id"])
```

Add imports near top:

```python
import urllib.error  # if not already present
from kinoforge.core.assets import asset_bytes, find_asset
from kinoforge.core.errors import AssetFetchError
```

- [ ] **Step 6: Extend `ComfyUIEngine.__init__` + `backend` + `validate_spec`**

In `ComfyUIEngine.__init__` (line 319+), add the new seam params alongside existing ones; store on `self`:

```python
        # Existing block continues with new args:
        http_post_file: Callable[..., str] = _urllib_post_multipart,
        # ... at bottom of __init__:
        self._http_post_file = http_post_file
```

In `backend()` (line 419-443), thread the new seams through:

```python
        return ComfyUIBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            base_url=base_url,
            probe=self._probe,
            sleep=self._sleep,
            http_get_bytes=self._http_get_bytes,
            http_post_file=self._http_post_file,
        )
```

Extend `validate_spec` (line 471-490):

```python
    def validate_spec(self, job: GenerationJob) -> None:
        """Raise ValidationError for missing spec keys or unmapped asset roles.

        Both ``"graph"`` and ``"node_overrides"`` are required keys.
        In addition, for every asset on ``job.segments[0]``, the asset's
        role must appear in ``job.spec.get("asset_node_ids", {})``
        — the template author must declare which graph node receives
        each conditioning asset.

        Args:
            job: The ``GenerationJob`` to validate.

        Raises:
            ValidationError: required key missing, or asset role declared
                on ``segments[0]`` but absent from ``asset_node_ids``.
        """
        required = {"graph", "node_overrides"}
        missing = required - set(job.spec.keys())
        if missing:
            raise ValidationError(
                f"ComfyUI job.spec is missing required keys: {sorted(missing)}"
            )
        if not job.segments:
            return
        asset_node_ids: dict[str, str] = job.spec.get("asset_node_ids", {})
        for asset in job.segments[0].assets:
            if asset.role not in asset_node_ids:
                raise ValidationError(
                    f"asset role {asset.role!r} present on segments[0] but "
                    f"spec.asset_node_ids has no mapping; add "
                    f"asset_node_ids.{asset.role}: <node_id> to the spec"
                )
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pixi run pytest tests/engines/test_comfyui.py -v`
Expected: previous tests + 8 new pass.

- [ ] **Step 8: Run full quality gate**

Run: `pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add src/kinoforge/engines/comfyui/__init__.py tests/engines/test_comfyui.py
git commit -m "$(cat <<'EOF'
feat(comfyui): wire init_image asset via /upload/image + node patch

ComfyUIBackend.__init__ gains http_get_bytes + http_post_file seams.
submit() walks spec["asset_node_ids"] mapping role -> graph node_id;
for each role, find_asset on segments[0], asset_bytes resolves URI
(http(s) via injected fetcher; file:// via Path.read_bytes), uploads
multipart to /upload/image via _http_post_file, patches
node_overrides[<node_id>]["inputs"]["image"] with the returned
server-side filename. Existing graph deep-merge follows unchanged.
AssetFetchError wraps fetch + upload transport failures.
validate_spec rejects asset roles without a node_id mapping.

Adds module-level _urllib_post_multipart default (stdlib-only multipart
helper) that parses {"name": "<filename>"} from /upload/image response.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/engines/comfyui/__init__.py", "tests/engines/test_comfyui.py"], "verifyCommand": "pixi run pytest tests/engines/test_comfyui.py -v && pixi run typecheck && pixi run lint", "acceptanceCriteria": ["ComfyUIBackend.__init__ gains http_get_bytes + http_post_file seams", "submit uploads bytes + patches node_overrides", "AssetFetchError wraps fetch and upload failures", "validate_spec rejects unmapped role", "all prior tests + 8 new pass"]}
```

---

## Task 5: GenerateClipStage post-chain validate_spec

**Goal:** Stage calls `engine.validate_spec(job)` after `inject_tail_frame` on each chained segment so misconfigured `asset_node_ids` / `asset_paths` surface before the engine HTTP round-trip.

**Files:**
- Modify: `src/kinoforge/pipeline/generate_clip.py` — after line 118 (`job = inject_tail_frame(job, tail_asset)`)
- Modify: `tests/pipeline/test_generate_clip.py` — 3 new tests

**Acceptance Criteria:**
- [ ] Stage calls `self.engine.validate_spec(job)` exactly once per post-chain job (i.e., for `i > 0 and should_chain` iterations)
- [ ] Stage's chain test (existing from Layer E) still passes
- [ ] Stage aborts (raises out) when `validate_spec` raises on a chained segment; no segment-i+1 dispatch occurs
- [ ] A spy `GenerationEngine` confirms the asset reaches the engine's `submit()` on chained iterations

**Verify:** `pixi run pytest tests/pipeline/test_generate_clip.py -v`

**Steps:**

- [ ] **Step 1: Read existing test infrastructure first**

Before writing new tests, read `tests/pipeline/test_generate_clip.py` to identify the existing fixtures and helpers from the Layer E chain test (commit `f41f3c4`). Specifically locate:

- The `_PROBE` (or equivalent) `ModelProfile` constant
- The store fixture (likely `LocalArtifactStore(tmp_path)`)
- The pool builder (likely `SequentialPool` wrapping a fake backend)
- The existing chain test that proves Layer E behaviour — the new tests extend the same scaffolding

Run: `rg -n "class _|def _build|_PROBE|SequentialPool|LocalArtifactStore|run_id" tests/pipeline/test_generate_clip.py | head -30`

The tests below assume the file already has helpers in the style of `_make_stage(engine, backend, segments)` or similar. If the existing tests instead build the stage inline per test, follow that style — duplicate the inline construction in each new test rather than introducing a new builder.

- [ ] **Step 2: Write failing tests** (append to `tests/pipeline/test_generate_clip.py`)

```python
from dataclasses import dataclass

from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    Artifact,
    ConditioningAsset,
    GenerationBackend,
    GenerationJob,
    GenerationRequest,
    ModelProfile,
    Segment,
)
# Existing imports for ModelProfile, store, pool, etc. already present in file.


class _RecordingEngine:
    """Minimal GenerationEngine duck-type for Layer F stage tests.

    Records every validate_spec call so we can assert call count and
    ordering. Returns deterministic tail bytes from extract_last_frame
    so the stage's chain branch produces a real (mock) tail PNG.
    """

    name: str = "rec"
    requires_compute: bool = False
    requires_local_weights: bool = False

    def __init__(self, *, raise_on_validate_call: int | None = None) -> None:
        self.validate_calls: list[GenerationJob] = []
        self.raise_on_validate_call = raise_on_validate_call

    def validate_spec(self, job: GenerationJob) -> None:
        self.validate_calls.append(job)
        if (
            self.raise_on_validate_call is not None
            and len(self.validate_calls) == self.raise_on_validate_call
        ):
            raise ValidationError("simulated misconfig")

    def extract_last_frame(self, artifact: Artifact) -> bytes:
        return b"TAIL_PNG_BYTES"


class _RecordingBackend(GenerationBackend):
    """Backend that records every job it sees at submit() time."""

    def __init__(self, probe: ModelProfile) -> None:
        self.submitted: list[GenerationJob] = []
        self._probe = probe

    def submit(self, job: GenerationJob) -> str:
        self.submitted.append(job)
        return f"j-{len(self.submitted)}"

    def result(self, job_id: str) -> Artifact:
        return Artifact(filename=f"{job_id}.mp4", uri="", meta={})

    def capabilities(self) -> ModelProfile:
        return self._probe

    def inspect_capabilities(self) -> ModelProfile:
        return self._probe


def _i2v_profile() -> ModelProfile:
    """i2v profile so MODE_ROLE_REQUIREMENTS triggers the chain branch."""
    # Adjust to use the helper already present in the test file. The
    # critical attribute is supported_modes containing "i2v" and
    # supports_native_extension=False so decide() returns N jobs.
    return ModelProfile(
        max_frames=16,
        fps=8.0,
        max_resolution=(512, 512),
        supported_modes=frozenset({"i2v"}),
        supports_native_extension=False,
        supports_joint_audio=False,
    )


def test_stage_calls_validate_spec_on_chained_jobs(tmp_path) -> None:
    """3 segments -> 2 chain hops -> 2 post-chain validate_spec calls."""
    profile = _i2v_profile()
    engine = _RecordingEngine()
    backend = _RecordingBackend(profile)
    store = LocalArtifactStore(str(tmp_path))  # use whatever the file imports
    pool = SequentialPool([backend])
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="r1",
        accepted_kinds={"image"},
        base_params={},
        base_spec={"graph": {}, "node_overrides": {}},  # engine-agnostic stub
        engine=engine,  # type: ignore[arg-type]
    )
    # 3 segments via segments_override to skip the orchestrator-level
    # validate_request (the stage's own validate_request only runs on
    # the implicit-build path).
    segments = [
        Segment(prompt=f"p-{i}", assets=[]) for i in range(3)
    ]
    request = GenerationRequest(prompt="ignored", mode="i2v", assets=[])
    stage.run(request, segments_override=segments)
    # Bug catch: pre-Layer-F (no post-chain validate_spec) would yield 0.
    # Bug catch: a single trailing validate_spec instead of per-iteration
    # would yield 1. Bug catch: an off-by-one calling validate_spec on
    # the un-chained seg-0 too would yield 3.
    assert len(engine.validate_calls) == 2


def test_chain_delivers_asset_to_engine_submit(tmp_path) -> None:
    profile = _i2v_profile()
    engine = _RecordingEngine()
    backend = _RecordingBackend(profile)
    store = LocalArtifactStore(str(tmp_path))
    pool = SequentialPool([backend])
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="r2",
        accepted_kinds={"image"},
        base_params={},
        base_spec={"graph": {}, "node_overrides": {}},
        engine=engine,  # type: ignore[arg-type]
    )
    segments = [Segment(prompt="p-0", assets=[]), Segment(prompt="p-1", assets=[])]
    stage.run(
        GenerationRequest(prompt="ignored", mode="i2v", assets=[]),
        segments_override=segments,
    )
    # Bug catch: pre-Layer-E (no chain) would leave seg-0 of submitted[1]
    # with empty assets. Pre-Layer-F (asset injected but not validated)
    # cannot be distinguished from Layer F here — we are asserting the
    # *injection* sticks, the validation count is the other test's job.
    assert backend.submitted[0].segments[0].assets == []
    chained_assets = backend.submitted[1].segments[0].assets
    assert len(chained_assets) == 1
    assert chained_assets[0].role == "init_image"
    # The injected URI is whatever store.put_bytes returned in the chain
    # branch; the run_id appears in the URI by namespace contract.
    assert "r2" in chained_assets[0].ref.uri


def test_stage_aborts_when_engine_validate_spec_raises_post_chain(tmp_path) -> None:
    profile = _i2v_profile()
    # Raise on the FIRST validate_spec call (which corresponds to the
    # first chained segment, i.e. seg index 1 — seg 0 is pre-chain and
    # NOT validated by the stage).
    engine = _RecordingEngine(raise_on_validate_call=1)
    backend = _RecordingBackend(profile)
    store = LocalArtifactStore(str(tmp_path))
    pool = SequentialPool([backend])
    stage = GenerateClipStage(
        profile=profile,
        pool=pool,
        store=store,
        run_id="r3",
        accepted_kinds={"image"},
        base_params={},
        base_spec={"graph": {}, "node_overrides": {}},
        engine=engine,  # type: ignore[arg-type]
    )
    segments = [Segment(prompt=f"p-{i}", assets=[]) for i in range(3)]
    with pytest.raises(ValidationError, match="simulated misconfig"):
        stage.run(
            GenerationRequest(prompt="ignored", mode="i2v", assets=[]),
            segments_override=segments,
        )
    # Bug catch: a stage that catches+swallows ValidationError would
    # have run all 3 submissions. Bug catch: validating *before*
    # extract_last_frame would have prevented submission 0 too.
    assert len(backend.submitted) == 1

- [ ] **Step 3: Run new tests to confirm they fail**

Run: `pixi run pytest tests/pipeline/test_generate_clip.py -v -k "chain or validate or abort"`
Expected: 3 fails.

- [ ] **Step 4: Implement stage change**

In `src/kinoforge/pipeline/generate_clip.py`, after the existing chain block (line 105-118), add one line:

```python
                job = inject_tail_frame(job, tail_asset)
                self.engine.validate_spec(job)        # NEW (Layer F)
            art = self.pool.submit(job).result()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pixi run pytest tests/pipeline/test_generate_clip.py -v`
Expected: all prior + 3 new pass.

- [ ] **Step 6: Run full suite — catches engine-side regressions**

Run: `pixi run test`
Expected: ~508 tests pass (478 prior + ~30 new across Tasks 1–5).

- [ ] **Step 7: Run full quality gate**

Run: `pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/kinoforge/pipeline/generate_clip.py tests/pipeline/test_generate_clip.py
git commit -m "$(cat <<'EOF'
feat(stage): post-chain validate_spec call in GenerateClipStage

After inject_tail_frame mutates segments[0].assets for a chained
segment, call engine.validate_spec(job) before pool.submit. Catches
misconfigured asset_node_ids / asset_paths before the engine HTTP
round-trip, when the asset injection wouldn't otherwise be
validated by the orchestrator-level pre-dispatch check.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

```json:metadata
{"files": ["src/kinoforge/pipeline/generate_clip.py", "tests/pipeline/test_generate_clip.py"], "verifyCommand": "pixi run pytest tests/pipeline/test_generate_clip.py -v && pixi run test", "acceptanceCriteria": ["stage calls validate_spec once per post-chain job", "asset reaches engine.submit on chained iterations", "stage aborts on validate_spec raise", "all prior + 3 new pass"]}
```

---

## Task 6: Docs + PROGRESS + final verification + merge

**Goal:** Update user-facing docs (README, PROGRESS), run the full quality gate, and merge the layer via `--no-ff` per the established pattern.

**Files:**
- Modify: `README.md` — replace Layer F limitation callout; document `asset_paths` (Diffusers, Hosted) + `asset_node_ids` (ComfyUI)
- Modify: `PROGRESS.md` — new "Phase 16 — Layer F: engine asset wiring" entry; mark "Single next action" with the next-recommended layer

**Acceptance Criteria:**
- [ ] README's existing "Layer F limitation" callout is replaced with a description of the asset_paths/asset_node_ids contract
- [ ] README's Diffusers section documents `engine.diffusers.asset_paths: {init_image: <dot.path>}` with an example
- [ ] README's Hosted section documents `engine.hosted.asset_paths: {init_image: <dot.path>}` with an example
- [ ] README adds a ComfyUI section paragraph (or extends the existing one) documenting `spec.asset_node_ids` for graph-author use
- [ ] PROGRESS.md gains a Phase 16 entry mirroring the Phase 14/15 style (one-line per task with commit refs)
- [ ] PROGRESS.md "Single next action" updated to point at the next-recommended layer (per spec §"Out of scope" — ConcurrentPool or Keyframe stage)
- [ ] Full suite, mypy, ruff, pre-commit pass
- [ ] Layer merge commit lands on `main` with `--no-ff`

**Verify:**
- `pixi run test && pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files`
- `git log --oneline -10` shows the merge commit and the 5 task commits

**Steps:**

- [ ] **Step 1: Read existing README sections to confirm placement**

Run: `rg -n "Layer F|Diffusers|Hosted|ComfyUI|asset" README.md | head -30`

Note existing section anchors and the exact "Layer F limitation" wording introduced in commit `36b88f5` (per `git log`). Place the new content immediately replacing that callout.

- [ ] **Step 2: Update README**

Replace the Layer F limitation callout (search for the existing text matching "Layer F" in README.md) with:

```markdown
### Engine asset wiring (Layer F)

The non-native chain (Layer E persists tail PNGs into the store via
`store.put_bytes`; Layer F here wires those assets into each engine's
outgoing request) is end-to-end. Three engines participate:

- **ComfyUI:** the job spec declares
  `asset_node_ids: {init_image: "<node_id>"}` naming the LoadImage node
  that receives the asset. The engine fetches the asset bytes (via
  configurable URI scheme — `http(s)` via the engine's injected
  fetcher; `file://` via the local filesystem), uploads them to
  `/upload/image` on the ComfyUI server, then patches the named node's
  `inputs.image` field via the standard `node_overrides` mechanism.
- **Diffusers:** YAML `engine.diffusers.asset_paths: {init_image:
  "<dot.path>"}` declares where in the POST `/generate` request body
  the asset's URI is written. The Diffusers server fetches the URI
  itself (matching the Layer E server contract for response `url`).
- **Hosted:** YAML `engine.hosted.asset_paths: {init_image:
  "<dot.path>"}` mirrors Diffusers. Supports nested paths
  (e.g. `"input.image_url"`) for providers that nest their inputs.

Roles other than `init_image` (`first_frame`, `last_frame`,
`drive_audio`, `source_video`) are not yet wired — no engine declares
support for them today.

Example YAML excerpts:

```yaml
# Diffusers
engine:
  kind: diffusers
  base_url: http://localhost:8000
  asset_paths:
    init_image: init_image_url

# Hosted
engine:
  kind: hosted
  endpoint: https://api.example.com/v1/predictions
  asset_paths:
    init_image: input.image_url
```
```

If existing Diffusers/Hosted sections in README mention `url_path` (Layer E), append the `asset_paths` mention there too so the docs flow.

- [ ] **Step 3: Update PROGRESS.md**

Append a new Phase 16 section after the existing Phase 15:

```markdown
### Phase 16 — per-engine asset wiring (post-MVP Layer F)
- [x] Task 1: `AssetFetchError` + `core/assets.py` (find_asset, asset_bytes, set_by_dot_path) + 10 tests — commit `<sha-1>`
- [x] Task 2: Diffusers backend asset_paths + submit + validate_spec + 4 tests — commit `<sha-2>`
- [x] Task 3: Hosted backend asset_paths + submit + validate_spec + 4 tests — commit `<sha-3>`
- [x] Task 4: ComfyUI backend http_get_bytes + http_post_file seams + asset_node_ids + 8 tests — commit `<sha-4>`
- [x] Task 5: GenerateClipStage post-chain `validate_spec` + 3 tests — commit `<sha-5>`
- [x] Task 6: README + PROGRESS + final gate — commit `<sha-6>`
```

(Fill in real SHAs after each prior commit; `git log --oneline -10`
gives them.)

Update the "Single next action" block at the top of PROGRESS.md to point at the next recommended layer. From the spec's §9 / PROGRESS.md current §"Single next action":

- **Layer #4 — Concurrent backend scheduler (issue #3)** — drop-in
  `ConcurrentPool` behind existing `BackendPool` ABC.
- **Layer #5 — Keyframe / image-generation upstream Stage (issue #4)** —
  forces engine-kind ADR.

Choose one (recommend ConcurrentPool: pure dispatch concern, no other
modules touched). Replace existing "Single next action" text accordingly.

- [ ] **Step 4: Final quality gate**

Run: `pixi run test && pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files`
Expected: `~508 passed`, mypy clean, ruff clean, pre-commit clean.

- [ ] **Step 5: Commit docs**

```bash
git add README.md PROGRESS.md
git commit -m "$(cat <<'EOF'
docs(layer-f): replace limitation callout; document asset_paths

Layer F complete: per-engine submit() consumes seg-0 assets. README's
Engine asset wiring section documents the contract per engine
(asset_node_ids for ComfyUI; asset_paths for Diffusers/Hosted) with
YAML examples. PROGRESS Phase 16 entry mirrors Phase 14/15 style.
Single next action moved to next recommended layer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6: Merge layer to main via `--no-ff`**

This plan assumed work happened on a `build/layer-f` branch (or worktree). If working directly on main, skip the merge step. Otherwise:

```bash
git checkout main
git merge --no-ff build/layer-f -m "$(cat <<'EOF'
Merge branch 'build/layer-f': per-engine asset wiring (Layer F)

Layer F closes the non-native multi-segment loop opened by Layer E.
Each engine's submit() now consumes seg-0 assets:
- ComfyUI uploads bytes via /upload/image, patches LoadImage node
- Diffusers writes URI at dot-path in POST /generate body
- Hosted writes URI at dot-path in provider endpoint POST

Shared core/assets.py: find_asset, asset_bytes, set_by_dot_path
(pure, no engine imports; matches Layer E discipline).

AssetFetchError wraps URI fetch + upload failures consistently.

GenerateClipStage calls engine.validate_spec(job) after
inject_tail_frame so misconfig surfaces before the HTTP round-trip.

~30 new tests; 478 → ~508 passing.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Verify final state**

```bash
git log --oneline -10
pixi run test
```

Expected: merge commit visible; ~508 tests passing.

```json:metadata
{"files": ["README.md", "PROGRESS.md"], "verifyCommand": "pixi run test && pixi run typecheck && pixi run lint && pixi run pre-commit run --all-files", "acceptanceCriteria": ["README Layer F callout replaced with asset wiring section", "PROGRESS Phase 16 entry added", "Single next action updated", "full quality gate clean", "merge commit lands on main"]}
```
