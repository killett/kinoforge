# FlashVSR video upscaling implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land FlashVSR v1.1 as v1 default video upscaler in kinoforge — engine-agnostic seam validated by three concrete implementers (spandrel + seedvr2-stub + flashvsr); Wan 2.2 → FlashVSR multi-stage live-GREEN with warm-reuse across two prompts.

**Architecture:** Additive third `UpscalerEngine` implementer (`kinoforge.upscalers.flashvsr`); zero changes to core ABCs/registry/orchestrator/CLI; server-side changes limited to slug-prefix dispatch + one Pydantic body field. Runtime wraps upstream `flashvsr.pipeline.StreamingDMDPipeline`; Block-Sparse-Attention CUDA kernel compiled once at pod cold-boot via `render_provision`, `.so` cached on `/workspace` bind-mount.

**Tech Stack:** Python 3.12, pixi, ruff, mypy, pytest. Runtime: `torch`, `flashvsr` (git-installable), `mit-han-lab/Block-Sparse-Attention` (nvcc-compiled), `imageio[ffmpeg]`, `huggingface_hub`. Existing server: FastAPI in `wan_t2v_server.py`. Live compute: RunPod A100 80GB / A6000 (SM80+).

**User decisions (already made):**
- FlashVSR v1.1 picked over SeedVR2 / STAR (spec §1, brainstorm approval).
- Spandrel demoted to fallback, NOT deprecated (spec §2 goal 3).
- SeedVR2 stays dormant in `[seedvr]` extras (spec §2 goal 4).
- No text-prompt-guided upscale — FlashVSR has no text encoder (spec §2 non-goals).
- Live budget ceiling $3; happy-path ~$0.65 (spec §2 goal 7).
- Run autonomously — no user-gate handshakes (memory: `feedback_autonomous_no_gates.md`; live spend pre-authorized to $20 session budget).

---

## File Structure

**New package** — `src/kinoforge/upscalers/flashvsr/`:
- `__init__.py` — self-register on import (mirror `spandrel/__init__.py`, ~30 LOC).
- `_engine.py` — `FlashVSREngine(UpscalerEngine)`: HTTP dispatch to `/upscale`, `/upscale/status/{id}`, `/upload` (mirror `spandrel/_engine.py`, ~320 LOC).
- `_runtime.py` — `FlashVSRRuntime`: wraps `StreamingDMDPipeline`, satisfies LRU `LoadedModel` contract (`~150` LOC).
- `_fetch_weights.py` — pod-safe CLI (no `kinoforge.core.registry` import); fetches 4-file bundle from HF (~180 LOC).
- `weights_manifest.json` — SHA256 pins per bundle file (~40 LOC).

**Edits** — targeted, small:
- `src/kinoforge/core/errors.py` — append `BSACompileFailed`, `FlashVSRWeightsIncomplete`, `UnsupportedGpuArch`.
- `src/kinoforge/core/config.py` — append `FlashVSREngineConfig`; extend `UpscaleConfig.flashvsr`; extend `capability_key()` branch.
- `src/kinoforge/_adapters.py` — one-line `import kinoforge.upscalers.flashvsr`.
- `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` — 5 edits: `_load_model_to_gpu` prefix branch, `_capability_for_model` branch, `_flashvsr_weights_dir()` helper, `UpscaleRequest.flashvsr` field + `FlashVSRParams` Pydantic model, `_run_upscale_job` dispatch table + `upscale_handler` engine allowlist.

**New examples**:
- `examples/configs/runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml` — multi-stage T2V + upscale.
- `examples/configs/runpod-upscale-only-flashvsr.yaml` — standalone `kinoforge upscale`.

**New tests** — `tests/upscalers/flashvsr/`:
- `test_config.py` — 8 tests.
- `test_engine.py` — 10 tests.
- `test_runtime.py` — 6 tests.
- `test_fetch_weights.py` — 6 tests.

**Test edits**:
- `tests/engines/diffusers/servers/test_wan_t2v_server.py` — extend dispatch + capability tests.
- `tests/test_adapters.py` — extend registration test.
- `tests/test_examples.py` — 2 lockdown tests for new cfgs.

**New live-smoke file**:
- `tests/live/test_flashvsr_live.py` — RED xfail-gated scaffold; 3 smokes.

**Docs**:
- `docs/engines.md` — extend with flashvsr section (if file exists; skip if not).
- `PROGRESS.md` — update "Active workstream" pointer at plan-write time; flip to SHIPPED after T10.
- `/workspace/successful-generations.md` — new section after F-multi GREEN.

---

## Task 0: Errors + config surface

**Goal:** Land new exceptions + `FlashVSREngineConfig` + `UpscaleConfig.flashvsr` wiring. RED-first; no engine code yet.

**Files:**
- Modify: `src/kinoforge/core/errors.py` — append 3 exceptions
- Modify: `src/kinoforge/core/config.py` — append `FlashVSREngineConfig`; extend `UpscaleConfig`; extend `capability_key`
- Create: `tests/upscalers/flashvsr/__init__.py` — empty
- Create: `tests/upscalers/flashvsr/test_config.py`

**Acceptance Criteria:**
- [ ] `BSACompileFailed`, `FlashVSRWeightsIncomplete`, `UnsupportedGpuArch` importable from `kinoforge.core.errors`; each subclasses `KinoforgeError`; each has typed `__init__` recording problem context.
- [ ] `FlashVSREngineConfig` dataclass validates: `precision ∈ {"fp16","fp32"}`; `window_size ∈ [8, 64]`; `tile_size ∈ {0, 256, 384, 512, 768}`; `weights_bundle` scheme ∈ `{"hf:", "http://", "https://", "civitai:", "civarchive:"}`. All rejections raise `ConfigError`.
- [ ] `UpscaleConfig.flashvsr: FlashVSREngineConfig | None` field wired; when `engine == "flashvsr"` + `flashvsr is None`, cfg-time raises `ConfigError`.
- [ ] `engine == "flashvsr"` + `scale.kind == "height"` raises `ConfigError` at cfg-time (spec §4.2 fail-fast).
- [ ] `Config.capability_key()` populates `upscaler = "flashvsr"`, `upscaler_precision = <precision>` when engine is flashvsr.

**Verify:** `pixi run pytest tests/upscalers/flashvsr/test_config.py -v` → 8 passed

**Steps:**

- [ ] **Step 1: Write failing tests (RED) — `tests/upscalers/flashvsr/test_config.py`**

