# MiniMax-H3 Sub-project C — t2va server and config

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One `kinoforge generate --mode t2va` on Modal H200 produces a 24 fps MiniMax-H3
clip whose MP4 carries a verified stereo soundtrack, frame-QA'd and audio-QA'd, pod torn
down and verified from a fresh process.

**Architecture:** A new `servers/minimax_h3_server.py` (sibling of `wan_t2v_server.py`, not
an extension) serves the existing DiffusersBackend HTTP contract and writes its output
through the already-committed `servers/_av_io.write_mp4_with_audio`. The diffusers engine is
config-driven, so no engine fork is needed — except for one genuinely new seam: the engine's
capability probe is a module constant declaring `supported_modes={"t2v"}`, so t2va needs a
per-config capability declaration rather than a global widening.

**Tech Stack:** diffusers `0.40.0` `ModularPipeline` + `ComponentsManager`, transformers
Qwen3-VL, torch 2.6.0+cu124, FastAPI/uvicorn, Modal serverless H200, HF weights pre-resident
on the `kinoforge-hf-cache` Volume (Sub-project B).

**Spec:** `docs/superpowers/specs/2026-09-17-minimax-h3-t2va-design.md`

## Global Constraints

Five from the spec handoff, plus three found by reading the v0.40.0 source while writing
this plan. Each is a live failure if missed.

1. **`workflow="t2va"` is MANDATORY on `ModularPipeline.from_pretrained`.** Without it
   `load_components` pulls **both** 61.7 GiB transformer partitions. `transformer_ref/` was
   deliberately NOT prefetched, so the miss costs a 66 GB download **on the H200 at
   $4.54/hr**. Confirmed verbatim in `MiniMaxH3Blocks.description` at v0.40.0. This is the
   single most expensive mistake available in this sub-project, and Task 3 freezes it with a
   test that asserts the kwarg reaches `from_pretrained`.
2. **`max_usd_per_hr >= 4.54`.** Modal offers are `mode="serverless"`, so `filter_offers`
   skips the ceiling at selection time (`ModalProvider.capability_matrix` marks
   `max_usd_per_hr` UNSUPPORTED) — but `_enforce_rate_cap` still destroys the instance
   *after* launch. An under-capped config pays for a launch it cannot keep.
3. **CPU offload is mandatory, but `enable_model_cpu_offload` DOES NOT EXIST on
   `ModularPipeline`.** *Spec correction, found 2026-09-17 while writing this plan.*
   `ModularPipeline` subclasses `ConfigMixin, PushToHubMixin` — not `DiffusionPipeline` — and
   defines no `enable_*_cpu_offload` method at all; `grep -n 'def enable_'` over
   `modular_pipelines/modular_pipeline.py@v0.40.0` returns nothing. Calling it would be an
   `AttributeError` on a booked H200. The real mechanism, and the one the diffusers H3 doc
   gives as *the* single-card recipe, is:
   ```py
   manager = ComponentsManager()
   pipe = ModularPipeline.from_pretrained(repo, workflow="t2va", components_manager=manager)
   pipe.load_components(dtype=torch.bfloat16)
   manager.enable_auto_cpu_offload(device="cuda", memory_reserve_margin="12GB")
   ```
   Weights live in host RAM; the manager moves onto the accelerator only what each block
   needs and evicts to make room.
4. **Pin `diffusers==0.40.0`.** 0.39.0 and 0.38.0 do not export `MiniMaxH3ModularPipeline`.
   Far above the `diffusers>=0.32` the Wan configs use, so H3 pins its own and must not
   perturb the Wan images.
5. **No `guidance_scale`, no `negative_prompt`.** The checkpoint is guidance-distilled: no
   guider, one forward pass per step. A request schema offering them offers what the model
   cannot use, so the schema must `extra="forbid"` them rather than accept-and-ignore.
6. **Geometry is a hard contract, checked on the GPU.** *Read from
   `minimax_h3/before_denoise.py@v0.40.0`.* `height` and `width` must be multiples of **32**
   (`canvas_multiple` = `vae_spatial_compression_ratio` 16 × `patch_size[2]` 2), and
   `num_frames` is snapped up to the next `17 * n + 5` whose duration lands in
   **5.0–15.0 s at the fixed 24 fps** — i.e. 120–360 requested frames, default **124**.
   Every one of those raises `ValueError` inside the pipeline call, which on a booked H200 is
   a job error minutes after boot. The request schema must reject them at the HTTP edge.
7. **Widening the engine's capability probe globally would break every cached Wan
   profile.** `DiffusersEngine._DEFAULT_PROBE` declares `supported_modes={"t2v"}` and is
   shared by every diffusers config. `JsonProfileCache.verify` compares `supported_modes`
   against the live probe and raises `CapabilityMismatch`, which tears the instance down —
   and this workspace's `.kinoforge/_profiles/` already holds **10** cached `diffusers`
   profiles with `["t2v"]`. So t2va must arrive as a per-config declaration. Task 2's second
   test is the no-regression guard.
8. **`supports_joint_audio=True` is a declaration, not a mechanism.** `core/strategy.py:55`
   writes an `_audio_mode` marker that **nothing reads** — grep-confirmed. Operator decision
   2026-09-17: leave it inert and say so in the code, at both sites. No test may assert
   behaviour through `_audio_mode`; audio is tested at `_av_io` and at the QA arm, where it
   is real.

**User decisions (already made):**
- Engine route is **diffusers**, not ComfyUI (spec "RESOLVED 2026-09-17").
- **t2va only** for H3 — no FlashVSR, no RIFE: H3 is natively 24 fps and interpolation would
  desync the joint audio.
- **H200 bf16**, not A100 offload or INT8.
- Leave the `_audio_mode` seam **inert and documented**.
- Live spend pre-authorised up to the $20 session budget; ~$0.10 spent. **No confirmation
  handshakes** — run it.
- Every video-gen live smoke reads its prompt **verbatim** from
  `examples/configs/prompts/field-realistic.txt`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/kinoforge/core/interfaces.py` | add `MODE_ROLE_REQUIREMENTS["t2va"] = {}` |
| `src/kinoforge/core/strategy.py` | the inert-`_audio_mode` comment (no behaviour change) |
| `src/kinoforge/core/config.py` | new `DiffusersCapabilityConfig` + `DiffusersEngineConfig.capability` |
| `src/kinoforge/engines/diffusers/__init__.py` | `backend()` applies the cfg capability block over `_DEFAULT_PROBE` |
| `src/kinoforge/engines/diffusers/servers/minimax_h3_server.py` | **new** — the H3 t2va inference server |
| `examples/configs/modal-diffusers-minimax-h3-t2va.yaml` | **new** — the shipped config |
| `tests/providers/golden/launch_payloads/modal-diffusers-minimax-h3-t2va.json` | **new** — golden ratchet entry |
| `tests/core/test_mode_t2va.py` | **new** — the mode/role contract for t2va |
| `tests/engines/test_diffusers_capability_cfg.py` | **new** — cfg-declared profile + the Wan no-regression guard |
| `tests/servers/test_minimax_h3_server.py` | **new** — the server's contract, offline |
| `tests/providers/modal/test_h200_rate_cap.py` | **new** — freezes Global Constraint 2 |

`servers/_av_io.py` and `servers/_util_stats.py` are reused unchanged. `servers/_video_io.py`
is **not touched**, so the Wan / FlashVSR / RIFE paths cannot regress.

---

### Task 1: t2va mode + the inert-seam comments

**Goal:** `t2va` is a first-class mode in the role contract, and both `_audio_mode` sites say
in the code that the strategy marker is not the audio mechanism.

**Files:**
- Modify: `src/kinoforge/core/interfaces.py` (`MODE_ROLE_REQUIREMENTS`, ~line 799)
- Modify: `src/kinoforge/core/strategy.py:55`
- Test: `tests/core/test_mode_t2va.py` (create)

**Acceptance Criteria:**
- [ ] `MODE_ROLE_REQUIREMENTS["t2va"] == {}`
- [ ] `validate_request` accepts a `mode="t2va"` request with no assets against a profile
      declaring t2va, and returns it unchanged
- [ ] `validate_request` still rejects `mode="t2va"` against a profile that does not declare
      it, with the `not in supported_modes` message
- [ ] `core/strategy.py:55` carries a comment recording that `_audio_mode` is written and
      read nowhere, that this is deliberate as of 2026-09-17, and that H3 is the first model
      for which the value is not the constant `"separate"`
- [ ] No test asserts behaviour through `_audio_mode`

**Verify:** `pixi run python -m pytest tests/core/test_mode_t2va.py tests/core/test_strategy.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
"""Behavior: t2va is a first-class mode in the role contract.

The mode gate (`profile.supported_modes`) and the role contract
(`MODE_ROLE_REQUIREMENTS`) are two separate checks in `validate_request`, and they
fail in different ways. A profile that declares t2va passes the first and then
KeyErrors on the second if the map has no entry — an uncaught KeyError mid-
orchestration, on a pod that is already booked and billing.
"""

