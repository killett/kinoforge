# Layer 5a — Luma direct-API retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete every code path that calls the retired Luma Dream Machine direct video API, sweep `provider="luma"` string labels in three peripheral test files, and replace the README's Luma section with a forward-pointing tombstone. Two atomic commits, fully offline.

**Architecture:** Pure source-tree deletion plus four narrow edits to keep the registry, the vendor-confinement invariant, the engine-kind allowlist, and the adapter-self-registration hub consistent. No new code, no new tests, no live spend. Closes the `project_luma_video_retirement_2026.md` carry-forward and clears the way for Layer 5b (`LumaAgentsImageEngine`, separate spec).

**Tech Stack:** Python (kinoforge package layout). Pixi tasks: `pixi run lint | format | typecheck | test | pre-commit run --all-files`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-06-07-luma-direct-api-retirement-design.md` (commit `8189951`).

---

## File Structure

| File | Action | Reason |
|---|---|---|
| `src/kinoforge/engines/luma/__init__.py` | DELETE (164 lines) | The dead `LumaEngine`/`LumaBackend` package targeting the retired API. |
| `tests/engines/test_luma.py` | DELETE (297 lines, 12 tests) | Unit tests for the deleted engine. |
| `examples/configs/comparison/luma-t2v.yaml` | DELETE (30 lines) | Comparison-batch YAML targeting `engine.kind: luma`. |
| `src/kinoforge/_adapters.py` | EDIT (1 line) | Drop `import kinoforge.engines.luma` self-registration line. |
| `src/kinoforge/core/config.py` | EDIT (1 line) | Drop `"luma"` from `KNOWN_ENGINES`. |
| `tests/test_core_invariant.py` | EDIT (5 lines) | Drop the `lumaai` tuple from the vendor-SDK confinement-scan list. |
| `tests/test_examples.py` | EDIT (1 line) | Tighten comparison-YAML kind set from `{"replicate","runway","luma"}` to `{"replicate","runway"}`. |
| `tests/pipeline/test_generate_clip.py` | EDIT (2 sites) | Free-form label sweep: `provider="luma"` → `provider="replicate"`. |
| `tests/outputs/test_local.py` | EDIT (3 sites) | Same sweep; also `model="ray-2"` → `model="seedance-1-lite"` for label coherence. |
| `tests/outputs/test_format_filename.py` | EDIT (1 site + cascading assertions) | Same sweep; downstream filename-substring assertions must follow. |
| `README.md` | EDIT (4 sites) | Bearer-strategy row, section heading, table row → tombstone, echo-line recomment. |
| `PROGRESS.md` | EDIT | New `### Phase 44 — Layer 5a` block, Phase 43 Task 10 stale-count fix, carry-forward flip. |

---

## Pre-flight (run once before Task 1)

Capture the baseline test count so the post-Task-1 drop is provable:

```bash
pixi run test 2>&1 | tail -3 | tee /tmp/kf-pretest-baseline.txt
```

Expected: a line like `===== N passed, M skipped in T s =====`. Record N. After Task 1, the count drops by 13 (12 from the deleted `test_luma.py` plus 1 from the parametrize loop in `test_comparison_yaml_loads` losing the `luma-t2v.yaml` case). Any other drop or any failure is a regression to investigate before committing.

---

### Task 1: Source-tree deletion + edits + label sweep

**Goal:** Remove every code path that calls the retired Luma direct API and every test that imports `LumaEngine`/`LumaBackend`; sweep the three peripheral test files that use `provider="luma"` as a free-form `LocalOutputSink` label so future readers do not infer that a Luma direct engine still exists. Single atomic commit.

**Files:**
- Delete: `src/kinoforge/engines/luma/__init__.py`
- Delete: `tests/engines/test_luma.py`
- Delete: `examples/configs/comparison/luma-t2v.yaml`
- Modify: `src/kinoforge/_adapters.py:29` (drop one import line)
- Modify: `src/kinoforge/core/config.py:81` (drop `"luma"` from `KNOWN_ENGINES`)
- Modify: `tests/test_core_invariant.py:124-128` (drop the `lumaai` tuple)
- Modify: `tests/test_examples.py:124` (tighten kind set)
- Modify: `tests/pipeline/test_generate_clip.py:1480, 1488` (label sweep)
- Modify: `tests/outputs/test_local.py:295, 312, 315` (label + model-string sweep)
- Modify: `tests/outputs/test_format_filename.py:23` (label sweep + cascading filename-substring assertions)

