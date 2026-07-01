# Pod file-upload path for upscale-only flow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a `PUT /upload` endpoint on `wan_t2v_server` + a `SpandrelEngine._upload_source` client helper + server-side cleanup in `SpandrelRuntime`, so `SpandrelEngine.upscale` can ship a local mp4 to the pod before the `/upscale` POST. Unblocks T15/T16 of `2026-06-29-upscaler-packaging-pivot.md`.

**Architecture:** Streaming PUT — raw `video/mp4` body, no multipart. Server writes to `/tmp/kf-uploads/<sha8>.mp4` and returns `{path, size, sha256}`. Client uploads `file://` sources, passes through `http(s)://` sources. Server deletes the upload in the upscale `finally` block if the source path lives under `/tmp/kf-uploads/`.

**Tech Stack:** FastAPI / starlette streaming requests on the server; `urllib.request` (already in use) on the client; `hashlib.sha256` for integrity; `pytest` + `fastapi.testclient.TestClient` for tests.

**User decisions (already made):**
- Upload via `PUT /upload` on `wan_t2v_server` (rejected: SCP/SFTP volume, S3/HF staging).
- Cleanup: server unlinks after `/upscale` returns (rejected: GC-on-destroy, TTL sweeper).
- Source spec: `docs/superpowers/specs/2026-06-29-upscale-pod-upload-design.md`.

---

## File Structure

| Path | Purpose | Action |
|---|---|---|
| `src/kinoforge/core/errors.py` | `UploadIntegrityError` exception class | Modify |
| `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` | `PUT /upload` route, `_UPLOAD_DIR`, `_sanitize_upload_filename`, `/health` capability tag | Modify |
| `src/kinoforge/upscalers/spandrel/__init__.py` | `_upload_source` helper, `upscale()` flow change | Modify |
| `src/kinoforge/upscalers/spandrel/_runtime.py` | `_maybe_cleanup_upload` + `finally` wiring around `pipe.upscale` | Modify |
| `tests/engines/diffusers/test_server_upload.py` | Server unit tests (TestClient) | Create |
| `tests/upscalers/test_spandrel_upload.py` | Client unit tests for `_upload_source` + `upscale()` dispatch | Create |
| `tests/upscalers/test_spandrel_runtime_cleanup.py` | Runtime cleanup tests | Create |

Runtime cleanup actually lives **server-side** (the wan_t2v_server `upscale_handler` / `_run_upscale_job` body) per design §4. Filename `test_spandrel_runtime_cleanup.py` is kept under `tests/upscalers/` because the cleanup helper is logically owned by spandrel's upscale path even though the call site is the server `_run_upscale_job`. Tests use the same TestClient pattern as the server tests.

> Implementation note for Task 4 below: the cleanup hook will be added to `_run_upscale_job` in `wan_t2v_server.py` (since that owns the upscale `finally`), but the helper `_maybe_cleanup_upload` lives in `wan_t2v_server.py` next to the upload route — they share the `_UPLOAD_DIR` constant. The spec language "SpandrelRuntime cleanup" was loose; the implementation puts the helper next to the upload endpoint to keep the upload-dir constant single-sourced.

---

### Task 0: `UploadIntegrityError` exception

**Goal:** Add a typed exception for sha256-mismatch surfacing at upload boundary.

**Files:**
- Modify: `src/kinoforge/core/errors.py`
- Test: `tests/core/test_errors_upload_integrity.py` (new)

**Acceptance Criteria:**
- [ ] `UploadIntegrityError` subclass of `KinoforgeError` exists.
- [ ] Carries `local_sha256`, `server_sha256`, `bytes_sent` attributes.
- [ ] `__str__` includes both hashes in a comparable form.

**Verify:** `pixi run pytest tests/core/test_errors_upload_integrity.py -v` → 2 passed.

**Steps:**

- [ ] **Step 1: Write failing test**

```python
# tests/core/test_errors_upload_integrity.py
"""UploadIntegrityError surface contract."""

import pytest

from kinoforge.core.errors import KinoforgeError, UploadIntegrityError


def test_upload_integrity_error_is_kinoforge_error() -> None:
    """UploadIntegrityError should be catchable as KinoforgeError."""
    exc = UploadIntegrityError(
        local_sha256="a" * 64,
        server_sha256="b" * 64,
        bytes_sent=1024,
    )
    assert isinstance(exc, KinoforgeError)
    assert exc.local_sha256 == "a" * 64
    assert exc.server_sha256 == "b" * 64
    assert exc.bytes_sent == 1024


def test_upload_integrity_error_str_mentions_both_hashes() -> None:
    """str() must include both hashes so operators can grep logs."""
    exc = UploadIntegrityError(
        local_sha256="abc" + "0" * 61,
        server_sha256="def" + "0" * 61,
        bytes_sent=42,
    )
    msg = str(exc)
    assert "abc" in msg
    assert "def" in msg
    assert "42" in msg
```

- [ ] **Step 2: Run test to verify it fails**

```
pixi run pytest tests/core/test_errors_upload_integrity.py -v
```

Expected: FAIL with `ImportError: cannot import name 'UploadIntegrityError'`.

- [ ] **Step 3: Add the class**

At the end of `src/kinoforge/core/errors.py`:

```python
class UploadIntegrityError(KinoforgeError):
    """sha256 of bytes received by the server did not match what the client sent.

    Raised by ``SpandrelEngine._upload_source`` when the ``/upload`` response
    sha256 disagrees with the locally computed digest. Either side of the
    upload pipe corrupted bytes (network, kernel buffer, dirty filename
    sanitization).
    """

    def __init__(self, local_sha256: str, server_sha256: str, bytes_sent: int) -> None:
        self.local_sha256 = local_sha256
        self.server_sha256 = server_sha256
        self.bytes_sent = bytes_sent
        super().__init__(
            f"upload sha256 mismatch: client={local_sha256} server={server_sha256} "
            f"bytes_sent={bytes_sent}"
        )
```

- [ ] **Step 4: Run test to verify it passes**

