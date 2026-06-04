# aria2c fast-path on the downloader (GitHub issue #9)

**Date:** 2026-06-03
**Layer label:** post-Layer-P #1 candidate
**Scope:** single-file change to `src/kinoforge/core/downloader.py` + 7 new tests.
**Issue:** GH #9 — aria2c fast-path on the downloader.
**Motivation:** stdlib `urllib` is single-connection per file. Wan i2v
weights (~5 GiB GGUF, ~3 GiB VAE, ~1 GiB text-encoder) from HuggingFace and
CivitAI CDNs are throttled per-connection. aria2c with `-x 16 -s 16` saturates
available bandwidth (10×+ speedup is typical on residential connections, 3-5×
on data-center links). Every future live Wan run pays this tax until the
fast-path lands.

---

## 1. Decisions locked during brainstorming

| Q | Topic | Decision | Rationale |
|---|---|---|---|
| Q1 | Activation | Auto-detect via `shutil.which("aria2c")` per call | Zero ceremony; immediate payoff for operators with aria2c on PATH; tests inject a stub that returns `None` to exercise the stdlib branch. |
| Q2 | Subprocess failure | Silent fallback to stdlib + `WARNING` log | Operator still gets the file; lost wall-time is the only cost. Matches Layer P's defence-in-depth posture. |
| Q3 | Test seam | Two injectable callables: `which_aria2: WhichCallable` and `run_aria2: RunAriaCallable` | Mirrors the existing `fetch` seam pattern; no monkey-patching of `shutil` / `subprocess` across the test file. |
| Q4 | Size gate | None — aria2c used for every artifact when detected | Subprocess fork overhead is ~50 ms; rounding error against multi-GiB transfers. YAGNI. |
| Q5 | Knob source | Hard-coded defaults (no env-var, no YAML) | `-x 16 -s 16 -k 1M` is battle-tested for HF / CivitAI; future tuning is a future layer. |
| Q6 | Auth headers | `--header='K: V'` passthrough mechanism shipped; population deferred | `Artifact` does not yet carry headers; the seam contract is final so populating it later is one-line. Public Wan weights need no auth; CivitAI's `?api_key=` rides in the URL. |
| Q7 | sha256 verify | Keep existing post-download `sha256_file()`; do NOT use aria2c's `--checksum=` flag | Single checksum code path for both transports; corrupt-`.part` recovery contract stays uniform. |

---

## 2. Architecture

Single-file change to `src/kinoforge/core/downloader.py`. No new module. No
new Python dependency (aria2c is a system binary, detected via `shutil.which`).
Public API of `download_one` and `download_all` is unchanged at the call
site — both functions gain two new kwargs (`which_aria2`, `run_aria2`) with
production defaults, so every existing call site keeps working unchanged.

Transport branch lives **between** the skip-path early-return and the
sha256-verify path. The aria2c branch writes the entire file to `<target>.part`
via one subprocess call; success → fall through to the existing sha256 verify
+ atomic rename. Failure → log `WARNING`, unlink the (possibly partial)
`.part`, fall through to the existing stdlib fetch branch with its Range-resume
semantics intact.

`download_all` forwards both new kwargs to every `pool.submit(download_one, …)`
call. The file-level `ThreadPoolExecutor` is preserved unchanged; per-file
parallelism is now aria2c's job (`-x 16 -s 16`) instead of being absent.

---

## 3. Components & contracts

### 3.1 New type aliases

```python
WhichCallable = Callable[[], str | None]
RunAriaCallable = Callable[[str, Path, dict[str, str]], None]
```

### 3.2 New default seams

```python
def _shutil_which_aria2() -> str | None:
    return shutil.which("aria2c")


_ARIA2_BASE_ARGS: tuple[str, ...] = (
    "-x", "16",
    "-s", "16",
    "-k", "1M",
    "--file-allocation=none",
    "--max-tries=3",
    "--retry-wait=2",
    "--allow-overwrite=true",
    "--auto-file-renaming=false",
    "--summary-interval=0",
    "--console-log-level=warn",
)


def _subprocess_run_aria2(
    url: str,
    part_path: Path,
    headers: dict[str, str],
) -> None:
    """Spawn aria2c to download `url` → `part_path`.

    Args:
        url: Source URL.
        part_path: Target .part file (parent dir must exist).
        headers: HTTP headers (e.g. {"Authorization": "Bearer ..."}).

    Raises:
        KinoforgeError: On non-zero exit, missing binary, or wall-clock timeout.
    """
    header_args: list[str] = []
    for key, value in headers.items():
        header_args.extend(["--header", f"{key}: {value}"])
    cmd = [
        "aria2c",
        *_ARIA2_BASE_ARGS,
        "-d", str(part_path.parent),
        "-o", part_path.name,
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

### 3.3 Updated `download_one` signature

```python
def download_one(
    artifact: Artifact,
    dest: Path,
    *,
    fetch: FetchCallable = _urllib_fetch,
    which_aria2: WhichCallable = _shutil_which_aria2,
    run_aria2: RunAriaCallable = _subprocess_run_aria2,
) -> Artifact:
```

### 3.4 Updated `download_all` signature

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
```

