# Modal Milestone 3 — FlashVSR 4x full-res on 80GB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a FlashVSR v1.1 4x upscale of a 480²×81f clip → 1920² on a Modal 80GB GPU, frame-QA it, and log it — proving the upscale axis on the Modal transport.

**Architecture:** Modal's serialized web-server fn forces a py3.13 image, but FlashVSR's Block-Sparse-Attention prebuilt wheel is cp311 (pip-rejects on py3.13). So first build a **cp313** BSA wheel (generalize the existing one-shot RunPod builder), host it on `killett/kinoforge-artifacts`, then mirror the RunPod torch2.6 FlashVSR cfg onto Modal py3.13-slim with `bsa_wheel_url` → the cp313 wheel. Small provider hardening wires `HF_HOME=/cache/hf`. Live upscale-only proof drives the existing `kinoforge upscale` path unchanged.

**Tech Stack:** Python 3.13, Modal SDK (`live-modal` pixi env), RunPod (wheel build), diffusers/FlashVSR server, torch 2.6.0+cu124, Block-Sparse-Attention, pytest.

**User decisions (already made):**
- BSA-on-Modal-py3.13 fork: **"Build cp313 BSA wheel"** (vs source-compile at boot, vs defer to M4). Selected 2026-07-09 via AskUserQuestion.
- Dense-attention fallback ruled out (debug-only, OOM-risky) — not offered as a live path.
- Full native 480→1920 4x on 80GB (not the §21 downscaled-to-288² compromise).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `tools/build_bsa_wheel.py` (modify) | Parameterize the one-shot builder to emit a cp313 wheel to a new release tag | 0 |
| `tests/tools/test_build_bsa_wheel.py` (create) | Characterize the generalized builder's `--dry-run` provision script | 0 |
| `examples/configs/modal-flashvsr-x4.yaml` (create) | Modal FlashVSR cfg — py3.13-slim + cp313 wheel + upscale-only 80GB | 2 |
| `tests/test_modal_config.py` (modify) | Offline characterization of the new cfg | 2 |
| `src/kinoforge/providers/modal/__init__.py` (modify) | Wire `HF_HOME=/cache/hf` into the container env | 3 |
| `tests/providers/modal/test_hf_home_env.py` (create) | Unit-test the HF_HOME wiring | 3 |
| `tests/live/test_modal_flashvsr_x4.py` (create) | RED live-proof scaffold (xfail contract) | 4 |
| `successful-generations.md` (modify) | Log §24 after the live proof | 5 |
| `PROGRESS.md` (modify) | RESUME SNAPSHOT + pointers after the milestone | 5 |

Ordering: **0 → 1 (live wheel build) → 2 → 4 → 5 (live proof)**; Task 3 (HF_HOME) is independent and can land anytime before Task 5.

---

### Task 0: Generalize `build_bsa_wheel.py` to emit a cp313 wheel

**Goal:** Parameterize the one-shot BSA wheel builder so it can build a `cp313-cp313` wheel (torch 2.6.0+cu124) to a new release tag, keeping the cp311 path reproducible.

**Files:**
- Modify: `tools/build_bsa_wheel.py`
- Test: `tests/tools/test_build_bsa_wheel.py` (create)

**Acceptance Criteria:**
- [ ] `_GH_TAG` targets `bsa-cu124-torch2.6-cp313-v1`.
- [ ] The rendered provision script installs a py3.13 interpreter and runs `pip wheel` under it (so the emitted tag is `cp313-cp313`).
- [ ] Torch pins stay `torch==2.6.0 torchvision==0.21.0 torchaudio==2.6.0` + cu124 index; BSA commit stays `3453bbb1`; arch list stays `8.0;8.6;8.9;9.0`.
- [ ] `--dry-run` prints the script and exits 0 without creating a pod.

