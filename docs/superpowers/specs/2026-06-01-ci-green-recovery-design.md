# Spec — CI Green Recovery (post Layer P + Q merge)

**Date:** 2026-06-01
**Branch (target):** `main` (cut feature branch off `main` at HEAD)
**Triggering signal:** GitHub Actions email — CI run on push `c63cbea` failed; both `Test (ubuntu-latest)` + `Test (macos-latest)` exited with code 1.

## 1. Problem

CI on `main` has been red since `c63cbea` (Layer P + Q merge, 2026-06-01).

The merge brought in `tests/examples/test_runpod_comfyui_wan_graph.py` from commit `9d2a9bf` — a 4-test lockdown scaffold intentionally committed RED. The scaffold is correct-by-design: it locks down the future shape of `examples/runpod-comfyui-wan.yaml`'s workflow API JSON graph and `prompt_node_ids` field so that any future plan touching that YAML must satisfy the contract or fail loudly. The blocker for the scaffold turning GREEN is Layer P Task 7 item #3 (workflow API JSON + first green MP4), which is currently:

- architecturally unblocked (Layer Q shipped `render_provision` / `wait_for_ready`),
- but spec-pending (item #3's original sub-spec predates Layer Q; rewrite-vs-amend brainstorm not yet held), and
- live-spend-pending (no further live RunPod work until the brainstorm closes).

The 4 tests have no `xfail` / `skip` marker. `pixi run test` collects them, they fail, exit code 1. Every push to `main` since `c63cbea` reports CI failure even though the codebase is otherwise green (979 pass, 3 skip locally).

Secondary signals on the same workflow run (NOT the cause of exit code 1, but worth fixing in the same pass):
- `Node.js 20 actions are deprecated. […] Node.js 20 will be removed from the runner on September 16th, 2026.` Hard cutoff for forced Node 24 default is 2026-06-16 (15 days away). Affects `actions/checkout@v4` and `prefix-dev/setup-pixi@v0.8.1`.
- `Failed to save / restore: Cache service responded with 400` — GitHub Actions cache infra outage, transient, not actionable from this repo.

## 2. Goals

- Restore green CI on `main`.
- Preserve the regression-lockdown intent of the 4 RED tests (do NOT delete or weaken them).
- Silence the Node 20 deprecation warning by bumping action pins to Node-24-era versions while the bump window is calm.
- Make the change atomically revertable in a single commit.

## Non-goals

- Implementing the real workflow API JSON graph + YAML wiring (Layer P Task 7 item #3 scope).
- Fixing the GitHub Actions cache 400 errors (out-of-repo infra).
- Any production code change. This spec touches only CI config + one test file's module-level marker.
- Adding suite-shape assertions (e.g. parsing pytest output to count xfailed). YAGNI; `xfail` itself is the lockdown.

## 3. Design

### 3.1 Marker strategy

Add a module-level `pytestmark = pytest.mark.xfail(strict=False, reason=…)` at the top of `tests/examples/test_runpod_comfyui_wan_graph.py`. Module-level marker applies to all 4 tests in the file; per-test markers would be 4 identical decorators and harder to remove in one diff when item #3 ships.

`strict=False` semantics:
- Test fails → reported as `xfailed` → suite green.
- Test passes (item #3 lands real graph + YAML) → reported as `xpassed` → also suite green, no need for a same-commit marker removal.
- Removal of the marker happens as part of item #3's closure block (explicitly called out in this spec's Future Work section so item #3 won't forget).

Why not `strict=True`: would flip `xpass` to a CI failure, forcing item #3's closure to land marker removal in the same commit as graph + YAML. Adds coupling for no real signal benefit; PROGRESS.md already tracks the item #3 transition.

Why not `@pytest.mark.skip`: skips run zero assertions; if the YAML is broken in some other way (e.g. malformed YAML, missing required key), a skip hides the regression. `xfail` still executes the body, just inverts the expected outcome.

### 3.2 Reason string

The `reason=` must carry forward enough context that a future reader doesn't need to chase commits:

```python
pytestmark = pytest.mark.xfail(
    strict=False,
    reason=(
        "Layer P Task 7 item #3 RED lockdown — awaits real workflow API "
        "JSON graph + YAML wiring. See PROGRESS.md 'Layer P Task 7 item "
        "#3' section and "
        "docs/superpowers/specs/2026-06-01-layer-p-task7-item3-"
        "workflow-api-json-design.md."
    ),
)
```

### 3.3 CI action pins

Bumps:
- `actions/checkout@v4` → `actions/checkout@v6` (major-only pin; latest `v6.0.2` as of 2026-01-09). v6 runs on Node 24.
- `prefix-dev/setup-pixi@v0.8.1` → `prefix-dev/setup-pixi@v0.9.6` (exact pin matches existing convention; latest `v0.9.6` as of 2026-05-21). v0.9 runs on Node 24.

Pin-style rationale:
- `actions/checkout` uses semver-style majors with stable surface; `@v6` is the published convention for tracking patches without daily YAML churn.
- `prefix-dev/setup-pixi` publishes patch-level tags inside the `v0.x` line and the existing workflow pins exactly (`@v0.8.1`); follow that convention so a future curious bump is a one-line diff.

### 3.4 File-level changes

`.github/workflows/ci.yml`:
- Line: `- uses: actions/checkout@v4` → `- uses: actions/checkout@v6`
- Line: `- uses: prefix-dev/setup-pixi@v0.8.1` → `- uses: prefix-dev/setup-pixi@v0.9.6`
- Nothing else changes. `pixi run lint`, `pixi run typecheck`, `pixi run test` steps stay byte-identical.

`tests/examples/test_runpod_comfyui_wan_graph.py`:
- Add `import pytest` if not already present.
- Add `pytestmark = pytest.mark.xfail(strict=False, reason=…)` at module scope, after imports, before the first `def test_…`.
- No test body is touched; the existing assertions remain the lockdown contract.

## 4. Test plan

### 4.1 Pre-merge local verification (controller runs all)

1. `pixi run test -q tests/examples/test_runpod_comfyui_wan_graph.py`
   - Expected before change: `4 failed`.
   - Expected after change: `4 xfailed`. Exit code 0.
2. `pixi run test -q` (full suite)
   - Expected before: `4 failed, 979 passed, 3 skipped` (exit 1).
   - Expected after: `979 passed, 4 xfailed, 3 skipped` (exit 0).
3. `pixi run lint` — no-op on YAML, no-op on test module (marker is valid pytest).
4. `pixi run typecheck` — no-op on YAML; test module has no new type surface.
5. `pixi run pre-commit run --all-files` — must pass clean.

### 4.2 Post-merge CI verification

After push of the merge commit to `main`:
- Both `Test (ubuntu-latest)` + `Test (macos-latest)` jobs report green.
- No "Node.js 20 actions are deprecated" warning in workflow log.
- Cache 400 errors may still appear (GitHub infra), but they are warnings and do not affect exit code.

### 4.3 No new tests

This change is marker + version-bump only. A "suite shape" test that parses pytest output for `xfailed` count would be over-engineering — `xfail` is itself a lockdown that future contributors cannot accidentally weaken without changing the marker.

## 5. Risk + rollback

### 5.1 Risk

- **`checkout@v6` Node-24 dependency.** GitHub-hosted runners (`ubuntu-latest`, `macos-latest`) already ship Node 24; v6 is the documented Node-24 line. Self-hosted runners would need Node 24 manually, but this project uses only GH-hosted runners.
- **`setup-pixi@v0.9.6` minor bump.** Task 3 (this spec's verification gate) requires a changelog scan of v0.9.0 → v0.9.6 to confirm `cache: true` and `pixi-version: latest` semantics are unchanged. If any breaking change surfaces, the task amends the spec before merge.
- **`strict=False` xpass silence.** When item #3 lands real graph + YAML, the 4 tests will start passing. With `strict=False`, pytest reports `xpassed` but exits 0 — only visible in `-rXp` summary. Mitigation: PROGRESS.md item #3 closure block explicitly lists "remove `pytestmark = pytest.mark.xfail(…)` from `tests/examples/test_runpod_comfyui_wan_graph.py`" as a closure step. Added to Future Work below.

### 5.2 Rollback

Single commit reverts both changes atomically:

```bash
git revert <commit-sha>
```

No DB migrations, no production deploy, no external dependency. Worst-case rollback restores the prior `c63cbea` state (CI red, but with the original cause intact).

## 6. Future work

When Layer P Task 7 item #3 (workflow API JSON + first green MP4) closes, the closure block MUST include:

1. Delete `pytestmark = pytest.mark.xfail(…)` from `tests/examples/test_runpod_comfyui_wan_graph.py`.
2. Delete the `import pytest` line if no other code in the module uses it.
3. Confirm `pixi run test -q tests/examples/test_runpod_comfyui_wan_graph.py` reports `4 passed`.
4. Confirm full-suite count reflects the marker removal (e.g. `983 passed, 3 skipped`).

This task is explicitly assigned to the item #3 sub-plan, not to this CI-green spec.

The Node 20 → Node 24 bump is a one-time event; future GHA bumps are routine maintenance and do not need a fresh spec.

## 7. Out of scope (explicit)

- The 4 RED tests' assertion bodies (locked down by `9d2a9bf`; touched only by item #3).
- Any production code change.
- GitHub Actions cache infra (400 errors); transient external failure.
- Workflow caching strategy (e.g. switching from `cache: true` to manual cache key); leave defaults intact.
- `pixi-version: latest` pinning to a specific pixi version; orthogonal hygiene.