(Line numbers are advisory at the time of writing; grep for the symbols before editing.)

**Acceptance Criteria:**
- [ ] `src/kinoforge/engines/luma/` directory no longer exists.
- [ ] `tests/engines/test_luma.py` no longer exists.
- [ ] `examples/configs/comparison/luma-t2v.yaml` no longer exists.
- [ ] `rg -n "engines\.luma|engines/luma|LumaEngine|LumaBackend" src tests examples` returns no hits.
- [ ] `rg -n 'provider="luma"' tests` returns no hits.
- [ ] `python -c "import kinoforge._adapters"` returns exit 0 with no traceback.
- [ ] `pixi run lint`, `pixi run format`, `pixi run typecheck` are all green.
- [ ] `pixi run test` is green AND the passing-test count is exactly 13 lower than the pre-flight baseline (12 from `test_luma.py` deletion + 1 from the parametrize loop in `test_comparison_yaml_loads`).
- [ ] `pixi run pre-commit run --all-files` is green.
- [ ] Working tree contains the deletions and edits and nothing else.

**Verify:**

```bash
pixi run lint && pixi run format && pixi run typecheck && pixi run test && pixi run pre-commit run --all-files
```

All five subcommands must exit 0. The `pixi run test` step must print a count exactly 13 lower than `/tmp/kf-pretest-baseline.txt`.

**Steps:**

- [ ] **Step 1: Delete the three retired files**

```bash
git rm src/kinoforge/engines/luma/__init__.py
rmdir src/kinoforge/engines/luma  # if pycache empty; otherwise `rm -rf` it
git rm tests/engines/test_luma.py
git rm examples/configs/comparison/luma-t2v.yaml
```

If `src/kinoforge/engines/luma/__pycache__` still exists from a prior import, `rm -rf src/kinoforge/engines/luma` after `git rm` so the directory is gone. The cache is `.gitignore`d so no further git action is needed.

- [ ] **Step 2: Edit `src/kinoforge/_adapters.py` — drop the luma import line**

Locate this line (at line 29 at time of writing, between the `hosted` and `replicate` imports):

```python
import kinoforge.engines.luma  # noqa: F401  # self-registers under "luma"
```

Delete the entire line. Keep the surrounding `hosted` line above and `replicate` line below intact.

- [ ] **Step 3: Edit `src/kinoforge/core/config.py` — drop `"luma"` from `KNOWN_ENGINES`**

Replace the existing set literal (lines 71-82 at time of writing):

```python
KNOWN_ENGINES = {
    "comfyui",
    "diffusers",
    "hosted",
    "fake",
    "fal",
    "bedrock_video",
    # Layer 4 — hosted Bearer-key video providers; no engine-specific YAML block.
    "replicate",
    "runway",
    "luma",
}
```

With:

```python
KNOWN_ENGINES = {
    "comfyui",
    "diffusers",
    "hosted",
    "fake",
    "fal",
    "bedrock_video",
    # Layer 4 — hosted Bearer-key video providers; no engine-specific YAML block.
    "replicate",
    "runway",
}
```

- [ ] **Step 4: Edit `tests/test_core_invariant.py` — drop the `lumaai` confinement tuple**

Locate this block (lines 124-128 at time of writing):

```python
    (
        re.compile(r"^\s*(import|from)\s+lumaai\b"),
        [SRC_ROOT / "engines" / "luma"],
        "lumaai",
    ),
```

Delete the entire 5-line tuple (including the leading and trailing parens and the trailing comma). The neighbouring `runwayml` tuple above and `fal_client` tuple below stay intact. After the edit, the surrounding context reads:

```python
    (
        re.compile(r"^\s*(import|from)\s+runwayml\b"),
        [SRC_ROOT / "engines" / "runway"],
        "runwayml",
    ),
    (
        re.compile(r"^\s*(import|from)\s+fal_client\b"),
        [SRC_ROOT / "engines" / "fal"],
        "fal_client",
    ),
]
```

- [ ] **Step 5: Edit `tests/test_examples.py` — tighten the comparison-YAML kind set**

Locate this line (line 124 at time of writing):

```python
    assert cfg.engine.kind in {"replicate", "runway", "luma"}
```

Replace with:

```python
    assert cfg.engine.kind in {"replicate", "runway"}
```

- [ ] **Step 6: Edit `tests/pipeline/test_generate_clip.py` — sweep label**

Two sites, both around lines 1480-1488 at time of writing. Use this `sed`-style replace (verify the file context first to make sure neither site is wrapped in an assertion that compares against a `luma` substring):

```bash
rg -n 'provider="luma"' tests/pipeline/test_generate_clip.py
```

For each hit, change `provider="luma"` to `provider="replicate"`. If a downstream assertion in the same test compares against a string containing `"luma"` (e.g. expected-filename), update that string to match the new provider (e.g. swap `luma` for `replicate` in the expected substring).

- [ ] **Step 7: Edit `tests/outputs/test_local.py` — sweep label + model string**

Three sites around lines 295, 312, 315 at time of writing. The two later sites also pass `model="ray-2"`. For each occurrence:

- `provider="luma"` → `provider="replicate"`
- `model="ray-2"` → `model="seedance-1-lite"` (a real Replicate model id, keeps the label coherent with the new provider)

Then check the same test bodies for cascading assertions that compare against `"luma"` or `"ray-2"` substrings (e.g. `assert "luma" in fname` or expected-filename literals). Update each to the new provider/model substring.

```bash
rg -n 'luma|ray-2' tests/outputs/test_local.py
```

After the edit, this command must return zero hits.

- [ ] **Step 8: Edit `tests/outputs/test_format_filename.py` — sweep label + cascading assertions**

Single constructor site at line 23 at time of writing. Same rule: `provider="luma"` → `provider="replicate"`. Then sweep cascading assertions:

```bash
rg -n 'luma' tests/outputs/test_format_filename.py
```

If the test asserts that the formatted filename contains the substring `"luma"`, the assertion must be updated to the new provider substring. After the edit, the command above must return zero hits.

- [ ] **Step 9: Verify no orphan Luma references remain in src + tests + examples**

```bash
rg -n "LumaEngine|LumaBackend|engines\.luma|engines/luma|kind: luma|engine: luma|provider=\"luma\"|provider='luma'" src tests examples
```

Expected output: zero hits. If any hit appears, fix it before proceeding.

- [ ] **Step 10: Verify the adapters hub still imports cleanly**

```bash
pixi run python -c "import kinoforge._adapters; print('ok')"
```

Expected: `ok`. No traceback.

- [ ] **Step 11: Run the full verification gate**

```bash
pixi run lint && pixi run format && pixi run typecheck && pixi run test 2>&1 | tail -3 | tee /tmp/kf-posttest.txt && pixi run pre-commit run --all-files
```

Expected: all subcommands exit 0. Compare `/tmp/kf-posttest.txt` to the pre-flight baseline `/tmp/kf-pretest-baseline.txt` — the `N passed` count must be exactly 13 lower. If the drop is wrong, investigate (a side-test may have been removed accidentally, or a test may now fail) before continuing.

- [ ] **Step 12: Commit**

Per the user's CLAUDE.md commit workflow, stage the working tree and create one atomic commit:

```bash
git add -A   # picks up the three deletions plus seven edited files
git status   # sanity-check the staged set; abort if anything unexpected
git commit -m "$(cat <<'EOF'
chore(engines): retire LumaEngine — direct Dream Machine API ended 2026

Luma retired the direct Dream Machine developer video API; the legacy
host now 308-redirects to the consumer subscription dashboard. Reach
Luma video via Bedrock (BedrockVideoEngine + luma-ray.yaml) or
Replicate (luma/ray-flash-2 on ReplicateEngine).

- delete src/kinoforge/engines/luma/ + tests/engines/test_luma.py
- delete examples/configs/comparison/luma-t2v.yaml
- drop "luma" from KNOWN_ENGINES + the _adapters.py import line
- drop the lumaai tuple from the vendor-SDK confinement invariant
- sweep three peripheral test files that used provider="luma" as a
  LocalOutputSink free-form label

Closes the project_luma_video_retirement_2026 carry-forward.
Pairs with Layer 5b (LumaAgentsImageEngine for UNI-1 keyframes,
separate spec).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git status --short
```

The final `git status --short` must print nothing — the tree must be clean after the commit. If pre-commit hooks stage additional files (e.g. `pixi.lock` regen, end-of-file fixes), re-stage them and create one NEW commit (do NOT amend) per the project's git workflow.

---

### Task 2: README tombstone + PROGRESS Phase 44 entry

**Goal:** Replace the README's Hosted-Bearer Luma row with a forward-pointing tombstone, strip "Luma" from the surrounding headings + prose, and record Layer 5a in `PROGRESS.md` under a new Phase 44 block. Single atomic commit, depends on Task 1.

**Files:**
- Modify: `README.md` (4 sites at lines 640, 671, 682, 694 at time of writing)
- Modify: `PROGRESS.md` (append a Phase 44 block, fix the Phase 43 Task 10 count, and update the resume pointer)

**Acceptance Criteria:**
- [ ] `README.md` no longer mentions a "Luma" Bearer-provider row in the Hosted Bearer providers table; the section heading reads `## Hosted Bearer providers (Replicate / Runway)`.
- [ ] A one-paragraph tombstone appears immediately below the Bearer providers table pointing to Bedrock + Replicate as the still-live ways to reach Luma video and to Layer 5b for UNI-1 keyframes.
- [ ] The `echo 'LUMAAI_API_KEY=...' >> .env` line in the quickstart is kept and re-commented to note that the key is reserved for Layer 5b (image keyframes), not the retired video API.
- [ ] The `bearer` strategy row in the auth-strategy table (line 640 at time of writing) reads `(fal, Replicate, Runway)` — `Luma` removed.
- [ ] `PROGRESS.md` gains a new `### Phase 44 — Layer 5a (Luma direct-API retirement)` block at the end of the Post-MVP section listing the deletions, the carry-forward closure, and the commit SHAs from Task 1 and Task 2.
- [ ] The Phase 43 Task 10 line is updated from "3 of 15 YAMLs" to "2 of 15 YAMLs" (stale-fact fix, not a rewrite of history).
- [ ] The Phase 43 Task 13 (Luma Bearer live smoke) DEFERRED note is updated to "CLOSED — API retired, see Layer 5a / Phase 44".
- [ ] If the RESUME pointer or single-next-action block still references LumaEngine as live, it is updated to point to Layer 5a as complete and Layer 5b as the next surface.
- [ ] `rg -n "Luma|luma" README.md | grep -v "luma-ray\|luma\\.ray\|Bedrock\|test_luma_ray_live\|Phase 42\|LUMAAI_API_KEY"` returns only the tombstone paragraph (Bedrock + Replicate + Layer 5b mentions); no stale "Luma" Bearer-provider references remain.
- [ ] `pixi run pre-commit run --all-files` is green.

**Verify:**

```bash
pixi run pre-commit run --all-files && \
  rg -n "Luma|luma" README.md
```

Pre-commit exits 0. The `rg` output is reviewed by eye — every remaining hit must be either the tombstone paragraph, a Bedrock/Bedrock-Video reference (which is the still-live path), the `luma-ray.yaml` example name, the `LUMAAI_API_KEY` echo line (with the new Layer-5b comment), or the Phase 42 historical entry. Any other hit is a stale reference to fix.

**Steps:**

- [ ] **Step 1: Edit `README.md` — strip "Luma" from the auth-strategy table row**

Locate this line (around line 640 at time of writing):

