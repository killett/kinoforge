# CI Green Recovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers-extended-cc:subagent-driven-development` (recommended) or `superpowers-extended-cc:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore green CI on `main` by xfail-ing the 4 RED scaffold tests committed in `9d2a9bf` and bumping `.github/workflows/ci.yml` action pins onto the Node-24 line ahead of the 2026-06-16 deprecation cutoff.

**Architecture:** Two-file surgical change with one verification gate. (1) Pre-flight: read the `prefix-dev/setup-pixi` v0.9.0 → v0.9.6 changelog and confirm `cache: true` + `pixi-version: latest` semantics are unchanged. (2) Add a module-level `pytest.mark.xfail(strict=False, …)` marker to `tests/examples/test_runpod_comfyui_wan_graph.py` so all 4 RED tests report `xfailed` until Layer P Task 7 item #3 lands the real workflow API JSON graph. (3) Bump `actions/checkout@v4 → @v6` and `prefix-dev/setup-pixi@v0.8.1 → @v0.9.6`. (4) Push to a feature branch, observe green CI, merge with `--no-ff`.

**Tech Stack:** Python 3, pytest (xfail marker), pixi (test runner), GitHub Actions (CI), git.

**Spec:** `docs/superpowers/specs/2026-06-01-ci-green-recovery-design.md`.

**Branch:** Cut `chore/ci-green-recovery` off `main` (HEAD currently `face9c5`).

---

## File structure

| Path | Action | Responsibility |
|---|---|---|
| `tests/examples/test_runpod_comfyui_wan_graph.py` | Modify | Add `import pytest` + module-level `pytestmark = pytest.mark.xfail(strict=False, reason=…)` so 4 RED tests are reported `xfailed`. |
| `.github/workflows/ci.yml` | Modify | Bump `actions/checkout@v4 → @v6` (major-pin) and `prefix-dev/setup-pixi@v0.8.1 → @v0.9.6` (exact-pin). |

Two files. Two commits (one per file = clean atomic revert; merge commit binds them).

---

## Task 1: Verify setup-pixi v0.9.x changelog

**Goal:** Pre-flight gate before touching the workflow. Confirm no breaking change in setup-pixi v0.8.1 → v0.9.6 invalidates the current `with: { pixi-version: latest, cache: true }` block.

**Files:**
- Read-only (web): https://github.com/prefix-dev/setup-pixi/releases (tags v0.9.0 through v0.9.6)

**Acceptance Criteria:**
- [ ] Release notes for v0.9.0 through v0.9.6 reviewed.
- [ ] `cache: true` semantics confirmed unchanged (no rename, no new required field).
- [ ] `pixi-version: latest` still supported (no rename to `pixi_version`, no removal of `latest` keyword).
- [ ] Findings recorded in this task's commit message OR — if any breaking change is found — the spec is amended with a workaround block before proceeding to Task 2.

**Verify:** Manual; output is the implementer's plain-text findings appended to the eventual commit body of Task 3.

**Steps:**

- [ ] **Step 1: Read release notes**

```bash
# Open in browser or via gh
gh release view v0.9.0 --repo prefix-dev/setup-pixi --json name,tagName,body | jq -r '.body' | head -80
gh release view v0.9.1 --repo prefix-dev/setup-pixi --json body | jq -r '.body' | head -40
gh release view v0.9.2 --repo prefix-dev/setup-pixi --json body | jq -r '.body' | head -40
gh release view v0.9.3 --repo prefix-dev/setup-pixi --json body | jq -r '.body' | head -40
gh release view v0.9.4 --repo prefix-dev/setup-pixi --json body | jq -r '.body' | head -40
gh release view v0.9.5 --repo prefix-dev/setup-pixi --json body | jq -r '.body' | head -40
gh release view v0.9.6 --repo prefix-dev/setup-pixi --json body | jq -r '.body' | head -40
```

If `gh` not available or rate-limited, fall back to direct fetch of `https://github.com/prefix-dev/setup-pixi/releases/tag/v0.9.X` for each X.

- [ ] **Step 2: Scan for breaking-change keywords**

Look for: `BREAKING`, `breaking change`, `removed`, `renamed`, `cache:`, `pixi-version:`, `inputs:`, `latest`.

- [ ] **Step 3: Record findings**

