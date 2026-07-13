# Uniform config filename scheme — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename every config in `examples/configs/` to one uniform, self-describing scheme (`<provider>-<engine>-<subject>-<qualifier>-<operation>`) and update all functional + living-doc references in lockstep, leaving dated archives frozen.

**Architecture:** Pure mechanical refactor. Each task renames one config *class* with `git mv` (history preserved), then updates every non-frozen reference to those exact basenames via a deterministic `rg -l → sed` sweep, then verifies with the offline test suite + a "zero stale basename" grep gate. No config *content* changes except intra-`configs/` reference lines (grid sweep-spec `config:` paths) and the DEAD-config deletion.

**Tech Stack:** git, ripgrep (`rg`), sed, pytest via `pixi run`, pre-commit.

**User decisions (already made):**
- Scope: "Every config, uniform scheme."
- Old dated docs (`docs/superpowers/plans/` + `specs/`): "No — leave archives frozen."
- Odd classes (grids/manifests/root smokes): "Full prefix everywhere" — EXCEPT `.grid.yaml` sweep specs, which have no engine and can span sizes → version-normalize only (approved carve-out).
- Tool/demo configs: "Normalize what fits, leave pure-tool" (`diffusers.yaml`→`runpod-diffusers-serverless.yaml`; `cost`/`sweeper`/`hosted`/`local-fake` unchanged).
- DEAD `runpod-comfyui-wan-t2v-14b-2_2.yaml`: "Delete it."
- Two-stage configs: "Chain both ops."
- Flagged judgment calls approved: sweep-spec carve-out; `fal.yaml`→`fal-t2v.yaml`; `keyframe-luma.yaml`→`fal-luma-keyframe-i2v.yaml`.

**Spec:** `docs/superpowers/specs/2026-07-12-config-filename-scheme-design.md`

---

## Shared conventions (read once, applies to every task)

**Frozen paths — NEVER edit (exclude from every sweep):**

```
-g '!.pixi/**' -g '!.git/**' -g '!output/**' -g '!.kinoforge/**' \
-g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' \
-g '!tests/live/evidence/**'
```

Note: this plan file lives under `docs/superpowers/plans/` and is therefore itself frozen from the sweeps — correct; its old→new tables are documentation, not live references.

**The reference-update primitive** (used in every task). For a fully-unique basename `OLD.yaml → NEW.yaml`:

```bash
rg -l -F 'OLD.yaml' \
  -g '!.pixi/**' -g '!.git/**' -g '!output/**' -g '!.kinoforge/**' \
  -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' \
  -g '!tests/live/evidence/**' \
| xargs -r sed -i 's|OLD\.yaml|NEW.yaml|g'
```

**Ambiguous short name — `wan.yaml` only.** `wan.yaml` is a suffix of `runpod-comfyui-wan.yaml`, so a bare `-F wan.yaml` sed would corrupt that sibling. `wan.yaml` is referenced only two ways in non-frozen files: as a path `examples/configs/wan.yaml` (preceded by `/`) and as a quoted bare `"wan.yaml"` (in `test_examples.py` lists). Both are safe to target explicitly; the anchored commands are given in Task 1. The other short names (`fal.yaml`, `diffusers.yaml`, `skypilot.yaml`) are NOT suffixes of any other config name, so plain `-F` is safe for them.

**Per-task verify gate (all tasks):**
1. `pixi run pytest tests/test_examples.py -q` → PASS (primary offline gate; hardcodes many names).
2. The task's targeted offline test(s) → PASS.
3. **Zero stale basename** — for every OLD name renamed in the task:
   `rg -F 'OLD.yaml' <frozen-excludes>` → **no output** (exit 1). Frozen archives may still contain it; that is intended.
4. `pixi run test` → collection succeeds (catches any module-level `load_config` on a moved path in live-test files, which are otherwise skipped).

**Live tests** (`tests/live/**`, `tests/smoke/**`) require cloud creds + `KINOFORGE_LIVE_TESTS=1` and DO NOT execute in `pixi run test`. Their path correctness is verified by the grep-zero gate (step 3) + successful collection (step 4), NOT by running them. Do not spend to validate a rename.

---

## Task 0: Baseline capture

**Goal:** Record a green offline baseline so every later task compares against a known-good state.

**Files:**
- None modified.

**Acceptance Criteria:**
- [ ] Offline suite result captured to `/tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/baseline.txt`.
- [ ] Working tree clean at start (spec already committed).

**Verify:** `git status --porcelain` → empty; baseline file exists.

**Steps:**

- [ ] **Step 1: Confirm clean tree**

```bash
cd /workspace
git status --porcelain   # expected: empty
```

- [ ] **Step 2: Capture baseline suite result**

```bash
cd /workspace
pixi run test > /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/baseline.txt 2>&1; echo "exit=$?" | tee -a /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/baseline.txt
tail -5 /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/baseline.txt
```
Expected: suite passes (exit 0). If baseline is red, STOP and surface — do not rename on top of a broken tree.

- [ ] **Step 3: No commit** (nothing changed).

---

## Task 1: RunPod generation configs (comfyui + diffusers) + delete DEAD

**Goal:** Rename the 10 RunPod text/image-gen configs to the new scheme and delete the DEAD collision config.

**Files:**
- Rename (git mv), ComfyUI/RunPod:
  - `examples/configs/wan.yaml` → `examples/configs/runpod-comfyui-wan-2_2-14b-t2v.yaml`
  - `examples/configs/runpod-comfyui-wan-t2v.yaml` → `examples/configs/runpod-comfyui-wan-2_1-14b-t2v.yaml`
  - `examples/configs/runpod-comfyui-wan.yaml` → `examples/configs/runpod-comfyui-wan-2_1-14b-i2v.yaml`
  - `examples/configs/runpod-comfyui-wan-t2v-1_3b.yaml` → `examples/configs/runpod-comfyui-wan-2_1-1_3b-t2v.yaml`
  - `examples/configs/runpod-comfyui-wan-t2v-5b.yaml` → `examples/configs/runpod-comfyui-wan-2_2-5b-t2v.yaml`
- Rename (git mv), Diffusers/RunPod:
  - `examples/configs/runpod-diffusers-wan-t2v-14b-2_2.yaml` → `examples/configs/runpod-diffusers-wan-2_2-14b-t2v.yaml`
  - `examples/configs/wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml` → `examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml`
  - `examples/configs/wan21-1_3b-strength-grid.yaml` → `examples/configs/runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid.yaml`
  - `examples/configs/wan22-14b-lora-flexible-warm-reuse-release.yaml` → `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release.yaml`
  - `examples/configs/wan22-14b-strength-grid.yaml` → `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-strength-grid.yaml`