from __future__ import annotations

import pytest

from kinoforge.core.errors import ValidationError
from kinoforge.core.interfaces import (
    MODE_ROLE_REQUIREMENTS,
    GenerationRequest,
    ModelProfile,
)
from kinoforge.core.validation import validate_request


def _profile(modes: set[str]) -> ModelProfile:
    return ModelProfile(
        name="minimax-h3",
        max_frames=124,
        fps=24,
        supported_modes=modes,
        max_resolution=(1344, 768),
        supports_native_extension=False,
        supports_joint_audio=True,
    )


def test_t2va_requires_no_conditioning_roles() -> None:
    """t2va takes no image roles — same shape as t2v.

    Bug caught: t2va added to a profile's supported_modes but not to
    MODE_ROLE_REQUIREMENTS. The mode gate passes, then
    `MODE_ROLE_REQUIREMENTS[request.mode]` raises KeyError — which is not a
    ValidationError, so the orchestrator's `except ValidationError` teardown does
    not fire and the pod is left running.
    """
    assert MODE_ROLE_REQUIREMENTS["t2va"] == {}


def test_validate_request_accepts_t2va_with_no_assets() -> None:
    """A text-only t2va request survives validation unchanged.

    Bug caught: as above, but through the real entry point rather than the map.
    """
    request = GenerationRequest(prompt="a fox in snow", mode="t2va")
    validated = validate_request(_profile({"t2va"}), request, accepted_kinds={"image"})
    assert validated.mode == "t2va"
    assert validated.assets == []