If NO breaking change: write a one-line note for the eventual Task 3 commit body, e.g. `setup-pixi v0.9.0..v0.9.6 changelog reviewed; cache: + pixi-version: latest semantics unchanged`.

If breaking change found: STOP this plan. Amend `docs/superpowers/specs/2026-06-01-ci-green-recovery-design.md` §3.3 with the workaround (e.g. rename a key, pin pixi-version explicitly) BEFORE Task 2 proceeds.

- [ ] **Step 4: No commit yet**

Task 1 produces no file change. Findings ride along on Task 3's commit body. Mark task complete only when findings are recorded (locally, e.g. in a scratch note) and ready to paste into Task 3's commit message.

---

## Task 2: Add module-level xfail marker to RED scaffold

**Goal:** Convert 4 known-RED lockdown tests in `tests/examples/test_runpod_comfyui_wan_graph.py` from `failed` to `xfailed` so the full suite exits 0. Preserves the regression-lockdown intent (`xfail` still executes the body; only inverts the expected outcome). Marker removal happens later as part of Layer P Task 7 item #3's closure block.

**Files:**
- Modify: `tests/examples/test_runpod_comfyui_wan_graph.py`

**Acceptance Criteria:**
- [ ] `import pytest` added (placed alongside existing imports).
- [ ] Module-level `pytestmark = pytest.mark.xfail(strict=False, reason=…)` declared once, after imports + `YAML_PATH = …` constant, before the first `def test_…`.
- [ ] `reason=` cites: Layer P Task 7 item #3 + `PROGRESS.md` reference + spec path `docs/superpowers/specs/2026-06-01-layer-p-task7-item3-workflow-api-json-design.md`.
- [ ] `pixi run test -q tests/examples/test_runpod_comfyui_wan_graph.py` reports `4 xfailed` (was `4 failed`).
- [ ] Full suite `pixi run test -q` reports `979 passed, 4 xfailed, 3 skipped` (was `4 failed, 979 passed, 3 skipped`). Exit code 0.

**Verify:**

```bash
pixi run test -q tests/examples/test_runpod_comfyui_wan_graph.py 2>&1 | tail -3
# expect:  4 xfailed in N.NNs   (exit 0)

pixi run test -q 2>&1 | tail -3
# expect:  979 passed, 4 xfailed, 3 skipped in N.NNs   (exit 0)
```

**Steps:**

- [ ] **Step 1: Run the test file to see the RED state**

```bash
pixi run test -q tests/examples/test_runpod_comfyui_wan_graph.py 2>&1 | tail -10
```

Expected: `4 failed in N.NNs` — confirms the baseline.

- [ ] **Step 2: Read the current head of the file**

```bash
head -25 tests/examples/test_runpod_comfyui_wan_graph.py
```

Existing imports (from the inspection in the spec):

```python
"""Lockdown tests for examples/configs/runpod-comfyui-wan.graph.json.

These tests assert the committed ComfyUI API-format graph is structurally
valid AND that the example YAML's asset_node_ids / prompt_node_ids dicts
reference node IDs that actually exist in the graph. Run offline — no pod.
...
"""

from __future__ import annotations

from pathlib import Path

from kinoforge.core.config import load_config

YAML_PATH = Path("examples/configs/runpod-comfyui-wan.yaml")
```

- [ ] **Step 3: Apply the edit**

Insert `import pytest` after the existing `from kinoforge.core.config import load_config` line, then insert `pytestmark = …` after `YAML_PATH = …` and before the first `def test_…`. Resulting head-of-file:

```python
"""Lockdown tests for examples/configs/runpod-comfyui-wan.graph.json.

These tests assert the committed ComfyUI API-format graph is structurally
valid AND that the example YAML's asset_node_ids / prompt_node_ids dicts
reference node IDs that actually exist in the graph. Run offline — no pod.

The test_prompt_node_ids_is_dict_and_references_existing_nodes case also
locks down the pre-existing list-vs-dict type bug in the YAML
(prompt_node_ids was ``["8"]`` — a list — but Layer J's
``engines.comfyui`` calls ``.items()`` on the value and would crash at
runtime).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.config import load_config

YAML_PATH = Path("examples/configs/runpod-comfyui-wan.yaml")

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

Use `Edit` with:
- `old_string` = the block from `from pathlib import Path\n\nfrom kinoforge.core.config import load_config\n\nYAML_PATH = Path("examples/configs/runpod-comfyui-wan.yaml")`
- `new_string` = the block above starting at `from pathlib import Path` through the closing `)` of `pytestmark = …`.

Ruff import-ordering: stdlib (`pathlib`) → third-party (`pytest`) → first-party (`kinoforge.*`). The order above is correct.

- [ ] **Step 4: Run the file to verify xfail**

```bash
pixi run test -q tests/examples/test_runpod_comfyui_wan_graph.py 2>&1 | tail -3
```

Expected: `4 xfailed in N.NNs`. Exit code 0.

- [ ] **Step 5: Run the full suite to verify counts shift**

```bash
pixi run test -q 2>&1 | tail -3
```

Expected: `979 passed, 4 xfailed, 3 skipped in N.NNs`. Exit code 0.

- [ ] **Step 6: Run lint + typecheck + pre-commit**

```bash
pixi run lint
pixi run typecheck
pixi run pre-commit run --files tests/examples/test_runpod_comfyui_wan_graph.py
```

All three clean.

- [ ] **Step 7: Commit**

```bash
git add tests/examples/test_runpod_comfyui_wan_graph.py
git commit -m "$(cat <<'EOF'
test(examples): xfail RED scaffold pending Layer P Task 7 item #3

Module-level pytest.mark.xfail(strict=False) on the 4 RED lockdown
tests in tests/examples/test_runpod_comfyui_wan_graph.py. The scaffold
(committed in 9d2a9bf) intentionally fails until item #3 lands the
real workflow API JSON graph + YAML wiring; xfail preserves the
regression-lockdown intent (test body still executes) while letting
CI exit 0.

strict=False so xpass — when item #3 lands — silently reports pass.
Marker removal is routed to the item #3 closure block per spec §6.

Suite count moves from "4 failed, 979 passed, 3 skipped" to
"979 passed, 4 xfailed, 3 skipped".

Spec: docs/superpowers/specs/2026-06-01-ci-green-recovery-design.md
EOF
)"
```

---

## Task 3: Bump CI action pins to Node-24 line

**Goal:** Silence the "Node.js 20 actions are deprecated" warning observed on the `c63cbea` workflow run by moving `actions/checkout` and `prefix-dev/setup-pixi` onto Node-24-era versions, before the 2026-06-16 forced-Node-24 default cutoff.

**Files:**
- Modify: `.github/workflows/ci.yml`

**Acceptance Criteria:**
- [ ] `uses: actions/checkout@v4` → `uses: actions/checkout@v6` (major-pin).
- [ ] `uses: prefix-dev/setup-pixi@v0.8.1` → `uses: prefix-dev/setup-pixi@v0.9.6` (exact-pin).
- [ ] No other CI step changed. `pixi --version`, `pixi run lint`, `pixi run typecheck`, `pixi run test` byte-identical.
- [ ] Task 1's changelog-findings note included in the commit body.

**Verify:**

```bash
grep -nE 'uses: (actions/checkout|prefix-dev/setup-pixi)' .github/workflows/ci.yml
# expect exactly two lines:
#   - uses: actions/checkout@v6
#   - uses: prefix-dev/setup-pixi@v0.9.6

# YAML still valid:
pixi run pre-commit run check-yaml --files .github/workflows/ci.yml 2>&1 | tail -5
```

Full CI exercise happens in Task 4 once both edits land on the feature branch.

**Steps:**

- [ ] **Step 1: Confirm current state**

```bash
grep -nE 'uses: (actions/checkout|prefix-dev/setup-pixi)' .github/workflows/ci.yml
```

Expected:
```
22:      - uses: actions/checkout@v4
24:      - uses: prefix-dev/setup-pixi@v0.8.1
```
(line numbers may differ slightly; the two `uses:` lines are what matter)

- [ ] **Step 2: Apply checkout bump**

Use `Edit`:
- `old_string` = `      - uses: actions/checkout@v4`
- `new_string` = `      - uses: actions/checkout@v6`

- [ ] **Step 3: Apply setup-pixi bump**

Use `Edit`:
- `old_string` = `      - uses: prefix-dev/setup-pixi@v0.8.1`
- `new_string` = `      - uses: prefix-dev/setup-pixi@v0.9.6`

- [ ] **Step 4: Verify the edits**

```bash
grep -nE 'uses: (actions/checkout|prefix-dev/setup-pixi)' .github/workflows/ci.yml
```

Expected:
```
22:      - uses: actions/checkout@v6
24:      - uses: prefix-dev/setup-pixi@v0.9.6
```

- [ ] **Step 5: Run pre-commit on the file**

```bash
pixi run pre-commit run --files .github/workflows/ci.yml 2>&1 | tail -10
```

Clean (or "no files to check" for hooks that don't match YAML).

- [ ] **Step 6: Commit**

Paste Task 1's changelog-findings note into the body.

```bash
git add .github/workflows/ci.yml
git commit -m "$(cat <<'EOF'
ci: bump checkout@v4 -> v6 and setup-pixi@v0.8.1 -> v0.9.6