- Delete: `examples/configs/runpod-comfyui-wan-t2v-14b-2_2.yaml` (DEAD)
- Reference sites updated by the sweep include (non-exhaustive): `tests/test_examples.py`, `tests/live/test_comfyui_wan_live.py`, `tests/live/test_comfyui_wan_t2v_live.py`, `tests/live/test_diffusers_wan_t2v_live.py`, `tests/live/test_diffusers_wan_t2v_4prompt_live.py`, `tests/live/test_runpod_ephemeral_warm_reuse_smoke.py`, `tests/live/_ephemeral_warm_reuse_smoke_evidence.json`, `tests/live/cfg_c25_wan_comfyui.yaml`, `tests/engines/test_comfyui_wan_t2v_graph_shape.py`, `tests/examples/test_runpod_comfyui_wan_graph.py`, `tests/smoke/live_wan21/*`, `tests/smoke/release_wan22/*`, `tests/_smoke_harness/lora_swap_grid.py`, `tests/cli/test_cmd_generate_loras.py`, `examples/configs/manifests/batch-prompts.yaml` (comment), `README.md`, `PROGRESS.md`, `successful-generations.md`, `docs/*.md`.

**Acceptance Criteria:**
- [ ] All 10 renames land as `git mv` (rename detected in `git status`).
- [ ] DEAD config deleted; `rg -F 'runpod-comfyui-wan-t2v-14b-2_2.yaml' <frozen-excludes>` → empty.
- [ ] Each of the 10 old basenames → zero non-frozen hits.
- [ ] `test_examples.py` list/functions updated (`"wan.yaml"`, `"runpod-comfyui-wan.yaml"`, `runpod-comfyui-wan-t2v-5b.yaml`, `runpod-diffusers-wan-t2v-14b-2_2.yaml` references all resolve to new names).
- [ ] Verify gate passes.

**Verify:** `pixi run pytest tests/test_examples.py tests/engines/test_comfyui_wan_t2v_graph_shape.py tests/examples/test_runpod_comfyui_wan_graph.py -q` → PASS.

**Steps:**

- [ ] **Step 1: Confirm DEAD config has no functional refs, then delete**

```bash
cd /workspace
rg -F 'runpod-comfyui-wan-t2v-14b-2_2.yaml' \
  -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' \
  -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**'
# Expected: no output (no live refs). If any appear, they are DEAD-marker mentions
# in living docs — the Step 4 sweep will not touch this name; delete such mentions
# manually only if they claim the file exists.
git rm examples/configs/runpod-comfyui-wan-t2v-14b-2_2.yaml
```

- [ ] **Step 2: git mv the 5 ComfyUI configs**

```bash
cd /workspace/examples/configs
git mv wan.yaml runpod-comfyui-wan-2_2-14b-t2v.yaml
git mv runpod-comfyui-wan-t2v.yaml runpod-comfyui-wan-2_1-14b-t2v.yaml
git mv runpod-comfyui-wan.yaml runpod-comfyui-wan-2_1-14b-i2v.yaml
git mv runpod-comfyui-wan-t2v-1_3b.yaml runpod-comfyui-wan-2_1-1_3b-t2v.yaml
git mv runpod-comfyui-wan-t2v-5b.yaml runpod-comfyui-wan-2_2-5b-t2v.yaml
```

- [ ] **Step 3: git mv the 5 Diffusers configs**

```bash
cd /workspace/examples/configs
git mv runpod-diffusers-wan-t2v-14b-2_2.yaml runpod-diffusers-wan-2_2-14b-t2v.yaml
git mv wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke.yaml
git mv wan21-1_3b-strength-grid.yaml runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid.yaml
git mv wan22-14b-lora-flexible-warm-reuse-release.yaml runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release.yaml
git mv wan22-14b-strength-grid.yaml runpod-diffusers-wan-2_2-14b-t2v-strength-grid.yaml
```

- [ ] **Step 4: Sweep references — unique long basenames (order: longest first is irrelevant, all unique)**

```bash
cd /workspace
EXCL="-g !.pixi/** -g !.git/** -g !output/** -g !.kinoforge/** -g !docs/superpowers/plans/** -g !docs/superpowers/specs/** -g !tests/live/evidence/**"

replace() {  # $1=old (no .yaml)  $2=new (no .yaml)
  rg -l -F "$1.yaml" $EXCL | xargs -r sed -i "s|$1\.yaml|$2.yaml|g"
}

replace runpod-comfyui-wan-t2v-1_3b runpod-comfyui-wan-2_1-1_3b-t2v
replace runpod-comfyui-wan-t2v-5b   runpod-comfyui-wan-2_2-5b-t2v
replace runpod-comfyui-wan-t2v      runpod-comfyui-wan-2_1-14b-t2v
replace runpod-diffusers-wan-t2v-14b-2_2 runpod-diffusers-wan-2_2-14b-t2v
replace wan21-1_3b-lora-flexible-warm-reuse-smoke runpod-diffusers-wan-2_1-1_3b-t2v-lora-flexible-warm-reuse-smoke
replace wan21-1_3b-strength-grid    runpod-diffusers-wan-2_1-1_3b-t2v-strength-grid
replace wan22-14b-lora-flexible-warm-reuse-release runpod-diffusers-wan-2_2-14b-t2v-lora-flexible-warm-reuse-release
replace wan22-14b-strength-grid     runpod-diffusers-wan-2_2-14b-t2v-strength-grid
```

Ordering note: `replace runpod-comfyui-wan-t2v-1_3b` and `-5b` run BEFORE the shorter `runpod-comfyui-wan-t2v`, so the longer, more-specific names are consumed first and the generic `-t2v` sed cannot mangle them (the `.yaml` boundary in the pattern already prevents overlap, but longest-first removes all doubt).

- [ ] **Step 5: Sweep the two ambiguous forms of `runpod-comfyui-wan.yaml` and `wan.yaml`**

`runpod-comfyui-wan.yaml` is unique (no other name ends in it after Step 4 renames), so plain replace is safe. `wan.yaml` needs the anchored pair.

```bash
cd /workspace
EXCL="-g !.pixi/** -g !.git/** -g !output/** -g !.kinoforge/** -g !docs/superpowers/plans/** -g !docs/superpowers/specs/** -g !tests/live/evidence/**"

# i2v sibling — unique tail, safe:
rg -l -F 'runpod-comfyui-wan.yaml' $EXCL | xargs -r sed -i 's|runpod-comfyui-wan\.yaml|runpod-comfyui-wan-2_1-14b-i2v.yaml|g'

# wan.yaml — path form (preceded by '/'):
rg -l 'configs/wan\.yaml' $EXCL | xargs -r sed -i 's|configs/wan\.yaml|configs/runpod-comfyui-wan-2_2-14b-t2v.yaml|g'
# wan.yaml — quoted bare form (test_examples.py lists):
rg -l '"wan\.yaml"' $EXCL | xargs -r sed -i 's|"wan\.yaml"|"runpod-comfyui-wan-2_2-14b-t2v.yaml"|g'
```