```
pixi run pytest tests/core/test_errors_upload_integrity.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/errors.py tests/core/test_errors_upload_integrity.py
git commit -m "feat(errors): UploadIntegrityError for /upload sha256 mismatch"
```

---

### Task 1: Server-side `PUT /upload` route (happy path + filename sanitization)

**Goal:** Add `PUT /upload` to `wan_t2v_server`. Stream body into `/tmp/kf-uploads/<sanitized>`, sha256 in-stream, return `{path, size, sha256}`.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (insert near `/upscale` block ~line 1521 onward)
- Create: `tests/engines/diffusers/test_server_upload.py`

**Acceptance Criteria:**
- [ ] `PUT /upload` exists and returns `{"path": "<abs>", "size": int, "sha256": "<hex>"}` for a `video/mp4` body.
- [ ] File lands under `/tmp/kf-uploads/` with sanitized filename.
- [ ] `_UPLOAD_DIR` module constant points at `Path("/tmp/kf-uploads")`.
- [ ] `_sanitize_upload_filename` strips path components, allows `[A-Za-z0-9._-]`, falls back to `secrets.token_hex(4) + ".mp4"` on empty/dirty input.

**Verify:** `pixi run pytest tests/engines/diffusers/test_server_upload.py::test_upload_writes_under_upload_dir tests/engines/diffusers/test_server_upload.py::test_upload_sanitizes_filename tests/engines/diffusers/test_server_upload.py::test_upload_falls_back_on_empty_filename -v` → 3 passed.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/engines/diffusers/test_server_upload.py
"""PUT /upload route — happy path + filename sanitization."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Return a TestClient with /tmp/kf-uploads redirected to tmp_path."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

    monkeypatch.setattr(srv, "_UPLOAD_DIR", tmp_path / "kf-uploads")
    return TestClient(srv.app)


def _mp4_bytes(size: int = 4096) -> bytes:
    """Return ``size`` deterministic bytes for upload tests."""
    return bytes((i % 256 for i in range(size)))


def test_upload_writes_under_upload_dir(client, tmp_path):
    """PUT /upload streams body into _UPLOAD_DIR and returns matching sha256."""
    body = _mp4_bytes(8192)
    expected_sha = hashlib.sha256(body).hexdigest()
    resp = client.put(
        "/upload",
        content=body,
        headers={"Content-Type": "video/mp4", "X-Filename": "src.mp4"},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["sha256"] == expected_sha
    assert payload["size"] == 8192
    written = Path(payload["path"])
    assert written.read_bytes() == body
    assert (tmp_path / "kf-uploads") in written.parents


def test_upload_sanitizes_filename(client, tmp_path):
    """X-Filename: ../../etc/passwd must land inside _UPLOAD_DIR, basename only."""
    body = _mp4_bytes(1024)
    resp = client.put(
        "/upload",
        content=body,
        headers={"Content-Type": "video/mp4", "X-Filename": "../../etc/passwd"},
    )
    assert resp.status_code == 200
    written = Path(resp.json()["path"])
    assert (tmp_path / "kf-uploads") in written.parents
    assert "/etc/" not in str(written)
    # ".." stripped → "passwd"; "p" allowed; written basename is "passwd"
    assert written.name == "passwd"


def test_upload_falls_back_on_empty_filename(client):
    """Missing X-Filename → server generates random <hex>.mp4 fallback."""
    body = _mp4_bytes(1024)
    resp = client.put(
        "/upload",
        content=body,
        headers={"Content-Type": "video/mp4"},
    )
    assert resp.status_code == 200
    written = Path(resp.json()["path"])
    assert written.name.endswith(".mp4")
    assert len(written.stem) == 8  # token_hex(4) → 8 hex chars
```

- [ ] **Step 2: Run tests to verify they fail**

```
pixi run pytest tests/engines/diffusers/test_server_upload.py -v
```

Expected: 3 failures — `404` for the route (or AttributeError for `_UPLOAD_DIR`).

- [ ] **Step 3: Add the route + helper**

Add near the top of `wan_t2v_server.py` next to other module-level constants:

```python
import secrets
import string

_UPLOAD_DIR: Path = Path("/tmp/kf-uploads")  # noqa: S108 — pod-local writable scratch
_UPLOAD_FILENAME_ALLOWED = set(string.ascii_letters + string.digits + "._-")
_UPLOAD_MAX_BYTES = int(os.environ.get("KINOFORGE_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
```

(`os` is already imported. Confirm `import os` near the top of the file before adding the env read; if not, add it.)

Add the helper above the `/upscale` block:

```python
def _sanitize_upload_filename(raw: str | None) -> str:
    """Strip path components and forbidden chars from a client-supplied filename.

    Always returns a non-empty basename ending in ``.mp4``. Falls back to a
    random ``<hex8>.mp4`` if the cleaned name is empty.
    """
    if not raw:
        return f"{secrets.token_hex(4)}.mp4"
    base = Path(raw).name  # strip any "/" path traversal
    cleaned = "".join(c for c in base if c in _UPLOAD_FILENAME_ALLOWED)
    if not cleaned:
        return f"{secrets.token_hex(4)}.mp4"
    return cleaned
```

Add the route below the existing `/upscale/status/{job_id}` handler:

```python
@app.put("/upload")
async def upload_handler(request: Request) -> dict[str, Any]:
    """Stream-write a mp4 body into ``_UPLOAD_DIR``; return path + size + sha256.

    Content-Type must be ``video/mp4``. ``X-Filename`` is sanitized to a basename
    in ``[A-Za-z0-9._-]``; empty or dirty filenames fall back to a random
    ``<hex8>.mp4``. Bodies larger than ``KINOFORGE_MAX_UPLOAD_MB`` (default
    2048 MiB) are rejected with HTTP 413 and the partial tempfile is removed.
    """
    ct = request.headers.get("content-type", "")
    if not ct.startswith("video/mp4"):
        raise HTTPException(
            status_code=415,
            detail=f"Content-Type must be video/mp4, got {ct!r}",
        )
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe_name = _sanitize_upload_filename(request.headers.get("x-filename"))

    # Stream into a sibling tempfile in the same dir so os.replace is atomic.
    fd, tmp_path_str = tempfile.mkstemp(dir=str(_UPLOAD_DIR), suffix=".part")
    tmp_path = Path(tmp_path_str)
    hasher = hashlib.sha256()
    written = 0
    try:
        with os.fdopen(fd, "wb") as fobj:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > _UPLOAD_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload exceeded {_UPLOAD_MAX_BYTES} bytes",
                    )
                hasher.update(chunk)
                fobj.write(chunk)
    except HTTPException:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    final = _UPLOAD_DIR / safe_name
    os.replace(tmp_path, final)
    return {
        "path": str(final),
        "size": written,
        "sha256": hasher.hexdigest(),
    }
```

Confirm at top of the file these imports are present; add any missing:

```python
import tempfile
from fastapi import Request
```

(`hashlib`, `os`, `HTTPException`, `Path`, `Any` are already imported.)

- [ ] **Step 4: Run tests to verify they pass**

```
pixi run pytest tests/engines/diffusers/test_server_upload.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/diffusers/test_server_upload.py
git commit -m "feat(server): PUT /upload — streaming mp4 upload with sha256 echo"
```

---

### Task 2: Server `/upload` rejection paths + atomic publish + health capability

**Goal:** Lock down the rejection corners (wrong content-type, oversize, partial-stream abort) and advertise the `"upload"` capability on `/health`.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (`/health` handler around line 1124; `_capabilities_from_loaded` around line 1108)
- Modify: `tests/engines/diffusers/test_server_upload.py` (add 3 tests) and `tests/engines/diffusers/test_server_health.py` (add 1)

**Acceptance Criteria:**
- [ ] Non-`video/mp4` Content-Type → 415.
- [ ] Body > `KINOFORGE_MAX_UPLOAD_MB` → 413; partial tempfile removed.
- [ ] Mid-stream abort leaves no file at the advertised path (atomic publish via `os.replace`).
- [ ] `/health` JSON includes `"upload"` in `capabilities[]`.

**Verify:** `pixi run pytest tests/engines/diffusers/test_server_upload.py tests/engines/diffusers/test_server_health.py -v` → all green.

**Steps:**

- [ ] **Step 1: Add failing tests**

Append to `tests/engines/diffusers/test_server_upload.py`:

```python
def test_upload_rejects_wrong_content_type(client):
    """text/plain must be rejected with 415."""
    resp = client.put(
        "/upload",
        content=b"hello",
        headers={"Content-Type": "text/plain", "X-Filename": "x.mp4"},
    )
    assert resp.status_code == 415


def test_upload_rejects_oversize(client, monkeypatch, tmp_path):
    """Body larger than the cap → 413 and no leftover file under upload dir."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

    monkeypatch.setattr(srv, "_UPLOAD_MAX_BYTES", 1024)
    body = _mp4_bytes(2048)
    resp = client.put(
        "/upload",
        content=body,
        headers={"Content-Type": "video/mp4", "X-Filename": "big.mp4"},
    )
    assert resp.status_code == 413
    leftover = list((tmp_path / "kf-uploads").iterdir()) if (tmp_path / "kf-uploads").exists() else []
    # Only .part tempfiles or nothing — never a published big.mp4.
    assert not any(p.name == "big.mp4" for p in leftover)


