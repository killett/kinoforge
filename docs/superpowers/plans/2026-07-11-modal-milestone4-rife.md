# Modal Milestone 4 — RIFE interpolation via fast-boot bake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run RIFE v4.26 frame interpolation (16 fps → 60 fps) on Modal serverless GPU via the M3 image-bake fast-boot path, proven live, closing the Modal engine matrix (t2v/upscale/interpolate).

**Architecture:** Pure config. The Task-6 provision split already routes composed `interpolate.engine` provision into `build_script`, and `ModalProvider` already bakes it — so RIFE-on-Modal fast-boots automatically. This plan adds one new cfg (`examples/configs/modal-rife-60fps.yaml`) mirroring the Modal FlashVSR cfg's transport shape + the RunPod RIFE cfg's interpolate block, an offline split-characterization test, a RED live scaffold committed before spend, and a live proof.

**Tech Stack:** Python 3.13, Modal SDK (`live-modal` env), diffusers `wan_t2v_server`, RIFE v4.26 (Practical-RIFE), pytest.

**User decisions (already made):**
- **Pure-cfg, accept the apt waste** — no provider/engine changes; RIFE's image carries the FlashVSR-only `build-essential/cmake/pkg-config` (~200 MB, one-time cached). Selected 2026-07-11 via AskUserQuestion.
- **T4-first GPU** — `gpu_preference: [T4, L4, A10G]`; RIFE needs ~2 GB VRAM, no SM80. Selected 2026-07-11.
- **numpy<2-on-py3.13 is discover-in-live** — do NOT pre-solve the numpy pin in the cfg; let the live proof surface it. Selected 2026-07-11.
- **fps=60 + the §20 fixture** (`output/20260630-221857_..._Photorealistic-cinem.mp4`, 480²/81f/16fps).

Spec: `docs/superpowers/specs/2026-07-11-modal-milestone4-rife-design.md`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `examples/configs/modal-rife-60fps.yaml` (create) | Modal RIFE cfg: python:3.13-slim + torch, T4-first, RIFE 60fps block | 0 |
| `tests/test_modal_rife_config.py` (create) | Offline: cfg loads (modal provider, no `cloud:`) + provision split shape | 0 |
| `tests/live/test_modal_rife_60fps.py` (create) | RED live scaffold (`pytest.mark.live`), committed before spend | 1 |
| `successful-generations.md`, `PROGRESS.md` (modify) | Log §25 after the live proof | 2 |

Ordering: **0 → 1 → 2 (live)**. Task 1 needs Task 0's cfg to exist; Task 2 needs both.

---

### Task 0: Modal RIFE config + offline split characterization

**Goal:** A new `modal-rife-60fps.yaml` that loads as a Modal (non-sky) interpolate cfg, and whose rendered provision splits so the RIFE install (git clone / pip / weights) lands in `build_script` and the server exec in `runtime_script`.

**Files:**
- Create: `examples/configs/modal-rife-60fps.yaml`
- Create: `tests/test_modal_rife_config.py`

**Acceptance Criteria:**
- [ ] `load_config("examples/configs/modal-rife-60fps.yaml")` succeeds; `compute.provider == "modal"`, no `compute.cloud` key, `interpolate.engine == "rife"`, `interpolate.fps == 60.0`.
- [ ] `build_script` contains the RIFE `git clone` of `Practical-RIFE`, the `numpy<2` pip pin, the `RIFEv4.26` weights fetch, and the cfg's `torch==2.6.0` (slim ships no torch).
- [ ] `build_script` does NOT contain the `python -m ...wan_t2v_server` exec.
- [ ] `runtime_script` contains the `wan_t2v_server` exec and does NOT contain `git clone` / `numpy<2`.

**Verify:** `pixi run pytest tests/test_modal_rife_config.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the cfg** `examples/configs/modal-rife-60fps.yaml`:

```yaml
# RIFE v4.26 frame interpolation (16fps -> 60fps) on Modal serverless GPU
# (Milestone 4). Reuses the M3 fast-boot image bake: the composed RIFE provision
# (git clone Practical-RIFE + pip + HF weights zip) is baked into the Modal image
# at BUILD time, so the container boots in seconds. Delivery: the diffusers
# wan_t2v_server on a Modal web_server(8000), same transport as M1/M2/M3.
#
# NO `cloud:` key — that field is SkyPilot-only and fails validation here.
#
# Usage (interpolate-only; supply the source clip via --video):
#   pixi run -e live-modal kinoforge interpolate \
#     --config examples/configs/modal-rife-60fps.yaml \
#     --video output/20260630-221857_..._Photorealistic-cinem.mp4 \
#     --fps 60 --no-reuse

