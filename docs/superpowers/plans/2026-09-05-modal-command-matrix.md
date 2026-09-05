# Modal Command Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run every kinoforge subcommand and the important flag combinations against the Modal provider, record a PASS / FAIL / EXPECTED-REFUSAL verdict per cell in `docs/modal-command-matrix.md`, and fix the cheap failures, within a $20 budget.

**Architecture:** A live test campaign, not a code change. Four cost-ordered tiers (offline → Wan 2.1 1.3B on A10 → FlashVSR/RIFE → Wan 2.2 14B). One tracked results document is the deliverable; each cell writes its row and is committed before the next cell runs. Failures with a small, local cause are fixed red/green and the cell re-run once; everything else is filed.

**Tech Stack:** kinoforge CLI in the `live-modal` pixi env (`pixi run -e live-modal kinoforge …`), Modal serverless GPUs, `kinoforge.providers.modal.util.ModalUtilEndpoint` for utilisation polling, `kinoforge.core.frames.ffmpeg_frames_by_count` for visual QA.

**Spec:** `docs/superpowers/specs/2026-09-05-modal-command-matrix-design.md`

## Global Constraints

- **Budget:** $20 hard. Stop launching new cells once cumulative recorded spend reaches $18. Task 8 (Wan 2.2 14B) is capped at $4 and never retried.
- **Invocation:** every kinoforge command runs as `pixi run -e live-modal kinoforge …`. The default env lacks the `modal` binary (destroy silently skips the orphan probe there — PROGRESS EM2 gotcha).
- **The bare `modal` CLI needs the env loaded.** Use `pixi run -e live-modal python -c "from kinoforge.core.dotenv_loader import load_env_file; load_env_file(); import subprocess; subprocess.run(['modal','app','list'])"` — never source `.env` in a shell (the secret-read hook blocks it, and it would put the token in the transcript).
- **Preflight:** `pixi run preflight` must exit 0 before the first live spend of every task.
- **Prompt:** every generate uses `examples/configs/prompts/field-realistic.txt` verbatim (`--prompt "$(cat examples/configs/prompts/field-realistic.txt)"`).
- **Utilisation polling:** during every live generation poll every 60–90 s. Resolve the pod URL from `kinoforge status --id <id>` (it prints the endpoints), then `curl -s <url>/util`. GPU 0% for three consecutive polls while a generation is in flight → capture logs, `destroy`, mark FAIL.
- **Visual QA:** every output mp4 gets 5 frames via `ffmpeg_frames_by_count`, tiled into one montage, read, and a one-line verdict recorded. No cell is PASS without it.
- **Teardown proof:** after every teardown run `pixi run -e live-modal kinoforge list` and the Modal CLI app list (command above). PASS requires `[instance overview] No running instances.` + `No instances recorded in ledger.` and no running `kinoforge-*` app.
- **Results row format** (one row per cell in `docs/modal-command-matrix.md`):
  `| <cell id> | <command + flags> | <config> | PASS / FAIL / EXPECTED-REFUSAL / SKIPPED | $<cost> | <evidence path or log excerpt> | <notes / follow-up> |`
- **Commit after every cell** (`docs: matrix <cell id> <verdict>`), and after every fix (`fix(...)` with the test).
- **Fix policy:** a failure is fixed in-session only if the cause is one function / one config key / one guard; fix under red/green TDD with the `test-design` skill, commit, re-run that cell once. Otherwise file a follow-up in `PROGRESS.md` and leave the cell FAIL.
- **Never** run `git checkout <sha>` in the working tree; never Write/Edit `.env`; never print a credential.
- **Ephemeral runs never appear in `successful-generations.md`.**

**User decisions (already made):**
- "Include Wan 2.2 14B, capped: run it once, last, hard stop ~$4, no retry."
- "Record every failure, then fix the cheap ones; retries capped at one per cell."
- "Plan for everything" — no known error list to prioritise from.

---

## Cell inventory

Tier 0 (offline): T0-01…T0-17. Tier 1 (A10): T1-01…T1-29. Tier 2 (A100/T4): T2-01…T2-07. Tier 3: T3-01. Each task below lists its cells with the exact command.