def test_upload_atomic_publish_no_partial(client, tmp_path, monkeypatch):
    """If body stream raises mid-flight, _UPLOAD_DIR has no published file.

    Drive failure by patching ``Path.write_bytes`` is too intrusive; instead
    confirm the .part suffix never lands under the published name when the
    stream is truncated. TestClient's chunked send + an oversize cap is the
    cleanest available proxy for a mid-flight abort: see oversize test.
    """
    # Reuse oversize path as the abort surrogate — same atomicity invariant.
    import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

    monkeypatch.setattr(srv, "_UPLOAD_MAX_BYTES", 1024)
    resp = client.put(
        "/upload",
        content=_mp4_bytes(2048),
        headers={"Content-Type": "video/mp4", "X-Filename": "atom.mp4"},
    )
    assert resp.status_code == 413
    upload_dir = tmp_path / "kf-uploads"
    if upload_dir.exists():
        for p in upload_dir.iterdir():
            assert p.suffix == ".part" or not p.name.startswith("atom"), p
```

Append to `tests/engines/diffusers/test_server_health.py`:

```python
def test_health_advertises_upload_capability():
    """/health JSON includes 'upload' so client can probe before PUT."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

    client = TestClient(srv.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "upload" in resp.json()["capabilities"]
```

- [ ] **Step 2: Run tests to verify they fail**

```
pixi run pytest tests/engines/diffusers/test_server_upload.py tests/engines/diffusers/test_server_health.py -v
```

Expected: 4 new failures (3 upload-side + 1 health-side).

- [ ] **Step 3: Wire `/health` capability**

In `wan_t2v_server.py`, find `_capabilities_from_loaded` (~line 1108) and the `/health` handler (~line 1123). Append `"upload"` to the capabilities list returned by `/health`. Pick whichever site is the single chokepoint; if `/health` builds the list inline, just append there:

```python
@app.get("/health")
def health() -> dict[str, Any]:
    # ... existing body ...
    caps = _capabilities_from_loaded()
    if "upload" not in caps:
        caps.append("upload")
    # ... continue ...
```

(If `_capabilities_from_loaded` returns a tuple or frozenset, return `list(caps) + ["upload"]`. Read the actual function body first to match its style.)

- [ ] **Step 4: Run tests to verify they pass**

```
pixi run pytest tests/engines/diffusers/test_server_upload.py tests/engines/diffusers/test_server_health.py -v
```

Expected: all green (6 upload tests + however many health tests).

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/diffusers/test_server_upload.py tests/engines/diffusers/test_server_health.py
git commit -m "feat(server): /upload rejection paths + health 'upload' capability"
```

---

### Task 3: `SpandrelEngine._upload_source` client helper + `upscale()` dispatch

**Goal:** Add `_upload_source` and switch `upscale()` to upload `file://` sources before submitting `/upscale`. Pass `http(s)://` through unchanged.

**Files:**
- Modify: `src/kinoforge/upscalers/spandrel/__init__.py` (around `class SpandrelEngine`, line 39; `upscale` at line 137; `_http_json` at line 214)
- Create: `tests/upscalers/test_spandrel_upload.py`

**Acceptance Criteria:**
- [ ] `_upload_source(instance, local_path)` computes sha256 + filename, PUTs to `/upload`, verifies returned sha256, returns `f"file://{path}"`.
- [ ] sha256 mismatch raises `UploadIntegrityError` carrying both hashes.
- [ ] First request 502 retries via the existing `_retry_proxy_call` substrate (or matches the pattern already used by `/lora/set_stack`).
- [ ] `upscale()` calls `_upload_source` for `cfg.source` (or `req.source_url`) starting with `file://`; passes other schemes through.

**Verify:** `pixi run pytest tests/upscalers/test_spandrel_upload.py -v` → 5 passed.

**Steps:**

- [ ] **Step 1: Read existing upscale() body**

```
rg -n "def upscale" src/kinoforge/upscalers/spandrel/__init__.py
```

Pin down the exact arg shape (`instance`, what cfg/req object, where `proxy_base` + `api_key` come from). Use the same accessors the existing `_http_json` call uses.

- [ ] **Step 2: Write failing tests**

```python
# tests/upscalers/test_spandrel_upload.py
"""SpandrelEngine._upload_source + upscale() dispatch."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.core.errors import UploadIntegrityError
from kinoforge.upscalers.spandrel import SpandrelEngine