def test_validate_request_rejects_t2va_on_a_profile_that_does_not_declare_it() -> None:
    """The mode gate still gates.

    Bug caught: t2va is made to work by widening the role map AND loosening the
    mode gate, so every model claims joint audio. Wan configs must keep rejecting
    t2va.
    """
    request = GenerationRequest(prompt="a fox in snow", mode="t2va")
    with pytest.raises(ValidationError, match=r"not in supported_modes"):
        validate_request(_profile({"t2v"}), request, accepted_kinds={"image"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run python -m pytest tests/core/test_mode_t2va.py -v`
Expected: FAIL — `KeyError: 't2va'` on the first two tests (the third already passes,
which is the point: it proves the gate was already there and is not what is being changed).

- [ ] **Step 3: Write minimal implementation**

In `src/kinoforge/core/interfaces.py`:

```python
MODE_ROLE_REQUIREMENTS: dict[str, dict[str, str]] = {
    "t2v": {},
    # MiniMax-H3's joint video+audio mode. Empty for the same reason t2v is: the
    # `t2va` workflow's `_workflow_map` entry in diffusers is `{"prompt": True}` —
    # text only, no image roles. The audio is an OUTPUT, not a conditioning role,
    # so it has no entry here at all.
    "t2va": {},
    "i2v": {"init_image": "image"},
    "flf2v": {"first_frame": "image", "last_frame": "image"},
}
```

In `src/kinoforge/core/strategy.py`, immediately above line 55:

```python
    # INERT SEAM — deliberate, 2026-09-17. `_audio_mode` is written here and read
    # NOWHERE: grep the tree for it and this line is the only hit outside tests.
    # MiniMax-H3 is the first model for which the value is not the constant
    # "separate", and it is STILL not a mechanism — H3's audio reaches the output
    # through `engines/diffusers/servers/_av_io.write_mp4_with_audio`, on the pod,
    # never through this marker. Do not build on it without wiring a reader first;
    # an inert seam that reads as working is how U40 cost a live run.
    audio_mode = "joint" if profile.supports_joint_audio else "separate"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run python -m pytest tests/core/test_mode_t2va.py tests/core/test_strategy.py tests/core/test_interfaces.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/interfaces.py src/kinoforge/core/strategy.py
git add tests/core/test_mode_t2va.py
git commit -m "feat(core): add the t2va mode, and mark the _audio_mode seam inert"
```

---

### Task 2: per-config capability declaration for the diffusers engine

**Goal:** A diffusers config can declare its own capability profile, so the H3 config
reports `t2va` + `supports_joint_audio=True` without changing what any Wan config reports.

**Files:**
- Modify: `src/kinoforge/core/config.py` (new `DiffusersCapabilityConfig`; add
  `capability` to `DiffusersEngineConfig`, ~line 445-468)
- Modify: `src/kinoforge/engines/diffusers/__init__.py` (`backend()`, ~line 1390-1422)
- Test: `tests/engines/test_diffusers_capability_cfg.py` (create)

**Acceptance Criteria:**
- [ ] `engine.diffusers.capability` survives `load_config` (pydantic's default
      `extra="ignore"` on `DiffusersEngineConfig` would silently DROP an undeclared key)
- [ ] `DiffusersEngine.backend(instance, cfg)` with a capability block returns a backend
      whose `capabilities()` / `inspect_capabilities()` report the declared
      `supported_modes`, `fps`, `max_frames`, `max_resolution` and `supports_joint_audio`
- [ ] `DiffusersEngine.backend(instance, cfg)` with **no** capability block returns
      `_DEFAULT_PROBE` unchanged — object-equal field-for-field, including
      `supported_modes == {"t2v"}`
- [ ] Unknown keys inside the capability block are refused, not ignored

**Verify:** `pixi run python -m pytest tests/engines/test_diffusers_capability_cfg.py tests/engines/ tests/core/test_config.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
"""Behavior: a diffusers config declares its own capability profile.

`DiffusersEngine._DEFAULT_PROBE` is a module constant shared by every diffusers
config, declaring `supported_modes={"t2v"}`. MiniMax-H3 needs `t2va`. Widening the
constant would change what EVERY diffusers config reports, and
`JsonProfileCache.verify` compares `supported_modes` against the live probe and
raises `CapabilityMismatch` — which tears the instance down. This workspace already
holds 10 cached diffusers profiles with `["t2v"]`, so the global widening is a
money-costing regression on the next warm Wan run. Hence: per-config declaration.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from kinoforge.core.interfaces import Instance
from kinoforge.engines.diffusers import _DEFAULT_PROBE, DiffusersEngine

_H3_CAPABILITY = {
    "supported_modes": ["t2va"],
    "max_frames": 124,
    "fps": 24,
    "max_resolution": [1344, 768],
    "supports_joint_audio": True,
}


def _cfg(capability: dict | None) -> dict:
    diffusers_block: dict = {"base_url": "http://localhost:8000"}
    if capability is not None:
        diffusers_block["capability"] = capability
    return {"engine": {"kind": "diffusers", "diffusers": diffusers_block}}


def test_declared_capability_reaches_the_backend_profile() -> None:
    """The cfg block, not the module constant, is what the backend reports.

    Bug caught: the capability block is declared in the config model, threaded no
    further, and `validate_request` rejects `mode='t2va'` with "not in
    supported_modes" — after the pod is booked, because the profile is probed from
    the live backend.
    """
    backend = DiffusersEngine().backend(None, _cfg(_H3_CAPABILITY))
    profile = backend.inspect_capabilities()
    assert profile.supported_modes == {"t2va"}
    assert profile.supports_joint_audio is True
    assert profile.fps == 24
    assert profile.max_frames == 124
    assert profile.max_resolution == (1344, 768)


def test_a_cfg_without_a_capability_block_reports_the_default_probe() -> None:
    """Wan configs are untouched — the no-regression guard.

    Bug caught: t2va is delivered by widening `_DEFAULT_PROBE.supported_modes` to
    `{"t2v", "t2va"}`. Every cached Wan profile then mismatches the live probe on
    the next warm run, `JsonProfileCache.verify` raises CapabilityMismatch, and the
    orchestrator destroys a pod it just paid to boot.
    """
    profile = DiffusersEngine().backend(None, _cfg(None)).inspect_capabilities()
    assert profile.supported_modes == {"t2v"}
    assert profile.supports_joint_audio is False
    assert profile == _DEFAULT_PROBE


def test_partial_capability_block_keeps_the_default_for_absent_fields() -> None:
    """Declaring only the modes leaves the rest of the probe alone.

    Bug caught: the merge is written as a wholesale replacement, so a config that
    declares `supported_modes` alone silently zeroes `fps` / `max_frames` and the
    splitter's `max_segment_seconds` divides by zero.
    """
    profile = (
        DiffusersEngine()
        .backend(None, _cfg({"supported_modes": ["t2va"]}))
        .inspect_capabilities()
    )
    assert profile.supported_modes == {"t2va"}
    assert profile.fps == _DEFAULT_PROBE.fps
    assert profile.max_frames == _DEFAULT_PROBE.max_frames
    assert profile.max_resolution == _DEFAULT_PROBE.max_resolution


def test_capability_block_survives_load_config() -> None:
    """The YAML key is not silently dropped by pydantic.

    Bug caught: `DiffusersEngineConfig` has no `model_config`, so pydantic's default
    `extra="ignore"` drops any key the model does not declare. A capability block
    added to YAML but not to the model vanishes between `load_config` and the
    engine, and the whole feature is a no-op that no offline test would catch.
    """
    from kinoforge.core.config import load_config

    cfg = load_config("examples/configs/modal-diffusers-minimax-h3-t2va.yaml")
    assert cfg.engine.diffusers is not None
    capability = cfg.engine.diffusers.capability
    assert capability is not None
    assert capability.supported_modes == ["t2va"]
    assert capability.supports_joint_audio is True


def test_unknown_capability_key_is_refused() -> None:
    """A typo in the capability block fails loudly at load.

    Bug caught: `suported_modes` (one 'p') is ignored, the profile keeps `{"t2v"}`,
    and the failure surfaces as "mode 't2va' not in supported_modes" on a booked pod
    rather than as a config error at $0.
    """
    from kinoforge.core.config import DiffusersCapabilityConfig

    with pytest.raises(PydanticValidationError):
        DiffusersCapabilityConfig(suported_modes=["t2va"])  # type: ignore[call-arg]


def test_remote_instance_does_not_lose_the_declared_capability() -> None:
    """The remote branch of `backend()` keeps the profile override.

    Bug caught: the override is applied in the local branch only, so it works in
    every unit test and is absent on the one path that runs live.
    """
    instance = Instance(
        id="pod-1",
        provider="modal",
        status="running",
        endpoints={"8000": "https://example.modal.run"},
    )
    profile = (
        DiffusersEngine().backend(instance, _cfg(_H3_CAPABILITY)).inspect_capabilities()
    )
    assert profile.supported_modes == {"t2va"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run python -m pytest tests/engines/test_diffusers_capability_cfg.py -v`
Expected: FAIL — `AttributeError`/import error for `DiffusersCapabilityConfig`, and the
profile tests report `{"t2v"}`.

Note: `test_capability_block_survives_load_config` stays RED until Task 4 writes the YAML.
That is intentional — it is the test that proves the two halves meet. Confirm it fails with
"No such file" and not with a silently-dropped key.

- [ ] **Step 3: Write minimal implementation**

In `src/kinoforge/core/config.py`, above `DiffusersEngineConfig`:

```python
class DiffusersCapabilityConfig(BaseModel):
    """Per-config override of the diffusers engine's capability probe.

    ``DiffusersEngine`` ships ONE ``_DEFAULT_PROBE`` shared by every diffusers
    config — ``supported_modes={"t2v"}``, 81 frames, 24 fps, 1280x720. That was
    fine while every diffusers config was a Wan t2v config. MiniMax-H3 is a
    ``t2va`` model, and widening the shared constant would change what every Wan
    config reports: ``JsonProfileCache.verify`` compares ``supported_modes``
    against the live probe and raises ``CapabilityMismatch``, which destroys the
    instance. Cached Wan profiles already on disk say ``["t2v"]``, so the global
    widening is a teardown on the next warm run.

    Every field is optional and absent fields fall through to the module default,
    so a config declaring only ``supported_modes`` does not zero the rest.

    ``extra="forbid"``: a typo here would otherwise be dropped silently and
    resurface as "mode 't2va' not in supported_modes" on a pod that is already
    billing.

    Attributes:
        supported_modes: Modes this config's model serves, e.g. ``["t2va"]``.
        max_frames: Longest clip in frames.
        fps: Frame rate the model generates at.
        max_resolution: ``[width, height]`` ceiling.
        supports_joint_audio: Whether the model emits a soundtrack jointly with
            the frames. **A declaration, not a mechanism** — see the note at
            ``core/strategy.py:55``: the ``_audio_mode`` marker it feeds is read
            nowhere. H3's audio reaches the output through
            ``servers/_av_io.write_mp4_with_audio``, on the pod.
        supports_native_extension: Whether one job can carry every segment.
    """

    model_config = ConfigDict(extra="forbid")

    supported_modes: list[str] | None = None
    max_frames: int | None = None
    fps: int | None = None
    max_resolution: tuple[int, int] | None = None
    supports_joint_audio: bool | None = None
    supports_native_extension: bool | None = None
```

Add to `DiffusersEngineConfig` (and a line to its docstring):

```python
    capability: DiffusersCapabilityConfig | None = None  # Per-config capability
    # probe override. None => the engine's shared _DEFAULT_PROBE, which is what
    # every Wan config uses and must keep using. See DiffusersCapabilityConfig.
```

In `src/kinoforge/engines/diffusers/__init__.py`, add a module-level helper and call it
from `backend()` where `probe_profile=self._probe` is passed today:

```python
def _probe_with_cfg_capability(
    probe: ModelProfile, diffusers_cfg: dict[str, Any]
) -> ModelProfile:
    """Return *probe* with any cfg-declared capability fields applied.

    ``_DEFAULT_PROBE`` is shared by every diffusers config. A config may declare
    ``engine.diffusers.capability`` to describe its own model instead; absent
    fields fall through to *probe* so a partial declaration cannot zero the rest.

    Args:
        probe: The engine's default ``ModelProfile``.
        diffusers_cfg: The resolved ``engine.diffusers`` block.

    Returns:
        *probe* unchanged when no capability block is declared, else a new
        ``ModelProfile`` carrying the declared fields.
    """
    # `or {}`, not `.get`'s default — U33: `Config.model_dump()` emits an unset
    # `X | None = None` field as a PRESENT key whose value is None, so the default
    # is dead code.
    declared = diffusers_cfg.get("capability") or {}
    if not declared:
        return probe
    overrides: dict[str, Any] = {}
    if declared.get("supported_modes"):
        overrides["supported_modes"] = set(declared["supported_modes"])
    if declared.get("max_resolution"):
        overrides["max_resolution"] = tuple(declared["max_resolution"])
    for key in ("max_frames", "fps", "supports_joint_audio", "supports_native_extension"):
        value = declared.get(key)
        if value is not None:
            overrides[key] = value
    return dataclasses.replace(probe, **overrides)
```

and in `backend()`:

```python
        return DiffusersBackend(
            http_post=self._http_post,
            http_get=self._http_get,
            base_url=base_url,
            probe_profile=_probe_with_cfg_capability(self._probe, diffusers_cfg),
            ...
```

Add `import dataclasses` to the module's imports if absent.

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run python -m pytest tests/engines/test_diffusers_capability_cfg.py -v --deselect tests/engines/test_diffusers_capability_cfg.py::test_capability_block_survives_load_config`
Expected: PASS (the deselected one goes green in Task 4)

Then the no-regression sweep:
Run: `pixi run python -m pytest tests/engines/ tests/core/ -q`
Expected: PASS, no test newly red.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/core/config.py src/kinoforge/engines/diffusers/__init__.py
git add tests/engines/test_diffusers_capability_cfg.py
git commit -m "feat(diffusers): let a cfg declare its own capability profile"
```

---

### Task 3: the MiniMax-H3 t2va inference server

**Goal:** `servers/minimax_h3_server.py` serves the DiffusersBackend HTTP contract for H3,
loads the modular pipeline with `workflow="t2va"` under ComponentsManager auto offload, and
writes an MP4 carrying the jointly-generated stereo soundtrack.

**Files:**
- Create: `src/kinoforge/engines/diffusers/servers/minimax_h3_server.py`
- Test: `tests/servers/test_minimax_h3_server.py` (create)

**Acceptance Criteria:**
- [ ] `POST /generate` accepts `prompt`, `width`, `height`, `num_frames`,
      `num_inference_steps`, `seed`; **rejects** `guidance_scale` and `negative_prompt` with
      HTTP 422
- [ ] `width`/`height` not a multiple of 32 → 422; `num_frames` outside 120–360 → 422;
      defaults are 1344x768 / 124 / 50 steps
- [ ] The loader calls `ModularPipeline.from_pretrained` with `workflow="t2va"` and a
      `components_manager`, then `load_components(dtype=torch.bfloat16)`, then
      `manager.enable_auto_cpu_offload(device="cuda", memory_reserve_margin=...)` — in that
      order
- [ ] The pipeline is called with `output=["videos", "audio", "sampling_rate"]` and NO
      `guidance_scale` / `negative_prompt` kwarg
- [ ] The worker writes via `_av_io.write_mp4_with_audio` with `(T,H,W,3)` uint8 frames,
      `(samples, 2)` float audio, `fps=24`, and the pipeline's own `sampling_rate`
- [ ] `GET /health` reports `ready`, the model id and the torch build; `GET /util` returns
      the five `read_gpu_stats` keys; `GET /artifacts/{name}` refuses path traversal
- [ ] Host + device memory facts are logged BEFORE the load, so a host-RAM shortfall is
      legible in the Modal log rather than an unexplained container death
- [ ] `KINOFORGE_H3_LOAD_STUB` swaps the pipeline construction for a test double, so none of
      the above needs torch or CUDA

**Verify:** `pixi run python -m pytest tests/servers/test_minimax_h3_server.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
"""Behavior: the H3 t2va server's HTTP contract, its loading recipe and its mux.

Everything here runs offline against a fake pipeline installed through
`KINOFORGE_H3_LOAD_STUB`. The three things worth testing are the three that cost
money to get wrong live: the `workflow="t2va"` kwarg (a miss pulls 66 GB on a
$4.54/hr card), the geometry gate (every violation is a ValueError raised INSIDE
the pipeline call, minutes into a booked pod), and the audio reaching the mux in
the layout `_av_io` expects (a silent video is a "successful" run).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient


class FakeComponentsManager:
    """Records the offload call the real ComponentsManager would receive."""

    def __init__(self) -> None:
        self.offload_calls: list[dict[str, Any]] = []

    def enable_auto_cpu_offload(self, **kwargs: Any) -> None:
        self.offload_calls.append(kwargs)


class FakePipe:
    """A ModularPipeline stand-in that records how it was built and called."""

    def __init__(
        self,
        *,
        frames: int = 124,
        height: int = 768,
        width: int = 1344,
        samples: int = 165_333,
    ) -> None:
        self.from_pretrained_kwargs: dict[str, Any] = {}
        self.load_components_kwargs: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []
        self._frames, self._h, self._w, self._samples = frames, height, width, samples

    def load_components(self, **kwargs: Any) -> None:
        self.load_components_kwargs = kwargs

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        # (batch, T, H, W, 3) float in [0, 1] — what VideoProcessor.postprocess_video
        # returns for output_type="np".
        videos = np.full((1, self._frames, self._h, self._w, 3), 0.5, dtype=np.float32)
        # (1, 2, num_samples) — the documented shape of the `audio` output.
        audio = np.zeros((1, 2, self._samples), dtype=np.float32)
        audio[0, 0, :] = 0.25  # distinguishable left channel
        audio[0, 1, :] = -0.25
        return {"videos": videos, "audio": audio, "sampling_rate": 32000}


_FAKE: dict[str, Any] = {}


def _stub_loader() -> tuple[Any, Any]:
    """Installed via KINOFORGE_H3_LOAD_STUB; returns (pipe, manager)."""
    pipe = FakePipe()
    manager = FakeComponentsManager()
    pipe.from_pretrained_kwargs = dict(_FAKE.get("from_pretrained_kwargs", {}))
    _FAKE["pipe"] = pipe
    _FAKE["manager"] = manager
    return pipe, manager


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("KINOFORGE_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv(
        "KINOFORGE_H3_LOAD_STUB", "tests.servers.test_minimax_h3_server._stub_loader"
    )
    monkeypatch.setenv("WAN_MODEL_ID", "MiniMaxAI/MiniMax-H3")
    _FAKE.clear()
    import importlib

    from kinoforge.engines.diffusers.servers import minimax_h3_server as mod

    importlib.reload(mod)
    return mod


def test_generate_rejects_guidance_scale(server: Any) -> None:
    """The schema refuses what a guidance-distilled checkpoint cannot use.

    Bug caught: the request model is copied from the Wan server, so it carries
    `guidance_scale` and `negative_prompt`. Both are accepted, neither reaches the
    pipeline, and an operator tuning `guidance_scale` sees no effect and no error —
    the worst of the three outcomes.
    """
    with TestClient(server.app) as client:
        for field in ("guidance_scale", "negative_prompt"):
            resp = client.post("/generate", json={"prompt": "x", field: 6.0})
            assert resp.status_code == 422, f"{field} was accepted: {resp.text}"


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ({"prompt": "x", "width": 1000}, "width not a multiple of 32"),
        ({"prompt": "x", "height": 100}, "height not a multiple of 32"),
        ({"prompt": "x", "num_frames": 119}, "under 5 s at 24 fps"),
        ({"prompt": "x", "num_frames": 361}, "over 15 s at 24 fps"),
    ],
)
def test_generate_rejects_geometry_the_pipeline_would_reject(
    server: Any, body: dict[str, Any], why: str
) -> None:
    """Geometry is refused at the HTTP edge, not inside the pipeline.

    Bug caught: the checks live only in diffusers, which raises ValueError from
    `before_denoise`. On Modal that is a job error some minutes after a 124 GiB
    load finished — the most expensive possible place to learn that a width was
    1000 instead of 992.
    """
    with TestClient(server.app) as client:
        assert client.post("/generate", json=body).status_code == 422, why


