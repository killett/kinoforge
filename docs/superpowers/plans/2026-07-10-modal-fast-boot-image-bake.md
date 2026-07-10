# Modal Fast-Boot via Image Bake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bake the slow diffusers provision steps (pip deps, BSA wheel, model weights) into the Modal image at build time so container boot is seconds — closing the ~15 min preemption window that killed the 2026-07-09 FlashVSR live run — then re-run Modal M3 Task 5 to green.

**Architecture:** Split `DiffusersEngine.render_provision` into a `build_script` (bakeable: pip + composed upscaler/interpolator install) and a `runtime_script` (fast: log surface, trap, embed, exec server), while keeping the combined `script` byte-identical for RunPod. Thread `build_script` through `InstanceSpec.image_build_script` + `runtime_script` through `InstanceSpec.runtime_provision_script`; `ModalProvider`/`build_modal_app` bake the build script into the image via `Image.run_commands(...)` and run only the runtime script at container start. RunPod is untouched.

**Tech Stack:** Python 3.13, Modal SDK (`live-modal` env), diffusers `wan_t2v_server`, FlashVSR/BSA, pytest.

**User decisions (already made):**
- Bake scope: **"Full bake (pip + BSA + weights)"** — bake torch+deps, the cp313 BSA wheel, and FlashVSR weights into the image; runtime = embed + exec (seconds). Selected 2026-07-10 via AskUserQuestion.
- RunPod behaviour must not change (Modal-only feature).
- The runtime SM80 GPU guard is dropped on the Modal-baked path (cfg pins A100-80GB/H100 = SM80+); documented, not silent.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/kinoforge/core/interfaces.py` (modify) | `RenderedProvision` += `build_script`/`runtime_script`; `InstanceSpec` += `image_build_script`/`runtime_provision_script` | 6, 8 |
| `src/kinoforge/engines/diffusers/__init__.py` (modify) | `render_provision` buckets lines into build/runtime phases | 6 |
| `src/kinoforge/upscalers/flashvsr/_engine.py` (modify) | BSA SM80 guard tolerates a no-GPU (build-time) environment | 7 |
| `src/kinoforge/core/orchestrator.py` (modify) | Populate the two new `InstanceSpec` fields from the rendered split | 8 |
| `src/kinoforge/providers/modal/__init__.py` + `_app.py` (modify) | Bake `image_build_script` into the image; boot with runtime script only | 9 |
| `tests/**` (create/modify) | Characterization + unit tests per task | 6-9 |
| `successful-generations.md`, `PROGRESS.md` (modify) | Log §24 after the live proof | 10 |

Ordering: **6 → 7 → 8 → 9 → 10 (live)**. Task 7 is independent of 6/8 (different file) and can be done in parallel, but must land before 10.

---

### Task 6: Split render_provision into build/runtime phases

**Goal:** `render_provision` returns `build_script` (pip + composed upscaler/interpolator install) and `runtime_script` (everything else) while `script` stays byte-identical to today.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (`RenderedProvision` dataclass, ~line 109-129)
- Modify: `src/kinoforge/engines/diffusers/__init__.py` (`render_provision`, lines 950-1100)
- Test: `tests/engines/diffusers/test_render_provision_split.py` (create)

**Acceptance Criteria:**
- [ ] `RenderedProvision` has `build_script: str = ""` and `runtime_script: str = ""`.
- [ ] For the Modal FlashVSR cfg: `build_script` contains the `pip install` line, the BSA-wheel `curl`+`pip install --no-deps` lines, and the FlashVSR weights fetch; `build_script` contains NEITHER the `server_cmd` line NOR `sleep infinity` NOR the `http.server 8001` sidecar.
- [ ] `runtime_script` contains the `server_cmd` line, the embed lines, the `exec > /tmp/bootstrap.log` redirect, and NONE of the pip/BSA/weights lines.
- [ ] `render_provision(cfg).script` is unchanged (golden): equals the pre-split output for both a Wan t2v cfg and the FlashVSR cfg.

**Verify:** `pixi run pytest tests/engines/diffusers/test_render_provision_split.py -v` → PASS

**Steps:**

- [ ] **Step 1: Capture the golden `script` BEFORE refactoring** (so the byte-identical test is real).

Run and save the current combined script for a Wan + FlashVSR cfg:
```bash
pixi run python -c "
from kinoforge.core.config import load_config
from kinoforge.engines.diffusers import DiffusersEngine
import json, pathlib
out={}
for name in ['examples/configs/modal-wan-t2v-1_3b.yaml','examples/configs/modal-flashvsr-x4.yaml']:
    cfg=load_config(name).model_dump()
    out[name]=DiffusersEngine().render_provision(cfg).script
pathlib.Path('tests/engines/diffusers/_golden_provision.json').write_text(json.dumps(out,indent=0))
print('captured', {k:len(v) for k,v in out.items()})
"
```
(If `render_provision` takes the raw cfg dict vs a model — check the existing call in `orchestrator.py`; mirror that shape. `load_config(...).model_dump()` gives the dict the engine reads.)

- [ ] **Step 2: Write the failing test** `tests/engines/diffusers/test_render_provision_split.py`:

```python
"""Behavior: render_provision splits into build/runtime phases; script unchanged."""

import json
from pathlib import Path

from kinoforge.core.config import load_config
from kinoforge.engines.diffusers import DiffusersEngine

_GOLDEN = json.loads(Path("tests/engines/diffusers/_golden_provision.json").read_text())
_FLASHVSR = "examples/configs/modal-flashvsr-x4.yaml"
_WAN = "examples/configs/modal-wan-t2v-1_3b.yaml"


def _render(path):
    return DiffusersEngine().render_provision(load_config(path).model_dump())


def test_script_is_byte_identical_to_golden():
    # Bug caught: a careless refactor reorders lines -> RunPod boot script drifts.
    for path, golden in _GOLDEN.items():
        assert _render(path).script == golden, f"{path} script drifted"


def test_flashvsr_build_script_has_installs_not_runtime():
    rp = _render(_FLASHVSR)
    b = rp.build_script
    assert "pip install" in b
    assert "block_sparse_attn" in b  # BSA wheel curl+install
    assert "FlashVSR" in b or "flashvsr" in b  # weights fetch
    # NOT the runtime-only bits:
    assert "sleep infinity" not in b
    assert "http.server 8001" not in b
    assert "kinoforge.engines.diffusers.servers.wan_t2v_server" not in b.split(
        "# ---- upscaler"
    )[0].split("pip install")[0]  # server exec not in build


def test_flashvsr_runtime_script_has_server_not_installs():
    rp = _render(_FLASHVSR)
    r = rp.runtime_script
    assert "wan_t2v_server" in r  # the server_cmd line
    assert "/tmp/bootstrap.log" in r  # runtime log redirect
    assert "block_sparse_attn" not in r  # BSA is baked, not runtime
    # the heavy pip line must be gone from runtime:
    assert "torch==2.6.0" not in r
```

- [ ] **Step 3: Run — confirm it fails** (`build_script`/`runtime_script` don't exist yet).

Run: `pixi run pytest tests/engines/diffusers/test_render_provision_split.py -v` → FAIL (AttributeError / empty).

- [ ] **Step 4: Add fields to `RenderedProvision`** (`interfaces.py` after `env_required`):

```python
    build_script: str = ""
    runtime_script: str = ""
```
Update the docstring: `build_script` = bakeable install steps (pip, composed upscaler/interpolator install), safe to run at image-build; `runtime_script` = container-start steps (log surface, trap, embed, exec server); `script` = the two joined, for providers that provision at runtime (RunPod).

- [ ] **Step 5: Refactor `render_provision`** to bucket each appended segment. Replace the single `lines: list[str]` accumulation with a phase-tagged helper. At the top of the method body (where `lines: list[str] = [...]` is initialised):

```python
        build_lines: list[str] = []
        runtime_lines: list[str] = []

        def _add(phase: str, *new: str) -> None:
            """Append line(s) to the combined stream AND the phase bucket."""
            for ln in new:
                lines.append(ln)
                (build_lines if phase == "build" else runtime_lines).append(ln)
```

Keep the existing `lines: list[str] = [ ...preamble... ]` initial value, but ALSO seed `runtime_lines` with that same preamble (it is all runtime). Simplest: build the preamble list once, assign to both:
```python
        _preamble = [ ...the existing set -e / exec log / trap / sidecar / selfterm list... ]
        lines: list[str] = list(_preamble)
        runtime_lines: list[str] = list(_preamble)
        build_lines: list[str] = []
```
Then convert each subsequent `lines.append(x)` / `lines.extend(...)` to `_add(<phase>, ...)`:
- embed block → `_add("runtime", *embed_lines)` (the `_render_embed_lines` / single-file + the PYTHONPATH export)
- pip install line → `_add("build", pip_line)`
- `export KINOFORGE_SKIP_WAN_LOAD=1` → `_add("runtime", ...)`
- upscaler composed lines (the `# ---- upscaler` comment + `upscale_rp.script` non-empty lines) → `_add("build", ...)`
- interpolator composed lines → `_add("build", ...)`
- `export WAN_MODEL_ID=...` → `_add("runtime", ...)`
- `server_cmd` join line → `_add("runtime", ...)`

- [ ] **Step 6: Build the two scripts in the return.** Replace the `return RenderedProvision(script="\n".join(lines), ...)` with:

```python
        build_script = ""
        if build_lines:
            # build_lines run at image-build (Modal) — give them their own
            # fail-fast preamble (the combined script's set -e lives in runtime).
            build_script = "set -euo pipefail\n" + "\n".join(build_lines)
        runtime_script = "\n".join(runtime_lines)
        return RenderedProvision(
            script="\n".join(lines),
            build_script=build_script,
            runtime_script=runtime_script,
            run_cmd=server_cmd,
            image=image,
            ports=ports,
            env_required=["HF_TOKEN"],
        )
```

- [ ] **Step 7: Run tests** → PASS (golden byte-identical + split assertions).

Run: `pixi run pytest tests/engines/diffusers/test_render_provision_split.py -v`
Also run the broader engine suite to catch regressions: `pixi run pytest tests/engines/diffusers -q`

- [ ] **Step 8: Commit** (stage the golden fixture too):

```bash
pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/engines/diffusers/__init__.py tests/engines/diffusers/test_render_provision_split.py tests/engines/diffusers/_golden_provision.json
git add src/kinoforge/core/interfaces.py src/kinoforge/engines/diffusers/__init__.py tests/engines/diffusers/test_render_provision_split.py tests/engines/diffusers/_golden_provision.json
git commit -m "feat(diffusers): split render_provision into build/runtime phases (script byte-identical)"
```

---

### Task 7: Make the FlashVSR BSA guard tolerate a no-GPU build env

**Goal:** The BSA install's SM80 guard no-ops when no CUDA device is present (image-build time) instead of `exit 87`, so `build_script` bakes on Modal's CPU image builder; the guard still enforces SM80+ when a GPU IS present (RunPod runtime).

**Files:**
- Modify: `src/kinoforge/upscalers/flashvsr/_engine.py` (the guard line, ~line 106-109)
- Test: `tests/upscalers/flashvsr/test_engine.py` (add a case) or the existing provision test

**Acceptance Criteria:**
- [ ] The rendered BSA provision guard passes (exit 0) when `torch.cuda.is_available()` is False, and still asserts `>= 8` when a device is present.
- [ ] The `curl` + `pip install --no-deps <wheel>` lines are unchanged.

**Verify:** `pixi run pytest tests/upscalers/flashvsr/test_engine.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing test** (add to `tests/upscalers/flashvsr/test_engine.py`):

```python
def test_bsa_guard_tolerates_no_gpu_build_env():
    # Bug caught: the SM80 guard calls torch.cuda.get_device_capability()
    # unconditionally -> RuntimeError -> exit 87 on Modal's CPU image builder,
    # so build_script cannot bake. It must no-op when no CUDA device exists.
    from kinoforge.upscalers.flashvsr._engine import FlashVSREngine  # adjust import
    cfg = {
        "upscale": {"engine": "flashvsr", "scale": "4x",
                    "flashvsr": {"weights_bundle": "hf:JunhaoZhuang/FlashVSR-v1.1"}}
    }
    script = FlashVSREngine().render_provision(cfg).script
    assert "torch.cuda.is_available()" in script
    assert "get_device_capability" in script
```
(Check the real class/entry name in `_engine.py` — it exposes `render_provision`. Mirror an existing test in that file for cfg shape.)

- [ ] **Step 2: Run — confirm it fails** (guard has no `is_available()` check yet).

- [ ] **Step 3: Edit the guard** in `_engine.py`. Replace:

```python
                'python -c "import torch; '
                "assert torch.cuda.get_device_capability()[0] >= 8, "
                "f'flashvsr: BSA needs SM80+, got {torch.cuda.get_device_capability()}'"
                '" || exit 87\n',
```
with a version that skips the check when no CUDA device is present (image-build time), and still enforces it at runtime:
```python
                # SM80+ guard. At image-BUILD time (Modal CPU builder) there is
                # no CUDA device -> is_available() is False -> the guard no-ops so
                # the wheel can still bake. At runtime (GPU present) it enforces
                # SM80+. The Modal cfg pins A100-80GB/H100 (SM80/SM90), so the
                # baked path's dropped runtime check is belt-and-suspenders only.
                'python -c "import torch; '
                "cap = torch.cuda.get_device_capability() "
                "if torch.cuda.is_available() else None; "
                "assert cap is None or cap[0] >= 8, "
                "f'flashvsr: BSA needs SM80+, got {cap}'"
                '" || exit 87\n',
```

- [ ] **Step 4: Run tests** → PASS. Also `pixi run pytest tests/upscalers/flashvsr -q`.

- [ ] **Step 5: Commit:**
```bash
pixi run pre-commit run --files src/kinoforge/upscalers/flashvsr/_engine.py tests/upscalers/flashvsr/test_engine.py
git add src/kinoforge/upscalers/flashvsr/_engine.py tests/upscalers/flashvsr/test_engine.py
git commit -m "fix(flashvsr): BSA SM80 guard no-ops on no-GPU build env (bakeable on Modal)"
```

---

### Task 8: Thread the split through InstanceSpec + the orchestrator

**Goal:** `InstanceSpec` carries `image_build_script` + `runtime_provision_script`; the orchestrator populates them from the rendered split. RunPod ignores both (uses `provision_script`).

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (`InstanceSpec`, ~line 138-167)
- Modify: `src/kinoforge/core/orchestrator.py` (the `InstanceSpec(...)` construction, ~line 776)
- Test: `tests/core/test_orchestrator_provision_threading.py` (create) or extend an existing orchestrator test

**Acceptance Criteria:**
- [ ] `InstanceSpec` has `image_build_script: str | None = None` and `runtime_provision_script: str | None = None`.
- [ ] After the orchestrator builds the spec from a FlashVSR cfg, `spec.image_build_script` equals `rendered.build_script` and `spec.runtime_provision_script` equals `rendered.runtime_script`; `spec.provision_script` still equals `rendered.script`.

**Verify:** `pixi run pytest tests/core/test_orchestrator_provision_threading.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing test.** Find the orchestrator seam that renders provision + builds the spec (grep `render_provision(` and `InstanceSpec(` in `orchestrator.py`). Mirror an existing orchestrator unit test's setup (fake engine/provider). Assert the three fields on the constructed spec:
```python
def test_spec_carries_build_and_runtime_scripts(...):
    # Bug caught: without threading, ModalProvider can't bake -> the whole
    # fast-boot feature is inert and FlashVSR keeps preempting on long boots.
    spec = build_spec_from_cfg("examples/configs/modal-flashvsr-x4.yaml")  # via the orch seam
    assert spec.image_build_script and "pip install" in spec.image_build_script
    assert spec.runtime_provision_script and "wan_t2v_server" in spec.runtime_provision_script
    assert spec.provision_script == rendered_script  # unchanged combined
```
(Use the same construction path the orchestrator uses; if there's no clean seam, assert via the real orchestrator method with injected provider that captures the spec — mirror `tests/core/test_orchestrator*.py`.)

- [ ] **Step 2: Run — confirm it fails** (fields don't exist).

- [ ] **Step 3: Add the fields to `InstanceSpec`** (after `provision_script`):
```python
    # Modal fast-boot (2026-07-10): the engine's bakeable install steps and its
    # runtime-only steps, split out of `provision_script`. RunPod ignores both
    # and uses the combined `provision_script`; Modal bakes image_build_script
    # into the image and boots with runtime_provision_script.
    image_build_script: str | None = None
    runtime_provision_script: str | None = None
```

- [ ] **Step 4: Populate them in the orchestrator** where `InstanceSpec(...)` is built from `rendered`. Add the two kwargs (guard empties to None):
```python
        image_build_script=(rendered.build_script or None),
        runtime_provision_script=(rendered.runtime_script or None),
```

- [ ] **Step 5: Run tests** → PASS. Broader: `pixi run pytest tests/core -q`.

- [ ] **Step 6: Commit:**
```bash
pixi run pre-commit run --files src/kinoforge/core/interfaces.py src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_provision_threading.py
git add src/kinoforge/core/interfaces.py src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_provision_threading.py
git commit -m "feat(core): thread build/runtime provision split onto InstanceSpec (Modal fast-boot)"
```

---

### Task 9: ModalProvider bakes the build script into the image

**Goal:** `build_modal_app` runs `image_build_script` at image-build via `Image.run_commands(...)`; the container boot payload uses ONLY the runtime script (so no re-download at container start).

**Files:**
- Modify: `src/kinoforge/providers/modal/_app.py` (`ModalAppRequest` + `build_modal_app`, lines 26-115)
- Modify: `src/kinoforge/providers/modal/__init__.py` (`create_instance`, ~line 100-116)
- Test: `tests/providers/modal/test_image_bake.py` (create)

**Acceptance Criteria:**
- [ ] `ModalAppRequest` has `image_build_script: str | None = None`.
- [ ] When `image_build_script` is set, `build_modal_app` calls `image.run_commands(image_build_script)` on the `from_registry` image before `App(image=...)`.
- [ ] The web-server boot payload (`_boot_payload`) is built from the RUNTIME provision script (via `create_instance` passing `runtime_provision_script or provision_script`), not the combined one — assert the pip/BSA lines are ABSENT from the payload and the server exec IS present.

**Verify:** `pixi run pytest tests/providers/modal/test_image_bake.py -v` → PASS

**Steps:**

- [ ] **Step 1: Write the failing test** with a fake `modal_mod` capturing calls (mirror the existing `tests/providers/modal` fake-modal pattern). Assert:
```python
def test_build_modal_app_bakes_build_script():
    # Bug caught: without run_commands(build_script), the image lacks torch/BSA/
    # weights and the container re-downloads at boot -> the ~15min preemption
    # window that killed the 2026-07-09 FlashVSR live run stays open.
    calls = []
    fake_image = _FakeImage(calls)      # .from_registry/.run_commands record to calls
    fake_modal = _FakeModal(fake_image)
    req = ModalAppRequest(run_id="t", image="python:3.13-slim", gpu="A100-80GB",
                          provision_script="exec server\n", run_cmd=["python","-m","s"],
                          image_build_script="set -e\npip install torch==2.6.0\n")
    build_modal_app(req, fake_modal)
    assert any("run_commands" == c[0] and "pip install" in c[1] for c in calls)


def test_boot_payload_uses_runtime_script_only():
    # create_instance must pass the runtime script (no pip/BSA) as the boot payload.
    captured = {}
    provider = ModalProvider(app_factory=lambda r, m: (captured.setdefault("req", r), ("A","S"))[1],
                             deployer=lambda a, s: "https://x.modal.run", clock=lambda: 0.0)
    spec = _spec(provision_script="set -e\npip install torch\nexec server\n",
                 runtime_provision_script="set -e\nexec server\n",
                 image_build_script="set -e\npip install torch\n")
    provider.create_instance(spec)
    payload = captured["req"].provision_script
    assert "pip install" not in payload and "exec server" in payload
    assert captured["req"].image_build_script and "pip install" in captured["req"].image_build_script
```
(Adjust `_FakeImage`/`_FakeModal`/`_spec` to the real fakes in `tests/providers/modal/` — reuse them; `InstanceSpec`/`Offer` fields per `interfaces.py`.)

- [ ] **Step 2: Run — confirm it fails.**

- [ ] **Step 3: Add the field to `ModalAppRequest`** (`_app.py`, after `add_python`):
```python
    image_build_script: str | None = None
```

- [ ] **Step 4: Bake it in `build_modal_app`** — after `image = modal_mod.Image.from_registry(...)`:
```python
    image = modal_mod.Image.from_registry(req.image, add_python=req.add_python)
    if req.image_build_script:
        # Bake the slow install steps (pip/BSA/weights) into the image so
        # container boot is seconds — no ~15min runtime provision for Modal to
        # preempt (2026-07-09 FlashVSR failure). run_commands streams to the
        # build log, so a bad wheel/weights fetch surfaces at build, not as a
        # silent boot hang.
        image = image.run_commands(req.image_build_script)
```
(`run_commands` accepts a single multi-line string; if the real API needs a list, pass `[req.image_build_script]` — verify against the modal 1.5.1 signature used in `tools/build_bsa_wheel_modal.py`, which chains `.run_commands("cmd", "cmd", ...)`. A single string arg is valid.)

- [ ] **Step 5: Wire `create_instance`** (`__init__.py`) to pass the build script AND use the runtime script for the boot payload:
```python
        volume_mount = spec.volume_mount or "/cache/hf"
        env = dict(spec.env)
        env.setdefault("HF_HOME", volume_mount)
        # Modal boots with the RUNTIME script only — the build script is baked
        # into the image below, so re-running it at container start would
        # re-download everything and re-open the preemption window.
        boot_script = spec.runtime_provision_script or spec.provision_script
        req = ModalAppRequest(
            run_id=spec.run_id,
            image=spec.image,
            gpu=spec.offer.gpu_type,
            provision_script=boot_script,
            run_cmd=list(spec.run_cmd),
            env=env,
            volume_mount=volume_mount,
            scaledown_window_s=int(spec.lifecycle.idle_timeout_s),
            startup_timeout_s=int(spec.lifecycle.boot_timeout_s) or 1800,
            image_build_script=spec.image_build_script,
        )
```
(Keep the existing validation that `spec.run_cmd`/`spec.provision_script` are set — `provision_script` remains required as the fallback.)

- [ ] **Step 6: Run tests** → PASS. Broader: `pixi run pytest tests/providers/modal tests/test_modal_config.py -q`.

- [ ] **Step 7: Commit:**
```bash
pixi run pre-commit run --files src/kinoforge/providers/modal/_app.py src/kinoforge/providers/modal/__init__.py tests/providers/modal/test_image_bake.py
git add src/kinoforge/providers/modal/_app.py src/kinoforge/providers/modal/__init__.py tests/providers/modal/test_image_bake.py
git commit -m "feat(modal): bake build_script into the image; boot with runtime script (fast boot)"
```

---

### Task 10: Live FlashVSR 4x proof on Modal (re-run of M3 Task 5)

**Goal:** Re-run the FlashVSR upscale on Modal 80GB with fast boot; the container binds quickly (no preemption loop), produces a 1920×1920 clip; frame-QA vs source; teardown clean; log §24.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation (unblock the M3 live proof). It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `successful-generations.md` (new §24), `PROGRESS.md` (RESUME SNAPSHOT)

**Acceptance Criteria:**
- [ ] Container `/health` binds within a few minutes (fast boot confirmed) — NOT a ~15 min preemption loop.
- [ ] `kinoforge upscale` completes; output mp4 is **1920×1920** (ffprobe).
- [ ] Frame-QA on ~5 frames: no corruption, temporally coherent, faithful to the 480² source sibling (⚠️-flag if not clearly HQ).
- [ ] After orchestrator exit: `kinoforge list` → no instances AND `modal app list` → no running kinoforge app.
- [ ] `successful-generations.md` §24 written (FlashVSR upscale on Modal 80GB, cp313-wheel + image-bake recipe).

**Verify:** `pixi run kinoforge list` → No running instances + empty ledger; `ffprobe` output → `1920x1920`; §24 present.

**Steps:**

- [ ] **Step 1: Preflight** — `pixi run preflight` → PASS.
- [ ] **Step 2: Run the live upscale** (fixture 480²/81f, `--no-reuse`):
```bash
pixi run -e live-modal kinoforge upscale \
  --config examples/configs/modal-flashvsr-x4.yaml \
  --video output/20260630-221857_diffusers_Wan2.2-T2V-A14B-Diffuser_Photorealistic-cinem.mp4 \
  --no-reuse
```
The FIRST run triggers the Modal image build (bakes torch+BSA+weights — streams to the deploy log; watch it). Subsequent boots reuse the cached image → seconds.
- [ ] **Step 3: Monitor** — the image build streams; once deployed, `/health` should bind fast. If the build errors (bad wheel/weights), it surfaces in the deploy log (unlike the old silent hang). Watch `modal app list` + orchestrator log.
- [ ] **Step 4: Frame-QA** the 1920² output vs the 480² source via `kinoforge.core.frames.ffmpeg_frames_by_count`; record the verdict.
- [ ] **Step 5: Verify teardown** — `pixi run kinoforge list` (+ `modal app list`); destroy/stop any leftover.
- [ ] **Step 6: Log §24 + update PROGRESS; commit:**
```bash
git add successful-generations.md PROGRESS.md
git commit -m "docs(gen): Modal M3 FlashVSR 4x 480->1920 live-green (image-bake fast boot) + frame-QA"
```

---

## Self-Review

**Spec coverage:** Split (spec §"The split") → Task 6. Build-env GPU-guard (spec §Modal build specifics, "no token"/CPU-build) → Task 7. Threading (spec §Data flow) → Task 8. Modal image bake + runtime-only boot (spec §Data flow item 4) → Task 9. Backward-compat byte-identical `script` (spec §Backward-compatibility) → Task 6 golden test. Live re-run (spec §Testing "Live re-run") → Task 10. All covered.

**Placeholder scan:** No TBD/TODO. Test bodies reference real fakes that the implementer must match to `tests/providers/modal/` (flagged inline) — not placeholders but adaptation notes.

**Type consistency:** `build_script`/`runtime_script` (RenderedProvision) → `image_build_script`/`runtime_provision_script` (InstanceSpec) → `image_build_script` (ModalAppRequest) + `boot_script` local. Names consistent across Tasks 6/8/9. `run_commands` usage matches `tools/build_bsa_wheel_modal.py`.
