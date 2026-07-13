# Modal FlashVSR 1080p Height-Target Upscale — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Modal FlashVSR height-target ("1080p") upscale config + offline test, then live-prove it on Modal (480²→1920²→1080²).

**Architecture:** Height-target is provider-agnostic controller logic already shipped (`pipeline/upscale.py:_run_height` → `resolve_height_target` → materialize lanczos downscale). No production code changes. Task 0 adds the config + offline parse-guard + RED live scaffold in one pre-spend commit; Task 1 runs the live smoke with util-poll, dims + frame-QA, teardown verify, and logs it.

**Tech Stack:** kinoforge CLI (`kinoforge upscale`), Modal provider (`live-modal` pixi env), FlashVSR v1.1 (cp313 BSA wheel), pytest.

**User decisions (already made):**
- Scope = "Config + live Modal 1080p smoke" (offline test + RED scaffold + live proof + `successful-generations.md` entry). NOT the render+upscale variant.
- Spec: `docs/superpowers/specs/2026-07-12-modal-flashvsr-1080p-height-target-design.md`.
- Fixture: `output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4` (480²/81f).

---

### Task 0: Config + offline parse-guard + RED live scaffold (pre-spend commit)

**Goal:** Add the Modal 1080p config, an offline test proving it parses to a height ScaleTarget, and the RED live scaffold — all committed atomically before any live spend.

**Files:**
- Create: `examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml`
- Modify: `tests/test_modal_config.py` (append two tests)
- Create: `tests/live/test_modal_flashvsr_1080p.py`

**Acceptance Criteria:**
- [ ] New config is byte-identical to `modal-diffusers-flashvsr-x4-upscale.yaml` except `upscale.scale: 1080p` (not `4x`) and the header/usage comments.
- [ ] Offline test asserts `ScaleTarget.parse(cfg.upscale.scale)` → `kind="height"`, `value==1080.0`.
- [ ] Offline test asserts `cfg.upscale.engine=="flashvsr"`, `cfg.compute.provider=="modal"`, `cfg.engine.diffusers.upscale_only is True`.
- [ ] RED live scaffold mirrors `tests/live/test_modal_flashvsr_x4.py` (xfail, `live` marker, names the new config + fixture, asserts on 1080×1080).
- [ ] `pixi run pre-commit run --all-files` passes; all three files committed in one commit.

**Verify:** `pixi run test -- tests/test_modal_config.py -v` → the two new tests PASS; `pixi run test -- tests/live/test_modal_flashvsr_1080p.py -v` → xfailed (not error).

**Steps:**

- [ ] **Step 1: Write the failing offline tests**

Append to `tests/test_modal_config.py` (uses the existing `_load` helper + `ScaleTarget` — check imports at top of file; add `from kinoforge.core.scale_target import ScaleTarget` if absent):

```python
CFG_FLASHVSR_1080P = Path("examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml")


def test_flashvsr_1080p_config_is_height_target():
    """The Modal 1080p cfg must parse to a HEIGHT ScaleTarget, not a factor.

    Bug caught: a copy-paste from the x4 cfg that leaves `scale: 4x` (factor)
    would silently ship a non-height config under a 1080p filename.
    """
    cfg = _load(CFG_FLASHVSR_1080P)
    assert cfg.upscale is not None
    assert cfg.upscale.scale == "1080p"
    target = ScaleTarget.parse(cfg.upscale.scale)
    assert target.kind == "height"
    assert target.value == 1080.0


def test_flashvsr_1080p_config_is_modal_flashvsr_upscale_only():
    """The 1080p cfg targets the same Modal/FlashVSR/upscale-only surface as x4.

    Bug caught: wrong provider or engine, or losing upscale_only (which would
    eagerly load Wan and blow the boot budget).
    """
    cfg = _load(CFG_FLASHVSR_1080P)
    assert cfg.compute.provider == "modal"
    assert cfg.upscale is not None
    assert cfg.upscale.engine == "flashvsr"
    assert cfg.engine.diffusers.upscale_only is True
    assert cfg.upscale.flashvsr is not None
    assert "cp313" in cfg.upscale.flashvsr.bsa_wheel_url
```

Note: confirm the loader helper name in `tests/test_modal_config.py` (existing tests call `_load(...)` or similar — reuse whatever `test_flashvsr_config_is_upscale_only_80gb_cp313` uses at line ~66). Match it exactly.

- [ ] **Step 2: Run tests — verify they fail**

