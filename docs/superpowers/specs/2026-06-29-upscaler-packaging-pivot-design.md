# Upscaler packaging pivot — v1 default = spandrel; SeedVR2 → `[seedvr]` extras

**Date:** 2026-06-29
**Status:** Approved (pre-plan)
**Supersedes:** `docs/superpowers/specs/2026-06-28-video-upscaling-design.md` §SeedVR2 packaging assumptions
**Companion workstream:** Phase 2 — SeedVR2 vendoring (separate spec, post-ship)

---

## 1. Why this exists

The original video-upscaling spec assumed `ByteDance-Seed/SeedVR` was pip-installable as
`seedvr @ git+https://...@<sha>`. Pre-spend due-diligence found the upstream repo carries
no `setup.py` and no `pyproject.toml` — it is research code structured as
`projects/inference_seedvr2_{3b,7b}.py` + `common/` + `models/` + `requirements.txt`. The
`pip install git+...@<any-sha>` step in `SeedVR2Engine.render_provision` would fail for
"no project metadata" against every commit on `main`.

Three architectural gaps surfaced from that single discovery (cross-ref
`PROGRESS.md` 2026-06-29 BLOCKERS A/B/C):

- **Blocker A** — upstream packaging.
- **Blocker B** — `DiffusersEngine.render_provision` does not compose
  `UpscalerEngine.render_provision` even when `cfg.upscale` is set, so a pod set up for an
  upscaler boots without the upscaler's deps + weights.
- **Blocker C** — `_cmd_upscale` non-dry-run raises `NotYetImplementedError`; the
  warm-reuse / cold-create / orchestrate-one-stage plumbing for standalone upscale was
  deferred in plan T15 with `"..."`.

This spec pivots the v1 default upscaler to a pip-installable engine and resolves
Blockers B + C. SeedVR2 stays in the registry, gated behind `kinoforge[seedvr]` extras
that Phase 2 will fill in.

## 2. Goals + non-goals

**Goals:**
- v1 ships with a working default upscaler end-to-end, including live spend.
- `UpscalerEngine` ABC validated by two concrete implementers — proves the abstraction
  is engine-agnostic rather than implicitly SeedVR-shaped.
- Provision composition seam established; future upscalers drop in without touching
  `DiffusersEngine`.
- Standalone `kinoforge upscale --video x.mp4` orchestration plumbed against the same
  warm-reuse + ledger machinery as `kinoforge generate`.

**Non-goals (this workstream):**
- SeedVR2 vendoring — Phase 2.
- Temporal-coherence post-pass — future.
- Upscale-only cfg shape with no `engine:` block — T5 deferral stays deferred.
- FlashVSR adapter — future; design when needed.

## 3. v1 default engine: `SpandrelEngine`

### 3.1 Runtime library choice

| Package | Last release | Verdict |
|---------|--------------|---------|
| `realesrgan` 0.3.0 | 2022-09-20 | Stale ~3.5 yrs; unmaintained |
| `basicsr` 1.4.2 | 2022-08-30 | Stale ~3.5 yrs |
| `spandrel` 0.4.2 | 2026-02-21 | Actively maintained; architecture-agnostic SR runtime |

`spandrel` wins on maintenance + flexibility: one runtime loads RealESRGAN, ESRGAN,
SwinIR, OmniSR, plus future architectures. Used by chaiNNer and ComfyUI as the SR
substrate. License: MIT.

### 3.2 Registry name + model naming

- Engine registers under `"spandrel"` via `register_upscaler`.
- Server-side model slugs follow `spandrel-<arch>-<precision>` — three dash-separated
  tokens, matching the existing `_load_model_to_gpu` parse contract
  (`parts[-2], parts[-1] == arch, precision`).
- v1 default model slug: `spandrel-realesrgan-fp16`. The `-x2` scale is **implicit**
  in the weights file — spandrel's `ModelLoader` reports `model.scale` after load, so
  the runtime knows the scale without it being in the slug. This keeps slugs at the
  three-token shape the server already parses.