Operator-side scratch dir for files that must live outside the repo: `/home/claudeuser/kinoforge-matrix/` (grid spec, vault, batch manifest).

---

### Task 0: Results document skeleton

**Goal:** Create `docs/modal-command-matrix.md` with every cell pre-listed as `PENDING`, so a mid-campaign crash leaves a legible record of what was and was not run.

**Files:**
- Create: `docs/modal-command-matrix.md`

**Acceptance Criteria:**
- [ ] The document has a preamble (purpose, date, budget, link to the plan and spec), a running `Spend so far` line, and one table per tier.
- [ ] Every cell id from Tasks 1–8 appears exactly once with verdict `PENDING`.
- [ ] `pixi run pre-commit run --files docs/modal-command-matrix.md` passes.

**Verify:** `rg -c "PENDING" docs/modal-command-matrix.md` → 54

**Steps:**

- [ ] **Step 1: Write the skeleton.** Preamble + four tables using the row format from Global Constraints; copy the cell ids and commands from Tasks 1–8 below.
- [ ] **Step 2: Run pre-commit on the file, fix whitespace.**
- [ ] **Step 3: Commit.** `git add docs/modal-command-matrix.md && git commit -m "docs: add the Modal command matrix skeleton"`

---

### Task 1: Tier 0 — the offline surface ($0)

**Goal:** Run every command that spends nothing against all five Modal configs and an empty ledger, and record each verdict.

**Files:**
- Modify: `docs/modal-command-matrix.md`

**Acceptance Criteria:**
- [ ] Every T0 cell has a verdict and, for FAIL, the exact error text.
- [ ] Any validation regression from S1–S5 that surfaces here is either fixed (small) or filed before Tier 1 starts.

**Verify:** `rg -c "T0-.*PENDING" docs/modal-command-matrix.md` → 0

**Cells** (`CFGS` = the five `examples/configs/modal-*.yaml`; `FIX` = `output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4`):

| Cell | Command | Expected |
|------|---------|----------|
| T0-01 | `kinoforge --version` | prints version, exit 0 |
| T0-02 | `kinoforge doctor -c <cfg>` for each of CFGS | report, exit 0, no ERROR rows |
| T0-03 | `kinoforge deploy --config <cfg> --dry-run` for each of CFGS | plan printed, exit 0 (already green 2026-09-05 — re-record) |
| T0-04 | `kinoforge upscale -c modal-diffusers-flashvsr-x4-upscale.yaml --video FIX --dry-run` | exit 0 |
| T0-05 | `kinoforge upscale -c modal-diffusers-flashvsr-1080p-upscale.yaml --video FIX --dry-run` | exit 0, resolves `ScaleTarget(kind="height", value=1080)` |
| T0-06 | `kinoforge interpolate -c modal-diffusers-rife-60fps-interpolate.yaml --video FIX --fps 60 --dry-run` | exit 0 |
| T0-07 | `kinoforge generate -c modal-diffusers-wan-2_1-1_3b-t2v.yaml --mode t2v --prompt "$(cat examples/configs/prompts/field-realistic.txt)" --dry-run-swap` | swap plan printed, no deploy, exit 0 |
| T0-08 | `kinoforge batch -c modal-diffusers-wan-2_1-1_3b-t2v.yaml --manifest /home/claudeuser/kinoforge-matrix/batch.yaml --dry-run-swap` | exit 0 |
| T0-09 | `kinoforge grid --spec /home/claudeuser/kinoforge-matrix/grid.yaml --out /home/claudeuser/kinoforge-matrix/grid-dry.mp4 --dry-run` | exit 0, two cells planned |
| T0-10 | `kinoforge list` | both "no instances" lines |
| T0-11 | `kinoforge status --id does-not-exist` | clean not-found, non-zero exit, no traceback |
| T0-12 | `kinoforge reap` / `kinoforge reap --format json` | empty classification, exit 0 |
| T0-13 | `kinoforge cost` / `kinoforge cost --json` / `kinoforge cost -c modal-…-1_3b-t2v.yaml` | dashboard, Modal row present or documented absent |
| T0-14 | `kinoforge gc --config modal-…-1_3b-t2v.yaml` | exit 0 on empty store |
| T0-15 | `kinoforge sweeper status` / `kinoforge sweeper metrics` | "not running", exit code documented |
| T0-16 | `kinoforge forget --id does-not-exist` | clean not-found |
| T0-17 | bare `modal app list` without env → "Token missing"; with env loaded (Global Constraints command) → app table | both recorded as EXPECTED |