@pytest.fixture
def mp4(tmp_path) -> Path:
    """Write a 16 KiB deterministic mp4 stub and return its path."""
    p = tmp_path / "src.mp4"
    p.write_bytes(bytes(i % 256 for i in range(16 * 1024)))
    return p


@pytest.fixture
def fake_instance():
    """Stand-in for an Instance with the proxy_base + api_key the engine consumes."""
    inst = MagicMock()
    inst.endpoints = {"proxy": "https://example-pod.proxy.runpod.net"}
    inst.metadata = {"api_key": "fake-key"}
    return inst


def test_upload_source_happy_path(mp4, fake_instance):
    """Successful upload returns file:// URL pointing at server-reported path."""
    expected_sha = hashlib.sha256(mp4.read_bytes()).hexdigest()
    server_path = "/tmp/kf-uploads/abcd1234.mp4"

    def fake_putter(url, data, headers, timeout):
        # Read the streamed body so the helper completes its file handle.
        body = data.read()
        assert len(body) == mp4.stat().st_size
        return {
            "path": server_path,
            "size": len(body),
            "sha256": expected_sha,
        }

    engine = SpandrelEngine()
    with patch.object(engine, "_put_upload", side_effect=fake_putter) as putter:
        url = engine._upload_source(fake_instance, mp4)
    assert url == f"file://{server_path}"
    assert putter.call_count == 1


def test_upload_source_integrity_mismatch(mp4, fake_instance):
    """Server sha256 != local sha256 → UploadIntegrityError with both hashes."""
    engine = SpandrelEngine()
    bad_sha = "0" * 64
    with patch.object(
        engine,
        "_put_upload",
        return_value={"path": "/tmp/kf-uploads/x.mp4", "size": mp4.stat().st_size, "sha256": bad_sha},
    ):
        with pytest.raises(UploadIntegrityError) as exc_info:
            engine._upload_source(fake_instance, mp4)
    assert exc_info.value.server_sha256 == bad_sha
    assert exc_info.value.local_sha256 != bad_sha


def test_upload_source_502_recovers(mp4, fake_instance, monkeypatch):
    """First PUT 502 → retry via existing proxy-recovery substrate succeeds."""
    expected_sha = hashlib.sha256(mp4.read_bytes()).hexdigest()
    calls = {"n": 0}

    def flaky_putter(url, data, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            from urllib.error import HTTPError

            raise HTTPError(url, 502, "Bad Gateway", hdrs=None, fp=None)
        data.seek(0)
        body = data.read()
        return {"path": "/tmp/kf-uploads/y.mp4", "size": len(body), "sha256": expected_sha}

    engine = SpandrelEngine()
    with patch.object(engine, "_put_upload", side_effect=flaky_putter):
        url = engine._upload_source(fake_instance, mp4)
    assert url.startswith("file:///tmp/kf-uploads/")
    assert calls["n"] == 2


def test_upscale_passes_through_http_source(fake_instance, monkeypatch):
    """cfg.source starting with https:// → no upload helper called."""
    engine = SpandrelEngine()
    captured: dict = {}

    def fake_http_json(url, payload):
        captured.update(payload)
        return {"job_id": "j1"}

    with patch.object(engine, "_upload_source") as upl, patch(
        "kinoforge.upscalers.spandrel._http_json", side_effect=fake_http_json
    ):
        # Construct the smallest cfg the engine will accept; mirror the
        # field shape used elsewhere (engine, source_url, scale, spandrel block).
        engine.upscale(
            fake_instance,
            cfg={
                "source_url": "https://example.com/x.mp4",
                "source_filename": "x.mp4",
                "engine": "spandrel-realesrgan-x2",
                "scale": "2x",
                "spandrel": {"model": "realesrgan-x2"},
            },
        )
    upl.assert_not_called()
    assert captured["source_url"] == "https://example.com/x.mp4"


def test_upscale_uploads_file_source(mp4, fake_instance):
    """cfg.source starting with file:// → upload helper called once with that path."""
    engine = SpandrelEngine()
    with patch.object(engine, "_upload_source", return_value=f"file:///tmp/kf-uploads/up.mp4") as upl, patch(
        "kinoforge.upscalers.spandrel._http_json", return_value={"job_id": "j2"}
    ) as http:
        engine.upscale(
            fake_instance,
            cfg={
                "source_url": f"file://{mp4}",
                "source_filename": mp4.name,
                "engine": "spandrel-realesrgan-x2",
                "scale": "2x",
                "spandrel": {"model": "realesrgan-x2"},
            },
        )
    upl.assert_called_once_with(fake_instance, mp4)
    submitted = http.call_args.kwargs.get("payload") or http.call_args.args[1]
    assert submitted["source_url"] == "file:///tmp/kf-uploads/up.mp4"
```

> If the existing `SpandrelEngine.upscale(...)` signature uses a typed `UpscaleJob` instead of a raw dict, update the tests to instantiate that type with the same field values. The Acceptance Criteria above (and Steps 4-5) describe behavior, not the exact type — match what `rg -n "def upscale" src/kinoforge/upscalers/spandrel/__init__.py` shows.

- [ ] **Step 3: Run tests to verify they fail**

```
pixi run pytest tests/upscalers/test_spandrel_upload.py -v
```

Expected: 5 failures — `AttributeError: '_upload_source'` / `'_put_upload'`, plus the dispatch tests failing because `upscale()` does not branch on the scheme yet.

- [ ] **Step 4: Add `_put_upload` + `_upload_source` to SpandrelEngine**

In `src/kinoforge/upscalers/spandrel/__init__.py`, inside `class SpandrelEngine` (the existing class around line 39):

```python
import hashlib
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# ... existing class body ...

def _put_upload(
    self,
    url: str,
    data: BinaryIO,
    headers: dict[str, str],
    timeout: int,
) -> dict[str, object]:
    """Single PUT /upload request — streams ``data`` body, parses JSON response.

    Split out so tests can patch HTTP without monkeypatching urllib globally.
    """
    req = Request(url, data=data, method="PUT", headers=headers)  # noqa: S310
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))

