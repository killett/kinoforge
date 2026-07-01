# Design — pod file-upload path for upscale-only flow

**Date:** 2026-06-29
**Workstream:** video-upscaling P2 — unblock T15/T16
**Predecessor specs / plans:**
- `docs/superpowers/specs/2026-06-29-upscaler-packaging-pivot-design.md`
- `docs/superpowers/plans/2026-06-29-upscaler-packaging-pivot.md`

## Problem

`SpandrelEngine.upscale()` builds an `/upscale` payload with
`source_url=file:///workspace/output/<...>.mp4` — the operator-host path
of the local mp4. The pod's `SpandrelRuntime._download_to_local_temp`
short-circuits `file://` URLs by reading them off the local pod
filesystem. The operator's file does not exist on the pod, so the
upscale fails before any GPU work. T15 (single-shot spandrel) and T16
(Wan T2V + spandrel multi-stage warm-reuse) live smokes are blocked on
this gap.

No upload mechanism currently exists in either the engine client or
the wan_t2v_server.

## Goal

Add a one-shot HTTP upload path so `SpandrelEngine.upscale` can ship a
local mp4 to the pod immediately before submitting `/upscale`, then
have the server free the upload after the upscale returns.

Out of scope:
- Multi-file uploads, resumable uploads, chunked PUT.
- Non-mp4 mime types (jpeg / png / webm / etc.).
- Upload from one pod to another.
- Operator-side S3/HF staging (the rejected option-C from brainstorm).

## Architecture

```
operator (SpandrelEngine.upscale)
  │
  │ 1. PUT /upload?api_key=…   body=mp4 bytes
  │                            Content-Type: video/mp4
  │                            X-Filename: <sha8>.mp4
  ▼
wan_t2v_server (proxy-fronted, async FastAPI)
  │  stream-write to /tmp/kf-uploads/<safe-name>
  │  compute sha256 in-stream
  │  return {"path": "...", "size": N, "sha256": "<hex>"}
  │
  │ 2. POST /upscale source_url="file:///tmp/kf-uploads/<safe-name>"
  ▼
SpandrelRuntime.upscale
  │  _download_to_local_temp reads file:// directly
  │  run spandrel
  │  write output to /tmp
  │  finally: if source under /tmp/kf-uploads/ → unlink
  └─ return {"output_url": "file:///tmp/.../upscaled.mp4"}
```

`PUT` (not multipart `POST`) chosen:
- single-file body — no multipart boundary parsing
- streaming-friendly on both ends (urllib client streams a file handle;
  FastAPI / starlette `request.stream()` on server)
- no multipart parser dep, no extra LOC

## Server side — `PUT /upload`

**Location:** `src/kinoforge/runtimes/wan/wan_t2v_server.py`
(alongside existing `/upscale` handler).

### Contract

| Field | Value |
|---|---|
| Method | `PUT /upload` |
| Auth | `?api_key=…` (matches `/upscale`; existing UA shim covers Cloudflare) |
| Headers | `Content-Type: video/mp4` (whitelisted); `X-Filename: <basename>` |
| Body | raw bytes, streamed |
| Response 200 | `{"path": "/tmp/kf-uploads/<safe>", "size": int, "sha256": "<hex>"}` |
| Response 413 | body exceeds `KINOFORGE_MAX_UPLOAD_MB` (default 2048) |
| Response 415 | Content-Type not `video/mp4` |
| Response 422 | `X-Filename` after sanitization is empty |

### Behavior

1. Validate Content-Type. Reject 415 if not `video/mp4`.
2. Sanitize `X-Filename`:
   - `Path(filename).name` (strip directory components).
   - Reject characters outside `[A-Za-z0-9._-]`.
   - Fall back to random `secrets.token_hex(4) + ".mp4"` if empty / dirty.
3. Create `/tmp/kf-uploads/` with mode 0700 idempotently.
4. Stream body into `tempfile.NamedTemporaryFile(delete=False,
   dir="/tmp/kf-uploads")`:
   - 64 KiB chunks.
   - Cumulative size guard — abort + 413 once `KINOFORGE_MAX_UPLOAD_MB`
     reached, unlink partial.
   - Hash via `hashlib.sha256()` in the same loop (single read).
5. `os.replace(tmp, final)` for atomic publish — partial file never
   visible at the advertised path.
6. Whole handler wrapped in `asyncio.to_thread` for the synchronous
   stream→write→hash leg (memory `feedback_wan_server_async_blocking`).
   FastAPI request.stream is already async-iterable, so the wrapping is
   for the hash + write side.