- [ ] **Step 6: Manually confirm no other bare `wan.yaml` mention survives in living docs**

```bash
cd /workspace
rg -n 'wan\.yaml' -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' \
  -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**' \
| rg -v 'comfyui-wan-2_1-14b-i2v\.yaml|runpod-comfyui-wan-2_2-14b-t2v\.yaml|wan-2_2-14b|wan-2_1'
# Expected: no line that is a bare `wan.yaml` reference. Any prose hit (e.g. a
# README sentence "see wan.yaml") → edit by hand to the new name.
```

- [ ] **Step 7: Run verify gate**

```bash
cd /workspace
pixi run pytest tests/test_examples.py tests/engines/test_comfyui_wan_t2v_graph_shape.py tests/examples/test_runpod_comfyui_wan_graph.py -q
for n in wan runpod-comfyui-wan-t2v runpod-comfyui-wan runpod-comfyui-wan-t2v-1_3b runpod-comfyui-wan-t2v-5b runpod-comfyui-wan-t2v-14b-2_2 runpod-diffusers-wan-t2v-14b-2_2 wan21-1_3b-lora-flexible-warm-reuse-smoke wan21-1_3b-strength-grid wan22-14b-lora-flexible-warm-reuse-release wan22-14b-strength-grid; do
  hits=$(rg -c -F "$n.yaml" -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**' 2>/dev/null | grep -v 'wan21-1_3b-strength-grid.*wan-2_1' )
  echo "STALE $n.yaml -> ${hits:-none}"
done
pixi run test >/tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t1.txt 2>&1; tail -3 /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t1.txt
```
Expected: pytest PASS; every `STALE … -> none` (note `wan.yaml` line: ignore matches that are substrings of new names — the sweep in Steps 4–6 removed all true refs; a `wan.yaml` count >0 means a genuine bare ref survived → fix). `pixi run test` PASS.

- [ ] **Step 8: Commit**

```bash
cd /workspace
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(configs): rename RunPod gen configs to uniform scheme; delete DEAD"
```

---

## Task 2: Modal configs

**Goal:** Rename the 4 Modal configs (add explicit `diffusers` engine + operation-last).

**Files:**
- `examples/configs/modal-wan-t2v-1_3b.yaml` → `examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml`
- `examples/configs/modal-wan-t2v-14b-2_2.yaml` → `examples/configs/modal-diffusers-wan-2_2-14b-t2v.yaml`
- `examples/configs/modal-flashvsr-x4.yaml` → `examples/configs/modal-diffusers-flashvsr-x4-upscale.yaml`
- `examples/configs/modal-rife-60fps.yaml` → `examples/configs/modal-diffusers-rife-60fps-interpolate.yaml`
- Reference sites (swept): `tests/test_modal_config.py`, `tests/test_modal_rife_config.py`, `tests/live/test_modal_wan_t2v_1_3b.py`, `tests/live/test_modal_wan_t2v_14b_2_2.py`, `tests/live/test_modal_util_probe.py`, `tests/live/test_modal_warm_reuse_hf_cache.py`, `tests/live/test_modal_flashvsr_x4.py`, `tests/live/test_modal_rife_60fps.py`, `tests/providers/modal/test_image_bake.py`, `tests/engines/diffusers/test_render_provision_split.py`, `tests/engines/diffusers/_golden_provision.json`, `tests/core/test_orchestrator_provision_threading.py`, `pixi.toml`, living docs.

**Acceptance Criteria:**
- [ ] All 4 renames land as `git mv`.
- [ ] Each old basename → zero non-frozen hits.
- [ ] `pixi.toml` Modal config refs (if any) updated.
- [ ] `_golden_provision.json` config-path field updated to the new name (golden is compared byte-for-byte in `test_render_provision_split.py`).
- [ ] Verify gate passes.

**Verify:** `pixi run pytest tests/test_modal_config.py tests/test_modal_rife_config.py tests/engines/diffusers/test_render_provision_split.py -q` → PASS.

**Steps:**

- [ ] **Step 1: git mv**

```bash
cd /workspace/examples/configs
git mv modal-wan-t2v-1_3b.yaml   modal-diffusers-wan-2_1-1_3b-t2v.yaml
git mv modal-wan-t2v-14b-2_2.yaml modal-diffusers-wan-2_2-14b-t2v.yaml
git mv modal-flashvsr-x4.yaml    modal-diffusers-flashvsr-x4-upscale.yaml
git mv modal-rife-60fps.yaml     modal-diffusers-rife-60fps-interpolate.yaml
```

- [ ] **Step 2: Sweep references** (all four basenames unique)

```bash
cd /workspace
EXCL="-g !.pixi/** -g !.git/** -g !output/** -g !.kinoforge/** -g !docs/superpowers/plans/** -g !docs/superpowers/specs/** -g !tests/live/evidence/**"
replace() { rg -l -F "$1.yaml" $EXCL | xargs -r sed -i "s|$1\.yaml|$2.yaml|g"; }
replace modal-wan-t2v-1_3b   modal-diffusers-wan-2_1-1_3b-t2v
replace modal-wan-t2v-14b-2_2 modal-diffusers-wan-2_2-14b-t2v
replace modal-flashvsr-x4    modal-diffusers-flashvsr-x4-upscale
replace modal-rife-60fps     modal-diffusers-rife-60fps-interpolate
```

- [ ] **Step 3: Verify gate**

```bash
cd /workspace
pixi run pytest tests/test_modal_config.py tests/test_modal_rife_config.py tests/engines/diffusers/test_render_provision_split.py -q
for n in modal-wan-t2v-1_3b modal-wan-t2v-14b-2_2 modal-flashvsr-x4 modal-rife-60fps; do
  echo "STALE $n.yaml:"; rg -F "$n.yaml" -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**'
done
pixi run test >/tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t2.txt 2>&1; tail -3 /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t2.txt
```
Expected: pytest PASS; each `STALE …` prints nothing; `pixi run test` PASS.

- [ ] **Step 4: Commit**

```bash
cd /workspace
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(configs): rename Modal configs to uniform scheme"
```

---

## Task 3: RunPod upscale / interpolate / two-stage configs

**Goal:** Rename the 6 upscale/interpolate configs + 4 two-stage pipeline configs.