**Steps:**

- [ ] **Step 1: Create the operator-side files.**

`/home/claudeuser/kinoforge-matrix/batch.yaml`:
```yaml
- prompt_file: /workspace/examples/configs/prompts/field-realistic.txt
  mode: t2v
  run_id: matrix-a
- prompt_file: /workspace/examples/configs/prompts/field-realistic.txt
  mode: t2v
  run_id: matrix-b
```

`/home/claudeuser/kinoforge-matrix/grid.yaml`:
```yaml
title: "Modal matrix — Wan 2.1 1.3B 1x2"
layout: "1x2"
budget_cap_usd: 0.60
cells:
  - generate:
      config: /workspace/examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml
    caption: "cell A"
  - generate:
      config: /workspace/examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml
      overrides:
        spec.num_frames: 17
    caption: "cell B 17f"
```

`/home/claudeuser/kinoforge-matrix/vault.yaml`:
```yaml
positive_prompt: |
  <contents of examples/configs/prompts/field-realistic.txt, verbatim>
```

- [ ] **Step 2: Run T0-01 … T0-17 in order, capturing stdout+stderr to `/home/claudeuser/kinoforge-matrix/logs/T0-NN.log`.**
- [ ] **Step 3: Record every row; for FAILs apply the fix policy.**
- [ ] **Step 4: Commit.** `git add docs/modal-command-matrix.md && git commit -m "docs: matrix tier 0 recorded"`

---

### Task 2: Tier 1a — one warm Wan 2.1 1.3B pod through every stateful command (~$0.30)

**Goal:** Cold-boot one A10 pod with default warm-reuse and drive every read, attach, batch and teardown command against it before destroying it.

**Files:**
- Modify: `docs/modal-command-matrix.md`, `successful-generations.md` (See-also under §22; new section for batch-on-Modal if T1-13 passes)

**Acceptance Criteria:**
- [ ] T1-01 produces an mp4 with frame-QA PASS and the pod survives (warm-reuse default).
- [ ] Every T1-02 … T1-14 cell has a verdict recorded against the live pod.
- [ ] Teardown proof (ledger empty + no running app) recorded after T1-16.

**Verify:** `rg -c "T1-(0[1-9]|1[0-6]).*PENDING" docs/modal-command-matrix.md` → 0

**Cells** (`CFG` = `examples/configs/modal-diffusers-wan-2_1-1_3b-t2v.yaml`, `PROMPT` = `"$(cat examples/configs/prompts/field-realistic.txt)"`, `ID` = the instance id printed by T1-01):