```markdown
| `bearer` | `HostedAPIEngine` (fal today; Replicate / Runway / Luma later) | `Authorization: Bearer <env-var>` |
```

Replace with:

```markdown
| `bearer` | `HostedAPIEngine` (fal, Replicate, Runway) | `Authorization: Bearer <env-var>` |
```

(The "today / later" phrasing predates Layer 4 landing all three Bearer providers; this edit also stale-fact-fixes the surrounding text.)

- [ ] **Step 2: Edit `README.md` — strip "Luma" from the Hosted Bearer section heading**

Locate this line (around line 671 at time of writing):

```markdown
## Hosted Bearer providers (Replicate / Runway / Luma)
```

Replace with:

```markdown
## Hosted Bearer providers (Replicate / Runway)
```

- [ ] **Step 3: Edit `README.md` — remove the Luma table row, insert the tombstone**

Locate this row in the Bearer-provider wire-shape table (around line 682 at time of writing):

```markdown
| Luma | `luma` | `LUMAAI_API_KEY` | `state` (lowercase) | `assets.video: str` |
```

Delete the entire row.

Then, immediately after the closing of the table (after the row for Runway) and before the next paragraph beginning "Each engine's `provision()`...", insert this tombstone paragraph:

```markdown
> **Luma direct video API retired 2026.** The legacy
> `api.lumalabs.ai/dream-machine/...` endpoint was retired by the
> provider and now 308-redirects to the consumer dashboard. Reach Luma
> video models via AWS Bedrock (`luma.ray-v2:0`, see the Bedrock Video
> section below) or Replicate (`luma/ray-flash-2`, see the Replicate
> row above). UNI-1 image-keyframe support via `LumaAgentsImageEngine`
> is planned in Layer 5b — track the `LUMAAI_API_KEY` env var, which
> is reserved for that engine.
```

- [ ] **Step 4: Edit `README.md` — recomment the LUMAAI_API_KEY echo line**

Locate this line in the comparison-batch quickstart (around line 694 at time of writing):

```bash
echo 'LUMAAI_API_KEY=luma-zzzzz'    >> .env
```

Replace with:

```bash
# LUMAAI_API_KEY (reserved for Layer 5b UNI-1 keyframe engine; direct video API retired)
# echo 'LUMAAI_API_KEY=luma-zzzzz' >> .env
```

The two `echo` lines for `REPLICATE_API_TOKEN` and `RUNWAYML_API_SECRET` above stay live (they still drive Bearer-provider live smokes). Only the Luma echo is commented out — leaving the env-var name on a hash-line keeps it greppable from the README without an active `.env` mutation.

- [ ] **Step 5: Append a Phase 44 block to `PROGRESS.md`**

Locate the end of the Post-MVP section (the very last `### Phase NN — ...` block in the file at the time of writing). Append immediately after it:

```markdown
### Phase 44 — Layer 5a (Luma direct-API retirement, deletion-only)

Luma retired the Dream Machine direct video API in 2026; the dead
`LumaEngine` package that targeted it and its 12-test unit-test file
are removed in this layer. The carry-forward in project memory
`project_luma_video_retirement_2026.md` is now CLOSED.

Spec: `docs/superpowers/specs/2026-06-07-luma-direct-api-retirement-design.md`.
Plan: `docs/superpowers/plans/2026-06-07-layer-5a-luma-retirement.md`.

- [x] Task 1: code + test deletions + label sweep — commit `<TASK1-SHA>`
- [x] Task 2: README tombstone + PROGRESS Phase 44 entry — this commit (`<TASK2-SHA>`)

**Files removed:**
- `src/kinoforge/engines/luma/__init__.py` (164 lines)
- `tests/engines/test_luma.py` (297 lines, 12 tests)
- `examples/configs/comparison/luma-t2v.yaml` (30 lines)

**Files edited (1-5 line changes):**
- `src/kinoforge/_adapters.py` — drop the `engines.luma` self-registration import.
- `src/kinoforge/core/config.py` — drop `"luma"` from `KNOWN_ENGINES`.
- `tests/test_core_invariant.py` — drop the `lumaai` tuple from the vendor-confinement scan list.
- `tests/test_examples.py` — tighten the comparison-YAML kind allowlist set to `{"replicate","runway"}`.
- `tests/pipeline/test_generate_clip.py`, `tests/outputs/test_local.py`,
  `tests/outputs/test_format_filename.py` — sweep `provider="luma"`
  free-form labels to `provider="replicate"`.
- `README.md` — strip Luma from the Bearer-strategy table row, the
  Hosted Bearer section heading, and the wire-shape table; insert a
  forward-pointing tombstone paragraph; recomment the
  `LUMAAI_API_KEY` echo line in the quickstart.

**Test count:** N pre-Layer-5a → N − 13 post-Layer-5a (12 from the deleted
`test_luma.py` plus 1 from the comparison-YAML parametrize loop losing
`luma-t2v.yaml`).

**Live spend:** $0. Fully offline source-tree deletion; no provider
calls, no cloud mutations.

**Out of scope — landed in a separate spec:**

- `LumaAgentsImageEngine` for UNI-1 image keyframes (Layer 5b).
- Anything Bedrock-side (Luma Ray v2 lives there and is unaffected).

Closes carry-forward: `project_luma_video_retirement_2026.md`.
```