7. Return JSON.

### `/health` capability tag

Append `"upload"` to existing `capabilities: list[str]`. Client probes
this before issuing PUT — if absent, surface a clear error (server is
older than this spec).

## Client side — `SpandrelEngine.upscale`

**Location:** `src/kinoforge/upscalers/spandrel/__init__.py`.

### New helper

```python
def _upload_source(self, instance, local_path: Path) -> str:
    """Upload local mp4 to pod via PUT /upload; return pod-side file:// URL."""
```

Steps:

1. Pre-compute `sha256(local_path)` and `sha8 = sha256[:8]`.
   Filename = `f"{sha8}.mp4"`.
2. Build URL: `f"{proxy_base}/upload?api_key={api_key}"` — using same
   `proxy_base` + `api_key` resolution path that `/upscale` already uses
   (no new credential surface).
3. Construct `urllib.request.Request`:
   - method `PUT`
   - headers: `Content-Type: video/mp4`, `X-Filename: <sha8>.mp4`,
     `User-Agent: kinoforge-spandrel/0.1` (existing UA shim),
     `Content-Length: <stat.st_size>`
   - data: opened binary file handle (urllib auto-streams when `data`
     supports `.read()`)
4. Submit via existing `_retry_proxy_call` substrate so the proxy
   cold-warmup 502 chain (memory
   `wan_server_set_stack_proxy_warmup`) is handled by the same recovery
   path `/lora/set_stack` already uses.
5. Parse JSON response. Compare returned `sha256` against locally
   computed. On mismatch raise `UploadIntegrityError`.
6. Return `f"file://{response['path']}"`.

### Flow change in `upscale()`

```python
def upscale(self, instance, cfg) -> dict:
    if cfg.source.startswith("file://"):
        local = Path(cfg.source[7:])
        source_url = self._upload_source(instance, local)
    else:
        source_url = cfg.source           # http(s):// pass through
    payload = {"source_url": source_url, "spandrel": {...}, ...}
    return self._http_json(f"{proxy_base}/upscale", payload=payload)
```

Local-only sources upload. Remote http(s) sources pass through —
SpandrelRuntime's existing HTTP fetch path covers them.

### New error class

`UploadIntegrityError(KinoforgeError)` — surfaces sha256 mismatch with
local-vs-server hash + bytes-uploaded. Lives next to existing
`ExtrasNotInstalled` in `src/kinoforge/core/errors.py`.

## Cleanup

**Location:** `src/kinoforge/upscalers/spandrel/_runtime.py`
(`SpandrelRuntime.upscale` body).

### Rule

The server unlinks the upload after `/upscale` returns IFF the source
file is under `/tmp/kf-uploads/`. Other `file://` paths (operator
pre-staged on the pod, e.g. stage-1 Wan output for T16) are untouched.

### Implementation

```python
from pathlib import Path

UPLOAD_DIR = Path("/tmp/kf-uploads")

def _maybe_cleanup_upload(source_path: Path) -> None:
    try:
        resolved = source_path.resolve()
        if UPLOAD_DIR in resolved.parents:
            resolved.unlink(missing_ok=True)
    except OSError:
        pass   # best-effort; pod destroy is backstop
```

Called in a `try / finally` around the spandrel render block, so a
failed upscale still frees disk on warm-reuse.

### Why server-side, not client-side

- Client cannot know which file the server actually wrote (server may
  have sanitized filename) — split-brain risk on cleanup target.
- Server already owns the file lifecycle (it wrote it).
- One round-trip, not two.

## Testing

### Unit — client (offline)

`tests/upscalers/spandrel/test_upload.py`

- `test_upload_source_happy_path` — fake server returns matching sha256
  + path; helper returns `file://<path>`.
- `test_upload_source_integrity_mismatch` — server returns wrong
  sha256 → `UploadIntegrityError`.
- `test_upload_source_502_recovers` — first request 502, retry succeeds
  (via `_retry_proxy_call` mock).
- `test_upscale_passes_through_http_source` — `cfg.source="https://..."`
  → no upload helper called, source forwarded as-is.
- `test_upscale_uploads_file_source` — `cfg.source="file:///x.mp4"` →
  upload helper called once with that path.

### Unit — server (offline, FastAPI TestClient)

`tests/runtimes/wan/test_wan_t2v_server_upload.py`

- `test_upload_writes_under_upload_dir` — 2 MB mp4 → 200, file lands at
  returned path, contents match.
- `test_upload_rejects_wrong_content_type` — `text/plain` → 415.
- `test_upload_rejects_oversize` — body > `KINOFORGE_MAX_UPLOAD_MB`
  → 413; partial file removed.
