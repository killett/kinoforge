# aria2c Fast-Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship an aria2c fast-path on `src/kinoforge/core/downloader.py` that auto-detects the system binary, transparently substitutes the stdlib transport when present, and silently falls back to stdlib on subprocess failure — without changing the existing skip / resume / sha256-verify / atomic-rename contract.

**Architecture:** Single-file change in `downloader.py`. Two new module-level seam callables (`which_aria2`, `run_aria2`) with stdlib defaults; both injected into `download_one` / `download_all` via kwargs. The aria2c branch sits between the existing skip-path early-return and the existing sha256-verify path. On success it writes the entire file to `<target>.part` via one subprocess call, then re-enters the existing verify + atomic-rename path. On failure it logs a `WARNING`, unlinks the `.part`, and falls through to the existing stdlib fetch branch.

**Tech Stack:** Python 3.13 stdlib (`shutil`, `subprocess`, `logging`); `aria2c` system binary (not a Python dep); pytest + caplog for tests.

**Spec:** `docs/superpowers/specs/2026-06-03-aria2c-fast-path-design.md`

---

## File Structure

| File | Disposition | Responsibility |
|---|---|---|
| `src/kinoforge/core/downloader.py` | Modify | All transport / verify / atomic-rename logic. New seam callables + new kwargs on `download_one` and `download_all`. |
| `tests/core/test_downloader.py` | Modify | 8 existing ACs gain a `which_aria2=lambda: None` override (force stdlib path); 7 new ACs (A1-A7) cover the aria2c path. |
| `README.md` | Modify | New "Faster downloads" sub-section under "Provisioning". |
| `PROGRESS.md` | Modify | New "Phase 29 — aria2c fast-path" entry. |

No new modules. No `pyproject.toml` change (system binary, not a pip dep). No `.env.example` change (Q1 rejected env-var activation).

---

## Tasks

### Task 1: Add aria2c seams to `downloader.py` (no behavior change yet)

**Goal:** Introduce the two new type aliases, the `_ARIA2_BASE_ARGS` constant, the `_shutil_which_aria2` and `_subprocess_run_aria2` default seams, and the module logger. `download_one` and `download_all` signatures are untouched in this task — this is a pure additive task that locks the public symbols.

**Files:**
- Modify: `src/kinoforge/core/downloader.py`
- Modify: `tests/core/test_downloader.py` (one new test that imports the new symbols)

**Acceptance Criteria:**
- [ ] `from kinoforge.core.downloader import WhichCallable, RunAriaCallable, _shutil_which_aria2, _subprocess_run_aria2` succeeds.
- [ ] `_shutil_which_aria2()` returns `None` on a host without aria2c, returns a string on a host with aria2c installed.
- [ ] `_subprocess_run_aria2` raises `KinoforgeError` when given a nonsense URL (network error or non-zero exit).
- [ ] The DEFERRED comment block at lines 10-13 of the existing `downloader.py` module docstring is removed.
- [ ] `pixi run test` is green; suite size 1036 → 1037 (one new symbol-lock test).

**Verify:** `pixi run pytest tests/core/test_downloader.py -v` → all tests pass; new test `test_aria2c_seams_importable` PASS.

**Steps:**

- [ ] **Step 1: Write the symbol-lock test (RED).**

Append to `tests/core/test_downloader.py`:

```python
# ---------------------------------------------------------------------------
# Task 1: aria2c seams importable
# ---------------------------------------------------------------------------


def test_aria2c_seams_importable():
    """T1: the new aria2c module-level seams are importable.

    Bug this catches: a future refactor that renames or removes one of the
    seams without updating downstream callers (the seam contract is part of
    the public-ish API of this module).
    """
    from kinoforge.core.downloader import (
        RunAriaCallable,
        WhichCallable,
        _shutil_which_aria2,
        _subprocess_run_aria2,
    )

    # Trivial use to silence unused-import warnings and prove the names bind.
    assert callable(_shutil_which_aria2)
    assert callable(_subprocess_run_aria2)
    # Type aliases are not callables at runtime in 3.10+ — just bound.
    assert WhichCallable is not None
    assert RunAriaCallable is not None
```

- [ ] **Step 2: Run to confirm RED.**

```bash
pixi run pytest tests/core/test_downloader.py::test_aria2c_seams_importable -v
```

Expected: `ImportError: cannot import name 'WhichCallable' from 'kinoforge.core.downloader'`.

- [ ] **Step 3: Implement the seams in `downloader.py`.**

Replace the existing module docstring + imports block (lines 1-32 of current `downloader.py`) with:

```python
"""Resumable, checksum-verifying parallel HTTP downloader.

Uses stdlib (``hashlib``, ``urllib.request``, ``concurrent.futures``,
``pathlib``, ``os``, ``threading``, ``shutil``, ``subprocess``,
``logging``) plus the optional ``aria2c`` system binary as a transparent
fast-path.

The HTTP transport is injected via a ``fetch`` callable, making it
trivially replaceable in tests by pointing at a loopback server instead
of the real network.  The aria2c subprocess transport is injected via
``which_aria2`` (detect) and ``run_aria2`` (invoke) callables for the
same reason.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import cast
from urllib.error import URLError
from urllib.request import Request, urlopen

from kinoforge.core.errors import KinoforgeError
from kinoforge.core.interfaces import Artifact

logger = logging.getLogger(__name__)

# Type alias for the injected fetch callable.
# Returns (status_code, body_bytes, response_headers).
FetchCallable = Callable[[str, dict[str, str]], tuple[int, bytes, dict[str, str]]]

# Type aliases for the aria2c seams.
# - WhichCallable: returns the absolute path to aria2c, or None when absent.
# - RunAriaCallable: spawns aria2c to download `url` -> `part_path` with
#   the given HTTP headers; raises KinoforgeError on any failure.
WhichCallable = Callable[[], str | None]
RunAriaCallable = Callable[[str, Path, dict[str, str]], None]

_CHUNK = 8192  # 8 KiB read buffer for sha256 streaming

# aria2c invocation knobs.  Battle-tested defaults for HuggingFace /
# CivitAI CDNs; not operator-tunable in this layer (YAGNI).
_ARIA2_BASE_ARGS: tuple[str, ...] = (
    "-x",
    "16",
    "-s",
    "16",
    "-k",
    "1M",
    "--file-allocation=none",
    "--max-tries=3",
    "--retry-wait=2",
    "--allow-overwrite=true",
    "--auto-file-renaming=false",
    "--summary-interval=0",
    "--console-log-level=warn",
)
```