```python
"""FlashVSREngineConfig + UpscaleConfig integration + capability_key population."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import (
    BSACompileFailed,
    ConfigError,
    FlashVSRWeightsIncomplete,
    UnsupportedGpuArch,
)


def test_new_exceptions_importable_and_subclass_kinoforge_error() -> None:
    """RED: import path exists.

    Bug caught: forgetting to add the exception → downstream `except
    BSACompileFailed:` silently catches nothing.
    """
    from kinoforge.core.errors import KinoforgeError

    assert issubclass(BSACompileFailed, KinoforgeError)
    assert issubclass(FlashVSRWeightsIncomplete, KinoforgeError)
    assert issubclass(UnsupportedGpuArch, KinoforgeError)


def test_new_exceptions_carry_context() -> None:
    """RED: exceptions record post-mortem context, not just a string.

    Bug caught: raising `BSACompileFailed()` with no args → post-mortem
    can't tell which pod / stderr tail triggered it.
    """
    e1 = BSACompileFailed(pod_id="pod-xyz", stderr_tail="nvcc: OOM")
    assert e1.pod_id == "pod-xyz"
    assert "nvcc: OOM" in e1.stderr_tail

    e2 = FlashVSRWeightsIncomplete(filename="TCDecoder.ckpt", got_sha256="abc", want_sha256="def")
    assert e2.filename == "TCDecoder.ckpt"

    e3 = UnsupportedGpuArch(got=(7, 5), required_major=8)
    assert e3.got == (7, 5)
    assert e3.required_major == 8


def test_flashvsr_config_valid_defaults() -> None:
    """RED: happy-path construction with all defaults."""
    from kinoforge.core.config import FlashVSREngineConfig

    c = FlashVSREngineConfig(weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1")
    assert c.precision == "fp16"
    assert c.window_size == 24
    assert c.tile_size == 0
    assert c.long_video_mode is False


@pytest.mark.parametrize("bad_precision", ["bf16", "int8", "FP16", ""])
def test_flashvsr_config_rejects_bad_precision(bad_precision: str) -> None:
    """RED: precision allowlist enforced at cfg-time.

    Bug caught: typo `precision: bf16` silently accepted → runtime OOM
    on a dtype that maps to fp32 headers.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    with pytest.raises(ConfigError, match="precision"):
        FlashVSREngineConfig(
            weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1",
            precision=bad_precision,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("bad_window", [0, 7, 65, 128, -1])
def test_flashvsr_config_rejects_bad_window_size(bad_window: int) -> None:
    """RED: window_size clamp is HARD (raises) not soft (clamps silently).

    Bug caught: silent clamp masks a cfg typo (`window_size: 240` intended
    for a large-window path) → subtle quality regression.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    with pytest.raises(ConfigError, match="window_size"):
        FlashVSREngineConfig(
            weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1",
            window_size=bad_window,
        )


@pytest.mark.parametrize("bad_tile", [1, 100, 513, 1024])
def test_flashvsr_config_rejects_off_allowlist_tile_size(bad_tile: int) -> None:
    """RED: tile_size allowlist enforced.

    Bug caught: `tile_size: 100` silently accepted → BSA hits a shape
    misaligned with its block size and either crashes at runtime or
    produces subtle border artifacts.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    with pytest.raises(ConfigError, match="tile_size"):
        FlashVSREngineConfig(
            weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1",
            tile_size=bad_tile,
        )


def test_flashvsr_config_rejects_unknown_weights_scheme() -> None:
    """RED: unknown scheme fails at cfg-time, not resolver-time.

    Bug caught: `weights_bundle: gs://bucket/...` silently accepted →
    pod cold-boot burns 60s before failing on the resolver.
    """
    from kinoforge.core.config import FlashVSREngineConfig

    with pytest.raises(ConfigError, match="weights_bundle"):
        FlashVSREngineConfig(weights_bundle="gs://bucket/flashvsr")


def test_upscale_config_flashvsr_engine_requires_block() -> None:
    """RED: `engine: flashvsr` with no `flashvsr:` block fails cfg-time.

    Bug caught: forgotten block → pod cold-boot then late runtime error
    about missing weights_bundle.
    """
    from kinoforge.core.config import UpscaleConfig

    with pytest.raises(ConfigError, match="flashvsr"):
        UpscaleConfig(engine="flashvsr", scale="2x", flashvsr=None)


def test_upscale_config_flashvsr_rejects_height_scale() -> None:
    """RED: height-target refused at cfg-time (spec §4.2 fail-fast).

    Bug caught: deferring the reject to runtime burns the pod cold-boot.
    """
    from kinoforge.core.config import FlashVSREngineConfig, UpscaleConfig

    with pytest.raises(ConfigError, match="height-target"):
        UpscaleConfig(
            engine="flashvsr",
            scale="1080p",
            flashvsr=FlashVSREngineConfig(weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1"),
        )


def test_capability_key_populates_flashvsr_precision() -> None:
    """RED: capability_key threads flashvsr precision into the key.

    Bug caught: forgetting to extend `capability_key()` → an fp32
    FlashVSR request lands on an fp16-warm pod without triggering a
    reload; wrong-dtype inference.
    """
    from kinoforge.core.config import Config, FlashVSREngineConfig, UpscaleConfig

    cfg = Config.minimal_valid_for_test()  # existing helper
    cfg.upscale = UpscaleConfig(
        engine="flashvsr",
        scale="2x",
        flashvsr=FlashVSREngineConfig(
            weights_bundle="hf:JunhaoZhuang/FlashVSR-v1.1",
            precision="fp32",
        ),
    )
    key = cfg.capability_key()
    assert key.upscaler == "flashvsr"
    assert key.upscaler_precision == "fp32"
```

- [ ] **Step 2: Confirm RED**

```bash
pixi run pytest tests/upscalers/flashvsr/test_config.py -v
```
Expected: all 8 tests fail with `ImportError: cannot import name 'BSACompileFailed'` etc. (and `Config.minimal_valid_for_test` may need a small helper — if absent, use `Config()` with sensible defaults from an existing test fixture).

- [ ] **Step 3: Append to `src/kinoforge/core/errors.py`**

```python
class BSACompileFailed(KinoforgeError):
    """Block-Sparse-Attention nvcc compile failed on pod.

    Raised inside the server when `import block_sparse_attention` fails at
    first /upscale after cold boot — the compile happened at provision
    time but produced no importable module.
    """

    def __init__(self, pod_id: str, stderr_tail: str) -> None:
        """Record the pod that failed to compile + tail of the compiler stderr."""
        super().__init__(
            f"Block-Sparse-Attention compile failed on pod {pod_id}: "
            f"{stderr_tail[-500:]}"
        )
        self.pod_id = pod_id
        self.stderr_tail = stderr_tail


class FlashVSRWeightsIncomplete(KinoforgeError):
    """FlashVSR weights bundle failed SHA256 verification against manifest.

    Distinguishes CDN corruption / repo tampering from a plain
    download-timeout (which would surface as TransportError).
    """

    def __init__(self, filename: str, got_sha256: str, want_sha256: str) -> None:
        """Record the file, observed sha, and expected sha for post-mortem."""
        super().__init__(
            f"FlashVSR weights integrity failure on {filename}: "
            f"got sha256={got_sha256[:8]}..., want={want_sha256[:8]}..."
        )
        self.filename = filename
        self.got_sha256 = got_sha256
        self.want_sha256 = want_sha256


class UnsupportedGpuArch(KinoforgeError):
    """Pod GPU compute capability is below FlashVSR's SM80 requirement.

    Raised via provision-script exit code 87. Surfaces up the orchestrator
    as a hard-fail before any /upscale work is attempted.
    """

    def __init__(self, got: tuple[int, int], required_major: int) -> None:
        """Record the observed SM (major, minor) and the minimum required major."""
        super().__init__(
            f"GPU compute capability sm_{got[0]}{got[1]} below required sm_{required_major}0+"
        )
        self.got = got
        self.required_major = required_major
```

- [ ] **Step 4: Append to `src/kinoforge/core/config.py`**

```python
# Append near the existing SpandrelEngineConfig — same section.

_FLASHVSR_VALID_PRECISIONS = ("fp16", "fp32")
_FLASHVSR_VALID_TILE_SIZES = (0, 256, 384, 512, 768)
_FLASHVSR_WINDOW_MIN = 8
_FLASHVSR_WINDOW_MAX = 64
_FLASHVSR_VALID_SCHEMES = ("hf:", "http://", "https://", "civitai:", "civarchive:")


@dataclass
class FlashVSREngineConfig:
    """FlashVSR v1.1 engine params — validated at cfg-load-time.

    See docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §4.
    """

    weights_bundle: str
    precision: Literal["fp16", "fp32"] = "fp16"
    window_size: int = 24
    tile_size: int = 0
    long_video_mode: bool = False

    def __post_init__(self) -> None:
        if not any(self.weights_bundle.startswith(s) for s in _FLASHVSR_VALID_SCHEMES):
            raise ConfigError(
                f"flashvsr weights_bundle {self.weights_bundle!r}: unsupported "
                f"scheme (supported: {_FLASHVSR_VALID_SCHEMES})"
            )
        if self.precision not in _FLASHVSR_VALID_PRECISIONS:
            raise ConfigError(
                f"flashvsr precision {self.precision!r} not in "
                f"{_FLASHVSR_VALID_PRECISIONS}"
            )
        if not (_FLASHVSR_WINDOW_MIN <= self.window_size <= _FLASHVSR_WINDOW_MAX):
            raise ConfigError(
                f"flashvsr window_size {self.window_size} out of range "
                f"[{_FLASHVSR_WINDOW_MIN}, {_FLASHVSR_WINDOW_MAX}]"
            )
        if self.tile_size not in _FLASHVSR_VALID_TILE_SIZES:
            raise ConfigError(
                f"flashvsr tile_size {self.tile_size} not in "
                f"{_FLASHVSR_VALID_TILE_SIZES}"
            )


# Inside UpscaleConfig — extend the existing dataclass:
#   flashvsr: FlashVSREngineConfig | None = None
#
# Extend UpscaleConfig.__post_init__:
#   if self.engine == "flashvsr":
#       if self.flashvsr is None:
#           raise ConfigError("engine=flashvsr requires cfg.upscale.flashvsr block")
#       if self.scale_target.kind == "height":
#           raise ConfigError(
#               "engine=flashvsr: height-target scale not yet wired; use factor form"
#           )
#
# Extend Config.capability_key — inside the existing upscaler-branch match:
#   if self.upscale.engine == "flashvsr":
#       upscaler_precision = (
#           self.upscale.flashvsr.precision if self.upscale.flashvsr else ""
#       )
```

- [ ] **Step 5: Run tests, iterate to GREEN**

```bash
pixi run pytest tests/upscalers/flashvsr/test_config.py -v
```
Expected: 8 passed.

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
    src/kinoforge/core/errors.py \
    src/kinoforge/core/config.py \
    tests/upscalers/flashvsr/__init__.py \
    tests/upscalers/flashvsr/test_config.py
git add src/kinoforge/core/errors.py src/kinoforge/core/config.py tests/upscalers/flashvsr/
git commit -m "feat(errors,config): FlashVSR exceptions + FlashVSREngineConfig cfg-time validation

New in errors.py: BSACompileFailed, FlashVSRWeightsIncomplete, UnsupportedGpuArch.
New in config.py: FlashVSREngineConfig; UpscaleConfig.flashvsr; cfg-time rejects
height-target scale and missing flashvsr block; capability_key populates
upscaler_precision. 8 tests GREEN.

Ref: docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §4"
```

---

## Task 1: `_fetch_weights.py` + `weights_manifest.json`

**Goal:** Pod-safe CLI that fetches FlashVSR 4-file bundle (or 2-file lite bundle) from HF and verifies SHA256 against a shipped manifest.

**Files:**
- Create: `src/kinoforge/upscalers/flashvsr/__init__.py` — empty package marker for this task; overwritten by T4
- Create: `src/kinoforge/upscalers/flashvsr/_fetch_weights.py`
- Create: `src/kinoforge/upscalers/flashvsr/weights_manifest.json`
- Create: `tests/upscalers/flashvsr/test_fetch_weights.py`

**Acceptance Criteria:**
- [ ] CLI shape: `python -m kinoforge.upscalers.flashvsr._fetch_weights --bundle <ref> --dest <dir> --include-long-video {0,1}`.
- [ ] With `--include-long-video 0`: fetches 2 files (`diffusion_pytorch_model_streaming_dmd.safetensors`, `Wan2.1_VAE.pth`).
- [ ] With `--include-long-video 1`: fetches 4 files (adds `LQ_proj_in.ckpt`, `TCDecoder.ckpt`).
- [ ] Post-download SHA256 verified against `weights_manifest.json`; mismatch raises `FlashVSRWeightsIncomplete`.
- [ ] Module imports only stdlib + `hashlib` — no `kinoforge.core.registry` (pod-safe embed).
- [ ] Manifest ships in the package (`importlib.resources` locate).

**Verify:** `pixi run pytest tests/upscalers/flashvsr/test_fetch_weights.py -v` → 6 passed

**Steps:**

- [ ] **Step 1: Write failing tests — `tests/upscalers/flashvsr/test_fetch_weights.py`**

```python
"""FlashVSR _fetch_weights CLI: bundle selection + SHA256 verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kinoforge.core.errors import FlashVSRWeightsIncomplete
from kinoforge.upscalers.flashvsr import _fetch_weights as fw


BASE_FILES = (
    "diffusion_pytorch_model_streaming_dmd.safetensors",
    "Wan2.1_VAE.pth",
)
LONG_VIDEO_FILES = ("LQ_proj_in.ckpt", "TCDecoder.ckpt")


def _fake_bytes(name: str) -> bytes:
    """Deterministic per-file bytes for hash assertions."""
    return f"flashvsr::{name}".encode()


def _fake_sha(name: str) -> str:
    return hashlib.sha256(_fake_bytes(name)).hexdigest()


@pytest.fixture
def fake_manifest(monkeypatch, tmp_path: Path) -> Path:
    """Inject a manifest matching the deterministic _fake_bytes."""
    manifest = {
        name: {"sha256": _fake_sha(name)}
        for name in BASE_FILES + LONG_VIDEO_FILES
    }
    p = tmp_path / "weights_manifest.json"
    p.write_text(json.dumps(manifest))
    monkeypatch.setattr(fw, "_load_manifest", lambda: manifest)
    return p


def test_lite_bundle_fetches_two_files(
    monkeypatch, tmp_path: Path, fake_manifest: Path
) -> None:
    """RED: --include-long-video 0 stops at BASE_FILES.

    Bug caught: off-by-one on the include flag → fetches all 4 files always,
    wastes ~4 GB HF pull on cold boot.
    """
    calls: list[str] = []

    def fake_download(ref: str, filename: str, dest: Path) -> Path:
        calls.append(filename)
        p = dest / filename
        p.write_bytes(_fake_bytes(filename))
        return p

    monkeypatch.setattr(fw, "_download_one", fake_download)
    rc = fw.main([
        "--bundle", "hf:JunhaoZhuang/FlashVSR-v1.1",
        "--dest", str(tmp_path),
        "--include-long-video", "0",
    ])
    assert rc == 0
    assert set(calls) == set(BASE_FILES)


def test_full_bundle_fetches_four_files(
    monkeypatch, tmp_path: Path, fake_manifest: Path
) -> None:
    """RED: --include-long-video 1 fetches BASE + LONG_VIDEO."""
    calls: list[str] = []

    def fake_download(ref: str, filename: str, dest: Path) -> Path:
        calls.append(filename)
        p = dest / filename
        p.write_bytes(_fake_bytes(filename))
        return p

    monkeypatch.setattr(fw, "_download_one", fake_download)
    rc = fw.main([
        "--bundle", "hf:JunhaoZhuang/FlashVSR-v1.1",
        "--dest", str(tmp_path),
        "--include-long-video", "1",
    ])
    assert rc == 0
    assert set(calls) == set(BASE_FILES + LONG_VIDEO_FILES)


def test_sha_mismatch_raises_incomplete(
    monkeypatch, tmp_path: Path, fake_manifest: Path
) -> None:
    """RED: post-download hash mismatch raises FlashVSRWeightsIncomplete.

    Bug caught: silent tolerance of corrupted download → runtime tensor
    shape errors that mask themselves as generic torch failures.
    """
    def bad_download(ref: str, filename: str, dest: Path) -> Path:
        p = dest / filename
        p.write_bytes(b"CORRUPT")
        return p

    monkeypatch.setattr(fw, "_download_one", bad_download)
    with pytest.raises(FlashVSRWeightsIncomplete):
        fw.main([
            "--bundle", "hf:JunhaoZhuang/FlashVSR-v1.1",
            "--dest", str(tmp_path),
            "--include-long-video", "0",
        ])