Substitute `<TASK1-SHA>` with the actual short SHA from Task 1's commit (run `git log --oneline -1` after Task 1 lands to get it) and `<TASK2-SHA>` with the SHA of this commit (added as a post-commit `git commit --amend`-free follow-up — i.e. once you have the SHA, edit the line and create a tiny third commit or backfill in a future progress-update layer; do NOT amend the docs commit). The simplest approach: leave `<TASK2-SHA>` as the literal string in the initial Task-2 commit, then immediately after the commit lands run `git log --oneline -1` and follow the project's existing PROGRESS-SHA-backfill pattern (see e.g. Phase 17 Task 6 SHA backfill `eed9706`).

- [ ] **Step 6: Edit `PROGRESS.md` — fix the Phase 43 Task 10 count**

Search for the existing Phase 43 entry and the carry-forward line that reads:

```markdown
- [PARTIAL] Task 10: Comparison configs — 3 of 15 YAMLs (t2v only) — commit `a054877`. i2v/flf2v/keyframe-prestage/manifest deferred.
```

Replace `3 of 15` with `2 of 15` (Luma t2v YAML was deleted in Task 1). The rest of the line stays identical:

```markdown
- [PARTIAL] Task 10: Comparison configs — 2 of 15 YAMLs (t2v only) — commit `a054877`. i2v/flf2v/keyframe-prestage/manifest deferred; luma-t2v.yaml removed in Phase 44.
```

- [ ] **Step 7: Edit `PROGRESS.md` — update the Phase 43 Task 13 deferral note**

Search for the existing line:

```markdown
- [DEFERRED] Task 13: Luma live smoke — `LUMAAI_API_KEY` returns 403 at provider (verified bypassing SDK). User-side credential.
```

Replace with:

```markdown
- [CLOSED] Task 13: Luma live smoke — API was retired by the provider in 2026; see Phase 44 / Layer 5a. The 403 observed at deferral time was the provider winding the endpoint down.
```

- [ ] **Step 8: Edit `PROGRESS.md` — update the RESUME pointer**

Open the `### RESUME — START HERE` block (around line 213 at time of writing). If any line references `LumaEngine` as live or implies the direct Luma API is still operative, replace it with a one-line pointer to Phase 44: "Phase 44 closes the Luma direct-API carry-forward; Layer 5b adds `LumaAgentsImageEngine` (UNI-1 image keyframes) — separate spec."

If the RESUME block does not mention Luma at all, this step is a no-op; leave the block alone.

- [ ] **Step 9: Sweep README + PROGRESS for any orphan Luma references**

```bash
rg -n "Luma|luma" README.md | grep -v "luma-ray\|luma\\.ray\|Bedrock\|test_luma_ray_live\|Phase 42\|LUMAAI_API_KEY\|LumaAgentsImageEngine"
```