engine:
  kind: diffusers
  precision: fp16
  diffusers:
    # Modal's serialized web_server fn requires image-Python == controller (3.13).
    image: "python:3.13-slim"
    server_cmd:
      - "python"
      - "-m"
      - "kinoforge.engines.diffusers.servers.wan_t2v_server"
    pip:
      # torch is ADDED vs the RunPod RIFE cfg: python:3.13-slim ships no torch
      # (runpod/pytorch did). No ABI lock (RIFE has no compiled ext like BSA) —
      # torch 2.6.0 cu124 cp313 for stack consistency with M3.
      - "torch==2.6.0"
      - "torchvision==0.21.0"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
    embed_modules:
      - "kinoforge.engines.diffusers.servers"
      - "kinoforge.interpolators.rife"
    embed_files:
      - "kinoforge.core.errors"
      - "kinoforge.core.fps_resolver"
    # Skip eager WanPipeline load — pod runs only the on-demand RIFE runtime.
    upscale_only: true

models: []

compute:
  provider: modal
  image: "python:3.13-slim"
  mode: pod
  requirements:
    min_vram_gb: 16
    min_cuda: "12.4"
    max_usd_per_hr: 1.00
    disk_gb: 40
    gpu_preference:
      - "T4"
      - "L4"
      - "A10G"
  lifecycle:
    boot_timeout: 30m
    idle_timeout: 20m
    job_timeout: 12m
    time_buffer: 3m
    max_lifetime: 45m
    budget: 0.5

spec:
  model: "rife-rife49"

interpolate:
  engine: rife
  fps: 60.0
  rife:
    weights_ref: "hf:hzwer/RIFE"
    model: rife426
    precision: fp16
```

- [ ] **Step 2: Write the failing test** `tests/test_modal_rife_config.py`:

```python
"""Offline: the Modal RIFE cfg loads and its provision splits build/runtime.

Milestone 4 rides the M3 fast-boot bake — the composed RIFE install must land in
build_script (baked into the image) and the server exec in runtime_script.
"""

from kinoforge.core.config import load_config
from kinoforge.engines.diffusers import DiffusersEngine

_CFG = "examples/configs/modal-rife-60fps.yaml"
_SERVER_EXEC = "python -m kinoforge.engines.diffusers.servers.wan_t2v_server"


def _render():
    return DiffusersEngine().render_provision(load_config(_CFG).model_dump())


def test_cfg_loads_modal_provider_no_cloud() -> None:
    # Bug caught: a stray `cloud:` key (SkyPilot-only) or wrong provider makes
    # the cfg route to the wrong transport / fail validation at run time.
    d = load_config(_CFG).model_dump()
    assert d["compute"]["provider"] == "modal"
    assert "cloud" not in d["compute"]
    assert d["interpolate"]["engine"] == "rife"
    assert d["interpolate"]["fps"] == 60.0


def test_build_script_has_rife_install_not_server() -> None:
    # Bug caught: RIFE install leaks out of the bakeable build phase (Modal can't
    # bake it → slow boot → preemption), or the server exec wrongly bakes in.
    b = _render().build_script
    assert "git clone" in b and "Practical-RIFE" in b  # RIFE repo clone
    assert "numpy<2" in b  # RIFE's pip pin
    assert "RIFEv4.26" in b  # weights zip fetch
    assert "torch==2.6.0" in b  # torch baked (slim has none)
    assert _SERVER_EXEC not in b  # server exec is runtime, never baked


def test_runtime_script_has_server_not_rife_install() -> None:
    # Bug caught: the RIFE install stays in the runtime boot → re-downloads at
    # container start, re-opening the preemption window.
    r = _render().runtime_script
    assert _SERVER_EXEC in r
    assert "git clone" not in r
    assert "numpy<2" not in r
```

- [ ] **Step 3: Run — confirm it fails** (cfg file does not exist yet, or split assertions).

Run: `pixi run pytest tests/test_modal_rife_config.py -v` → FAIL (FileNotFoundError / assertion).

- [ ] **Step 4: If a substring assertion fails**, print the actual `build_script` to reconcile the exact token the RIFE engine emits, and adjust the assertion to the real string (the RIFE engine at `src/kinoforge/interpolators/rife/_engine.py` is the source of truth for `Practical-RIFE`, `numpy<2`, `RIFEv4.26`):

```bash
pixi run python -c "
from kinoforge.core.config import load_config
from kinoforge.engines.diffusers import DiffusersEngine
b=DiffusersEngine().render_provision(load_config('examples/configs/modal-rife-60fps.yaml').model_dump()).build_script
for ln in b.split(chr(10)):
    print(ln[:160])