def test_module_does_not_import_kinoforge_core_registry() -> None:
    """RED: pod-safe import surface — registry must NOT be pulled.

    Bug caught: accidental `from kinoforge.core.registry import ...` reintroduces
    the P2-era embed-tree bloat that busted the 64 KB pod env-var ceiling.
    """
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c",
         "import sys; import kinoforge.upscalers.flashvsr._fetch_weights; "
         "assert 'kinoforge.core.registry' not in sys.modules, "
         "'registry leaked into pod-safe module'"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_unknown_scheme_rejected(monkeypatch, tmp_path: Path) -> None:
    """RED: bundle ref with unknown scheme fails resolver.

    Bug caught: `gs://bucket/...` silently attempted → HTTP resolver
    returns confusing 400.
    """
    with pytest.raises(ValueError, match="unsupported"):
        fw.main([
            "--bundle", "gs://bucket/flashvsr",
            "--dest", str(tmp_path),
            "--include-long-video", "0",
        ])


def test_manifest_shipped_in_package() -> None:
    """RED: real manifest is packaged.

    Bug caught: forgetting to add weights_manifest.json to package
    manifest → pod runs but `_load_manifest()` raises FileNotFoundError.
    """
    manifest = fw._load_manifest()
    for name in BASE_FILES + LONG_VIDEO_FILES:
        assert name in manifest
        assert isinstance(manifest[name]["sha256"], str)
        assert len(manifest[name]["sha256"]) == 64  # sha256 hex length
```

- [ ] **Step 2: Confirm RED**

```bash
pixi run pytest tests/upscalers/flashvsr/test_fetch_weights.py -v
```
Expected: all 6 fail with `ImportError: kinoforge.upscalers.flashvsr._fetch_weights`.

- [ ] **Step 3: Create `src/kinoforge/upscalers/flashvsr/__init__.py`**

Minimal placeholder (T4 overwrites):

```python
"""FlashVSR upscaler package (T1 placeholder — full engine wiring in T4)."""
```

- [ ] **Step 4: Create `src/kinoforge/upscalers/flashvsr/weights_manifest.json`**

Real SHA256s pulled from the current HF revision at plan-execute time. Fetch via:

```bash
HF_TOKEN=$(pixi run python -c 'import os; print(os.environ["HF_TOKEN"])') \
  pixi run python -c "
from huggingface_hub import HfApi
api = HfApi()
info = api.repo_info('JunhaoZhuang/FlashVSR-v1.1', files_metadata=True)
for s in info.siblings:
    if s.rfilename in (
        'diffusion_pytorch_model_streaming_dmd.safetensors',
        'Wan2.1_VAE.pth', 'LQ_proj_in.ckpt', 'TCDecoder.ckpt',
    ):
        print(f'  {s.rfilename!r}: {{\"sha256\": {s.lfs.sha256!r}}},')
"
```

File shape:

```json
{
  "diffusion_pytorch_model_streaming_dmd.safetensors": {"sha256": "<real-64-hex>"},
  "Wan2.1_VAE.pth":                                    {"sha256": "<real-64-hex>"},
  "LQ_proj_in.ckpt":                                   {"sha256": "<real-64-hex>"},
  "TCDecoder.ckpt":                                    {"sha256": "<real-64-hex>"}
}
```

- [ ] **Step 5: Create `src/kinoforge/upscalers/flashvsr/_fetch_weights.py`**

```python
r"""CLI — fetch FlashVSR v1.1 weights bundle, verify SHA256 against manifest.

Invoked in the pod bootstrap:

    python -m kinoforge.upscalers.flashvsr._fetch_weights \
        --bundle hf:JunhaoZhuang/FlashVSR-v1.1 \
        --dest /workspace/models/flashvsr \
        --include-long-video 0

Pod-safe: does NOT import kinoforge.core.registry / interfaces / adapters —
runs with only `kinoforge.upscalers.flashvsr` + `kinoforge.core.errors`
embedded (mirrors spandrel _fetch_weights lesson from P2).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request
from importlib import resources
from pathlib import Path
from typing import Any

from kinoforge.core.errors import FlashVSRWeightsIncomplete

_BASE_FILES = (
    "diffusion_pytorch_model_streaming_dmd.safetensors",
    "Wan2.1_VAE.pth",
)
_LONG_VIDEO_FILES = ("LQ_proj_in.ckpt", "TCDecoder.ckpt")

_HF_REF_RE = re.compile(r"^hf:([^/]+/[^/]+)$")
_HF_BASE = "https://huggingface.co"


def _load_manifest() -> dict[str, dict[str, str]]:
    """Read the shipped weights_manifest.json."""
    with resources.files("kinoforge.upscalers.flashvsr").joinpath(
        "weights_manifest.json"
    ).open("r") as f:
        return json.load(f)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_one(bundle_ref: str, filename: str, dest_dir: Path) -> Path:
    """Fetch one file from the bundle ref into dest_dir; return the path.

    Test seam — patched to a deterministic stub in unit tests.
    """
    if bundle_ref.startswith("hf:"):
        m = _HF_REF_RE.match(bundle_ref)
        if m is None:
            raise ValueError(f"malformed hf bundle ref: {bundle_ref!r}")
        repo = m.group(1)
        url = f"{_HF_BASE}/{repo}/resolve/main/{filename}"
        token = os.environ.get("HF_TOKEN")
        headers: dict[str, str] = (
            {"Authorization": f"Bearer {token}"} if token else {}
        )
    elif bundle_ref.startswith(("http://", "https://")):
        url = bundle_ref.rstrip("/") + "/" + filename
        headers = {}
    else:
        raise ValueError(
            f"unsupported bundle scheme: {bundle_ref!r} "
            "(supported: hf:, http(s)://)"
        )

    target = dest_dir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    headers.setdefault("User-Agent", "kinoforge-flashvsr-fetch/0.1")
    req = urllib.request.Request(url, headers=headers)  # noqa: S310
    tmp = target.with_suffix(target.suffix + ".partial")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp, tmp.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out)
        tmp.replace(target)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    return target


def _verify(path: Path, expected_sha: str) -> None:
    got = _sha256(path)
    if got != expected_sha:
        raise FlashVSRWeightsIncomplete(
            filename=path.name, got_sha256=got, want_sha256=expected_sha
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kinoforge.upscalers.flashvsr._fetch_weights"
    )
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--include-long-video", required=True, choices=("0", "1"))
    args = parser.parse_args(argv)

    files = list(_BASE_FILES)
    if args.include_long_video == "1":
        files += list(_LONG_VIDEO_FILES)

    args.dest.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    for name in files:
        path = _download_one(args.bundle, name, args.dest)
        _verify(path, manifest[name]["sha256"])
        print(f"wrote {path} sha256={manifest[name]['sha256'][:8]}")
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    raise SystemExit(main())
```

- [ ] **Step 6: Run tests → GREEN**

```bash
pixi run pytest tests/upscalers/flashvsr/test_fetch_weights.py -v
```
Expected: 6 passed.

- [ ] **Step 7: Register manifest in package data**

If `pyproject.toml` does not already include `*.json` in the flashvsr package, add:

```toml
# In pyproject.toml [tool.setuptools.package-data] or equivalent
"kinoforge.upscalers.flashvsr" = ["weights_manifest.json"]
```

- [ ] **Step 8: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
    src/kinoforge/upscalers/flashvsr/__init__.py \
    src/kinoforge/upscalers/flashvsr/_fetch_weights.py \
    src/kinoforge/upscalers/flashvsr/weights_manifest.json \
    tests/upscalers/flashvsr/test_fetch_weights.py \
    pyproject.toml pixi.lock
git add src/kinoforge/upscalers/flashvsr/ tests/upscalers/flashvsr/test_fetch_weights.py \
    pyproject.toml pixi.lock
git commit -m "feat(flashvsr): _fetch_weights CLI + manifest SHA256 verification

Pod-safe: no kinoforge.core.registry import. Fetches 2-file lite or 4-file
long-video bundle from hf:JunhaoZhuang/FlashVSR-v1.1; verifies each file
sha256 against shipped weights_manifest.json; mismatch raises
FlashVSRWeightsIncomplete. 6 tests GREEN.

Ref: docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §5.4"
```

---

## Task 2: `_runtime.py` — FlashVSRRuntime

**Goal:** Lazy-import wrapper around upstream `flashvsr.pipeline.StreamingDMDPipeline` that satisfies the LRU `LoadedModel` contract (`.to()`, `.vram_bytes`, `.upscale()`).

**Files:**
- Create: `src/kinoforge/upscalers/flashvsr/_runtime.py`
- Create: `tests/upscalers/flashvsr/test_runtime.py`

**Acceptance Criteria:**
- [ ] `FlashVSRRuntime(weights_dir, precision, window_size, tile_size, long_video_mode)` constructs; lazy-imports `flashvsr.pipeline`.
- [ ] `upscale(video_path, scale, params)` returns Path with suffix `.flashvsr.mp4`.
- [ ] `scale.kind == "height"` raises `NotYetImplementedError`.
- [ ] `scale.value != self._native_scale` raises `UnsupportedScaleError`.
- [ ] `params.get("prompt")` logs a warning; does not raise.
- [ ] `.to("cuda")` moves the wrapped pipe.
- [ ] `vram_bytes == 8 * 1024**3` (conservative — 2.6 GB weights + streaming state).

**Verify:** `pixi run pytest tests/upscalers/flashvsr/test_runtime.py -v` → 6 passed

**Steps:**

- [ ] **Step 1: Write failing tests — `tests/upscalers/flashvsr/test_runtime.py`**

```python
"""FlashVSRRuntime: LRU contract + scale validation + prompt-ignore behavior."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget


class _StubPipe:
    """Duck-types StreamingDMDPipeline for unit tests."""

    def __init__(self, native_scale: float = 2.0) -> None:
        self.scale = native_scale
        self._device = "cpu"
        self.stream_calls: list[dict[str, Any]] = []

    @classmethod
    def from_pretrained(cls, weights_dir: str, **kwargs: Any) -> "_StubPipe":  # noqa: ARG003
        return cls()

    def stream_upscale(
        self,
        input_path: str,
        output_path: str,
        window_size: int,
        tile: int | None,
    ) -> None:
        self.stream_calls.append({
            "input_path": input_path,
            "output_path": output_path,
            "window_size": window_size,
            "tile": tile,
        })
        Path(output_path).write_bytes(b"MP4-STUB")

    def to(self, device: str) -> "_StubPipe":
        self._device = device
        return self


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a stub `flashvsr.pipeline` module."""
    import sys
    import types

    mod = types.ModuleType("flashvsr.pipeline")
    mod.StreamingDMDPipeline = _StubPipe  # type: ignore[attr-defined]
    pkg = types.ModuleType("flashvsr")
    pkg.pipeline = mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "flashvsr", pkg)
    monkeypatch.setitem(sys.modules, "flashvsr.pipeline", mod)


def test_construct_lazy_imports_flashvsr(stub_pipeline, tmp_path: Path) -> None:
    """RED: constructor pulls flashvsr only inside __init__, not at module load.

    Bug caught: top-level `import flashvsr` in _runtime.py breaks the
    kinoforge-default env (flashvsr lives only in the live-flashvsr feature env).
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(
        weights_dir=tmp_path,
        precision="fp16",
        window_size=24,
        tile_size=0,
        long_video_mode=False,
    )
    assert rt._native_scale == 2.0
    assert rt.vram_bytes == 8 * 1024**3


