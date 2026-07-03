# CI Back to Green Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 7 CI failure buckets (19 failed + 14 errors in run 28683391787) and add a live-gating lockdown guard so ungated live smokes can never again reach CI or spend money locally.

**Architecture:** Test-side fixes plus two policy changes: (1) both-layer live-test gating — module-level `KINOFORGE_LIVE_TESTS` skipif in the two ungated smokes AND `-m "not live"` deselect in the default pixi test tasks, enforced by a new lockdown test; (2) `# kinoforge:public-write` exemption tags on the two AC7-flagged write sites. All other buckets are stale-test repairs (mock drift, format drift, env-dependence).

**Tech Stack:** pytest, pixi tasks, tomllib, text-scan lockdown idiom (as in `tests/test_no_unredacted_writes.py`).

**Spec:** `docs/superpowers/specs/2026-07-03-ci-failures-fix-design.md`

**User decisions (already made):**
- All 7 buckets in one spec/branch.
- Live gating: both layers (module skipif + task deselect).
- AC7: exempt-tag both write sites, no refactor.
- Approach B: fixes + live-gate guard lockdown test. CI notifications out of scope.
- Dirty `pixi.toml`/`pixi.lock` dep additions: commit first as separate chore (Task 0).

---

### Task 0: Commit pre-existing pixi dep additions

**Goal:** Clear the dirty working tree (httpx/typer/rich/stamina/vermin adds in `pixi.toml` + regenerated `pixi.lock`) so later tasks can commit `pixi.toml` edits cleanly.

**Files:**
- Commit as-is: `pixi.toml`, `pixi.lock`

**Acceptance Criteria:**
- [ ] `git status --porcelain -- pixi.toml pixi.lock` is empty after commit
- [ ] Commit contains ONLY those two files

**Verify:** `git status --porcelain -- pixi.toml pixi.lock` → no output

**Steps:**

- [ ] **Step 1: Confirm the diff is only dep additions**

Run: `git diff pixi.toml`
Expected: only the 5 added dep lines (httpx, typer, rich, stamina, vermin) under `[dependencies]`.

- [ ] **Step 2: Commit both files (memory: pre-commit regenerates/stages pixi.lock — stage BOTH)**

```bash
git add pixi.toml pixi.lock
git commit -m "chore(deps): add httpx, typer, rich, stamina, vermin"
```

---

### Task 1: Live-test gating — guard lockdown test + module gates + pixi deselect

**Goal:** No test under `tests/live/` runs during a plain `pixi run test`, enforced by a lockdown test.

**Files:**
- Create: `tests/test_live_gating_lockdown.py`
- Modify: `tests/live/test_wan_then_spandrel_warm_reuse_smoke.py` (add `import os` + `pytestmark`)
- Modify: `tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py` (add `import os` + `pytestmark`)
- Modify: `tests/live/test_c27_fake_util_endpoint.py` (add ci-safe tag)
- Modify: `pixi.toml:124-125` (test / test-cov tasks)

**Acceptance Criteria:**
- [ ] New lockdown test fails BEFORE the gate fixes (3 ungated modules + missing task deselect), passes after
- [ ] `pixi run test --co -q tests/live/test_wan_then_spandrel_warm_reuse_smoke.py` selects 0 tests
- [ ] `pixi run test-live` task unchanged and still selects live tests

**Verify:** `pixi run python -m pytest tests/test_live_gating_lockdown.py -v` → 2 passed

**Steps:**

- [ ] **Step 1: Write the failing lockdown test**

Create `tests/test_live_gating_lockdown.py`:

```python
"""Lockdown: every module under tests/live/ must be gated out of default runs.

Money invariant, not style: an ungated live test under ``tests/live/`` runs
during a plain ``pixi run test`` — locally that can spend real provider money
(the tests/live conftest auto-loads ``.env`` creds), and in CI it fails on
missing creds. On 2026-07-03 two live smokes shipped with no gate and broke
main CI (run 28683391787).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = REPO_ROOT / "tests" / "live"

# A module counts as gated when it references any of these. Text scan on
# purpose (same idiom as test_no_unredacted_writes) — coarse but cheap, and
# it catches the observed failure mode: a module with NO gating signal at all.
_GATE_SIGNALS = (
    "KINOFORGE_LIVE_TESTS",  # module-level env gate (preferred for new tests)
    "skipif",  # pytestmark / decorator conditional skip (e.g. on RUNPOD_API_KEY)
    "pytest.skip",  # imperative module- or fixture-level skip
    "importorskip",
    "mark.live",  # deselected by the default task's -m "not live"
    "kinoforge:ci-safe",  # explicit reviewed opt-out: fast, no network, no spend
)


def test_every_live_module_declares_a_gate() -> None:
    """Bug caught: a new tests/live module with no gate at all — it runs (and
    can spend money) under a plain ``pixi run test``."""
    ungated = [
        str(p.relative_to(REPO_ROOT))
        for p in sorted(LIVE_DIR.glob("test_*.py"))
        if not any(sig in p.read_text() for sig in _GATE_SIGNALS)
    ]
    assert not ungated, (
        "tests/live modules with no gating signal — add a KINOFORGE_LIVE_TESTS "
        "module gate, a `live` marker, or a reviewed '# kinoforge:ci-safe' tag: "
        f"{ungated}"
    )


def test_default_pixi_test_tasks_deselect_live_marker() -> None:
    """Bug caught: someone drops ``-m 'not live'`` from the default task and
    live-marked smokes silently rejoin plain ``pixi run test`` runs."""
    tasks = tomllib.loads((REPO_ROOT / "pixi.toml").read_text())["tasks"]
    for name in ("test", "test-cov"):
        task = tasks[name]
        cmd = task if isinstance(task, str) else task["cmd"]
        assert "not live" in cmd, (
            f"pixi task {name!r} must deselect live tests with -m 'not live'; got: {cmd!r}"
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run python -m pytest tests/test_live_gating_lockdown.py -v`
Expected: 2 FAILED — first lists 3 ungated files (`test_c27_fake_util_endpoint.py`, `test_spandrel_realesrgan_x2_upscale_smoke.py`, `test_wan_then_spandrel_warm_reuse_smoke.py`); second reports missing `not live` in task `test`.

- [ ] **Step 3: Gate the two smokes**

In `tests/live/test_wan_then_spandrel_warm_reuse_smoke.py`, change the import block (lines 8-13) and add `pytestmark` right after the imports:

```python
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="live smoke: set KINOFORGE_LIVE_TESTS=1 (spends RunPod money)",
)
```

In `tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py`, same change — its import block (lines 8-14) gains `import os`, and the same `pytestmark` block goes right after `import pytest`:

```python
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("KINOFORGE_LIVE_TESTS") != "1",
    reason="live smoke: set KINOFORGE_LIVE_TESTS=1 (spends RunPod money)",
)
```

Keep the existing `@pytest.mark.live` decorators on the test functions in both files.

- [ ] **Step 4: Tag the deliberately CI-safe module**

In `tests/live/test_c27_fake_util_endpoint.py`, add one line directly after the module docstring (after line 5):

```python
# kinoforge:ci-safe — fast local fake, no network, no cloud spend.
```

- [ ] **Step 5: Add the deselect to the default pixi tasks**

In `pixi.toml`, replace lines 124-125:

```toml
test = "python -m pytest -m 'not live'"
test-cov = "python -m pytest -m 'not live' --cov"  # Later on, consider adding --cov-fail-under=80
```

- [ ] **Step 6: Run lockdown test to verify it passes**

Run: `pixi run python -m pytest tests/test_live_gating_lockdown.py -v`
Expected: 2 passed

- [ ] **Step 7: Verify both layers behave**

Run: `pixi run test --co -q tests/live/test_wan_then_spandrel_warm_reuse_smoke.py`
Expected: `no tests ran` / 0 selected (deselected by marker + skipif).