Run: `pixi run test -- tests/test_modal_config.py -k 1080p -v`
Expected: FAIL — config file does not exist yet (`FileNotFoundError` / loader error).

- [ ] **Step 3: Create the config**

Create `examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml` — clone of `modal-diffusers-flashvsr-x4-upscale.yaml` with the header rewritten and `scale: 1080p`. Full content:

```yaml
# FlashVSR v1.1 HEIGHT-TARGET upscale to 1080p on Modal serverless GPU.
# Height-target variant of modal-diffusers-flashvsr-x4-upscale.yaml: instead of a
# raw 4x factor, `upscale.scale: 1080p` asks for a 1080-pixel vertical deliverable.
#
# FlashVSR is 4x-native, so UpscaleStage resolves 1080p on a 480² source to 4x
# (→1920²) and the orchestrator materialize boundary lanczos-downscales 1920→1080
# (aspect preserved → 1080×1080 square). The downscale runs on the CONTROLLER after
# fetching the upscaled bytes — height-target is provider-agnostic pipeline logic,
# identical to the RunPod 1080p path. Wan is NOT involved (upscale_only) — supply
# the source clip via --video.
#
# See docs/superpowers/specs/2026-07-05-height-target-upscale-design.md and
# docs/superpowers/specs/2026-07-12-modal-flashvsr-1080p-height-target-design.md.
#
# BSA note: Modal's serialized web-server fn forces a py3.13 image, but FlashVSR's
# Block-Sparse-Attention prebuilt wheels are cp311. This cfg points bsa_wheel_url
# at a cp313 wheel (torch2.6+cu124, BSA 3453bbb1) built on Modal; see
# tools/build_bsa_wheel_modal.py.
#
# NO `cloud:` key — that field is SkyPilot-only and fails validation here.
#
# Usage (upscale-only; supply the source clip via --video):
#   pixi run -e live-modal kinoforge upscale \
#     --config examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml \
#     --video output/20260630-221857_..._Photorealistic-cinem.mp4 \
#     --no-reuse

engine:
  kind: diffusers
  precision: bfloat16
  diffusers:
    # Modal serialized web_server fn requires image-Python == controller (3.13).
    # python:3.13-slim matches; provision pip-installs torch 2.6 cu124 (cp313
    # wheels bundle the CUDA runtime + libgomp the BSA wheel needs at runtime).
    image: "python:3.13-slim"
    server_cmd:
      - "python"
      - "-m"
      - "kinoforge.engines.diffusers.servers.wan_t2v_server"
    pip:
      # torch 2.6.0+cu124 trio — the cp313 BSA wheel below links against exactly
      # this torch (ABI lockstep: a mismatch surfaces as a c10 undefined-symbol
      # at inference, not at install). Do NOT bump without rebuilding the wheel.
      - "torch==2.6.0"
      - "torchvision==0.21.0"
      - "torchaudio==2.6.0"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
      # diffsynth (FlashVSR upstream) does a module-top
      # `from modelscope import snapshot_download` even for HF weights.
      - "modelscope"
      # FlashVSR is pip-installed from git+https; its setup.py imports
      # pkg_resources at build. python:3.13-slim's pip ships no setuptools, and
      # modern setuptools (>=81) REMOVED pkg_resources — pin <81 (last series that
      # bundles pkg_resources). wheel too, for the PEP 517 build backend.
      - "setuptools<81"
      - "wheel"
    embed_modules:
      - "kinoforge.engines.diffusers.servers"
      - "kinoforge.upscalers.flashvsr"
    embed_files:
      - "kinoforge.core.errors"
      - "kinoforge.core.scale_target"
    # Skip eager WanPipeline load — pod runs only the on-demand FlashVSR runtime.
    upscale_only: true

models: []

compute:
  provider: modal
  image: "python:3.13-slim"
  mode: pod
  requirements:
    # FlashVSR 480->1920 4x peaks ~42-46 GB with tile_size=512 — an 80GB card runs
    # the full 81f clip without a source downscale. (The 1920->1080 deliverable
    # downscale happens on the controller, post-fetch, and costs no GPU.)
    min_vram_gb: 80
    min_cuda: "12.4"
    max_usd_per_hr: 4.00
    disk_gb: 60
    gpu_preference:
      - "A100-80GB"
      - "H100"
  lifecycle:
    # BSA wheel fetch+install ~60s; FlashVSR pip+weights ~5min; first-call setup.
    # 45m mirrors M2/M3 and gives wide headroom over the ~8min happy path.
    idle_timeout: 30m
    job_timeout: 15m
    time_buffer: 3m
    max_lifetime: 90m
    boot_timeout: 45m
    budget: 2.0

spec:
  model: "flashvsr-wan21-bfloat16"

upscale:
  # Height target: resolve to FlashVSR's native 4x (480→1920), then lanczos-
  # downscale 1920→1080 at the orchestrator materialize boundary.
  engine: flashvsr
  scale: 1080p
  flashvsr:
    weights_bundle: "hf:JunhaoZhuang/FlashVSR-v1.1"
    # cp313 BSA wheel — built on Modal (tools/build_bsa_wheel_modal.py) against
    # torch 2.6.0+cu124 / BSA 3453bbb1, hosted on killett/kinoforge-artifacts.
    bsa_wheel_url: "https://github.com/killett/kinoforge-artifacts/releases/download/bsa-cu124-torch2.6-cp313-v1/block_sparse_attn-0.0.1-cp313-cp313-linux_x86_64.whl"
    precision: bfloat16
    window_size: 24
    tile_size: 512
    long_video_mode: false
```