Review every remaining hit by eye. Every one must be either (a) the new tombstone paragraph, (b) the new Layer-5b forward pointer, or (c) the existing Bedrock-side prose. Anything else is a stale reference — fix it.

Repeat for `PROGRESS.md`:

```bash
rg -n "LumaEngine|LumaBackend|engines\.luma|engines/luma" PROGRESS.md
```

Every hit must be inside a historical Phase entry or inside the new Phase 44 block — these are immutable history and stay as-is. There must be no claim that the direct API is still operative.

- [ ] **Step 10: Verify pre-commit + commit**

```bash
pixi run pre-commit run --all-files
```

Expected: green. Then commit:

```bash
git add README.md PROGRESS.md
git status   # sanity-check; no other files should be staged
git commit -m "$(cat <<'EOF'
docs(readme,progress): mark Luma direct API retired (Layer 5a)

- README: strip Luma from the Hosted Bearer providers section
  heading, the wire-shape table, and the auth-strategy table row;
  insert a forward-pointing tombstone paragraph that lists the
  still-live ways to reach Luma video (Bedrock, Replicate) and
  points at Layer 5b for UNI-1 image keyframes; recomment the
  LUMAAI_API_KEY echo line in the quickstart so future readers see
  it is reserved for Layer 5b, not the retired video API.
- PROGRESS: new Phase 44 block recording the Layer 5a deletion +
  the closed carry-forward; Phase 43 Task 10 count fixed from
  3-of-15 to 2-of-15; Phase 43 Task 13 (Luma Bearer live smoke)
  moved from DEFERRED to CLOSED with the retirement note.

Closes the project_luma_video_retirement_2026 carry-forward.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git status --short
```

Final `git status --short` must print nothing. If pre-commit hooks stage extra files, re-stage and create one NEW commit (do NOT amend) per the project's git workflow.

- [ ] **Step 11: Backfill the Task 2 SHA into the Phase 44 block (optional polish)**

If you left `<TASK2-SHA>` as a literal in the Phase 44 block, after the docs commit lands run:

```bash
git log --oneline -1
```

Then either: (a) follow the existing PROGRESS-SHA-backfill pattern (small follow-up commit like Phase 17's `eed9706`), or (b) accept the literal as a known cosmetic gap that will be cleaned up in the next docs-touching commit. Either is consistent with how prior phases handled this. Do NOT amend the docs commit.

---

## Post-flight (after both tasks land)

Final-state sanity:

```bash
git log --oneline -3
# expect:
#   <task2-sha> docs(readme,progress): mark Luma direct API retired (Layer 5a)
#   <task1-sha> chore(engines): retire LumaEngine — direct Dream Machine API ended 2026
#   8189951    docs(spec): Layer 5a — Luma direct-API retirement (deletion-only)

git status --short
# expect: empty

rg -n "LumaEngine|LumaBackend|engines\.luma|engines/luma|kind: luma|engine: luma" src tests examples
# expect: zero hits

pixi run test
# expect: green; count exactly 13 lower than the pre-Layer-5a baseline
```

If any of these fail, do NOT push or merge until they pass.

---

## Notes for the implementer

- The plan calls out advisory line numbers at the time of writing. Always grep for the symbol first; the line numbers will shift after each edit. Use the `old_string` content shown in each step as the contract, not the line number.
- The `provider="luma"` sweep in three test files is the only step that requires reading downstream test bodies to catch cascading assertions on `"luma"` or `"ray-2"` substrings. Treat the constructor sites as the entry point, not the full edit.
- No new tests are written in this layer. The verification gate is purely "the existing suite is still green, the count drops by exactly 13, and the orphan-reference greps return zero hits". A green test run with no count drop is itself a regression — it means a deleted test was secretly excluded somewhere.
- The repository has a strict `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` trailer on every Claude-authored commit. Both commits in this plan include it.
- If pre-commit hooks insist on staging additional files (most commonly `pixi.lock` when deps change — they don't in this layer, but the hook may still run a YAML check that touches `pixi.lock`), re-stage and create a NEW commit; per the project's git workflow, never `--amend`.