def test_loader_passes_the_t2va_workflow_and_enables_auto_offload(server: Any) -> None:
    """The $66 kwarg, and the offload recipe, are both on the load path.

    Bug caught: `workflow="t2va"` is omitted, so `load_components` pulls BOTH 61.7
    GiB transformer partitions. `transformer_ref/` is not on the Volume, so that is
    a 66 GB download on an H200 at $4.54/hr. Second bug caught: no offload is
    enabled, so 123.8 GiB of weights are asked to be resident on a 131.3 GiB card
    and the denoise OOMs.
    """
    with TestClient(server.app):
        pass
    pipe, manager = _FAKE["pipe"], _FAKE["manager"]
    assert pipe.from_pretrained_kwargs.get("workflow") == "t2va"
    assert pipe.from_pretrained_kwargs.get("components_manager") is manager
    assert manager.offload_calls, "auto CPU offload was never enabled"
    assert manager.offload_calls[0]["device"] == "cuda"


def test_worker_muxes_audio_into_the_mp4(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The soundtrack reaches `_av_io.write_mp4_with_audio` in its expected layout.

    Bug caught: the writer is `_video_io.write_mp4`, which takes frames and nothing
    else, so the output is a silent video that passes every exit-code and ffprobe-
    dimension check. Second bug caught: `audio` is handed over as the raw
    `(1, 2, samples)` the pipeline returns instead of the `(samples, channels)`
    `_av_io` documents, which mislabels 2 samples as 165k channels.
    """
    seen: dict[str, Any] = {}

    def fake_write(frames, audio, fps, sample_rate, path) -> None:  # noqa: ANN001
        seen.update(
            frames=frames, audio=audio, fps=fps, sample_rate=sample_rate, path=path
        )
        Path(path).write_bytes(b"mp4")

    monkeypatch.setattr(server, "write_mp4_with_audio", fake_write)

    with TestClient(server.app) as client:
        job_id = client.post("/generate", json={"prompt": "x"}).json()["job_id"]
        server._drain_for_test()
        status = client.get(f"/status/{job_id}").json()

    assert status["status"] == "done", status
    assert seen["frames"].dtype == np.uint8
    assert seen["frames"].ndim == 4 and seen["frames"].shape[-1] == 3
    assert seen["audio"].shape == (165_333, 2), seen["audio"].shape
    assert seen["fps"] == 24
    assert seen["sample_rate"] == 32000
    # Channel order preserved: left was +0.25, right -0.25.
    assert seen["audio"][0, 0] > 0 > seen["audio"][0, 1]


def test_pipeline_is_asked_for_all_three_outputs(server: Any) -> None:
    """`videos`, `audio` and `sampling_rate` are all requested.

    Bug caught: `output="videos"` is passed (the single-string form), the call
    returns the frames alone, and the sample rate is hardcoded to 32000 — which
    silently desyncs the moment the checkpoint's audio VAE reports anything else.
    """
    with TestClient(server.app) as client:
        client.post("/generate", json={"prompt": "x"})
        server._drain_for_test()
    call = _FAKE["pipe"].calls[0]
    assert call["output"] == ["videos", "audio", "sampling_rate"]
    assert "guidance_scale" not in call
    assert "negative_prompt" not in call
    assert call["num_frames"] == 124
    assert call["num_inference_steps"] == 50


def test_health_and_util_are_served(server: Any) -> None:
    """The two routes the orchestrator and the CLAUDE.md polling rule depend on.

    Bug caught: `/util` is omitted because the server "does not need it", and the
    mandatory utilisation polling has nothing to read — which is how the 2026-07-05
    RIFE smoke burned 12 minutes on a pod at 0% GPU.
    """
    with TestClient(server.app) as client:
        health = client.get("/health").json()
        assert health["ready"] is True
        assert health["model"] == "MiniMaxAI/MiniMax-H3"
        util = client.get("/util").json()
        assert set(util) == {
            "gpu_util_percent",
            "cpu_percent",
            "memory_percent",
            "disk_percent",
            "uptime_seconds",
        }


def test_artifact_route_refuses_traversal(server: Any) -> None:
    """`../` cannot escape the artifact dir.

    Bug caught: the guard is copied without the `..` check, and the pod serves
    arbitrary container files — including the boot script, which carries HF_TOKEN.
    """
    with TestClient(server.app) as client:
        assert client.get("/artifacts/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run python -m pytest tests/servers/test_minimax_h3_server.py -v`
Expected: FAIL — `ModuleNotFoundError: kinoforge.engines.diffusers.servers.minimax_h3_server`

Create `tests/servers/__init__.py` if the directory is new.

- [ ] **Step 3: Write minimal implementation**

Create `src/kinoforge/engines/diffusers/servers/minimax_h3_server.py`. Shape, in order:

1. Module docstring stating the HTTP contract and that this is a SIBLING of
   `wan_t2v_server.py`, not an extension (2610 lines, 65 Wan-specific references).
2. Before any HF import: `os.environ.setdefault("HF_HUB_DISABLE_XET", "1")` and
   `os.environ.setdefault("HF_HOME", "/cache/hf")` — the Modal Volume mount, which the
   provider also exports; `setdefault` respects the provider's value.
3. `MODEL_ID = os.environ.get("WAN_MODEL_ID", "MiniMaxAI/MiniMax-H3")` with a comment: the
   env var is a misnomer inherited from `render_provision`, which derives it from
   `cfg.models[kind=base].ref` for every diffusers config and would move 20 launch goldens
   if renamed. Read it, do not rename it.
4. Geometry constants read from the v0.40.0 source, each with its provenance:
   `_CANVAS_MULTIPLE = 32`, `_FPS = 24`, `_MIN_FRAMES = 120`, `_MAX_FRAMES = 360`,
   `_DEFAULT_FRAMES = 124`, `_DEFAULT_WIDTH = 1344`, `_DEFAULT_HEIGHT = 768`,
   `_DEFAULT_STEPS = 50`.
5. `class GenerateRequest(BaseModel)` with `model_config = ConfigDict(extra="forbid")` and a
   docstring saying that `extra="forbid"` is what refuses `guidance_scale` /
   `negative_prompt`; `field_validator`s for the multiple-of-32 and frame-window rules.
6. `def _log_memory_facts()` — reads `/proc/meminfo` MemTotal/MemAvailable and, when torch
   is importable, `torch.cuda.mem_get_info()`; logs both. Never raises. Comment: 123.8 GiB
   of weights live in HOST RAM under auto offload, Modal's default container memory request
   is 128 MiB with burst-if-available, so this line is the difference between a legible
   shortfall and an unexplained container death.
7. `def _load() -> tuple[Any, Any]` returning `(pipe, manager)`; honours
   `KINOFORGE_H3_LOAD_STUB` (dotted path, same seam shape as the Wan server's
   `KINOFORGE_DIFFUSERS_LOAD_STUB`) and otherwise:
   ```python
   import torch
   from diffusers import ComponentsManager, ModularPipeline

   manager = ComponentsManager()
   pipe = ModularPipeline.from_pretrained(
       MODEL_ID, workflow="t2va", components_manager=manager
   )
   pipe.load_components(dtype=torch.bfloat16)
   manager.enable_auto_cpu_offload(
       device="cuda", memory_reserve_margin=_OFFLOAD_MARGIN
   )
   return pipe, manager
   ```
   with the Global Constraint 1 and 3 comments inline, `_OFFLOAD_MARGIN =
   os.environ.get("KINOFORGE_H3_OFFLOAD_MARGIN", "12GB")` (the value in the diffusers
   single-card recipe — not invented), and a note that
   `pipe.transformer.set_attention_backend("_flash_3_hub")` is the documented ~3x Hopper
   speed-up, deliberately NOT enabled on the first run because it fetches kernels from the
   Hub at request time and an unproven fetch on a $4.54/hr card is the same class of
   unproven link the engine-route ruling rejected. Gate it behind
   `KINOFORGE_H3_ATTENTION_BACKEND` so it is one env var away on a warm pod.
8. `_to_uint8_frames(videos)` — takes the `(1, T, H, W, 3)` float output, indexes batch 0,
   scales to uint8 when not already uint8. Handles the list-of-PIL case too, like the Wan
   worker, so an `output_type` change does not silently produce garbage.
9. `_to_interleaved_audio(audio)` — `(1, 2, N)` torch/numpy → `(N, 2)` float32 numpy, via
   `np.asarray(...)`, batch-index 0, `.T`. Docstring names the layout on both sides and why
   the transpose is load-bearing.
10. `_worker_loop()` + `_drain_for_test()` (a single-shot drain the tests call so no test
    sleeps on a thread), `_startup()` (logs the torch build and the memory facts, loads,
    spawns the worker, sets `ready`), and the routes `/health`, `/util`, `/generate`,
    `/status/{job_id}`, `/artifacts/{filename}` — the last three mirroring the Wan server's
    bodies, including the traversal guard.
11. `if __name__ == "__main__": uvicorn.run(app, host="0.0.0.0", port=8000)`

- [ ] **Step 4: Run test to verify it passes**

Run: `pixi run python -m pytest tests/servers/test_minimax_h3_server.py -v`
Expected: PASS

Then falsify the audio arm — the same discipline `_av_io` was built with:
```bash
# Temporarily make _to_interleaved_audio return the raw (1,2,N) array.
pixi run python -m pytest tests/servers/test_minimax_h3_server.py::test_worker_muxes_audio_into_the_mp4 -v
```
Expected: FAIL on the `(165_333, 2)` assertion. Revert.

- [ ] **Step 5: Commit**

```bash
git add src/kinoforge/engines/diffusers/servers/minimax_h3_server.py
git add tests/servers/test_minimax_h3_server.py tests/servers/__init__.py
git commit -m "feat(servers): add the MiniMax-H3 t2va server with joint-audio output"
```

---

### Task 4: the shipped config, its golden, and the H200 rate-cap guard

**Goal:** `examples/configs/modal-diffusers-minimax-h3-t2va.yaml` exists, is inside the
golden ratchet, and a test freezes the rule that any config booking H200 must cap at or
above $4.54/hr.

**Files:**
- Create: `examples/configs/modal-diffusers-minimax-h3-t2va.yaml`
- Create: `tests/providers/golden/launch_payloads/modal-diffusers-minimax-h3-t2va.json`
  (generated, never hand-written)
- Create: `tests/providers/modal/test_h200_rate_cap.py`

**Acceptance Criteria:**
- [ ] `pixi run kinoforge doctor -c examples/configs/modal-diffusers-minimax-h3-t2va.yaml`
      reports no ERROR
- [ ] `placement.accelerators == ["H200"]`, `min_vram_gb: 120`, `max_usd_per_hr >= 4.54`
- [ ] `engine.diffusers.pip` pins `diffusers==0.40.0` and does NOT loosen it to a range
- [ ] `spec` declares 1344x768, 124 frames, 24 fps, 50 steps
- [ ] `prompt` is byte-identical to `examples/configs/prompts/field-realistic.txt`
- [ ] The golden exists and `test_launch_payload_matches_golden` passes for it;
      `EXCLUDED_CONFIGS` stays empty
- [ ] A test asserts, over every shipped config, that declaring H200 implies
      `max_usd_per_hr >= 4.54`

**Verify:** `pixi run python -m pytest tests/providers/test_launch_payload_goldens.py tests/providers/modal/ -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing test**

```python
"""Behavior: an H200 config caps its rate at or above what H200 actually costs.

Before H200 existed in the Modal catalog, a config asking for more than 80 GB of
VRAM got a CapacityError for free. Now it books H200 at $4.54/hr — and because
every Modal offer is `mode="serverless"`, `filter_offers` SKIPS the
`max_usd_per_hr` ceiling at selection time (ModalProvider.capability_matrix marks
it UNSUPPORTED). The ceiling is then applied by `_enforce_rate_cap` AFTER launch,
which destroys the instance. An under-capped H200 config therefore pays for a boot
it cannot keep: the most silent way to spend money in this repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kinoforge.core.config import load_config
from kinoforge.providers.modal._catalog import MODAL_GPU_CATALOG

_H200_RATE = next(o.cost_rate_usd_per_hr for o in MODAL_GPU_CATALOG if o.id == "H200")


def _configs_declaring_h200() -> list[Path]:
    out: list[Path] = []
    for path in sorted(Path("examples/configs").glob("*.yaml")):
        cfg = load_config(str(path))
        if cfg.compute is None:
            continue
        if "H200" in (cfg.placement().accelerators or []):
            out.append(path)
    return out


def test_h200_is_still_priced_at_the_rate_this_guard_assumes() -> None:
    """The guard is pinned to the catalog, not to a literal.

    Bug caught: Modal reprices H200 upward, every cap in the tree is now too low,
    and a guard hardcoding 4.54 keeps passing while every H200 config launches-then-
    dies.
    """
    assert _H200_RATE == pytest.approx(4.54)


def test_every_h200_config_caps_at_or_above_the_h200_rate() -> None:
    """No shipped config can book H200 and then be reaped for being over cap.

    Bug caught: the MiniMax-H3 config is copied from a Wan config, inherits
    `max_usd_per_hr: 4.00`, books H200 at $4.54, and `_enforce_rate_cap` destroys it
    after the boot is paid for.
    """
    offenders = {
        path.name: load_config(str(path)).placement().max_usd_per_hr
        for path in _configs_declaring_h200()
    }
    assert offenders, "no config declares H200 — this guard is covering nothing"
    too_low = {name: cap for name, cap in offenders.items() if cap < _H200_RATE}
    assert not too_low, (
        f"these configs book H200 at ${_H200_RATE}/hr with a lower cap, so "
        f"_enforce_rate_cap destroys them after launch: {too_low}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pixi run python -m pytest tests/providers/modal/test_h200_rate_cap.py -v`
Expected: `test_every_h200_config_caps_at_or_above_the_h200_rate` FAILS on
"no config declares H200 — this guard is covering nothing".

- [ ] **Step 3: Write minimal implementation**

Create `examples/configs/modal-diffusers-minimax-h3-t2va.yaml`:

```yaml
# MiniMax-H3 text-to-video-AND-AUDIO (t2va) on Modal H200.
#
# First joint-audio model in kinoforge, first H200 config, first `mode: t2va`.
# Design: docs/superpowers/specs/2026-09-17-minimax-h3-t2va-design.md
# Plan:   docs/superpowers/plans/2026-09-17-minimax-h3-sub-project-c.md
#
# Weights are ALREADY on the `kinoforge-hf-cache` Modal Volume (Sub-project B:
# 144.05 GB fetched on a T4 for ~$0.08 instead of ~$3.94 on this card). The
# provider mounts that Volume at HF_HOME=/cache/hf, so from_pretrained reads
# rather than downloads. Do NOT delete the Volume expecting a cheap re-fetch.
#
# Five things here are load-bearing and cost real money if edited carelessly:
#  1. `capability.supported_modes: [t2va]` — the diffusers engine's shared probe
#     declares t2v only; without this block `validate_request` refuses the run
#     AFTER the pod is booked. Never widen the shared probe instead: 10 cached
#     Wan profiles say ["t2v"] and `verify` tears down on a mismatch.
#  2. `max_usd_per_hr: 5.00` — H200 is $4.54/hr and Modal offers are serverless,
#     so `filter_offers` skips the cap at SELECT time and `_enforce_rate_cap`
#     applies it AFTER launch. A lower cap pays for a boot it cannot keep.
#  3. `diffusers==0.40.0` exactly — 0.39.0 and 0.38.0 do not export
#     MiniMaxH3ModularPipeline. This pin is H3's alone; the Wan images keep
#     `diffusers>=0.32` and must not be dragged along.
#  4. `boot_timeout: 45m` — 123.8 GiB of weights come off the Volume into HOST
#     RAM (auto CPU offload) before the first step runs. It is also the Modal
#     function `timeout`, so it has to cover boot AND generation.
#  5. 1344x768 / 124 frames is the model's own trained canvas and its shortest
#     legal clip (5.17 s of the 5-15 s window, `17*n+5` aligned). If the run
#     OOMs, drop to 960x544 — the diffusers H3 doc measures it at ~2.3x faster
#     per step — before touching hardware.
#
# Modal cfgs need the live-modal env: `pixi run -e live-modal ...`. The default
# env has no `modal` module.
#
# Usage (one-shot; --no-reuse so the pod auto-destroys, then verify with
# `pixi run kinoforge list`):
#   pixi run -e live-modal kinoforge generate \
#     --config examples/configs/modal-diffusers-minimax-h3-t2va.yaml \
#     --mode t2va \
#     --prompt "$(cat examples/configs/prompts/field-realistic.txt)" \
#     --no-reuse
#
# NO `backend_options.skypilot` block — those keys are SkyPilot-only and fail
# validation here (the owning provider validates its own namespace).

mode: t2va
prompt: "<verbatim contents of examples/configs/prompts/field-realistic.txt>"

engine:
  kind: diffusers
  precision: bf16
  diffusers:
    # Modal's serialized web_server fn requires image-Python == controller
    # (live-modal env = 3.13). python:3.13-slim matches.
    image: "python:3.13-slim"
    server_cmd:
      - "python"
      - "-m"
      - "kinoforge.engines.diffusers.servers.minimax_h3_server"
    pip:
      - "torch==2.6.0"
      - "torchvision==0.21.0"
      - "torchaudio==2.6.0"
      # EXACT pin — see note 3 in the header.
      - "diffusers==0.40.0"
      # Qwen3VLForConditionalGeneration is the H3 conditioner; it lands in
      # transformers 4.57. Older releases cannot construct the text encoder and
      # the failure is an unhelpful KeyError on the component class name.
      - "transformers>=4.57"
      - "accelerate>=1.0"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"
      - "imageio[ffmpeg]>=2.34"
      # Util-probe deps: psutil -> cpu/mem/disk %, nvidia-ml-py -> pynvml GPU%.
      # Load-bearing, not optional: the CLAUDE.md live-smoke rule requires
      # polling gpuUtilPercent, and a 10-minute generation with no util signal
      # is exactly the 2026-07-05 RIFE failure (12 min at 0% GPU, unnoticed).
      - "psutil>=5.9"
      - "nvidia-ml-py>=12"
    base_url: "http://localhost:8000"
    prompt_body_key: "prompt"
    embed_modules: ["kinoforge.engines.diffusers.servers"]
    capability:
      # See note 1 in the header. `supports_joint_audio: true` is the first
      # `true` in the repo — and it is a DECLARATION, not a mechanism: the
      # `_audio_mode` marker it feeds (core/strategy.py:55) is read nowhere.
      # H3's soundtrack reaches the MP4 through
      # servers/_av_io.write_mp4_with_audio, on the pod.
      supported_modes: ["t2va"]
      max_frames: 360          # 15 s at 24 fps, the model's ceiling
      fps: 24                  # fixed by the checkpoint, not a preference
      max_resolution: [1344, 768]
      supports_joint_audio: true

models:
  # Repo ROOT, not FL2VA/. The nested FL2VA manifest declares
  # `_class_name: MiniMaxH3Pipeline`, a class diffusers does not export at all;
  # only the root-level modular layout (MiniMaxH3ModularPipeline) is loadable.
  - ref: "hf:MiniMaxAI/MiniMax-H3"
    kind: base
    target: checkpoints  # informational; diffusers manages the HF cache

compute:
  provider: modal
  image: "python:3.13-slim"
  mode: pod
  placement:
    # 123.8 GiB of bf16 weights. H200 (141 GB = 131.3 GiB) is the only card in
    # the Modal catalog above 80 GB, deliberately — Modal does not publish
    # B200/B300 VRAM and inventing the constant is the U48 defect.
    min_vram_gb: 120
    min_cuda: "12.4"
    max_usd_per_hr: 5.00  # >= 4.54; see note 2 in the header
    accelerators:
      - "H200"
  lifecycle:
    heartbeat_interval_s: 30  # required when warm_reuse_auto_attach=true
    idle_timeout: 20m
    job_timeout: 30m
    time_buffer: 3m
    max_lifetime: 75m
    boot_timeout: 45m  # see note 4 in the header
    budget: 6.0

spec:
  model: "MiniMax-H3"
  pipeline: "MiniMaxH3ModularPipeline"
  # Two schedulers, stepped inside a single transformer call: shift=12.0 for
  # video, shift=3.0 for audio. Both are MiniMaxH3Scheduler; the spec key takes
  # one name, so it names the class.
  scheduler: "MiniMaxH3Scheduler"
  width: 1344
  height: 768
  num_frames: 124
  fps: 24
  num_inference_steps: 50
```

Fill `prompt:` by reading `examples/configs/prompts/field-realistic.txt` — verbatim, one
line, no paraphrase (`feedback_standard_test_prompt`).

- [ ] **Step 4: Run test to verify it passes**

```bash
pixi run python -m pytest tests/providers/modal/test_h200_rate_cap.py -v
pixi run kinoforge doctor -c examples/configs/modal-diffusers-minimax-h3-t2va.yaml
pixi run python -m pytest tests/engines/test_diffusers_capability_cfg.py -v
```
Expected: rate-cap tests PASS; doctor reports 0 ERRORs; the Task-2
`test_capability_block_survives_load_config` is now GREEN.

Then bring it into the ratchet. **Order matters** — run pre-commit FIRST, then snapshot,
or the goldens capture pre-format bytes and move again on the next commit:

```bash
pixi run pre-commit run --all-files
pixi run python tools/snapshot_launch_payloads.py
git status --short tests/providers/golden/launch_payloads/
```
Expected: exactly ONE new file, `modal-diffusers-minimax-h3-t2va.json`, and **no other
golden modified**. If another golden moved, stop and find out why before committing — a
moved Wan golden means the engine change in Task 2 leaked into `render_provision`.

```bash
pixi run python -m pytest tests/providers/test_launch_payload_goldens.py -q
```
Expected: PASS, and `test_excluded_configs_exist_and_still_fail_to_capture` trivially green
because `EXCLUDED_CONFIGS` is still empty.

- [ ] **Step 5: Commit**

```bash
git add examples/configs/modal-diffusers-minimax-h3-t2va.yaml
git add tests/providers/golden/launch_payloads/modal-diffusers-minimax-h3-t2va.json
git add tests/providers/modal/test_h200_rate_cap.py
git commit -m "feat(configs): add the MiniMax-H3 t2va config and freeze the H200 cap"
```

---

### Task 5: whole-suite green, then commit the RED-safe scaffold

**Goal:** Everything offline is green and committed BEFORE any live spend, per the
CLAUDE.md durability rule.

**Files:** none new — this task is verification.

**Acceptance Criteria:**
- [ ] `pixi run test` passes with no new failures against the pre-task baseline
- [ ] `pixi run typecheck` clean
- [ ] `pixi run pre-commit run --all-files` clean
- [ ] Working tree clean; `pixi run preflight` exits 0
- [ ] No golden other than the new one has moved

**Verify:** `pixi run test -q && pixi run typecheck && pixi run preflight` → exit 0

**Steps:**

- [ ] **Step 1: Record the baseline**

```bash
git stash list
pixi run python -m pytest -m 'not live' -q 2>&1 | tail -20
```

- [ ] **Step 2: Full suite + types + hooks**

```bash
pixi run python -m pytest -m 'not live' -q
pixi run typecheck
pixi run pre-commit run --all-files
```
Expected: all pass. Conflict order if one breaks another: tests > mypy > ruff.

- [ ] **Step 3: Preflight**

```bash
pixi run preflight
```
Expected: exit 0 — RUNPOD/HF creds present, zero active pods, clean tree.

- [ ] **Step 4: Commit anything the hooks re-formatted**

```bash
git status --short
git add -A && git commit -m "chore: pre-commit fixups for the MiniMax-H3 scaffold"
```
(Skip if the tree is already clean.)

---

### Task 6: the live run — generate, QA video AND audio, tear down, log

**Goal:** One live `kinoforge generate --mode t2va` produces a frame-QA'd, audio-QA'd clip;
the pod is destroyed and verified gone from a fresh process; the run is written up.

**Files:**
- Modify: `successful-generations.md` (new section — new model, new mode, new GPU, first
  joint audio)
- Modify: `PROGRESS.md` (RESUME SNAPSHOT + STATUS INDEX if anything is found)

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 immediately before the spend
- [ ] The run uses `--no-reuse` and the prompt file verbatim
- [ ] Utilisation is polled every 60-90 s during the run — `gpuUtilPercent` /
      `cpuPercent` / `memoryPercent`, never `est_spend` as the health signal. GPU 0% for
      ≥3 consecutive probes → pull logs, destroy, fail fast
- [ ] The output MP4 has a video stream at 1344x768 and 124 frames at 24 fps
- [ ] The output MP4 has an **audio** stream: stereo, 32000 Hz, duration within 0.2 s of
      the video, and **not digital silence** (peak and RMS both above floor)
- [ ] ~5 frames extracted with `kinoforge.core.frames.ffmpeg_frames_by_count` and read as a
      contact sheet; verdict recorded, with an explicit ⚠️ on anything not clearly good
- [ ] `pixi run kinoforge list` from a FRESH process reports both "No running instances."
      and "No instances recorded in ledger."
- [ ] `modal app list` shows no running kinoforge app
- [ ] `successful-generations.md` entry written per that file's schema preamble

**Verify:** `pixi run kinoforge list` → "No running instances." AND "No instances recorded in ledger."

**Steps:**

- [ ] **Step 1: Preflight and arm the safety net**

```bash
pixi run preflight
pixi run kinoforge sweeper start &
```

- [ ] **Step 2: Launch, backgrounded so the poll loop can run**

```bash
pixi run -e live-modal kinoforge generate \
  --config examples/configs/modal-diffusers-minimax-h3-t2va.yaml \
  --mode t2va \
  --prompt "$(cat examples/configs/prompts/field-realistic.txt)" \
  --no-reuse
```

- [ ] **Step 3: Poll utilisation every 60-90 s while it runs**

Modal's util probe, not `est_spend`:

```python
from kinoforge.core.dotenv_loader import load_env_file; load_env_file()
from kinoforge.providers.modal.util import ModalUtilEndpoint  # confirm the class name
print(ModalUtilEndpoint().probe("<app-or-instance-id>"))
```

The server also serves `/util` directly on the Modal web URL, which is the shortest path:
`curl -s <web-url>/util`. Surface `gpu_util_percent`, `cpu_percent`, `memory_percent` every
probe. During the load phase expect GPU 0% with **memory rising** — that is the 123.8 GiB
coming off the Volume and is healthy. GPU 0% **with flat memory** for ≥3 probes is a dead
boot: pull `modal app logs`, destroy, fail fast.

- [ ] **Step 4: Audio QA — the arm that does not exist for any other model**

```bash
OUT=$(ls -t output/*.mp4 | head -1)
ffprobe -v error -show_streams -of json "$OUT" > /tmp/h3_probe.json
```

Then assert, in Python rather than by eye, that the audio is real:

```python
import json, subprocess
import numpy as np

probe = json.load(open("/tmp/h3_probe.json"))
streams = {s["codec_type"]: s for s in probe["streams"]}
assert "audio" in streams, "NO AUDIO STREAM — a silent 'success' is the failure mode"
audio = streams["audio"]
assert int(audio["channels"]) == 2, audio["channels"]
assert int(audio["sample_rate"]) == 32000, audio["sample_rate"]
video = streams["video"]
assert abs(float(audio["duration"]) - float(video["duration"])) < 0.2

# Decode to raw PCM and prove it is not digital silence.
raw = subprocess.run(
    ["ffmpeg", "-v", "error", "-i", OUT, "-f", "s16le", "-ac", "2", "-"],
    capture_output=True, check=True,
).stdout
pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
peak, rms = float(np.abs(pcm).max()), float(np.sqrt((pcm**2).mean()))
print(f"peak={peak:.4f} rms={rms:.4f}")
assert peak > 0.01 and rms > 1e-4, "digital silence — ffprobe alone cannot hear this"
```

- [ ] **Step 5: Frame QA**

```python
from pathlib import Path
from kinoforge.core.frames import ffmpeg_frames_by_count
frames = ffmpeg_frames_by_count(Path(OUT).read_bytes(), count=5)
# write to the scratchpad, montage into one contact sheet, then READ it
```
Judge artifacts, temporal coherence and prompt adherence like the entry-#18 keyframe review.
Record the verdict. Anything not clearly high quality gets an explicit ⚠️.

- [ ] **Step 6: Teardown, verified from a fresh process**

```bash
pixi run kinoforge list
pixi run -e live-modal modal app list | head -20
```
Expected: "No running instances." AND "No instances recorded in ledger."; no running
`kinoforge-*` app. If either shows a pod:
```bash
pixi run kinoforge destroy --id <pod-id>
```

- [ ] **Step 7: Write it up and commit**

Add a `successful-generations.md` section per that file's preamble schema (new capability
axis: new model, new mode `t2va`, new GPU H200, first joint-audio output) carrying the exact
reproduction command, the measured spend, the frame-QA verdict and the audio-QA numbers.
Update the `PROGRESS.md` RESUME SNAPSHOT: Sub-project C done, what the live run corrected,
and the next action.

```bash
git add successful-generations.md PROGRESS.md
git commit -m "docs: MiniMax-H3 t2va live-proven on Modal H200 with joint audio"
```

---

## Self-Review

**Spec coverage:** Server (Task 3) · audio output via `_av_io` (Task 3, module already
committed at `ea4843dc`) · mode (Task 1) · capability profile incl. the inert-seam comments
(Tasks 1, 2, 4) · config (Task 4) · golden ratchet (Task 4) · frame QA with an audio arm
(Task 6) · Done-when (Task 6). All spec sections have a task.

**Spec corrections this plan carries:**
1. `enable_model_cpu_offload` does not exist on `ModularPipeline` — the mechanism is
   `ComponentsManager.enable_auto_cpu_offload`. Global Constraint 3.
2. The spec's config sketch said `models: [...] restricted to FL2VA/*`. That is the
   superseded layout: FL2VA declares a class diffusers does not export. The config declares
   the repo root, matching the spec's own later CORRECTION section.
3. `num_frames` has a hard 5-15 s window and a `17*n+5` alignment, and `height`/`width` must
   be multiples of 32 — none of which was in the spec. Global Constraint 6, gated at the
   HTTP edge in Task 3.
4. Under auto offload the peak GPU residency is ~62 GiB, not ~124 GiB, so spec risk 1 (the
   141 GB fit) is much weaker than written — the binding resource becomes ~124 GiB of HOST
   RAM, which Modal grants on a burst basis. Task 3 logs both numbers so the first run
   answers it.

**Placeholder scan:** the only intentional placeholder is the `prompt:` line in the Task 4
config body, which Step 3 says explicitly to fill from
`examples/configs/prompts/field-realistic.txt` verbatim, and the `<app-or-instance-id>` /
`ModalUtilEndpoint` name in Task 6 Step 3, flagged there as needing confirmation against the
module.

**Type consistency:** `DiffusersCapabilityConfig` field names are identical in the config
model, the YAML, `_probe_with_cfg_capability` and the tests. `write_mp4_with_audio(frames,
audio, fps, sample_rate, path)` matches the committed `_av_io` signature.
`_to_interleaved_audio` / `_to_uint8_frames` / `_drain_for_test` are named once and used
consistently in Task 3's tests and implementation.