def test_upscale_produces_flashvsr_mp4_suffix(
    stub_pipeline, tmp_path: Path
) -> None:
    """RED: upscale returns Path with .flashvsr.mp4 suffix.

    Bug caught: sibling-suffix collision with .upscaled.mp4 (spandrel's
    output naming) → later stage overwrites the earlier stage's artifact.
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    rt = FlashVSRRuntime(tmp_path, "fp16", 24, 0, False)
    out = rt.upscale(src, ScaleTarget(kind="factor", value=2.0), {})
    assert out.name == "in.flashvsr.mp4"
    assert out.exists()


def test_upscale_height_target_raises(stub_pipeline, tmp_path: Path) -> None:
    """RED: kind=height is not yet supported at runtime either.

    Bug caught: cfg-time gate bypassed via direct runtime call →
    silently proceeds and produces a wrong-dimension output.
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "fp16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    with pytest.raises(NotYetImplementedError):
        rt.upscale(src, ScaleTarget(kind="height", value=1080), {})


def test_upscale_mismatched_scale_raises(stub_pipeline, tmp_path: Path) -> None:
    """RED: cfg scale must match checkpoint's native scale.

    Bug caught: --scale 4x against a 2x checkpoint silently runs and
    produces 2x output labeled as 4x.
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "fp16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    with pytest.raises(UnsupportedScaleError):
        rt.upscale(src, ScaleTarget(kind="factor", value=4.0), {})


def test_upscale_ignores_prompt_with_warning(
    stub_pipeline, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """RED: params['prompt'] logs a warning; does NOT raise.

    Bug caught: raising on prompt breaks multi-stage cfgs that pass
    the Wan generation prompt through job.params for observability.
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "fp16", 24, 0, False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"SRC")
    with caplog.at_level(logging.WARNING):
        rt.upscale(
            src, ScaleTarget(kind="factor", value=2.0),
            {"prompt": "a field of wildflowers"},
        )
    assert any(
        "prompt" in r.message and "ignored" in r.message
        for r in caplog.records
    )


def test_to_moves_underlying_pipe(stub_pipeline, tmp_path: Path) -> None:
    """RED: .to("cuda") delegates to the wrapped pipe.

    Bug caught: no-op `.to()` implementation — LRU thinks the model is
    on CUDA but inference runs on CPU (silent, catastrophic slowdown).
    """
    from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

    rt = FlashVSRRuntime(tmp_path, "fp16", 24, 0, False)
    rt.to("cuda")
    assert rt._pipe._device == "cuda"  # noqa: SLF001
```

- [ ] **Step 2: Confirm RED** → `ImportError: FlashVSRRuntime`

- [ ] **Step 3: Write `src/kinoforge/upscalers/flashvsr/_runtime.py`**

```python
"""FlashVSRRuntime — streaming diffusion VSR wrapper around StreamingDMDPipeline.

Lazy-imports `flashvsr.pipeline` so the kinoforge-default env doesn't need
FlashVSR deps installed. Satisfies the LRU LoadedModel contract used by
wan_t2v_server's model registry via the `flashvsr-*` slug prefix.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget

_log = logging.getLogger(__name__)


class FlashVSRRuntime:
    """Loads FlashVSR weights + runs streaming VSR through the wan_t2v_server.

    Args:
        weights_dir: Local dir holding the 2-file or 4-file bundle.
        precision: ``"fp16"`` (DMD-native) or ``"fp32"``.
        window_size: Streaming attention window (frames).
        tile_size: 0 = whole-frame; >0 = spatial tiling for VRAM headroom.
        long_video_mode: ``True`` enables LCSA + TCDecoder (needs 4-file bundle).

    Raises:
        ImportError: `flashvsr` package not installed.
    """

    def __init__(
        self,
        weights_dir: Path,
        precision: Literal["fp16", "fp32"],
        window_size: int,
        tile_size: int,
        long_video_mode: bool,
    ) -> None:
        import torch
        from flashvsr.pipeline import StreamingDMDPipeline  # lazy import

        dtype = torch.float16 if precision == "fp16" else torch.float32
        self._pipe = StreamingDMDPipeline.from_pretrained(
            str(weights_dir),
            torch_dtype=dtype,
            enable_lcsa=long_video_mode,
        )
        self._window = window_size
        self._tile = tile_size
        # Native scale from checkpoint. Attribute name pinned by
        # StreamingDMDPipeline; if upstream renames to `.upscale_factor`
        # adjust here and update test_runtime.py's stub.
        self._native_scale: float = float(self._pipe.scale)

    def upscale(
        self, video_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        """Run streaming VSR on ``video_path``; return sibling .flashvsr.mp4."""
        if scale.kind == "height":
            raise NotYetImplementedError(
                f"flashvsr: height-target scale ({int(scale.value)}p) not yet "
                "wired; use --scale Nx"
            )
        if scale.value != self._native_scale:
            raise UnsupportedScaleError(scale=scale, engine_name="flashvsr")
        if params.get("prompt"):
            _log.warning(
                "flashvsr: params['prompt'] ignored — model has no text encoder"
            )
        out = video_path.with_suffix(".flashvsr.mp4")
        self._pipe.stream_upscale(
            input_path=str(video_path),
            output_path=str(out),
            window_size=self._window,
            tile=self._tile or None,
        )
        return out

    def to(self, device: str) -> None:
        """LRU eviction hook — move underlying nn.Modules between cuda/cpu."""
        self._pipe.to(device)

    @property
    def vram_bytes(self) -> int:
        """Wan 2.1 1.3B backbone fp16 ≈ 2.6 GB + streaming state ≈ 4-8 GB peak."""
        return int(8 * 1024**3)
```

- [ ] **Step 4: Run tests → GREEN** (6 passed)

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
    src/kinoforge/upscalers/flashvsr/_runtime.py \
    tests/upscalers/flashvsr/test_runtime.py
git add src/kinoforge/upscalers/flashvsr/_runtime.py \
    tests/upscalers/flashvsr/test_runtime.py
git commit -m "feat(flashvsr): FlashVSRRuntime wraps StreamingDMDPipeline

Lazy-imports flashvsr.pipeline; satisfies LRU LoadedModel contract
(.to, .vram_bytes, .upscale). Refuses height-target scale + mismatched
factor; ignores params['prompt'] with warning. 6 tests GREEN.

Ref: docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §5.1"
```

---

## Task 3: `_engine.py` — FlashVSREngine HTTP dispatch

**Goal:** Client-side `UpscalerEngine` implementer that talks to `wan_t2v_server`'s `/upload`, `/upscale`, `/upscale/status/{id}` endpoints. Mirrors `SpandrelEngine` structure line-for-line.

**Files:**
- Create: `src/kinoforge/upscalers/flashvsr/_engine.py`
- Create: `tests/upscalers/flashvsr/test_engine.py`

**Acceptance Criteria:**
- [ ] `FlashVSREngine.render_provision(cfg)` emits: SM80+ guard with exit 87; `TORCH_EXTENSIONS_DIR=/workspace/.cache/bsa`; `MAX_JOBS=4`; BSA install; FlashVSR install; `_fetch_weights` call with `--include-long-video {0,1}`; `HF_HUB_OFFLINE=1` tail. Order preserved.
- [ ] `FlashVSREngine.model_identity(cfg)` → `f"flashvsr-wan21-{precision}"`.
- [ ] `FlashVSREngine.upscale(instance, job, cfg)`: POST `/upscale` with `engine: "flashvsr"` + full `flashvsr` block; polls `/upscale/status/{id}`; returns `UpscaleResult`.
- [ ] `job.source.uri` starting with `file://` or `/` triggers `_upload_source(instance, local_path)` before submit.
- [ ] `job.scale.kind == "height"` raised as `NotYetImplementedError` from `validate_spec`.
- [ ] Provision script body ≤ 12 KB (bootstrap env-var headroom).
- [ ] `env_required == ["HF_TOKEN"]`.

**Verify:** `pixi run pytest tests/upscalers/flashvsr/test_engine.py -v` → 10 passed

**Steps:**

- [ ] **Step 1: Write failing tests — `tests/upscalers/flashvsr/test_engine.py`**

```python
"""FlashVSREngine: render_provision layout + HTTP dispatch shape."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.interfaces import Artifact, Instance, UpscaleJob
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.upscalers.flashvsr._engine import FlashVSREngine


def _cfg(precision: str = "fp16", long_video: bool = False) -> dict[str, Any]:
    return {
        "upscale": {
            "engine": "flashvsr",
            "scale": "2x",
            "flashvsr": {
                "weights_bundle": "hf:JunhaoZhuang/FlashVSR-v1.1",
                "precision": precision,
                "window_size": 24,
                "tile_size": 0,
                "long_video_mode": long_video,
            },
        }
    }


def test_model_identity_shape() -> None:
    """RED: three-token slug shape (server parse contract).

    Bug caught: emitting `flashvsr-fp16` (two tokens) breaks the server's
    `parts[-2], parts[-1]` slug parser.
    """
    e = FlashVSREngine()
    assert e.model_identity(_cfg()) == "flashvsr-wan21-fp16"
    assert e.model_identity(_cfg(precision="fp32")) == "flashvsr-wan21-fp32"


def test_render_provision_step_order() -> None:
    """RED: SM80+ guard first, BSA before FlashVSR, HF_HUB_OFFLINE tail.

    Bug caught: FlashVSR pip-installed before BSA — its setup.py may
    shadow-import a stub kernel and never notice BSA is missing.
    """
    e = FlashVSREngine()
    rp = e.render_provision(_cfg())
    script = rp.script

    guard_pos = script.find("torch.cuda.get_device_capability")
    bsa_pos = script.find("Block-Sparse-Attention")
    fvsr_pos = script.find("OpenImagingLab/FlashVSR")
    fetch_pos = script.find("_fetch_weights")
    offline_pos = script.find("HF_HUB_OFFLINE=1")

    assert 0 <= guard_pos < bsa_pos < fvsr_pos < fetch_pos < offline_pos


def test_render_provision_has_sm80_exit_87() -> None:
    """RED: guard uses exit 87 (documented UnsupportedGpuArch code).

    Bug caught: exit 1 conflates with generic pod-boot failure.
    """
    e = FlashVSREngine()
    rp = e.render_provision(_cfg())
    assert "|| exit 87" in rp.script


def test_render_provision_pins_torch_extensions_dir_and_max_jobs() -> None:
    """RED: TORCH_EXTENSIONS_DIR + MAX_JOBS both exported before BSA install.

    Bug caught: missing MAX_JOBS → 16-core cheap pod OOMs the nvcc compile
    around fan-out; missing TORCH_EXTENSIONS_DIR → .so lands in $HOME (lost
    on warm-reuse cycle boundary).
    """
    e = FlashVSREngine()
    rp = e.render_provision(_cfg())
    ext_pos = rp.script.find("TORCH_EXTENSIONS_DIR=/workspace/.cache/bsa")
    maxj_pos = rp.script.find("MAX_JOBS=4")
    bsa_pos = rp.script.find("Block-Sparse-Attention")
    assert 0 <= ext_pos < bsa_pos
    assert 0 <= maxj_pos < bsa_pos


def test_render_provision_threads_include_long_video_flag() -> None:
    """RED: long_video_mode cfg → --include-long-video 1 in the fetch call."""
    e = FlashVSREngine()
    rp_lite = e.render_provision(_cfg(long_video=False))
    rp_full = e.render_provision(_cfg(long_video=True))
    assert "--include-long-video 0" in rp_lite.script
    assert "--include-long-video 1" in rp_full.script


def test_render_provision_env_required_and_size() -> None:
    """RED: HF_TOKEN required; script fits in bootstrap env ceiling.

    Bug caught: script size drift busts the 64 KB RunPod env-var ceiling
    (P2 discovery); test enforces < 12 KB with generous headroom.
    """
    e = FlashVSREngine()
    rp = e.render_provision(_cfg())
    assert rp.env_required == ["HF_TOKEN"]
    assert len(rp.script.encode()) < 12 * 1024