| Cell | Command | Expected |
|------|---------|----------|
| T1-01 | `kinoforge generate -c CFG --mode t2v --prompt PROMPT` | cold boot, mp4, exit 0, pod stays; record instance id, deploy time, gen time |
| T1-02 | `kinoforge list` | the pod listed with provider=modal |
| T1-03 | `kinoforge status --id ID` | state + endpoints + util |
| T1-04 | `kinoforge logs --id ID` and `--file server.log --out /home/claudeuser/kinoforge-matrix/logs/T1-04-server.log` | bytes or a clean "unsupported on modal" — record which |
| T1-05 | `kinoforge cost` / `--json` / `--no-cache` | Modal burn rate visible |
| T1-06 | `kinoforge pod lora ls ID` | inventory (empty) or clean unsupported |
| T1-07 | `kinoforge reap` and `reap --format json --id ID` | classifies the pod LIVE, no destroy |
| T1-08 | `kinoforge sweeper status` with no daemon | "not running" |
| T1-09 | `kinoforge generate -c CFG --mode t2v --prompt PROMPT` (second process) | `warm-reuse: attached`, no deploy, mp4, frame-QA |
| T1-10 | same + `--instance-id ID` | explicit attach |
| T1-11 | same + `--force-attach --instance-id ID` | attach bypassing matcher |
| T1-12 | `kinoforge --vault /home/claudeuser/kinoforge-matrix/vault.yaml generate -c CFG --mode t2v` (no `--prompt`; if argparse still requires it, pass `--prompt ""` and record) | prompt taken from vault, mp4 |
| T1-13 | `kinoforge batch -c CFG --manifest /home/claudeuser/kinoforge-matrix/batch.yaml --concurrent 1` then `--stream-format jsonl` | two artifacts, batch_summary, one deploy |
| T1-14 | `kinoforge generate … --run-id matrix-runid --output-dir /home/claudeuser/kinoforge-matrix/out` and `--no-output-dir` | artifact placement honours flags |
| T1-15 | `kinoforge stop --id ID` | EXPECTED-REFUSAL: "modal cannot pause billing", clean message, pod still alive |
| T1-16 | `kinoforge destroy --id ID` then `kinoforge list` + Modal app list | teardown proof |
| T1-17 | `kinoforge forget --id ID` after destroy | clean "not in ledger" (already removed) |
| T1-18 | `kinoforge gc --config CFG` | store artifacts listed / collected, exit 0 |

**Steps:**