**Files:**
- Upscale / interpolate:
  - `examples/configs/upscale-flashvsr-x4.yaml` → `examples/configs/runpod-diffusers-flashvsr-x4-upscale.yaml`
  - `examples/configs/upscale-flashvsr-x4-torch26.yaml` → `examples/configs/runpod-diffusers-flashvsr-x4-torch26-upscale.yaml`
  - `examples/configs/upscale-flashvsr-1080p.yaml` → `examples/configs/runpod-diffusers-flashvsr-1080p-upscale.yaml`
  - `examples/configs/upscale-spandrel-x2.yaml` → `examples/configs/runpod-diffusers-spandrel-x2-upscale.yaml`
  - `examples/configs/interpolate-rife-60fps.yaml` → `examples/configs/runpod-diffusers-rife-60fps-interpolate.yaml`
  - `examples/configs/extras/upscale-seedvr2-3b.yaml` → `examples/configs/extras/runpod-diffusers-seedvr2-3b-upscale.yaml`
- Two-stage (Wan 2.2 14B t2v):
  - `examples/configs/wan-with-upscale-flashvsr.yaml` → `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-flashvsr-upscale.yaml`
  - `examples/configs/wan-with-upscale-flashvsr-1080p.yaml` → `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-flashvsr-1080p-upscale.yaml`
  - `examples/configs/wan-with-upscale-spandrel.yaml` → `examples/configs/runpod-diffusers-wan-2_2-14b-t2v-spandrel-upscale.yaml`
  - `examples/configs/extras/wan-with-upscale-seedvr2.yaml` → `examples/configs/extras/runpod-diffusers-wan-2_2-14b-t2v-seedvr2-upscale.yaml`
- Reference sites (swept): `tests/test_examples.py`, `tests/core/test_config.py`, `tests/core/test_config_upscale.py`, `tests/live/test_flashvsr_live.py`, `tests/live/test_flashvsr_height_target_live.py`, `tests/live/test_rife_interpolate_live.py`, `tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py`, `tests/live/test_wan_then_spandrel_warm_reuse_smoke.py`, `src/kinoforge/cli/_commands.py`, living docs.

**Acceptance Criteria:**
- [ ] All 10 renames land as `git mv` (2 under `extras/`).
- [ ] `src/kinoforge/cli/_commands.py` references (`upscale-spandrel-x2.yaml`, `interpolate-rife-60fps.yaml` in CLI help/examples) updated.
- [ ] Each old basename → zero non-frozen hits.
- [ ] Verify gate passes.

**Verify:** `pixi run pytest tests/test_examples.py tests/core/test_config.py tests/core/test_config_upscale.py -q` → PASS.

**Steps:**

- [ ] **Step 1: git mv (longest/most-specific first to avoid prefix overlap)**

```bash
cd /workspace/examples/configs
# two-stage (contain 'flashvsr'/'spandrel' but prefixed by wan-with-upscale)
git mv wan-with-upscale-flashvsr-1080p.yaml runpod-diffusers-wan-2_2-14b-t2v-flashvsr-1080p-upscale.yaml
git mv wan-with-upscale-flashvsr.yaml       runpod-diffusers-wan-2_2-14b-t2v-flashvsr-upscale.yaml
git mv wan-with-upscale-spandrel.yaml       runpod-diffusers-wan-2_2-14b-t2v-spandrel-upscale.yaml
git mv extras/wan-with-upscale-seedvr2.yaml extras/runpod-diffusers-wan-2_2-14b-t2v-seedvr2-upscale.yaml
# upscale-only / interpolate
git mv upscale-flashvsr-x4-torch26.yaml runpod-diffusers-flashvsr-x4-torch26-upscale.yaml
git mv upscale-flashvsr-x4.yaml         runpod-diffusers-flashvsr-x4-upscale.yaml
git mv upscale-flashvsr-1080p.yaml      runpod-diffusers-flashvsr-1080p-upscale.yaml
git mv upscale-spandrel-x2.yaml         runpod-diffusers-spandrel-x2-upscale.yaml
git mv interpolate-rife-60fps.yaml      runpod-diffusers-rife-60fps-interpolate.yaml
git mv extras/upscale-seedvr2-3b.yaml   extras/runpod-diffusers-seedvr2-3b-upscale.yaml
```

- [ ] **Step 2: Sweep references (longest first)**

```bash
cd /workspace
EXCL="-g !.pixi/** -g !.git/** -g !output/** -g !.kinoforge/** -g !docs/superpowers/plans/** -g !docs/superpowers/specs/** -g !tests/live/evidence/**"
replace() { rg -l -F "$1.yaml" $EXCL | xargs -r sed -i "s|$1\.yaml|$2.yaml|g"; }
replace wan-with-upscale-flashvsr-1080p runpod-diffusers-wan-2_2-14b-t2v-flashvsr-1080p-upscale
replace wan-with-upscale-flashvsr       runpod-diffusers-wan-2_2-14b-t2v-flashvsr-upscale
replace wan-with-upscale-spandrel       runpod-diffusers-wan-2_2-14b-t2v-spandrel-upscale
replace wan-with-upscale-seedvr2        runpod-diffusers-wan-2_2-14b-t2v-seedvr2-upscale
replace upscale-flashvsr-x4-torch26     runpod-diffusers-flashvsr-x4-torch26-upscale
replace upscale-flashvsr-x4             runpod-diffusers-flashvsr-x4-upscale
replace upscale-flashvsr-1080p          runpod-diffusers-flashvsr-1080p-upscale
replace upscale-spandrel-x2             runpod-diffusers-spandrel-x2-upscale
replace interpolate-rife-60fps          runpod-diffusers-rife-60fps-interpolate
replace upscale-seedvr2-3b              runpod-diffusers-seedvr2-3b-upscale
```

- [ ] **Step 3: Verify gate**

```bash
cd /workspace
pixi run pytest tests/test_examples.py tests/core/test_config.py tests/core/test_config_upscale.py -q
for n in upscale-flashvsr-x4-torch26 upscale-flashvsr-x4 upscale-flashvsr-1080p upscale-spandrel-x2 interpolate-rife-60fps upscale-seedvr2-3b wan-with-upscale-flashvsr-1080p wan-with-upscale-flashvsr wan-with-upscale-spandrel wan-with-upscale-seedvr2; do
  echo "STALE $n.yaml:"; rg -F "$n.yaml" -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**'
done
pixi run test >/tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t3.txt 2>&1; tail -3 /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t3.txt
```
Expected: pytest PASS; each `STALE …` empty; `pixi run test` PASS.

- [ ] **Step 4: Commit**

```bash
cd /workspace
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(configs): rename upscale/interpolate/two-stage configs to uniform scheme"
```

---

## Task 4: Bedrock + fal + keyframe configs

**Goal:** Rename the 6 hosted-provider generation configs.

