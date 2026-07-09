# Modal Milestone 2 — Wan 2.2 T2V-A14B on 80GB — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a Wan 2.2 T2V-A14B video on a Modal 80GB GPU via the proven Milestone-1 Modal transport, frame-QA it, and log it — a new-config-only milestone.

**Architecture:** Add ONE config (`modal-wan-t2v-14b-2_2.yaml`) that merges the M1 Modal transport (py3.13-slim image, torch2.6 cu124 pip stack, `wan_t2v_server` on a Modal `web_server(8000)`, gzip-chunked Secret boot payload, embed_modules) with the RunPod A14B model+hardware target (Wan2.2-T2V-A14B-Diffusers, bf16, 80GB `gpu_preference [A100-80GB, H100]`, 81 frames). Zero `src/` change — the ModalProvider transport is reused verbatim. Tests: offline config characterization + a RED live-smoke scaffold committed before any spend.

**Tech Stack:** kinoforge ModalProvider, diffusers WanPipeline, Modal serverless GPU (A100-80GB), pixi `live-modal` env, pytest.

**User decisions (already made):** Run autonomously — skip pre-spend confirmations; live smokes pre-authorized within the $30 Modal credit (`feedback_autonomous_no_gates` + roadmap brief). Standard smoke prompt verbatim from `examples/configs/prompts/field-realistic.txt`. `--no-reuse` on the one-shot smoke.

---

### Task 0: Wan 2.2 A14B Modal config + offline characterization test

**Goal:** A validated `modal-wan-t2v-14b-2_2.yaml` that resolves to a ModalProvider selecting an 80GB card, proven green offline (no spend).

**Files:**
- Create: `examples/configs/modal-wan-t2v-14b-2_2.yaml`
- Modify: `tests/test_modal_config.py`

**Acceptance Criteria:**
- [ ] `load_config` on the cfg → `compute.provider == "modal"`, `compute.cloud is None`, `min_vram_gb == 80`.
- [ ] `build_provider_for(cfg)` returns a `ModalProvider`.
- [ ] Model ref contains `Wan2.2-T2V-A14B` (the `-Diffusers` variant).
- [ ] `modal_offers(cfg.compute.requirements)` returns an 80GB offer first, and its gpu_type is `A100-80GB`.
- [ ] `spec.model` non-empty and not `unknown` (filename-slug lockdown).

**Verify:** `pixi run -e live-modal python -m pytest tests/test_modal_config.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write the config.** Create `examples/configs/modal-wan-t2v-14b-2_2.yaml`:

```yaml
# Wan 2.2 T2V-A14B (dual-14B MoE) on Modal serverless GPU (Milestone 2 live proof).
# Delivery: Option-A generic Modal app — the diffusers wan_t2v_server runs on a
# Modal web_server(8000) via the same provision_script; exec run_cmd as RunPod.
# Reuses the Milestone-1 Modal transport verbatim (py3.13-slim image, torch2.6
# cu124 pip stack, gzip-chunked Secret boot payload); swaps model + 80GB target.
# NOTE: NO `cloud:` key — that field is SkyPilot-only and fails validation here.
#
# `prompt:` carries the standard video-gen smoke prompt (verbatim from
# examples/configs/prompts/field-realistic.txt) per feedback_standard_test_prompt.

mode: t2v
prompt: "Photorealistic, cinematic 5-second shot on anamorphic lenses with shallow depth of field and subtle lens flare. A slow push-in toward a young woman in a sweeping alpine meadow of wildflowers; behind her, a tall waterfall tumbles down moss-covered cliffs into a misting pool. Warm golden-hour light rakes across the field, backlighting her glowing silhouette and igniting floating pollen and mist that drift like tiny embers of light. Her simple but vividly colored dress ripples in the breeze, strands of hair lifting. Facing away, she turns to glance over her shoulder with a coy, gentle smile as the camera glides into an intimate close-up on her eyes. Around her, friendly magical creatures move with slow grace — luminous butterflies and glowing wisps trailing ribbons of soft light. The sky is clear and radiant, brushed with wisps of cloud. Filmic color grade, warm highlights, soft shadows, fine film grain, volumetric god rays cutting through the mist. Serene, ethereal, breathtaking."

engine:
  kind: diffusers
  precision: bf16
  diffusers:
    # Modal serialized web_server fn requires the image Python to match the
    # controller (live-modal env = 3.13). python:3.13-slim matches; the provision
    # pip-installs torch/diffusers (cu124 cp313 wheels bundle CUDA + libgomp;
    # imageio[ffmpeg] bundles ffmpeg). Same transport as Milestone 1.
    image: "python:3.13-slim"
    server_cmd: ["python", "-m", "kinoforge.engines.diffusers.servers.wan_t2v_server"]
    pip:
      - "torch==2.6.0"
      - "torchvision==0.21.0"
      - "torchaudio==2.6.0"
      - "diffusers>=0.32"
      - "transformers>=4.45"
      - "accelerate>=1.0"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
    base_url: "http://localhost:8000"
    prompt_body_key: "prompt"
    embed_modules: ["kinoforge.engines.diffusers.servers"]