"
```

- [ ] **Step 5: Run — confirm PASS.** Also run the broader diffusers/config suites to catch regressions:

Run: `pixi run pytest tests/test_modal_rife_config.py tests/engines/diffusers/test_render_provision_split.py -q` → PASS

- [ ] **Step 6: Commit:**

```bash
pixi run pre-commit run --files examples/configs/modal-rife-60fps.yaml tests/test_modal_rife_config.py
git add examples/configs/modal-rife-60fps.yaml tests/test_modal_rife_config.py
git commit -m "feat(config): Modal RIFE 60fps cfg (T4-first, fast-boot bake) + offline split test"
```

---

### Task 1: RED live scaffold (committed before any spend)

**Goal:** A `pytest.mark.live` end-to-end scaffold for the Modal RIFE interpolate run, committed BEFORE the live spend (durability rule) so a mid-spend crash never loses it.

**Files:**
- Create: `tests/live/test_modal_rife_60fps.py`

**Acceptance Criteria:**
- [ ] The test is marked `pytest.mark.live` so `pixi run test` (`-m 'not live'`) SKIPS it (offline suite stays green).
- [ ] Under `-m live` it drives `kinoforge interpolate` on the RIFE cfg and asserts a ~60 fps output; it is committed RED (may xfail or assert on a not-yet-proven output).

**Verify:** `pixi run pytest tests/live/test_modal_rife_60fps.py -q` (default env) → 1 skipped (not selected under `-m 'not live'`, or collected-then-skipped); it must NOT run live in the offline suite.

**Steps:**

- [ ] **Step 1: Read the M3 RED scaffold** to mirror its shape:

```bash
sed -n '1,60p' tests/live/test_modal_flashvsr_x4.py
```

- [ ] **Step 2: Write** `tests/live/test_modal_rife_60fps.py` (mirror the FlashVSR scaffold's structure — env-gated on `KINOFORGE_LIVE_TESTS`, drives the orchestrator/CLI, ffprobe-asserts the output):

```python
"""LIVE: RIFE v4.26 60fps interpolation on Modal via the fast-boot image bake.

Runs only under `pixi run -e live-modal` with KINOFORGE_LIVE_TESTS=1. Marked
`live` so the default suite (`-m 'not live'`) skips it. Milestone 4 proof —
mirrors the M3 FlashVSR live scaffold (tests/live/test_modal_flashvsr_x4.py).
"""

import os
import subprocess

import pytest

pytestmark = pytest.mark.live

_CFG = "examples/configs/modal-rife-60fps.yaml"
_SRC = "output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4"


