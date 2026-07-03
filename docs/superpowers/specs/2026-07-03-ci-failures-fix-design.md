# CI back to green — 7 failure buckets + live-gate guard

**Date:** 2026-07-03
**Status:** approved (brainstorm session 2026-07-03)
**Context:** Main CI has been red since run 28312332662 went green 5 days ago.
Latest run 28683391787 (merge of T7.6 FlashVSR runtime rewrite) shows
19 failed + 14 errors across 7 distinct buckets. Several buckets predate
T7.6 and accumulated across merges because nobody was alerted to the red.

## Failure inventory

| # | Bucket | Count | Local repro | Root cause |
|---|--------|-------|-------------|------------|
| 1 | Live smokes run in CI | 2 F | n/a | `tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py` and `tests/live/test_wan_then_spandrel_warm_reuse_smoke.py` lack the `KINOFORGE_LIVE_TESTS` module gate; default `pixi run test` has no `-m "not live"` deselect. In CI (no creds) they die on `AuthError: RUNPOD_API_KEY must be set…`. Locally, with `.env` creds present, a plain `pixi run test` would launch real pods — a spend footgun. |
| 2 | Privacy lockdown AC7 | 1 F | yes | Two new direct-write sites outside store/sink: `src/kinoforge/cli/_commands.py:2194` (`fetch --out` writes fetched bytes to user path) and `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py:1854` (upload endpoint spools request body to tempfile). |
| 3 | flashvsr pyav ImportError | 4 F | CI-only | `tests/upscalers/flashvsr/test_runtime.py` installs its `imageio.v3` stub only `if "imageio.v3" not in sys.modules`. In a full-suite run an earlier test imports real imageio first, the stub is skipped, and real imageio lacks the pyav plugin (`av` not a dependency). Order-dependent test isolation. |
| 4 | `/workspace` PermissionError | 14 E | CI-only | `tests/engines/diffusers/test_server_upscale.py::fresh_server` patches `ARTIFACT_DIR` but not `LORAS_DIR`; server `_startup` (wan_t2v_server.py:1109) does `LORAS_DIR.mkdir()` → `/workspace/loras`. Writable inside the dev container, permission-denied on the GitHub runner. |
| 5 | warm-reuse scan mock | 1 F | yes | `tests/core/warm_reuse/test_scan_warm_candidates_ephemeral_index.py`: production scan path gained a `/health` probe; the test's MagicMock `endpoints.get()` leaks into `urllib.request` → `ValueError: unknown url type`. |
| 6 | render_provision embed | 2 F | yes | Provisioning switched to gzip+base64 written via a python-stdin one-liner (to stay under RunPod's 64 KB bootstrap limit); `tests/engines/test_diffusers_render_provision_embed.py` still parses the old `echo '<b64>' | base64 -d > target` line format and plain-b64-decodes. |
| 7 | grid-config slug | 9 F | yes | `tests/integration/test_no_unknown_slug_for_example_configs.py` globs `examples/configs/**/*.yaml`, which now picks up `*.grid.yaml` files. Grid YAMLs are not `Config`-shaped (no top-level `engine`/`models`) → pydantic ValidationError. |

Counts: 19 failed + 14 errors = buckets 1(2) + 2(1) + 3(4) + 5(1) + 6(2) + 7(9) failed, bucket 4(14) errors.

## Decisions (user-approved)

- **Scope:** all 7 buckets in one spec / one branch.
- **Live gating:** both layers — module-level skipif gate AND `-m "not live"`
  in the default test tasks.
- **AC7:** exempt-tag both write sites (no refactor). Rationale below.
- **Approach:** fixes plus a regression-guard lockdown test for live gating
  (approach "B"). CI-notification hardening explicitly deferred.

## Design

### 1. Live-smoke gating + guard (bucket 1)

Add to both ungated files, after imports (project convention already used by
other live modules):

```python
pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="live test: set KINOFORGE_LIVE_TESTS=1",
)
```

Keep the existing `@pytest.mark.live` markers.

`pixi.toml` task changes:

```toml
test = "python -m pytest -m 'not live'"
test-cov = "python -m pytest -m 'not live' --cov"
```

`test-live` / `test-live-skypilot` already inject `KINOFORGE_LIVE_TESTS=1`
and remain unchanged.

**New guard test** `tests/test_live_gating_lockdown.py`, in the same idiom as
`tests/test_no_unredacted_writes.py`: scan every `tests/live/test_*.py`
module and assert it gates itself on `KINOFORGE_LIVE_TESTS` at module level
(text/AST scan for the skipif pattern). An ungated future live test then
fails locally and in CI before it can spend money. This is a money
invariant, not style policing: the miss has already happened twice.

### 2. AC7 exemptions (bucket 2)

Both sites get the established `# kinoforge:public-write` tag (~11 existing
uses in `src/`) plus a one-line justification:

- `src/kinoforge/cli/_commands.py:2194` — user explicitly requested the
  destination via `fetch --out <path>`; the write is the command's contract.
- `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py:1854` — the
  upload spool executes pod-side, not on the operator's host; same rationale
  as AC7's built-in exclusion for string-literal embedded writes.

### 3. Test-only fixes (buckets 3–7)

- **Bucket 3:** stub `imageio.v3` unconditionally via
  `monkeypatch.setitem(sys.modules, "imageio.v3", stub)` (and the parent
  `imageio` entry as needed); delete the `if "imageio.v3" not in sys.modules`
  guard. Removes the collection-order dependence; no new `av` dependency for
  unit tests.
- **Bucket 4:** `fresh_server` fixture additionally patches
  `srv.LORAS_DIR` to `tmp_path / "loras"`; sweep `_startup` for any other
  module-level dir constants that mkdir outside tmp and patch those too.
- **Bucket 5:** update the stale mock so `endpoints.get()` returns a real
  URL string (or stub the health-probe callable directly) — no MagicMock
  may reach `urllib.request`.
- **Bucket 6:** update both embed tests to parse the current provision
  format: locate the write line for the target path, extract the blob, then
  `gzip.decompress(base64.b64decode(blob))` before the fingerprint check.
- **Bucket 7:** exclude `*.grid.yaml` from the glob in
  `test_no_unknown_slug_for_example_configs.py`. Grid YAMLs are consumed by
  the grid loader, not `load_config`; per-cell model-identity checking for
  grids is out of scope for this test.

### 4. Verification

- Red/green per task. Buckets 2, 5, 6, 7 reproduce locally today.
  Bucket 3's fix is provable locally by importing real `imageio.v3` before
  running the tests (simulating CI order). Bucket 4's by asserting the
  fixture leaves no `/workspace` path reachable from `_startup`.
- Bucket 1: `pixi run test --collect-only` selects zero live tests;
  `pixi run test-live --collect-only` still selects them.
- Final gate: `pixi run test`, `pixi run lint`, `pixi run typecheck`,
  `pixi run pre-commit run --all-files` all green locally; push; confirm the
  CI run on GitHub goes green.
- The pre-existing uncommitted `pixi.toml`/`pixi.lock` diff (httpx, typer,
  rich, stamina, vermin additions) is unrelated and stays out of this
  branch's commits.

## Out of scope

- CI failure notifications / branch protection (the 5-days-red
  observability gap) — separate decision, repo-ops not code.
- Grid-aware model-identity coverage for `*.grid.yaml` configs.
- Migrating `wan_t2v_server` off deprecated FastAPI `on_event` startup
  hooks (warning noise only).