- `_capability_for_model` in the server's `/health` payload (T13) adds the `spandrel-`
  prefix → `"upscale"` mapping.

### 3.3 Config block

`SpandrelEngineConfig` lives under `cfg.upscale.spandrel`:

```yaml
upscale:
  engine: spandrel
  scale: 2x
  spandrel:
    model_url: "hf:lllyasviel/realesrgan/RealESRGAN_x2plus.pth"
    arch: "realesrgan"   # informational; spandrel auto-detects from weights
    precision: fp16      # fp16 | fp32
    tile_size: 512       # frame-tile for VRAM headroom
    batch_size: 4        # frames per CUDA batch
```

`model_url` accepts any source supported by the resolver chain (`hf:`, `civitai:`,
`civarchive:`, plain `http(s)://`).

### 3.4 Runtime layer (`src/kinoforge/upscalers/spandrel/_runtime.py`)

```python
class SpandrelRuntime:
    def __init__(
        self,
        weights_path: Path,
        precision: Literal["fp16", "fp32"],
        tile_size: int,
        batch_size: int,
    ) -> None:
        from spandrel import ModelLoader  # lazy import
        self._model = ModelLoader().load_from_file(str(weights_path))
        self._scale = self._model.scale
        self._tile = tile_size
        self._batch = batch_size
        self._dtype = torch.float16 if precision == "fp16" else torch.float32

    def upscale(
        self, video_path: Path, scale: ScaleTarget, params: dict[str, Any]
    ) -> Path:
        if scale.kind == "height":
            raise NotYetImplementedError(...)
        if scale.value != self._scale:
            raise UnsupportedScaleError(...)
        # decode → batch frames → tiled inference → re-encode mp4
        ...

    def to(self, device: str) -> None:
        self._model.to(device)
```

### 3.5 Engine layer (`src/kinoforge/upscalers/spandrel/__init__.py`)

```python
class SpandrelEngine(UpscalerEngine):
    name = "spandrel"
    requires_compute = True
    requires_local_weights = True
    supported_scales = ()  # empty = runtime declares scale at load time

    def render_provision(self, cfg: dict) -> RenderedProvision:
        block = cfg["upscale"]["spandrel"]
        return RenderedProvision(
            script=(
                'pip install "spandrel>=0.4.2" "imageio[ffmpeg]>=2.34"\n'
                f"python -m kinoforge.upscalers.spandrel._fetch_weights "
                f"--url {block['model_url']} --dest /workspace/models/spandrel\n"
            ),
            run_cmd=[], image="", ports=[], env_required=[],
        )

    # provision / upscale / validate_spec / model_identity mirror the
    # SeedVR2Engine HTTP-aware shape (POST /upscale, poll /upscale/status).
```

### 3.6 Weights fetch CLI

`src/kinoforge/upscalers/spandrel/_fetch_weights.py` mirrors the existing SeedVR2
`_fetch_weights` pattern: accepts `--url <ref>`, dispatches the resolver chain,
materialises the weights file under `--dest`.

### 3.7 LRU registry compatibility

`SpandrelRuntime.to(device)` satisfies the LoadedModel contract from T11.
`vram_bytes` reported as parameter-count × dtype-bytes × fudge_factor (RealESRGAN-x2
fp16 ≈ 200 MB — order of magnitude smaller than Wan / SeedVR2; lots of headroom).

## 4. ABC refinements

### 4.1 Audit result

The current `UpscalerEngine` ABC (`src/kinoforge/core/interfaces.py:894`) holds up
against a second implementer with **no contract changes**. One convention is lifted
to explicit documentation:

> `model_identity(cfg)` and `render_provision(cfg)` read the engine's namespaced
> config block at `cfg["upscale"][self.name]`. Implementations MUST NOT hardcode their
> name as a key literal — use `self.name`.

The existing `SeedVR2Engine._build_payload` already uses this convention with
`cfg.get("upscale", {}).get("seedvr2", {})`; document it so SpandrelEngine and future
implementers follow.