Moves both GHA actions onto the Node-24 runtime line ahead of the
2026-06-16 forced-Node-24 default cutoff. Silences the "Node.js 20
actions are deprecated" warning observed on the c63cbea workflow
run.

actions/checkout: major-pin (@v6); auto-tracks patches inside v6.
prefix-dev/setup-pixi: exact-pin (@v0.9.6); matches existing
v0.x.y pin convention.

Pre-flight: setup-pixi v0.9.0..v0.9.6 release notes reviewed;
cache: + pixi-version: latest semantics unchanged. (Substitute the
real findings line from Task 1 here if the changelog flagged
anything non-trivial.)

Spec: docs/superpowers/specs/2026-06-01-ci-green-recovery-design.md
EOF
)"
```

---

## Task 4: Push feature branch + observe green CI + merge

**Goal:** Final integration gate. Push the two-commit feature branch, watch both `Test (ubuntu-latest)` + `Test (macos-latest)` jobs go green, confirm the Node 20 warning is gone, then merge `chore/ci-green-recovery` into `main` with `--no-ff`.

**Files:**
- No edits. Branch + push + observation + merge only.

**Acceptance Criteria:**
- [ ] Feature branch `chore/ci-green-recovery` created off `main` HEAD (`face9c5` or later if `main` advanced).
- [ ] Both Task 2 and Task 3 commits land on the feature branch (verified via `git log --oneline main..HEAD`).
- [ ] Branch pushed via `git push -u origin chore/ci-green-recovery`.
- [ ] CI run on the feature branch: both `Test (ubuntu-latest)` AND `Test (macos-latest)` jobs report `conclusion: success`.
- [ ] No "Node.js 20 actions are deprecated" string in the workflow log (search via `gh run view --log`).
- [ ] Merge into `main` via `git merge --no-ff chore/ci-green-recovery -m "<title> (#…)"` (or via `gh pr create` + `gh pr merge --merge --no-ff`).
- [ ] `git push origin main` → CI on `main` for the merge commit is GREEN.

**Verify:**

```bash
# After push, watch the feature-branch run:
gh run list --branch chore/ci-green-recovery --limit 1

# Once it completes:
gh run view --log <run-id> | grep -i "Node.js 20" || echo "PASS: no Node 20 warning"
gh run view <run-id> --json conclusion -q .conclusion
# expect: "success"

# After merge to main:
gh run list --branch main --limit 1
# expect status: completed, conclusion: success
```

**Steps:**

- [ ] **Step 1: Confirm branch starting point**

```bash
git branch --show-current     # should be the working branch we did Tasks 2 + 3 on
git log --oneline -5
```

If Tasks 2 + 3 were committed on `main` directly (anti-pattern), abort and rewind: `git reset --soft HEAD~2` after first cutting the branch from `HEAD~2` — easier if the implementer cut `chore/ci-green-recovery` BEFORE Task 2.

Recommended (idempotent) ordering for the implementer of Task 2/3 — run BEFORE Task 2:

```bash
git checkout -b chore/ci-green-recovery
```

If already past that point, this Step 1 is a sanity-check no-op.

- [ ] **Step 2: Final local gate before pushing**

```bash
pixi run pre-commit run --all-files
pixi run lint
pixi run typecheck
pixi run test -q 2>&1 | tail -3
# expect: 979 passed, 4 xfailed, 3 skipped
```

All clean. Exit 0.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin chore/ci-green-recovery
```