def _upload_source(self, instance: "Instance", local_path: Path) -> str:
    """Upload ``local_path`` mp4 to the pod via PUT /upload; return file:// URL.

    Computes sha256 locally, opens the file as the streaming PUT body, and
    cross-checks the server's reported sha256 before returning. Recovers from
    a single 502 (proxy cold-warmup) by retrying once.
    """
    body = local_path.read_bytes()
    local_sha = hashlib.sha256(body).hexdigest()
    short = local_sha[:8]
    base_url = self._base_url(instance)
    api_key = instance.metadata["api_key"]
    url = f"{base_url}/upload?api_key={api_key}"
    headers = {
        "Content-Type": "video/mp4",
        "X-Filename": f"{short}.mp4",
        "Content-Length": str(len(body)),
        "User-Agent": "kinoforge-spandrel/0.1",
    }

    for attempt in range(2):
        with local_path.open("rb") as fobj:
            try:
                payload = self._put_upload(url, fobj, headers, timeout=600)
                break
            except HTTPError as exc:
                if exc.code == 502 and attempt == 0:
                    continue
                raise

    server_sha = str(payload.get("sha256", ""))
    if server_sha != local_sha:
        raise UploadIntegrityError(
            local_sha256=local_sha,
            server_sha256=server_sha,
            bytes_sent=len(body),
        )
    return f"file://{payload['path']}"
```

Add imports at top of the file (some may already be present):

```python
import hashlib
import json
from typing import TYPE_CHECKING, BinaryIO
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kinoforge.core.errors import UploadIntegrityError
```

In `SpandrelEngine.upscale` (~line 137), add the dispatch at the start of the body, before the existing `/upscale` POST:

```python
def upscale(self, instance: "Instance", cfg: dict[str, object]) -> dict[str, object]:
    source_url = str(cfg["source_url"])
    if source_url.startswith("file://"):
        local_path = Path(source_url[len("file://") :])
        new_url = self._upload_source(instance, local_path)
        cfg = dict(cfg)
        cfg["source_url"] = new_url
    # ... existing /upscale POST body unchanged ...
```

> Match the actual `upscale()` arg/return types in the current file before pasting. The dispatch is the diff; the rest of `upscale()` stays.

- [ ] **Step 5: Run tests to verify they pass**

```
pixi run pytest tests/upscalers/test_spandrel_upload.py -v
```

Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/upscalers/spandrel/__init__.py tests/upscalers/test_spandrel_upload.py
git commit -m "feat(upscale): SpandrelEngine._upload_source + file:// dispatch"
```

---

### Task 4: Server-side cleanup — unlink upload after `/upscale` finishes

**Goal:** In `_run_upscale_job`, if the resolved local source path lives under `_UPLOAD_DIR`, unlink it in a `finally` block so warm-reuse repeats don't pile up uploads on the pod.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` (`_run_upscale_job` body around line 1655)
- Create: `tests/engines/diffusers/test_server_upload_cleanup.py`

**Acceptance Criteria:**
- [ ] After `/upscale` completes (state=done OR state=error), an input file whose path lives under `_UPLOAD_DIR` is unlinked.
- [ ] A `file://` source pointing OUTSIDE `_UPLOAD_DIR` (e.g. operator pre-staged at `/workspace/foo.mp4`) is NOT touched.
- [ ] A failed upscale (mock pipe raises) still triggers cleanup.