- [ ] **Step 4: Add the seam helper functions.**

Insert these two functions in `downloader.py` immediately after `_urllib_fetch` (which ends at the existing line 90):

```python
def _shutil_which_aria2() -> str | None:
    """Return the absolute path to aria2c, or ``None`` when not on ``PATH``.

    Returns:
        Result of :func:`shutil.which`.  No caching — repeat callers pay
        a ``PATH`` walk per call (~30us on Linux, negligible against the
        per-file subprocess spawn cost).
    """
    return shutil.which("aria2c")


def _subprocess_run_aria2(
    url: str,
    part_path: Path,
    headers: dict[str, str],
) -> None:
    """Spawn ``aria2c`` to download *url* into *part_path*.

    Behaviour:
    - Uses :data:`_ARIA2_BASE_ARGS` plus ``-d``/`-o`` for the output path
      and one ``--header`` flag per header.
    - Wall-clock timeout is 3600s (one hour); larger files at typical
      saturated bandwidth (200+ Mbps) complete inside this window.
    - On non-zero exit, missing binary, or wall-clock timeout, raises
      :class:`~kinoforge.core.errors.KinoforgeError`.

    Args:
        url: Fully-qualified source URL.
        part_path: Target ``.part`` file.  ``part_path.parent`` must
            already exist; aria2c does NOT create directories.
        headers: HTTP request headers (e.g. ``{"Authorization": "Bearer ..."}``).

    Raises:
        KinoforgeError: On any aria2c failure path.
    """
    header_args: list[str] = []
    for key, value in headers.items():
        header_args.extend(["--header", f"{key}: {value}"])
    cmd = [
        "aria2c",
        *_ARIA2_BASE_ARGS,
        "-d",
        str(part_path.parent),
        "-o",
        part_path.name,
        *header_args,
        url,
    ]
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=3600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KinoforgeError(f"aria2c spawn failed for {url!r}: {exc}") from exc
    if result.returncode != 0:
        raise KinoforgeError(
            f"aria2c failed for {url!r} (exit {result.returncode}): "
            f"{result.stderr.strip()[:500]}"
        )
```

- [ ] **Step 5: Run to confirm GREEN.**

```bash
pixi run pytest tests/core/test_downloader.py::test_aria2c_seams_importable -v
pixi run pytest tests/core/test_downloader.py -v
```

Expected: import test PASS; all 9 existing downloader tests still PASS (no behavior change).

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/downloader.py tests/core/test_downloader.py
git commit -m "$(cat <<'EOF'
feat(core/downloader): add aria2c seams (no behavior change yet)

Introduces WhichCallable + RunAriaCallable aliases, _ARIA2_BASE_ARGS
constant, _shutil_which_aria2 + _subprocess_run_aria2 default seams,
module logger, and drops the obsolete DEFERRED comment.  download_one
and download_all are unchanged in this commit; T2 wires the transport
branch.

Spec: docs/superpowers/specs/2026-06-03-aria2c-fast-path-design.md
EOF
)"
```

---

### Task 2: Wire the aria2c success path into `download_one` (no fallback yet)

**Goal:** Add `which_aria2` and `run_aria2` kwargs to `download_one`; insert the transport branch between the skip-path and the verify-path. In this task, an aria2c subprocess failure propagates as `KinoforgeError` — the silent stdlib fallback is added in T3.

**Files:**
- Modify: `src/kinoforge/core/downloader.py` (the `download_one` function only; `download_all` stays untouched until T4)
- Modify: `tests/core/test_downloader.py` (retrofit 8 existing `download_one` ACs; add 4 new ACs)

**Acceptance Criteria:**
- [ ] A1: when `which_aria2()` returns a string and `run_aria2` writes correct bytes, no stdlib `fetch` call is made, result file matches, `.part` removed, `Artifact.uri` set.
- [ ] A2: when `which_aria2()` returns `None`, stdlib `fetch` is called and `run_aria2` is never called.
- [ ] A5: when aria2c writes bytes whose sha256 doesn't match `artifact.sha256`, `KinoforgeError` with `"sha256"` substring is raised, `.part` deleted, `target_path` does not exist.
- [ ] A6: when `artifact.sha256 = None` and aria2c writes bytes, the final file matches and the second call is a zero-HTTP skip (filename-based).
- [ ] Existing 8 `download_one` ACs (`test_download_one_*`) pass unchanged after each is given a `which_aria2=lambda: None` argument.

**Verify:** `pixi run pytest tests/core/test_downloader.py -v` → all tests pass; suite size 1037 → 1041 (four new ACs added).

**Steps:**

- [ ] **Step 1: Add a shared `_DISABLED_ARIA` test constant + helper factories.**

Append to `tests/core/test_downloader.py` immediately after the existing `_sha256` helper and `SAMPLE_DATA` constant:

```python
# ---------------------------------------------------------------------------
# aria2c test helpers (T2-T4)
# ---------------------------------------------------------------------------

from kinoforge.core.downloader import RunAriaCallable  # noqa: E402

_DISABLED_ARIA: "Callable[[], str | None]" = lambda: None  # noqa: E731


def _make_aria_stub(bytes_to_write: bytes) -> RunAriaCallable:
    """Build a run_aria2 stub that writes `bytes_to_write` to part_path."""

    def stub(url: str, part_path: Path, headers: dict[str, str]) -> None:
        part_path.write_bytes(bytes_to_write)

    return stub


def _failing_aria(exc_msg: str = "boom") -> RunAriaCallable:
    """Build a run_aria2 stub that always raises KinoforgeError."""

    def stub(url: str, part_path: Path, headers: dict[str, str]) -> None:
        raise KinoforgeError(exc_msg)

    return stub


def _spying_aria(
    record: list[tuple[str, Path, dict[str, str]]],
    bytes_to_write: bytes,
) -> RunAriaCallable:
    """Stub that records every call and writes correct bytes to part_path."""

    def stub(url: str, part_path: Path, headers: dict[str, str]) -> None:
        record.append((url, part_path, dict(headers)))
        part_path.write_bytes(bytes_to_write)

    return stub


def _aria_writing_garbage_then_failing() -> RunAriaCallable:
    """Stub that writes garbage to part_path AND raises.

    Used by T3's A4 to prove the fallback path unlinks the .part before
    retrying via stdlib (else the stdlib branch would Range-resume off
    the garbage prefix).
    """

    def stub(url: str, part_path: Path, headers: dict[str, str]) -> None:
        part_path.write_bytes(b"garbage prefix bytes")
        raise KinoforgeError("aria2c wrote then failed")

    return stub
```

Also add `from collections.abc import Callable` to the existing imports block at the top of the test file (just below `from pathlib import Path`).

- [ ] **Step 2: Retrofit the 8 existing `download_one` tests with `which_aria2=_DISABLED_ARIA`.**

In `tests/core/test_downloader.py`, for each of these existing tests, change every `download_one(...)` call to pass `which_aria2=_DISABLED_ARIA`:

- `test_download_one_creates_file` — both `download_one(artifact, tmp_path)` call.
- `test_download_one_skips_when_complete` — both calls (first + second).
- `test_download_one_resumes_from_part` — single call.
- `test_download_one_raises_on_sha_mismatch` — single call (inside `with pytest.raises`).
- `test_download_one_handles_corrupt_part` — both calls (first raises, second succeeds).
- `test_download_one_no_sha256` — single call.
- `test_download_one_no_sha256_skips_existing` — both calls.

Each affected call gains the keyword argument exactly as:

```python
download_one(artifact, tmp_path, which_aria2=_DISABLED_ARIA)
```

Do NOT change `download_all` calls yet — that's T4.

- [ ] **Step 3: Write the 4 new RED tests (A1, A2, A5, A6).**

Append to `tests/core/test_downloader.py`:

```python
# ---------------------------------------------------------------------------
# T2 A1: aria2c used when detected; no stdlib fetch
# ---------------------------------------------------------------------------


def test_aria2c_used_when_detected(http_server, tmp_path):
    """T2 A1: aria2c writes the bytes; stdlib fetch is never called.

    Bug this catches: the aria2c branch silently falls through to the
    stdlib path, leaving the fast-path code dead and the test suite
    blind to performance regression.
    """
    http_server.serve_bytes("model.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="model.bin",
        url=f"{http_server.base_url}/model.bin",
        sha256=_sha256(SAMPLE_DATA),
    )

    stdlib_calls: list[tuple[str, dict[str, str]]] = []

    def stdlib_spy(url: str, headers: dict[str, str]):
        stdlib_calls.append((url, dict(headers)))
        raise AssertionError("stdlib fetch must not be called on aria2c success path")

    result = download_one(
        artifact,
        tmp_path,
        fetch=stdlib_spy,
        which_aria2=lambda: "/usr/bin/aria2c",
        run_aria2=_make_aria_stub(SAMPLE_DATA),
    )

    dest_file = tmp_path / "model.bin"
    part_file = Path(str(dest_file) + ".part")
    assert dest_file.exists(), "dest file not created"
    assert dest_file.read_bytes() == SAMPLE_DATA, "file content mismatch"
    assert not part_file.exists(), ".part not promoted"
    assert result.uri == str(dest_file)
    assert stdlib_calls == [], "stdlib fetch was called on aria2c success path"


# ---------------------------------------------------------------------------
# T2 A2: aria2c skipped when binary not detected
# ---------------------------------------------------------------------------


def test_aria2c_skipped_when_not_detected(http_server, tmp_path):
    """T2 A2: which_aria2 returns None -> run_aria2 is never invoked.

    Bug this catches: auto-detect breaks (e.g. shutil.which call removed)
    and operators without aria2c hit a regression.
    """
    http_server.serve_bytes("model.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="model.bin",
        url=f"{http_server.base_url}/model.bin",
        sha256=_sha256(SAMPLE_DATA),
    )

    aria_calls: list[tuple[str, Path, dict[str, str]]] = []

    def aria_spy(url: str, part_path: Path, headers: dict[str, str]) -> None:
        aria_calls.append((url, part_path, dict(headers)))

    download_one(
        artifact,
        tmp_path,
        which_aria2=lambda: None,
        run_aria2=aria_spy,
    )

    dest_file = tmp_path / "model.bin"
    assert dest_file.exists()
    assert dest_file.read_bytes() == SAMPLE_DATA
    assert aria_calls == [], "aria2c was invoked despite which_aria2 returning None"
    # Stdlib path served the file via the loopback http_server.
    assert len(http_server.request_log) >= 1


# ---------------------------------------------------------------------------
# T2 A5: aria2c "succeeds" but sha256 mismatches
# ---------------------------------------------------------------------------


def test_aria2c_sha256_mismatch_raises(http_server, tmp_path):
    """T2 A5: aria2c wrote bytes; sha256 verify fails; .part deleted.

    Bug this catches: aria2c "succeeds" with corrupt bytes (CDN edge bug,
    truncated response) and wrong weights end up in the cache.
    """
    http_server.serve_bytes("weights.pt", SAMPLE_DATA)
    artifact = Artifact(
        filename="weights.pt",
        url=f"{http_server.base_url}/weights.pt",
        sha256=_sha256(SAMPLE_DATA),  # expected
    )

    wrong_bytes = SAMPLE_DATA + b"trailing garbage"  # sha will mismatch

    with pytest.raises(KinoforgeError, match="sha256"):
        download_one(
            artifact,
            tmp_path,
            which_aria2=lambda: "/usr/bin/aria2c",
            run_aria2=_make_aria_stub(wrong_bytes),
        )

    dest_file = tmp_path / "weights.pt"
    part_file = Path(str(dest_file) + ".part")
    assert not dest_file.exists(), "target file must not exist after sha mismatch"
    assert not part_file.exists(), ".part must be cleaned up after sha mismatch"


# ---------------------------------------------------------------------------
# T2 A6: aria2c + sha256 None path
# ---------------------------------------------------------------------------


def test_aria2c_no_sha256_succeeds(http_server, tmp_path):
    """T2 A6: when sha256 is None, aria2c bytes are accepted as-is; second
    call is a zero-HTTP skip via the filename-based skip-path.

    Bug this catches: the sha256-verify code accidentally runs even when
    sha is None (e.g. an over-eager refactor), corrupting the no-sha
    contract.
    """
    http_server.serve_bytes("raw.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="raw.bin",
        url=f"{http_server.base_url}/raw.bin",
        sha256=None,
    )

    aria_call_count = [0]

    def counting_aria(url: str, part_path: Path, headers: dict[str, str]) -> None:
        aria_call_count[0] += 1
        part_path.write_bytes(SAMPLE_DATA)

    # First call: aria2c writes, no verify, atomic rename.
    download_one(
        artifact,
        tmp_path,
        which_aria2=lambda: "/usr/bin/aria2c",
        run_aria2=counting_aria,
    )
    dest_file = tmp_path / "raw.bin"
    assert dest_file.read_bytes() == SAMPLE_DATA
    assert aria_call_count[0] == 1

    # Second call: skip-path; aria2c MUST NOT be called.
    download_one(
        artifact,
        tmp_path,
        which_aria2=lambda: "/usr/bin/aria2c",
        run_aria2=counting_aria,
    )
    assert aria_call_count[0] == 1, "second call hit aria2c instead of skip-path"
```

- [ ] **Step 4: Run to confirm RED.**

```bash
pixi run pytest tests/core/test_downloader.py -v
```

Expected: 4 new tests FAIL with `TypeError: download_one() got an unexpected keyword argument 'which_aria2'`; 8 retrofitted tests FAIL with the same error.

- [ ] **Step 5: Wire the transport branch into `download_one`.**

In `src/kinoforge/core/downloader.py`, replace the existing `download_one` function entirely with:

```python
def download_one(
    artifact: Artifact,
    dest: Path,
    *,
    fetch: FetchCallable = _urllib_fetch,
    which_aria2: WhichCallable = _shutil_which_aria2,
    run_aria2: RunAriaCallable = _subprocess_run_aria2,
) -> Artifact:
    """Download *artifact* into *dest*, resuming from a ``.part`` file if present.

    Transport selection (added in Phase 29):
    - If ``which_aria2()`` returns a non-``None`` path, the aria2c
      subprocess is used to fetch the entire file in one shot
      (multi-connection per file, configurable via :data:`_ARIA2_BASE_ARGS`).
    - On aria2c success, the existing sha256 verify + atomic rename runs
      unchanged.
    - On aria2c failure, ``KinoforgeError`` is raised in this task; the
      silent stdlib fallback is added in T3.
    - When ``which_aria2()`` returns ``None`` (binary absent or test stub),
      the stdlib branch runs exactly as before.

    See module docstring for the rest of the skip / resume / verify
    behaviour.

    Args:
        artifact: Source descriptor; ``filename``, ``url``, and optionally
            ``sha256`` are used.
        dest: Directory into which the final file is written.
        fetch: Injectable stdlib HTTP callable (default: :func:`_urllib_fetch`).
        which_aria2: Detector callable returning the absolute path of
            ``aria2c`` or ``None`` (default: :func:`_shutil_which_aria2`).
        run_aria2: Subprocess invoker for aria2c (default:
            :func:`_subprocess_run_aria2`).

    Returns:
        A new :class:`~kinoforge.core.interfaces.Artifact` with ``uri`` set to
        the absolute path of the downloaded file.

    Raises:
        KinoforgeError: On sha256 mismatch, aria2c subprocess failure, or
            stdlib HTTP transport failure.
    """
    target_path = dest / artifact.filename
    part_path = Path(str(target_path) + ".part")

    # ------------------------------------------------------------------
    # Skip path (unchanged)
    # ------------------------------------------------------------------
    if target_path.exists():
        if artifact.sha256 is None:
            return replace(artifact, uri=str(target_path))
        if sha256_file(target_path) == artifact.sha256:
            return replace(artifact, uri=str(target_path))
        target_path.unlink()

    # ------------------------------------------------------------------
    # Transport branch
    # ------------------------------------------------------------------
    aria_path = which_aria2()
    if aria_path is not None:
        # Pre-delete any pre-existing .part so aria2c starts fresh.
        # Resume / Range stays a stdlib-branch responsibility.
        part_path.unlink(missing_ok=True)
        run_aria2(artifact.url, part_path, {})
    else:
        # Stdlib branch (unchanged from pre-Phase-29).
        req_headers: dict[str, str] = {}
        if part_path.exists():
            n = part_path.stat().st_size
            req_headers["Range"] = f"bytes={n}-"

        _status, body, _resp_headers = fetch(artifact.url, req_headers)

        if part_path.exists():
            with part_path.open("ab") as fh:
                fh.write(body)
        else:
            with part_path.open("wb") as fh:
                fh.write(body)

    # ------------------------------------------------------------------
    # Verify checksum (unchanged)
    # ------------------------------------------------------------------
    if artifact.sha256 is not None:
        actual = sha256_file(part_path)
        if actual != artifact.sha256:
            part_path.unlink(missing_ok=True)
            raise KinoforgeError(
                f"sha256 mismatch for {artifact.filename!r}: "
                f"expected {artifact.sha256}, got {actual}"
            )

    # ------------------------------------------------------------------
    # Atomic promote (unchanged)
    # ------------------------------------------------------------------
    os.replace(part_path, target_path)
    return replace(artifact, uri=str(target_path))
```

- [ ] **Step 6: Run to confirm GREEN.**

```bash
pixi run pytest tests/core/test_downloader.py -v
```

Expected: all 13 downloader tests pass (8 retrofitted + 4 new + 1 T1 symbol-lock).

- [ ] **Step 7: Commit.**

```bash
git add src/kinoforge/core/downloader.py tests/core/test_downloader.py
git commit -m "$(cat <<'EOF'
feat(core/downloader): aria2c transport branch in download_one

Adds which_aria2 + run_aria2 kwargs to download_one and inserts the
aria2c subprocess branch between the skip-path and the sha256 verify.
On aria2c success the existing verify + atomic-rename path runs
unchanged.  On aria2c failure this task raises KinoforgeError; the
silent stdlib fallback follows in T3.

Existing 8 download_one ACs retrofitted with which_aria2=_DISABLED_ARIA
to force the stdlib path; new ACs A1/A2/A5/A6 cover the aria2c path.

Spec: docs/superpowers/specs/2026-06-03-aria2c-fast-path-design.md
EOF
)"
```

---

### Task 3: Add the silent stdlib fallback on aria2c failure

**Goal:** Wrap the aria2c call in `try/except KinoforgeError`; on failure log a `WARNING`, unlink the `.part`, and fall through to the stdlib branch with `Range` semantics intact.

**Files:**
- Modify: `src/kinoforge/core/downloader.py` (the transport-branch block in `download_one` only)
- Modify: `tests/core/test_downloader.py` (add 2 new ACs)

**Acceptance Criteria:**
- [ ] A3: when `run_aria2` raises `KinoforgeError`, stdlib `fetch` is called next, the final file is correct, and a `WARNING` log record is emitted whose message contains `"aria2c"` and `"fallback"` substrings.
- [ ] A4: when `run_aria2` writes garbage bytes to `.part` AND raises, the fallback's stdlib `fetch` is called WITHOUT a `Range` header (proving the poisoned `.part` was unlinked before fallback) and the final file is correct.

**Verify:** `pixi run pytest tests/core/test_downloader.py -v` → all tests pass; suite size 1041 → 1043 (two new ACs).

**Steps:**

- [ ] **Step 1: Write the 2 RED tests.**

Append to `tests/core/test_downloader.py`:

```python
# ---------------------------------------------------------------------------
# T3 A3: aria2c failure falls back to stdlib + WARNING log
# ---------------------------------------------------------------------------


def test_aria2c_failure_falls_back_to_stdlib(http_server, tmp_path, caplog):
    """T3 A3: aria2c raises -> WARNING logged, stdlib fetch produces the file.

    Bug this catches: subprocess failure becomes a hard error and operators
    lose their existing single-connection download path.
    """
    http_server.serve_bytes("model.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="model.bin",
        url=f"{http_server.base_url}/model.bin",
        sha256=_sha256(SAMPLE_DATA),
    )

    with caplog.at_level("WARNING", logger="kinoforge.core.downloader"):
        result = download_one(
            artifact,
            tmp_path,
            which_aria2=lambda: "/usr/bin/aria2c",
            run_aria2=_failing_aria("simulated aria2c exit 22"),
        )

    dest_file = tmp_path / "model.bin"
    assert dest_file.exists()
    assert dest_file.read_bytes() == SAMPLE_DATA
    assert result.uri == str(dest_file)

    # WARNING log records contain the contract substrings.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings, "expected a WARNING log on aria2c fallback"
    msg = warnings[0].getMessage()
    assert "aria2c" in msg, f"WARNING message missing 'aria2c': {msg!r}"
    assert "fallback" in msg.lower(), f"WARNING message missing 'fallback': {msg!r}"


# ---------------------------------------------------------------------------
# T3 A4: aria2c failure unlinks poisoned .part before fallback
# ---------------------------------------------------------------------------


def test_aria2c_failure_unlinks_part_before_fallback(http_server, tmp_path):
    """T3 A4: a failed aria2c that wrote garbage bytes must NOT cause the
    stdlib fallback to Range-resume off the garbage prefix.

    Bug this catches: the fallback path inherits the poisoned .part,
    producing a corrupt assembled file that fails sha256 verify on every
    retry forever.
    """
    http_server.serve_bytes("model.bin", SAMPLE_DATA)
    artifact = Artifact(
        filename="model.bin",
        url=f"{http_server.base_url}/model.bin",
        sha256=_sha256(SAMPLE_DATA),
    )

    # Snapshot the loopback server's request log so we can detect any
    # Range header sent by the fallback.
    pre_count = len(http_server.request_log)

    download_one(
        artifact,
        tmp_path,
        which_aria2=lambda: "/usr/bin/aria2c",
        run_aria2=_aria_writing_garbage_then_failing(),
    )

    dest_file = tmp_path / "model.bin"
    part_file = Path(str(dest_file) + ".part")
    assert dest_file.exists()
    assert dest_file.read_bytes() == SAMPLE_DATA, (
        "fallback produced corrupt bytes — likely Range-resumed off the "
        "garbage prefix from the failed aria2c run"
    )
    assert not part_file.exists()

    # Fallback request MUST NOT carry a Range header (no resume off garbage).
    new_requests = http_server.request_log[pre_count:]
    range_requests = [entry for entry in new_requests if entry[2].startswith("bytes=")]
    assert range_requests == [], (
        "fallback sent a Range header — poisoned .part was not unlinked"
    )
```

- [ ] **Step 2: Run to confirm RED.**

```bash
pixi run pytest tests/core/test_downloader.py::test_aria2c_failure_falls_back_to_stdlib tests/core/test_downloader.py::test_aria2c_failure_unlinks_part_before_fallback -v
```

Expected: both FAIL — first raises `KinoforgeError` instead of falling back; second also raises `KinoforgeError`.

- [ ] **Step 3: Wrap the aria2c call with try/except + WARNING + unlink.**

In `src/kinoforge/core/downloader.py`, replace the transport-branch block inside `download_one` (the block introduced in T2 starting at `aria_path = which_aria2()`) with:

```python
    # ------------------------------------------------------------------
    # Transport branch
    # ------------------------------------------------------------------
    aria_path = which_aria2()
    aria_succeeded = False
    if aria_path is not None:
        # Pre-delete any pre-existing .part so aria2c starts fresh.
        # Resume / Range stays a stdlib-branch responsibility.
        part_path.unlink(missing_ok=True)
        try:
            run_aria2(artifact.url, part_path, {})
        except KinoforgeError as exc:
            logger.warning(
                "aria2c failed for %s: %s; falling back to stdlib",
                artifact.filename,
                exc,
            )
            # Discard any partial bytes aria2c wrote before raising so
            # the stdlib branch does not Range-resume off poisoned data.
            part_path.unlink(missing_ok=True)
        else:
            aria_succeeded = True

    if not aria_succeeded:
        # Stdlib branch (unchanged from pre-Phase-29).
        req_headers: dict[str, str] = {}
        if part_path.exists():
            n = part_path.stat().st_size
            req_headers["Range"] = f"bytes={n}-"

        _status, body, _resp_headers = fetch(artifact.url, req_headers)

        if part_path.exists():
            with part_path.open("ab") as fh:
                fh.write(body)
        else:
            with part_path.open("wb") as fh:
                fh.write(body)
```

The verify-path and atomic-rename code below is unchanged from T2.

- [ ] **Step 4: Run to confirm GREEN.**

```bash
pixi run pytest tests/core/test_downloader.py -v
```

Expected: all 15 downloader tests pass (1 T1 + 8 retrofitted + 4 T2 + 2 T3).

- [ ] **Step 5: Commit.**

```bash
git add src/kinoforge/core/downloader.py tests/core/test_downloader.py
git commit -m "$(cat <<'EOF'
feat(core/downloader): silent stdlib fallback on aria2c failure

Wraps run_aria2 in try/except KinoforgeError; on failure logs a WARNING
and unlinks the (possibly-poisoned) .part before falling through to the
stdlib branch.  Range-resume on the stdlib branch is preserved for
genuine retry scenarios; the unlink-before-fallback step guarantees a
failed aria2c run cannot poison the next transport.

ACs A3 + A4 added.

Spec: docs/superpowers/specs/2026-06-03-aria2c-fast-path-design.md
EOF
)"
```

---

### Task 4: Forward seams through `download_all` + A7

**Goal:** Add `which_aria2` and `run_aria2` kwargs to `download_all` with the same defaults, forward them to every `pool.submit(download_one, …)` call, retrofit the existing `download_all` test, and add A7 (per-artifact aria2c invocation).

**Files:**
- Modify: `src/kinoforge/core/downloader.py` (the `download_all` function only)
- Modify: `tests/core/test_downloader.py` (retrofit `test_download_all_concurrent`; add A7)

**Acceptance Criteria:**
- [ ] `download_all(artifacts, dest, max_workers=4, which_aria2=lambda: None)` keeps the existing AC6 behavior — file-level concurrency unchanged.
- [ ] A7: 3 artifacts + `which_aria2` returning a path + a spying `run_aria2` → spy is called 3 times, stdlib `fetch` is called zero times, all 3 files match their payloads.

**Verify:** `pixi run pytest tests/core/test_downloader.py -v` → all tests pass; suite size 1043 → 1044 (one new AC; existing AC6 retrofitted in place).

**Steps:**

- [ ] **Step 1: Write the RED test for A7.**

Append to `tests/core/test_downloader.py`:

```python
# ---------------------------------------------------------------------------
# T4 A7: download_all forwards the seams to every download_one call
# ---------------------------------------------------------------------------


def test_download_all_uses_aria2c_per_artifact(http_server, tmp_path):
    """T4 A7: download_all forwards which_aria2 + run_aria2 so every
    artifact takes the aria2c path; stdlib fetch is never called.

    Bug this catches: download_all forgets to forward the seams kwargs
    and the parallel-files path silently degrades to stdlib transport.
    """
    names = [f"file_{i:02d}.bin" for i in range(3)]
    payloads = {name: os.urandom(4096) for name in names}
    for name, data in payloads.items():
        http_server.serve_bytes(name, data)

    artifacts = [
        Artifact(
            filename=name,
            url=f"{http_server.base_url}/{name}",
            sha256=_sha256(data),
        )
        for name, data in payloads.items()
    ]

    aria_calls: list[tuple[str, Path, dict[str, str]]] = []

    def fan_aria(url: str, part_path: Path, headers: dict[str, str]) -> None:
        # Pick the payload by URL suffix — matches what aria2c-on-real-CDN
        # would do.
        for name, data in payloads.items():
            if url.endswith(name):
                aria_calls.append((url, part_path, dict(headers)))
                part_path.write_bytes(data)
                return
        raise AssertionError(f"unexpected URL in aria2c stub: {url!r}")

    stdlib_calls: list[tuple[str, dict[str, str]]] = []

    def stdlib_spy(url: str, headers: dict[str, str]):
        stdlib_calls.append((url, dict(headers)))
        raise AssertionError("stdlib fetch must not be called in A7")

    results = download_all(
        artifacts,
        tmp_path,
        max_workers=4,
        fetch=stdlib_spy,
        which_aria2=lambda: "/usr/bin/aria2c",
        run_aria2=fan_aria,
    )

    assert len(results) == 3
    assert len(aria_calls) == 3, f"expected 3 aria2c calls, got {len(aria_calls)}"
    assert stdlib_calls == [], "stdlib fetch was called despite aria2c success"
    for i, (name, data) in enumerate(payloads.items()):
        dest_file = tmp_path / name
        assert dest_file.read_bytes() == data
        assert results[i].uri == str(dest_file)
```

- [ ] **Step 2: Retrofit `test_download_all_concurrent` to force stdlib.**

In `tests/core/test_downloader.py`, change the `download_all(...)` call inside `test_download_all_concurrent` from:

```python
    results = download_all(artifacts, tmp_path, max_workers=4)
```

to:

```python
    results = download_all(
        artifacts,
        tmp_path,
        max_workers=4,
        which_aria2=_DISABLED_ARIA,
    )
```

- [ ] **Step 3: Run to confirm RED.**

```bash
pixi run pytest tests/core/test_downloader.py::test_download_all_uses_aria2c_per_artifact tests/core/test_downloader.py::test_download_all_concurrent -v
```

Expected: both FAIL — `TypeError: download_all() got an unexpected keyword argument 'which_aria2'`.

- [ ] **Step 4: Forward the seams through `download_all`.**

In `src/kinoforge/core/downloader.py`, replace the existing `download_all` function entirely with:

```python
def download_all(
    artifacts: list[Artifact],
    dest: Path,
    *,
    max_workers: int = 4,
    fetch: FetchCallable = _urllib_fetch,
    which_aria2: WhichCallable = _shutil_which_aria2,
    run_aria2: RunAriaCallable = _subprocess_run_aria2,
) -> list[Artifact]:
    """Download multiple artifacts concurrently.

    File-level concurrency is provided by a
    :class:`concurrent.futures.ThreadPoolExecutor` with *max_workers*
    threads.  Per-file connection-level concurrency is the
    responsibility of the chosen transport (aria2c uses
    :data:`_ARIA2_BASE_ARGS` ``-x16 -s16``; the stdlib transport uses
    one connection per file).

    Args:
        artifacts: List of artifacts to download.
        dest: Common destination directory.
        max_workers: Maximum number of concurrent download threads.
        fetch: Injectable stdlib HTTP callable forwarded to each
            :func:`download_one` call.
        which_aria2: Detector callable forwarded to each
            :func:`download_one` call.
        run_aria2: Subprocess invoker forwarded to each
            :func:`download_one` call.

    Returns:
        List of updated :class:`~kinoforge.core.interfaces.Artifact`
        instances (one per input, in input order) with ``uri`` set to
        the absolute on-disk path.

    Raises:
        KinoforgeError: Propagated from any failing
            :func:`download_one` call.
    """
    results: list[Artifact] = [cast(Artifact, None)] * len(artifacts)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {
            pool.submit(
                download_one,
                artifact,
                dest,
                fetch=fetch,
                which_aria2=which_aria2,
                run_aria2=run_aria2,
            ): idx
            for idx, artifact in enumerate(artifacts)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            results[idx] = future.result()

    return results
```

- [ ] **Step 5: Run to confirm GREEN.**

```bash
pixi run pytest tests/core/test_downloader.py -v
```

Expected: all 16 downloader tests pass.

- [ ] **Step 6: Commit.**

```bash
git add src/kinoforge/core/downloader.py tests/core/test_downloader.py
git commit -m "$(cat <<'EOF'
feat(core/downloader): forward aria2c seams through download_all

download_all gains which_aria2 + run_aria2 kwargs with the same defaults
as download_one; each pool.submit(download_one, ...) forwards them
explicitly so the concurrent fan-out cannot silently downgrade to
stdlib transport.

AC A7 added; existing AC6 retrofitted with which_aria2=_DISABLED_ARIA.

Spec: docs/superpowers/specs/2026-06-03-aria2c-fast-path-design.md
EOF
)"
```

---

### Task 5: README + PROGRESS docs

**Goal:** Add a "Faster downloads (aria2c)" sub-section to README.md and a "Phase 29 — aria2c fast-path" entry to PROGRESS.md per the established convention.

**Files:**
- Modify: `README.md`
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] README has a sub-section that names `aria2c`, the `apt`/`brew`/`choco` install lines, the auto-detect behavior, and the silent-fallback contract.
- [ ] PROGRESS Phase 29 entry exists with per-task SHAs, test-count delta (1036 → 1044), GH #9 closure trailer, and the standard "Key design decisions" block.
- [ ] `pixi run pytest tests/test_examples.py tests/test_source_audit.py -v` still passes (README + spec doc do not introduce credential-shaped literals).

**Verify:** `pixi run pytest tests/test_source_audit.py -v` → PASS; `pixi run pytest -v` → 1044 passed.

**Steps:**

- [ ] **Step 1: Add the README sub-section.**

In `README.md`, find the existing "Provisioning" heading (or the closest equivalent that describes model fetches). Insert a new sub-section immediately after it:

```markdown
### Faster downloads (aria2c)

kinoforge auto-detects `aria2c` on `PATH` and uses it as a transparent
multi-connection fast-path for every model fetch. With aria2c installed
on a typical residential link, the Wan 2.1 weight set (~9 GiB total)
downloads in roughly one-tenth the wall-clock time it takes via the
stdlib transport.

Install:
- Debian / Ubuntu: `sudo apt install aria2`
- macOS (Homebrew): `brew install aria2`
- Windows (Chocolatey): `choco install aria2`

No configuration is required. If aria2c is absent, or if the subprocess
fails for any reason (CDN rate-limit, transient network error,
unexpected flag deprecation in a future aria2c release), the failure is
logged at `WARNING` level and the stdlib single-connection path is used
as a fallback — operators always get the file.
```

- [ ] **Step 2: Add the PROGRESS Phase 29 entry.**

In `PROGRESS.md`, append the following entry directly after the existing "Phase 28 — Layer P close-out" block (use the actual commit SHAs captured by T1-T4 once those commits exist; the executor fills these in during Step 4 below before committing this task):

```markdown
### Phase 29 — aria2c fast-path (GitHub issue #9)

Single-file change to `src/kinoforge/core/downloader.py` that auto-detects
the `aria2c` system binary and uses it as a transparent multi-connection
fast-path on every model fetch.  Silent stdlib fallback on subprocess
failure preserves the existing single-connection path as a safety net.

- Spec: `docs/superpowers/specs/2026-06-03-aria2c-fast-path-design.md`
- Plan: `docs/superpowers/plans/2026-06-03-aria2c-fast-path.md`
- T1 (seams + types + helpers + logger + drop DEFERRED): `<T1-SHA>`
- T2 (download_one transport branch + 4 ACs): `<T2-SHA>`
- T3 (silent fallback + WARNING log + 2 ACs): `<T3-SHA>`
- T4 (download_all forwarding + A7): `<T4-SHA>`
- T5 (README + PROGRESS): `<T5-SHA>`

**Key design decisions:**
- Auto-detect by `shutil.which("aria2c")` per call (Q1=A): zero ceremony;
  tests inject `which_aria2=lambda: None` to force the stdlib path.
- Silent fallback to stdlib on aria2c failure with `WARNING` log (Q2=A):
  operators always get the file; lost wall-clock is the only cost.
- Injectable `run_aria2` + `which_aria2` callables (Q3=A): mirrors the
  existing `fetch` seam pattern; no monkey-patching of `shutil` or
  `subprocess` in tests.
- Hard-coded knobs `-x 16 -s 16 -k 1M --max-tries=3 --retry-wait=2`
  (Q5=A): battle-tested HF / CivitAI defaults; tuning is YAGNI.
- Keep post-download `sha256_file()` verify; do NOT use aria2c's
  `--checksum=` flag (Q7=A): single checksum code path for both
  transports.
- `--header=` passthrough mechanism shipped, population deferred (Q6=A):
  `Artifact` has no `headers` field yet, so the aria2c branch passes
  `headers={}`.  The seam contract is final; populating it is a one-line
  follow-up when (and if) `Artifact.headers` is added.

**Test count:** 1036 (post-Layer-P) → 1044 (post-Phase-29).  Delta: +8 net
new (A1-A7 + the T1 symbol-lock test).

**Out of scope (carry-forward):**
- Real-binary smoke test (`KINOFORGE_LIVE_ARIA2=1`).
- `Artifact.headers` field for HF-gated weights via
  `Authorization: Bearer hf_…`.
- aria2c knobs via env-var / YAML config.
- aria2c's `--checksum=` flag as a verify short-circuit.

Closes GH #9.
```

Also update the "GitHub issues status" table in PROGRESS.md: change the row for `#9` from `Open` to `CLOSED (Phase 29)`.

- [ ] **Step 3: Run the source-audit + examples gate.**

```bash
pixi run pytest tests/test_source_audit.py tests/test_examples.py -v
```

Expected: PASS. (The README addition does not introduce credential-shaped literals; the Phase 29 entry only mentions `hf_…` as a prose ellipsis, which the audit's `\bhf_[A-Za-z0-9]{32,}\b` regex does not match.)

- [ ] **Step 4: Backfill the per-task SHAs into the PROGRESS entry.**

```bash
git log --oneline -10
```

Identify the four commits from T1-T4 (the most-recent four `feat(core/downloader): …` and `refactor(core/downloader): …` commits) and replace the four `<T1-SHA>` / `<T2-SHA>` / `<T3-SHA>` / `<T4-SHA>` placeholders in PROGRESS.md with the actual short SHAs.

- [ ] **Step 5: Commit.**

```bash
git add README.md PROGRESS.md
git commit -m "$(cat <<'EOF'
docs: Phase 29 — aria2c fast-path (README + PROGRESS)

Closes GH #9.

Spec: docs/superpowers/specs/2026-06-03-aria2c-fast-path-design.md
Plan: docs/superpowers/plans/2026-06-03-aria2c-fast-path.md
EOF
)"
```

- [ ] **Step 6: Self-SHA the T5 entry.**

```bash
git log --oneline -1
```

Capture this commit's SHA; replace the `<T5-SHA>` placeholder in PROGRESS.md with it, then amend:

```bash
git add PROGRESS.md
git commit --amend --no-edit
```

---

### Task 6: Whole-suite gate + push

**Goal:** Run the full test suite, confirm 1044 passing (or the actual current count plus +8), then push the Phase 29 commits to `origin/main` per the established direct-to-main pattern (no merge commit, no `--no-ff`, no feature branch — Phases 26 and 28 set this precedent).

**Files:**
- No file changes in this task (gate + push only).

**Acceptance Criteria:**
- [ ] `pixi run test` returns exit 0 with `1044 passed` (or pre-Phase-29-count + 8 = expected total).
- [ ] `git push origin main` succeeds.
- [ ] `git log origin/main --oneline -10` shows the five Phase 29 commits.

**Verify:** `pixi run test` then `git push origin main` then `git log origin/main --oneline -10`.

**Steps:**

- [ ] **Step 1: Run the full test suite.**

```bash
pixi run test
```

Expected: `1044 passed, 3 skipped` (or the exact pre-existing skip count). If anything fails, do NOT push — debug, fix in a follow-up commit, then re-run.

- [ ] **Step 2: Run pre-commit on all files as a final formatting / type gate.**

```bash
pixi run pre-commit run --all-files
```

Expected: all hooks pass (ruff, ruff-format, mypy, trailing whitespace, etc.).

- [ ] **Step 3: Push to origin.**

```bash
git push origin main
```

Expected: five new commits land on `origin/main` (T1, T2, T3, T4, T5).

- [ ] **Step 4: Confirm remote state.**

```bash
git log origin/main --oneline -10
git status
```

Expected: working tree clean; `origin/main` at the same SHA as local `main`; T1-T5 commits visible in the log.

---

## Self-Review

**1. Spec coverage:**
- §1 Q1 (auto-detect): covered in T2 step 5 — `aria_path = which_aria2()` is read per call inside `download_one`.
- §1 Q2 (silent fallback): covered in T3 step 3 — `try/except KinoforgeError` + `logger.warning(...)`.
- §1 Q3 (injectable seams): covered in T1 + T2 + T4 — `WhichCallable` / `RunAriaCallable` as kwargs.
- §1 Q4 (no size gate): covered implicitly — neither T2 nor T3 introduces a size check.
- §1 Q5 (hard-coded knobs): covered in T1 step 3 — `_ARIA2_BASE_ARGS` tuple is module-level constant.
- §1 Q6 (header passthrough mechanism shipped, population deferred): covered in T1 step 4 (`_subprocess_run_aria2` builds `--header` flags) + T2 step 5 (`run_aria2(artifact.url, part_path, {})` passes empty dict).
- §1 Q7 (post-download sha256 verify, no `--checksum=` flag): covered — `_ARIA2_BASE_ARGS` does not include `--checksum=`; the verify path in T2 step 5 is unchanged from pre-Phase-29.
- §2 Architecture (single-file change, no new module): covered — only `src/kinoforge/core/downloader.py` is modified for the source code; `tests/core/test_downloader.py`, `README.md`, and `PROGRESS.md` are doc / test changes.
- §3 Components & contracts: every code block in the spec is reproduced verbatim in T1-T4.
- §4 Data flow: matches T2 + T3 step-by-step.
- §5 Error handling: covered by A3 (WARNING + fallback), A4 (unlink before fallback), A5 (sha mismatch raise), and the unchanged corrupt-`.part` AC #5.
- §6 Testing — A1: T2 step 3 ✓ — A2: T2 step 3 ✓ — A3: T3 step 1 ✓ — A4: T3 step 1 ✓ — A5: T2 step 3 ✓ — A6: T2 step 3 ✓ — A7: T4 step 1 ✓.
- §7 Documentation: covered in T5.
- §8 Out of scope: not implemented (by definition); each item is listed in the T5 PROGRESS entry's "Out of scope (carry-forward)" block.
- §10 Implementation outline (6 tasks): T1-T6 here is a 1-to-1 mapping.

**2. Placeholder scan:** four `<TN-SHA>` placeholders in T5 step 2 are explicitly resolved in T5 step 4 (backfill) and T5 step 6 (self-SHA + amend). No other placeholders.

**3. Type consistency:**
- `WhichCallable = Callable[[], str | None]` — used consistently in T1, T2, T4.
- `RunAriaCallable = Callable[[str, Path, dict[str, str]], None]` — used consistently in T1, T2, T3, T4.
- `download_one(..., which_aria2=..., run_aria2=...)` — same parameter names in T2 step 5 and T4 step 4.
- `download_all(..., which_aria2=..., run_aria2=...)` — same parameter names in T4 step 4.
- Test helper function names (`_make_aria_stub`, `_failing_aria`, `_spying_aria`, `_aria_writing_garbage_then_failing`) are introduced in T2 step 1 and reused in T3 step 1 and T4 step 1 — no rename drift.

No issues found. Plan is ready for execution.