Run: `KINOFORGE_LIVE_TESTS=1 pixi run python -m pytest tests/live/test_wan_then_spandrel_warm_reuse_smoke.py --co -q`
Expected: 1 test collected (gate opens with the env var; DO NOT run it — collect only).

- [ ] **Step 8: Commit**

```bash
git add tests/test_live_gating_lockdown.py tests/live/test_wan_then_spandrel_warm_reuse_smoke.py tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py tests/live/test_c27_fake_util_endpoint.py pixi.toml pixi.lock
git commit -m "test: gate live smokes out of default runs + live-gating lockdown"
```

(`pixi.lock` included defensively per pre-commit-stages-pixi.lock memory; task-only edits normally leave it untouched.)

---

### Task 2: AC7 exemption tags on the two flagged write sites

**Goal:** `tests/test_no_unredacted_writes.py::test_ac7_no_path_write_outside_store_and_sink` passes.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py:2194`
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py:1854`
- Test: `tests/test_no_unredacted_writes.py` (no changes — existing test goes green)

**Acceptance Criteria:**
- [ ] AC7 test passes; all other lockdown tests in the file still pass
- [ ] Both tags carry a one-line justification

**Verify:** `pixi run python -m pytest tests/test_no_unredacted_writes.py -q` → all passed

**Steps:**

- [ ] **Step 1: Confirm the test currently fails**

Run: `pixi run python -m pytest tests/test_no_unredacted_writes.py::test_ac7_no_path_write_outside_store_and_sink -q`
Expected: FAILED listing exactly the two sites below.

- [ ] **Step 2: Tag the CLI fetch --out write**

In `src/kinoforge/cli/_commands.py` line 2194, change:

```python
        Path(out_path).write_bytes(body)
```

to:

```python
        # kinoforge:public-write — user explicitly requested this destination
        # via `fetch --out <path>`; the write IS the command's contract.
        Path(out_path).write_bytes(body)  # kinoforge:public-write
```

- [ ] **Step 3: Tag the pod-side upload spool**

In `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` line 1854, change:

```python
        with os.fdopen(fd, "wb") as fobj:
```

to:

```python
        # kinoforge:public-write — upload spool executes pod-side, not on the
        # operator's host (same rationale as AC7's string-literal exclusion).
        with os.fdopen(fd, "wb") as fobj:  # kinoforge:public-write
```

(The tag must sit on a line within the call extent — the inline comment on the call line is what the AST scan reads; the preceding comment block is for humans.)

- [ ] **Step 4: Run the full lockdown file to verify green**