**Files:**
- `examples/configs/luma-ray.yaml` → `examples/configs/bedrock-luma-ray-t2v.yaml`
- `examples/configs/nova-reel.yaml` → `examples/configs/bedrock-nova-reel-t2v.yaml`
- `examples/configs/fal.yaml` → `examples/configs/fal-t2v.yaml`
- `examples/configs/keyframe-fal-i2v.yaml` → `examples/configs/fal-keyframe-i2v.yaml`
- `examples/configs/keyframe-fal-flf2v.yaml` → `examples/configs/fal-keyframe-flf2v.yaml`
- `examples/configs/keyframe-luma.yaml` → `examples/configs/fal-luma-keyframe-i2v.yaml`
- Reference sites (swept): `tests/test_examples.py`, `tests/live/test_luma_ray_live.py`, `tests/live/test_nova_reel_live.py`, `tests/live/test_fal_live.py`, `tests/engines/test_bedrock_video.py`, `tests/test_layer_r_backcompat.py`, `tests/pipeline/test_keyframe_stage.py`, living docs.

**Acceptance Criteria:**
- [ ] All 6 renames land as `git mv`.
- [ ] `test_examples.py` bare refs (`"fal.yaml"`, `keyframe-fal-i2v.yaml`, `keyframe-fal-flf2v.yaml`, `luma-ray.yaml`) updated — including the `keyframe-*` membership asserts at lines ~601-612.
- [ ] Each old basename → zero non-frozen hits.
- [ ] Verify gate passes.

**Verify:** `pixi run pytest tests/test_examples.py tests/engines/test_bedrock_video.py tests/test_layer_r_backcompat.py -q` → PASS.

**Steps:**

- [ ] **Step 1: git mv**

```bash
cd /workspace/examples/configs
git mv luma-ray.yaml  bedrock-luma-ray-t2v.yaml
git mv nova-reel.yaml bedrock-nova-reel-t2v.yaml
git mv keyframe-fal-i2v.yaml   fal-keyframe-i2v.yaml
git mv keyframe-fal-flf2v.yaml fal-keyframe-flf2v.yaml
git mv keyframe-luma.yaml      fal-luma-keyframe-i2v.yaml
git mv fal.yaml fal-t2v.yaml
```

- [ ] **Step 2: Sweep references** (`fal.yaml` is NOT a suffix of any other config name — plain `-F` safe; run the two `keyframe-*` and `keyframe-luma` before nothing else depends on order)

```bash
cd /workspace
EXCL="-g !.pixi/** -g !.git/** -g !output/** -g !.kinoforge/** -g !docs/superpowers/plans/** -g !docs/superpowers/specs/** -g !tests/live/evidence/**"
replace() { rg -l -F "$1.yaml" $EXCL | xargs -r sed -i "s|$1\.yaml|$2.yaml|g"; }
replace luma-ray  bedrock-luma-ray-t2v
replace nova-reel bedrock-nova-reel-t2v
replace keyframe-fal-i2v   fal-keyframe-i2v
replace keyframe-fal-flf2v fal-keyframe-flf2v
replace keyframe-luma      fal-luma-keyframe-i2v
replace fal        fal-t2v
```
`replace fal fal-t2v` matches `fal.yaml` only (fixed string incl. `.yaml`); it cannot touch `fal-keyframe-*.yaml` (already renamed) or `fal-t2v.yaml`.

- [ ] **Step 3: Verify gate**

```bash
cd /workspace
pixi run pytest tests/test_examples.py tests/engines/test_bedrock_video.py tests/test_layer_r_backcompat.py -q
for n in luma-ray nova-reel keyframe-fal-i2v keyframe-fal-flf2v keyframe-luma; do
  echo "STALE $n.yaml:"; rg -F "$n.yaml" -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**'
done
# fal.yaml — expect zero bare refs (all now fal-t2v.yaml or fal-keyframe-*):
echo "STALE fal.yaml (bare):"; rg -n '(^|[^-a-z0-9_/])fal\.yaml' -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**'
pixi run test >/tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t4.txt 2>&1; tail -3 /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t4.txt
```
Expected: pytest PASS; all `STALE …` empty; `pixi run test` PASS.

- [ ] **Step 4: Commit**

```bash
cd /workspace
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(configs): rename bedrock/fal/keyframe configs to uniform scheme"
```

---

## Task 5: SkyPilot + tool/demo configs

**Goal:** Rename the 4 SkyPilot configs + `diffusers.yaml`; leave pure-tool configs untouched.

**Files:**
- `examples/configs/skypilot.yaml` → `examples/configs/skypilot-cpu.yaml`
- `examples/configs/skypilot-lambda.yaml` → `examples/configs/skypilot-lambda-comfyui.yaml`
- `examples/configs/skypilot-lambda-flashvsr.yaml` → `examples/configs/skypilot-lambda-diffusers-flashvsr-upscale.yaml`
- `examples/configs/skypilot-vast-flashvsr.yaml` → `examples/configs/skypilot-vast-diffusers-flashvsr-upscale.yaml`
- `examples/configs/diffusers.yaml` → `examples/configs/runpod-diffusers-serverless.yaml`
- Unchanged (assert, do NOT move): `skypilot-gpu.yaml`, `hosted.yaml`, `local-fake.yaml`, `cost.yaml`, `sweeper.yaml`
- Reference sites (swept): `tests/test_examples.py`, `tests/core/test_config.py` (skypilot-vast/lambda-flashvsr refs), `tests/providers/test_skypilot.py`, `tests/live/test_skypilot_live.py`, `src/kinoforge/providers/skypilot/__init__.py`, living docs.

**Acceptance Criteria:**
- [ ] 5 renames land as `git mv`.
- [ ] `test_examples.py` `EXAMPLE_CONFIGS` list + `test_skypilot_example_parses` / `skypilot-lambda` functions updated to new names.
- [ ] `skypilot-gpu.yaml` NOT renamed (still present, still referenced).
- [ ] `skypilot.yaml` sweep does not corrupt `skypilot-gpu`/`-lambda`/`-cpu` (guard below).
- [ ] Each old basename → zero non-frozen hits.
- [ ] Verify gate passes.

**Verify:** `pixi run pytest tests/test_examples.py tests/core/test_config.py tests/providers/test_skypilot.py -q` → PASS.

**Steps:**

- [ ] **Step 1: git mv (longest first; skypilot-lambda before skypilot)**

```bash
cd /workspace/examples/configs
git mv skypilot-lambda-flashvsr.yaml skypilot-lambda-diffusers-flashvsr-upscale.yaml
git mv skypilot-vast-flashvsr.yaml   skypilot-vast-diffusers-flashvsr-upscale.yaml
git mv skypilot-lambda.yaml          skypilot-lambda-comfyui.yaml
git mv skypilot.yaml                 skypilot-cpu.yaml
git mv diffusers.yaml                runpod-diffusers-serverless.yaml
```

- [ ] **Step 2: Sweep references — longest first; `skypilot.yaml` anchored**