models:
  # The `-Diffusers` variant (NOT bare Wan-AI/Wan2.2-T2V-A14B): sharded diffusers
  # layout with model_index.json + transformer/ + transformer_2/ (high/low-noise
  # MoE) that WanPipeline.from_pretrained loads. Bare repo 404s from_pretrained.
  - ref: "hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers"
    kind: base
    target: checkpoints  # informational; diffusers manages HF cache

compute:
  provider: modal
  image: "python:3.13-slim"
  mode: pod
  requirements:
    # MoE = two 14B transformers (~56GB bf16) + UMT5-XXL T5 + VAE + activations.
    # 48GB cards OOM (RunPod Task 8 attempts #17-19); 80GB fits comfortably.
    min_vram_gb: 80
    min_cuda: "12.4"
    max_usd_per_hr: 4.00
    gpu_preference:
      - "A100-80GB"
      - "H100"
    disk_gb: 150
  lifecycle:
    # ~63GB HF snapshot download dominates cold boot (~25-30m observed on RunPod).
    idle_timeout: 60m
    job_timeout: 15m
    time_buffer: 3m
    max_lifetime: 90m
    boot_timeout: 45m
    budget: 4.0

spec:
  model: "Wan2.2-T2V-A14B-Diffusers"
  pipeline: "WanPipeline"
  scheduler: "UniPCMultistepScheduler"
  width: 480
  height: 480
  num_frames: 81
  fps: 16
```

- [ ] **Step 2: Write the failing tests.** Append to `tests/test_modal_config.py`:

```python
from kinoforge.providers.modal._catalog import modal_offers

CFG_A14B = Path("examples/configs/modal-wan-t2v-14b-2_2.yaml")


def test_a14b_config_resolves_to_modal_provider():
    cfg = load_config(CFG_A14B)
    assert cfg.compute is not None
    assert cfg.compute.provider == "modal"
    assert cfg.compute.cloud is None  # non-sky
    assert isinstance(build_provider_for(cfg), ModalProvider)


def test_a14b_config_targets_80gb_wan22():
    cfg = load_config(CFG_A14B)
    assert cfg.compute is not None
    assert cfg.compute.requirements.min_vram_gb == 80
    assert any("Wan2.2-T2V-A14B" in m.ref for m in cfg.models)
    assert cfg.spec is not None
    assert cfg.spec.model and cfg.spec.model.lower() != "unknown"


def test_a14b_config_selects_80gb_offer_first():
    cfg = load_config(CFG_A14B)
    assert cfg.compute is not None
    offers = modal_offers(cfg.compute.requirements)
    assert offers, "expected at least one 80GB offer"
    assert offers[0].vram_gb >= 80
    assert offers[0].gpu_type == "A100-80GB"  # cheapest 80GB, first in preference
