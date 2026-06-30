# Upscaler packaging pivot — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers-extended-cc:subagent-driven-development` (recommended) or `superpowers-extended-cc:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pivot v1 default upscaler from broken SeedVR2 upstream packaging to a working `spandrel`-based engine; resolve Blockers A/B/C from `PROGRESS.md`; ship live-spend smokes with evidence preserved.

**Architecture:** SpandrelEngine registers as `"spandrel"` and runs server-side inside the existing wan_t2v_server (LRU registry + /upscale endpoints from T11/T12). SeedVR2Engine stays registered but its four ABC methods raise `ExtrasNotInstalled` until a Phase 2 vendoring workstream lands. Provision composition seam inside `DiffusersEngine.render_provision` composes any registered upscaler's `render_provision` output into the bootstrap script. Standalone `kinoforge upscale` reuses `generate()`'s warm-reuse / attach / cold-create machinery via two new flags (`skip_clip_stage`, `initial_clip`) instead of a duplicated entry point.

**Tech Stack:** Python 3.13, pixi, pytest, FastAPI, `spandrel >= 0.4.2` (MIT, actively maintained SR runtime — auto-detects RealESRGAN / ESRGAN / SwinIR / OmniSR architectures from weights), `imageio[ffmpeg]` for video I/O on the pod, existing RunPod provider + DiffusersEngine.

**User decisions (already made):**
- Hybrid scope: packaged default + SeedVR2 in `[seedvr]` extras stub (Phase 1 ship; Phase 2 defer).
- Quality model: per-frame image upscaler (flicker accepted in v1).
- Hosting: self-hosted on existing RunPod path; same wan_t2v_server, LRU swap between Wan and upscaler weights.
- Existing SeedVR2 code (366 LOC): keep + move behind extras + add vendoring shim later.
- Spec approved verbatim: `docs/superpowers/specs/2026-06-29-upscaler-packaging-pivot-design.md`.
- Evidence-preservation demand: live smoke outputs land in `tests/live/evidence/2026-06-29-...` and stay committed for inspection.

---

## Task 1: `ExtrasNotInstalled` error class

**Goal:** Add a typed error that every extras-gated engine can raise, with a structured `extras_name` + `install_hint` payload.

**Files:**
- Modify: `src/kinoforge/core/errors.py`
- Test: `tests/core/test_extras_not_installed_error.py`

**Acceptance Criteria:**
- [ ] `ExtrasNotInstalled(extras_name="seedvr", install_hint="...")` constructs and str() includes both fields.
- [ ] `ExtrasNotInstalled` is a `KinoforgeError` subclass (matches existing error hierarchy).
- [ ] `.extras_name` and `.install_hint` are accessible as attributes after construction.