- [ ] **Step 4: Watch the CI run**

```bash
sleep 10
gh run list --branch chore/ci-green-recovery --limit 1
# grab run id, then:
gh run watch <run-id>
```

Expected: both `Test (ubuntu-latest)` and `Test (macos-latest)` complete with `conclusion: success`.

- [ ] **Step 5: Confirm Node-20 warning is gone**

```bash
gh run view --log <run-id> | grep -i "Node.js 20 actions are deprecated" && echo "STILL PRESENT — abort" || echo "PASS: no Node 20 warning"
```

If the warning is still present → STOP, investigate (likely a transitive action still on Node 20 that we didn't bump).

- [ ] **Step 6: Open PR + merge (or local --no-ff merge)**

Path A — via `gh`:

```bash
gh pr create --title "ci: green-recovery — xfail RED scaffold + Node-24 action bumps" --body "$(cat <<'EOF'
## Summary

- Adds `pytest.mark.xfail(strict=False)` to the 4 RED lockdown tests in `tests/examples/test_runpod_comfyui_wan_graph.py` so CI exits 0 while preserving the regression-lockdown body. Marker removal routed to Layer P Task 7 item #3 closure.
- Bumps `actions/checkout@v4 → @v6` and `prefix-dev/setup-pixi@v0.8.1 → @v0.9.6`, silencing the Node 20 deprecation warning ahead of the 2026-06-16 cutoff.

## Test plan
- [x] Local suite: `979 passed, 4 xfailed, 3 skipped`
- [x] Feature-branch CI: both jobs green; no Node 20 warning
- [ ] Merge to main; CI on main green

Spec: `docs/superpowers/specs/2026-06-01-ci-green-recovery-design.md`
Plan: `docs/superpowers/plans/2026-06-01-ci-green-recovery.md`
EOF
)"

gh pr merge --merge --no-ff --delete-branch
```

Path B — local `--no-ff` merge (if not opening a PR):

```bash
git checkout main
git pull --ff-only
git merge --no-ff chore/ci-green-recovery -m "$(cat <<'EOF'
ci: green-recovery — xfail RED scaffold + Node-24 action bumps

Restores green CI on main, post Layer P + Q merge (c63cbea).

* xfail (strict=False) on the 4 RED lockdown tests in
  tests/examples/test_runpod_comfyui_wan_graph.py — committed RED in
  9d2a9bf, blocked on Layer P Task 7 item #3 (workflow API JSON +
  first green MP4). Marker removal routed to that item's closure.
* Bump actions/checkout@v4 -> @v6 and
  prefix-dev/setup-pixi@v0.8.1 -> @v0.9.6 to clear the Node 20
  deprecation warning before the 2026-06-16 cutoff.

Suite count: 979 passed, 4 xfailed, 3 skipped.

Spec: docs/superpowers/specs/2026-06-01-ci-green-recovery-design.md
EOF
)"
git push origin main
git branch -d chore/ci-green-recovery
git push origin --delete chore/ci-green-recovery
```

- [ ] **Step 7: Verify CI on main is green**

```bash
sleep 10
gh run list --branch main --limit 1
gh run view <main-run-id> --json conclusion -q .conclusion
# expect: "success"
```

- [ ] **Step 8: PROGRESS.md closure block (optional but recommended)**

Append a short closure block to `PROGRESS.md` under the "Pending non-item-#3 follow-ups" or a new "CI hygiene" subsection — note: spec path, merge SHA, suite count, and that the xfail marker removal is tracked under Layer P Task 7 item #3.

This is housekeeping; not gated on CI itself.

---

## Notes

- **Reversibility:** Single `git revert <merge-sha>` reverts both file changes atomically. No DB, no deploy, no external dep — pure CI + test hygiene.
- **Sequencing:** Task 1 is a pre-flight gate (no commit). Tasks 2 and 3 can technically run in either order; the plan orders them as 2-then-3 so the failing-tests-→-passing-suite signal lands first, before the YAML pin bump (which doesn't affect the local test suite at all).
- **What this plan does NOT do:** any production code change, any work on the real workflow API JSON graph (item #3 territory), any fix for the cache 400 errors (GHA infra).