**Verify:** `pixi run pytest tests/tools/test_build_bsa_wheel.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/tools/test_build_bsa_wheel.py
"""Behavior: the generalized BSA wheel builder renders a cp313 provision script."""

import importlib


def _mod():
    return importlib.import_module("tools.build_bsa_wheel")


def test_target_tag_is_cp313():
    m = _mod()
    assert m._GH_TAG == "bsa-cu124-torch2.6-cp313-v1"


def test_provision_script_builds_under_py313():
    m = _mod()
    script = m._build_provision_script(release_id=999)
    # A py3.13 interpreter is installed and used to build the wheel, so the
    # emitted wheel carries a cp313 ABI tag (not the image's cp311).
    assert "3.13" in script
    # Torch pin unchanged — the wheel links against build-time torch (T7.5).
    assert "torch==2.6.0" in script
    assert "torchvision==0.21.0" in script
    # BSA commit + arch list unchanged.
    assert "3453bbb1" in script
    assert "8.0;8.6;8.9;9.0" in script
    # Build runs pip wheel; the cp313 python drives it.
    assert "pip wheel" in script


def test_torch_index_is_cu124():
    m = _mod()
    assert "cu124" in m._TORCH_INDEX
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run pytest tests/tools/test_build_bsa_wheel.py -v`
Expected: FAIL — `_GH_TAG` is still `bsa-cu124-torch2.6-v1`; script has no `3.13`.

- [ ] **Step 3: Edit `tools/build_bsa_wheel.py`**

Change the tag constant:

```python
_GH_TAG = "bsa-cu124-torch2.6-cp313-v1"
```

In `_build_provision_script`, insert a py3.13 install prelude BEFORE the torch pin and drive the build with `python3.13`. The base image `runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04` ships apt; use the deadsnakes PPA for a clean py3.13 + venv so the wheel tag is cp313. Replace the section from `echo "=== PINNING TORCH ..."` through the `pip wheel` line with:

```python
    return f"""set -euo pipefail
echo "=== BSA WHEEL BUILDER (cp313) ==="
echo "pod=$(hostname) date=$(date -Is)"
# Idempotent-restart safety: if the wheel is already up, skip the build.
if curl -sf -H "Authorization: Bearer $GH_TOKEN" \\
     -H "Accept: application/vnd.github+json" \\
     "{assets_url}" | grep -q '"name".*\\.whl'; then
  echo "=== WHEEL_ALREADY_UPLOADED — skipping rebuild ==="
  sleep infinity
fi
echo "=== INSTALLING PYTHON 3.13 (wheel ABI tag = build python) ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -q
apt-get install -y -q python3.13 python3.13-venv python3.13-dev
python3.13 -m venv /tmp/py313
. /tmp/py313/bin/activate
python -c "import sys; assert sys.version_info[:2] == (3, 13), sys.version; print(sys.version)"
python -m ensurepip --upgrade
python -m pip install --quiet --upgrade pip
echo "=== PINNING TORCH (wheel links against build-time torch) ==="
python -m pip install --quiet {_TORCH_PINS} --extra-index-url {_TORCH_INDEX}
python -c "import torch; v = torch.__version__; \\
  assert v.startswith('2.6.0'), f'torch pin failed: {{v}}'; \\
  print(f'build torch={{v}} py={{__import__(\\"sys\\").version.split()[0]}}')"
python -m pip install --quiet packaging ninja
export TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0"
export MAX_JOBS=4
echo "=== CLONING BSA @{_BSA_COMMIT} ==="
git clone --depth 100 https://github.com/mit-han-lab/Block-Sparse-Attention.git /tmp/bsa
cd /tmp/bsa
git checkout {_BSA_COMMIT}
echo "=== BUILDING WHEEL (this is the long step) ==="
mkdir -p /tmp/whl
time python -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/whl .
WHEEL=$(ls /tmp/whl/*.whl | head -1)
NAME=$(basename "$WHEEL")
case "$NAME" in
  *cp313-cp313*) echo "=== CP313 TAG CONFIRMED: $NAME ===" ;;
  *) echo "FATAL: wheel is not cp313: $NAME" >&2; exit 91 ;;
esac
SHA=$(sha256sum "$WHEEL" | cut -d' ' -f1)
SIZE=$(stat -c%s "$WHEEL")
echo "=== WHEEL_MANIFEST ==="
echo "NAME=$NAME"
echo "SIZE=$SIZE"
echo "SHA256=$SHA"
echo "=== UPLOADING to {_GH_OWNER}/{_GH_REPO}@{_GH_TAG} ==="
curl -sSL --fail-with-body -X POST \\
  -H "Authorization: Bearer $GH_TOKEN" \\
  -H "Content-Type: application/octet-stream" \\
  -H "Accept: application/vnd.github+json" \\
  --data-binary @"$WHEEL" \\
  "{upload_url}?name=$NAME"
echo ""
echo "=== UPLOAD_DONE ==="
sleep infinity
"""
```