`skypilot.yaml` is a suffix of none of the others (they have `-gpu`/`-lambda`/`-vast`/`-cpu` before `.yaml`), but `-F skypilot.yaml` would still be a substring inside `skypilot-...`? No — fixed string `skypilot.yaml` requires the literal `t.yaml` sequence, which `skypilot-gpu.yaml` does not contain. So plain `-F` is safe. `diffusers.yaml` likewise is not a suffix of `runpod-diffusers-...` names (those end differently). Both safe.

```bash
cd /workspace
EXCL="-g !.pixi/** -g !.git/** -g !output/** -g !.kinoforge/** -g !docs/superpowers/plans/** -g !docs/superpowers/specs/** -g !tests/live/evidence/**"
replace() { rg -l -F "$1.yaml" $EXCL | xargs -r sed -i "s|$1\.yaml|$2.yaml|g"; }
replace skypilot-lambda-flashvsr skypilot-lambda-diffusers-flashvsr-upscale
replace skypilot-vast-flashvsr   skypilot-vast-diffusers-flashvsr-upscale
replace skypilot-lambda          skypilot-lambda-comfyui
replace skypilot                 skypilot-cpu
replace diffusers                runpod-diffusers-serverless
```
Guard: `replace skypilot skypilot-cpu` runs AFTER the `-lambda`/`-flashvsr` renames, and its fixed pattern `skypilot.yaml` cannot match `skypilot-gpu.yaml` (no `t.yaml`) or the already-renamed `skypilot-cpu.yaml` / `skypilot-lambda-comfyui.yaml`. `replace diffusers runpod-diffusers-serverless` matches `diffusers.yaml` only.

- [ ] **Step 3: Confirm untouched configs still present**

```bash
cd /workspace/examples/configs
ls skypilot-gpu.yaml hosted.yaml local-fake.yaml cost.yaml sweeper.yaml
# Expected: all five listed, none renamed.
```

- [ ] **Step 4: Verify gate**

```bash
cd /workspace
pixi run pytest tests/test_examples.py tests/core/test_config.py tests/providers/test_skypilot.py -q
for n in skypilot skypilot-lambda skypilot-lambda-flashvsr skypilot-vast-flashvsr diffusers; do
  echo "STALE $n.yaml:"; rg -n -F "$n.yaml" -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**' | rg -v 'skypilot-gpu|skypilot-cpu|skypilot-lambda-comfyui|skypilot-lambda-diffusers|skypilot-vast-diffusers|runpod-diffusers-serverless'
done
pixi run test >/tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t5.txt 2>&1; tail -3 /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t5.txt
```
Expected: pytest PASS; each `STALE …` empty after the `rg -v` filter of new names; `pixi run test` PASS.

- [ ] **Step 5: Commit**

```bash
cd /workspace
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(configs): rename skypilot configs + diffusers demo; leave pure-tool configs"
```

---

## Task 6: Grid cells + grid sweep specs

**Goal:** Rename the 5 grid base cells (full prefix) and the 9 `.grid.yaml` sweep specs (version-normalize), updating the sweep-spec `config:` cell paths and the `_grid_examples/*.json` `spec:` paths.

**Files:**
- Grid cells (full prefix):
  - `examples/configs/grids/wan21-14b-base-no-loras.yaml` → `.../grids/runpod-comfyui-wan-2_1-14b-base-no-loras.yaml`
  - `examples/configs/grids/wan21-1_3b-base-no-loras.yaml` → `.../grids/runpod-diffusers-wan-2_1-1_3b-base-no-loras.yaml`
  - `examples/configs/grids/wan21-1_3b-base.yaml` → `.../grids/runpod-diffusers-wan-2_1-1_3b-base.yaml`
  - `examples/configs/grids/wan21-5b-base-no-loras.yaml` → `.../grids/runpod-comfyui-wan-2_1-5b-base-no-loras.yaml`
  - `examples/configs/grids/wan22-14b-base.yaml` → `.../grids/runpod-diffusers-wan-2_2-14b-base.yaml`
- Grid sweep specs (version-normalize only):
  - `wan21-1_3b-loras-swap.grid.yaml` → `wan-2_1-1_3b-loras-swap.grid.yaml`
  - `wan21-1_3b-prompt-sweep.grid.yaml` → `wan-2_1-1_3b-prompt-sweep.grid.yaml`
  - `wan21-1_3b-strength-sweep.grid.yaml` → `wan-2_1-1_3b-strength-sweep.grid.yaml`
  - `wan21-mixed-path-plus-generate.grid.yaml` → `wan-2_1-mixed-path-plus-generate.grid.yaml`
  - `wan21-model-sweep.grid.yaml` → `wan-2_1-model-sweep.grid.yaml`
  - `wan22-14b-loras-swap.grid.yaml` → `wan-2_2-14b-loras-swap.grid.yaml`
  - `wan22-14b-mixed-path-plus-generate.grid.yaml` → `wan-2_2-14b-mixed-path-plus-generate.grid.yaml`
  - `wan22-14b-prompt-sweep.grid.yaml` → `wan-2_2-14b-prompt-sweep.grid.yaml`
  - `wan22-14b-strength-sweep.grid.yaml` → `wan-2_2-14b-strength-sweep.grid.yaml`
- Reference sites (swept): the sweep specs' own `config:` lines (point at cells), `tests/live/_grid_examples/*.json` (`spec:` → sweep specs), `tests/core/test_grid_spec.py`, `tests/core/test_grid_errors.py`, `tests/_smoke_harness/lora_swap_grid.py`, `docs/batch-and-grid.md`, `examples/grids/illustrative-strength-sweep.yaml` (if it references any of these), living docs.

**Acceptance Criteria:**
- [ ] 5 cells + 9 sweep specs renamed as `git mv` (14 total).
- [ ] Every sweep spec's `config:` line points at the NEW cell path (cells swept BEFORE sweep-spec files are renamed, so the edit lands in the still-old-named spec, then the spec is renamed).
- [ ] `_grid_examples/*.json` `spec:` fields point at NEW sweep-spec paths.
- [ ] Old cell + sweep-spec basenames → zero non-frozen hits.
- [ ] Verify gate passes.

**Verify:** `pixi run pytest tests/core/test_grid_spec.py tests/core/test_grid_errors.py -q` → PASS.

**Steps:**

- [ ] **Step 1: git mv the 5 cells**

```bash
cd /workspace/examples/configs/grids
git mv wan21-14b-base-no-loras.yaml   runpod-comfyui-wan-2_1-14b-base-no-loras.yaml
git mv wan21-1_3b-base-no-loras.yaml  runpod-diffusers-wan-2_1-1_3b-base-no-loras.yaml
git mv wan21-1_3b-base.yaml           runpod-diffusers-wan-2_1-1_3b-base.yaml
git mv wan21-5b-base-no-loras.yaml    runpod-comfyui-wan-2_1-5b-base-no-loras.yaml
git mv wan22-14b-base.yaml            runpod-diffusers-wan-2_2-14b-base.yaml
```