def test_validate_spec_rejects_height() -> None:
    """RED: engine-side rejection for height target (defense-in-depth)."""
    e = FlashVSREngine()
    with pytest.raises(NotYetImplementedError):
        e.validate_spec(UpscaleJob(
            source=Artifact(uri="file:///tmp/in.mp4"),
            scale=ScaleTarget(kind="height", value=1080),
        ))


def test_upscale_uploads_local_source_before_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: `file://` source triggers _upload_source before /upscale POST.

    Bug caught: skipping upload → pod's _download_to_local_temp reads a
    path that doesn't exist on the pod (P2 T15/T16 blocker).
    """
    inst = Instance(
        id="pod-abc", provider="runpod", instance_type="A100-80GB",
        endpoints={"8000": "http://pod-abc.runpod.io"},
    )
    e = FlashVSREngine()

    upload_calls: list[Path] = []

    def fake_upload(instance: Instance, path: Path) -> str:
        upload_calls.append(path)
        return "file:///workspace/uploads/abc123.mp4"

    monkeypatch.setattr(e, "_upload_source", fake_upload)

    submit_body: dict[str, Any] = {}

    def fake_http(*, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "POST" and url.endswith("/upscale"):
            submit_body.update(payload or {})
            return {"job_id": "j-1"}
        # status poll → return done immediately
        return {
            "state": "done",
            "result": {
                "filename": "out.mp4", "sha256": "0"*64, "size": 100,
                "input_resolution": [720, 480],
                "output_resolution": [1440, 960],
                "engine_meta": {},
            },
        }

    monkeypatch.setattr(
        "kinoforge.upscalers.flashvsr._engine._http_json", fake_http
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)

    e.upscale(
        instance=inst,
        job=UpscaleJob(
            source=Artifact(uri="file:///workspace/output/in.mp4"),
            scale=ScaleTarget(kind="factor", value=2.0),
        ),
        cfg=_cfg(),
    )

    assert len(upload_calls) == 1
    assert submit_body["engine"] == "flashvsr"
    assert submit_body["flashvsr"]["precision"] == "fp16"
    assert submit_body["source_url"].startswith("file:///workspace/uploads/")


def test_upscale_polls_until_done_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: polls status until state == 'done', returns UpscaleResult with dims.

    Bug caught: single-shot status poll silently accepts state='running'
    as 'done' when the response schema drifts.
    """
    inst = Instance(
        id="pod-abc", provider="runpod", instance_type="A100-80GB",
        endpoints={"8000": "http://pod-abc.runpod.io"},
    )
    e = FlashVSREngine()

    poll_count = {"n": 0}

    def fake_http(*, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "POST":
            return {"job_id": "j-1"}
        poll_count["n"] += 1
        if poll_count["n"] < 3:
            return {"state": "running", "progress": 0.5}
        return {
            "state": "done",
            "result": {
                "filename": "out.mp4", "sha256": "0"*64, "size": 200,
                "input_resolution": [1280, 720],
                "output_resolution": [2560, 1440],
                "engine_meta": {"elapsed_s_gpu": 12.5},
            },
        }

    monkeypatch.setattr(
        "kinoforge.upscalers.flashvsr._engine._http_json", fake_http
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    # Skip actual upload
    monkeypatch.setattr(e, "_upload_source", lambda *a, **k: "file:///workspace/uploads/x.mp4")

    result = e.upscale(
        instance=inst,
        job=UpscaleJob(
            source=Artifact(uri="file:///tmp/in.mp4"),
            scale=ScaleTarget(kind="factor", value=2.0),
        ),
        cfg=_cfg(),
    )
    assert result.input_resolution == (1280, 720)
    assert result.output_resolution == (2560, 1440)
    assert poll_count["n"] >= 3


def test_upscale_raises_on_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """RED: state=='error' → UpscaleFailed with server_error message.

    Bug caught: silent swallow of server error → orchestrator treats
    an empty result as success and sinks a zero-byte MP4.
    """
    from kinoforge.core.errors import UpscaleFailed

    inst = Instance(
        id="pod-abc", provider="runpod", instance_type="A100-80GB",
        endpoints={"8000": "http://pod-abc.runpod.io"},
    )
    e = FlashVSREngine()

    def fake_http(*, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if method == "POST":
            return {"job_id": "j-1"}
        return {"state": "error", "error": "CUDA OOM in stream_upscale"}

    monkeypatch.setattr(
        "kinoforge.upscalers.flashvsr._engine._http_json", fake_http
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(e, "_upload_source", lambda *a, **k: "file:///workspace/uploads/x.mp4")

    with pytest.raises(UpscaleFailed, match="CUDA OOM"):
        e.upscale(
            instance=inst,
            job=UpscaleJob(
                source=Artifact(uri="file:///tmp/in.mp4"),
                scale=ScaleTarget(kind="factor", value=2.0),
            ),
            cfg=_cfg(),
        )
```

- [ ] **Step 2: Confirm RED** → 10 fails on `ImportError: FlashVSREngine`.

- [ ] **Step 3: Write `src/kinoforge/upscalers/flashvsr/_engine.py`**

Mirror `spandrel/_engine.py`'s HTTP dispatch (including `_upload_source`, `_put_upload`, `_base_url`, `_http_json`) — same shape, swap `spandrel` for `flashvsr` in method_identity, UA string, and payload block. Full content:

```python
"""FlashVSREngine — HTTP-aware UpscalerEngine impl backed by FlashVSR runtime.

Talks to wan_t2v_server's /upscale + /upscale/status/{id} + /upload endpoints.
Reuses :func:`kinoforge.engines._proxy_retry.retry_proxy_call` for RunPod
proxy startup-window 404/502 tolerance.
"""

from __future__ import annotations

import hashlib
import json as _json
import time
import urllib.request
from pathlib import Path
from typing import IO, Any, cast
from urllib.error import HTTPError

from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import (
    NotYetImplementedError,
    UploadIntegrityError,
    UpscaleFailed,
)
from kinoforge.core.interfaces import (
    Artifact,
    Instance,
    RenderedProvision,
    UpscaleJob,
    UpscalerEngine,
    UpscaleResult,
)
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.engines._proxy_retry import retry_proxy_call

_DEFAULT_SERVER_PORT = "8000"


class FlashVSREngine(UpscalerEngine):
    """FlashVSR v1.1 streaming diffusion video upscaler."""

    name = "flashvsr"
    requires_compute = True
    requires_local_weights = True
    supported_scales: tuple[ScaleTarget, ...] = ()  # runtime declares at load

    def validate_spec(self, job: UpscaleJob) -> None:
        """Refuse height-target scales (spec §2 non-goal)."""
        if job.scale.kind == "height":
            raise NotYetImplementedError(
                f"flashvsr does not support height-target scale "
                f"({int(job.scale.value)}p); use --scale Nx"
            )

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return ``flashvsr-wan21-<precision>`` slug for the server LRU."""
        try:
            block = cast(
                dict[str, Any], cast(dict[str, Any], cfg["upscale"])["flashvsr"]
            )
            precision = str(block["precision"])
            return f"flashvsr-wan21-{precision}"
        except (KeyError, TypeError):
            return ""

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        """Emit BSA compile + FlashVSR install + weights fetch + hermetic flip."""
        block = cast(dict[str, Any], cast(dict[str, Any], cfg["upscale"])["flashvsr"])
        bundle = str(block["weights_bundle"])
        long_video = "1" if block.get("long_video_mode") else "0"
        script = "".join([
            "set -euo pipefail\n",
            'python -c "import torch; '
            "assert torch.cuda.get_device_capability()[0] >= 8, "
            "f'flashvsr: BSA needs SM80+, got {torch.cuda.get_device_capability()}'"
            '" || exit 87\n',
            "export TORCH_EXTENSIONS_DIR=/workspace/.cache/bsa\n",
            "export MAX_JOBS=4\n",
            'mkdir -p "$TORCH_EXTENSIONS_DIR"\n',
            'pip install '
            '"git+https://github.com/mit-han-lab/Block-Sparse-Attention@main" '
            '--no-build-isolation --no-cache-dir\n',
            'pip install '
            '"git+https://github.com/OpenImagingLab/FlashVSR@v1.1" '
            '"imageio[ffmpeg]>=2.34"\n',
            f"python -m kinoforge.upscalers.flashvsr._fetch_weights "
            f"--bundle {bundle} --dest /workspace/models/flashvsr "
            f"--include-long-video {long_video}\n",
            "export HF_HUB_OFFLINE=1\n",
        ])
        return RenderedProvision(
            script=script,
            run_cmd=[], image="", ports=[],
            env_required=["HF_TOKEN"],
        )

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """No-op — work captured in :meth:`render_provision`."""
        del instance, cfg, cancel_token

    def upscale(
        self,
        instance: Instance | None,
        job: UpscaleJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> UpscaleResult:
        """POST /upscale, poll /upscale/status/{id}, return UpscaleResult."""
        self.validate_spec(job)
        if instance is None:
            raise ValueError("FlashVSREngine requires a compute instance")
        base = self._base_url(instance)

        source_uri = job.source.uri
        if source_uri.startswith("file://") or source_uri.startswith("/"):
            local_path = Path(source_uri.removeprefix("file://"))
            source_uri = self._upload_source(instance, local_path)

        block = cast(
            dict[str, Any],
            cast(dict[str, Any], cfg.get("upscale", {})).get("flashvsr", {}),
        )
        submit_payload = {
            "source_url": source_uri,
            "source_filename": source_uri.rsplit("/", 1)[-1] or "in.mp4",
            "scale": f"{job.scale.value:g}x",
            "engine": "flashvsr",
            "flashvsr": block,
        }
        submit_resp = retry_proxy_call(
            label="flashvsr.submit",
            url=f"{base}/upscale",
            fn=lambda: _http_json(
                method="POST", url=f"{base}/upscale", payload=submit_payload
            ),
            sleep=time.sleep,
        )
        job_id: str = submit_resp["job_id"]

        t0 = time.monotonic()
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_set()
            status = retry_proxy_call(
                label="flashvsr.status",
                url=f"{base}/upscale/status/{job_id}",
                fn=lambda: _http_json(
                    method="GET", url=f"{base}/upscale/status/{job_id}"
                ),
                sleep=time.sleep,
            )
            state = status["state"]
            if state == "done":
                result = status["result"]
                return UpscaleResult(
                    artifact=Artifact(
                        uri=f"{base}/artifacts/{result['filename']}",
                        sha256=result["sha256"],
                        size=result["size"],
                    ),
                    input_resolution=tuple(result["input_resolution"]),
                    output_resolution=tuple(result["output_resolution"]),
                    elapsed_s=time.monotonic() - t0,
                    engine_meta=result.get("engine_meta", {}),
                )
            if state == "error":
                raise UpscaleFailed(job_id=job_id, server_error=status.get("error", ""))
            time.sleep(2.0)

    # _put_upload, _upload_source, _base_url — copy verbatim from
    # spandrel/_engine.py, changing UA to "kinoforge-flashvsr/0.1".

    def _put_upload(
        self,
        url: str,
        data: IO[bytes],
        headers: dict[str, str],
        timeout: int,
    ) -> dict[str, Any]:
        req = urllib.request.Request(url, data=data, method="PUT", headers=headers)  # noqa: S310
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return cast(dict[str, Any], _json.loads(resp.read().decode("utf-8")))

    def _upload_source(self, instance: Instance, local_path: Path) -> str:
        body = local_path.read_bytes()
        local_sha = hashlib.sha256(body).hexdigest()
        short = local_sha[:8]
        url = f"{self._base_url(instance)}/upload"
        headers = {
            "Content-Type": "video/mp4",
            "X-Filename": f"{short}.mp4",
            "Content-Length": str(len(body)),
            "User-Agent": "kinoforge-flashvsr/0.1",
        }

        last_error: HTTPError | None = None
        payload: dict[str, Any] | None = None
        for attempt in range(2):
            with local_path.open("rb") as fobj:
                try:
                    payload = self._put_upload(url, fobj, headers, timeout=600)
                    last_error = None
                    break
                except HTTPError as exc:
                    last_error = exc
                    if exc.code == 502 and attempt == 0:
                        continue
                    raise
        if payload is None:
            raise RuntimeError(
                f"_upload_source loop completed without payload (last_error={last_error!r})"
            )

        server_sha = str(payload.get("sha256", ""))
        if server_sha != local_sha:
            raise UploadIntegrityError(
                local_sha256=local_sha,
                server_sha256=server_sha,
                bytes_sent=len(body),
            )
        return f"file://{payload['path']}"

    @staticmethod
    def _base_url(instance: Instance) -> str:
        endpoints = instance.endpoints or {}
        url = endpoints.get(_DEFAULT_SERVER_PORT) or next(iter(endpoints.values()), "")
        if not url:
            raise ValueError(
                f"FlashVSREngine: instance {instance.id} has no endpoint for "
                f"port {_DEFAULT_SERVER_PORT}; endpoints={endpoints!r}"
            )
        return url.rstrip("/")


def _http_json(
    *, method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = _json.dumps(payload).encode("utf-8") if payload is not None else None
    headers: dict[str, str] = {"User-Agent": "kinoforge-flashvsr/0.1"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        body = resp.read()
    return cast(dict[str, Any], _json.loads(body))
```

- [ ] **Step 4: Run tests → GREEN** (10 passed)

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
    src/kinoforge/upscalers/flashvsr/_engine.py \
    tests/upscalers/flashvsr/test_engine.py
git add src/kinoforge/upscalers/flashvsr/_engine.py \
    tests/upscalers/flashvsr/test_engine.py
git commit -m "feat(flashvsr): FlashVSREngine HTTP dispatch + provision script

render_provision emits SM80+ guard (exit 87), TORCH_EXTENSIONS_DIR + MAX_JOBS,
BSA-then-FlashVSR install, weights fetch, HF_HUB_OFFLINE flip. model_identity
returns flashvsr-wan21-<precision>. upscale() POSTs /upscale + polls status
+ handles file:// upload path. 10 tests GREEN.

Ref: docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §5.2, §5.3"
```

---

## Task 4: `__init__.py` self-register + `_adapters.py` import

**Goal:** Registering `flashvsr` engine via `register_upscaler` on import; kinoforge package pulls it via `_adapters.py`.

**Files:**
- Modify: `src/kinoforge/upscalers/flashvsr/__init__.py` — overwrite T1 placeholder
- Modify: `src/kinoforge/_adapters.py` — add one import line
- Modify: `tests/test_adapters.py` — extend registration test

**Acceptance Criteria:**
- [ ] `import kinoforge` registers `"flashvsr"` in `get_upscaler` table; `"flashvsr" in upscaler_names()`.
- [ ] Package init gracefully swallows `ImportError` on the pod (same pattern as spandrel: registry may not be embedded).

**Verify:** `pixi run pytest tests/test_adapters.py -v -k flashvsr` → 1 passed

**Steps:**

- [ ] **Step 1: Write failing test — append to `tests/test_adapters.py`**

```python
def test_flashvsr_registered_on_kinoforge_import() -> None:
    """RED: flashvsr appears in upscaler_names after `import kinoforge`.

    Bug caught: forgetting the _adapters.py import line → engine reachable
    only if the caller happens to `import kinoforge.upscalers.flashvsr`
    directly; cfg-driven dispatch fails.
    """
    import kinoforge  # noqa: F401
    from kinoforge.core.registry import get_upscaler, upscaler_names

    assert "flashvsr" in upscaler_names()
    engine_cls = get_upscaler("flashvsr")
    assert engine_cls.__name__ == "FlashVSREngine"
```

- [ ] **Step 2: Confirm RED** → assertion fails, `flashvsr` not in `upscaler_names()`.

- [ ] **Step 3: Overwrite `src/kinoforge/upscalers/flashvsr/__init__.py`**

```python
"""FlashVSR upscaler package.

Full client engine in :mod:`._engine`; self-registers with
``kinoforge.core.registry`` at import. On-pod embeds (which only include
``kinoforge.core.errors`` + ``.scale_target``) raise ImportError for the
engine deps; swallow so the pod can still import ``._runtime`` /
``._fetch_weights`` without the full package.
"""

from __future__ import annotations

try:
    from kinoforge.core import registry
    from kinoforge.core.errors import UnknownAdapter

    from ._engine import FlashVSREngine

    try:
        registry.register_upscaler("flashvsr", FlashVSREngine)
    except UnknownAdapter:
        pass

    __all__ = ["FlashVSREngine"]
except ImportError:
    __all__ = []
```

- [ ] **Step 4: Edit `src/kinoforge/_adapters.py` — add import**

Find the block that imports the other upscaler packages (near `import kinoforge.upscalers.spandrel`); add one line:

```python
import kinoforge.upscalers.flashvsr  # noqa: F401 — self-register on import
```

- [ ] **Step 5: Run test → GREEN**

```bash
pixi run pytest tests/test_adapters.py -v -k flashvsr
```

- [ ] **Step 6: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
    src/kinoforge/upscalers/flashvsr/__init__.py \
    src/kinoforge/_adapters.py \
    tests/test_adapters.py
git add src/kinoforge/upscalers/flashvsr/__init__.py \
    src/kinoforge/_adapters.py tests/test_adapters.py
git commit -m "feat(flashvsr): self-register on import via _adapters

FlashVSREngine registered under 'flashvsr' in the upscaler registry on
`import kinoforge`. Pod-safe: ImportError branch swallows registry
absence so on-pod _runtime import still works.

Ref: docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §3.1"
```

---

## Task 5: Server dispatch delta

**Goal:** Extend `wan_t2v_server.py` to dispatch `flashvsr-*` slugs, add `FlashVSRParams` Pydantic body, `_flashvsr_weights_dir()` helper, and thread the engine into `upscale_handler` + `_run_upscale_job`.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py` — 5 edit sites
- Modify: `tests/engines/diffusers/servers/test_wan_t2v_server.py` — extend dispatch + capability tests

**Acceptance Criteria:**
- [ ] `_load_model_to_gpu("flashvsr-wan21-fp16")` returns a `FlashVSRRuntime` instance (mocked pipeline).
- [ ] `_capability_for_model("flashvsr-wan21-fp16")` returns `"upscale"`.
- [ ] `_flashvsr_weights_dir()` returns `Path("/workspace/models/flashvsr")` unless overridden by `KINOFORGE_FLASHVSR_WEIGHTS_DIR`.
- [ ] `UpscaleRequest.flashvsr: FlashVSRParams | None` accepted; missing = None; unknown engine `"flashvsr"` no longer 400s.
- [ ] `_run_upscale_job` dispatches `req.engine == "flashvsr"` to `flashvsr-wan21-<precision>` slug + params from the flashvsr block.

**Verify:** `pixi run pytest tests/engines/diffusers/servers/test_wan_t2v_server.py -v -k "flashvsr or upscale"` → all pass

**Steps:**

- [ ] **Step 1: Write failing tests — append to `tests/engines/diffusers/servers/test_wan_t2v_server.py`**

Add 4 tests: (a) `_load_model_to_gpu` prefix dispatch, (b) `_capability_for_model` mapping, (c) `_flashvsr_weights_dir` env override, (d) `_run_upscale_job` slug + params. Each test with docstring stating the bug it catches (`arch-token mis-parse`, `capability omission`, `env-var contract regression`, `dispatch fallthrough to spandrel`). Follow the shape of the existing `test_load_model_to_gpu_spandrel_prefix` test.

- [ ] **Step 2: Confirm RED** → 4 fails.

- [ ] **Step 3: Edit `wan_t2v_server.py`**

Edit `_load_model_to_gpu` (line ~162 — add branch mirroring spandrel):

```python
    if name.startswith("flashvsr-"):
        from kinoforge.upscalers.flashvsr._runtime import FlashVSRRuntime

        # Slug: "flashvsr-wan21-<precision>" → precision tail.
        parts = name.split("-")
        precision = parts[-1]
        return FlashVSRRuntime(
            weights_dir=_flashvsr_weights_dir(),
            precision=precision,  # type: ignore[arg-type]
            window_size=24,
            tile_size=0,
            long_video_mode=False,
        )
```

Add helper alongside `_spandrel_weights_dir()`:

```python
def _flashvsr_weights_dir() -> Path:
    """FlashVSR weights dir; overridable via KINOFORGE_FLASHVSR_WEIGHTS_DIR."""
    return Path(os.environ.get(
        "KINOFORGE_FLASHVSR_WEIGHTS_DIR", "/workspace/models/flashvsr"
    ))
```

Edit `_capability_for_model` (line ~1104):

```python
    if (
        name.startswith("seedvr2-")
        or name.startswith("spandrel-")
        or name.startswith("flashvsr-")
    ):
        return "upscale"
```

Add `FlashVSRParams` Pydantic model near `SpandrelParams` (line ~1557):

```python
class FlashVSRParams(BaseModel):
    """Engine-specific overrides for a flashvsr upscale request."""

    weights_bundle: str | None = None
    precision: Literal["fp16", "fp32"] = "fp16"
    window_size: int = 24
    tile_size: int = 0
    long_video_mode: bool = False
```

Extend `UpscaleRequest`:

```python
class UpscaleRequest(BaseModel):
    # ... existing fields ...
    flashvsr: FlashVSRParams | None = None
```

Edit `upscale_handler` allowlist (line ~1660):

```python
    if req.engine not in {"seedvr2", "spandrel", "flashvsr"}:
        raise HTTPException(status_code=400, detail=f"unsupported engine: {req.engine}")
```

Edit `_run_upscale_job` dispatch (line ~1702). Extend the `if req.engine == "spandrel"` chain with an `elif req.engine == "flashvsr"` branch:

```python
            elif req.engine == "flashvsr":
                if req.flashvsr is None:
                    raise ValueError(
                        "flashvsr engine requires a flashvsr block"
                    )
                model_name = f"flashvsr-wan21-{req.flashvsr.precision}"
                # Drop bundle URL — that's a client-side download hint,
                # not a runtime knob. Precision is in the slug.
                params = req.flashvsr.model_dump(
                    exclude={"weights_bundle", "precision"}
                )
```

- [ ] **Step 4: Run tests → GREEN**

```bash
pixi run pytest tests/engines/diffusers/servers/test_wan_t2v_server.py -v \
    -k "flashvsr or upscale"
```

- [ ] **Step 5: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
    src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
    tests/engines/diffusers/servers/test_wan_t2v_server.py
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
    tests/engines/diffusers/servers/test_wan_t2v_server.py
git commit -m "feat(server): wan_t2v_server flashvsr-* prefix + FlashVSRParams

_load_model_to_gpu dispatches flashvsr-<...> to FlashVSRRuntime;
_capability_for_model returns 'upscale'; _flashvsr_weights_dir env
override honored. UpscaleRequest.flashvsr field + FlashVSRParams Pydantic
model; upscale_handler engine allowlist; _run_upscale_job branch.

Ref: docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §5.6"
```

---

## Task 6: Example cfgs + lockdown tests

**Goal:** Two example cfgs users can point `kinoforge generate` / `kinoforge upscale` at; lockdown tests defend against silent config drift.

**Files:**
- Create: `examples/configs/runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml`
- Create: `examples/configs/runpod-upscale-only-flashvsr.yaml`
- Modify: `tests/test_examples.py` — 2 lockdown tests

**Acceptance Criteria:**
- [ ] Multi-stage cfg: `engine: diffusers-wan22-t2v-a14b`, `upscale.engine: flashvsr`, `upscale.scale: 2x`, `gpu_preference: [A100 80GB, A6000, L40S, A100 40GB]`, `long_video_mode: false`.
- [ ] Upscale-only cfg: `upscale_only: true`, `upscale.engine: flashvsr`, `gpu_preference: [A6000, A100 40GB, L40S, A100 80GB]`.
- [ ] Both cfgs pin region — `region: us-west1` (memory: default Oregon across all clouds).
- [ ] Lockdown tests assert gpu_preference contents + upscale engine + long_video_mode default.

**Verify:** `pixi run pytest tests/test_examples.py -v -k flashvsr` → 2 passed; `pixi run kinoforge validate examples/configs/runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml` → 0

**Steps:**

- [ ] **Step 1: Write failing lockdown tests — append to `tests/test_examples.py`**

```python
def test_flashvsr_multistage_cfg_pins_engine_and_gpu_allowlist() -> None:
    """RED: multi-stage cfg locks upscale engine, GPU allowlist, region."""
    cfg = load_example("runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml")
    assert cfg["upscale"]["engine"] == "flashvsr"
    assert cfg["upscale"]["flashvsr"]["long_video_mode"] is False
    assert set(cfg["compute"]["gpu_preference"]) == {
        "A100 80GB", "A6000", "L40S", "A100 40GB",
    }
    assert cfg["compute"]["region"] == "us-west1"


def test_flashvsr_upscale_only_cfg_marks_upscale_only() -> None:
    """RED: upscale-only cfg skips Wan eager load and pins A6000-first."""
    cfg = load_example("runpod-upscale-only-flashvsr.yaml")
    assert cfg["engine_config"]["upscale_only"] is True
    assert cfg["upscale"]["engine"] == "flashvsr"
    assert cfg["compute"]["gpu_preference"][0] == "A6000"
```

- [ ] **Step 2: Confirm RED** → FileNotFoundError.

- [ ] **Step 3: Write `examples/configs/runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml`**

```yaml
# Wan 2.2 T2V-A14B → FlashVSR v1.1 multi-stage cfg.
# v1 default upscaler (2026-07-01 spec). Spandrel available via
# runpod-diffusers-wan22-t2v-a14b-spandrel.yaml for anime / tiny-VRAM.

engine: diffusers-wan22-t2v-a14b
engine_config:
  # Wan pipeline config — mirror existing t2v-a14b example cfg.
  pipeline_ref: "hf:Wan-AI/Wan2.2-T2V-A14B-Diffusers"

compute:
  provider: runpod
  region: us-west1                           # Oregon (memory: default region across clouds)
  gpu_preference: ["A100 80GB", "A6000", "L40S", "A100 40GB"]
  max_usd_per_hr: 2.50

upscale:
  engine: flashvsr
  scale: 2x
  flashvsr:
    weights_bundle: "hf:JunhaoZhuang/FlashVSR-v1.1"
    precision: fp16
    window_size: 24
    tile_size: 0
    long_video_mode: false

output:
  sink: local
  dir: /workspace/output
```

- [ ] **Step 4: Write `examples/configs/runpod-upscale-only-flashvsr.yaml`**

```yaml
# Standalone FlashVSR upscale — no video-gen stage.
# Use with: kinoforge upscale --video <path>.mp4 --config <this>.yaml

engine: diffusers-wan22-t2v-a14b   # placeholder; upscale_only skips eager load
engine_config:
  upscale_only: true

compute:
  provider: runpod
  region: us-west1
  gpu_preference: ["A6000", "A100 40GB", "L40S", "A100 80GB"]
  max_usd_per_hr: 1.50

upscale:
  engine: flashvsr
  scale: 2x
  flashvsr:
    weights_bundle: "hf:JunhaoZhuang/FlashVSR-v1.1"
    precision: fp16
    window_size: 24
    tile_size: 0
    long_video_mode: false

output:
  sink: local
  dir: /workspace/output
```

- [ ] **Step 5: Run lockdown tests + validate**

```bash
pixi run pytest tests/test_examples.py -v -k flashvsr
pixi run kinoforge validate examples/configs/runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml
pixi run kinoforge validate examples/configs/runpod-upscale-only-flashvsr.yaml
```
Expected: 2 tests pass; both `validate` invocations exit 0.

- [ ] **Step 6: Update spandrel cfg header comment**

Edit `examples/configs/runpod-diffusers-wan22-t2v-a14b-spandrel.yaml` — prepend at line 1:

```yaml
# NOTE: v1 default upscaler is now FlashVSR — see
# examples/configs/runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml.
# Spandrel remains supported for tiny-VRAM / anime use cases.
```

- [ ] **Step 7: Pre-commit + commit**

```bash
pixi run pre-commit run --files \
    examples/configs/runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml \
    examples/configs/runpod-upscale-only-flashvsr.yaml \
    examples/configs/runpod-diffusers-wan22-t2v-a14b-spandrel.yaml \
    tests/test_examples.py
git add examples/configs/runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml \
    examples/configs/runpod-upscale-only-flashvsr.yaml \
    examples/configs/runpod-diffusers-wan22-t2v-a14b-spandrel.yaml \
    tests/test_examples.py
git commit -m "feat(examples): flashvsr multi-stage + upscale-only cfgs

Two new cfgs: runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml (multi-stage)
and runpod-upscale-only-flashvsr.yaml (standalone). Both pin us-west1
(Oregon default) + SM80+ NVIDIA gpu_preference. Spandrel cfg header
comment redirects to flashvsr as the v1 default. 2 lockdown tests GREEN.

Ref: docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §8"
```

---

## Task 7: RED live-smoke scaffold

**Goal:** Committed live-smoke file that fails deterministically with `xfail`; unxfailed atomically at spend time so the scaffold exists in git BEFORE any spend (CLAUDE.md durability rule).

**Files:**
- Create: `tests/live/test_flashvsr_live.py`

**Acceptance Criteria:**
- [ ] Three test functions: `test_f_single`, `test_f_multi`, `test_f_warm`.
- [ ] All three marked `@pytest.mark.xfail(strict=True, reason="live spend not yet fired")` at commit time.
- [ ] Each test reads the standard prompt from `/workspace/examples/configs/prompts/field-realistic.txt` (memory: `feedback_standard_test_prompt.md`).
- [ ] Each test asserts: pod destroyed at end (via `kinoforge list` returning "No running instances"), output MP4 exists, ffprobe dims == 2× input.

**Verify:** `pixi run pytest tests/live/test_flashvsr_live.py -v` → 3 xfailed

**Steps:**

- [ ] **Step 1: Write `tests/live/test_flashvsr_live.py`**

```python
"""FlashVSR live-smoke matrix — RED xfail-gated pre-spend scaffold.

Committed BEFORE any live spend per CLAUDE.md durability rule. Each test
unxfailed atomically in the same commit as its evidence.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_STANDARD_PROMPT_PATH = Path("/workspace/examples/configs/prompts/field-realistic.txt")


def _ffprobe_dims(video: Path) -> tuple[int, int]:
    """Return (width, height) via ffprobe."""
    r = subprocess.run(  # noqa: S603
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0",
         str(video)],
        capture_output=True, text=True, check=True,
    )
    w, h = r.stdout.strip().split(",")
    return int(w), int(h)


def _kinoforge_list_shows_no_pods() -> bool:
    """Return True iff `kinoforge list` reports zero pods AND empty ledger."""
    r = subprocess.run(  # noqa: S603
        ["pixi", "run", "kinoforge", "list"],
        capture_output=True, text=True, check=False,
    )
    return (
        "No running instances." in r.stdout
        and "No instances recorded in ledger." in r.stdout
    )


@pytest.mark.xfail(strict=True, reason="live spend not yet fired")
def test_f_single(tmp_path: Path) -> None:
    """F-single: standalone kinoforge upscale on a pre-existing Wan clip.

    Bug caught: file:// source path unreachable from pod without upload
    seam (regressions of the P3 T15 blocker).
    """
    src = Path("/workspace/output/20260620-055823_diffusers_unknown_Photorealistic-cinem.mp4")
    assert src.exists(), f"missing fixture Wan clip {src}"

    r = subprocess.run(  # noqa: S603
        ["pixi", "run", "kinoforge", "upscale",
         "--config", "examples/configs/runpod-upscale-only-flashvsr.yaml",
         "--video", str(src),
         "--no-reuse"],
        capture_output=True, text=True, check=True, timeout=15 * 60,
    )
    assert "flashvsr-wan21-fp16" in r.stdout
    outs = sorted(Path("/workspace/output").glob("*_upscaled_flashvsr_*.mp4"))
    assert outs, "no upscaled artifact sunk"
    src_dims = _ffprobe_dims(src)
    out_dims = _ffprobe_dims(outs[-1])
    assert out_dims == (src_dims[0] * 2, src_dims[1] * 2), (
        f"expected 2x dims got {out_dims} vs src {src_dims}"
    )
    assert _kinoforge_list_shows_no_pods(), "pod not destroyed post-run"


@pytest.mark.xfail(strict=True, reason="live spend not yet fired")
def test_f_multi(tmp_path: Path) -> None:
    """F-multi: Wan generate → FlashVSR upscale on the same pod.

    Bug caught: DiffusersEngine.render_provision omits upscaler
    composition (P2 T8 seam regression) → pod boots without FlashVSR.
    """
    prompt = _STANDARD_PROMPT_PATH.read_text().strip()
    r = subprocess.run(  # noqa: S603
        ["pixi", "run", "kinoforge", "generate",
         "--config", "examples/configs/runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml",
         "--prompt", prompt],
        capture_output=True, text=True, check=True, timeout=45 * 60,
    )
    assert "wan-T2V-done" in r.stdout or "diffusers" in r.stdout
    assert "flashvsr-wan21-fp16" in r.stdout
    # Two MP4s expected — Wan raw + FlashVSR upscaled.
    wans = sorted(Path("/workspace/output").glob("*_diffusers_Wan2.2-*.mp4"))
    ups = sorted(Path("/workspace/output").glob("*_upscaled_flashvsr_*.mp4"))
    assert wans and ups, "missing wan or upscaled artifact"


@pytest.mark.xfail(strict=True, reason="live spend not yet fired")
def test_f_warm(tmp_path: Path) -> None:
    """F-warm: second kinoforge generate on same pod; no BSA recompile.

    Bug caught: TORCH_EXTENSIONS_DIR points at a non-persistent path →
    BSA nvcc-compiles again on second call (10-min tax).
    """
    prompt = _STANDARD_PROMPT_PATH.read_text().strip() + " variant B"
    r = subprocess.run(  # noqa: S603
        ["pixi", "run", "kinoforge", "generate",
         "--config", "examples/configs/runpod-diffusers-wan22-t2v-a14b-flashvsr.yaml",
         "--prompt", prompt, "--no-reuse"],
        capture_output=True, text=True, check=True, timeout=15 * 60,
    )
    # LRU hit — no cold model load; no BSA recompile.
    assert "LRU hit" in r.stdout or "warm reuse" in r.stdout
    assert "compiling block_sparse_attention" not in r.stdout.lower()
    assert _kinoforge_list_shows_no_pods(), "pod not destroyed after --no-reuse"
```

- [ ] **Step 2: Run to confirm xfail** (no import errors)

```bash
pixi run pytest tests/live/test_flashvsr_live.py -v
```
Expected: `3 xfailed`.

- [ ] **Step 3: Pre-commit + commit**

```bash
pixi run pre-commit run --files tests/live/test_flashvsr_live.py
git add tests/live/test_flashvsr_live.py
git commit -m "test(live): flashvsr F-single + F-multi + F-warm RED scaffold

Three xfail-gated smokes committed BEFORE live spend (CLAUDE.md durability
rule). Each unxfailed atomically with its evidence commit. Reads standard
prompt from examples/configs/prompts/field-realistic.txt (memory:
feedback_standard_test_prompt).

Ref: docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §7.2"
```

---

## Task 8: F-single live spend

**Goal:** Standalone `kinoforge upscale` on a pre-existing 720p Wan clip. Confirms `_upload_source` + `/upscale` + BSA compile-and-cache work end-to-end. Budget: ~$0.05.

**Files:**
- Modify: `tests/live/test_flashvsr_live.py` — un-xfail `test_f_single`
- Create: `tests/live/evidence/2026-07-01_flashvsr_f_single_stdout.txt`

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 (clean tree, zero pods, creds present).
- [ ] Pod polling every 60-90 s during compute phase; GPU util > 0% during BSA compile + upscale (memory: `feedback_proactive_pod_stats`).
- [ ] Output MP4 lands at `/workspace/output/<localtime>_upscaled_flashvsr_*.mp4`; dims == 2× source; ffprobe reports valid H.264.
- [ ] `kinoforge list` post-run shows both "No running instances." AND "No instances recorded in ledger.".
- [ ] Test moves from xfail to xpassed → assertion, then to plain PASS.
- [ ] Spend recorded in commit message; ≤ $0.15 (2× budget = hard ceiling).

**Verify:** `pixi run pytest tests/live/test_flashvsr_live.py::test_f_single -v` → 1 passed

**Steps:**

- [ ] **Step 1: Preflight**

```bash
pixi run preflight
```
Exit 0 required. If any pod exists, `kinoforge destroy --id <pod>` first.

- [ ] **Step 2: Un-xfail `test_f_single`**

Remove the `@pytest.mark.xfail(strict=True, ...)` decorator on `test_f_single` only. Leave the other two xfail-guarded.

- [ ] **Step 3: Run the smoke**

```bash
pixi run pytest tests/live/test_flashvsr_live.py::test_f_single -v \
    | tee tests/live/evidence/2026-07-01_flashvsr_f_single_stdout.txt
```

Monitor RunPod pod stats every 60-90 s during the run in a parallel shell:

```bash
# In parallel — while pytest runs
while true; do
  pixi run kinoforge status --id <pod-id-from-first-log-line> || break
  sleep 60
done
```

If GPU 0% for ≥ 3 consecutive probes during compute phase → kill:

```bash
pixi run kinoforge destroy --id <pod-id>
```

- [ ] **Step 4: Verify post-run**

```bash
pixi run kinoforge list
```
Expected both:
- `[instance overview] No running instances.`
- `No instances recorded in ledger.`

If any pod leaked → `pixi run kinoforge destroy --id <pod-id>` immediately.

- [ ] **Step 5: Commit evidence atomically**

```bash
git add tests/live/test_flashvsr_live.py \
    tests/live/evidence/2026-07-01_flashvsr_f_single_stdout.txt
git commit -m "test(live): F-single GREEN evidence — flashvsr standalone upscale

Pod <pod-id> spend ~\$<X>. Output <path>. Dims 720x480 → 1440x960
(2x confirmed). BSA compiled fresh at cold boot; cache landed at
/workspace/.cache/bsa. Pod destroyed post-run (kinoforge list clean).

Ref: docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §7.2"
```

If the smoke fails: DO NOT commit un-xfail. Capture the failure log to `evidence/2026-07-01_flashvsr_f_single_attN_FAIL.txt`, `kinoforge destroy` the pod, investigate, iterate, commit each attempt's evidence separately.

---

## Task 9: F-multi + F-warm combined live spend

**Goal:** Multi-stage smoke (Wan 2.2 → FlashVSR) + warm-reuse smoke on the same pod. Budget: ~$0.60.

**Files:**
- Modify: `tests/live/test_flashvsr_live.py` — un-xfail `test_f_multi` + `test_f_warm`
- Create: `tests/live/evidence/2026-07-01_flashvsr_f_multi_warm_stdout.txt`

**Acceptance Criteria:**
- [ ] F-multi: two artifacts in `/workspace/output/` — Wan 720p + FlashVSR 1440p; dims 2× confirmed.
- [ ] F-warm (same pod, second prompt): LRU hit logged; NO BSA recompile ("compiling block_sparse_attention" absent from second-run stdout).
- [ ] Post F-warm: `kinoforge list` clean.
- [ ] Combined spend ≤ $1.20 (2× happy path); if F-multi exceeds $1 alone → HARD STOP + investigate.

**Verify:** `pixi run pytest tests/live/test_flashvsr_live.py::test_f_multi tests/live/test_flashvsr_live.py::test_f_warm -v` → 2 passed

**Steps:**

- [ ] **Step 1: Preflight** (clean tree — F-single un-xfail already committed)

```bash
pixi run preflight
```

- [ ] **Step 2: Un-xfail both remaining tests**

Remove `@pytest.mark.xfail` from `test_f_multi` AND `test_f_warm`.

- [ ] **Step 3: Run F-multi (warm-reuse enabled) → F-warm (--no-reuse on second run)**

The two tests are sequential; the second reuses the pod the first left warm. Run them together:

```bash
pixi run pytest \
    tests/live/test_flashvsr_live.py::test_f_multi \
    tests/live/test_flashvsr_live.py::test_f_warm \
    -v \
    | tee tests/live/evidence/2026-07-01_flashvsr_f_multi_warm_stdout.txt
```

Poll RunPod stats every 60-90 s. Monitor: (a) BSA nvcc compile phase (`cpuPercent` high, `gpuUtilPercent` 0 for ~5-10 min); (b) Wan cold-load (`memoryPercent` climbs sharply); (c) T2V generate (GPU 90+%); (d) upscale (GPU 60-80%); (e) second-run LRU hit (no repeat of a-b).

- [ ] **Step 4: Verify post-run**

```bash
pixi run kinoforge list
```
Both "No running instances." + "No instances recorded in ledger." required.

- [ ] **Step 5: Commit evidence**

```bash
git add tests/live/test_flashvsr_live.py \
    tests/live/evidence/2026-07-01_flashvsr_f_multi_warm_stdout.txt
git commit -m "test(live): F-multi + F-warm GREEN evidence — Wan→FlashVSR + warm-reuse

Pod <pod-id> spend ~\$<X> across two prompts. Stage-1 Wan 720p output
<path>; stage-2 FlashVSR 1440p output <path>; dims 2x confirmed by
ffprobe. F-warm second prompt: LRU hit + NO BSA recompile
(TORCH_EXTENSIONS_DIR cache-hit on /workspace/.cache/bsa). Pod destroyed
post-run (kinoforge list clean).

Ref: docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md §7.2"
```

---

## Task 10: Log qualifying generation + PROGRESS.md SHIPPED flip

**Goal:** Persist the new capability axis (flashvsr engine + flashvsr-wan21-fp16 model + upscale mode) to `/workspace/successful-generations.md` per its schema; flip PROGRESS.md to SHIPPED.

**Files:**
- Modify: `/workspace/successful-generations.md` — new detailed section (CLAUDE.md durability rule)
- Modify: `PROGRESS.md` — active workstream section flip

**Acceptance Criteria:**
- [ ] `successful-generations.md` gains a new detailed section per its preamble schema — engine `flashvsr`, model `flashvsr-wan21-fp16`, mode `t2v→upscale`.
- [ ] `PROGRESS.md` "Active workstream" says "FlashVSR video upscaling — SHIPPED YYYY-MM-DD" with pointers to the spec + this plan; F-multi + F-warm pod IDs + spend recorded.
- [ ] Both files committed together in one commit.

**Verify:** `git log -1 --stat` shows both files touched; `rg -l "flashvsr-wan21-fp16" /workspace/successful-generations.md PROGRESS.md` → both listed

**Steps:**

- [ ] **Step 1: Read the schema preamble of `/workspace/successful-generations.md`** to confirm the entry format.

- [ ] **Step 2: Append a new detailed section following that schema** — include:
    - Date (local TZ per memory `feedback_local_timezone_only`)
    - Engine: `flashvsr`
    - Model: `flashvsr-wan21-fp16` (streaming DMD, Wan 2.1 backbone)
    - Mode: `t2v→upscale` multi-stage; also standalone upscale
    - Configs used
    - Pod IDs + spend
    - Output paths
    - Reproduction recipe (one-liner CLI)
    - Failure modes debugged during rollout (BSA compile time, HF_HUB_OFFLINE flip, etc.)

- [ ] **Step 3: Update `PROGRESS.md`**

Replace the "Active workstream" section head with:

```markdown
## Active workstream

**FlashVSR video upscaling — SHIPPED YYYY-MM-DD (F-single pod <id> ~\$<X>;
F-multi+F-warm pod <id> ~\$<Y>).**

Plan: `docs/superpowers/plans/2026-07-01-flashvsr-video-upscaling.md`
Spec: `docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md`

FlashVSR v1.1 replaces spandrel as v1 default upscaler. Spandrel remains
supported for tiny-VRAM / anime use cases via
`runpod-diffusers-wan22-t2v-a14b-spandrel.yaml`. SeedVR2 remains dormant
in `[seedvr]` extras.

### History
[preserve the P1/P2/P3 history block that was there before]
```

- [ ] **Step 4: Commit**

```bash
git add /workspace/successful-generations.md PROGRESS.md
git commit -m "docs: FlashVSR SHIPPED — v1 default upscaler live-GREEN

Log new capability axis to successful-generations.md (flashvsr engine
+ flashvsr-wan21-fp16 model + t2v→upscale mode). PROGRESS.md active
workstream flipped to SHIPPED with pod IDs and spend.

Ref: docs/superpowers/specs/2026-07-01-flashvsr-video-upscaling-design.md"
```

---

## Rollback path

If FlashVSR quality on Wan 2.2 output proves inadequate after F-multi lands:

1. Do NOT delete FlashVSR code — leave it registered and available via cfg.
2. Revert the spandrel cfg header comment (Task 6 Step 6).
3. Open a new brainstorm: STAR (I2VGen-XL, MIT) as fallback #2 candidate per spec §10.
4. All FlashVSR unit tests stay GREEN; only the "v1 default" designation moves.

---

## Self-review

**Spec coverage check** — every §2 goal maps to a task:
- Goal 1 (FlashVSREngine registered, default cfg) → T2 + T3 + T4 + T6
- Goal 2 (server surface reuse) → T5
- Goal 3 (spandrel stays as fallback) → T6 (cfg header comment)
- Goal 4 (SeedVR2 dormant, no change) → NO task needed (spec §2 non-goal)
- Goal 5 (BSA compile at cold boot + cache) → T3 (render_provision) + T8 (verified live)
- Goal 6 (end-to-end multi-stage smoke) → T9
- Goal 7 (budget ceiling $3) → T8 + T9 enforce per-run hard-stops

**Type consistency**: `FlashVSRRuntime` constructor signature (T2) matches server `_load_model_to_gpu` dispatch (T5) — 5 args: `weights_dir, precision, window_size, tile_size, long_video_mode`. `FlashVSREngine.model_identity` (T3) returns `flashvsr-wan21-<precision>` matched by server slug parser (T5). `FlashVSRParams` (T5) fields match `FlashVSREngineConfig` (T0). `flashvsr` block schema consistent across cfg → engine → runtime → server.

**Placeholder scan**: none. Every step shows code or exact command. `<X>` placeholders in T8/T9/T10 commit messages are variable outputs filled at spend time; the surrounding structure is fixed.

**Task granularity**: each task is TDD-inside, one commit at end, independently verifiable. T8 + T9 split so F-single feedback informs F-multi go/no-go decision.