**Verify:** `pixi run pytest tests/engines/diffusers/test_server_upload_cleanup.py -v` → 3 passed.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/engines/diffusers/test_server_upload_cleanup.py
"""Server cleans up uploaded inputs after /upscale finishes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def srv_module(tmp_path, monkeypatch):
    """Return wan_t2v_server with _UPLOAD_DIR redirected to tmp_path."""
    import kinoforge.engines.diffusers.servers.wan_t2v_server as srv

    monkeypatch.setattr(srv, "_UPLOAD_DIR", tmp_path / "kf-uploads")
    (tmp_path / "kf-uploads").mkdir()
    return srv


def _make_req(srv_module, source_path: Path):
    """Build a minimal UpscaleRequest pointing at source_path."""
    return srv_module.UpscaleRequest(
        source_url=f"file://{source_path}",
        source_filename=source_path.name,
        scale="2x",
        engine="spandrel-realesrgan-x2",
        spandrel=srv_module.SpandrelParams(model="realesrgan-x2"),
        job_id="job-1",
    )


def test_cleanup_unlinks_upload_dir_source(srv_module, tmp_path):
    """Source under _UPLOAD_DIR is unlinked after /upscale completes."""
    upload_dir = srv_module._UPLOAD_DIR
    src = upload_dir / "abcd1234.mp4"
    src.write_bytes(b"x" * 1024)

    req = _make_req(srv_module, src)
    srv_module._upscale_jobs["job-1"] = {"state": "queued", "progress": 0.0, "result": None, "error": None}

    pipe = MagicMock()
    pipe.upscale.return_value = tmp_path / "out.mp4"
    (tmp_path / "out.mp4").write_bytes(b"y" * 1024)

    with patch.object(
        srv_module, "_ensure_on_gpu", new=AsyncMock(return_value={"pipe": pipe, "name": "spandrel-realesrgan-x2"})
    ):
        asyncio.run(srv_module._run_upscale_job("job-1", req))
    assert not src.exists(), "upload should be unlinked"


def test_cleanup_skips_sibling_source(srv_module, tmp_path):
    """Source at /workspace/foo.mp4 (outside _UPLOAD_DIR) is NOT touched."""
    sibling = tmp_path / "sibling.mp4"
    sibling.write_bytes(b"z" * 1024)

    req = _make_req(srv_module, sibling)
    srv_module._upscale_jobs["job-1"] = {"state": "queued", "progress": 0.0, "result": None, "error": None}
    pipe = MagicMock()
    pipe.upscale.return_value = tmp_path / "out.mp4"
    (tmp_path / "out.mp4").write_bytes(b"y" * 1024)

    with patch.object(
        srv_module, "_ensure_on_gpu", new=AsyncMock(return_value={"pipe": pipe, "name": "spandrel-realesrgan-x2"})
    ):
        asyncio.run(srv_module._run_upscale_job("job-1", req))
    assert sibling.exists(), "sibling source must survive cleanup"


def test_cleanup_runs_on_failure(srv_module, tmp_path):
    """Pipe.upscale raising → upload still deleted in finally."""
    src = srv_module._UPLOAD_DIR / "fail1234.mp4"
    src.write_bytes(b"x" * 1024)

    req = _make_req(srv_module, src)
    srv_module._upscale_jobs["job-1"] = {"state": "queued", "progress": 0.0, "result": None, "error": None}
    pipe = MagicMock()
    pipe.upscale.side_effect = RuntimeError("boom")
    with patch.object(
        srv_module, "_ensure_on_gpu", new=AsyncMock(return_value={"pipe": pipe, "name": "spandrel-realesrgan-x2"})
    ):
        asyncio.run(srv_module._run_upscale_job("job-1", req))
    assert not src.exists(), "upload should be unlinked even on upscale failure"
    assert srv_module._upscale_jobs["job-1"]["state"] == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

```
pixi run pytest tests/engines/diffusers/test_server_upload_cleanup.py -v
```

Expected: 3 failures — the cleanup hook does not exist yet, so the upload files stick around. (Note: the existing `_run_upscale_job` may reject `engine="spandrel-..."` per the v1 seedvr2-only branch at line 1642; if that check still trips during this test, fix that first by reading current code OR by mocking the dispatch guard. T9 — `4730c77` — was supposed to land the spandrel-* prefix dispatch.)

- [ ] **Step 3: Add the cleanup helper next to the upload route**

```python
def _maybe_cleanup_upload(local_path: Path) -> None:
    """Unlink ``local_path`` IFF it lives under ``_UPLOAD_DIR``.

    Operator-pre-staged paths (anywhere else) are deliberately left alone so a
    repeat-upscale on the same pod-local file does not vaporize the source.
    """
    try:
        resolved = local_path.resolve()
        if _UPLOAD_DIR.resolve() in resolved.parents:
            resolved.unlink(missing_ok=True)
    except OSError:
        pass  # best-effort; pod destroy is backstop
```

- [ ] **Step 4: Wire it into `_run_upscale_job`**

Wrap the existing body in `try / finally`:

```python
async def _run_upscale_job(job_id: str, req: UpscaleRequest) -> None:
    """Run one upscale under ``_upscale_lock``; mutate ``_upscale_jobs[job_id]``.

    Uploaded inputs under ``_UPLOAD_DIR`` are unlinked in the finally block so
    warm-reuse repeats do not pile up source files on the pod.
    """
    from kinoforge.core.scale_target import ScaleTarget

    local: Path | None = None
    async with _upscale_lock:
        try:
            _upscale_jobs[job_id]["state"] = "running"
            # ... existing entry = await _ensure_on_gpu(...) ...
            local = await asyncio.to_thread(
                _download_to_local_temp, req.source_url, req.source_filename
            )
            # ... existing scale + params + pipe.upscale + sha + probes + result wiring ...
        except Exception as exc:  # noqa: BLE001
            _upscale_jobs[job_id]["error"] = str(exc)
            _upscale_jobs[job_id]["state"] = "error"
        finally:
            if local is not None:
                _maybe_cleanup_upload(local)
```

> Read the actual current body of `_run_upscale_job` before editing (it's ~40 lines at line 1655). Preserve the entire existing logic verbatim; the only structural change is moving the existing `try/except` body into `try/.../finally` with `local` hoisted to the outer scope.

- [ ] **Step 5: Run tests to verify they pass**

```
pixi run pytest tests/engines/diffusers/test_server_upload_cleanup.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py tests/engines/diffusers/test_server_upload_cleanup.py
git commit -m "feat(server): unlink upload under _UPLOAD_DIR in /upscale finally"
```

---

### Task 5: Full regression sweep + spec-coverage tests

**Goal:** Run the full unit suite, fix any regressions, and confirm the 17 acceptance-criteria tests (Task 0–4) all pass together.

**Files:** none (verification only).

**Acceptance Criteria:**
- [ ] `pixi run pre-commit run --all-files` clean.
- [ ] `pixi run pytest tests/core/test_errors_upload_integrity.py tests/engines/diffusers/test_server_upload.py tests/engines/diffusers/test_server_upload_cleanup.py tests/engines/diffusers/test_server_health.py tests/upscalers/test_spandrel_upload.py -v` → all green.
- [ ] `pixi run pytest -x` (full suite) → no new failures attributable to this workstream.

**Verify:** see Acceptance Criteria.

**Steps:**

- [ ] **Step 1: Run targeted suite**

```
pixi run pytest tests/core/test_errors_upload_integrity.py tests/engines/diffusers/test_server_upload.py tests/engines/diffusers/test_server_upload_cleanup.py tests/engines/diffusers/test_server_health.py tests/upscalers/test_spandrel_upload.py -v
```

- [ ] **Step 2: Run full suite**

```
pixi run pytest -x
```

Triage any failures: is it pre-existing (check git stash + branch parent) or introduced by this workstream? Fix only those introduced here.

- [ ] **Step 3: Pre-commit**

```
pixi run pre-commit run --all-files
```

- [ ] **Step 4: Commit (only if Steps 1-3 produced fixes)**

```bash
git add <changed paths>
git commit -m "test: regression fixes from /upload integration"
```

---

### Task 6: T15 live smoke — single-shot spandrel upscale

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Revive the blocked `tests/live/test_spandrel_x2_upscale.py` (commit `7588799`), feed it the 4.2 MB Wan output, run on RunPod, capture GREEN evidence.

**Files:**
- Modify: `examples/configs/upscale-spandrel-x2.yaml` if needed (path to local source).
- Run: `tests/live/test_spandrel_x2_upscale.py`.

**Acceptance Criteria:**
- [ ] Pod boots, `/health` reports `"upload"` capability before `/upscale` POST.
- [ ] Client uploads `/workspace/output/20260623-212902_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` via `PUT /upload`; server returns matching sha256.
- [ ] Upscale completes; output mp4 dimensions = 2× input.
- [ ] Server unlinks `/tmp/kf-uploads/<sha8>.mp4` (verify via `kinoforge` log inspection or `/health` follow-up if exposed).
- [ ] Post-run `pixi run kinoforge list` reports `No running instances.` AND `No instances recorded in ledger.`
- [ ] Spend within ~$0.05 envelope.
- [ ] Evidence captured at `tests/live/_t15_upscale_evidence.json` (or similar) — pod id, output sha256, dims in/out, wall, spend.

**Verify:** `pixi run preflight` clean → live smoke → `pixi run kinoforge list` → both "no instances" lines.

```json:metadata
{"files": ["tests/live/test_spandrel_x2_upscale.py", "examples/configs/upscale-spandrel-x2.yaml"], "verifyCommand": "pixi run pytest tests/live/test_spandrel_x2_upscale.py -v --runlive && pixi run kinoforge list", "acceptanceCriteria": ["/health advertises 'upload'", "PUT /upload returns matching sha256", "output dims = 2x input dims", "upload file unlinked under _UPLOAD_DIR", "kinoforge list shows zero pods + empty ledger", "spend <= $0.10", "evidence JSON committed"], "modelTier": "standard", "userGate": true, "tags": ["user-gate", "live-spend"], "requireEvidenceTokens": [["upload-PUT", "pre-upscale"], ["upscale-done", "dims-2x"], ["pod-destroyed", "kinoforge-list-empty"]]}
```

**Steps:**

- [ ] **Step 1: Confirm scaffold + preflight**

```
pixi run preflight
```

Expected exit 0. If `.env` is missing the symlink (`/workspace/.claude/worktrees/video-upscaling/.env -> /workspace/.env`), restore it — see PROGRESS §P2-resume-gotchas note 4.

- [ ] **Step 2: Inspect + adapt the RED scaffold**

```
pixi run cat tests/live/test_spandrel_x2_upscale.py
```

Confirm the source_url it feeds matches the path in PROGRESS:
`/workspace/output/20260623-212902_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4`. If absent, locate a substitute Wan output in `/workspace/output/` and update the test.

- [ ] **Step 3: Fire**

```
pixi run pytest tests/live/test_spandrel_x2_upscale.py -v --runlive
```

Poll RunPod every 60-90 s during the run (per CLAUDE.md live smoke monitoring rule). Surface GPU/CPU/mem + costPerHr deltas in chat as they cross thresholds.

- [ ] **Step 4: Verify ledger post-run**

```
pixi run kinoforge list
```

Expected: both `No running instances.` AND `No instances recorded in ledger.`

- [ ] **Step 5: Capture evidence + commit**

```bash
git add tests/live/_t15_upscale_evidence.json
git commit -m "test(live): T15 single-shot spandrel upscale GREEN — pod <id> spend \$X.XX"
```

---

### Task 7: T16 live smoke — Wan T2V + spandrel multi-stage warm-reuse

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Goal:** Revive `tests/live/test_wan_t2v_plus_spandrel_x2.py` (commit `486b7d8`). Stage 1 generates Wan T2V output on the pod; stage 2 upscales that pod-local file IN PLACE (no second upload). Validates warm-reuse + cross-engine on same pod.

**Files:**
- Run: `tests/live/test_wan_t2v_plus_spandrel_x2.py`.
- Capture: `tests/live/_t16_multistage_evidence.json`.

**Acceptance Criteria:**
- [ ] Stage 1 Wan T2V completes; output mp4 is at a pod-local path under `/workspace` (NOT `/tmp/kf-uploads/`).
- [ ] Stage 2 spandrel upscale runs on the same warm pod — NO `PUT /upload` happens (server logs absent of upload entry; sibling cleanup invariant from Task 4 confirms).
- [ ] Final output dims = 2× stage-1 dims.
- [ ] Post-run `pixi run kinoforge list` shows zero pods + empty ledger.
- [ ] Spend within ~$1-3 envelope (Wan 14B cold dominates).
- [ ] Evidence JSON at `tests/live/_t16_multistage_evidence.json`.

**Verify:** `pixi run preflight && pixi run pytest tests/live/test_wan_t2v_plus_spandrel_x2.py -v --runlive && pixi run kinoforge list`.

```json:metadata
{"files": ["tests/live/test_wan_t2v_plus_spandrel_x2.py"], "verifyCommand": "pixi run pytest tests/live/test_wan_t2v_plus_spandrel_x2.py -v --runlive && pixi run kinoforge list", "acceptanceCriteria": ["stage-1 Wan output lands outside _UPLOAD_DIR", "stage-2 upscale runs with no PUT /upload (sibling-skip path)", "final dims = 2x stage-1 dims", "kinoforge list shows zero pods + empty ledger", "spend <= $3.50", "evidence JSON committed"], "modelTier": "standard", "userGate": true, "tags": ["user-gate", "live-spend"], "requireEvidenceTokens": [["stage-1-generate", "wan-T2V-done"], ["stage-2-upscale", "no-upload-PUT"], ["dims-2x", "warm-reuse-confirmed"], ["pod-destroyed"]]}
```

**Steps:**

- [ ] **Step 1: Preflight**

```
pixi run preflight
```

- [ ] **Step 2: Fire**

```
pixi run pytest tests/live/test_wan_t2v_plus_spandrel_x2.py -v --runlive
```

Poll the pod every 60-90 s. Surface restart loops or cost-cache drift early — Wan 14B cold-load is the biggest spend slice.

- [ ] **Step 3: Inspect server log for `upload_received`**

After the smoke writes evidence, confirm via `kinoforge` server-log fetch (or stored stderr capture) that EXACTLY ZERO `upload_received bytes=...` lines fired for stage 2. The stage-2 source path is pod-local under `/workspace`, NOT under `_UPLOAD_DIR`.

- [ ] **Step 4: Verify ledger post-run**

```
pixi run kinoforge list
```

- [ ] **Step 5: Capture evidence + commit**

```bash
git add tests/live/_t16_multistage_evidence.json
git commit -m "test(live): T16 Wan T2V + spandrel multi-stage warm-reuse GREEN — pod <id> spend \$X.XX"
```

---

### Task 8: PROGRESS.md close + successful-generations.md entry

**Goal:** Close the video-upscaling P2 workstream end-to-end in PROGRESS.md. Add `successful-generations.md` entry for the new (provider, engine, model, mode) tuple introduced by the spandrel upscale capability.

**Files:**
- Modify: `PROGRESS.md`
- Modify: `/workspace/successful-generations.md` (per CLAUDE.md durability rule)
- Modify: `docs/superpowers/plans/2026-06-29-upscaler-packaging-pivot.md.tasks.json` (mark T15/T16 completed; T17 closed).

**Acceptance Criteria:**
- [ ] PROGRESS.md "Active workstream" entry changes from "PARTIAL" to "SHIPPED" with the new commit shas.
- [ ] `successful-generations.md` has a new section for the spandrel x2 upscale tuple (T15) and a "See also" line for T16 multi-stage.
- [ ] tasks.json statuses updated: T13/T14/T15/T16/T17 all `completed`.

**Verify:** `git log --oneline -5` shows the close commit; `rg 'spandrel' /workspace/successful-generations.md` finds the new entry.

**Steps:**

- [ ] **Step 1: Update tasks.json**

Mark T15/T16/T17 statuses to `completed` (also confirm T13/T14 which were committed but never status-flipped per session resume note).

- [ ] **Step 2: Update PROGRESS.md**

In the "Active workstream" block, flip "PARTIAL" → "SHIPPED" with the two live-smoke commit shas (T15 + T16). Move the workstream below the "---" separator into the SHIPPED history list.

- [ ] **Step 3: Update successful-generations.md**

Add a numbered section per the schema at the top of the file: provider, engine, model, mode (upscale-only), prompt n/a, source mp4 reference, output sha256s, dims in/out, wall, spend, repro recipe, failure-modes recap.

- [ ] **Step 4: Commit**

```bash
git add PROGRESS.md docs/superpowers/plans/2026-06-29-upscaler-packaging-pivot.md.tasks.json /workspace/successful-generations.md
git commit -m "docs: P2 video-upscaling SHIPPED — T15+T16 live GREEN"
```

---

## Self-Review

**Spec coverage check:**
- §1 Architecture → Task 1 + 3 + 4
- §2 Server PUT /upload contract → Task 1 (happy) + 2 (rejection + atomic + health)
- §3 Client `_upload_source` + dispatch → Task 3
- §4 Cleanup → Task 4
- §5 Testing block (16 unit tests) → Tasks 0/1/2/3/4 cover all 16 + UploadIntegrityError contract (Task 0)
- §6 Live-smoke retry → Tasks 6 (T15) + 7 (T16)
- §AC1 PUT /upload exists → Task 1
- §AC2 /health upload capability → Task 2
- §AC3 file:// upload, http(s):// passthrough → Task 3
- §AC4 cleanup → Task 4
- §AC5 16 unit tests pass → Task 5
- §AC6 T15 GREEN → Task 6
- §AC7 T16 GREEN → Task 7
- §AC8 post-smoke ledger empty → Tasks 6 + 7

All spec sections + ACs mapped.

**Placeholder scan:** None. Every step has concrete code or commands.

**Type consistency:** `_UPLOAD_DIR`, `_UPLOAD_MAX_BYTES`, `_upload_source`, `_put_upload`, `_maybe_cleanup_upload`, `UploadIntegrityError`, `_run_upscale_job` — names match across tasks. `Path("/tmp/kf-uploads")` is the single source of truth.

**Per-task isolation self-check (Task 6 + 7 user-gates):** Both Goal sentences name an observable (output dims = 2× input; PUT /upload absent for stage 2), a capture method (evidence JSON), and a pass/fail value (zero pods + empty ledger). Neither needs `requiresUserSpecification`.