Run: `pixi run python -m pytest tests/test_no_unredacted_writes.py -q`
Expected: all passed (AC7 green, no other AC regressed by the new tags).

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/cli/_commands.py src/kinoforge/engines/diffusers/servers/wan_t2v_server.py
git commit -m "fix(lockdown): public-write tags for fetch --out and pod-side upload spool"
```

---

### Task 3: flashvsr runtime tests — unconditional imageio stub

**Goal:** The 4 `tests/upscalers/flashvsr/test_runtime.py` failures are collection-order-independent (CI full-suite ordering imports real imageio first; stub must win anyway).

**Files:**
- Modify: `tests/upscalers/flashvsr/test_runtime.py:287-323` (the `if "imageio.v3" not in sys.modules:` block inside the `stub_diffsynth` fixture)

**Acceptance Criteria:**
- [ ] Tests pass with real `imageio.v3` pre-imported (CI-order repro)
- [ ] Tests still pass standalone

**Verify:** `pixi run python -c "import imageio.v3; import pytest; raise SystemExit(pytest.main(['tests/upscalers/flashvsr/test_runtime.py', '-q']))"` → all passed

**Steps:**

- [ ] **Step 1: Reproduce the CI failure locally**

Run: `pixi run python -c "import imageio.v3; import pytest; raise SystemExit(pytest.main(['tests/upscalers/flashvsr/test_runtime.py', '-q']))"`
Expected: 4 FAILED with `ImportError: The 'pyav' plugin is not installed` (same as CI). This works because pre-importing real `imageio.v3` makes the `if "imageio.v3" not in sys.modules` guard skip the stub.

- [ ] **Step 2: Make the stub unconditional and give the parent stub a .v3 attribute**

In `tests/upscalers/flashvsr/test_runtime.py`, replace the block starting at line 287 (`if "imageio.v3" not in sys.modules:` through the two `monkeypatch.setitem` lines at 322-323). Remove the `if` guard, dedent the body one level, and set `.v3` on the parent stub:

```python
    # Always install the imageio stub — even when real imageio was imported
    # earlier in the session (full-suite ordering) — otherwise
    # `import imageio.v3 as iio` binds the real module, which needs the pyav
    # plugin that is not a project dependency. The parent stub must carry a
    # .v3 attribute because `import a.b as c` binds via getattr(a, "b") when
    # the parent is already in sys.modules.
    ii = types.ModuleType("imageio.v3")

    def imwrite(
        path: str,
        data: Any,
        fps: float = 24.0,
        plugin: str = "pyav",
        codec: str = "libx264",
    ) -> None:  # noqa: ARG001
        Path(path).write_bytes(b"MP4-STUB")

    # imopen is needed in case _input_prep.prepare_input_tensor is ever
    # called without the stub (e.g. due to module-cache ordering across tests).
    def imopen(path: str, mode: str, plugin: str = "pyav") -> Any:  # noqa: ARG001
        class _Reader:
            def metadata(self) -> dict[str, Any]:
                return {"fps": 24.0}

            def iter(self) -> Any:
                import numpy as _np

                for _ in range(16):
                    yield _np.zeros((16, 16, 3), dtype="uint8")

            def close(self) -> None: ...

            def __enter__(self) -> _Reader:
                return self

            def __exit__(self, *a: Any) -> None: ...

        return _Reader()

    ii.imwrite = imwrite  # type: ignore[attr-defined]
    ii.imopen = imopen  # type: ignore[attr-defined]
    imageio_parent = types.ModuleType("imageio")
    imageio_parent.v3 = ii  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "imageio", imageio_parent)
    monkeypatch.setitem(sys.modules, "imageio.v3", ii)
```

(The `imwrite` / `imopen` bodies are identical to the current ones — only the guard, indentation, and the parent `.v3` assignment change.)

- [ ] **Step 3: Verify the repro now passes**

Run: `pixi run python -c "import imageio.v3; import pytest; raise SystemExit(pytest.main(['tests/upscalers/flashvsr/test_runtime.py', '-q']))"`
Expected: all passed.

- [ ] **Step 4: Verify standalone still passes**

Run: `pixi run python -m pytest tests/upscalers/flashvsr/test_runtime.py -q`
Expected: all passed.

- [ ] **Step 5: Commit**

```bash
git add tests/upscalers/flashvsr/test_runtime.py
git commit -m "test(flashvsr): install imageio stub unconditionally, fix CI ordering"
```

---

### Task 4: test_server_upscale — patch LORAS_DIR into tmp_path

**Goal:** The 14 `tests/engines/diffusers/test_server_upscale.py` errors stop depending on a writable `/workspace` (GH runner denies `LORAS_DIR.mkdir()` at `wan_t2v_server.py:1109` in `_startup`).

**Files:**
- Modify: `tests/engines/diffusers/test_server_upscale.py:62` (the `fresh_server` fixture)

**Acceptance Criteria:**
- [ ] Fixture patches BOTH module-level dirs `_startup` mkdirs (`ARTIFACT_DIR` — already patched — and `LORAS_DIR`)
- [ ] File passes locally

**Verify:** `pixi run python -m pytest tests/engines/diffusers/test_server_upscale.py -q` → all passed

**Steps:**

- [ ] **Step 1: Add the missing patch**

In `tests/engines/diffusers/test_server_upscale.py`, directly after line 62 (`monkeypatch.setattr(srv, "ARTIFACT_DIR", tmp_path / "artifacts")`), add:

```python
    monkeypatch.setattr(srv, "LORAS_DIR", tmp_path / "loras")