- [ ] **Step 1: `pixi run preflight` → PASS.**
- [ ] **Step 2: Run T1-01 in the background, polling `/util` every 60–90 s; log to `/home/claudeuser/kinoforge-matrix/logs/T1-01.log`.**
- [ ] **Step 3: Frame-QA the T1-01 mp4** (montage → read → verdict).
- [ ] **Step 4: Run T1-02 … T1-08 (reads) against the live pod, then T1-09 … T1-14 (generations, each frame-QA'd), then T1-15 … T1-18.**
- [ ] **Step 5: Record rows + spend; write the §22 See-also line and, if T1-13 passed, a new `successful-generations.md` section "batch on Modal".**
- [ ] **Step 6: Commit** `docs: matrix tier 1a recorded`.

---

### Task 3: Tier 1b — deploy-first lifecycle (~$0.10)

**Goal:** Prove the `deploy` → `provision` → `generate --instance-id` → `reap --apply` path on Modal.

**Files:**
- Modify: `docs/modal-command-matrix.md`, `successful-generations.md` (new section if PASS: deploy-first on Modal)

**Acceptance Criteria:**
- [ ] `deploy` returns an instance id and writes the pre-launch provisional row (visible in `list` during boot).
- [ ] `generate --instance-id` attaches without a second deploy and produces a frame-QA-PASS mp4.
- [ ] `reap --apply --id` destroys it; teardown proof recorded.

**Verify:** `rg -c "T1-(19|2[0-2]).*PENDING" docs/modal-command-matrix.md` → 0

| Cell | Command | Expected |
|------|---------|----------|
| T1-19 | `kinoforge deploy --config CFG` (also try `--diagnostic-mode` value listed in help) | instance id, endpoints, exit 0 |
| T1-20 | `kinoforge provision -c CFG` | re-provision of the existing instance or a clean "already provisioned"; record |
| T1-21 | `kinoforge generate -c CFG --mode t2v --prompt PROMPT --instance-id ID --skip-preflight` | attach, mp4 |
| T1-22 | `kinoforge reap --apply --id ID` (fallback `destroy --id ID`) then teardown proof | destroyed |

**Steps:** preflight → run cells in order with util polling during T1-21 → frame-QA → record + commit `docs: matrix tier 1b recorded`.

---

### Task 4: Tier 1c — ephemeral runs and the reapers (~$0.15)

**Goal:** Prove `--ephemeral generate` leaves no local record, that `sweeper start` reaps the idle ephemeral app, and that `reap --include-orphans --apply` is the alternative.

**Files:**
- Modify: `docs/modal-command-matrix.md` (no successful-generations entry — ephemeral)

**Acceptance Criteria:**
- [ ] After T1-23 the ledger is empty, the artifact exists locally only where `--ephemeral` allows, and `ephemeral-index.json` names the app.
- [ ] The sweeper (stall-tight window) reaches `STALL_REAP` → `destroyed_and_forgot`; app gone from Modal.
- [ ] Second ephemeral gen reaped by `reap --include-orphans --apply`; teardown proof.

**Verify:** `rg -c "T1-2[3-7].*PENDING" docs/modal-command-matrix.md` → 0

| Cell | Command | Expected |
|------|---------|----------|
| T1-23 | `kinoforge --ephemeral generate -c CFG --mode t2v --prompt PROMPT` | app `kinoforge-eph-<8hex>`, mp4, ledger empty |
| T1-24 | `kinoforge sweeper start -c CFG --interval-s 30` (background, ≤ 5 min) + `sweeper status` + `sweeper metrics` while it runs, then `sweeper stop` | LIVE → STALL_REAP → destroyed |
| T1-25 | `kinoforge --ephemeral generate …` again (second app) | mp4 |
| T1-26 | `kinoforge reap --include-orphans` then `--apply` | orphan destroyed, index row gone |
| T1-27 | `kinoforge --ephemeral --debug-show-secrets list` | EXPECTED-REFUSAL (forbidden under --ephemeral) |

**Steps:** preflight → cells in order → frame-QA both mp4s → teardown proof → record + commit `docs: matrix tier 1c recorded`.

---

### Task 5: Tier 1d — grid (~$0.15)

**Goal:** Prove `kinoforge grid` composes a 1x2 grid from two Modal generations sharing one warm pod, with the last cell auto-destroying.

**Files:**
- Modify: `docs/modal-command-matrix.md`, `successful-generations.md` (new section if PASS: grid on Modal)

**Acceptance Criteria:**
- [ ] Exit code 0 (`full`), composed mp4 with two captioned cells, frame-QA PASS.
- [ ] `budget_cap_usd` respected (exit 3 recorded if crossed).
- [ ] Teardown proof after the run.

**Verify:** `rg -c "T1-2[89].*PENDING" docs/modal-command-matrix.md` → 0

| Cell | Command | Expected |
|------|---------|----------|
| T1-28 | `kinoforge grid --spec /home/claudeuser/kinoforge-matrix/grid.yaml --out /home/claudeuser/kinoforge-matrix/grid.mp4 --max-parallel-groups 1` | exit 0, grid mp4 |
| T1-29 | `kinoforge grid … --ephemeral` with `--out …/grid-eph.mp4` | exit 0, no ledger residue |

**Steps:** preflight → T1-28 with util polling → frame-QA → teardown proof → T1-29 → proof → record + commit `docs: matrix tier 1d recorded`.

---

### Task 6: Tier 2a — FlashVSR upscale on A100-80GB (~$0.45)

**Goal:** Prove `upscale` x4 with warm-reuse, the `--scale 1080p` warm-attach on the same pod, and an ephemeral `--no-reuse` upscale.

**Files:**
- Modify: `docs/modal-command-matrix.md`, `successful-generations.md` (See-also under §24 and §27)

**Acceptance Criteria:**
- [ ] T2-01 output is 1920² and frame-QA PASS (no false colour — the 2026-07-03 failure mode).
- [ ] T2-02 attaches to the T2-01 pod without a second deploy and outputs 1080².
- [ ] Teardown proof after T2-04 and after T2-05.

**Verify:** `rg -c "T2-0[1-5].*PENDING" docs/modal-command-matrix.md` → 0

| Cell | Command | Expected |
|------|---------|----------|
| T2-01 | `kinoforge upscale -c examples/configs/modal-diffusers-flashvsr-x4-upscale.yaml --video FIX` | cold boot, 1920² mp4, pod stays |
| T2-02 | `kinoforge upscale -c …-flashvsr-x4-upscale.yaml --video FIX --scale 1080p --attach-pod ID` | warm attach, 1080² mp4 |
| T2-03 | `kinoforge upscale -c …-flashvsr-1080p-upscale.yaml --video FIX` (separate cfg, same capability key?) | attach or cold boot — record which |
| T2-04 | `kinoforge destroy --id ID` + proof | clean |
| T2-05 | `kinoforge --ephemeral upscale -c …-flashvsr-x4-upscale.yaml --video FIX --scale 1080p --no-reuse` + proof | 1080² mp4, app stopped, no residue |

**Steps:** preflight → T2-01 with util polling (boot ≤ 10 min; A100 idle at 0% for 3 polls after boot = dead) → ffprobe dims + frame-QA → T2-02/03 → T2-04 proof → T2-05 proof → record + commit `docs: matrix tier 2a recorded`.

---

### Task 7: Tier 2b — RIFE interpolate on T4 (~$0.10)

**Goal:** Prove `interpolate --fps 60 --no-reuse`, an alternate fps, and chaining on the T2-01 upscaled output.

**Files:**
- Modify: `docs/modal-command-matrix.md`, `successful-generations.md` (See-also under §25)

**Acceptance Criteria:**
- [ ] T2-06 output has 60 fps per `ffprobe_fps` and ~4× the input frame count; frame-QA PASS.
- [ ] T2-07 (input = T2-01's 1920² mp4, `--fps 32`) succeeds or fails with a recorded reason (VRAM on T4).
- [ ] Teardown proof after each (both `--no-reuse`).

**Verify:** `rg -c "T2-0[67].*PENDING" docs/modal-command-matrix.md` → 0

| Cell | Command | Expected |
|------|---------|----------|
| T2-06 | `kinoforge interpolate -c examples/configs/modal-diffusers-rife-60fps-interpolate.yaml --video FIX --fps 60 --no-reuse` | 60 fps mp4, app gone |
| T2-07 | same cfg, `--video <T2-01 output> --fps 32 --no-reuse` | mp4 or recorded VRAM failure |

**Steps:** preflight → cells with util polling → `ffprobe_fps` + frame-QA → proofs → record + commit `docs: matrix tier 2b recorded`.

---

### Task 8: Tier 3 — Wan 2.2 14B on A100-80GB, capped $4

**Goal:** One `generate --no-reuse` on the 14B config, killed at $4 of spend, never retried.

**Files:**
- Modify: `docs/modal-command-matrix.md`, `successful-generations.md` (See-also under §23 if PASS)

**Acceptance Criteria:**
- [ ] Cumulative matrix spend before launch ≤ $14 (else SKIPPED, recorded).
- [ ] The run is destroyed the moment wall-clock × $2.50/hr reaches $4 (96 min) or the util rule fires.
- [ ] Verdict recorded with boot time, gen time, and whether the HF Volume cache served the weights.

**Verify:** `rg -c "T3-01.*PENDING" docs/modal-command-matrix.md` → 0

| Cell | Command | Expected |
|------|---------|----------|
| T3-01 | `kinoforge generate -c examples/configs/modal-diffusers-wan-2_2-14b-t2v.yaml --mode t2v --prompt PROMPT --no-reuse` | 81f 480² mp4, frame-QA, app gone |

**Steps:** preflight → launch in background with a 90-minute watchdog that runs `destroy` → util polling (weight download phase: CPU > 0, memory rising; if flat for 3 polls, kill) → frame-QA → proof → record + commit `docs: matrix tier 3 recorded`.

---

### Task 9: Wrap-up

**Goal:** Turn the matrix into the operator's answer: a summary of what works, what does not, what was fixed, and what is filed.

**Files:**
- Modify: `docs/modal-command-matrix.md` (summary section at top), `PROGRESS.md` (RESUME SNAPSHOT entry + follow-ups), `README.md` (one line pointing at the matrix under project structure)

**Acceptance Criteria:**
- [ ] No `PENDING` left; every FAIL names a follow-up or the fix commit.
- [ ] Total spend line ≤ $18 and reconciled against `kinoforge cost` + Modal's dashboard figure the operator can check.
- [ ] `pixi run pre-commit run --all-files` green; final commit on `main`.

**Verify:** `rg -c "PENDING" docs/modal-command-matrix.md` → 0

**Steps:** write summary → update PROGRESS.md snapshot → README line → `pixi run pre-commit run --all-files` → commit `docs: close the Modal command matrix campaign`.