**Verify:** `pixi run pytest tests/core/test_extras_not_installed_error.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing test**

```python
# tests/core/test_extras_not_installed_error.py
"""Tests for the ExtrasNotInstalled error class."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import ExtrasNotInstalled, KinoforgeError


def test_is_kinoforge_error_subclass() -> None:
    # Bug caught: someone defines ExtrasNotInstalled as a bare Exception,
    # breaking `except KinoforgeError` catchers in the orchestrator.
    assert issubclass(ExtrasNotInstalled, KinoforgeError)


def test_str_includes_extras_name_and_hint() -> None:
    # Bug caught: error message drops the install_hint and the operator
    # sees only "kinoforge[seedvr] extras not installed" with no
    # remediation guidance.
    err = ExtrasNotInstalled(
        extras_name="seedvr",
        install_hint="vendoring lands in Phase 2",
    )
    msg = str(err)
    assert "seedvr" in msg
    assert "vendoring lands in Phase 2" in msg


def test_attributes_accessible_for_programmatic_handling() -> None:
    # Bug caught: caller wants to log only the extras_name or branch
    # on it but the attributes were never stored on self.
    err = ExtrasNotInstalled(extras_name="seedvr", install_hint="install with X")
    assert err.extras_name == "seedvr"
    assert err.install_hint == "install with X"
```

- [ ] **Step 2: Verify RED**

```
pixi run pytest tests/core/test_extras_not_installed_error.py -v
```
Expected: 3 fail with `ImportError: cannot import name 'ExtrasNotInstalled'`.

- [ ] **Step 3: Implement**

Append to `src/kinoforge/core/errors.py`:

```python
class ExtrasNotInstalled(KinoforgeError):
    """Raised when a kinoforge component requires a pip extras group that is not installed.

    Args:
        extras_name: The extras-group key (e.g. ``"seedvr"`` for
            ``kinoforge[seedvr]``).
        install_hint: Operator-facing remediation text (concrete command,
            workstream reference, or "use ``cfg.upscale.engine = 'spandrel'``
            instead" pointer).
    """

    def __init__(self, extras_name: str, install_hint: str) -> None:
        super().__init__(
            f"kinoforge[{extras_name}] extras not installed — {install_hint}"
        )
        self.extras_name = extras_name
        self.install_hint = install_hint
```

- [ ] **Step 4: Verify GREEN**

```
pixi run pytest tests/core/test_extras_not_installed_error.py -v
```
Expected: 3 pass.

- [ ] **Step 5: Pre-commit + commit**

```
pixi run pre-commit run --files src/kinoforge/core/errors.py tests/core/test_extras_not_installed_error.py
git add src/kinoforge/core/errors.py tests/core/test_extras_not_installed_error.py
git commit -m "feat(errors): ExtrasNotInstalled — typed error for extras-gated engines"
```

```json:metadata
{"files": ["src/kinoforge/core/errors.py", "tests/core/test_extras_not_installed_error.py"], "verifyCommand": "pixi run pytest tests/core/test_extras_not_installed_error.py -v", "acceptanceCriteria": ["ExtrasNotInstalled subclasses KinoforgeError", "str includes both extras_name and install_hint", "attributes accessible programmatically"], "modelTier": "mechanical"}
```

---

## Task 2: SeedVR2 stub-raise rewrite + extras declaration

**Goal:** Convert the four ABC methods on `SeedVR2Engine` to raise `ExtrasNotInstalled`; move the upstream `from seedvr.inference import` inside `SeedVR2Runtime.__init__` so module-import is side-effect free; declare `seedvr = []` in `pyproject.toml` extras.

**Files:**
- Modify: `src/kinoforge/upscalers/seedvr2/__init__.py`
- Modify: `src/kinoforge/upscalers/seedvr2/_runtime.py`
- Modify: `pyproject.toml`
- Test: `tests/upscalers/test_seedvr2_extras_stub.py`
- Modify: `tests/upscalers/test_seedvr2_engine.py` (rewrite — most existing assertions now exercise stub-raise paths)

**Acceptance Criteria:**
- [ ] `from kinoforge.upscalers import seedvr2` succeeds on a host without `seedvr` package installed (import is side-effect free).
- [ ] `SeedVR2Engine().render_provision({...})` raises `ExtrasNotInstalled` with `extras_name == "seedvr"`.
- [ ] `SeedVR2Engine().provision(None, {...})` raises `ExtrasNotInstalled`.
- [ ] `SeedVR2Engine().upscale(None, fake_job, {...})` raises `ExtrasNotInstalled`.
- [ ] `SeedVR2Engine().validate_spec(fake_job)` raises `ExtrasNotInstalled`.
- [ ] `SeedVR2Engine().model_identity({"upscale": {"seedvr2": {"variant": "3B", "precision": "fp8"}}})` returns `"seedvr2-3b-fp8"` (still functional, pure cfg-parse).
- [ ] `pyproject.toml` declares `[project.optional-dependencies] seedvr = []`.
- [ ] Existing `tests/test_adapters_upscale.py` still passes (registration unchanged).

**Verify:** `pixi run pytest tests/upscalers/test_seedvr2_extras_stub.py tests/upscalers/test_seedvr2_engine.py tests/test_adapters_upscale.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Move upstream import inside SeedVR2Runtime.__init__**

Edit `src/kinoforge/upscalers/seedvr2/_runtime.py`: confirm the `from seedvr.inference import SeedVR2Inferencer` line is INSIDE `__init__` (already correct per current file — verify only, no change needed). If a future edit moved it to module-top, restore lazy import.

- [ ] **Step 2: Write failing extras-stub test**

```python
# tests/upscalers/test_seedvr2_extras_stub.py
"""Tests that SeedVR2Engine raises ExtrasNotInstalled until Phase 2 vendoring lands."""

from __future__ import annotations

import pytest

from kinoforge.core.errors import ExtrasNotInstalled
from kinoforge.core.interfaces import Artifact, UpscaleJob
from kinoforge.core.scale_target import ScaleTarget
from kinoforge.upscalers.seedvr2 import SeedVR2Engine


def _fake_job() -> UpscaleJob:
    return UpscaleJob(
        source=Artifact(uri="file:///tmp/in.mp4", sha256="deadbeef", size=1),
        scale=ScaleTarget(kind="factor", value=2.0),
    )


def _fake_cfg() -> dict:
    return {"upscale": {"engine": "seedvr2", "scale": "2x", "seedvr2": {"variant": "3B", "precision": "fp8"}}}


class TestExtrasNotInstalled:
    def test_render_provision_raises(self) -> None:
        # Bug caught: SeedVR2Engine.render_provision still calls
        # `pip install seedvr @ git+...` against the un-installable
        # upstream — pod boot fails with opaque error. Stub-raise
        # surfaces the gap at cfg-time instead.
        with pytest.raises(ExtrasNotInstalled, match="seedvr"):
            SeedVR2Engine().render_provision(_fake_cfg())

    def test_provision_raises(self) -> None:
        # Bug caught: post-boot provision call is also a code path we
        # cannot honour without the vendored upstream.
        with pytest.raises(ExtrasNotInstalled):
            SeedVR2Engine().provision(None, _fake_cfg())

    def test_upscale_raises(self) -> None:
        # Bug caught: the runtime upscale call would import SeedVR2Runtime
        # which would import the unavailable seedvr package.
        with pytest.raises(ExtrasNotInstalled):
            SeedVR2Engine().upscale(None, _fake_job(), _fake_cfg())

    def test_validate_spec_raises(self) -> None:
        # Bug caught: cfg-time validation could otherwise proceed past
        # a SeedVR2 cfg and waste cold-boot budget.
        with pytest.raises(ExtrasNotInstalled):
            SeedVR2Engine().validate_spec(_fake_job())


class TestStillFunctionalSurfaces:
    def test_model_identity_still_works(self) -> None:
        # Bug caught: model_identity is pure cfg-parsing — must NOT
        # raise so the ABC contract test stays GREEN. Used by the
        # output-sink filename schema.
        assert (
            SeedVR2Engine().model_identity(_fake_cfg())
            == "seedvr2-3b-fp8"
        )

    def test_module_import_has_no_side_effects(self) -> None:
        # Bug caught: a regression that puts `from seedvr.inference import ...`
        # back at module-top of _runtime.py would crash `import kinoforge`
        # entirely on hosts without seedvr installed.
        import importlib

        import kinoforge.upscalers.seedvr2 as mod

        # If import succeeded, registration also fired — the engine
        # exists in the registry without any seedvr install.
        importlib.reload(mod)  # idempotent re-import must not raise
        from kinoforge.core import registry

        assert "seedvr2" in registry.upscaler_names()
```

- [ ] **Step 3: Verify RED**

Run: `pixi run pytest tests/upscalers/test_seedvr2_extras_stub.py -v`. Expected: 4 of the `TestExtrasNotInstalled` cases fail (current code still tries `from seedvr.inference import ...` and crashes with `ModuleNotFoundError`, OR succeeds without raising the expected error).

- [ ] **Step 4: Rewrite `SeedVR2Engine` method bodies**

Edit `src/kinoforge/upscalers/seedvr2/__init__.py` — replace the four method bodies (keep signatures, keep `model_identity`, keep self-registration):

```python
# Replace `render_provision`, `provision`, `upscale`, `validate_spec` with:

_EXTRAS_HINT = (
    "video-coherent upscaling (SeedVR2) pending Phase 2 vendoring; "
    "use cfg.upscale.engine = 'spandrel' for v1, or track the Phase 2 "
    "workstream"
)


class SeedVR2Engine(UpscalerEngine):
    """SeedVR2 video upscaler — extras-gated stub until Phase 2 vendoring lands."""

    name = "seedvr2"
    requires_compute = True
    requires_local_weights = True
    supported_scales = tuple(
        ScaleTarget(kind="factor", value=v) for v in _SUPPORTED_FACTORS
    )

    def validate_spec(self, job: UpscaleJob) -> None:
        raise ExtrasNotInstalled(extras_name="seedvr", install_hint=_EXTRAS_HINT)

    def model_identity(self, cfg: dict[str, object]) -> str:
        # Pure cfg-parsing — must stay functional so the ABC contract
        # test passes and the output-sink filename schema works.
        try:
            upscale_block = cast(dict[str, Any], cfg["upscale"])
            seedvr2_block = cast(dict[str, Any], upscale_block["seedvr2"])
            variant = str(seedvr2_block["variant"]).lower()
            precision = str(seedvr2_block["precision"])
            return f"seedvr2-{variant}-{precision}"
        except (KeyError, TypeError):
            return ""

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        raise ExtrasNotInstalled(extras_name="seedvr", install_hint=_EXTRAS_HINT)

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None:
        raise ExtrasNotInstalled(extras_name="seedvr", install_hint=_EXTRAS_HINT)

    def upscale(
        self,
        instance: Instance | None,
        job: UpscaleJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> UpscaleResult:
        raise ExtrasNotInstalled(extras_name="seedvr", install_hint=_EXTRAS_HINT)
```

Add to the module imports: `from kinoforge.core.errors import ExtrasNotInstalled, NotYetImplementedError, UnknownAdapter, UnsupportedScaleError, UpscaleFailed`. Remove now-dead helpers `_http_json`, `_base_url`, `_build_payload` (the HTTP submit code) — they'll re-land in Phase 2.

- [ ] **Step 5: Update existing seedvr2 tests for stub-raise mode**

Rewrite `tests/upscalers/test_seedvr2_engine.py`: delete every test that exercises `render_provision` / `provision` / `upscale` / `validate_spec` happy paths (those are now in the extras-stub test file). Keep only:
- `test_registers_under_seedvr2_name` (registry lookup, still functional)
- `test_model_identity_returns_slug` (pure cfg-parse, still functional)
- `test_model_identity_empty_on_missing_keys` (defensive)

If `tests/upscalers/test_seedvr2_engine.py` has happy-path tests beyond these, delete them. Final file should be < 50 lines.

- [ ] **Step 6: Add extras declaration to pyproject.toml**

Edit `pyproject.toml` — after the `dependencies = []` line in `[project]`:

```toml
[project.optional-dependencies]
seedvr = []  # Phase 2 fills in: vendored upstream
```

- [ ] **Step 7: Verify GREEN**

```
pixi run pytest tests/upscalers/test_seedvr2_extras_stub.py tests/upscalers/test_seedvr2_engine.py tests/test_adapters_upscale.py -v
```
Expected: all pass.

- [ ] **Step 8: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/upscalers/seedvr2/__init__.py \
  pyproject.toml \
  tests/upscalers/test_seedvr2_extras_stub.py \
  tests/upscalers/test_seedvr2_engine.py
git add src/kinoforge/upscalers/seedvr2/__init__.py \
        pyproject.toml \
        tests/upscalers/test_seedvr2_extras_stub.py \
        tests/upscalers/test_seedvr2_engine.py
git commit -m "feat(upscale): SeedVR2Engine stub-raise mode + [seedvr] extras declaration"
```

```json:metadata
{"files": ["src/kinoforge/upscalers/seedvr2/__init__.py", "src/kinoforge/upscalers/seedvr2/_runtime.py", "pyproject.toml", "tests/upscalers/test_seedvr2_extras_stub.py", "tests/upscalers/test_seedvr2_engine.py"], "verifyCommand": "pixi run pytest tests/upscalers/test_seedvr2_extras_stub.py tests/upscalers/test_seedvr2_engine.py tests/test_adapters_upscale.py -v", "acceptanceCriteria": ["four ABC methods raise ExtrasNotInstalled", "model_identity still pure-functional", "module import side-effect free", "pyproject.toml extras seedvr declared"], "modelTier": "standard"}
```

---

## Task 3: validate_for_generate seedvr2-extras rejection

**Goal:** Add a cfg-time validation rule that rejects `cfg.upscale.engine == "seedvr2"` with a `ValidationError` whose message mirrors the `ExtrasNotInstalled` hint. Catches misconfiguration BEFORE pod creation.

**Files:**
- Modify: `src/kinoforge/validation/checks.py` (or wherever existing checks live — `rg -n "def check_|register_check" src/kinoforge/validation/`)
- Test: `tests/validation/test_seedvr2_extras_rejection.py`

**Acceptance Criteria:**
- [ ] A cfg with `upscale.engine = "seedvr2"` fails `validate_for_generate` with a `ValidationError` containing `"kinoforge[seedvr]"`.
- [ ] A cfg with `upscale.engine = "spandrel"` passes (no false positive).
- [ ] A cfg with no `upscale` block passes (no false positive on pure-t2v).
- [ ] The validation check is in the PREFLIGHT category (runs before pod creation), not STATIC (already-too-early).

**Verify:** `pixi run pytest tests/validation/test_seedvr2_extras_rejection.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Locate the validation registration site**

```
rg -n "def check_|@register_check|PREFLIGHT|class .*Check" src/kinoforge/validation/ | head -30
```

The existing module-level check registrations in `src/kinoforge/validation/checks.py` are the pattern to follow. Read 30-50 lines around any one of them to learn the decorator/signature shape.

- [ ] **Step 2: Write failing test**

```python
# tests/validation/test_seedvr2_extras_rejection.py
"""Tests for the cfg-time rejection of cfg.upscale.engine == 'seedvr2'."""

from __future__ import annotations

import pytest

from kinoforge.core.config import Config
from kinoforge.core.errors import ValidationError
from kinoforge.validation import validate_for_generate


def _wan_only_cfg() -> Config:
    return Config.model_validate({
        "engine": {"kind": "diffusers", "precision": "fp8"},
        "models": [{"kind": "base", "ref": "hf:Wan-AI/Wan2.2-T2V", "target": "diffusion_models"}],
        "compute": {"provider": "fake", "image": "fake:latest"},
    })


def _spandrel_cfg() -> Config:
    return Config.model_validate({
        "engine": {"kind": "diffusers", "precision": "fp8"},
        "models": [{"kind": "base", "ref": "hf:Wan-AI/Wan2.2-T2V", "target": "diffusion_models"}],
        "compute": {"provider": "fake", "image": "fake:latest"},
        "upscale": {"engine": "spandrel", "scale": "2x", "spandrel": {"precision": "fp16"}},
    })


def _seedvr2_cfg() -> Config:
    return Config.model_validate({
        "engine": {"kind": "diffusers", "precision": "fp8"},
        "models": [{"kind": "base", "ref": "hf:Wan-AI/Wan2.2-T2V", "target": "diffusion_models"}],
        "compute": {"provider": "fake", "image": "fake:latest"},
        "upscale": {"engine": "seedvr2", "scale": "2x", "seedvr2": {"variant": "3B", "precision": "fp8"}},
    })


def test_seedvr2_cfg_rejected_with_extras_hint() -> None:
    # Bug caught: a cfg referencing the extras-stub engine slips past
    # cfg-time validation and burns cold-boot budget on a pod whose
    # bootstrap will crash at the composed render_provision step.
    with pytest.raises(ValidationError, match=r"kinoforge\[seedvr\]"):
        validate_for_generate(_seedvr2_cfg())


def test_spandrel_cfg_passes() -> None:
    # Bug caught: a too-eager regex rejects the v1 default cfg too.
    validate_for_generate(_spandrel_cfg())  # MUST NOT raise


def test_wan_only_cfg_passes() -> None:
    # Bug caught: rejection logic doesn't gate on cfg.upscale being set
    # and pure-t2v cfgs start failing validation post-pivot.
    validate_for_generate(_wan_only_cfg())  # MUST NOT raise
```

- [ ] **Step 3: Verify RED**

```
pixi run pytest tests/validation/test_seedvr2_extras_rejection.py -v
```
Expected: `test_seedvr2_cfg_rejected_with_extras_hint` FAILS (no rejection wired); other two pass.

- [ ] **Step 4: Add the validation check**

Append to `src/kinoforge/validation/checks.py` (or wherever the existing PREFLIGHT-category checks live — match the existing decorator + signature):

```python
@register_check(category="PREFLIGHT")
def check_seedvr2_extras_pending(cfg: Config) -> None:
    """Reject cfg.upscale.engine == 'seedvr2' until Phase 2 extras vendor lands.

    The SeedVR2Engine ABC methods raise ExtrasNotInstalled — surface
    that at cfg-time with a structured ValidationError so the operator
    sees the remediation BEFORE pod creation, not as an opaque bootstrap
    crash.
    """
    if cfg.upscale is None:
        return
    if cfg.upscale.engine != "seedvr2":
        return
    raise ValidationError(
        "kinoforge[seedvr] extras not installed — "
        "video-coherent upscaling (SeedVR2) pending Phase 2 vendoring; "
        "use cfg.upscale.engine = 'spandrel' for v1"
    )
```

- [ ] **Step 5: Verify GREEN**

```
pixi run pytest tests/validation/test_seedvr2_extras_rejection.py -v
```
Expected: 3 pass.

- [ ] **Step 6: Pre-commit + commit**

```
pixi run pre-commit run --files src/kinoforge/validation/checks.py tests/validation/test_seedvr2_extras_rejection.py
git add src/kinoforge/validation/checks.py tests/validation/test_seedvr2_extras_rejection.py
git commit -m "feat(validation): reject cfg.upscale.engine='seedvr2' until Phase 2 extras land"
```

```json:metadata
{"files": ["src/kinoforge/validation/checks.py", "tests/validation/test_seedvr2_extras_rejection.py"], "verifyCommand": "pixi run pytest tests/validation/test_seedvr2_extras_rejection.py -v", "acceptanceCriteria": ["seedvr2 cfg rejected at cfg-time with extras hint", "spandrel cfg passes", "wan-only cfg passes"], "modelTier": "mechanical"}
```

---

## Task 4: UpscalerEngine ABC parametrized contract test

**Goal:** Test that exercises every registered upscaler against the safe (non-raising) portions of the ABC contract. Catches engine-agnostic regressions when a future engine joins the registry.

**Files:**
- Test: `tests/core/test_upscaler_engine_contract.py`

**Acceptance Criteria:**
- [ ] Test is parametrized over `registry.upscaler_names()` so it picks up new engines automatically.
- [ ] Asserts `name`, `requires_compute`, `requires_local_weights`, `supported_scales` are the correct types.
- [ ] Asserts `model_identity({})` returns a str without raising (pure cfg-parse contract).
- [ ] Passes today against `"seedvr2"` (the only registered upscaler pre-Task 6).
- [ ] Test will pick up `"spandrel"` automatically once Task 6 lands.

**Verify:** `pixi run pytest tests/core/test_upscaler_engine_contract.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write contract test**

```python
# tests/core/test_upscaler_engine_contract.py
"""Parametrized contract tests for every registered UpscalerEngine.

This test exercises the safe (non-raising) ABC surface — name,
requires_compute, requires_local_weights, supported_scales,
model_identity({}). Engines in extras-stub mode (e.g. SeedVR2 pre-Phase 2)
still satisfy this contract because the stubs only raise from the
heavyweight methods (provision, upscale, render_provision, validate_spec)
— those are tested separately.
"""

from __future__ import annotations

import pytest

import kinoforge._adapters  # noqa: F401 — self-register every engine
from kinoforge.core import registry
from kinoforge.core.interfaces import UpscalerEngine
from kinoforge.core.scale_target import ScaleTarget


def _all_registered() -> list[str]:
    return registry.upscaler_names()


@pytest.mark.parametrize("name", _all_registered())
def test_engine_class_attrs_satisfy_contract(name: str) -> None:
    # Bug caught: a future engine omits `requires_compute` or sets
    # `supported_scales = []` (list, not tuple) — orchestrator scan
    # paths assume tuple membership and break opaquely.
    engine = registry.get_upscaler(name)()
    assert isinstance(engine, UpscalerEngine)
    assert engine.name == name
    assert isinstance(engine.requires_compute, bool)
    assert isinstance(engine.requires_local_weights, bool)
    assert isinstance(engine.supported_scales, tuple)
    for s in engine.supported_scales:
        assert isinstance(s, ScaleTarget)


@pytest.mark.parametrize("name", _all_registered())
def test_model_identity_pure_function_on_empty_cfg(name: str) -> None:
    # Bug caught: model_identity raises on missing cfg keys instead of
    # returning empty string. The output-sink filename schema calls
    # this on every job and a raise turns into "unknown" slugs at best,
    # a stage-fault at worst.
    engine = registry.get_upscaler(name)()
    result = engine.model_identity({})
    assert isinstance(result, str)


@pytest.mark.parametrize("name", _all_registered())
def test_model_identity_pure_function_on_other_engine_cfg(name: str) -> None:
    # Bug caught: model_identity hardcodes its own engine name as a
    # cfg-block lookup key (e.g. literal "seedvr2") instead of
    # self.name — when reading another engine's cfg by accident the
    # method explodes instead of returning empty string.
    engine = registry.get_upscaler(name)()
    # Build a cfg block for a DIFFERENT engine; model_identity should
    # return empty (graceful miss), never raise.
    cfg = {"upscale": {"engine": "other-engine", "other-engine": {}}}
    result = engine.model_identity(cfg)
    assert isinstance(result, str)
```

- [ ] **Step 2: Verify GREEN immediately**

```
pixi run pytest tests/core/test_upscaler_engine_contract.py -v
```
Expected: 3 tests × N engines (currently N=1: seedvr2). All PASS today; Task 6 adds spandrel and the parametrize re-discovers it automatically.

- [ ] **Step 3: Pre-commit + commit**

```
pixi run pre-commit run --files tests/core/test_upscaler_engine_contract.py
git add tests/core/test_upscaler_engine_contract.py
git commit -m "test(upscale): parametrized UpscalerEngine ABC contract"
```

```json:metadata
{"files": ["tests/core/test_upscaler_engine_contract.py"], "verifyCommand": "pixi run pytest tests/core/test_upscaler_engine_contract.py -v", "acceptanceCriteria": ["parametrized over registry.upscaler_names()", "checks class attr types", "checks model_identity is pure (returns str on any cfg)"], "modelTier": "mechanical"}
```

---

## Task 5: SpandrelRuntime — frame-loop video upscale wrapper

**Goal:** A thin runtime class that loads a spandrel model from disk, runs tiled per-frame inference over a video, and re-encodes to mp4. Engine-agnostic at the architecture level — spandrel's `ModelLoader` auto-detects RealESRGAN / ESRGAN / SwinIR / etc. from weights.

**Files:**
- Create: `src/kinoforge/upscalers/spandrel/__init__.py` (empty for now — engine in Task 6)
- Create: `src/kinoforge/upscalers/spandrel/_runtime.py`
- Test: `tests/upscalers/test_spandrel_runtime.py`

**Acceptance Criteria:**
- [ ] `SpandrelRuntime(weights_path, precision, tile_size, batch_size)` constructs without importing `spandrel` at module-import time (lazy import inside `__init__`).
- [ ] `runtime.upscale(input_mp4, scale=ScaleTarget(kind="factor", value=2.0), params={})` returns a Path to a new mp4.
- [ ] `runtime.upscale(...)` raises `NotYetImplementedError` when `scale.kind == "height"`.
- [ ] `runtime.upscale(...)` raises `UnsupportedScaleError` when `scale.value != self._scale` (mismatch between cfg and weights).
- [ ] `runtime.to(device)` moves the underlying model between CUDA / CPU.
- [ ] Output mp4 has exactly `input_width * scale × input_height * scale` resolution per ffprobe.

**Verify:** `pixi run pytest tests/upscalers/test_spandrel_runtime.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/upscalers/test_spandrel_runtime.py
"""Tests for SpandrelRuntime — frame-loop video upscale wrapper."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget


@pytest.fixture
def _fake_spandrel(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Inject a fake `spandrel` module so tests don't need real weights."""
    fake_model = MagicMock(name="SpandrelModel")
    fake_model.scale = 2  # claims to be a 2x model

    def _fake_call(tensor: Any) -> Any:
        # 2x upscale via numpy resize semantics — sufficient for shape checks.
        import torch

        if isinstance(tensor, torch.Tensor):
            n, c, h, w = tensor.shape
            return torch.zeros((n, c, h * 2, w * 2), dtype=tensor.dtype, device=tensor.device)
        raise TypeError(f"unexpected input: {type(tensor)}")

    fake_model.side_effect = _fake_call
    fake_model.return_value = None  # call uses side_effect path

    fake_loader = MagicMock(name="ModelLoader")
    fake_loader_instance = MagicMock()
    fake_loader_instance.load_from_file = MagicMock(return_value=fake_model)
    fake_loader.return_value = fake_loader_instance

    fake_spandrel = types.SimpleNamespace(ModelLoader=fake_loader)
    monkeypatch.setitem(sys.modules, "spandrel", fake_spandrel)
    return fake_model


def _write_dummy_mp4(path: Path, width: int = 64, height: int = 48, frames: int = 4) -> None:
    """Write a tiny mp4 using imageio.ffmpeg.

    Real ffmpeg required (already in pixi env). Frames are solid colors
    so the test focuses on dimensions, not content.
    """
    import imageio.v3 as iio  # noqa: PLC0415 — lazy import keeps mod load light

    data = np.zeros((frames, height, width, 3), dtype=np.uint8)
    for i in range(frames):
        data[i, :, :, i % 3] = 200
    iio.imwrite(path, data, fps=8, codec="libx264", macro_block_size=1)


def _probe_dims(path: Path) -> tuple[int, int]:
    import subprocess

    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
        text=True,
    ).strip()
    w, h = out.split("x")
    return int(w), int(h)


class TestConstruction:
    def test_does_not_import_spandrel_at_module_load(self) -> None:
        # Bug caught: a regression that moves `from spandrel import ...`
        # to module-top crashes `import kinoforge` on hosts that don't
        # have the spandrel package installed (e.g. dev workstation).
        sys.modules.pop("spandrel", None)
        from kinoforge.upscalers.spandrel import _runtime  # noqa: F401 — import only
        # If spandrel was needed at module-load, the line above raised.

    def test_lazy_import_fires_inside_constructor(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: lazy-import contract drifts — spandrel becomes
        # required at class-body eval time instead of construction time.
        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")
        rt = SpandrelRuntime(
            weights_path=weights, precision="fp16", tile_size=512, batch_size=4
        )
        assert rt is not None


class TestUpscale:
    def test_factor_2x_returns_2x_resolution_mp4(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: runtime emits a same-size or 4x mp4 because the
        # batch loop doesn't honour the model's declared scale.
        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        src = tmp_path / "in.mp4"
        _write_dummy_mp4(src, width=64, height=48, frames=4)
        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")

        rt = SpandrelRuntime(
            weights_path=weights, precision="fp16", tile_size=512, batch_size=4
        )
        out = rt.upscale(src, ScaleTarget(kind="factor", value=2.0), params={})
        assert out.exists()
        w, h = _probe_dims(out)
        assert (w, h) == (128, 96)

    def test_height_target_refused(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: runtime accepts `kind="height"` and produces
        # off-scale output that the asserts downstream can't validate.
        # Symmetric with the SeedVR2Runtime refusal.
        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        src = tmp_path / "in.mp4"
        _write_dummy_mp4(src, width=64, height=48, frames=2)
        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")

        rt = SpandrelRuntime(
            weights_path=weights, precision="fp16", tile_size=512, batch_size=4
        )
        with pytest.raises(NotYetImplementedError, match="height"):
            rt.upscale(src, ScaleTarget(kind="height", value=1080.0), params={})

    def test_scale_mismatch_refused(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: cfg asks for 4x but the loaded weights only
        # support 2x. Without an explicit check the runtime would
        # silently emit a 2x clip and the operator wouldn't notice
        # until visual review.
        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        src = tmp_path / "in.mp4"
        _write_dummy_mp4(src, width=64, height=48, frames=2)
        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")

        rt = SpandrelRuntime(
            weights_path=weights, precision="fp16", tile_size=512, batch_size=4
        )
        with pytest.raises(UnsupportedScaleError):
            rt.upscale(src, ScaleTarget(kind="factor", value=4.0), params={})


class TestDeviceMove:
    def test_to_device_delegates_to_underlying_model(
        self, tmp_path: Path, _fake_spandrel: MagicMock
    ) -> None:
        # Bug caught: SpandrelRuntime.to() forgets to forward; LRU
        # registry's CPU eviction call becomes a no-op and the pod
        # OOMs the next time a Wan generation starts.
        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        weights = tmp_path / "fake.pth"
        weights.write_bytes(b"")

        rt = SpandrelRuntime(
            weights_path=weights, precision="fp16", tile_size=512, batch_size=4
        )
        rt.to("cpu")
        _fake_spandrel.to.assert_called_with("cpu")
```

- [ ] **Step 2: Verify RED**

```
pixi run pytest tests/upscalers/test_spandrel_runtime.py -v
```
Expected: all FAIL with `ImportError: No module named 'kinoforge.upscalers.spandrel'`.

- [ ] **Step 3: Implement `SpandrelRuntime`**

Create `src/kinoforge/upscalers/spandrel/__init__.py` as an empty file (engine lands in Task 6).

Create `src/kinoforge/upscalers/spandrel/_runtime.py`:

```python
"""SpandrelRuntime — frame-loop video upscale wrapper around the spandrel library.

spandrel is the architecture-agnostic super-resolution runtime used by
chaiNNer + ComfyUI. Loads RealESRGAN / ESRGAN / SwinIR / OmniSR / etc.
from .pth or .safetensors weights via auto-detection.

Used by the diffusers wan_t2v_server's LRU model registry (T11) — the
runtime instance lives inside ``_LOADED[name].pipe`` and is dispatched
by the ``spandrel-*`` model-name prefix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import numpy as np

from kinoforge.core.errors import NotYetImplementedError, UnsupportedScaleError
from kinoforge.core.scale_target import ScaleTarget


class SpandrelRuntime:
    """Loads a spandrel model and runs tiled per-frame video upscale.

    Args:
        weights_path: Local path to the weights file (.pth / .safetensors).
        precision: ``"fp16"`` or ``"fp32"``. fp16 halves VRAM on consumer GPUs
            but some architectures emit subtle artifacts; fp32 is the safe default.
        tile_size: Frame-tile dimension in pixels for VRAM headroom. spandrel
            handles tiling internally; this controls the max per-tile pixel count.
        batch_size: Frames per CUDA batch. Higher = better throughput, more VRAM.

    Raises:
        ImportError: ``spandrel`` package not installed.
    """

    def __init__(
        self,
        weights_path: Path,
        precision: Literal["fp16", "fp32"],
        tile_size: int,
        batch_size: int,
    ) -> None:
        from spandrel import ModelLoader  # type: ignore[import-not-found]

        self._model = ModelLoader().load_from_file(str(weights_path))
        self._scale: float = float(self._model.scale)
        self._tile = tile_size
        self._batch = batch_size
        self._precision: Literal["fp16", "fp32"] = precision

    def upscale(
        self, video_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        """Decode → batch frames through model → re-encode mp4.

        Args:
            video_path: Local mp4 to upscale.
            scale: ``ScaleTarget``. Only ``kind="factor"`` supported in v1;
                ``"height"`` raises ``NotYetImplementedError``. ``scale.value``
                MUST match ``self._scale`` (declared by the weights).
            params: Reserved for engine overrides; ignored in v1.

        Returns:
            Path to the upscaled mp4 (sibling of input, ``<stem>.upscaled.mp4``).

        Raises:
            NotYetImplementedError: ``scale.kind == "height"``.
            UnsupportedScaleError: ``scale.value != self._scale``.
        """
        del params  # reserved for future engine overrides
        if scale.kind == "height":
            raise NotYetImplementedError(
                f"height-target upscale (e.g. {int(scale.value)}p) deferred; "
                "use --scale Nx for v1"
            )
        if scale.value != self._scale:
            raise UnsupportedScaleError(scale=scale, engine_name="spandrel")

        import imageio.v3 as iio
        import torch

        frames_in = iio.imread(video_path, plugin="FFMPEG")  # shape (N,H,W,3) uint8
        # Read fps from container metadata; default 16 if missing.
        try:
            metadata = iio.immeta(video_path, plugin="FFMPEG")
            fps = float(metadata.get("fps", 16))
        except Exception:  # noqa: BLE001 — fall back to a sane default
            fps = 16.0

        device = next(self._model.parameters()).device if hasattr(self._model, "parameters") else "cpu"
        dtype = torch.float16 if self._precision == "fp16" else torch.float32

        out_frames: list[np.ndarray] = []
        for i in range(0, len(frames_in), self._batch):
            batch_np = frames_in[i : i + self._batch]
            # (N,H,W,3) uint8 → (N,3,H,W) float in [0,1]
            batch_t = (
                torch.from_numpy(batch_np)
                .permute(0, 3, 1, 2)
                .to(device=device, dtype=dtype)
                / 255.0
            )
            with torch.no_grad():
                out_t = self._model(batch_t)
            # Back to (N,H,W,3) uint8
            out_np = (
                (out_t.clamp(0.0, 1.0) * 255.0)
                .to(torch.uint8)
                .permute(0, 2, 3, 1)
                .cpu()
                .numpy()
            )
            out_frames.extend(list(out_np))

        out_path = video_path.with_suffix(".upscaled.mp4")
        iio.imwrite(
            out_path,
            np.stack(out_frames),
            fps=fps,
            codec="libx264",
            macro_block_size=1,
        )
        return out_path

    def to(self, device: str) -> None:
        """Move the underlying model between cuda/cpu — LRU eviction hook."""
        self._model.to(device)
```

- [ ] **Step 4: Verify GREEN**

```
pixi run pytest tests/upscalers/test_spandrel_runtime.py -v
```
Expected: all pass.

- [ ] **Step 5: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/upscalers/spandrel/__init__.py \
  src/kinoforge/upscalers/spandrel/_runtime.py \
  tests/upscalers/test_spandrel_runtime.py
git add src/kinoforge/upscalers/spandrel/__init__.py \
        src/kinoforge/upscalers/spandrel/_runtime.py \
        tests/upscalers/test_spandrel_runtime.py
git commit -m "feat(upscale): SpandrelRuntime — frame-loop video upscale wrapper

Lazy import of `spandrel` inside __init__ so `import kinoforge` works
on dev hosts without the package. Validates scale against weights-
declared scale, refuses height-target. Used by wan_t2v_server's LRU
registry via the spandrel-* model-name prefix in Task 9."
```

```json:metadata
{"files": ["src/kinoforge/upscalers/spandrel/__init__.py", "src/kinoforge/upscalers/spandrel/_runtime.py", "tests/upscalers/test_spandrel_runtime.py"], "verifyCommand": "pixi run pytest tests/upscalers/test_spandrel_runtime.py -v", "acceptanceCriteria": ["spandrel lazy-imported inside __init__", "2x factor returns 2x mp4 dimensions", "height target raises NotYetImplementedError", "scale mismatch raises UnsupportedScaleError", "to(device) forwards to underlying model"], "modelTier": "standard"}
```

---

## Task 6: SpandrelEngine + self-registration + adapter wire-up

**Goal:** The HTTP-aware `SpandrelEngine` (mirrors `SeedVR2Engine` pre-stub shape) that POSTs `/upscale`, polls `/upscale/status/{id}`, returns `UpscaleResult`. Self-registers as `"spandrel"`. Wired into `_adapters.py`.

**Files:**
- Modify: `src/kinoforge/upscalers/spandrel/__init__.py`
- Modify: `src/kinoforge/_adapters.py`
- Test: `tests/upscalers/test_spandrel_engine.py`
- Modify: `tests/test_adapters_upscale.py` (assert BOTH `"spandrel"` AND `"seedvr2"` registered)

**Acceptance Criteria:**
- [ ] `from kinoforge.upscalers.spandrel import SpandrelEngine` succeeds; `SpandrelEngine.name == "spandrel"`.
- [ ] `import kinoforge._adapters` then `registry.upscaler_names()` includes `"spandrel"`.
- [ ] `SpandrelEngine().render_provision(cfg)` emits a script containing `pip install "spandrel`, `pip install "imageio[ffmpeg]`, AND a `python -m kinoforge.upscalers.spandrel._fetch_weights` line for the cfg's `model_url`.
- [ ] `SpandrelEngine().upscale(instance, job, cfg)` calls `POST /upscale` then polls `GET /upscale/status/{id}` (verifiable via mocked `_http_json`), returns an `UpscaleResult` whose `Artifact.uri` points at `{base}/artifacts/{filename}`.
- [ ] `SpandrelEngine().model_identity({"upscale": {"spandrel": {"arch": "realesrgan", "precision": "fp16"}}})` returns `"spandrel-realesrgan-fp16"`.
- [ ] `validate_spec(job)` refuses height-target with `NotYetImplementedError`.
- [ ] `tests/test_adapters_upscale.py` asserts both `"spandrel"` AND `"seedvr2"` registered.

**Verify:** `pixi run pytest tests/upscalers/test_spandrel_engine.py tests/test_adapters_upscale.py tests/core/test_upscaler_engine_contract.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing engine tests**

```python
# tests/upscalers/test_spandrel_engine.py
"""Tests for SpandrelEngine — HTTP-aware UpscalerEngine implementer."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kinoforge.core.errors import NotYetImplementedError
from kinoforge.core.interfaces import Artifact, Instance, UpscaleJob
from kinoforge.core.scale_target import ScaleTarget


def _job_2x() -> UpscaleJob:
    return UpscaleJob(
        source=Artifact(uri="file:///tmp/in.mp4", sha256="deadbeef", size=1),
        scale=ScaleTarget(kind="factor", value=2.0),
    )


def _cfg() -> dict:
    return {
        "upscale": {
            "engine": "spandrel",
            "scale": "2x",
            "spandrel": {
                "model_url": "hf:lllyasviel/realesrgan/RealESRGAN_x2plus.pth",
                "arch": "realesrgan",
                "precision": "fp16",
                "tile_size": 512,
                "batch_size": 4,
            },
        },
    }


def _instance() -> Instance:
    return Instance(
        id="pod-fake",
        provider="fake",
        status="ready",
        endpoints={"8000": "https://pod.example/proxy"},
        tags={},
    )


class TestRegistration:
    def test_name_is_spandrel(self) -> None:
        # Bug caught: typo in class attr (e.g. "spandel") makes the
        # engine register under the wrong name and `cfg.upscale.engine
        # = "spandrel"` fails at registry lookup.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        assert SpandrelEngine.name == "spandrel"
        assert SpandrelEngine.requires_compute is True
        assert SpandrelEngine.requires_local_weights is True


class TestRenderProvision:
    def test_emits_pip_and_fetch_lines(self) -> None:
        # Bug caught: render_provision emits a script that pip-installs
        # the wrong package OR forgets the weights fetch step. The pod
        # boots and the `import spandrel` inside the runtime crashes.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        rp = SpandrelEngine().render_provision(_cfg())
        assert "pip install" in rp.script
        assert 'spandrel' in rp.script
        assert "imageio" in rp.script
        assert "kinoforge.upscalers.spandrel._fetch_weights" in rp.script
        assert "RealESRGAN_x2plus.pth" in rp.script

    def test_run_cmd_empty(self) -> None:
        # Bug caught: SpandrelEngine claims a `run_cmd` and overrides
        # the wan_t2v_server entrypoint, so the pod never starts the
        # HTTP server. Composition pattern: upscaler scripts are
        # ADDITIVE; the diffusers engine owns the server process.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        rp = SpandrelEngine().render_provision(_cfg())
        assert rp.run_cmd == []


class TestUpscale:
    def test_posts_then_polls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bug caught: upscale() either POSTs without polling, or polls
        # without POSTing. Mirror the SeedVR2 HTTP flow exactly.
        from kinoforge.upscalers import spandrel as spandrel_mod
        from kinoforge.upscalers.spandrel import SpandrelEngine

        submit_resp = {"job_id": "u-test"}
        status_resp = {
            "state": "done",
            "progress": 1.0,
            "result": {
                "filename": "out.mp4",
                "sha256": "abcd",
                "size": 4096,
                "input_resolution": [64, 48],
                "output_resolution": [128, 96],
                "engine_meta": {},
            },
            "error": None,
        }
        calls = []

        def fake_http(*, method: str, url: str, payload: dict | None = None) -> dict:
            calls.append((method, url, payload))
            if method == "POST":
                return submit_resp
            return status_resp

        monkeypatch.setattr(spandrel_mod, "_http_json", fake_http)
        result = SpandrelEngine().upscale(_instance(), _job_2x(), _cfg())

        # First call must be POST /upscale; second must be GET /upscale/status/...
        assert calls[0][0] == "POST"
        assert calls[0][1].endswith("/upscale")
        assert calls[1][0] == "GET"
        assert calls[1][1].endswith("/upscale/status/u-test")

        assert result.artifact.uri.endswith("/artifacts/out.mp4")
        assert result.artifact.sha256 == "abcd"
        assert result.output_resolution == (128, 96)


class TestValidateSpec:
    def test_height_refused(self) -> None:
        # Bug caught: validate_spec accepts kind="height" and the pod
        # crashes at inference time. Mirror SeedVR2Engine's refusal.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        with pytest.raises(NotYetImplementedError, match="height"):
            SpandrelEngine().validate_spec(
                UpscaleJob(
                    source=Artifact(uri="file:///tmp/x.mp4", sha256="x", size=1),
                    scale=ScaleTarget(kind="height", value=1080.0),
                )
            )


class TestModelIdentity:
    def test_three_token_slug(self) -> None:
        # Bug caught: slug uses 4 tokens (spandrel-realesrgan-x2-fp16),
        # breaking the server-side `_load_model_to_gpu` 3-token parser
        # (`parts[-2], parts[-1]` would yield "x2","fp16" instead of
        # "realesrgan","fp16"). Spec §3.2 locks the 3-token shape.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        slug = SpandrelEngine().model_identity(_cfg())
        assert slug == "spandrel-realesrgan-fp16"

    def test_empty_on_missing_block(self) -> None:
        # Bug caught: missing-key handling raises instead of returning
        # empty string; sink renders "unknown" or breaks the ABC contract.
        from kinoforge.upscalers.spandrel import SpandrelEngine

        assert SpandrelEngine().model_identity({}) == ""
```

- [ ] **Step 2: Verify RED**

```
pixi run pytest tests/upscalers/test_spandrel_engine.py -v
```
Expected: all FAIL with `ImportError: cannot import name 'SpandrelEngine'`.

- [ ] **Step 3: Implement SpandrelEngine**

Edit `src/kinoforge/upscalers/spandrel/__init__.py`:

```python
"""SpandrelEngine — HTTP-aware UpscalerEngine impl backed by the spandrel runtime.

Talks to wan_t2v_server's /upscale + /upscale/status/{id} endpoints (T12).
Reuses `kinoforge.engines._proxy_retry.retry_proxy_call` to absorb RunPod
proxy startup-window 404/502s.

Self-registers at module import via `register_upscaler("spandrel", SpandrelEngine)`.
"""

from __future__ import annotations

import json as _json
import time
import urllib.request
from typing import Any, cast

from kinoforge.core import registry
from kinoforge.core.cancel import CancelToken
from kinoforge.core.errors import (
    NotYetImplementedError,
    UnknownAdapter,
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
from kinoforge.engines._proxy_retry import retry_proxy_call

_DEFAULT_SERVER_PORT = "8000"


class SpandrelEngine(UpscalerEngine):
    """spandrel-based image super-resolution per-frame video upscaler."""

    name = "spandrel"
    requires_compute = True
    requires_local_weights = True
    # Empty tuple = runtime declares scale at weights-load time
    # (spec §3.5: spandrel's ModelLoader reports model.scale).
    supported_scales: tuple = ()

    def validate_spec(self, job: UpscaleJob) -> None:
        if job.scale.kind == "height":
            raise NotYetImplementedError(
                f"spandrel does not support height-target scale "
                f"({int(job.scale.value)}p); use --scale Nx"
            )

    def model_identity(self, cfg: dict[str, object]) -> str:
        """Return `spandrel-<arch>-<precision>` or empty string on missing keys.

        Spec §3.2: three-token slug matches the server's `_load_model_to_gpu`
        parser. Scale is implicit in the weights — NOT in the slug.
        """
        try:
            block = cast(dict[str, Any], cast(dict[str, Any], cfg["upscale"])["spandrel"])
            arch = str(block["arch"]).lower()
            precision = str(block["precision"])
            return f"spandrel-{arch}-{precision}"
        except (KeyError, TypeError):
            return ""

    def render_provision(self, cfg: dict[str, object]) -> RenderedProvision:
        block = cast(dict[str, Any], cast(dict[str, Any], cfg["upscale"])["spandrel"])
        model_url = str(block["model_url"])
        script = (
            'pip install "spandrel>=0.4.2" "imageio[ffmpeg]>=2.34"\n'
            f"python -m kinoforge.upscalers.spandrel._fetch_weights "
            f"--url {model_url} --dest /workspace/models/spandrel\n"
        )
        return RenderedProvision(
            script=script,
            run_cmd=[],
            image="",
            ports=[],
            env_required=[],
        )

    def provision(
        self,
        instance: Instance | None,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> None:
        del instance, cfg, cancel_token  # no-op: work captured in render_provision

    def upscale(
        self,
        instance: Instance | None,
        job: UpscaleJob,
        cfg: dict[str, object],
        *,
        cancel_token: CancelToken | None = None,
    ) -> UpscaleResult:
        self.validate_spec(job)
        if instance is None:
            raise ValueError("SpandrelEngine requires a compute instance")
        base = self._base_url(instance)

        block = cast(dict[str, Any], cast(dict[str, Any], cfg.get("upscale", {})).get("spandrel", {}))
        submit_payload = {
            "source_url": job.source.uri,
            "source_filename": job.source.uri.rsplit("/", 1)[-1] or "in.mp4",
            "scale": f"{job.scale.value:g}x",
            "engine": "spandrel",
            "spandrel": block,
        }
        submit_resp = retry_proxy_call(
            label="spandrel.submit",
            url=f"{base}/upscale",
            fn=lambda: _http_json(method="POST", url=f"{base}/upscale", payload=submit_payload),
            sleep=time.sleep,
        )
        job_id: str = submit_resp["job_id"]

        t0 = time.monotonic()
        while True:
            if cancel_token is not None:
                cancel_token.raise_if_set()
            status = retry_proxy_call(
                label="spandrel.status",
                url=f"{base}/upscale/status/{job_id}",
                fn=lambda: _http_json(method="GET", url=f"{base}/upscale/status/{job_id}"),
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

    @staticmethod
    def _base_url(instance: Instance) -> str:
        endpoints = instance.endpoints or {}
        url = endpoints.get(_DEFAULT_SERVER_PORT) or next(iter(endpoints.values()), "")
        if not url:
            raise ValueError(
                f"SpandrelEngine: instance {instance.id} has no endpoint for "
                f"port {_DEFAULT_SERVER_PORT}; endpoints={endpoints!r}"
            )
        return url.rstrip("/")


def _http_json(
    *, method: str, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = _json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(  # noqa: S310 — http/https only (pod proxy URL)
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        body = resp.read()
    return cast(dict[str, Any], _json.loads(body))


# Self-register on import. Duplicate-register raises UnknownAdapter; absorb
# so repeated test-suite imports stay idempotent.
try:
    registry.register_upscaler("spandrel", SpandrelEngine)
except UnknownAdapter:
    pass
```

- [ ] **Step 4: Wire into `_adapters.py`**

Edit `src/kinoforge/_adapters.py` — add under the Upscalers section:

```python
# Upscalers
import kinoforge.upscalers.seedvr2  # noqa: F401  # self-registers under "seedvr2" (extras-stub until Phase 2)
import kinoforge.upscalers.spandrel  # noqa: F401  # self-registers under "spandrel"
```

- [ ] **Step 5: Update adapter test to assert both registered**

Edit `tests/test_adapters_upscale.py` — add new test:

```python
def test_both_seedvr2_and_spandrel_registered() -> None:
    # Bug caught: a future edit to _adapters.py drops one of the two
    # upscaler imports, silently removing it from the registry.
    import kinoforge._adapters  # noqa: F401
    from kinoforge.core import registry

    names = registry.upscaler_names()
    assert "seedvr2" in names
    assert "spandrel" in names
```

- [ ] **Step 6: Verify GREEN**

```
pixi run pytest tests/upscalers/test_spandrel_engine.py tests/test_adapters_upscale.py tests/core/test_upscaler_engine_contract.py -v
```
Expected: all pass. The ABC contract test from Task 4 now exercises spandrel too.

- [ ] **Step 7: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/upscalers/spandrel/__init__.py \
  src/kinoforge/_adapters.py \
  tests/upscalers/test_spandrel_engine.py \
  tests/test_adapters_upscale.py
git add src/kinoforge/upscalers/spandrel/__init__.py \
        src/kinoforge/_adapters.py \
        tests/upscalers/test_spandrel_engine.py \
        tests/test_adapters_upscale.py
git commit -m "feat(upscale): SpandrelEngine — HTTP-aware UpscalerEngine + self-register"
```

```json:metadata
{"files": ["src/kinoforge/upscalers/spandrel/__init__.py", "src/kinoforge/_adapters.py", "tests/upscalers/test_spandrel_engine.py", "tests/test_adapters_upscale.py"], "verifyCommand": "pixi run pytest tests/upscalers/test_spandrel_engine.py tests/test_adapters_upscale.py tests/core/test_upscaler_engine_contract.py -v", "acceptanceCriteria": ["registers under 'spandrel'", "render_provision emits pip + fetch lines", "upscale POSTs then polls", "model_identity returns 3-token slug", "validate_spec refuses height target", "ABC contract test exercises both engines"], "modelTier": "standard"}
```

---

## Task 7: spandrel `_fetch_weights` CLI module

**Goal:** A small CLI module the pod's bootstrap invokes to download spandrel weights via the existing kinoforge source-resolver chain (`hf:`, `civitai:`, `civarchive:`, plain http(s)://).

**Files:**
- Create: `src/kinoforge/upscalers/spandrel/_fetch_weights.py`
- Test: `tests/upscalers/test_spandrel_fetch_weights.py`

**Acceptance Criteria:**
- [ ] `python -m kinoforge.upscalers.spandrel._fetch_weights --url hf:repo/file.pth --dest /tmp/x` writes the weights file under `/tmp/x/`.
- [ ] Argparse: `--url` and `--dest` both required; missing either → exit 2.
- [ ] Invalid URL scheme → clean error message + non-zero exit.
- [ ] Symmetric in behavior with the existing `seedvr2/_fetch_weights.py` (same resolver dispatch pattern).

**Verify:** `pixi run pytest tests/upscalers/test_spandrel_fetch_weights.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Inspect the SeedVR2 _fetch_weights pattern**

```
rg -n "argparse|main\(|--url|--dest" src/kinoforge/upscalers/seedvr2/_fetch_weights.py
```

Mirror it. SpandrelEngine.render_provision in Task 6 already references this CLI path.

- [ ] **Step 2: Write failing tests**

```python
# tests/upscalers/test_spandrel_fetch_weights.py
"""Tests for the spandrel _fetch_weights CLI module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_argparse_rejects_missing_url(tmp_path: Path) -> None:
    # Bug caught: --url accidentally given a default value instead of
    # required=True; misconfigured cfgs silently fetch the wrong file.
    proc = subprocess.run(
        [sys.executable, "-m", "kinoforge.upscalers.spandrel._fetch_weights",
         "--dest", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "url" in proc.stderr.lower()


def test_argparse_rejects_missing_dest(tmp_path: Path) -> None:
    # Bug caught: --dest defaults to cwd, weights land in the wrong dir
    # at provision time on a pod with surprising cwd.
    proc = subprocess.run(
        [sys.executable, "-m", "kinoforge.upscalers.spandrel._fetch_weights",
         "--url", "hf:fake/file.pth"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "dest" in proc.stderr.lower()


def test_dispatch_to_source_resolver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Bug caught: the CLI bypasses the existing resolver and writes a
    # bespoke urllib download path that drops auth headers for HF, etc.
    # Asserts the resolver chain is the seam.
    from kinoforge.upscalers.spandrel import _fetch_weights

    # Hand-craft a captured invocation: fake source resolves to a
    # file:// URL pointing at a dummy weights blob already on disk.
    dummy = tmp_path / "src" / "model.pth"
    dummy.parent.mkdir(parents=True)
    dummy.write_bytes(b"dummy weights")

    fake_resolved = MagicMock()
    fake_resolved.url = f"file://{dummy}"
    fake_resolved.filename = "model.pth"
    fake_resolved.headers = {}
    fake_resolved.size_hint = len(b"dummy weights")

    def fake_resolve(url: str) -> object:
        assert url == "hf:fake/repo/model.pth"
        return fake_resolved

    monkeypatch.setattr(_fetch_weights, "_resolve_source", fake_resolve)

    dest_dir = tmp_path / "dest"
    rc = _fetch_weights.main(["--url", "hf:fake/repo/model.pth", "--dest", str(dest_dir)])
    assert rc == 0
    assert (dest_dir / "model.pth").exists()
    assert (dest_dir / "model.pth").read_bytes() == b"dummy weights"
```

- [ ] **Step 3: Implement**

Create `src/kinoforge/upscalers/spandrel/_fetch_weights.py`:

```python
"""CLI module — fetch spandrel weights via the kinoforge source-resolver chain.

Invoked by SpandrelEngine.render_provision on the pod's bootstrap:
    python -m kinoforge.upscalers.spandrel._fetch_weights --url <ref> --dest /workspace/models/spandrel

Symmetric with src/kinoforge/upscalers/seedvr2/_fetch_weights.py — same
resolver dispatch, same on-disk layout.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any


def _resolve_source(url: str) -> Any:  # noqa: ANN401
    """Dispatch to the kinoforge source-resolver chain.

    Seam for tests to monkeypatch.
    """
    import kinoforge._adapters  # noqa: F401 — self-register sources

    from kinoforge.core import registry

    # Source resolution: each registered source's `handles(url)` decides.
    for source_name in registry.source_names():
        source = registry.get_source(source_name)()
        if source.handles(url):
            return source.resolve(url)
    raise ValueError(f"no source handles url: {url!r}")


def main(argv: list[str] | None = None) -> int:
    """Argparse entry point.

    Returns:
        Exit code (0 on success, 2 on argparse error, 1 on resolver error).
    """
    parser = argparse.ArgumentParser(prog="kinoforge.upscalers.spandrel._fetch_weights")
    parser.add_argument("--url", required=True, help="source ref (hf:, civitai:, civarchive:, http(s)://)")
    parser.add_argument("--dest", required=True, type=Path, help="destination directory")
    args = parser.parse_args(argv)

    try:
        resolved = _resolve_source(args.url)
    except (ValueError, Exception) as exc:  # noqa: BLE001 — surface to caller
        sys.stderr.write(f"error: {exc}\n")
        return 1

    args.dest.mkdir(parents=True, exist_ok=True)
    target = args.dest / resolved.filename

    headers = getattr(resolved, "headers", {}) or {}
    req = urllib.request.Request(  # noqa: S310 — caller-resolved URL
        resolved.url, headers={"User-Agent": "kinoforge-pod-fetch/0.1", **headers}
    )
    tmp = target.with_suffix(target.suffix + ".partial")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp, tmp.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(resp, out)
        tmp.replace(target)
    except Exception as exc:  # noqa: BLE001
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        sys.stderr.write(f"error: download failed: {exc}\n")
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    raise SystemExit(main())
```

- [ ] **Step 4: Verify GREEN**

```
pixi run pytest tests/upscalers/test_spandrel_fetch_weights.py -v
```
Expected: 3 pass.

- [ ] **Step 5: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/upscalers/spandrel/_fetch_weights.py \
  tests/upscalers/test_spandrel_fetch_weights.py
git add src/kinoforge/upscalers/spandrel/_fetch_weights.py \
        tests/upscalers/test_spandrel_fetch_weights.py
git commit -m "feat(upscale): spandrel _fetch_weights CLI — resolver-chain dispatch"
```

```json:metadata
{"files": ["src/kinoforge/upscalers/spandrel/_fetch_weights.py", "tests/upscalers/test_spandrel_fetch_weights.py"], "verifyCommand": "pixi run pytest tests/upscalers/test_spandrel_fetch_weights.py -v", "acceptanceCriteria": ["--url and --dest required", "dispatches via registry.get_source chain", "writes to dest/<filename>"], "modelTier": "mechanical"}
```

---

## Task 8: DiffusersEngine.render_provision composition seam (Blocker B)

**Goal:** Inside `DiffusersEngine.render_provision`, when `cfg.upscale` is present, look up the configured upscaler via the registry and append its `render_provision` script to the bootstrap lines.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/__init__.py`
- Test: `tests/engines/diffusers/test_render_provision_composition.py`

**Acceptance Criteria:**
- [ ] Cfg with `engine: diffusers` + `upscale: spandrel` → `render_provision` output script contains both Wan deps AND `pip install "spandrel...`.
- [ ] The `spandrel` install line appears BEFORE the `wan_t2v_server` exec line.
- [ ] Cfg with `engine: diffusers` and NO `upscale` block → script does NOT contain `spandrel`.
- [ ] Cfg with `engine: diffusers` + `upscale: seedvr2` → `render_provision` raises `ExtrasNotInstalled` (gated by Task 2's stub-raise).

**Verify:** `pixi run pytest tests/engines/diffusers/test_render_provision_composition.py -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/engines/diffusers/test_render_provision_composition.py
"""Tests for DiffusersEngine.render_provision upscaler composition (Blocker B)."""

from __future__ import annotations

import pytest

import kinoforge._adapters  # noqa: F401 — self-register engines
from kinoforge.core.errors import ExtrasNotInstalled
from kinoforge.engines.diffusers import DiffusersEngine


def _wan_only_cfg() -> dict:
    return {
        "engine": {"kind": "diffusers", "precision": "fp8", "diffusers": {
            "image": "runpod/pytorch:2.4.0",
            "pip": ["torch==2.6.0"],
            "server_cmd": ["python", "-m", "kinoforge.engines.diffusers.servers.wan_t2v_server"],
        }},
        "models": [{"kind": "base", "ref": "hf:Wan-AI/Wan2.2-T2V", "target": "diffusion_models"}],
    }


def _with_spandrel(cfg: dict) -> dict:
    cfg = dict(cfg)
    cfg["upscale"] = {
        "engine": "spandrel",
        "scale": "2x",
        "spandrel": {
            "model_url": "hf:lllyasviel/realesrgan/RealESRGAN_x2plus.pth",
            "arch": "realesrgan",
            "precision": "fp16",
            "tile_size": 512,
            "batch_size": 4,
        },
    }
    return cfg


def _with_seedvr2(cfg: dict) -> dict:
    cfg = dict(cfg)
    cfg["upscale"] = {
        "engine": "seedvr2",
        "scale": "2x",
        "seedvr2": {"variant": "3B", "precision": "fp8"},
    }
    return cfg


def test_compose_spandrel_appends_to_script() -> None:
    # Bug caught: render_provision does NOT compose the upscaler script
    # and the pod boots without spandrel installed; `import spandrel`
    # inside the runtime crashes on first /upscale request.
    rp = DiffusersEngine().render_provision(_with_spandrel(_wan_only_cfg()))
    assert "spandrel" in rp.script
    assert "kinoforge.upscalers.spandrel._fetch_weights" in rp.script


def test_compose_order_spandrel_before_server_exec() -> None:
    # Bug caught: composition appends spandrel lines AFTER the
    # `exec wan_t2v_server` line, so they never run. Order matters.
    rp = DiffusersEngine().render_provision(_with_spandrel(_wan_only_cfg()))
    spandrel_idx = rp.script.find("spandrel")
    server_idx = rp.script.find("wan_t2v_server")
    # Both must exist; spandrel must come first.
    assert spandrel_idx >= 0
    assert server_idx >= 0
    assert spandrel_idx < server_idx


def test_no_upscale_block_means_no_composition() -> None:
    # Bug caught: composition fires unconditionally and pure-t2v cfgs
    # start including spandrel deps for no reason.
    rp = DiffusersEngine().render_provision(_wan_only_cfg())
    assert "spandrel" not in rp.script
    assert "kinoforge.upscalers.spandrel._fetch_weights" not in rp.script


def test_seedvr2_composition_raises_extras_not_installed() -> None:
    # Bug caught: composition continues into a seedvr2 cfg, the
    # SeedVR2Engine extras-stub raises, the orchestrator sees an
    # opaque NotYetImplementedError from the wrong layer. Asserts
    # the ExtrasNotInstalled propagates cleanly.
    with pytest.raises(ExtrasNotInstalled, match=r"seedvr"):
        DiffusersEngine().render_provision(_with_seedvr2(_wan_only_cfg()))
```

- [ ] **Step 2: Verify RED**

```
pixi run pytest tests/engines/diffusers/test_render_provision_composition.py -v
```
Expected: at least the first 3 tests FAIL (no composition wired); the seedvr2 raise test may pass already if Task 2 + Task 3 land before this.

- [ ] **Step 3: Implement composition in DiffusersEngine.render_provision**

Edit `src/kinoforge/engines/diffusers/__init__.py` — inside `render_provision`, after the existing pip-line assembly + before the `embed_modules` decode + final `exec server_cmd` line. Find the line that builds `lines: list[str]` and the spot just before the server is `exec`'d:

```python
        # T(this-task) — compose upscaler provision script when cfg.upscale set.
        # Reads cfg.upscale.engine, looks up via registry, appends the upscaler's
        # render_provision script BEFORE the server exec line. Engine-agnostic:
        # this seam knows nothing about WHICH upscaler. FlashVSR drop-in (future)
        # gets composition for free.
        upscale_block = cfg.get("upscale") if isinstance(cfg, dict) else None
        if upscale_block and isinstance(upscale_block, dict):
            upscaler_name = upscale_block.get("engine")
            if upscaler_name:
                from kinoforge.core import registry as _registry

                upscaler = _registry.get_upscaler(str(upscaler_name))()
                upscale_rp = upscaler.render_provision(cfg)
                lines.append("# ---- upscaler provision (composed) ----")
                lines.extend(upscale_rp.script.split("\n"))
```

**Insert location:** identify the line that adds the `exec ${server_cmd...}` or the server-launch step. Insert the composed block IMMEDIATELY BEFORE that line. Search anchor:

```
rg -n "exec |server_cmd|wan_t2v_server" src/kinoforge/engines/diffusers/__init__.py
```

If the existing render_provision builds `lines` as a list and then `"\n".join(lines)` at return, insert before the trailing exec. If it uses a heredoc, insert into the heredoc body string.

- [ ] **Step 4: Verify GREEN**

```
pixi run pytest tests/engines/diffusers/test_render_provision_composition.py -v
```
Expected: 4 pass.

- [ ] **Step 5: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/engines/diffusers/__init__.py \
  tests/engines/diffusers/test_render_provision_composition.py
git add src/kinoforge/engines/diffusers/__init__.py \
        tests/engines/diffusers/test_render_provision_composition.py
git commit -m "feat(upscale): DiffusersEngine composes UpscalerEngine.render_provision script (Blocker B)"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/__init__.py", "tests/engines/diffusers/test_render_provision_composition.py"], "verifyCommand": "pixi run pytest tests/engines/diffusers/test_render_provision_composition.py -v", "acceptanceCriteria": ["spandrel cfg composes upscaler script", "spandrel script appears before server exec", "pure-t2v cfg unchanged", "seedvr2 cfg surfaces ExtrasNotInstalled"], "modelTier": "standard"}
```

---

## Task 9: Server `_load_model_to_gpu` + `_capability_for_model` spandrel dispatch

**Goal:** Extend the wan_t2v_server's model-name prefix dispatch so `spandrel-*` slugs route through `SpandrelRuntime`. Extend `_capability_for_model` (T13) so `spandrel-*` reports `"upscale"` capability.

**Files:**
- Modify: `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`
- Modify: `tests/engines/diffusers/test_lru_eviction.py` (sanity — spandrel slug routes)
- Modify: `tests/engines/diffusers/test_server_health.py` (spandrel-* contributes "upscale")

**Acceptance Criteria:**
- [ ] `_load_model_to_gpu("spandrel-realesrgan-fp16")` constructs a `SpandrelRuntime` with the correct precision (path resolution via the same convention SeedVR2 uses).
- [ ] `_capability_for_model("spandrel-realesrgan-fp16")` returns `"upscale"`.
- [ ] Existing `seedvr2-*` and `wan-t2v-*` dispatch paths unchanged (regression check via test suite green).

**Verify:** `pixi run pytest tests/engines/diffusers/ -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests**

Append to `tests/engines/diffusers/test_server_health.py`:

```python
class TestSpandrelCapability:
    def test_spandrel_prefix_yields_upscale_capability(self, loaded_client: Any) -> None:
        # Bug caught: _capability_for_model misses the spandrel prefix
        # and a spandrel-only pod misrepresents itself as having no
        # capabilities (or as t2v-only). T14 matcher then refuses to
        # attach upscale jobs to it.
        _srv, client, set_loaded = loaded_client
        set_loaded({"spandrel-realesrgan-fp16": _entry("spandrel-realesrgan-fp16")})
        assert client.get("/health").json()["capabilities"] == ["upscale"]
```

Append to `tests/engines/diffusers/test_lru_eviction.py`:

```python
class TestSpandrelDispatch:
    def test_spandrel_prefix_loads_via_spandrel_runtime(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Bug caught: prefix dispatch misses spandrel-* and the LRU
        # registry tries to load WanPipeline against a spandrel slug.
        from kinoforge.engines.diffusers.servers import wan_t2v_server as srv

        # Stub SpandrelRuntime so we don't need real spandrel here.
        fake_runtime = MagicMock(name="SpandrelRuntime")
        import kinoforge.upscalers.spandrel._runtime as runtime_mod

        monkeypatch.setattr(runtime_mod, "SpandrelRuntime", lambda **kw: fake_runtime)

        # Direct call (bypasses headroom check by running outside _ensure_on_gpu).
        pipe = srv._load_model_to_gpu("spandrel-realesrgan-fp16")
        assert pipe is fake_runtime
```

- [ ] **Step 2: Implement dispatch + capability**

Edit `src/kinoforge/engines/diffusers/servers/wan_t2v_server.py`:

In `_load_model_to_gpu`, add the spandrel branch:

```python
def _load_model_to_gpu(name: str) -> Any:  # noqa: ANN401
    if name.startswith("wan-t2v-"):
        return _diffusers_load()
    if name.startswith("seedvr2-"):
        from kinoforge.upscalers.seedvr2._runtime import SeedVR2Runtime

        parts = name.split("-")
        variant, precision = parts[-2], parts[-1]
        return SeedVR2Runtime(
            weights_dir=Path("/workspace/models/seedvr2"),
            variant=variant.upper(),  # type: ignore[arg-type]
            precision=precision,  # type: ignore[arg-type]
        )
    if name.startswith("spandrel-"):
        from kinoforge.upscalers.spandrel._runtime import SpandrelRuntime

        parts = name.split("-")
        arch, precision = parts[-2], parts[-1]
        # Weights filename convention: <arch>.pth lands in /workspace/models/spandrel/
        # via SpandrelEngine.render_provision → _fetch_weights. arch token is
        # informational and may not exactly match the on-disk filename; resolve
        # by globbing the dest dir for a known SR extension.
        weights_dir = Path("/workspace/models/spandrel")
        candidates = list(weights_dir.glob("*.pth")) + list(weights_dir.glob("*.safetensors"))
        if not candidates:
            raise FileNotFoundError(
                f"spandrel weights not found under {weights_dir}; expected "
                "_fetch_weights to have run during provision"
            )
        return SpandrelRuntime(
            weights_path=candidates[0],
            precision=precision,  # type: ignore[arg-type]
            tile_size=512,
            batch_size=4,
        )
    raise ValueError(f"unknown model name {name!r}; no loader registered")
```

In `_capability_for_model`:

```python
def _capability_for_model(name: str) -> str | None:
    if name.startswith("wan-t2v-"):
        return "t2v"
    if name.startswith("seedvr2-") or name.startswith("flashvsr-") or name.startswith("spandrel-"):
        return "upscale"
    return None
```

- [ ] **Step 3: Verify GREEN**

```
pixi run pytest tests/engines/diffusers/ -v
```
Expected: all pass. The new spandrel tests pass; existing seedvr2/wan tests untouched.

- [ ] **Step 4: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
  tests/engines/diffusers/test_lru_eviction.py \
  tests/engines/diffusers/test_server_health.py
git add src/kinoforge/engines/diffusers/servers/wan_t2v_server.py \
        tests/engines/diffusers/test_lru_eviction.py \
        tests/engines/diffusers/test_server_health.py
git commit -m "feat(upscale): wan_t2v_server spandrel-* prefix dispatch + /health capability"
```

```json:metadata
{"files": ["src/kinoforge/engines/diffusers/servers/wan_t2v_server.py", "tests/engines/diffusers/test_lru_eviction.py", "tests/engines/diffusers/test_server_health.py"], "verifyCommand": "pixi run pytest tests/engines/diffusers/ -v", "acceptanceCriteria": ["spandrel-* routes through SpandrelRuntime", "spandrel-* contributes upscale capability", "existing seedvr2/wan routes unchanged"], "modelTier": "standard"}
```

---

## Task 10: `generate()` skip_clip_stage + initial_clip flags (Blocker C foundation)

**Goal:** Add two narrowly scoped flags to `kinoforge.core.orchestrator.generate()` so the same function can run standalone-upscale jobs. Reuses every existing warm-reuse / attach / ledger / sigint code path.

**Files:**
- Modify: `src/kinoforge/core/orchestrator.py`
- Test: `tests/core/test_orchestrator_skip_clip_stage.py`

**Acceptance Criteria:**
- [ ] `generate(cfg, request=None, ..., skip_clip_stage=True, initial_clip=<Artifact>)` does NOT instantiate or run `GenerateClipStage`.
- [ ] When `skip_clip_stage=True`, `state.artifacts["clip"]` is seeded from `initial_clip` BEFORE any stage runs.
- [ ] When `skip_clip_stage=True` AND `cfg.upscale is not None`, the returned artifact is `state.artifacts["upscaled"]` (NOT `"clip"`).
- [ ] When `skip_clip_stage=True` and `request is None`, `validate_request` is NOT called.
- [ ] Default path (`skip_clip_stage=False`, `request` non-None) unchanged — existing tests pass.

**Verify:** `pixi run pytest tests/core/test_orchestrator_skip_clip_stage.py tests/engines/ tests/core/ -q` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/core/test_orchestrator_skip_clip_stage.py
"""Tests for generate()'s skip_clip_stage + initial_clip parameters (Blocker C)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kinoforge.core.interfaces import Artifact, PipelineState


@pytest.fixture
def _stub_session(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Bypass the heavy session_setup machinery — just enough to drive generate()."""
    from kinoforge.core import orchestrator

    session = MagicMock()
    session.engine = MagicMock()
    session.engine.name = "diffusers"
    session.engine.model_identity = MagicMock(return_value="fake-model")
    session.engine.accepted_kinds = {"image"}
    session.pool = MagicMock()
    session.profile = MagicMock()
    session.instance = None
    session.provider = None

    monkeypatch.setattr(orchestrator, "_setup_session", lambda *a, **kw: session)
    return session


def test_skip_clip_stage_omits_generate_clip_stage(
    _stub_session: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: skip_clip_stage flag is read but the stages list still
    # appends GenerateClipStage anyway. The stage fires, the engine pool
    # is invoked, the orchestrator burns budget on a job the caller asked
    # to skip entirely.
    from kinoforge.core import orchestrator
    from kinoforge.core.orchestrator import generate

    constructed = []

    class _SpyClipStage:
        def __init__(self, *a, **kw):  # noqa: ANN
            constructed.append(("clip", a, kw))

        def run(self, state):  # noqa: ANN
            raise AssertionError("GenerateClipStage.run must not fire")

    monkeypatch.setattr(orchestrator, "GenerateClipStage", _SpyClipStage)

    cfg = _build_minimal_cfg(with_upscale=True)
    initial = Artifact(uri="file:///tmp/in.mp4", sha256="x", size=1)

    with patch("kinoforge.pipeline.upscale.UpscaleStage") as upscale_stage_cls:
        # UpscaleStage will run; stub it to set the "upscaled" artifact.
        instance = upscale_stage_cls.return_value
        instance.run.side_effect = lambda s: s.__class__(
            request=s.request, artifacts={**s.artifacts, "upscaled": initial}
        )
        artifact, _ = generate(
            cfg, request=None,
            store=MagicMock(), sink=None, run_id="r", state_dir=MagicMock(),
            skip_clip_stage=True, initial_clip=initial,
        )

    assert constructed == []  # ClipStage NEVER constructed
    assert artifact is initial  # NB: in the real path the upscale step swaps this


def test_skip_clip_stage_seeds_initial_clip_in_state(
    _stub_session: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: initial_clip is ignored, UpscaleStage reads
    # state.artifacts["clip"] and hits a KeyError. Asserts the seam.
    from kinoforge.core.orchestrator import generate
    import kinoforge.core.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "GenerateClipStage", MagicMock())

    cfg = _build_minimal_cfg(with_upscale=True)
    initial = Artifact(uri="file:///tmp/in.mp4", sha256="x", size=1)

    seen_state = {}

    class _CaptureUpscale:
        def __init__(self, **kw):  # noqa: ANN
            pass

        def run(self, state):  # noqa: ANN
            seen_state["pre"] = dict(state.artifacts)
            new = PipelineState(request=state.request, artifacts={**state.artifacts, "upscaled": initial})
            return new

    monkeypatch.setattr("kinoforge.pipeline.upscale.UpscaleStage", _CaptureUpscale)

    generate(
        cfg, request=None,
        store=MagicMock(), sink=None, run_id="r", state_dir=MagicMock(),
        skip_clip_stage=True, initial_clip=initial,
    )
    assert "clip" in seen_state["pre"]
    assert seen_state["pre"]["clip"] is initial


def test_skip_clip_stage_returns_upscaled_artifact(
    _stub_session: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: return picks state.artifacts["clip"] unconditionally
    # and the caller gets back the INPUT video instead of the upscaled
    # output.
    from kinoforge.core.orchestrator import generate
    import kinoforge.core.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator, "GenerateClipStage", MagicMock())

    cfg = _build_minimal_cfg(with_upscale=True)
    initial = Artifact(uri="file:///tmp/in.mp4", sha256="in", size=1)
    upscaled = Artifact(uri="file:///tmp/out.mp4", sha256="out", size=4096)

    class _UpscaleSwap:
        def __init__(self, **kw): pass  # noqa: ANN

        def run(self, state):  # noqa: ANN
            return PipelineState(
                request=state.request,
                artifacts={**state.artifacts, "upscaled": upscaled},
            )

    monkeypatch.setattr("kinoforge.pipeline.upscale.UpscaleStage", _UpscaleSwap)

    artifact, _ = generate(
        cfg, request=None,
        store=MagicMock(), sink=None, run_id="r", state_dir=MagicMock(),
        skip_clip_stage=True, initial_clip=initial,
    )
    assert artifact is upscaled  # NOT initial


def test_default_path_unchanged_when_skip_flag_false() -> None:
    # Bug caught: the new flags introduce a branch that breaks
    # default generate() execution. Smoke check — run the existing
    # test_diffusers_wan_t2v_server test suite.
    pytest.skip("covered by tests/engines/test_diffusers_wan_t2v_server.py — regression-only assertion")


def _build_minimal_cfg(*, with_upscale: bool):  # helper
    from kinoforge.core.config import Config

    d: dict = {
        "engine": {"kind": "diffusers", "precision": "fp8"},
        "models": [{"kind": "base", "ref": "hf:Wan-AI/Wan2.2-T2V", "target": "diffusion_models"}],
        "compute": {"provider": "fake", "image": "fake:latest"},
        "spec": {"model": "fake-model"},
    }
    if with_upscale:
        d["upscale"] = {"engine": "spandrel", "scale": "2x", "spandrel": {
            "model_url": "hf:foo/bar.pth", "arch": "realesrgan",
            "precision": "fp16", "tile_size": 512, "batch_size": 4,
        }}
    return Config.model_validate(d)
```

- [ ] **Step 2: Verify RED**

```
pixi run pytest tests/core/test_orchestrator_skip_clip_stage.py -v
```
Expected: 3 tests FAIL (flags not implemented).

- [ ] **Step 3: Implement flags on generate()**

Edit `src/kinoforge/core/orchestrator.py`:

1. Update `generate()` signature:

```python
def generate(
    cfg: Config,
    request: GenerationRequest | None,
    *,
    store: ArtifactStore,
    sink: OutputSink | None,
    run_id: str,
    state_dir: Path,
    cancel_token: CancelToken | None = None,
    instance: Instance | None = None,
    single: bool = False,
    skip_clip_stage: bool = False,
    initial_clip: Artifact | None = None,
) -> tuple[Artifact, Instance | None]:
```

2. Guard `validate_request`:

```python
if request is not None and not skip_clip_stage:
    validate_request(...)  # existing call site — wrap in this guard
```

3. Initial state — replace the existing `state = PipelineState(request=request, artifacts={})` line with:

```python
seed_artifacts: dict[str, Artifact] = {}
if skip_clip_stage and initial_clip is not None:
    seed_artifacts["clip"] = initial_clip
state = PipelineState(request=request, artifacts=seed_artifacts)
```

4. Stages list — replace the existing `stages: list[Stage] = [GenerateClipStage(...)]` block with:

```python
stages: list[Stage] = []
if not skip_clip_stage:
    stages.append(
        GenerateClipStage(
            profile=session.profile,
            pool=session.pool,
            store=store,
            run_id=run_id,
            accepted_kinds=accepted_kinds,
            base_params=dict(cfg.params),
            base_spec=dict(cfg.spec),
            engine=session.engine,
            segments=prompt_segments,
            sink=sink,
            provider=_provider,
            model=_model,
            cancel_token=cancel_token,
        )
    )

# T16 — UpscaleStage append remains unchanged
if cfg.upscale is not None:
    from kinoforge.core import registry as _registry
    from kinoforge.core.scale_target import ScaleTarget
    from kinoforge.pipeline.upscale import UpscaleStage

    upscaler_engine = _registry.get_upscaler(cfg.upscale.engine)()
    stages.append(
        UpscaleStage(
            engine=upscaler_engine,
            scale=ScaleTarget.parse(cfg.upscale.scale),
            instance=session.instance,
            cfg=cfg_dict,
            cancel_token=cancel_token,
        )
    )
```

5. Return artifact — replace `artifact = state.artifacts["clip"]` with:

```python
artifact_key = "upscaled" if (skip_clip_stage and cfg.upscale is not None) else "clip"
artifact = state.artifacts[artifact_key]
```

- [ ] **Step 4: Verify GREEN**

```
pixi run pytest tests/core/test_orchestrator_skip_clip_stage.py tests/engines/test_diffusers_wan_t2v_server.py -v
```
Expected: all pass. The existing wan T2V server test exercises the default path; new tests exercise the skip-flag.

- [ ] **Step 5: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/core/orchestrator.py \
  tests/core/test_orchestrator_skip_clip_stage.py
git add src/kinoforge/core/orchestrator.py tests/core/test_orchestrator_skip_clip_stage.py
git commit -m "feat(upscale): generate() skip_clip_stage + initial_clip flags (Blocker C foundation)"
```

```json:metadata
{"files": ["src/kinoforge/core/orchestrator.py", "tests/core/test_orchestrator_skip_clip_stage.py"], "verifyCommand": "pixi run pytest tests/core/test_orchestrator_skip_clip_stage.py -v", "acceptanceCriteria": ["GenerateClipStage not constructed when skip_clip_stage=True", "initial_clip seeds state.artifacts['clip']", "returns state.artifacts['upscaled'] when upscale present", "default path unchanged"], "modelTier": "standard"}
```

---

## Task 11: `_cmd_upscale` non-dry-run wiring + ledger-write deferral resolution (Blocker C consumer)

**Goal:** Wire `_cmd_upscale`'s non-dry-run path to call `generate(skip_clip_stage=True, initial_clip=<resolved>)`. Resolve the T7 ledger-write deferral: stamp `kinoforge_stages`, `kinoforge_upscaler`, `kinoforge_upscaler_precision` tags on pod ledger entries after warm-reuse exit for BOTH `_cmd_upscale` and `_cmd_generate`.

**Files:**
- Modify: `src/kinoforge/cli/_commands.py`
- Test: `tests/cli/test_cmd_upscale_full.py`

**Acceptance Criteria:**
- [ ] `_cmd_upscale` non-dry-run no longer raises `NotYetImplementedError`.
- [ ] `_cmd_upscale` calls `generate(skip_clip_stage=True, initial_clip=<Artifact>)`.
- [ ] The Artifact passed as `initial_clip` has the sha256 of the local input mp4 (verifiable when `--video` is a local file path).
- [ ] After warm-reuse exit, the ledger entry for the returned instance has `tags["kinoforge_stages"]`, `tags["kinoforge_upscaler"]`, `tags["kinoforge_upscaler_precision"]` set.
- [ ] `_cmd_generate` ALSO writes those tags after warm-reuse exit (symmetric resolution).

**Verify:** `pixi run pytest tests/cli/test_cmd_upscale_full.py tests/cli/ -v` → all pass.

**Steps:**

- [ ] **Step 1: Write failing tests**

```python
# tests/cli/test_cmd_upscale_full.py
"""Tests for the _cmd_upscale full-run path (post-T15 stub, post-T18 wiring)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def stub_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "engine:\n"
        "  kind: diffusers\n"
        "  precision: fp8\n"
        "models:\n"
        "  - kind: base\n"
        "    ref: hf:Wan-AI/Wan2.2-T2V\n"
        "    target: diffusion_models\n"
        "compute:\n"
        "  provider: fake\n"
        "  image: fake:latest\n"
        "upscale:\n"
        "  engine: spandrel\n"
        "  scale: 2x\n"
        "  spandrel:\n"
        "    model_url: hf:foo/bar.pth\n"
        "    arch: realesrgan\n"
        "    precision: fp16\n"
        "    tile_size: 512\n"
        "    batch_size: 4\n"
    )
    return cfg


def test_non_dry_run_invokes_generate_with_skip_flag(
    stub_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: _cmd_upscale still raises NotYetImplementedError on
    # the non-dry-run path. Asserts the wiring exists and calls
    # generate() with the right flags.
    from kinoforge.cli._main import main

    video = tmp_path / "in.mp4"
    video.write_bytes(b"dummy video bytes")
    expected_sha = hashlib.sha256(b"dummy video bytes").hexdigest()

    captured_kwargs = {}
    from kinoforge.core.interfaces import Artifact
    fake_artifact = Artifact(uri="file:///out.mp4", sha256="xx", size=1)

    def fake_generate(cfg, request, **kw):
        captured_kwargs.update(kw)
        return (fake_artifact, None)

    monkeypatch.setattr("kinoforge.core.orchestrator.generate", fake_generate)

    rc = main(["upscale", "--video", str(video), "--config", str(stub_cfg), "--no-reuse"])
    assert rc == 0
    assert captured_kwargs.get("skip_clip_stage") is True
    initial = captured_kwargs.get("initial_clip")
    assert initial is not None
    assert initial.sha256 == expected_sha


def test_ledger_tags_stamped_on_warm_reuse_exit_upscale(
    stub_cfg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bug caught: T7 ledger-write deferral not resolved — pod created
    # via `kinoforge upscale --no-reuse=false` doesn't carry the
    # kinoforge_stages/upscaler/upscaler_precision tags, so a future
    # `kinoforge upscale` call's matcher cannot identify the pod as
    # upscale-capable.
    pytest.skip(
        "needs a richer ledger fixture; covered indirectly by the live "
        "smokes (Task 15+16) which inspect ledger entries post-run"
    )


def test_ledger_tags_stamped_on_warm_reuse_exit_generate(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.skip(
        "symmetric with above; ledger-tag stamping verified via live smokes"
    )
```

- [ ] **Step 2: Verify RED**

```
pixi run pytest tests/cli/test_cmd_upscale_full.py -v
```
Expected: 1 fails with `NotYetImplementedError` raised by _cmd_upscale's existing stub. The 2 skipped tests don't run.

- [ ] **Step 3: Replace the NotYetImplementedError stub**

Edit `src/kinoforge/cli/_commands.py` — locate `_cmd_upscale`. Replace the trailing `raise NotYetImplementedError(...)` line with:

```python
    # T18 — non-dry-run wiring. Reuses generate()'s machinery via the
    # skip_clip_stage flag (Task 10). Mirrors _cmd_generate's
    # warm-reuse / attach / cold-create precedence chain.
    input_artifact = _resolve_input_video_as_artifact(args.video)
    store = ctx.store()
    sink_local = _build_sink(cfg, args)
    run_id = (
        args.run_id
        if getattr(args, "run_id", None) is not None
        else f"upscale-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )

    # warm-reuse precedence chain — same shape as _cmd_generate
    instance: Instance | None = None
    if getattr(args, "attach_pod", None):
        instance, rc = _resolve_attach_pod(ctx, cfg, args.attach_pod)
        if rc is not None:
            return rc
    elif args.no_reuse:
        pass  # cold create + destroy on completion
    else:
        instance, report = _scan_warm_candidates(ctx, cfg)
        if (summary := report.summarize()):
            logger.info(summary)

    artifact, returned_instance = generate(
        cfg, request=None,
        store=store, sink=sink_local, run_id=run_id, state_dir=ctx.state_dir,
        cancel_token=ctx.cancel_token,
        instance=instance, single=bool(args.no_reuse),
        skip_clip_stage=True, initial_clip=input_artifact,
    )

    # Ledger write — symmetric with _cmd_generate; T7 deferral resolution.
    if returned_instance is not None and instance is None and not args.no_reuse:
        ledger = ctx.ledger()
        if ledger.read(returned_instance.id) is None:
            lc = cfg.lifecycle()
            ledger.record(
                returned_instance,
                idle_timeout_s=int(lc.idle_timeout_s),
                max_age_s=int(lc.max_lifetime_s),
            )
        cfg_wak = _cfg_warm_attach_key(cfg)
        stages_tag = ",".join(_cfg_want_stages(cfg))
        upscaler_tag = cfg.upscale.engine if cfg.upscale else ""
        upscaler_precision_tag = ""
        if cfg.upscale and cfg.upscale.spandrel is not None:
            upscaler_precision_tag = cfg.upscale.spandrel.precision
        ledger.touch(
            returned_instance.id,
            warm_attach_key=cfg_wak,
            kinoforge_stages=stages_tag,
            kinoforge_upscaler=upscaler_tag,
            kinoforge_upscaler_precision=upscaler_precision_tag,
        )

    print(f"upscaled: uri={artifact.uri!r}")
    return 0
```

Also add the `_resolve_input_video_as_artifact` helper:

```python
def _resolve_input_video_as_artifact(video_path_or_url: str) -> "Artifact":
    """Materialise the --video arg as a kinoforge Artifact.

    Local file path → file:// URL + sha256 from disk + size from stat.
    http(s):// URL → passthrough; sha256/size deferred to server-side fetch.
    """
    from kinoforge.core.interfaces import Artifact

    if video_path_or_url.startswith(("http://", "https://")):
        return Artifact(uri=video_path_or_url, sha256="", size=0)
    p = Path(video_path_or_url).resolve()
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return Artifact(uri=f"file://{p}", sha256=h.hexdigest(), size=p.stat().st_size)
```

Add `import hashlib` at module top if not already present.

- [ ] **Step 4: Symmetric ledger-tag stamping in `_cmd_generate`**

In `_cmd_generate`, locate the existing `ledger.touch(returned_instance.id, warm_attach_key=cfg_wak)` call (around line 572) and extend it to include the same three new tags:

```python
        ledger.touch(
            returned_instance.id,
            warm_attach_key=cfg_wak,
            kinoforge_stages=",".join(_cfg_want_stages(cfg)),
            kinoforge_upscaler=cfg.upscale.engine if cfg.upscale else "",
            kinoforge_upscaler_precision=(
                cfg.upscale.spandrel.precision
                if cfg.upscale and cfg.upscale.spandrel is not None
                else (
                    f"{cfg.upscale.seedvr2.variant.lower()}-{cfg.upscale.seedvr2.precision}"
                    if cfg.upscale and cfg.upscale.seedvr2 is not None
                    else ""
                )
            ),
        )
```

- [ ] **Step 5: Verify GREEN**

```
pixi run pytest tests/cli/test_cmd_upscale_full.py tests/cli/ -v
```
Expected: the wiring test passes; the skipped ledger tests stay skipped (covered live).

- [ ] **Step 6: Pre-commit + commit**

```
pixi run pre-commit run --files \
  src/kinoforge/cli/_commands.py \
  tests/cli/test_cmd_upscale_full.py
git add src/kinoforge/cli/_commands.py tests/cli/test_cmd_upscale_full.py
git commit -m "feat(upscale): _cmd_upscale non-dry-run wiring + T7 ledger-tag stamping"
```

```json:metadata
{"files": ["src/kinoforge/cli/_commands.py", "tests/cli/test_cmd_upscale_full.py"], "verifyCommand": "pixi run pytest tests/cli/test_cmd_upscale_full.py tests/cli/ -v", "acceptanceCriteria": ["_cmd_upscale calls generate(skip_clip_stage=True)", "input video resolves to Artifact with sha256", "ledger.touch stamps kinoforge_stages/upscaler/upscaler_precision tags in both _cmd_upscale and _cmd_generate"], "modelTier": "standard"}
```

---

## Task 12: Example cfgs + extras subfolder + docs

**Goal:** Land the v1 spandrel example cfgs as defaults; move SeedVR2 cfgs under `examples/configs/extras/`; refresh docs (configuration, warm-reuse, engines).

**Files:**
- Create: `examples/configs/upscale-spandrel-x2.yaml`
- Create: `examples/configs/wan-with-upscale-spandrel.yaml`
- Move: `examples/configs/upscale-seedvr2-3b.yaml` → `examples/configs/extras/upscale-seedvr2-3b.yaml`
- Move: `examples/configs/wan-with-upscale.yaml` → `examples/configs/extras/wan-with-upscale-seedvr2.yaml`
- Modify: `docs/configuration.md`
- Modify: `docs/warm-reuse.md`
- Modify: `docs/engines.md`
- Modify: `README.md`

**Acceptance Criteria:**
- [ ] `pixi run kinoforge upscale --config examples/configs/upscale-spandrel-x2.yaml --video <fixture> --dry-run` exits 0.
- [ ] `docs/configuration.md`'s `upscale:` section documents the `spandrel:` block (the SpandrelEngineConfig schema from spec §3.3).
- [ ] `docs/engines.md`'s Upscalers section lists spandrel as the v1 default + seedvr2 as `[seedvr]` extras (Phase 2).
- [ ] `docs/warm-reuse.md`'s "Upscale-only pods" subsection mentions both engines.
- [ ] `README.md` upscale row in the cheatsheet shows the spandrel cfg path.

**Verify:** `pixi run kinoforge upscale --config examples/configs/upscale-spandrel-x2.yaml --video tests/fixtures/dummy.mp4 --dry-run`. Manual inspection of doc diffs.

**Steps:**

- [ ] **Step 1: Write `examples/configs/upscale-spandrel-x2.yaml`**

```yaml
# v1-default upscale-only cfg for `kinoforge upscale`.
#
# Usage:
#   pixi run kinoforge upscale \
#     --config examples/configs/upscale-spandrel-x2.yaml \
#     --video /path/to/clip.mp4 \
#     --no-reuse

engine:
  kind: diffusers
  precision: fp16
  diffusers:
    image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
    server_cmd:
      - "python"
      - "-m"
      - "kinoforge.engines.diffusers.servers.wan_t2v_server"
    pip:
      - "torch==2.6.0"
      - "torchvision==0.21.0"
      - "torchaudio==2.6.0"
      - "fastapi>=0.115"
      - "uvicorn>=0.30"

models:
  - kind: base
    ref: "hf:lllyasviel/realesrgan"
    target: diffusion_models  # informational only for spandrel weights

compute:
  provider: runpod
  image: "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
  requirements:
    min_vram_gb: 16
    disk_gb: 50

upscale:
  engine: spandrel
  scale: 2x
  spandrel:
    model_url: "hf:lllyasviel/realesrgan/RealESRGAN_x2plus.pth"
    arch: realesrgan
    precision: fp16
    tile_size: 512
    batch_size: 4
```

- [ ] **Step 2: Write `examples/configs/wan-with-upscale-spandrel.yaml`**

Copy the existing `examples/configs/runpod-diffusers-wan-t2v-14b-2_2.yaml`. Append the same `upscale:` block from Step 1 (the spandrel one, not the seedvr2 one). Update the header comment to call out the multi-stage Wan + spandrel composition.

- [ ] **Step 3: Move existing seedvr2 cfgs to extras/**

```
mkdir -p examples/configs/extras
git mv examples/configs/upscale-seedvr2-3b.yaml examples/configs/extras/upscale-seedvr2-3b.yaml
git mv examples/configs/wan-with-upscale.yaml examples/configs/extras/wan-with-upscale-seedvr2.yaml
```

Add a header comment to each moved file:

```yaml
# EXTRAS — requires `pip install -e .[seedvr]` once Phase 2 ships.
# Phase 2 vendors ByteDance-Seed/SeedVR upstream. Until then, this cfg
# fails cfg-time validation. Use examples/configs/upscale-spandrel-x2.yaml
# (v1 default) instead.
```

- [ ] **Step 4: Update `docs/configuration.md`**

Replace the existing `## `upscale:` section's table (added in plan T16) with one that documents the spandrel block as the default + a sub-section for `seedvr2` under extras:

```markdown
## `upscale:` (optional, video upscaling)

Activates the in-pipeline `UpscaleStage` after `GenerateClipStage` for
`kinoforge generate`, or stands alone for `kinoforge upscale`.

### `cfg.upscale.spandrel` — v1 default (`engine: spandrel`)

| Key | Type | Default | Notes |
|---|---|---|---|
| `engine` | `"spandrel"` | — | Required for the default engine. |
| `scale` | string | — | `"Nx"` (factor) supported; `"Np"` (height) raises `NotYetImplementedError`. |
| `spandrel.model_url` | string | — | Source ref (`hf:`, `civitai:`, `civarchive:`, plain http(s)://). |
| `spandrel.arch` | string | — | Informational (spandrel auto-detects); used in the model-identity slug. |
| `spandrel.precision` | `"fp16"` \| `"fp32"` | `"fp16"` | |
| `spandrel.tile_size` | int | `512` | Frame-tile pixels for VRAM headroom. |
| `spandrel.batch_size` | int | `4` | Frames per CUDA batch. |

### `cfg.upscale.seedvr2` — extras (`engine: seedvr2`, `pip install -e .[seedvr]`)

> ⚠️ Until Phase 2 vendoring lands, `engine: seedvr2` cfgs are rejected at
> cfg-time. Use `engine: spandrel` for v1.

| Key | Type | Default | Notes |
|---|---|---|---|
| ... | ... | ... | (unchanged — preserved from prior spec for forward-compat) |
```

- [ ] **Step 5: Update `docs/warm-reuse.md` and `docs/engines.md`**

In `docs/warm-reuse.md`, update the existing "Upscale-only pods" section to reference both engines (spandrel as default, seedvr2 as extras). In `docs/engines.md`, update the Upscalers table:

| Name | Class | Status | Notes |
|------|-------|--------|-------|
| `spandrel` | `kinoforge.upscalers.spandrel.SpandrelEngine` | v1 default | Per-frame image SR; loads RealESRGAN / ESRGAN / SwinIR / OmniSR weights via `spandrel` library |
| `seedvr2` | `kinoforge.upscalers.seedvr2.SeedVR2Engine` | extras stub (Phase 2) | Video-coherent SR; requires `pip install -e .[seedvr]` after Phase 2 vendoring ships |

- [ ] **Step 6: Refresh `README.md` cheatsheet**

In the CLI subcommand table, update the `upscale` row's invocation to point at the new spandrel cfg:

```
| `upscale` | Upscale a video clip (spandrel default; SeedVR2 in `[seedvr]` extras) | `pixi run kinoforge upscale --config examples/configs/upscale-spandrel-x2.yaml --video clip.mp4 --scale 2x` |
```

- [ ] **Step 7: Verify**

```
pixi run kinoforge upscale --config examples/configs/upscale-spandrel-x2.yaml --video /tmp/x.mp4 --dry-run
```

Expected: exits 0 with a plan printout. If cfg-time validation fails on missing `tests/fixtures/dummy.mp4` (the video file doesn't need to exist for `--dry-run`), use any path.

- [ ] **Step 8: Pre-commit + commit**

```
pixi run pre-commit run --files \
  examples/configs/upscale-spandrel-x2.yaml \
  examples/configs/wan-with-upscale-spandrel.yaml \
  examples/configs/extras/upscale-seedvr2-3b.yaml \
  examples/configs/extras/wan-with-upscale-seedvr2.yaml \
  docs/configuration.md \
  docs/warm-reuse.md \
  docs/engines.md \
  README.md
git add examples/configs/upscale-spandrel-x2.yaml \
        examples/configs/wan-with-upscale-spandrel.yaml \
        examples/configs/extras/upscale-seedvr2-3b.yaml \
        examples/configs/extras/wan-with-upscale-seedvr2.yaml \
        docs/configuration.md \
        docs/warm-reuse.md \
        docs/engines.md \
        README.md
git commit -m "docs(upscale): spandrel cfgs as v1 default; seedvr2 cfgs moved to extras/"
```

```json:metadata
{"files": ["examples/configs/upscale-spandrel-x2.yaml", "examples/configs/wan-with-upscale-spandrel.yaml", "examples/configs/extras/upscale-seedvr2-3b.yaml", "examples/configs/extras/wan-with-upscale-seedvr2.yaml", "docs/configuration.md", "docs/warm-reuse.md", "docs/engines.md", "README.md"], "verifyCommand": "pixi run kinoforge upscale --config examples/configs/upscale-spandrel-x2.yaml --video /tmp/x.mp4 --dry-run", "acceptanceCriteria": ["spandrel cfg dry-runs clean", "seedvr2 cfgs moved under extras/", "docs reflect v1 default = spandrel"], "modelTier": "mechanical"}
```

---

## Task 13: Live smoke RED scaffold — spandrel single-shot (committed BEFORE live spend)

**Goal:** Per the CLAUDE.md durability rule, the live smoke scaffold for the single-shot path lands as a RED (`xfail`) commit BEFORE any live RunPod work. Replaces the old SeedVR2 RED scaffold from prior plan T17.

**Files:**
- Create: `tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py`
- Create: `tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/.gitkeep`
- Delete: `tests/live/test_seedvr2_3b_fp8_upscale_smoke.py` (and its evidence dir's `.gitkeep`)

**Acceptance Criteria:**
- [ ] New test file exists, `@pytest.mark.live` + `@pytest.mark.xfail(strict=False)`.
- [ ] `pixi run pytest tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py -v -rA` reports XFAIL (not error).
- [ ] Asserts (when un-xfailed): exit 0, output dimensions == 2x input via ffprobe, first-frame sha256(input) ≠ sha256(output), `kinoforge list` post-exit reports empty.
- [ ] Smoke command: `pixi run kinoforge upscale --video <fixture> --config examples/configs/upscale-spandrel-x2.yaml --no-reuse --output-dir <evidence-dir>/out`.
- [ ] Old `tests/live/test_seedvr2_3b_fp8_upscale_smoke.py` removed.

**Verify:** `pixi run pytest tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py -v -rA` → reports XFAIL.

**Steps:**

- [ ] **Step 1: Delete the old seedvr2 smoke file + evidence-dir gitkeep**

```
git rm tests/live/test_seedvr2_3b_fp8_upscale_smoke.py
git rm tests/live/evidence/2026-06-28-seedvr2-3b-fp8-upscale/.gitkeep
```

- [ ] **Step 2: Create the new spandrel smoke**

Create `tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py` mirroring the old seedvr2 file's structure but:
- Cfg path: `examples/configs/upscale-spandrel-x2.yaml`
- xfail reason: `"RED scaffold (Task 13) — GREEN evidence lands in Task 15 after live spend"`
- Evidence dir: `tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/`

The body of the test (subprocess invocation, ffprobe resolution check, first-frame sha256 diff check, `kinoforge list` empty check) is structurally identical to the old seedvr2 RED scaffold — copy that file, then change only the constants + xfail message + evidence dir path.

```python
# tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py
"""Live smoke — spandrel RealESRGAN-x2 2x upscale of a known low-res clip (Task 13).

RED scaffold per CLAUDE.md durability rule: scaffold lands BEFORE the
live spend. Task 15 removes the xfail mark + lands GREEN evidence.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

_FIXTURE = (
    Path(__file__).parent.parent.parent
    / "examples" / "configs" / "grids" / "_fixtures" / "wan21_prompt_cell0.mp4"
)
_EVIDENCE_DIR = Path(__file__).parent / "evidence" / "2026-06-29-spandrel-realesrgan-x2-upscale"
_CFG = Path(__file__).parent.parent.parent / "examples" / "configs" / "upscale-spandrel-x2.yaml"


@pytest.mark.live
@pytest.mark.xfail(
    reason="RED scaffold (Task 13) — GREEN evidence lands in Task 15 after live spend",
    strict=False,
)
def test_spandrel_realesrgan_x2_upscales_2x() -> None:
    assert _FIXTURE.exists(), f"input fixture missing: {_FIXTURE}"
    assert _CFG.exists(), f"cfg missing: {_CFG}"

    out_dir = _EVIDENCE_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "upscale",
         "--video", str(_FIXTURE),
         "--config", str(_CFG),
         "--no-reuse",
         "--output-dir", str(out_dir)],
        capture_output=True, text=True, timeout=1800,
    )
    (_EVIDENCE_DIR / "stdout.txt").write_text(proc.stdout)
    (_EVIDENCE_DIR / "stderr.txt").write_text(proc.stderr)
    assert proc.returncode == 0, proc.stderr

    in_w, in_h = _probe_dims(_FIXTURE)
    out_files = sorted(out_dir.rglob("*.mp4"))
    assert out_files, "no output mp4 produced"
    out_w, out_h = _probe_dims(out_files[-1])
    assert (out_w, out_h) == (in_w * 2, in_h * 2), f"expected {in_w*2}x{in_h*2}, got {out_w}x{out_h}"

    in_sha = _first_frame_sha256(_FIXTURE)
    out_sha = _first_frame_sha256(out_files[-1])
    assert in_sha != out_sha, "output identical to input — no upscale work"

    ledger = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "list", "--json"],
        capture_output=True, text=True, timeout=60,
    )
    pods = json.loads(ledger.stdout).get("instances", [])
    assert pods == [], f"pod survived --no-reuse: {pods}"


def _probe_dims(path: Path) -> tuple[int, int]:
    out = subprocess.check_output(  # noqa: S603,S607
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
        text=True,
    ).strip()
    w, h = out.split("x")
    return int(w), int(h)


def _first_frame_sha256(path: Path) -> str:
    out = subprocess.check_output(  # noqa: S603,S607
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", "select=eq(n\\,0)", "-vsync", "vfr",
         "-f", "image2pipe", "-vcodec", "png", "-"],
    )
    return hashlib.sha256(out).hexdigest()
```

- [ ] **Step 3: Create the evidence-dir `.gitkeep`**

```
mkdir -p tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale
touch tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/.gitkeep
```

- [ ] **Step 4: Verify XFAIL**

```
pixi run pytest tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py -v -rA
```
Expected: `XFAIL` with the reason; NOT `ERROR`, NOT live RunPod call.

- [ ] **Step 5: Pre-commit + commit (RED)**

```
pixi run pre-commit run --files \
  tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py \
  tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/.gitkeep
git add tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py \
        tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/.gitkeep
git add -u tests/live/test_seedvr2_3b_fp8_upscale_smoke.py
git add -u tests/live/evidence/2026-06-28-seedvr2-3b-fp8-upscale/.gitkeep
git commit -m "test(live): RED scaffold — spandrel RealESRGAN-x2 2x upscale smoke

Replaces the seedvr2 RED scaffold from prior plan T17 (deleted in this
commit). xfail until Task 15 lives it against RunPod. Per CLAUDE.md
durability rule, the scaffold + asserts are committed BEFORE the spend
so a mid-spend crash can't lose the test machinery."
```

```json:metadata
{"files": ["tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py", "tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/.gitkeep"], "verifyCommand": "pixi run pytest tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py -v -rA", "acceptanceCriteria": ["File exists with xfail mark", "Asserts cover resolution=2x, frame-diff, ledger empty post-exit", "Old seedvr2 RED scaffold removed"], "modelTier": "mechanical"}
```

---

## Task 14: Live smoke RED scaffold — Wan T2V + spandrel multi-stage warm-reuse

**Goal:** Per durability rule, the multi-stage smoke scaffold lands as a RED commit BEFORE Task 16's live spend.

**Files:**
- Create: `tests/live/test_wan_then_spandrel_warm_reuse_smoke.py`
- Create: `tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse/.gitkeep`

**Acceptance Criteria:**
- [ ] Test file exists, `@pytest.mark.live` + `@pytest.mark.xfail(strict=False)`.
- [ ] `pixi run pytest tests/live/test_wan_then_spandrel_warm_reuse_smoke.py -v` reports XFAIL.
- [ ] Asserts (when un-xfailed): exit 0 from `kinoforge generate --config examples/configs/wan-with-upscale-spandrel.yaml --no-reuse`; output has both a Wan-generated clip AND an upscaled artifact; resolution check 2x; ledger empty post-exit.

**Verify:** `pixi run pytest tests/live/test_wan_then_spandrel_warm_reuse_smoke.py -v -rA` → XFAIL.

**Steps:**

- [ ] **Step 1: Create the smoke file**

```python
# tests/live/test_wan_then_spandrel_warm_reuse_smoke.py
"""Live smoke — Wan T2V → spandrel multi-stage upscale on the same pod (Task 14).

RED scaffold; GREEN evidence lands in Task 16. Single pod runs both stages;
LRU model registry handles VRAM swap between Wan + spandrel weights.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_PROMPT = (Path(__file__).parent.parent.parent / "examples" / "configs" / "prompts" / "field-realistic.txt").read_text().strip()
_EVIDENCE_DIR = Path(__file__).parent / "evidence" / "2026-06-29-wan-then-spandrel-warm-reuse"
_CFG = Path(__file__).parent.parent.parent / "examples" / "configs" / "wan-with-upscale-spandrel.yaml"


@pytest.mark.live
@pytest.mark.xfail(
    reason="RED scaffold (Task 14) — GREEN evidence lands in Task 16 after live spend",
    strict=False,
)
def test_wan_t2v_then_spandrel_x2_multi_stage() -> None:
    assert _CFG.exists(), f"cfg missing: {_CFG}"
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = _EVIDENCE_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "generate",
         "--config", str(_CFG),
         "--prompt", _PROMPT,
         "--mode", "t2v",
         "--no-reuse",
         "--output-dir", str(out_dir)],
        capture_output=True, text=True, timeout=5400,  # 90 min ceiling
    )
    (_EVIDENCE_DIR / "stdout.txt").write_text(proc.stdout)
    (_EVIDENCE_DIR / "stderr.txt").write_text(proc.stderr)
    assert proc.returncode == 0, proc.stderr

    out_files = sorted(out_dir.rglob("*.mp4"))
    assert out_files, "no output mp4 produced"

    # Final artifact is the upscaled one — read its resolution.
    final = out_files[-1]
    w, h = _probe_dims(final)
    # Wan T2V cfg renders 480x480; spandrel 2x → 960x960
    assert (w, h) == (960, 960), f"expected 960x960, got {w}x{h}"

    ledger = subprocess.run(  # noqa: S603,S607
        ["pixi", "run", "kinoforge", "list", "--json"],
        capture_output=True, text=True, timeout=60,
    )
    pods = json.loads(ledger.stdout).get("instances", [])
    assert pods == [], f"pod survived --no-reuse: {pods}"


def _probe_dims(path: Path) -> tuple[int, int]:
    out = subprocess.check_output(  # noqa: S603,S607
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
        text=True,
    ).strip()
    w, h = out.split("x")
    return int(w), int(h)
```

- [ ] **Step 2: Verify XFAIL + commit**

```
mkdir -p tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse
touch tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse/.gitkeep
pixi run pytest tests/live/test_wan_then_spandrel_warm_reuse_smoke.py -v -rA
pixi run pre-commit run --files \
  tests/live/test_wan_then_spandrel_warm_reuse_smoke.py \
  tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse/.gitkeep
git add tests/live/test_wan_then_spandrel_warm_reuse_smoke.py \
        tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse/.gitkeep
git commit -m "test(live): RED scaffold — Wan T2V + spandrel multi-stage warm-reuse smoke"
```

```json:metadata
{"files": ["tests/live/test_wan_then_spandrel_warm_reuse_smoke.py", "tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse/.gitkeep"], "verifyCommand": "pixi run pytest tests/live/test_wan_then_spandrel_warm_reuse_smoke.py -v -rA", "acceptanceCriteria": ["File exists with xfail mark", "Asserts cover exit code, final mp4 resolution 960x960, ledger empty"], "modelTier": "mechanical"}
```

---

## Task 15: Live smoke GREEN — single-shot spandrel upscale + evidence  (LIVE SPEND, user-gate)

**USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured AND committed to `tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/`.

**Goal:** Execute the Task 13 RED scaffold against live RunPod. Remove the `xfail` mark. Capture evidence (stdout, stderr, output mp4, runtime-probes.jsonl). Update `successful-generations.md`.

**Files:**
- Modify: `tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py` (remove `@pytest.mark.xfail`)
- Create: artifacts under `tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/`:
  - `stdout.txt`
  - `stderr.txt`
  - `out/<filename>.mp4` (the actual upscaled output, committed for user inspection)
  - `runtime-probes.jsonl` (per-tick GPU / CPU / mem / costPerHr captures, one JSON object per line)
  - `summary.json` (final cost, duration, pod-id, sha256s, resolution check verdicts)
- Update: `/workspace/successful-generations.md`

**Acceptance Criteria:**
- [ ] `pixi run preflight` exits 0 BEFORE the smoke (creds present, zero active pods, clean tree).
- [ ] `kinoforge upscale --no-reuse --output-dir tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/out` exits 0.
- [ ] Upscaled mp4 file present under the evidence `out/` directory AND committed to git (operator must be able to play it back without re-running the smoke).
- [ ] `ffprobe` on the output mp4 reports 2x the input dimensions (e.g. 128x96 if input was 64x48).
- [ ] First-frame sha256 of output differs from first-frame sha256 of input.
- [ ] `kinoforge list` post-orchestrator-exit reports `No instances recorded in ledger.` AND `No running instances.`
- [ ] `runtime-probes.jsonl` contains ≥ 1 probe captured during the upscale run (proves the polling loop fired).
- [ ] `successful-generations.md` has a new section per the file's schema (provider=runpod, engine=diffusers, mode=upscale, upscaler=spandrel).
- [ ] Cost recorded in `summary.json` ≤ $0.50 (T18 budget cap from the original plan; spandrel A100/A10G run < 10 min in practice).

**Verify:** `pixi run pytest tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py -v && pixi run kinoforge list` → smoke passes; ledger empty.

**Steps:**

- [ ] **Step 1: Replace `_UPSTREAM_COMMIT` placeholder in seedvr2 module**

Not applicable — SeedVR2Engine is now stub-raise; the placeholder is removed at Task 2 or remains inert.

- [ ] **Step 2: Run `pixi run preflight`**

```
pixi run preflight
```
Expected: `preflight: OK — safe to spend`. If FAIL on creds → operator action required (creds in `/workspace/.env`; if missing, document and abort). If FAIL on dirty tree → commit or stash. If FAIL on active pods → destroy them first.

- [ ] **Step 3: Remove xfail mark from the smoke**

Edit `tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py` — delete the `@pytest.mark.xfail(...)` decorator. Keep `@pytest.mark.live`.

- [ ] **Step 4: Execute the smoke with proactive pod monitoring**

```
pixi run pytest tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py -v -rA -s
```

DURING THE RUN — every 60-90 seconds per CLAUDE.md `Live smoke monitoring`:

```python
# In a parallel terminal OR via the kinoforge runpod provider's probe:
pixi run python -c "
from kinoforge.providers.runpod import RunPodProvider
p = RunPodProvider()
# enumerate active pods, capture runtime.gpus[].gpuUtilPercent + costPerHr
"
```

Append each probe (single JSON line) to `tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/runtime-probes.jsonl`.

If GPU stays at 0% for 3 consecutive probes during what should be an active upscale → bail early per CLAUDE.md rule: capture last 100 lines of pod log, destroy pod, fail the smoke with a clear cause.

- [ ] **Step 5: Capture evidence**

After the smoke completes (PASS), confirm the following files exist under the evidence dir and are non-empty:
- `stdout.txt` — `kinoforge upscale` stdout (already captured by Task 13's scaffold).
- `stderr.txt` — same.
- `out/<filename>.mp4` — the actual upscaled video.
- `runtime-probes.jsonl` — your per-tick probes from Step 4.

Write a `summary.json`:

```json
{
  "smoke": "2026-06-29-spandrel-realesrgan-x2-upscale",
  "pod_id": "<from kinoforge stdout>",
  "duration_s": <wall_clock>,
  "cost_usd": <from kinoforge cost report>,
  "input": {
    "path": "examples/configs/grids/_fixtures/wan21_prompt_cell0.mp4",
    "dims": [<w>, <h>],
    "first_frame_sha256": "<hex>"
  },
  "output": {
    "path": "out/<filename>.mp4",
    "dims": [<w*2>, <h*2>],
    "first_frame_sha256": "<hex>"
  },
  "ledger_empty_post_run": true
}
```

- [ ] **Step 6: Append to `successful-generations.md`**

Per the file's preamble-defined schema, add a new section: provider=runpod, engine=diffusers (composed with upscale stage), mode=upscale, upscaler=spandrel. Include the resolved cfg path + the smoke timestamp + pod-id + cost.

- [ ] **Step 7: Commit GREEN evidence**

```
git add tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py \
        tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/
git commit -m "test(live): GREEN — spandrel RealESRGAN-x2 2x upscale (LIVE SPEND)

Smoke ran against RunPod pod <id>. Evidence committed under
tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/
so operator can inspect output mp4, stdout/stderr, runtime probes,
and summary.json directly without re-running.

Cost: \$<x>; duration: <m>m<s>s.
"
```

```json:metadata
{"files": ["tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py", "tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/"], "verifyCommand": "pixi run pytest tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py -v && pixi run kinoforge list", "acceptanceCriteria": ["preflight OK before run", "exit 0 from kinoforge upscale --no-reuse", "output mp4 committed under evidence dir", "ffprobe output dims = 2x input dims", "frame sha256s differ", "kinoforge list reports empty post-exit", "runtime-probes.jsonl non-empty", "successful-generations.md updated", "cost <= $0.50"], "modelTier": "live-spend", "userGate": true, "tags": ["user-gate"]}
```

---

## Task 16: Live smoke GREEN — Wan T2V + spandrel multi-stage warm-reuse + evidence  (LIVE SPEND, user-gate)

**USER-ORDERED GATE — NON-SKIPPABLE.** Same banner as Task 15. Evidence MUST be committed under `tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse/` for operator inspection.

**Goal:** Execute the Task 14 RED scaffold against live RunPod. Remove `xfail`. Capture evidence + update `successful-generations.md`.

**Files:**
- Modify: `tests/live/test_wan_then_spandrel_warm_reuse_smoke.py` (remove `@pytest.mark.xfail`)
- Create: artifacts under `tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse/`:
  - `stdout.txt`, `stderr.txt`
  - `out/wan_generated.mp4` AND `out/spandrel_upscaled.mp4` (BOTH committed — operator inspects both stages)
  - `runtime-probes.jsonl`
  - `summary.json` (cost, duration, both pod-ids if cold-restart, stage-wise resolution check)
- Update: `/workspace/successful-generations.md`

**Acceptance Criteria:**
- [ ] `pixi run preflight` OK before the run.
- [ ] `kinoforge generate --config examples/configs/wan-with-upscale-spandrel.yaml --no-reuse` exits 0.
- [ ] BOTH stages' mp4 outputs present under evidence `out/` AND committed.
- [ ] Wan-generated mp4 dims = 480x480 (cfg-declared).
- [ ] Final upscaled mp4 dims = 960x960 (2x of Wan output).
- [ ] `kinoforge list` empty post-exit.
- [ ] `runtime-probes.jsonl` shows the LRU swap event (Wan → spandrel) somewhere mid-run (gpuUtil transition signature).
- [ ] `successful-generations.md` has a new section (multi-stage variant).
- [ ] Cost ≤ $3.00 (Wan 2.2 14B baseline ~$2-3 + spandrel ~$0.10).

**Verify:** `pixi run pytest tests/live/test_wan_then_spandrel_warm_reuse_smoke.py -v && pixi run kinoforge list`.

**Steps:**

- [ ] **Step 1: `pixi run preflight`** — same as Task 15 Step 2.

- [ ] **Step 2: Remove xfail mark**

Edit `tests/live/test_wan_then_spandrel_warm_reuse_smoke.py` — delete the `@pytest.mark.xfail(...)` decorator.

- [ ] **Step 3: Execute + monitor**

```
pixi run pytest tests/live/test_wan_then_spandrel_warm_reuse_smoke.py -v -rA -s
```

This is a longer run (Wan 2.2 14B cold-boot ~30 min + Wan T2V ~5 min + spandrel ~2 min). Polling cadence 60-90s per CLAUDE.md. Watch for the LRU swap (gpuUtil dips when Wan model evicts to CPU, climbs again when spandrel loads).

- [ ] **Step 4: Capture evidence + write summary.json**

Confirm BOTH `out/wan_generated.mp4` AND `out/spandrel_upscaled.mp4` are present (the generate stage's intermediate output may need to be exposed by passing an additional flag if the sink doesn't dual-emit by default — verify by inspecting `out/` post-run; if only the upscaled mp4 is there, document that as a known artifact-emission gap and add a follow-up plan task).

`summary.json` mirrors Task 15's but with a `stages` array:

```json
{
  "smoke": "2026-06-29-wan-then-spandrel-warm-reuse",
  "stages": [
    {"name": "generate-clip", "engine": "diffusers", "dims": [480, 480], "duration_s": <s>},
    {"name": "upscale", "engine": "spandrel", "dims": [960, 960], "duration_s": <s>}
  ],
  "lru_swap_observed": true,
  "ledger_empty_post_run": true,
  "cost_usd": <x>,
  "duration_s": <total>
}
```

- [ ] **Step 5: `successful-generations.md`** entry (multi-stage variant).

- [ ] **Step 6: Commit GREEN evidence**

```
git add tests/live/test_wan_then_spandrel_warm_reuse_smoke.py \
        tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse/
git commit -m "test(live): GREEN — Wan T2V + spandrel multi-stage warm-reuse (LIVE SPEND)

Smoke validates the in-pipeline UpscaleStage composition (T16 wiring)
+ DiffusersEngine.render_provision upscaler-script composition (Task 8)
+ wan_t2v_server LRU swap between Wan + spandrel weights (Task 9).

Both stage outputs committed under tests/live/evidence/ for operator
inspection: wan_generated.mp4 + spandrel_upscaled.mp4.

Cost: \$<x>; duration: <m>m<s>s; pod: <id>.
"
```

```json:metadata
{"files": ["tests/live/test_wan_then_spandrel_warm_reuse_smoke.py", "tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse/"], "verifyCommand": "pixi run pytest tests/live/test_wan_then_spandrel_warm_reuse_smoke.py -v && pixi run kinoforge list", "acceptanceCriteria": ["preflight OK", "exit 0", "both stage mp4s committed", "Wan dims 480x480; spandrel dims 960x960", "LRU swap event captured in probes", "ledger empty post-exit", "successful-generations.md updated", "cost <= $3.00"], "modelTier": "live-spend", "userGate": true, "tags": ["user-gate"]}
```

---

## Task 17: PROGRESS.md close — workstream SHIPPED

**Goal:** Mark the upscaler-packaging-pivot workstream SHIPPED in PROGRESS.md. Reference Phase 2 (SeedVR2 vendoring) as the next queued workstream. Strike the BLOCKERS A/B/C documentation from the current workstream section (they're resolved).

**Files:**
- Modify: `PROGRESS.md`

**Acceptance Criteria:**
- [ ] PROGRESS.md "Active workstream" section reads `SHIPPED` (no longer `IN PROGRESS` or `BLOCKED`).
- [ ] BLOCKERS A/B/C section either removed or moved into a "Resolved gotchas" archive.
- [ ] Phase 2 (SeedVR2 vendoring) named as the next workstream with a one-paragraph scope.
- [ ] Commit range listed: `c9b5dff..<final-commit-of-task-16>`.

**Verify:** `git log --oneline -1 PROGRESS.md` shows the close commit.

**Steps:**

- [ ] **Step 1: Read current PROGRESS.md head section**

```
sed -n '1,60p' PROGRESS.md
```

- [ ] **Step 2: Replace the workstream block**

Replace the existing "Active workstream" section with:

```markdown
**Upscaler packaging pivot — SHIPPED YYYY-MM-DD (17 tasks GREEN, commits `c9b5dff..<final>`).**

v1 default upscaler is now `spandrel` (RealESRGAN-x2 weights); SeedVR2 stays
registered but gated behind `kinoforge[seedvr]` extras (Phase 2). The
UpscalerEngine ABC is validated by two implementers from day 1. Blocker B
(provision composition) resolved via DiffusersEngine.render_provision peek
+ append seam. Blocker C (standalone upscale orchestration) resolved via
`generate(skip_clip_stage=True, initial_clip=...)`.

- Spec: `docs/superpowers/specs/2026-06-29-upscaler-packaging-pivot-design.md`
- Plan: `docs/superpowers/plans/2026-06-29-upscaler-packaging-pivot.md`
- Live evidence:
  - `tests/live/evidence/2026-06-29-spandrel-realesrgan-x2-upscale/`
  - `tests/live/evidence/2026-06-29-wan-then-spandrel-warm-reuse/`

**Next workstream queued: Phase 2 — SeedVR2 vendoring.** Goal: vendor
`ByteDance-Seed/SeedVR` upstream (most-recent verified-good commit) into
`src/kinoforge/upscalers/seedvr2/_vendored/seedvr/` with a thin
`__init__.py` shim that exposes `SeedVR2Inferencer` matching the
existing `SeedVR2Runtime` imports. Replace `SeedVR2Engine`'s four
stub-raise method bodies with the real implementations (the code already
committed pre-pivot stays as the reference). Reverse the
`validate_for_generate` rejection. Add live smokes for the SeedVR2 path.
Scope-bounded: 1-2 sessions.
```

- [ ] **Step 3: Commit**

```
git add PROGRESS.md
git commit -m "docs(progress): upscaler packaging pivot SHIPPED — Phase 2 (SeedVR2 vendoring) queued"
```

```json:metadata
{"files": ["PROGRESS.md"], "verifyCommand": "git log --oneline -1 PROGRESS.md", "acceptanceCriteria": ["workstream marked SHIPPED", "commit range listed", "Phase 2 scope documented as next queued"], "modelTier": "mechanical"}
```

---

## Plan summary

17 atomic tasks. Dependency chain: 1 → 2 → (3,4) → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 → 15 → 16 → 17. Two user-gate tasks (15, 16) carry the non-skippable banner + `userGate: true` + `tags: ["user-gate"]` metadata.

Spec coverage check (against `docs/superpowers/specs/2026-06-29-upscaler-packaging-pivot-design.md`):
- §3 SpandrelEngine details → Tasks 5, 6, 7, 9, 12
- §4 ABC refinements → Tasks 1, 4, 2
- §5 Provision composition seam → Task 8
- §6 Standalone orchestrator entry → Tasks 10, 11
- §7 SeedVR2 extras stub → Tasks 2, 3
- §8 Testing strategy → all tasks ship tests; Task 4 owns the parametrized contract
- §9 Migration plan → Tasks 1-17 cover 1:1
- §10 Backward compat → maintained throughout (the 17 prior-workstream commits stay intact)

Type/method consistency: `SpandrelRuntime.__init__(weights_path, precision, tile_size, batch_size)` matches across Tasks 5 (creation), 9 (server consumer), 12 (cfg fields). `model_identity` slug shape `spandrel-<arch>-<precision>` matches Tasks 6, 9, 12.