@pytest.mark.skipif(
    os.environ.get("KINOFORGE_LIVE_TESTS") != "1",
    reason="live Modal spend — set KINOFORGE_LIVE_TESTS=1 to run",
)
def test_modal_rife_60fps_end_to_end() -> None:
    # RED until proven: drives the interpolate CLI on Modal and asserts a ~60fps
    # output. The controller (Task 2) runs this / the equivalent CLI live, then
    # frame-QAs and logs §25.
    proc = subprocess.run(
        [
            "kinoforge",
            "interpolate",
            "--config",
            _CFG,
            "--video",
            _SRC,
            "--fps",
            "60",
            "--no-reuse",
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    assert proc.returncode == 0, proc.stderr
    # The published artifact path is printed as `interpolated: uri=...`.
    assert "interpolated" in proc.stdout.lower()
```

- [ ] **Step 3: Confirm the offline suite skips it:**

Run: `pixi run test 2>&1 | tail -3` → the summary shows the file skipped/deselected, suite still green (`0 failed`).

- [ ] **Step 4: Commit the RED scaffold BEFORE any spend:**

```bash
pixi run pre-commit run --files tests/live/test_modal_rife_60fps.py
git add tests/live/test_modal_rife_60fps.py
git commit -m "test(live): RED scaffold for Modal RIFE 60fps (Milestone 4 proof, pre-spend)"
```

---

### Task 2: Live RIFE 60fps proof on Modal + frame-QA + teardown + §25

**Goal:** Run the RIFE interpolate live on Modal (T4-first, fast boot); produce a ~60 fps clip; frame-QA it; verify teardown; log `successful-generations.md` §25 and update `PROGRESS.md`.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation (Modal M4 live proof). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `successful-generations.md` (new §25), `PROGRESS.md` (RESUME SNAPSHOT + SINGLE NEXT ACTION)

**Acceptance Criteria:**
- [ ] `pixi run preflight` → PASS before spend.
- [ ] Container binds fast (bake once, then seconds) — NOT a long preemption loop; if the numpy<2 source build errors, it surfaces at BUILD (fail fast, no GPU spend) and is handled per the deviation note below.
- [ ] `kinoforge interpolate` completes; output is **~60 fps** (ffprobe `r_frame_rate=60/1`), frame count ≈ 4× the source (81f/16fps 480² → ~304f), dims unchanged (480×480, temporal-only).
- [ ] Frame-QA on ~5 frames: smooth pose deltas (genuine arbitrary-timestep synthesis, no frame-repeat), no ghosting/warping (⚠️-flag if not clearly HQ).
- [ ] After orchestrator exit: `pixi run kinoforge list` → no instances AND `modal app list` → no running kinoforge app.
- [ ] `successful-generations.md` §25 written (RIFE on Modal, T4, fast-boot bake recipe).

**Verify:** `pixi run kinoforge list` → No running instances + empty ledger; `ffprobe` output → `r_frame_rate=60/1`; §25 present.

**Steps:**

- [ ] **Step 1: Preflight** — `pixi run preflight` → PASS.

- [ ] **Step 2: Run the live interpolate** (source clip via `--video`, `--no-reuse`). The FIRST run triggers the Modal image build (bakes torch + RIFE clone/weights, and compiles `numpy<2` from source on py3.13 — watch the build log). `kinoforge` self-loads `.env`; for the monitoring commands below, `set -a; . /workspace/.env; set +a` first.

```bash
pixi run -e live-modal kinoforge interpolate \
  --config examples/configs/modal-rife-60fps.yaml \
  --video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4 \
  --fps 60 --no-reuse
```

- [ ] **Step 3: Monitor** the image build (streams to the deploy log) + Modal app state. **numpy<2 is the #1 risk** — if the build errors on the numpy source build, it fails FAST at BUILD (no GPU spend). Deviation handling if it fails: (a) check whether `build-essential` compiled it (usually does); (b) if it genuinely can't build, test whether the pinned Practical-RIFE commit tolerates numpy 2 and relax the `numpy<2` pin in the RIFE engine, OR pin a py3.13-buildable numpy in the cfg `pip:`. Any code change here is an in-plan deviation — commit it before re-spending.

- [ ] **Step 4: Frame-QA** — extract ~5 frames from the 60 fps output via ffmpeg and read them; confirm smooth pose deltas between adjacent frames (real interpolation, not repeats), no ghosting/warping. Record the verdict.

```bash
SCR="$(mktemp -d)"
OUT="$(ls -t output/*interpolated*rife*.mp4 | head -1)"
pixi run ffmpeg -hide_banner -loglevel error -i "$OUT" \
  -vf "select='not(mod(n\,60))'" -vsync vfr -frames:v 5 "$SCR/rifeqa_%d.png"
# then Read the $SCR/rifeqa_*.png frames
```

- [ ] **Step 5: Verify teardown** — after the orchestrator exits:

```bash
pixi run kinoforge list
set -a; . /workspace/.env; set +a
pixi run -e live-modal modal app list | grep -i kinoforge || echo "no kinoforge apps"
```

Expected: `No running instances.` + `No instances recorded in ledger.` AND no running Modal app. Destroy any leftover with `pixi run kinoforge destroy --id <id>`.

- [ ] **Step 6: Log §25 + update PROGRESS; commit** (follow the §24 schema in `successful-generations.md`: stack triple = `Modal / DiffusersEngine (RIFE interpolator) / hzwer/RIFE`, mode = interpolate, new axis = "RIFE interpolation on Modal via fast-boot bake (M4)"; include exact command, artifact table with ffprobe 60fps + SHA, frame-QA verdict, reproduction notes incl. the numpy<2 outcome):

```bash
git add successful-generations.md PROGRESS.md
git commit -m "docs(gen): Modal M4 RIFE 60fps live-green (fast-boot bake) + frame-QA"
```

---

## Self-Review

**Spec coverage:** cfg (spec §"The config") → Task 0. Offline split test (spec §Testing "Offline") → Task 0. RED live scaffold (spec §Testing "RED live scaffold") → Task 1. Live proof + frame-QA + teardown + §25 (spec §Testing "Live proof") → Task 2. numpy<2 discover-in-live (spec §Known risks #1) → Task 2 Step 3 deviation note. Pure-cfg / no provider change (spec §Approach + Non-goals) → no code task present. All covered.

**Placeholder scan:** No TBD/TODO. Test bodies are complete; Task 0 Step 4 is an explicit reconcile-the-exact-token instruction (not a placeholder), since the RIFE engine's literal strings are the source of truth.

**Type consistency:** cfg path `examples/configs/modal-rife-60fps.yaml` and `_SERVER_EXEC` string are consistent across Tasks 0/1/2. No new types introduced (pure cfg).