Each `pool.submit(...)` call forwards `which_aria2` and `run_aria2` as kwargs
to `download_one`.

### 3.5 `run_aria2` contract

- **Input:** `url` (fully-qualified), `part_path` (parent dir exists; aria2c
  must write to exactly this path), `headers` (zero or more HTTP headers).
- **Success:** `part_path` exists on disk with all expected bytes. The caller
  (`download_one`) does sha256 verify next.
- **Failure:** raises `KinoforgeError`. The caller deletes any partial `.part`
  then falls back to the stdlib branch.
- **Resume disabled at the aria2c layer:** the caller pre-deletes any
  pre-existing `.part` before calling `run_aria2`, so the contract is
  "always full fresh download". Resume / Range stays the stdlib branch's
  responsibility — single resume implementation across the codebase.

---

## 4. Data flow inside `download_one`

```
target_path.exists()?
├─ YES → sha256 None or matches?
│        ├─ YES → return (uri = target_path)        [skip path, no HTTP]
│        └─ NO  → unlink target_path; continue
│
└─ NO  → continue
                                              │
                                              ▼
              which_aria2() returns a path?
              ├─ YES (string) ─────────────────────────────────┐
              │                                                │
              │   part_path.unlink(missing_ok=True)            │
              │   try:                                         │
              │       run_aria2(url, part_path, headers={})    │
              │   except KinoforgeError as exc:                │
              │       logger.warning("aria2c failed for %s: %s; falling back to stdlib", filename, exc)
              │       part_path.unlink(missing_ok=True)        │
              │       → fall through to STDLIB BRANCH          │
              │   else:                                        │
              │       → jump to SHA256 VERIFY                  │
              │                                                │
              └─ NO (None) ──→ STDLIB BRANCH ──────────────────┘
                                              │
              STDLIB BRANCH (unchanged from today):
              part_path.exists() → req_headers["Range"] = f"bytes={n}-"
              status, body, _ = fetch(url, req_headers)
              if part_path exists: append; else: write
                                              │
                                              ▼
              SHA256 VERIFY (unchanged):
              if artifact.sha256 set:
                  actual = sha256_file(part_path)
                  if actual != artifact.sha256:
                      part_path.unlink(missing_ok=True)
                      raise KinoforgeError(...)
                                              │
                                              ▼
              os.replace(part_path, target_path)
              return Artifact(uri=str(target_path))
```

**Header source today:** the stdlib branch builds `req_headers` containing
only `Range` (no auth). `Artifact` has no `headers` field — so the aria2c
branch passes `headers={}` for now. The `--header='K: V'` *mechanism* in
`_subprocess_run_aria2` is shipped (so the seam contract is final). Populating
it lands when (and if) `Artifact.headers` is added; that work is out of scope
for this layer.

---

## 5. Error handling

| Failure | Branch | Action |
|---|---|---|
| `which_aria2()` returns `None` | aria2c not installed | Skip to stdlib branch, no log. |
| `run_aria2` raises `KinoforgeError` (non-zero exit / OSError / timeout) | aria2c attempted, failed | `logger.warning("aria2c failed for %s: %s; falling back to stdlib", filename, exc)`; `part_path.unlink(missing_ok=True)`; fall through to stdlib branch. |
| stdlib `fetch` raises after aria2c fallback | both transports failed | Propagate `KinoforgeError` unchanged; `.part` stays unlinked from the fallback handoff. |
| sha256 verify fails after aria2c success | bytes corrupt | Existing path: unlink `.part`, raise `KinoforgeError`. No automatic retry. Next caller invocation retries fresh — likely picks aria2c again. AC #5 (corrupt-`.part`) contract unchanged. |
| aria2c writes wrong file name | unreachable in production (`-o` pins it); paranoia check | Caught at sha256 verify stage if sha is set; same path as corrupt-bytes. With sha unset, the stdlib skip-path on the next call would not find `target_path` and would re-download. |