```

- [ ] **Step 3: Run — confirm failure.** Run: `pixi run -e live-modal python -m pytest tests/test_modal_config.py -v`
  Expected: the three new tests FAIL (config file not found / not yet created) before Step 1 is saved; after Step 1 they should pass — if any fails on a value, fix the cfg to match the assertion.

- [ ] **Step 4: Run — confirm pass.** Same command → all pass. Also run `pixi run -e live-modal python -c "from kinoforge.core.config import load_config; load_config('examples/configs/modal-wan-t2v-14b-2_2.yaml'); print('cfg OK')"` to confirm full validation (spec/lifecycle) passes.

- [ ] **Step 5: Commit.**

```bash
git add examples/configs/modal-wan-t2v-14b-2_2.yaml tests/test_modal_config.py
git commit -m "feat(config): Modal Wan 2.2 T2V-A14B (80GB) config + offline characterization"
```

---

### Task 1: RED live-smoke scaffold (committed BEFORE any spend)

**Goal:** A committed, failing/xfail live-smoke test that names the exact generate invocation for Milestone 2 — satisfies the durability rule that a spend-driving scaffold exists in git before the spend.

**Files:**
- Create: `tests/live/test_modal_wan_t2v_14b_2_2.py`

**Acceptance Criteria:**
- [ ] Test module exists, references the M2 config path and mode `t2v`.
- [ ] Marked so it does NOT run in the default offline suite (xfail/skip-unless-live, mirroring `tests/live/test_modal_wan_t2v_1_3b.py`).
- [ ] Committed before any `kinoforge generate` against Modal.

**Verify:** `pixi run -e live-modal python -m pytest tests/live/test_modal_wan_t2v_14b_2_2.py -v` → collected, RED/xfail (not a live spend).

**Steps:**

- [ ] **Step 1: Read the M1 live scaffold** `tests/live/test_modal_wan_t2v_1_3b.py` to mirror its markers/structure (skip-unless-live gating, config path constant, expected-artifact assertion shape).

- [ ] **Step 2: Write** `tests/live/test_modal_wan_t2v_14b_2_2.py` mirroring the M1 scaffold, swapping:
  - config path → `examples/configs/modal-wan-t2v-14b-2_2.yaml`
  - expected model slug → `Wan2.2-T2V-A14B`
  - keep the same live-gating marker and the same "artifact exists + non-empty mp4" assertion the M1 scaffold uses.
  (Repeat the M1 file's marker/gating idiom exactly — do not invent a new gating mechanism.)

- [ ] **Step 3: Run — confirm RED/xfail, not a live spend.** Run: `pixi run -e live-modal python -m pytest tests/live/test_modal_wan_t2v_14b_2_2.py -v`
  Expected: collected and RED or xfail per the M1 gating (no pod deployed).

- [ ] **Step 4: Commit (BEFORE spend).**

```bash
git add tests/live/test_modal_wan_t2v_14b_2_2.py
git commit -m "test(live): RED scaffold for Modal Wan 2.2 A14B (80GB) Milestone 2 proof"
```

---

### Task 2: Live smoke — generate, frame-QA, verify teardown, log (USER-ORDERED GATE)

**Goal:** Run the real Wan 2.2 A14B generation on a Modal 80GB GPU, frame-QA the output, verify teardown, and log it to `successful-generations.md` §23.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `successful-generations.md` (new §23)
- Modify: `PROGRESS.md` (RESUME SNAPSHOT + SINGLE NEXT ACTION → Milestone 3)

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 (clean tree, creds, zero active pods) before spend.
- [ ] A real Modal 80GB container (A100-80GB or H100) allocated — confirmed via orchestrator log / `modal app list`. If no 80GB container allocates within ~2 min, ABORT (free-tier probe fails cheap).
- [ ] `kinoforge generate --config examples/configs/modal-wan-t2v-14b-2_2.yaml --mode t2v --prompt "$(cat examples/configs/prompts/field-realistic.txt)" --no-reuse` produces a non-empty mp4; ffprobe dims 480×480, ~81 frames.
- [ ] Frames extracted (`ffmpeg_frames_by_count`, ~5) and eyeballed: no corruption/false-color, coherent, prompt-adherent. ⚠️-flag if not clearly high quality.
- [ ] Teardown verified AFTER orchestrator exit: `pixi run kinoforge list` → no running instances + empty ledger AND `pixi run -e live-modal modal app list` shows the app stopped.
- [ ] `successful-generations.md` §23 written per schema (new axis: A14B on Modal 80GB). PROGRESS updated.

**Verify:** `pixi run kinoforge list` post-run → `No running instances.` + `No instances recorded in ledger.`; §23 present in `successful-generations.md`; total spend logged and under the $30 ceiling.

**Steps:**

- [ ] **Step 1: Preflight.** Run `pixi run preflight`. Must exit 0. If dirty tree — commit/stash first (scaffold from Tasks 0-1 must already be committed).

- [ ] **Step 2: Launch the smoke** (autonomous, pre-authorized within $30):

```bash
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-wan-t2v-14b-2_2.yaml \
  --mode t2v \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)" \
  --no-reuse
```

- [ ] **Step 3: Monitor (Modal has NO util probe).** Poll `pixi run -e live-modal modal app list` + the orchestrator log. Watch for: 80GB container allocated (early); HF download progressing (bootstrap not proxied on Modal — rely on orchestrator log). ABORT + stop the app if no GPU allocates or the download stalls past `boot_timeout` (45m). Idle burn is a silent failure, not patience.

- [ ] **Step 4: Frame-QA (mandatory).** Extract ~5 frames with `kinoforge.core.frames.ffmpeg_frames_by_count`, Read them, judge corruption/coherence/adherence. Record the verdict. Anything not clearly high quality gets an explicit ⚠️.

- [ ] **Step 5: Verify teardown.** `pixi run kinoforge list` → `No running instances.` + `No instances recorded in ledger.`. Then `pixi run -e live-modal modal app list` → the M2 app not active. If anything lingers: `pixi run kinoforge destroy --id <id>` and/or `modal app stop <app>`.

- [ ] **Step 6: Log + PROGRESS.** Add `successful-generations.md` §23 (schema in that file's preamble; new axis A14B-on-Modal-80GB, include prompt, cfg, GPU, spend, frame-QA verdict). Update `PROGRESS.md` RESUME SNAPSHOT (M2 live-green) + SINGLE NEXT ACTION → Milestone 3 (FlashVSR full-res on Modal 80GB). Commit:

```bash
git add successful-generations.md PROGRESS.md
git commit -m "docs(gen): Modal Milestone 2 — Wan 2.2 T2V-A14B on 80GB live-green + frame-QA"
```