- [ ] **Step 2: Sweep cell references (updates `config:` lines inside the still-old-named sweep specs + any test refs). Longest first.**

```bash
cd /workspace
EXCL="-g !.pixi/** -g !.git/** -g !output/** -g !.kinoforge/** -g !docs/superpowers/plans/** -g !docs/superpowers/specs/** -g !tests/live/evidence/**"
replace() { rg -l -F "$1.yaml" $EXCL | xargs -r sed -i "s|$1\.yaml|$2.yaml|g"; }
replace wan21-14b-base-no-loras   runpod-comfyui-wan-2_1-14b-base-no-loras
replace wan21-1_3b-base-no-loras  runpod-diffusers-wan-2_1-1_3b-base-no-loras
replace wan21-5b-base-no-loras    runpod-comfyui-wan-2_1-5b-base-no-loras
replace wan21-1_3b-base           runpod-diffusers-wan-2_1-1_3b-base
replace wan22-14b-base            runpod-diffusers-wan-2_2-14b-base
```
Order: the three `*-base-no-loras` (and `5b`) run before the shorter `wan21-1_3b-base` / `wan22-14b-base`; the `.yaml` boundary already prevents `wan21-1_3b-base` from matching inside `wan21-1_3b-base-no-loras.yaml`, but longest-first is belt-and-suspenders.

- [ ] **Step 3: git mv the 9 sweep specs**

```bash
cd /workspace/examples/configs/grids
git mv wan21-1_3b-loras-swap.grid.yaml            wan-2_1-1_3b-loras-swap.grid.yaml
git mv wan21-1_3b-prompt-sweep.grid.yaml          wan-2_1-1_3b-prompt-sweep.grid.yaml
git mv wan21-1_3b-strength-sweep.grid.yaml        wan-2_1-1_3b-strength-sweep.grid.yaml
git mv wan21-mixed-path-plus-generate.grid.yaml   wan-2_1-mixed-path-plus-generate.grid.yaml
git mv wan21-model-sweep.grid.yaml                wan-2_1-model-sweep.grid.yaml
git mv wan22-14b-loras-swap.grid.yaml             wan-2_2-14b-loras-swap.grid.yaml
git mv wan22-14b-mixed-path-plus-generate.grid.yaml wan-2_2-14b-mixed-path-plus-generate.grid.yaml
git mv wan22-14b-prompt-sweep.grid.yaml           wan-2_2-14b-prompt-sweep.grid.yaml
git mv wan22-14b-strength-sweep.grid.yaml         wan-2_2-14b-strength-sweep.grid.yaml
```

- [ ] **Step 4: Sweep sweep-spec references (updates `_grid_examples/*.json` `spec:` + test refs). Match on the `.grid.yaml` basename (unique).**

```bash
cd /workspace
EXCL="-g !.pixi/** -g !.git/** -g !output/** -g !.kinoforge/** -g !docs/superpowers/plans/** -g !docs/superpowers/specs/** -g !tests/live/evidence/**"
greplace() { rg -l -F "$1.grid.yaml" $EXCL | xargs -r sed -i "s|$1\.grid\.yaml|$2.grid.yaml|g"; }
greplace wan21-1_3b-loras-swap            wan-2_1-1_3b-loras-swap
greplace wan21-1_3b-prompt-sweep          wan-2_1-1_3b-prompt-sweep
greplace wan21-1_3b-strength-sweep        wan-2_1-1_3b-strength-sweep
greplace wan21-mixed-path-plus-generate   wan-2_1-mixed-path-plus-generate
greplace wan21-model-sweep                wan-2_1-model-sweep
greplace wan22-14b-loras-swap             wan-2_2-14b-loras-swap
greplace wan22-14b-mixed-path-plus-generate wan-2_2-14b-mixed-path-plus-generate
greplace wan22-14b-prompt-sweep           wan-2_2-14b-prompt-sweep
greplace wan22-14b-strength-sweep         wan-2_2-14b-strength-sweep
```

- [ ] **Step 5: Verify gate**

```bash
cd /workspace
pixi run pytest tests/core/test_grid_spec.py tests/core/test_grid_errors.py -q
for n in wan21-14b-base-no-loras wan21-1_3b-base-no-loras wan21-1_3b-base wan21-5b-base-no-loras wan22-14b-base; do
  echo "STALE $n.yaml:"; rg -F "$n.yaml" -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**'
done
for n in wan21-1_3b-loras-swap wan21-1_3b-prompt-sweep wan21-1_3b-strength-sweep wan21-mixed-path-plus-generate wan21-model-sweep wan22-14b-loras-swap wan22-14b-mixed-path-plus-generate wan22-14b-prompt-sweep wan22-14b-strength-sweep; do
  echo "STALE $n.grid.yaml:"; rg -F "$n.grid.yaml" -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**'
done
pixi run test >/tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t6.txt 2>&1; tail -3 /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t6.txt
```
Expected: pytest PASS; every `STALE …` empty; `pixi run test` PASS.

- [ ] **Step 6: Commit**

```bash
cd /workspace
pixi run pre-commit run --all-files
git add -A
git commit -m "refactor(configs): rename grid cells (full prefix) + sweep specs (version-normalize)"
```

---

## Task 7: Manifest rename + final global verification

**Goal:** Rename the one model-bearing manifest and prove the whole refactor is clean: zero stale basenames anywhere outside frozen archives, full suite green, pre-commit green.

**Files:**
- `examples/configs/manifests/runpod-comfyui-wan-manifest.yaml` → `examples/configs/manifests/runpod-comfyui-wan-2_1-14b-i2v-manifest.yaml`
- Unchanged: `manifests/batch-prompts.yaml`
- Reference sites (swept): `tests/test_examples.py` (`test_manifest_loads` at ~line 459-464), living docs.
- Final sweep may touch: `README.md`, `PROGRESS.md`, `successful-generations.md`, `docs/engines.md`, `docs/warm-reuse.md`, `docs/configuration.md`, `docs/batch-and-grid.md`, `docs/CLOUD-CREDS.md`, `docs/RELEASE-CHECKLIST.md`, `SPEC.md`, `DESIGN.md` — for any prose reference to a renamed config that the path-form sed missed.

**Acceptance Criteria:**
- [ ] Manifest renamed as `git mv`; old basename → zero non-frozen hits.
- [ ] A single global grep proves NO old basename (all 50) survives outside frozen archives.
- [ ] `pixi run test` full suite PASS (equal or better than Task 0 baseline — no new failures).
- [ ] `pixi run pytest tests/test_examples.py -q` PASS (all hardcoded lists resolve).
- [ ] `pixi run pre-commit run --all-files` clean.
- [ ] `tests/test_examples.py` docstring/heading "All 4 example configs" count corrected if it still says 4 (cosmetic — the list has 9).