- [ ] **Step 4: Run offline tests — verify they pass**

Run: `pixi run test -- tests/test_modal_config.py -k 1080p -v`
Expected: PASS (both new tests).

- [ ] **Step 5: Write the RED live scaffold**

Create `tests/live/test_modal_flashvsr_1080p.py`, mirroring `tests/live/test_modal_flashvsr_x4.py`:

```python
"""LIVE: FlashVSR HEIGHT-TARGET 1080p (480->1920->1080) on Modal 80GB. Driven
manually via the CLI; this file records the contract + a smoke assertion on the
artifact. Height-target is provider-agnostic pipeline logic — this proves it
end-to-end on Modal."""

import pytest

pytestmark = pytest.mark.live

UPSCALE_CMD = (
    "pixi run -e live-modal kinoforge upscale "
    "--config examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml "
    "--video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_"
    "Photorealistic-cinem.mp4 "
    "--no-reuse"
)


@pytest.mark.xfail(
    reason="live proof driven via CLI; see PROGRESS + successful-generations 1080p entry"
)
def test_modal_flashvsr_1080p_contract():
    raise AssertionError(
        "run UPSCALE_CMD live; assert 1080x1080 mp4 (NOT 1920x1920 — proves the "
        "materialize downscale ran) + frame-QA vs 480 source"
    )
```

- [ ] **Step 6: Run the live scaffold — verify xfail (not error)**

Run: `pixi run test -- tests/live/test_modal_flashvsr_1080p.py -v`
Expected: 1 xfailed (collection succeeds, no import error).

- [ ] **Step 7: Lint + commit (pre-spend, atomic)**

Run: `pixi run pre-commit run --all-files`
Expected: all hooks pass.

```bash
git add examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml \
        tests/test_modal_config.py \
        tests/live/test_modal_flashvsr_1080p.py
git commit -m "feat(config): Modal FlashVSR 1080p height-target upscale cfg + offline guard + RED live scaffold"
```

```json:metadata
{"files": ["examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml", "tests/test_modal_config.py", "tests/live/test_modal_flashvsr_1080p.py"], "verifyCommand": "pixi run test -- tests/test_modal_config.py -k 1080p -v", "acceptanceCriteria": ["1080p cfg parses to ScaleTarget(kind=height, value=1080)", "cfg is modal/flashvsr/upscale_only", "RED live scaffold xfails cleanly", "all three files committed pre-spend"], "modelTier": "standard"}
```

---

### Task 1: Live Modal 1080p smoke + frame-QA + log

**Goal:** Run the Modal 1080p upscale live, prove the output is 1080×1080 (downscale ran), frame-QA it, tear down clean, and log it to `successful-generations.md`.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `successful-generations.md` (new section + TOC entry)
- Modify: `PROGRESS.md` (RESUME SNAPSHOT + pointer)

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 before spend.
- [ ] Live run completes; artifact written.
- [ ] `ffprobe` on the output reports **1080×1080** (NOT 1920×1920 — proves the materialize downscale ran).
- [ ] Frame-QA: ~5 frames extracted + eyeballed; verdict recorded (⚠️ if not clearly high quality).
- [ ] `kinoforge list` after the run shows both `No running instances.` AND `No instances recorded in ledger.`
- [ ] New `successful-generations.md` section (new YAML shape = new capability axis: Modal + height-target).
- [ ] `PROGRESS.md` updated + committed.

**Verify:** `pixi run kinoforge list` → no running instances + empty ledger; `ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 <output>` → `1080,1080`.