**Logger:** add `logger = logging.getLogger(__name__)` at the top of the
module if not already present. `import logging` already used elsewhere in
`kinoforge.core`; the project convention is `logging.getLogger(__name__)`.

**Invariants preserved:**
- `.part` is deleted on every error exit; never leaked.
- Final `target_path` only appears via `os.replace(part_path, target_path)`
  after sha256 verify (or after the sha-None branch). Atomic write contract
  unchanged.
- Existing 8 ACs in `tests/core/test_downloader.py` keep passing unchanged
  with no source edits — they default `which_aria2` to a stub returning
  `None`, exercising the stdlib branch as before.

---

## 6. Testing

### 6.1 New ACs (7)

| AC | Test name | Behavior under test | Bug it catches |
|---|---|---|---|
| A1 | `test_aria2c_used_when_detected` | `which_aria2()` returns `/usr/bin/aria2c`, `run_aria2` writes correct bytes to `.part`. No stdlib `fetch` call; result file matches; `.part` removed; `uri` set. | Fast-path silently skipped — degraded perf, ignored by tests. |
| A2 | `test_aria2c_skipped_when_not_detected` | `which_aria2()` returns `None`. Stdlib `fetch` called exactly once; `run_aria2` spy never called. | Auto-detect break: operators without aria2c hit a regression. |
| A3 | `test_aria2c_failure_falls_back_to_stdlib` | `run_aria2` raises `KinoforgeError`. Stdlib `fetch` called next; final file correct; `WARNING` logged via `caplog`. | Silent total failure when aria2c is broken / mis-configured. |
| A4 | `test_aria2c_failure_unlinks_part_before_fallback` | `run_aria2` writes garbage to `.part` then raises. Fallback must produce a correct final file — meaning it did **not** Range-resume off the garbage prefix. | Fallback inherits aria2c's bad bytes → silent sha256 mismatch loop. |
| A5 | `test_aria2c_sha256_mismatch_raises` | `run_aria2` writes bytes whose sha256 does not match `artifact.sha256`. `KinoforgeError` raised with `"sha256"` substring; `.part` deleted; `target_path` does not exist. | aria2c "succeeds" with corrupt bytes — wrong weights end up in the cache. |
| A6 | `test_aria2c_no_sha256_succeeds` | `artifact.sha256 = None` + aria2c writes bytes. Final file matches; no verify; idempotent skip on the next call. | sha256 verify code leaks into the sha-None branch. |
| A7 | `test_download_all_uses_aria2c_per_artifact` | 3 artifacts, `run_aria2` spy counts calls. Expect `run_aria2` called 3 times, stdlib `fetch` zero times. | `download_all` forgets to forward the seams — silent degradation. |

### 6.2 Existing 8 ACs

Keep passing unchanged. The tests use the public `download_one` /
`download_all` API; they need a one-line addition: pass
`which_aria2=lambda: None` (or a shared `_DISABLED_ARIA = lambda: None`
constant) to every call so the auto-detect branch is forced off during the
stdlib AC suite. The default seam stays "auto-detect", which is correct
production behavior; tests explicitly opt out.

### 6.3 Helper stubs

```python
def _make_aria_stub(bytes_to_write: bytes) -> RunAriaCallable:
    """Build a run_aria2 stub that writes `bytes_to_write` to part_path."""
    def stub(url: str, part_path: Path, headers: dict[str, str]) -> None:
        part_path.write_bytes(bytes_to_write)
    return stub


def _failing_aria(exc_msg: str = "boom") -> RunAriaCallable:
    """Build a run_aria2 stub that always raises."""
    def stub(url: str, part_path: Path, headers: dict[str, str]) -> None:
        raise KinoforgeError(exc_msg)
    return stub


def _spying_aria(record: list[tuple[str, Path, dict[str, str]]],
                 bytes_to_write: bytes) -> RunAriaCallable:
    """Stub that records every call and writes correct bytes."""
    def stub(url: str, part_path: Path, headers: dict[str, str]) -> None:
        record.append((url, part_path, dict(headers)))
        part_path.write_bytes(bytes_to_write)
    return stub
```

### 6.4 Test-design notes (per `test-design` skill)

- WARNING log message in A3 is a behavioral contract — assert via `caplog`
  fixture, not by inspecting module-internal state.