Also update the module docstring's "Current target" paragraph to name the cp313 tag and the deadsnakes py3.13 build path.

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run pytest tests/tools/test_build_bsa_wheel.py -v`
Expected: PASS

- [ ] **Step 5: Lint/type/commit**

```bash
pixi run pre-commit run --files tools/build_bsa_wheel.py tests/tools/test_build_bsa_wheel.py
git add tools/build_bsa_wheel.py tests/tools/test_build_bsa_wheel.py
git commit -m "feat(tools): build_bsa_wheel emits cp313 wheel for Modal py3.13"
```

---

### Task 1: Create the cp313 release + run the live wheel build (RunPod spend)

**Goal:** Produce and host the `block_sparse_attn-...-cp313-cp313-...whl` asset on `killett/kinoforge-artifacts@bsa-cu124-torch2.6-cp313-v1`.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation (roadmap: "each its own live smoke"; fork decision to build the wheel). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- No source changes (uses the Task-0 tool). Live ops only.

**Acceptance Criteria:**
- [ ] GH release tag `bsa-cu124-torch2.6-cp313-v1` exists on `killett/kinoforge-artifacts`.
- [ ] A release asset whose name contains `cp313-cp313` and ends `.whl` is present.
- [ ] The builder pod was destroyed (RunPod ledger clean) after upload.

**Verify:** `curl -sf -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" https://api.github.com/repos/killett/kinoforge-artifacts/releases/tags/bsa-cu124-torch2.6-cp313-v1 | rg -o '"name":"[^"]*cp313[^"]*\.whl"'` → prints the wheel asset name; then `pixi run kinoforge list` → no instances.

**Steps:**

- [ ] **Step 1: Confirm Task 0 is committed** (durability rule — scaffold before spend).

Run: `git log --oneline -1 tools/build_bsa_wheel.py` → shows the cp313 commit.