### 4.2 New error class

```python
# src/kinoforge/core/errors.py
class ExtrasNotInstalled(KinoforgeError):
    def __init__(self, extras_name: str, install_hint: str) -> None:
        super().__init__(
            f"kinoforge[{extras_name}] extras not installed — {install_hint}"
        )
        self.extras_name = extras_name
        self.install_hint = install_hint
```

### 4.3 Parametrized contract test

`tests/core/test_upscaler_engine_contract.py` exercises every registered upscaler:

```python
@pytest.mark.parametrize("name", registry.upscaler_names())
def test_engine_implements_full_contract(name: str) -> None:
    engine = registry.get_upscaler(name)()
    assert isinstance(engine.name, str) and engine.name == name
    assert isinstance(engine.requires_compute, bool)
    assert isinstance(engine.requires_local_weights, bool)
    assert isinstance(engine.supported_scales, tuple)
    assert isinstance(engine.model_identity({}), str)
    if engine.supported_scales:
        engine.validate_spec(
            UpscaleJob(source=_fake_artifact(), scale=engine.supported_scales[0])
        )
```

SeedVR2's stub-raise mode is asserted by a separate test that verifies
`ExtrasNotInstalled` fires from `render_provision` / `provision` / `upscale` /
`validate_spec`.

## 5. Provision composition seam (Blocker B)

### 5.1 Where it lives

Inside `DiffusersEngine.render_provision`
(`src/kinoforge/engines/diffusers/__init__.py:848`), after the existing pip-line
assembly + before `embed_modules` decode + server `exec`:

```python
upscale_block = cfg.get("upscale")
if upscale_block:
    from kinoforge.core import registry as _registry
    upscaler_name = upscale_block.get("engine")
    if upscaler_name:
        upscaler = _registry.get_upscaler(upscaler_name)()
        upscale_rp = upscaler.render_provision(cfg)
        lines.append("# ---- upscaler provision (composed) ----")
        lines.extend(upscale_rp.script.split("\n"))
```

### 5.2 Why script-string append, not pip-list merge

Each engine's `render_provision` emits a multi-step script (pip install + weights
fetch + arch-specific setup). Treating the output as opaque lines lets each engine own
its bootstrap order. The Wan provision script is already heredoc-style; appending more
lines is structurally clean.

### 5.3 Engine-agnostic invariant

`DiffusersEngine.render_provision` knows nothing about WHICH upscaler. Only the
registry name + the engine's own `render_provision` output. FlashVSR drop-in (future)
gets composition for free; no edit to `DiffusersEngine` needed.

### 5.4 Bootstrap script order

1. `set -euo pipefail`
2. `exec > /tmp/bootstrap.log 2>&1`
3. Keep-alive trap
4. `pip install <Wan deps>`
5. `embed_modules` decode (kinoforge package files)
6. `# ---- upscaler provision (composed) ----`
7. `pip install <spandrel deps>`
8. `python -m kinoforge.upscalers.spandrel._fetch_weights ...`
9. `exec wan_t2v_server`

### 5.5 SeedVR2-in-extras-stub-mode interaction

`SeedVR2Engine.render_provision` raises `ExtrasNotInstalled` until Phase 2. The
composition seam re-raises. Cfg-time validation catches this BEFORE pod creation:

- New `validate_for_generate` check: when `cfg.upscale.engine == "seedvr2"`, dry-run
  `SeedVR2Engine().render_provision(cfg)` and convert `ExtrasNotInstalled` →
  `ValidationError` with the remediation message.

### 5.6 Tests

`tests/engines/diffusers/test_render_provision_composition.py`:

- `test_render_provision_composes_upscaler_script` — cfg has both `engine: diffusers`
  AND `upscale: spandrel`; assert `spandrel` token appears in `rp.script` AND appears
  BEFORE the `wan_t2v_server` exec line.