**Steps:**

- [ ] **Step 1: Preflight**

Run: `pixi run preflight`
Expected: exit 0 (creds present, zero pods, clean tree). If non-zero, stop and resolve before spending.

- [ ] **Step 2: Confirm fixture exists**

Run: `ls -la output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4`
Expected: file present. If absent, pick another 480² clip from `output/` and update the command.

- [ ] **Step 3: Launch the live upscale (background) + util-poll**

Run in the background so the poll loop can run concurrently:

```bash
pixi run -e live-modal kinoforge upscale \
  --config examples/configs/modal-diffusers-flashvsr-1080p-upscale.yaml \
  --video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4 \
  --no-reuse
```

Every 60–90 s while it runs, poll util (NOT spend) — ledger-resolved Modal probe:

```python
from kinoforge.core.dotenv_loader import load_env_file; load_env_file()
from kinoforge.providers.modal.util import ModalUtilEndpoint  # ModalUtilEndpoint.read_util
# resolve the .modal.run URL from the ledger, then read_util(); surface
# gpu_util_percent / cpu_percent / memory_percent each poll.
```

Action on stall: GPU 0% for ≥3 consecutive polls while a gen is in flight → capture the Modal boot log, destroy the app, fail fast (do NOT wait for the job timeout).

- [ ] **Step 4: Verify output dimensions = 1080×1080**

Run: `ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 <output.mp4>`
Expected: `1080,1080`. If `1920,1920` → the height-target downscale did NOT run; this is a regression — do NOT log green, investigate `pipeline/materialize.py` / `downscale.py`.

- [ ] **Step 5: Frame-QA (mandatory before green)**

Extract ~5 frames and eyeball for artifacts, temporal coherence, prompt adherence, and fidelity vs the 1920² x4 sibling (§24):

```python
from kinoforge.core.frames import ffmpeg_frames_by_count
frames = ffmpeg_frames_by_count("<output.mp4>", count=5)  # returns frame paths/bytes
```

Read the frames (contact-sheet montage keeps context cost low). Record the verdict. Flag ⚠️ if anything is not clearly high quality.

- [ ] **Step 6: Verify teardown**

Run: `pixi run kinoforge list`
Expected: `[instance overview] No running instances.` AND `No instances recorded in ledger.` If either shows a pod: `pixi run kinoforge destroy --id <id>`.

- [ ] **Step 7: Log to successful-generations.md + update PROGRESS**

Add a new section to `successful-generations.md` per its preamble schema (provider=modal, engine=diffusers/flashvsr, mode=upscale, scale=1080p height-target), including: config path, exact command, source→intermediate→final dims (480²→1920²→1080²), GPU/cost, util-poll observations, and the frame-QA verdict. Add the TOC entry.

Update `PROGRESS.md` RESUME SNAPSHOT + pointers to record Modal 1080p height-target live-green.

```bash
git add successful-generations.md PROGRESS.md
git commit -m "docs(gen): Modal FlashVSR 1080p height-target live-green (480->1920->1080) + frame-QA"
```

```json:metadata
{"files": ["successful-generations.md", "PROGRESS.md"], "verifyCommand": "ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 <output.mp4>", "acceptanceCriteria": ["preflight exits 0 pre-spend", "output ffprobe reports 1080x1080 not 1920x1920", "frame-QA verdict recorded", "kinoforge list shows no running instances AND empty ledger", "successful-generations.md entry added", "PROGRESS.md updated"], "userGate": true, "tags": ["user-gate"], "gateScope": "live-smoke", "modelTier": "standard"}
```

---

## Self-Review

**Spec coverage:** Config (§Components 1) → Task 0 Step 3. Offline test (§2) → Task 0 Steps 1–4. RED scaffold (§3) → Task 0 Steps 5–7. Live smoke incl. util-poll, dims, frame-QA, teardown, log (§4) → Task 1 all steps. Out-of-scope render+upscale cfg → excluded. All covered.

**Placeholder scan:** No TBD/TODO; config content shown in full; test code shown in full; commands concrete. The one soft reference — the `_load` helper name in `tests/test_modal_config.py` — is flagged with the exact existing test to match (line ~66). OK.

**Type consistency:** `ScaleTarget.parse` → `.kind`/`.value` (verified against `src/kinoforge/core/scale_target.py`). `ffmpeg_frames_by_count` + `ModalUtilEndpoint.read_util` match CLAUDE.md / codebase. Config keys match the x4 cfg exactly. Consistent.