```

- [ ] **Step 2: Confirm no other absolute-path mkdir remains in the startup path**

Run: `rg -n "mkdir" src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
Expected: mkdir call sites reference only `ARTIFACT_DIR`, `LORAS_DIR`, `_UPLOAD_DIR`, or a function-local `dest_dir`. `_startup` (lines 1103-1109) touches only `ARTIFACT_DIR` + `LORAS_DIR` — both now patched. (`_UPLOAD_DIR` is exercised only by upload-endpoint tests, which are not in this file's failing set; leave it.)

- [ ] **Step 3: Run the file**

Run: `pixi run python -m pytest tests/engines/diffusers/test_server_upscale.py -q`
Expected: all passed (was already green locally — the real verification is CI in Task 8; this step guards against regression).

- [ ] **Step 4: Commit**

```bash
git add tests/engines/diffusers/test_server_upscale.py
git commit -m "test(server-upscale): patch LORAS_DIR to tmp_path, unbreak GH runner"
```

---

### Task 5: warm-reuse scan test — neutralize the /health preflight probe

**Goal:** `tests/core/warm_reuse/test_scan_warm_candidates_ephemeral_index.py::test_scan_surfaces_index_row_when_ledger_empty` passes; no MagicMock reaches `urllib.request`.

**Files:**
- Modify: `tests/core/warm_reuse/test_scan_warm_candidates_ephemeral_index.py:59-66` (the `with patch(...)` block)

**Acceptance Criteria:**
- [ ] Test passes
- [ ] Fix patches the module-level seam (`_health_preflight_ok`), not urllib internals

**Verify:** `pixi run python -m pytest tests/core/warm_reuse/test_scan_warm_candidates_ephemeral_index.py -q` → all passed

**Steps:**

- [ ] **Step 1: Confirm the current failure**

Run: `pixi run python -m pytest tests/core/warm_reuse/test_scan_warm_candidates_ephemeral_index.py -q`
Expected: 1 FAILED with `ValueError: unknown url type: "<MagicMock ...>/health"` — production `_scan_warm_candidates` gained a `/health` capability preflight (`_commands.py:1414`) after this test was written; the MagicMock cfg/instance leak a mock URL into `urllib.request.Request`.

- [ ] **Step 2: Patch the preflight seam**

In the test's `with (...)` block (lines 59-65), add a third patch:

```python
    with (
        patch(
            "kinoforge.cli._commands._resolve_warm_instance",
            return_value=(fake_instance, None),
        ),
        patch("kinoforge.cli._commands._probe_lock_held", return_value=False),
        # Production scan gained a /health capability preflight; this test
        # covers index-row surfacing, not stage matching — report "covers".
        patch("kinoforge.cli._commands._health_preflight_ok", return_value=True),
    ):
        instance, report = _scan_warm_candidates(ctx, cfg)
```

- [ ] **Step 3: Run to verify it passes**

Run: `pixi run python -m pytest tests/core/warm_reuse/test_scan_warm_candidates_ephemeral_index.py -q`
Expected: all passed.

- [ ] **Step 4: Commit**

```bash
git add tests/core/warm_reuse/test_scan_warm_candidates_ephemeral_index.py
git commit -m "test(warm-reuse): stub /health preflight seam in ephemeral-index scan test"
```

---

### Task 6: render_provision embed tests — parse the gzip+base64 format

**Goal:** Both `tests/engines/test_diffusers_render_provision_embed.py` embed tests decode the current provision line format (gzip+base64 via python3 stdin one-liner, `src/kinoforge/engines/diffusers/__init__.py:123-130`) instead of the old `echo '<b64>' | base64 -d`.

**Files:**
- Modify: `tests/engines/test_diffusers_render_provision_embed.py:76-113` (both embed tests' line-parsing loops)

**Acceptance Criteria:**
- [ ] Both tests pass; fingerprint checks retained (decode → `gzip.decompress` → fingerprint)
- [ ] Other 4 tests in the file unaffected

**Verify:** `pixi run python -m pytest tests/engines/test_diffusers_render_provision_embed.py -q` → 6 passed

**Steps:**

- [ ] **Step 1: Confirm the current failure**

Run: `pixi run python -m pytest tests/engines/test_diffusers_render_provision_embed.py -q`
Expected: 2 FAILED (`test_embed_writes_wan_t2v_server_source_to_kfsrv`, `test_embed_writes_video_io_source_to_kfsrv`) — the emit format is now `echo '<b64-of-gzip>' | python3 -c "import sys,base64,gzip; sys.stdout.buffer.write(gzip.decompress(base64.b64decode(sys.stdin.read())))" > <target>`, so the tests' `"base64 -d" in line` match never fires / plain b64decode never contains the fingerprint.

- [ ] **Step 2: Add gzip import and a shared decode helper**

At the top of `tests/engines/test_diffusers_render_provision_embed.py`, add `import gzip` next to the existing `import base64`. Then add a module-level helper (above the first test):

```python
def _decoded_blob_for(script: str, target: str) -> bytes | None:
    """Decode the gzip+base64 payload written to ``target``, or None.

    Provision emits: ``echo '<b64>' | python3 -c "...gzip.decompress(
    base64.b64decode(...))..." > <target>`` — one line per embedded file.
    """
    for line in script.splitlines():
        if target in line and "base64" in line and line.startswith("echo '"):
            blob = line.split("'", 2)[1]
            return gzip.decompress(base64.b64decode(blob))
    return None
```

- [ ] **Step 3: Rewrite both tests' parse loops to use the helper**

In `test_embed_writes_wan_t2v_server_source_to_kfsrv`, replace the loop (the `decoded_any` block, lines ~81-91) with:

```python
    decoded = _decoded_blob_for(script, target)
    assert decoded is not None, f"no embed write line found for {target}"
    assert fingerprint in decoded, (
        f"embedded payload for {target} does not contain {fingerprint!r}"
    )
```

(keep the preceding `assert target in script` and the trailing on-disk fingerprint sanity check unchanged).

In `test_embed_writes_video_io_source_to_kfsrv`, replace its `found` loop (lines ~104-113) with:

```python
    decoded = _decoded_blob_for(rp.script, target)
    assert decoded is not None, f"no embed write line found for {target}"
    assert fingerprint in decoded
```

- [ ] **Step 4: Run to verify green**

Run: `pixi run python -m pytest tests/engines/test_diffusers_render_provision_embed.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add tests/engines/test_diffusers_render_provision_embed.py
git commit -m "test(provision): parse gzip+base64 embed lines in render_provision tests"
```

---

### Task 7: example-config slug test — exclude grid YAMLs

**Goal:** The 9 `tests/integration/test_no_unknown_slug_for_example_configs.py` failures stop — grid YAMLs are not `Config`-shaped and belong to the grid loader, not `load_config`.

**Files:**
- Modify: `tests/integration/test_no_unknown_slug_for_example_configs.py:26-31` (`_collect_example_configs`)

**Acceptance Criteria:**
- [ ] `*.grid.yaml` files excluded from parametrization (with a comment saying why)
- [ ] All remaining parametrized cases pass

**Verify:** `pixi run python -m pytest tests/integration/test_no_unknown_slug_for_example_configs.py -q` → all passed, zero `*.grid.yaml` params

**Steps:**

- [ ] **Step 1: Confirm the current failure**

Run: `pixi run python -m pytest tests/integration/test_no_unknown_slug_for_example_configs.py -q`
Expected: 9 FAILED, all with param names ending `.grid.yaml`, each a pydantic `ValidationError` (missing `engine` / `models`).

- [ ] **Step 2: Filter grid YAMLs out of the glob**

In `_collect_example_configs`, change:

```python
def _collect_example_configs() -> list[Path]:
    return sorted(
        p
        for p in _EXAMPLE_DIR.glob("**/*.yaml")
        if p.name not in _SKIP_YAMLS and "manifests" not in p.parts
    )
```

to:

```python
def _collect_example_configs() -> list[Path]:
    return sorted(
        p
        for p in _EXAMPLE_DIR.glob("**/*.yaml")
        if p.name not in _SKIP_YAMLS
        and "manifests" not in p.parts
        # *.grid.yaml files are grid-sweep specs consumed by the grid
        # loader, not load_config — they have no top-level engine/models.
        and not p.name.endswith(".grid.yaml")
    )
```

- [ ] **Step 3: Run to verify green**

Run: `pixi run python -m pytest tests/integration/test_no_unknown_slug_for_example_configs.py -q`
Expected: all passed; collected count drops by 9 vs. the failing run.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_no_unknown_slug_for_example_configs.py
git commit -m "test(configs): exclude *.grid.yaml from load_config slug parametrization"
```

---

### Task 8: Full local gate, push, confirm CI green

**Goal:** Entire suite + lint + typecheck + pre-commit green locally; push; CI run on GitHub completes green.

**Files:** none (verification only)

**Acceptance Criteria:**
- [ ] `pixi run test` all green (live tests deselected/skipped)
- [ ] `pixi run lint`, `pixi run typecheck`, `pixi run pre-commit run --all-files` green
- [ ] Pushed; `gh run watch` (or polled `gh run list`) shows the new CI run `success` on both ubuntu + macos jobs

**Verify:** `gh run list --workflow ci.yml --branch main --limit 1` → `completed success`

**Steps:**

- [ ] **Step 1: Full local suite**

Run: `pixi run test`
Expected: 0 failed, 0 errors. Note: some fixture-gated RunPod probes under tests/live may still run read-only network calls when `.env` creds are present — pre-existing behavior, out of scope; they must not create pods.

- [ ] **Step 2: Lint + typecheck + pre-commit**

Run: `pixi run lint && pixi run typecheck && pixi run pre-commit run --all-files`
Expected: all pass. Fix priority if conflicts: tests pass > mypy clean > ruff clean.

- [ ] **Step 3: Push and watch CI**

```bash
git push
gh run watch --exit-status "$(gh run list --workflow ci.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Expected: exit 0, conclusion `success` for both matrix jobs (ubuntu-latest, macos-latest). If a job fails, pull `gh run view <id> --log-failed`, fix, repeat.

- [ ] **Step 4: Update PROGRESS.md and commit**

Add to `PROGRESS.md`: CI-green effort complete — spec + plan paths, 7 buckets fixed, live-gating lockdown added, CI run id green. Commit:

```bash
git add PROGRESS.md
git commit -m "docs(progress): main CI back to green — 7 buckets fixed + live-gate lockdown"
git push
```

---

## Self-Review Notes

- Spec coverage: bucket 1 → Task 1; bucket 2 → Task 2; bucket 3 → Task 3; bucket 4 → Task 4; bucket 5 → Task 5; bucket 6 → Task 6; bucket 7 → Task 7; guard → Task 1; verification → Task 8. Dirty-deps decision → Task 0. Out-of-scope items (CI notifications, grid identity coverage, FastAPI on_event migration) have no tasks by design.
- Guard-test design deviation from spec noted: the spec's "every live module gates on KINOFORGE_LIVE_TESTS" is relaxed to "declares one of the recognized gating signals" because 35+ existing live modules legitimately gate via skipif/fixture/marker idioms; strict env-gate enforcement would fail them all. The guard still catches the observed failure mode (zero signals).
- Types/names consistent: `_decoded_blob_for` used in both Task 6 tests; `_GATE_SIGNALS` only in Task 1.