- `test_render_provision_skips_when_no_upscale_block` — idempotent skip.
- `test_seedvr2_composition_raises_extras_not_installed_until_phase2`.

## 6. Standalone `upscale_only` via `generate()` flags (Blocker C)

### 6.1 Design choice: flags on `generate()`, not new entry

Considered: separate `upscale_only()` entry vs flag on `generate()`. Picked flag —
the warm-reuse / attach / cold-create / sigint / sink / ledger machinery in
`generate()` is the SAME for upscale-only; a separate entry duplicates ~80 LOC that
must be kept in lockstep with `generate()` forever. Two narrowly scoped branches in
the existing function beat a permanent fork.

### 6.2 New parameters on `generate()`

```python
def generate(
    cfg: Config,
    request: GenerationRequest | None,   # None when skip_clip_stage=True
    *,
    store: ArtifactStore,
    sink: OutputSink | None,
    run_id: str,
    state_dir: Path,
    cancel_token: CancelToken | None = None,
    instance: Instance | None = None,
    single: bool = False,
    skip_clip_stage: bool = False,       # NEW
    initial_clip: Artifact | None = None,  # NEW
) -> tuple[Artifact, Instance | None]:
```

### 6.3 Branch points (4, narrowly scoped)

1. **Engine resolution** — when `skip_clip_stage` AND `cfg.engine is None`, skip
   GenerationEngine creation. (v1 workaround: cfg still carries an `engine:` block to
   satisfy validator; the upscale-only cfg in examples has the diffusers engine block
   for now. T5 deferral stays deferred — no actual code branch needed for v1.)
2. **Validate request** — skip `validate_request(request)` when `request is None`.
3. **Stages list assembly** — `stages = []` when `skip_clip_stage`; the existing
   `if cfg.upscale is not None: stages.append(UpscaleStage(...))` block remains.
4. **Initial state + return artifact key** —
   `state = PipelineState(request=request, artifacts={"clip": initial_clip})` when
   `initial_clip` non-None; `artifact = state.artifacts["upscaled" if skip_clip_stage else "clip"]`
   at return.

### 6.4 `_cmd_upscale` non-dry-run wiring

```python
if args.dry_run:
    _print_upscale_plan(...)
    return 0

cfg = ctx.cfg
input_artifact = _resolve_input_video_as_artifact(args.video, ctx.store())
store = ctx.store()
sink = _build_sink(cfg, args)
run_id = args.run_id or f"upscale-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

# warm-reuse precedence chain — same as _cmd_generate
instance: Instance | None = None
if args.attach_pod:
    instance, rc = _resolve_attach_pod(ctx, cfg, args.attach_pod)
    if rc is not None: return rc
elif args.no_reuse:
    pass  # cold create + destroy on completion
else:
    instance, report = _scan_warm_candidates(ctx, cfg)
    if (summary := report.summarize()): logger.info(summary)

artifact, returned_instance = generate(
    cfg, request=None,
    store=store, sink=sink, run_id=run_id, state_dir=ctx.state_dir,
    cancel_token=ctx.cancel_token,
    instance=instance, single=bool(args.no_reuse),
    skip_clip_stage=True,
    initial_clip=input_artifact,
)

if returned_instance is not None and instance is None and not args.no_reuse:
    # ledger.record + ledger.touch — same as _cmd_generate
    ...

print(f"upscaled: uri={artifact.uri!r}")
return 0
```

### 6.5 `_resolve_input_video_as_artifact` helper

Local file path → `file://` URL → `Artifact(uri=..., sha256=_sha256_file(path),
size=path.stat().st_size)`. `http(s)://` URL → passthrough with sha256/size deferred
to server-side fetch.

### 6.6 T7 ledger-write deferral resolution

When ledger.touch fires after `returned_instance` (warm-reuse exit), stamp
`kinoforge_stages`, `kinoforge_upscaler`, `kinoforge_upscaler_precision` tags
from `cfg.capability_key()`'s stages-derivation. Apply symmetrically in BOTH
`_cmd_upscale` AND `_cmd_generate` post-success paths.