- `test_upload_sanitizes_filename` — `X-Filename: ../../etc/passwd` →
  file lands under `/tmp/kf-uploads/`, not at the traversed path.
- `test_upload_falls_back_on_empty_filename` — missing `X-Filename`
  header → file lands at random `<hex>.mp4`.
- `test_upload_atomic_publish` — kill mid-stream → no file at final
  path (tempfile in same dir, never renamed).
- `test_health_advertises_upload_capability` — `/health` JSON includes
  `"upload"` in `capabilities`.

### Unit — runtime cleanup

`tests/upscalers/spandrel/test_runtime_cleanup.py`

- `test_cleanup_unlinks_upload_dir_source` — source under
  `/tmp/kf-uploads/` is deleted after upscale.
- `test_cleanup_skips_sibling_source` — source at `/tmp/foo.mp4`
  (outside upload dir) is left alone.
- `test_cleanup_skips_when_resolve_fails` — broken symlink does not
  raise.
- `test_cleanup_runs_on_failure` — upscale raises mid-render → upload
  still deleted in `finally`.

### Live smokes — revive

T15 — `tests/live/test_spandrel_x2_upscale.py`:
- Feed
  `file:///workspace/output/20260623-212902_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4`
  (~4.2 MB per PROGRESS).
- Assert: client `_upload_source` returns `file:///tmp/kf-uploads/<sha8>.mp4`,
  output mp4 dimensions = 2× input.
- Budget: ~$0.05 single attempt.
- Cleanup verification: post-run `kinoforge list` shows zero pods.

T16 — `tests/live/test_wan_t2v_plus_spandrel_x2.py`:
- Stage 1 Wan T2V output is pod-local — stage-2 upscale uses that path
  directly, NO upload happens for stage 2 (`file:///tmp/...` outside
  `kf-uploads/`).
- Budget: ~$1-3 (Wan 14B cold dominates; warm-reuse on upscale).

## Failure modes & resilience

| Failure | Detection | Recovery |
|---|---|---|
| Proxy cold-warmup 502 | `_retry_proxy_call` already shipped | retry 3x backoff (existing pattern) |
| Body cut mid-stream | size guard or socket EOF | server unlinks tempfile, returns 5xx |
| Pod disk full | `OSError` from write | server returns 507, client surfaces clearly |
| Concurrent upload of same sha8 | `os.replace` is atomic | second writer wins, both readers see same content (deterministic by sha8) |
| Server crash between upload and `/upscale` | client gets pod-down on next request | upload file lost with pod (which is fine) |
| Upscale crash before cleanup | `finally` block | upload deleted; pod destroy backstop |

## Security notes

- Auth piggybacks on existing `api_key` query param — same threat surface
  as `/upscale` itself. No new credential plumbing.
- Filename sanitization is the only path-traversal guard. The
  `[A-Za-z0-9._-]` allowlist + `Path.name` strip + write into a fixed
  parent dir together prevent `..` escapes.
- 2 GiB default cap matches the largest Wan T2V output the multi-stage
  pipeline produces; tunable via env for future workloads.
- No execute bit set on uploaded files (default umask).

## Telemetry

- Server logs (existing logger): `upload_received bytes=N sha8=...
  filename=... duration_ms=...`.
- `/health.capabilities` includes `"upload"` for client probe.
- No new ledger surface; uploads are ephemeral pod state, not part of
  warm-reuse identity.

## Acceptance criteria

1. `PUT /upload` exists on `wan_t2v_server`, returns 200 with
   `{path, size, sha256}` for a streamed mp4 body.
2. `/health` advertises `"upload"` capability.
3. `SpandrelEngine.upscale` uploads `file://` sources before submitting
   `/upscale`; passes http(s) sources through unchanged.
4. Server-side `_maybe_cleanup_upload` removes the upload after the
   `/upscale` finally-block fires.
5. All 16 unit tests above pass on `pixi run test`.
6. T15 single-shot live smoke GREEN against a fresh RunPod pod, output
   dims = 2× input.
7. T16 multi-stage live smoke GREEN — Wan T2V output upscaled on the
   same warm pod with no second upload.
8. Post-smoke `kinoforge list` reports zero pods.

## Carryover hooks

- Server-side per-prediction cost capture (open follow-up from
  Layer 5) is unaffected — upload is metered as wall-clock pod time
  via existing `costPerHr` refresh path.
- Future FlashVSR drop-in inherits the upload path for free — same
  `cfg.source=file://...` flow lands the same way.