- aria2c-success tests must verify `.part` content via the injected
  `run_aria2` writing **specific bytes** — not by mocking `sha256_file`.
  Real bytes → real sha256 → real verify. The seam is what's faked, not the
  hash function.
- A4 explicitly catches the failure-mode where the fallback inherits a
  poisoned `.part` from a failed aria2c run. This is the most-likely silent
  regression and the most-valuable bug-catch test in the layer.

### 6.5 Test count delta

8 (current) → 15 (post). Whole-suite delta: 1036 → 1043 passing.

---

## 7. Documentation

- README: new "Faster downloads" sub-section under the "Provisioning" /
  "Models" heading. Two sentences: "If `aria2c` is on PATH, kinoforge uses
  it transparently. Install via `apt install aria2` / `brew install aria2`
  / `choco install aria2`. No config required." Plus a one-line warning
  for the fall-back path: "Failures are logged at WARNING level and the
  stdlib path is used as a fallback."
- PROGRESS: new sub-entry under "Phase 28 — Layer P close-out" tail or a
  new "Phase 29 — aria2c fast-path" entry, per the layer-numbering
  convention already established. Pick the latter; it's a self-contained
  layer.
- `pyproject.toml`: no change. aria2c is a system binary, not a Python dep.
- `.env.example`: no change. Q1 ruled out env-var activation.

---

## 8. Out of scope

- Real-binary smoke test (`KINOFORGE_LIVE_ARIA2=1`). Cheap to add later;
  not required to ship.
- `Artifact.headers` field for auth-header propagation. Wan public weights
  need no auth; CivitAI's `?api_key=` rides in the URL. HF-gated weights
  via `Authorization: Bearer hf_…` are a follow-up.
- Per-file size threshold (Q4 decided always-on).
- Tunable knobs via YAML or env var (Q5 decided hard-coded).
- aria2c's own `--checksum=sha-256=…` flag (Q7 kept post-download verify
  as the single checksum code path).
- File-level concurrency tuning. `download_all` keeps its current
  `max_workers=4`; this layer changes per-file connection count, not the
  file-level fan-out.

---

## 9. Risk + mitigation

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| aria2c's CLI flags change in a future Debian / Homebrew release | Low | High (every download breaks) | `WARNING` log + silent fallback to stdlib (Q2). Operators get the file; lost speed is detected via wall-clock metrics, not data corruption. |
| Forgotten `which_aria2=lambda: None` in an existing test | Medium during the diff | Test order-dependent if `aria2c` is on the CI runner's PATH | CI matrix already runs on Ubuntu + macOS; the test file's existing 8 ACs gain one line each. Caught in the migration commit, not silently. |
| aria2c writes a file whose name doesn't match `part_path.name` | Very low | corrupt cache | `-o part_path.name` + `--auto-file-renaming=false` make aria2c either write to that exact name or fail with non-zero exit. The failure path is `KinoforgeError` → fallback. |
| Operator runs from a directory where aria2c's metadata file `.aria2` collides | Low | Cosmetic litter | `--file-allocation=none` minimises the metadata footprint; `.aria2` files in the dest dir are tolerated and cleaned up after successful completion by aria2c itself. |
| HuggingFace per-IP rate-limit triggers when aria2c opens 16 connections | Low-medium | aria2c fails → fallback to slow stdlib | `--max-tries=3 --retry-wait=2` gives aria2c three chances before raising. The fallback is the same single-connection path operators have today, so the worst-case wall-clock is "today's perf". |

---

## 10. Implementation outline (for `writing-plans`)

Single layer, ~6 tasks. Sketch only — the implementation plan refines this.

1. Add `logger`, `WhichCallable`, `RunAriaCallable` aliases, `_ARIA2_BASE_ARGS`,
   `_shutil_which_aria2`, `_subprocess_run_aria2` to `downloader.py`. Drop
   the obsolete `# DEFERRED: aria2c fast path …` module docstring comment.
2. Add the `which_aria2` + `run_aria2` kwargs to `download_one`; insert the
   transport branch between the skip-path and the stdlib fetch.
3. Forward both new kwargs through `download_all` to every
   `pool.submit(download_one, …)` call.
4. Write 7 new tests (A1-A7). Pre-existing 8 ACs gain a `which_aria2=lambda: None`
   override.
5. Doc updates: README "Faster downloads" sub-section + PROGRESS entry.
6. Whole-suite gate (`pixi run test`) + `--no-ff` merge to `main`.

Each task lands as one atomic commit. No new module, no new dep, no
breaking-change to public API.