## 7. SeedVR2 → `[seedvr]` extras stub

### 7.1 `pyproject.toml`

```toml
[project.optional-dependencies]
seedvr = []   # Phase 2 fills in: vendored upstream
```

### 7.2 SeedVR2Engine stub-raise mode

Module-level self-registration stays; constructor stays cheap (no upstream import).
All four ABC methods (`provision`, `render_provision`, `upscale`, `validate_spec`)
raise:

```python
raise ExtrasNotInstalled(
    extras_name="seedvr",
    install_hint=(
        "video-coherent upscaling (SeedVR2) pending Phase 2 vendoring; "
        "use cfg.upscale.engine = 'spandrel' for v1, or track the "
        "Phase 2 workstream"
    ),
)
```

`model_identity` keeps its pure-cfg-parse implementation (cheap, satisfies ABC
contract test).

`SeedVR2Runtime` + `_fetch_weights` stay committed but inert. Pre-existing
top-of-module `from seedvr.inference import ...` is moved INSIDE the `SeedVR2Runtime`
constructor so `import kinoforge.upscalers.seedvr2` is side-effect free.

### 7.3 Validation rejection

`validate_for_generate` rejects `cfg.upscale.engine == "seedvr2"` cfgs with
`ValidationError` (catches misconfig before pod work).

## 8. Testing strategy

### 8.1 New tests

- `tests/upscalers/test_spandrel_runtime.py` — frame loop, scale validation, tile/batch
- `tests/upscalers/test_spandrel_engine.py` — HTTP submit/poll mirror
- `tests/upscalers/test_spandrel_fetch_weights.py` — HF source dispatch
- `tests/engines/diffusers/test_render_provision_composition.py` — §5.6
- `tests/core/test_orchestrator_skip_clip_stage.py` — §6 branches
- `tests/cli/test_cmd_upscale_full.py` — replaces `NotYetImplementedError` tests
- `tests/upscalers/test_seedvr2_extras_stub.py` — `ExtrasNotInstalled` behavior
- `tests/core/test_upscaler_engine_contract.py` — parametrized ABC contract

### 8.2 Updated existing tests

- `tests/upscalers/test_seedvr2_engine.py` — most assertions exercise stub-raise paths
  now; keep the slim subset for `model_identity` (still functional). Rewrite the rest.
- `tests/test_adapters_upscale.py` — assert BOTH `"spandrel"` AND `"seedvr2"`
  registered after `import kinoforge._adapters`.

### 8.3 Live smokes

- `tests/live/test_seedvr2_3b_fp8_upscale_smoke.py` → rename to
  `tests/live/test_spandrel_realesrgan_x2_upscale_smoke.py`. xfail mark stays until
  live spend. Asserts unchanged (resolution=2x, frame-diff, ledger-empty post
  `--no-reuse`).
- New `tests/live/test_wan_then_spandrel_warm_reuse_smoke.py` for the multi-stage
  path. Same xfail-until-spend pattern.

## 9. Migration plan (rough — final sequencing in writing-plans)