- [ ] **Step 2: Create the empty release tag** (the builder's `_get_release_id` requires it to exist).

```bash
# GH_TOKEN is loaded from .env (repo scope). Create a draft-less release on main.
curl -sSL --fail-with-body -X POST \
  -H "Authorization: Bearer $GH_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/killett/kinoforge-artifacts/releases \
  -d '{"tag_name":"bsa-cu124-torch2.6-cp313-v1","name":"BSA cu124 torch2.6 cp313 v1","body":"Block-Sparse-Attention wheel for Python 3.13 (Modal serialized web-server fn). Built at BSA 3453bbb1 against torch 2.6.0+cu124.","draft":false,"prerelease":false}'
```

Expected: JSON with the new release `id`.

- [ ] **Step 3: Dry-run the builder** (no spend) to eyeball the script.

Run: `GH_TOKEN=$GH_TOKEN pixi run python -m tools.build_bsa_wheel --dry-run`
Expected: prints the py3.13 provision script; `script len=...` on stderr.

- [ ] **Step 4: Preflight, then run the live build.**

```bash
pixi run preflight   # clean tree, creds, zero RunPod pods
GH_TOKEN=$GH_TOKEN pixi run python -m tools.build_bsa_wheel
```

Monitor per CLAUDE.md live-smoke rules: the builder polls the pod GPU/CPU util + release assets every 60s and destroys the pod on any exit. Expect ~25 min build, ~$0.80 (ceiling $2). Abort if GPU 0% for ≥3 probes with no compile progress.

- [ ] **Step 5: Verify the asset + teardown, then commit a note.**

```bash
curl -sf -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/killett/kinoforge-artifacts/releases/tags/bsa-cu124-torch2.6-cp313-v1 \
  | rg -o '"name":"[^"]*cp313[^"]*\.whl"'
pixi run kinoforge list   # expect: no instances + empty ledger
```

Record the exact wheel URL (it feeds Task 2). No source commit needed here; capture the URL in the Task-2 cfg.

---

### Task 2: Modal FlashVSR cfg + offline characterization

**Goal:** Create `examples/configs/modal-flashvsr-x4.yaml` (Modal py3.13-slim + cp313 BSA wheel + upscale-only 80GB) and lock it with offline tests.

**Files:**
- Create: `examples/configs/modal-flashvsr-x4.yaml`
- Modify: `tests/test_modal_config.py`

**Acceptance Criteria:**
- [ ] Config resolves to a `ModalProvider`, `cloud is None`, `min_vram_gb == 80`.
- [ ] `upscale.engine == "flashvsr"`, scale resolves to 4x, `bsa_wheel_url` contains `cp313`, `models == []`, `upscale_only is True`.
- [ ] `modal_offers(reqs)` returns an 80GB card first (`A100-80GB` ahead of `H100`).

**Verify:** `pixi run pytest tests/test_modal_config.py -v` → PASS (all, including the 3 new tests)

**Steps:**

- [ ] **Step 1: Write the failing tests** (append to `tests/test_modal_config.py`)

```python
CFG_FLASHVSR = Path("examples/configs/modal-flashvsr-x4.yaml")


def test_flashvsr_config_resolves_to_modal_provider():
    cfg = load_config(CFG_FLASHVSR)
    assert cfg.compute is not None
    assert cfg.compute.provider == "modal"
    assert cfg.compute.cloud is None  # non-sky
    assert isinstance(build_provider_for(cfg), ModalProvider)


def test_flashvsr_config_is_upscale_only_80gb_cp313():
    cfg = load_config(CFG_FLASHVSR)
    assert cfg.compute is not None
    assert cfg.compute.requirements.min_vram_gb == 80
    assert cfg.models == []
    assert cfg.upscale is not None
    assert cfg.upscale.engine == "flashvsr"
    # Native 4x — full 480->1920 (the milestone's point).
    assert str(cfg.upscale.scale) in ("4x", "4.0", "4")
    fv = cfg.upscale.flashvsr
    assert fv is not None
    assert "cp313" in fv.bsa_wheel_url  # the Milestone-3 wheel
    assert cfg.engine.diffusers.upscale_only is True


def test_flashvsr_config_selects_80gb_offer_first():
    cfg = load_config(CFG_FLASHVSR)
    assert cfg.compute is not None
    offers = modal_offers(cfg.hardware_requirements())
    assert offers, "expected at least one 80GB offer"
    assert offers[0].vram_gb >= 80
    assert offers[0].gpu_type == "A100-80GB"
```

> If the accessor names (`cfg.upscale.flashvsr`, `cfg.engine.diffusers.upscale_only`, `cfg.upscale.scale`) differ from the loaded-config surface, adjust to match `tests/upscalers/flashvsr/test_config.py` — that file is the source of truth for the FlashVSR config shape.

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run pytest tests/test_modal_config.py -k flashvsr -v`
Expected: FAIL — config file does not exist.

- [ ] **Step 3: Create `examples/configs/modal-flashvsr-x4.yaml`**

Mirror `upscale-flashvsr-x4-torch26.yaml` (torch2.6 stack + `bsa_wheel_url`) merged with the M2 Modal transport. **Set `bsa_wheel_url` to the exact cp313 URL recorded in Task 1.**

```yaml
# FlashVSR v1.1 4x upscale (480x480 -> 1920x1920) on Modal serverless GPU
# (Milestone 3 live proof). Full native resolution on an 80GB card — unlike the
# SkyPilot/Lambda 40GB proof (successful-generations.md §21) that downscaled the
# source to 288sq. Delivery: Option-A generic Modal app — the diffusers
# wan_t2v_server runs on a Modal web_server(8000) via the same
# provision_script; exec run_cmd bundle as RunPod, reusing the M1/M2 transport.
#
# BSA note: Modal's serialized web-server fn forces a py3.13 image, but FlashVSR's
# Block-Sparse-Attention prebuilt wheels are cp311. This cfg points bsa_wheel_url
# at a cp313 wheel built for exactly this reason (torch2.6+cu124, BSA 3453bbb1).
# See docs/superpowers/specs/2026-07-09-modal-milestone3-flashvsr-design.md.
#
# NO `cloud:` key — that field is SkyPilot-only and fails validation here.
#
# Usage (upscale-only; supply the source clip via --video):
#   pixi run -e live-modal kinoforge upscale \
#     --config examples/configs/modal-flashvsr-x4.yaml \
#     --video output/20260630-221857_..._Photorealistic-cinem.mp4 \
#     --no-reuse

engine:
  kind: diffusers
  precision: bfloat16
  diffusers:
    # Modal serialized web_server fn requires image-Python == controller (3.13).
    # python:3.13-slim matches; provision pip-installs torch 2.6 cu124 (cp313
    # wheels bundle CUDA runtime + libgomp that the BSA wheel needs at runtime).
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
    # FlashVSR 480->1920 4x peaks ~42-46 GB with tile_size=512 (RunPod x4 cfg
    # history) — an 80GB card runs the full 81f clip without the §21 downscale.
    min_vram_gb: 80
    min_cuda: "12.4"
    max_usd_per_hr: 4.00
    disk_gb: 60
    gpu_preference:
      - "A100-80GB"
      - "H100"
  lifecycle:
    # BSA wheel fetch+install ~60s; FlashVSR pip+weights ~5min; first-call setup.
    # 45m mirrors M2 and gives wide headroom over the ~8min happy path.
    idle_timeout: 30m
    job_timeout: 15m
    time_buffer: 3m
    max_lifetime: 90m
    boot_timeout: 45m
    budget: 2.0

spec:
  model: "flashvsr-wan21-bfloat16"

upscale:
  engine: flashvsr
  scale: 4x
  flashvsr:
    weights_bundle: "hf:JunhaoZhuang/FlashVSR-v1.1"
    # <<< REPLACE with the exact cp313 wheel URL from Task 1 >>>
    bsa_wheel_url: "https://github.com/killett/kinoforge-artifacts/releases/download/bsa-cu124-torch2.6-cp313-v1/block_sparse_attn-0.0.1-cp313-cp313-linux_x86_64.whl"
    precision: bfloat16
    window_size: 24
    tile_size: 512
    long_video_mode: false
```

- [ ] **Step 4: Run to verify it passes**

Run: `pixi run pytest tests/test_modal_config.py -v`
Expected: PASS (all, incl. 3 new). If an accessor name is wrong, fix per the note in Step 1.

- [ ] **Step 5: Lint/commit**

```bash
pixi run pre-commit run --files examples/configs/modal-flashvsr-x4.yaml tests/test_modal_config.py
git add examples/configs/modal-flashvsr-x4.yaml tests/test_modal_config.py
git commit -m "feat(config): Modal FlashVSR 4x cfg (cp313 wheel, 80GB) + offline characterization"
```

---

### Task 3: Wire `HF_HOME=/cache/hf` into the Modal container env

**Goal:** Set `HF_HOME` to the Volume mount so HF-cached weights persist across Modal container starts (roadmap flag; M2 §23 follow-up). Independent of the FlashVSR path.

**Files:**
- Modify: `src/kinoforge/providers/modal/__init__.py` (`create_instance`, ~line 100)
- Test: `tests/providers/modal/test_hf_home_env.py` (create)

**Acceptance Criteria:**
- [ ] `ModalAppRequest.env` carries `HF_HOME` equal to the resolved `volume_mount` (`/cache/hf` by default).
- [ ] An operator-supplied `spec.env["HF_HOME"]` is NOT overridden.

**Verify:** `pixi run pytest tests/providers/modal/test_hf_home_env.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/modal/test_hf_home_env.py
"""Behavior: ModalProvider wires HF_HOME to the Volume mount for weight caching."""

from kinoforge.core.interfaces import HardwareRequirements, InstanceSpec, Lifecycle, Offer
from kinoforge.providers.modal import ModalProvider


def _captured_request():
    captured = {}

    def fake_factory(req, _modal_mod):
        captured["req"] = req
        return ("app", "server_fn")

    provider = ModalProvider(
        app_factory=fake_factory,
        deployer=lambda _app, _fn: "https://x--y.modal.run",
        clock=lambda: 0.0,
    )
    spec = InstanceSpec(
        image="python:3.13-slim",
        run_id="hf-home-test",
        run_cmd=["python", "-m", "server"],
        provision_script="echo provision",
        lifecycle=Lifecycle(
            idle_timeout_s=1800, job_timeout_s=900, time_buffer_s=180,
            max_lifetime_s=5400, budget_usd=2.0, boot_timeout_s=2700,
        ),
        offer=Offer(
            provider="modal", gpu_type="A100-80GB", vram_gb=80,
            cost_rate_usd_per_hr=2.5, region="us",
        ),
    )
    return provider, spec, captured


def test_hf_home_defaults_to_volume_mount():
    provider, spec, captured = _captured_request()
    provider.create_instance(spec)
    assert captured["req"].env["HF_HOME"] == "/cache/hf"


def test_hf_home_respects_operator_override():
    provider, spec, captured = _captured_request()
    spec = spec_with_env(spec, {"HF_HOME": "/custom/cache"})
    provider.create_instance(spec)
    assert captured["req"].env["HF_HOME"] == "/custom/cache"


def spec_with_env(spec, env):
    import dataclasses
    return dataclasses.replace(spec, env=env)
```

> Adjust `InstanceSpec`/`Offer`/`Lifecycle` kwargs to the real dataclass fields (check `src/kinoforge/core/interfaces.py`) — the constructor field names must match exactly. `ModalProvider.__init__` seam names (`app_factory`, `deployer`, `clock`) are in `src/kinoforge/providers/modal/__init__.py`.

- [ ] **Step 2: Run to verify it fails**

Run: `pixi run pytest tests/providers/modal/test_hf_home_env.py -v`
Expected: FAIL — `HF_HOME` not in `req.env`.

- [ ] **Step 3: Edit `create_instance`**

Replace the `env=dict(spec.env),` argument construction so it defaults `HF_HOME` to the resolved volume mount without clobbering an operator value:

```python
        volume_mount = spec.volume_mount or "/cache/hf"
        env = dict(spec.env)
        # Persist the HF cache onto the Modal Volume so a preempted/cold
        # container re-uses downloaded weights instead of re-fetching. The
        # server's own os.environ.setdefault("HF_HOME", ...) respects this.
        env.setdefault("HF_HOME", volume_mount)

        req = ModalAppRequest(
            run_id=spec.run_id,
            image=spec.image,
            gpu=spec.offer.gpu_type,
            provision_script=spec.provision_script,
            run_cmd=list(spec.run_cmd),
            env=env,
            volume_mount=volume_mount,
            scaledown_window_s=int(spec.lifecycle.idle_timeout_s),
            startup_timeout_s=int(spec.lifecycle.boot_timeout_s) or 1800,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `pixi run pytest tests/providers/modal/test_hf_home_env.py -v`
Expected: PASS

- [ ] **Step 5: Full modal suite + lint/type/commit**

```bash
pixi run pytest tests/providers/modal tests/test_modal_config.py -q
pixi run pre-commit run --files src/kinoforge/providers/modal/__init__.py tests/providers/modal/test_hf_home_env.py
git add src/kinoforge/providers/modal/__init__.py tests/providers/modal/test_hf_home_env.py
git commit -m "feat(modal): wire HF_HOME=/cache/hf onto the Volume for weight caching"
```

---

### Task 4: RED live-proof scaffold

**Goal:** Commit the failing (xfail) live-proof contract test BEFORE any Modal spend (durability rule).

**Files:**
- Create: `tests/live/test_modal_flashvsr_x4.py`

**Acceptance Criteria:**
- [ ] File is marked `pytest.mark.live` + `xfail`, records the exact `kinoforge upscale` CLI, and asserts a 1920×1920 mp4 + frame-QA in its reason/body.
- [ ] Committed before Task 5 runs.

**Verify:** `pixi run pytest tests/live/test_modal_flashvsr_x4.py -v` → 1 xfailed

**Steps:**

- [ ] **Step 1: Create the scaffold** (mirror `tests/live/test_modal_wan_t2v_1_3b.py`)

```python
"""LIVE Milestone 3: FlashVSR 4x (480->1920) on Modal 80GB. Driven manually via
the CLI; this file records the contract + a smoke assertion on the artifact."""

import pytest

pytestmark = pytest.mark.live

UPSCALE_CMD = (
    "pixi run -e live-modal kinoforge upscale "
    "--config examples/configs/modal-flashvsr-x4.yaml "
    '--video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4 '
    "--no-reuse"
)


@pytest.mark.xfail(
    reason="live proof driven via CLI; see PROGRESS + successful-generations §24"
)
def test_modal_flashvsr_x4_contract():
    raise AssertionError(
        "run UPSCALE_CMD live; assert 1920x1920 mp4 + frame-QA vs 480 source"
    )
```

- [ ] **Step 2: Run to confirm xfail**

Run: `pixi run pytest tests/live/test_modal_flashvsr_x4.py -v`
Expected: 1 xfailed

- [ ] **Step 3: Commit**

```bash
pixi run pre-commit run --files tests/live/test_modal_flashvsr_x4.py
git add tests/live/test_modal_flashvsr_x4.py
git commit -m "test(live): RED scaffold for Modal FlashVSR 4x (80GB) Milestone 3 proof"
```

---

### Task 5: Live FlashVSR 4x proof — run, frame-QA, teardown, log

**Goal:** Run the full 480→1920 4x upscale on Modal 80GB, frame-QA the output, verify teardown, and log §24.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation (roadmap: "each its own live smoke", "frame-QA'd", "prove one more axis"). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `successful-generations.md` (new §24)
- Modify: `PROGRESS.md` (RESUME SNAPSHOT + pointers)

**Acceptance Criteria:**
- [ ] `kinoforge upscale` completes; output mp4 is **1920×1920** (ffprobe).
- [ ] Frame-QA on ~5 extracted frames: no corruption, temporally coherent, faithful to the 480² source sibling (⚠️-flag if not clearly HQ).
- [ ] After the orchestrator exits: `kinoforge list` → no instances **and** `modal app list` shows no running app.
- [ ] `successful-generations.md` §24 written per the file's schema (new axis: FlashVSR upscale on Modal 80GB).

**Verify:** `pixi run kinoforge list` → `No running instances.` + `No instances recorded in ledger.`; `ffprobe` on the output → `1920x1920`; §24 present in `successful-generations.md`.

**Steps:**

- [ ] **Step 1: Preflight** (clean tree, creds, zero RunPod pods).

Run: `pixi run preflight` → exit 0.

- [ ] **Step 2: Run the live upscale** (`--no-reuse` → pod auto-destroys).

```bash
pixi run -e live-modal kinoforge upscale \
  --config examples/configs/modal-flashvsr-x4.yaml \
  --video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4 \
  --no-reuse
```

Monitor per CLAUDE.md: Modal has no util probe → watch `modal app list` + orchestrator log. Abort if no 80GB container starts within a couple minutes or boot stalls past 45m.

- [ ] **Step 3: Frame-QA the output** (mandatory before "green").

```python
from kinoforge.core.frames import ffmpeg_frames_by_count
# extract ~5 frames from the 1920² output; read them as a contact sheet and
# judge corruption / coherence / fidelity vs the 480² source.
```

Extract frames from BOTH the output and the source; eyeball side-by-side. Record the verdict.

- [ ] **Step 4: Verify teardown.**

```bash
pixi run kinoforge list       # expect: no instances + empty ledger
modal app list                # expect: no running kinoforge-* app
```

If any pod/app remains: `pixi run kinoforge destroy --id <id>` / `modal app stop <name> --yes`.

- [ ] **Step 5: Log §24 + update PROGRESS, then commit.**

Add a `successful-generations.md` §24 section per that file's preamble schema (provider=modal, engine=diffusers, model=flashvsr-wan21, mode=upscale, the cp313-wheel recipe, ffprobe dims, cost, frame-QA verdict). Update `PROGRESS.md` RESUME SNAPSHOT (M3 live-green) + the roadmap pointer (M4 RIFE remains). Mark the live scaffold xfail as covered (leave xfail; the proof is the doc).

```bash
git add successful-generations.md PROGRESS.md
git commit -m "docs(gen): Modal Milestone 3 — FlashVSR 4x 480->1920 on 80GB live-green + frame-QA"
```

---

## Self-Review

**Spec coverage:** Component 1 (cp313 wheel) → Tasks 0+1. Component 2 (Modal cfg) → Task 2. Component 3 (HF_HOME) → Task 3. Testing (offline char, build-tool, HF_HOME unit, RED live scaffold) → Tasks 0/2/3/4. Live-run protocol → Task 5. All spec sections covered.

**Placeholder scan:** The one deliberate placeholder is the `bsa_wheel_url` in the Task-2 cfg (`<<< REPLACE ... >>>`) — unavoidable because the exact URL is produced by the live Task 1; Step 3 flags it explicitly and the default written value is the expected canonical URL. No other TBD/TODO.

**Type consistency:** `bsa_wheel_url` / `weights_bundle` / `tile_size` / `upscale_only` match `FlashVSREngineConfig` + the diffusers block. `ModalAppRequest.env` + `volume_mount` match `_app.py`. Provider seam names (`app_factory`, `deployer`, `clock`) flagged for verification against the real `__init__` in Task 3's note. Live CLI + fixture filename identical across Tasks 4 and 5.