**Verify:** `pixi run test` → PASS; global stale-grep (Step 3) → empty.

**Steps:**

- [ ] **Step 1: git mv the manifest + sweep**

```bash
cd /workspace/examples/configs/manifests
git mv runpod-comfyui-wan-manifest.yaml runpod-comfyui-wan-2_1-14b-i2v-manifest.yaml
cd /workspace
EXCL="-g !.pixi/** -g !.git/** -g !output/** -g !.kinoforge/** -g !docs/superpowers/plans/** -g !docs/superpowers/specs/** -g !tests/live/evidence/**"
rg -l -F 'runpod-comfyui-wan-manifest.yaml' $EXCL | xargs -r sed -i 's|runpod-comfyui-wan-manifest\.yaml|runpod-comfyui-wan-2_1-14b-i2v-manifest.yaml|g'
```

- [ ] **Step 2: Global stale-basename sweep (all 50 old names)**

Write the old-name list and grep each. Any hit outside frozen archives is a miss to fix by hand.

```bash
cd /workspace
cat > /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/oldnames.txt <<'EOF'
runpod-comfyui-wan-t2v-14b-2_2.yaml
runpod-comfyui-wan-t2v-1_3b.yaml
runpod-comfyui-wan-t2v-5b.yaml
runpod-comfyui-wan-t2v.yaml
runpod-comfyui-wan.yaml
runpod-diffusers-wan-t2v-14b-2_2.yaml
wan21-1_3b-lora-flexible-warm-reuse-smoke.yaml
wan21-1_3b-strength-grid.yaml
wan22-14b-lora-flexible-warm-reuse-release.yaml
wan22-14b-strength-grid.yaml
modal-wan-t2v-1_3b.yaml
modal-wan-t2v-14b-2_2.yaml
modal-flashvsr-x4.yaml
modal-rife-60fps.yaml
upscale-flashvsr-x4-torch26.yaml
upscale-flashvsr-x4.yaml
upscale-flashvsr-1080p.yaml
upscale-spandrel-x2.yaml
interpolate-rife-60fps.yaml
upscale-seedvr2-3b.yaml
wan-with-upscale-flashvsr-1080p.yaml
wan-with-upscale-flashvsr.yaml
wan-with-upscale-spandrel.yaml
wan-with-upscale-seedvr2.yaml
luma-ray.yaml
nova-reel.yaml
keyframe-fal-i2v.yaml
keyframe-fal-flf2v.yaml
keyframe-luma.yaml
skypilot-lambda-flashvsr.yaml
skypilot-vast-flashvsr.yaml
skypilot-lambda.yaml
diffusers.yaml
runpod-comfyui-wan-manifest.yaml
wan21-14b-base-no-loras.yaml
wan21-1_3b-base-no-loras.yaml
wan21-1_3b-base.yaml
wan21-5b-base-no-loras.yaml
wan22-14b-base.yaml
wan21-1_3b-loras-swap.grid.yaml
wan21-1_3b-prompt-sweep.grid.yaml
wan21-1_3b-strength-sweep.grid.yaml
wan21-mixed-path-plus-generate.grid.yaml
wan21-model-sweep.grid.yaml
wan22-14b-loras-swap.grid.yaml
wan22-14b-mixed-path-plus-generate.grid.yaml
wan22-14b-prompt-sweep.grid.yaml
wan22-14b-strength-sweep.grid.yaml
EOF
# fal.yaml, wan.yaml, skypilot.yaml handled separately (suffix-ambiguous) — check below.
while read -r name; do
  [ -z "$name" ] && continue
  hits=$(rg -c -F "$name" -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**' 2>/dev/null | wc -l)
  [ "$hits" -ne 0 ] && echo "MISS: $name ($hits files)"
done < /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/oldnames.txt
# suffix-ambiguous trio:
echo "-- ambiguous --"
rg -n '(^|[^-a-z0-9_/])wan\.yaml' -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**'
rg -n '(^|[^-a-z0-9_/])fal\.yaml' -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**'
rg -n '(^|[^-a-z0-9_/])skypilot\.yaml' -g '!.pixi/**' -g '!.git/**' -g '!docs/superpowers/plans/**' -g '!docs/superpowers/specs/**' -g '!tests/live/evidence/**'
```
Expected: NO `MISS:` lines; the three ambiguous greps print nothing. Any `MISS` → open the file, replace by hand (prose reference the path-form sed skipped), re-run.

- [ ] **Step 3: Full suite + example gate + pre-commit**

```bash
cd /workspace
pixi run pytest tests/test_examples.py -q
pixi run test >/tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t7.txt 2>&1; echo "exit=$?"; tail -5 /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t7.txt
diff <(grep -oE '[0-9]+ (passed|failed|error)' /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/baseline.txt) <(grep -oE '[0-9]+ (passed|failed|error)' /tmp/claude-1000/-workspace/103e8c31-e4fa-45c5-8348-c1c79bd57adc/scratchpad/t7.txt) || echo "counts differ — inspect"
pixi run pre-commit run --all-files
```
Expected: `test_examples.py` PASS; `pixi run test` exit 0 with pass-count ≥ baseline and zero new failures/errors; pre-commit clean. If pass-count DROPPED, a live test that previously collected now errors on a path — inspect `t7.txt`.

- [ ] **Step 4: Fix the stale "4 example configs" docstring (cosmetic)**

```bash
cd /workspace
rg -n 'All 4 example configs' tests/test_examples.py
# If present, edit the two occurrences (module docstring + AC1 comment) to "9".
```

- [ ] **Step 5: Commit**

```bash
cd /workspace
git add -A
git commit -m "refactor(configs): rename model manifest + final scheme verification sweep"
```

---

## Self-review notes (author)

- **Spec coverage:** every rename-map row in the spec maps to a task — RunPod gen (T1), Modal (T2), upscale/interp/two-stage (T3), bedrock/fal/keyframe (T4), skypilot+tool-demo (T5), grids cells+specs (T6), manifest+final (T7). Delete-DEAD → T1 Step 1. Reference-update policy → per-task sweeps + T7 global gate. Frozen-archive exclusion → shared conventions, applied in every `rg`/sed.
- **No placeholders:** every step carries exact `git mv`, `rg`, `sed`, and pytest commands with expected output.
- **Type/name consistency:** the `replace()`/`greplace()` helper signatures and the frozen-exclude glob set are identical across all tasks; every new name matches the spec table verbatim.
- **Live-test spend:** no task runs a live/smoke test; correctness of live-test paths rests on grep-zero + successful collection (stated in Shared conventions).