| # | Commit | Touched |
|---|--------|---------|
| 1 | `ExtrasNotInstalled` error class | `core/errors.py` + tests |
| 2 | `SeedVR2Engine` stub-raise rewrite | `upscalers/seedvr2/__init__.py` + tests |
| 3 | Validate_for_generate seedvr2-extras rejection | `validation/checks.py` + tests |
| 4 | UpscalerEngine ABC contract test | `tests/core/test_upscaler_engine_contract.py` |
| 5 | `SpandrelRuntime` | `upscalers/spandrel/_runtime.py` + tests |
| 6 | `SpandrelEngine` HTTP-aware + self-register | `upscalers/spandrel/__init__.py` + tests |
| 7 | `_fetch_weights` CLI for spandrel | `upscalers/spandrel/_fetch_weights.py` + tests |
| 8 | `_adapters.py` self-register spandrel | `_adapters.py` + tests update |
| 9 | `DiffusersEngine.render_provision` composition | `engines/diffusers/__init__.py` + tests |
| 10 | Server `_load_model_to_gpu` + `_capability_for_model` spandrel dispatch | `engines/diffusers/servers/wan_t2v_server.py` + tests |
| 11 | `generate()` `skip_clip_stage` + `initial_clip` flags | `core/orchestrator.py` + tests |
| 12 | `_resolve_input_video_as_artifact` + `_cmd_upscale` full-run wiring | `cli/_commands.py` + tests |
| 13 | T7 ledger-write deferral wired in `_cmd_upscale` + `_cmd_generate` | `cli/_commands.py` + tests |
| 14 | `examples/configs/upscale-spandrel-x2.yaml` + `wan-with-upscale-spandrel.yaml`; SeedVR2 cfgs move to `examples/configs/extras/` | configs |
| 15 | Docs updates — configuration / warm-reuse / engines / README | docs |
| 16 | Live-smoke scaffold rename + retargeting to spandrel | `tests/live/` |
| 17 | T18 LIVE SPEND: spandrel single-shot smoke + evidence | `tests/live/evidence/` |
| 18 | T19 LIVE SPEND: Wan T2V → spandrel multi-stage warm-reuse + evidence | `tests/live/evidence/` |
| 19 | PROGRESS.md close — workstream SHIPPED; Phase 2 SeedVR2 vendoring queued | `PROGRESS.md` |

## 10. Backward compatibility

The 17 already-committed video-upscaling commits (`16cace5..9ad4cbe`) stay intact.
This workstream is additive on top plus narrow modifications (seedvr2 stub-raise,
render_provision composition, generate() flags, _cmd_upscale wiring). No `git revert`.
Original plan's T18 / T19 task slots are reused with a spandrel target.

## 11. Out of scope (re-stated)

- Phase 2 SeedVR2 vendoring (separate workstream).
- Temporal-coherence post-pass (future).
- Upscale-only cfg shape with no `engine:` block (T5 deferral stays deferred).
- FlashVSR adapter (future; may need separate temporal-model runtime, not a Spandrel
  variant).

## 12. Open risks

- **`spandrel` model-loader edge cases** — RealESRGAN-x2 specifically should be solid;
  newer architectures may need spandrel updates. Mitigation: pin `spandrel>=0.4.2`,
  add a `validate_for_generate` check that confirms the chosen model_url loads via a
  dry-run `ModelLoader().load_from_file` on cfg load.
- **VRAM headroom under tile_size=512 + batch_size=4** — A100 80GB has plenty; smaller
  cards may need tuning. Default cfg can ship conservative `tile_size=256, batch_size=2`
  if A40 / L40S coverage matters in v1.
- **Live spend lateness** — if T18 / T19 surface a runtime bug not caught by unit
  tests, fix budget eats into session time. Mitigation: T17 RED scaffold's contract
  is unchanged from the prior plan; the asserts are battle-tested.

## 13. Phase 2 — SeedVR2 vendoring (preview only; separate spec)

Phase 2 will:
- Vendor `ByteDance-Seed/SeedVR@6b061f467059` (or a more recent verified commit) into
  `src/kinoforge/upscalers/seedvr2/_vendored/seedvr/` with a thin `__init__.py` shim
  that exposes `SeedVR2Inferencer` matching the existing `SeedVR2Runtime` imports.
- Replace `[project.optional-dependencies] seedvr = []` with the actual list (pinned
  `torch`/`numpy` etc.).
- Replace the four stub-raise method bodies in `SeedVR2Engine` with the real
  implementations (largely the code that's already committed today, pre-pivot).
- Reverse the `validate_for_generate` rejection.
- Reverse the `test_seedvr2_extras_stub` assertions.
- Add live smokes for SeedVR2 path.

Phase 2 scope is bounded enough to ship in 1-2 sessions once v1 default lands.
